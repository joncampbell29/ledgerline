"""MCP server over data.db. Read-only; never opens secrets.db or calls Plaid.

    uv run mcp_server.py                        # stdio, for Claude Desktop / Claude Code
    claude mcp add ledgerline -- uv --directory ~/ledgerline run mcp_server.py

In Docker, see the MCP section of compose.yaml: the container gets data/ read-only,
a writable audit/ folder, and no network. secrets/ is never mounted.

Requires mcp>=2 (MCPServer). On mcp 1.x the same code works with
`from mcp.server.fastmcp import FastMCP as MCPServer`.
"""
import functools
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Clients launch this from an arbitrary cwd; default to the repo folder.
os.environ.setdefault("LEDGERLINE_DATA_DIR", str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

import db  # noqa: E402
import metrics  # noqa: E402

if not db.DATA_DB.exists():
    raise SystemExit(f"{db.DATA_DB} not found. Run `python sync.py` first.")

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
QUERY_MAX_ROWS = 500
QUERY_TIMEOUT_S = 5.0
# Separate from the data folder so data.db can be mounted read-only.
AUDIT_LOG = Path(os.environ.get("LEDGERLINE_AUDIT_DIR", str(db.DATA_DIR))) / "audit.jsonl"

mcp = MCPServer(
    "ledgerline",
    instructions=(
        "Personal finance data synced daily from the user's bank, card, brokerage and "
        "mortgage accounts. Prefer the specific tools (spending_by_category, "
        "search_transactions, upcoming_payments, ...) so numbers match the user's daily "
        "digest; use `query` only when none of them answers the question. "
        "Transaction amounts from Plaid are positive for money out and negative for money "
        "in; spending tools already convert to positive dollars. Dates are ISO YYYY-MM-DD. "
        "Today's date is not in the data; compute ranges relative to the date you know."
    ),
)

conn = db.data_conn(readonly=True)


def audited(fn):
    """Append one JSON line per tool call to audit.jsonl: time, tool, arguments, status,
    duration and result size. Results themselves are not logged (they hold your data).
    Never print() here: on stdio, stdout is the protocol channel."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "tool": fn.__name__, "args": kwargs}
        try:
            result = fn(*args, **kwargs)
            entry["status"] = "error" if isinstance(result, dict) and "error" in result else "ok"
            entry["result_bytes"] = len(json.dumps(result, default=str))
            return result
        except Exception as e:
            entry.update(status="exception", error=f"{type(e).__name__}: {e}")
            raise
        finally:
            entry["ms"] = round((time.monotonic() - start) * 1000, 1)
            try:
                with open(AUDIT_LOG, "a") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
            except OSError as e:  # don't fail the tool call over logging
                print(f"audit log write failed: {e}", file=sys.stderr)
    return wrapper


# ---- Overview ----------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY)
@audited
def daily_digest(as_of: str | None = None) -> dict:
    """Everything in the user's daily report for one date (default today): net worth and
    change since yesterday, account balances, yesterday's transactions, 7-day spending vs
    prior 7 days, month-to-date cash flow, payments due in 14 days, portfolio movers,
    unusual charges, and connections that need attention. Good first call for broad
    questions like "how am I doing?"."""
    return metrics.digest(as_of, conn=conn)


@mcp.tool(annotations=READ_ONLY)
@audited
def account_balances(days: int = 1, as_of: str | None = None) -> dict:
    """Net worth plus every account's latest balance and its change over the last `days`
    days. Credit card and loan balances are amounts owed (positive = debt)."""
    return {"net_worth": metrics.net_worth_change(days, as_of, conn),
            "accounts": metrics.balance_changes(days, as_of, conn)}


@mcp.tool(annotations=READ_ONLY)
@audited
def connection_health() -> list[dict]:
    """Status of each linked institution. status != 'ok' or stale = true means the data
    for that institution may be out of date; tell the user before relying on it."""
    return metrics.connection_health(conn=conn)


# ---- Spending ----------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY)
@audited
def spending_by_category(start: str, end: str) -> list[dict]:
    """Net spending per category between two dates (inclusive), largest first.
    Excludes income, transfers between own accounts, and card/loan payments, so nothing is
    double-counted. Refunds reduce the category total. Amounts are positive dollars."""
    return metrics.spending_by_category(start, end, conn)


@mcp.tool(annotations=READ_ONLY)
@audited
def spending_trend(days: int = 7, as_of: str | None = None) -> dict:
    """Spending per category for the last `days` days vs the `days` before that, with
    percent change. Use for "am I spending more than usual?" questions."""
    return metrics.spending_trend(days, as_of, conn)


@mcp.tool(annotations=READ_ONLY)
@audited
def cash_flow(start: str, end: str) -> dict:
    """Income, spending and net between two dates (inclusive). Spending uses the same
    rules as spending_by_category."""
    return metrics.cash_flow(start, end, conn)


@mcp.tool(annotations=READ_ONLY)
@audited
def top_merchants(start: str, end: str, limit: int = 10) -> list[dict]:
    """Merchants with the most net spending between two dates (inclusive)."""
    return metrics.top_merchants(start, end, limit, conn)


@mcp.tool(annotations=READ_ONLY)
@audited
def search_transactions(
    text: str | None = None,
    start: str | None = None,
    end: str | None = None,
    category: str | None = None,
    institution: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    limit: int = 50,
) -> dict:
    """Find individual transactions. All filters are optional and combined with AND.

    text: case-insensitive match on merchant or description (e.g. "uber").
    start / end: ISO dates, inclusive. Default is the last 90 days.
    category: Plaid primary category, e.g. FOOD_AND_DRINK, TRANSPORTATION, INCOME.
    institution: e.g. "Chase".
    min_amount / max_amount: on the raw amount (positive = money out).

    Returns matching rows (newest first, up to `limit`) plus the count and total of ALL
    matches, so use `total` for "how much did I spend on X" questions."""
    end_d = end or date.today().isoformat()
    start_d = start or (date.fromisoformat(end_d) - timedelta(days=90)).isoformat()
    where, params = ["t.date BETWEEN ? AND ?"], [start_d, end_d]
    if text:
        where.append("(t.merchant_name LIKE ? OR t.name LIKE ?)")
        params += [f"%{text}%", f"%{text}%"]
    if category:
        where.append("t.category = ?")
        params.append(category.upper())
    if institution:
        where.append("a.institution LIKE ?")
        params.append(f"%{institution}%")
    if min_amount is not None:
        where.append("t.amount >= ?")
        params.append(min_amount)
    if max_amount is not None:
        where.append("t.amount <= ?")
        params.append(max_amount)

    base = (" FROM transactions t JOIN accounts a ON a.account_id = t.account_id WHERE "
            + " AND ".join(where))
    count, total = conn.execute(f"SELECT COUNT(*), ROUND(COALESCE(SUM(t.amount), 0), 2){base}",
                                params).fetchone()
    rows = conn.execute(
        f"""SELECT t.date, COALESCE(t.merchant_name, t.name) AS merchant, t.name, t.amount,
                   t.category, t.category_detailed, t.pending, a.institution,
                   a.name AS account, a.mask{base}
            ORDER BY t.date DESC LIMIT ?""",
        params + [min(limit, QUERY_MAX_ROWS)]).fetchall()
    return {"start": start_d, "end": end_d, "count": count, "total": total,
            "transactions": [dict(r) for r in rows]}


@mcp.tool(annotations=READ_ONLY)
@audited
def unusual_transactions(days: int = 7, as_of: str | None = None) -> list[dict]:
    """Charges in the last `days` days flagged by rules: large (>= $500), spike (> 2.5x the
    merchant's average), new_merchant (first charge in 180 days and >= $100), or duplicate
    (same merchant, amount and day)."""
    return metrics.unusual_transactions(days, as_of, conn=conn)


# ---- Debt --------------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY)
@audited
def upcoming_payments(days: int = 14) -> list[dict]:
    """Credit card and loan payments due in the next `days` days, soonest first."""
    return metrics.upcoming_payments(days, conn=conn)


@mcp.tool(annotations=READ_ONLY)
@audited
def liabilities() -> list[dict]:
    """Latest details for every credit card and loan: balance, statement balance, minimum
    or monthly payment, due date, APR or interest rate, card utilization, mortgage escrow
    and year-to-date interest/principal paid."""
    return metrics.liabilities_summary(conn=conn)


# ---- Investments -------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY)
@audited
def portfolio(change_days: int = 1) -> dict:
    """Current holdings (ticker, quantity, price, value, cost basis, gain, weight) plus the
    portfolio's change over the last `change_days` days and the biggest movers."""
    return {"holdings": metrics.portfolio(conn=conn),
            "change": metrics.portfolio_change(change_days, conn=conn)}


@mcp.tool(annotations=READ_ONLY)
@audited
def investment_activity(start: str, end: str) -> dict:
    """Buys, sells, dividends, contributions and fees between two dates (inclusive), with
    total dividends. Plaid sign convention: positive amount = cash left the account."""
    return metrics.investment_activity(start, end, conn)


# ---- Raw SQL -----------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY)
@audited
def describe_schema() -> str:
    """CREATE statements for every table. Call this before writing SQL for `query`."""
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    return "\n\n".join(r[0] for r in rows)


_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE}


def _authorizer(action, *_):
    # Allow-list: plain reads only. Blocks ATTACH (which could open secrets.db), PRAGMA,
    # writes, and everything else, even though the connection is already read-only.
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def _query_conn():
    c = db.data_conn(readonly=True)
    c.set_authorizer(_authorizer)
    return c


qconn = _query_conn()


@mcp.tool(annotations=READ_ONLY)
@audited
def query(sql: str, limit: int = 200) -> dict:
    """Run one read-only SELECT (or WITH ... SELECT) against the finance database.
    Use only when no other tool answers the question. Results are capped at `limit` rows
    (max 500) and queries are stopped after 5 seconds. On error, the SQLite message is
    returned so you can fix the SQL and retry."""
    if not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
        return {"error": "Only SELECT queries are allowed."}

    deadline = time.monotonic() + QUERY_TIMEOUT_S
    qconn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    try:
        cur = qconn.execute(sql)
        cap = max(1, min(limit, QUERY_MAX_ROWS))
        rows = cur.fetchmany(cap + 1)
        return {"columns": [d[0] for d in cur.description or []],
                "rows": [dict(r) for r in rows[:cap]],
                "truncated": len(rows) > cap}
    except sqlite3.Error as e:
        msg = "query took longer than 5s" if "interrupted" in str(e) else str(e)
        return {"error": msg}
    finally:
        qconn.set_progress_handler(None, 0)


if __name__ == "__main__":
    mcp.run()
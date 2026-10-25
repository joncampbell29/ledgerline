"""Deterministic finance metrics over data.db.

Every function reads only from SQLite and returns plain dicts/lists, so the same
functions back the /digest page and (later) the MCP server's tools.

Sign conventions in the outputs:
  - Spending is reported as a positive number.
  - Liabilities (cards, mortgage) are reported as positive amounts owed.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta

import db

# Categories that move money between your own accounts or pay down debt.
# Counting them as "spending" would double-count (card purchase + card payment).
NON_SPENDING = ("INCOME", "TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS")
LIABILITY_TYPES = ("credit", "loan")


def _conn(conn):
    return conn or db.data_conn(readonly=True)


def _d(x):
    if x is None:
        return date.today()
    return x if isinstance(x, date) else date.fromisoformat(x)


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


def _pct(cur, prev):
    if not prev:
        return None
    return round((cur - prev) / abs(prev) * 100, 1)


# ---- Balances ----------------------------------------------------------------

def balances(as_of=None, conn=None):
    """Latest balance per account on or before `as_of`."""
    c = _conn(conn)
    return _rows(c.execute(
        """SELECT a.account_id, a.institution, a.name, a.mask, a.type, a.subtype,
                  b.date, b.current, b.available, b.credit_limit
           FROM accounts a
           JOIN balance_snapshots b ON b.account_id = a.account_id
           WHERE b.date = (SELECT MAX(date) FROM balance_snapshots
                           WHERE account_id = a.account_id AND date <= ?)
           ORDER BY a.institution, a.type, a.name""",
        (_d(as_of).isoformat(),)))


def net_worth(as_of=None, conn=None):
    rows = balances(as_of, conn)
    assets = sum(r["current"] or 0 for r in rows if r["type"] not in LIABILITY_TYPES)
    liabilities = sum(r["current"] or 0 for r in rows if r["type"] in LIABILITY_TYPES)
    return {"as_of": _d(as_of).isoformat(), "assets": round(assets, 2),
            "liabilities": round(liabilities, 2), "net_worth": round(assets - liabilities, 2)}


def balance_changes(days=1, as_of=None, conn=None):
    """Per-account change between `as_of - days` and `as_of`."""
    c = _conn(conn)
    end = _d(as_of)
    before = {r["account_id"]: r for r in balances(end - timedelta(days=days), c)}
    out = []
    for r in balances(end, c):
        prev = before.get(r["account_id"])
        prev_val = prev["current"] if prev and prev["date"] < r["date"] else None
        change = None if prev_val is None else round((r["current"] or 0) - prev_val, 2)
        out.append({**r, "previous": prev_val, "change": change})
    return out


def net_worth_change(days=1, as_of=None, conn=None):
    c = _conn(conn)
    end = _d(as_of)
    now, prev = net_worth(end, c), net_worth(end - timedelta(days=days), c)
    return {**now, "previous": prev["net_worth"],
            "change": round(now["net_worth"] - prev["net_worth"], 2)}


# ---- Spending & cash flow ----------------------------------------------------

def _spend_filter():
    marks = ",".join("?" * len(NON_SPENDING))
    return f"COALESCE(category, '') NOT IN ({marks})", NON_SPENDING


def spending_by_category(start, end, conn=None):
    """Net spending per category (refunds subtract), largest first."""
    where, params = _spend_filter()
    return _rows(_conn(conn).execute(
        f"""SELECT COALESCE(category, 'UNCATEGORIZED') AS category,
                   ROUND(SUM(amount), 2) AS total, COUNT(*) AS count
            FROM transactions
            WHERE date BETWEEN ? AND ? AND {where}
            GROUP BY 1 HAVING total > 0 ORDER BY total DESC""",
        (_d(start).isoformat(), _d(end).isoformat(), *params)))


def spending_trend(days=7, as_of=None, conn=None):
    """Spending per category for the last `days` vs the `days` before that."""
    c = _conn(conn)
    end = _d(as_of)
    cur_start = end - timedelta(days=days - 1)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    cur = {r["category"]: r["total"] for r in spending_by_category(cur_start, end, c)}
    prev = {r["category"]: r["total"] for r in spending_by_category(prev_start, prev_end, c)}
    rows = [{"category": k, "current": cur.get(k, 0), "previous": prev.get(k, 0),
             "pct_change": _pct(cur.get(k, 0), prev.get(k, 0))}
            for k in set(cur) | set(prev)]
    rows.sort(key=lambda r: r["current"], reverse=True)
    return {"days": days, "current_total": round(sum(cur.values()), 2),
            "previous_total": round(sum(prev.values()), 2),
            "pct_change": _pct(sum(cur.values()), sum(prev.values())), "categories": rows}


def cash_flow(start, end, conn=None):
    c = _conn(conn)
    income = c.execute(
        "SELECT COALESCE(-SUM(amount), 0) FROM transactions "
        "WHERE date BETWEEN ? AND ? AND category = 'INCOME'",
        (_d(start).isoformat(), _d(end).isoformat())).fetchone()[0]
    spending = sum(r["total"] for r in spending_by_category(start, end, c))
    return {"start": _d(start).isoformat(), "end": _d(end).isoformat(),
            "income": round(income, 2), "spending": round(spending, 2),
            "net": round(income - spending, 2)}


def month_to_date(as_of=None, conn=None):
    end = _d(as_of)
    return cash_flow(end.replace(day=1), end, conn)


def top_merchants(start, end, limit=10, conn=None):
    where, params = _spend_filter()
    return _rows(_conn(conn).execute(
        f"""SELECT COALESCE(merchant_name, name) AS merchant,
                   ROUND(SUM(amount), 2) AS total, COUNT(*) AS count
            FROM transactions
            WHERE date BETWEEN ? AND ? AND {where}
            GROUP BY 1 HAVING total > 0 ORDER BY total DESC LIMIT ?""",
        (_d(start).isoformat(), _d(end).isoformat(), *params, limit)))


def recent_transactions(days=1, as_of=None, limit=50, conn=None):
    end = _d(as_of)
    return _rows(_conn(conn).execute(
        """SELECT t.date, t.name, t.merchant_name, t.amount, t.category, t.pending,
                  a.institution, a.name AS account, a.mask
           FROM transactions t JOIN accounts a ON a.account_id = t.account_id
           WHERE t.date BETWEEN ? AND ?
           ORDER BY t.date DESC, ABS(t.amount) DESC LIMIT ?""",
        ((end - timedelta(days=days)).isoformat(), end.isoformat(), limit)))


def unusual_transactions(days=7, as_of=None, large=500, spike_multiple=2.5,
                         new_merchant_min=100, lookback=180, conn=None):
    """Rule-based flags. Each row gets a list of reasons.

    - large:        amount >= `large`
    - spike:        amount > spike_multiple x this merchant's average (needs 3+ prior charges)
    - new_merchant: first charge from this merchant in `lookback` days and amount >= new_merchant_min
    - duplicate:    same merchant, same amount, same day, more than once
    """
    c = _conn(conn)
    end = _d(as_of)
    start = end - timedelta(days=days)
    hist_start = start - timedelta(days=lookback)
    where, params = _spend_filter()

    recent = _rows(c.execute(
        f"""SELECT t.transaction_id, t.date, t.name, COALESCE(t.merchant_name, t.name) AS merchant,
                   t.amount, t.category, a.institution, a.name AS account
            FROM transactions t JOIN accounts a ON a.account_id = t.account_id
            WHERE t.date BETWEEN ? AND ? AND t.amount > 0 AND {where}""",
        (start.isoformat(), end.isoformat(), *params)))

    history = defaultdict(list)
    for r in c.execute(
            "SELECT COALESCE(merchant_name, name) AS m, amount FROM transactions "
            "WHERE date >= ? AND date < ? AND amount > 0",
            (hist_start.isoformat(), start.isoformat())):
        history[r["m"]].append(r["amount"])

    dupes = defaultdict(int)
    for r in recent:
        dupes[(r["merchant"], r["amount"], r["date"])] += 1

    out = []
    for r in recent:
        reasons = []
        prior = history.get(r["merchant"], [])
        if r["amount"] >= large:
            reasons.append("large")
        if len(prior) >= 3 and r["amount"] > spike_multiple * (sum(prior) / len(prior)):
            reasons.append("spike")
        if not prior and r["amount"] >= new_merchant_min:
            reasons.append("new_merchant")
        if dupes[(r["merchant"], r["amount"], r["date"])] > 1:
            reasons.append("duplicate")
        if reasons:
            avg = round(sum(prior) / len(prior), 2) if prior else None
            out.append({**r, "reasons": reasons, "merchant_avg": avg})
    out.sort(key=lambda r: r["amount"], reverse=True)
    return out


# ---- Liabilities -------------------------------------------------------------

def _latest_liabilities(as_of, c):
    return _rows(c.execute(
        """SELECT l.*, a.institution, a.name, a.mask, b.current AS balance, b.credit_limit
           FROM liability_snapshots l
           JOIN accounts a ON a.account_id = l.account_id
           LEFT JOIN balance_snapshots b ON b.account_id = l.account_id AND b.date = l.date
           WHERE l.date = (SELECT MAX(date) FROM liability_snapshots
                           WHERE account_id = l.account_id AND date <= ?)""",
        (as_of.isoformat(),)))


def liabilities_summary(as_of=None, conn=None):
    out = []
    for r in _latest_liabilities(_d(as_of), _conn(conn)):
        r.pop("raw", None)
        if r["kind"] == "credit" and r["credit_limit"]:
            r["utilization_pct"] = round((r["balance"] or 0) / r["credit_limit"] * 100, 1)
        out.append(r)
    return out


def upcoming_payments(days=14, as_of=None, conn=None):
    end = _d(as_of)
    horizon = end + timedelta(days=days)
    out = []
    for r in _latest_liabilities(end, _conn(conn)):
        due = r["next_payment_due_date"]
        if due and end.isoformat() <= due <= horizon.isoformat():
            out.append({
                "institution": r["institution"], "account": r["name"], "mask": r["mask"],
                "kind": r["kind"], "due_date": due,
                "days_until": (date.fromisoformat(due) - end).days,
                "amount_due": r["next_payment_amount"],
                "statement_balance": r["last_statement_balance"],
                "is_overdue": bool(r["is_overdue"]),
            })
    out.sort(key=lambda r: r["due_date"])
    return out


# ---- Investments -------------------------------------------------------------

def _holdings_on(as_of, c):
    snap = c.execute("SELECT MAX(date) FROM holding_snapshots WHERE date <= ?",
                     (as_of.isoformat(),)).fetchone()[0]
    if not snap:
        return None, []
    rows = _rows(c.execute(
        """SELECT h.account_id, h.security_id, h.quantity, h.price, h.value, h.cost_basis,
                  s.ticker, s.name, s.type, a.institution, a.name AS account
           FROM holding_snapshots h
           LEFT JOIN securities s ON s.security_id = h.security_id
           JOIN accounts a ON a.account_id = h.account_id
           WHERE h.date = ? ORDER BY h.value DESC""", (snap,)))
    return snap, rows


def portfolio(as_of=None, conn=None):
    snap, rows = _holdings_on(_d(as_of), _conn(conn))
    total = sum(r["value"] or 0 for r in rows)
    basis = sum(r["cost_basis"] or 0 for r in rows if r["cost_basis"] is not None)
    for r in rows:
        r["weight_pct"] = round((r["value"] or 0) / total * 100, 1) if total else None
        r["gain"] = round(r["value"] - r["cost_basis"], 2) if r["cost_basis"] is not None else None
    return {"as_of": snap, "total_value": round(total, 2),
            "cost_basis": round(basis, 2) if basis else None, "holdings": rows}


def portfolio_change(days=1, as_of=None, movers=5, conn=None):
    c = _conn(conn)
    end = _d(as_of)
    snap_now, now = _holdings_on(end, c)
    snap_prev, prev = _holdings_on(end - timedelta(days=days), c)
    if snap_prev == snap_now:
        snap_prev, prev = None, []
    total_now = sum(r["value"] or 0 for r in now)
    total_prev = sum(r["value"] or 0 for r in prev) if prev else None

    def key(r):
        return r["account_id"], r["security_id"]

    prev_by = {key(r): r for r in prev}
    changes = []
    for r in now:
        p = prev_by.get(key(r))
        if p:
            changes.append({"ticker": r["ticker"], "name": r["name"], "value": r["value"],
                            "change": round((r["value"] or 0) - (p["value"] or 0), 2),
                            "price_pct": _pct(r["price"] or 0, p["price"] or 0)})
    changes.sort(key=lambda r: abs(r["change"]), reverse=True)
    return {"as_of": snap_now, "compared_to": snap_prev, "total_value": round(total_now, 2),
            "previous_value": round(total_prev, 2) if total_prev is not None else None,
            "change": round(total_now - total_prev, 2) if total_prev is not None else None,
            "pct_change": _pct(total_now, total_prev) if total_prev else None,
            "top_movers": changes[:movers]}


def investment_activity(start, end, conn=None):
    c = _conn(conn)
    rows = _rows(c.execute(
        """SELECT t.date, t.type, t.subtype, t.name, t.quantity, t.price, t.amount,
                  s.ticker, a.institution, a.name AS account
           FROM investment_transactions t
           LEFT JOIN securities s ON s.security_id = t.security_id
           JOIN accounts a ON a.account_id = t.account_id
           WHERE t.date BETWEEN ? AND ? ORDER BY t.date DESC""",
        (_d(start).isoformat(), _d(end).isoformat())))
    dividends = -sum(r["amount"] or 0 for r in rows if r["subtype"] == "dividend")
    by_type = defaultdict(float)
    for r in rows:
        by_type[r["subtype"] or r["type"]] += r["amount"] or 0
    return {"start": _d(start).isoformat(), "end": _d(end).isoformat(),
            "dividends": round(dividends, 2),
            "totals_by_subtype": {k: round(v, 2) for k, v in by_type.items()},
            "transactions": rows}


# ---- Health ------------------------------------------------------------------

def connection_health(stale_hours=36, conn=None):
    rows = _rows(_conn(conn).execute("SELECT * FROM connections ORDER BY institution"))
    now = datetime.now()
    for r in rows:
        last = r["last_synced_at"]
        r["stale"] = (not last) or (now - datetime.fromisoformat(last)) > timedelta(hours=stale_hours)
    return rows


# ---- Digest ------------------------------------------------------------------

def digest(as_of=None, conn=None):
    """Everything the daily report needs, in one dict."""
    c = _conn(conn)
    end = _d(as_of)
    return {
        "as_of": end.isoformat(),
        "net_worth": net_worth_change(1, end, c),
        "balances": balance_changes(1, end, c),
        "yesterday": recent_transactions(1, end, conn=c),
        "spending_7d": spending_trend(7, end, c),
        "month_to_date": month_to_date(end, c),
        "upcoming_payments": upcoming_payments(14, end, c),
        "liabilities": liabilities_summary(end, c),
        "portfolio": portfolio_change(1, end, conn=c),
        "investment_activity_7d": investment_activity(end - timedelta(days=7), end, c),
        "unusual": unusual_transactions(7, end, conn=c),
        "health": connection_health(conn=c),
    }
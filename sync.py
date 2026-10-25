"""Pull everything from the provider into data.db.

    python sync.py                 # sync every linked Item
    python sync.py --item ITEM_ID  # sync one

Safe to re-run: every write is an upsert, and the transactions cursor only advances
after the data it covers is committed. Schedule it daily (cron, launchd, systemd timer).
"""
import argparse
import json
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

import db
from providers import PlaidProvider, ProviderError

# Errors that mean "try again later", not "something is broken".
SOFT_ERRORS = {"PRODUCT_NOT_READY"}
# Errors that mean the user has to reconnect the bank in Link (update mode).
RELINK_ERRORS = {"ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION", "ACCESS_NOT_GRANTED",
                 "INVALID_ACCESS_TOKEN", "ITEM_NOT_FOUND"}


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ---- Writers -----------------------------------------------------------------

def write_accounts(data, item, accounts, today):
    for a in accounts:
        b = a.get("balances") or {}
        data.execute(
            """INSERT INTO accounts (account_id, item_id, institution, name, official_name,
                                     mask, type, subtype, currency)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET
                 name=excluded.name, official_name=excluded.official_name, mask=excluded.mask,
                 type=excluded.type, subtype=excluded.subtype, currency=excluded.currency,
                 institution=excluded.institution""",
            (a["account_id"], item["item_id"], item["institution"], a.get("name"),
             a.get("official_name"), a.get("mask"), a.get("type"), a.get("subtype"),
             b.get("iso_currency_code")),
        )
        data.execute(
            "INSERT OR REPLACE INTO balance_snapshots VALUES (?, ?, ?, ?, ?)",
            (a["account_id"], today, b.get("current"), b.get("available"), b.get("limit")),
        )


def write_transactions(data, added, modified, removed):
    rows = []
    for t in added + modified:
        pfc = t.get("personal_finance_category") or {}
        rows.append((
            t["transaction_id"], t["account_id"], t["date"], t.get("authorized_date"),
            t.get("name"), t.get("merchant_name"), t["amount"], t.get("iso_currency_code"),
            pfc.get("primary"), pfc.get("detailed"), int(bool(t.get("pending"))),
            t.get("payment_channel"),
        ))
    data.executemany("INSERT OR REPLACE INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    data.executemany("DELETE FROM transactions WHERE transaction_id = ?",
                     [(r["transaction_id"],) for r in removed])


def write_liabilities(data, liabilities, today):
    def put(account_id, kind, raw, **cols):
        data.execute(
            """INSERT OR REPLACE INTO liability_snapshots
               (account_id, date, kind, last_statement_balance, minimum_payment,
                next_payment_amount, next_payment_due_date, is_overdue, apr, interest_rate,
                escrow_balance, ytd_interest_paid, ytd_principal_paid, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (account_id, today, kind, cols.get("last_statement_balance"),
             cols.get("minimum_payment"), cols.get("next_payment_amount"),
             cols.get("next_payment_due_date"), cols.get("is_overdue"), cols.get("apr"),
             cols.get("interest_rate"), cols.get("escrow_balance"),
             cols.get("ytd_interest_paid"), cols.get("ytd_principal_paid"), json.dumps(raw)),
        )

    for c in liabilities.get("credit") or []:
        aprs = c.get("aprs") or []
        purchase = next((a for a in aprs if a.get("apr_type") == "purchase_apr"), aprs[0] if aprs else {})
        put(c["account_id"], "credit", c,
            last_statement_balance=c.get("last_statement_balance"),
            minimum_payment=c.get("minimum_payment_amount"),
            next_payment_amount=c.get("minimum_payment_amount"),
            next_payment_due_date=c.get("next_payment_due_date"),
            is_overdue=int(bool(c.get("is_overdue"))),
            apr=purchase.get("apr_percentage"))

    for m in liabilities.get("mortgage") or []:
        put(m["account_id"], "mortgage", m,
            next_payment_amount=m.get("next_monthly_payment"),
            next_payment_due_date=m.get("next_payment_due_date"),
            is_overdue=int(bool(m.get("past_due_amount"))),
            interest_rate=(m.get("interest_rate") or {}).get("percentage"),
            escrow_balance=m.get("escrow_balance"),
            ytd_interest_paid=m.get("ytd_interest_paid"),
            ytd_principal_paid=m.get("ytd_principal_paid"))

    for s in liabilities.get("student") or []:
        put(s["account_id"], "student", s,
            last_statement_balance=s.get("last_statement_balance"),
            minimum_payment=s.get("minimum_payment_amount"),
            next_payment_amount=s.get("minimum_payment_amount"),
            next_payment_due_date=s.get("next_payment_due_date"),
            is_overdue=int(bool(s.get("is_overdue"))),
            interest_rate=s.get("interest_rate_percentage"))


def write_securities(data, securities):
    data.executemany(
        "INSERT OR REPLACE INTO securities VALUES (?, ?, ?, ?, ?, ?)",
        [(s["security_id"], s.get("ticker_symbol"), s.get("name"), s.get("type"),
          s.get("close_price"), s.get("close_price_as_of")) for s in securities],
    )


def write_holdings(data, account_ids, holdings, today):
    # Replace today's snapshot wholesale so positions you sold today disappear.
    data.executemany("DELETE FROM holding_snapshots WHERE account_id = ? AND date = ?",
                     [(a, today) for a in account_ids])
    data.executemany(
        "INSERT OR REPLACE INTO holding_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(h["account_id"], h["security_id"], today, h.get("quantity"),
          h.get("institution_price"), h.get("institution_value"), h.get("cost_basis"))
         for h in holdings],
    )


def write_investment_transactions(data, txs):
    data.executemany(
        "INSERT OR REPLACE INTO investment_transactions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(t["investment_transaction_id"], t["account_id"], t.get("security_id"), t["date"],
          t.get("name"), t.get("type"), t.get("subtype"), t.get("quantity"), t.get("price"),
          t.get("amount"), t.get("fees")) for t in txs],
    )


# ---- Per-Item sync -----------------------------------------------------------

def sync_item(provider, item, today=None):
    """Sync one Item. Returns {"item_id", "institution", "steps": {...}, "status"}."""
    today = today or date.today().isoformat()
    token = item["access_token"]
    products = set(item["products"].split(","))
    steps = {}

    def step(name, fn):
        try:
            fn()
            steps[name] = "ok"
        except ProviderError as e:
            steps[name] = e.code
            if e.code in RELINK_ERRORS:
                raise

    with db.data_conn() as data, db.secrets_conn() as secrets:
        account_ids = []
        try:
            def accounts():
                accts = provider.accounts(token)
                write_accounts(data, item, accts, today)
                account_ids.extend(a["account_id"] for a in accts)
            step("accounts", accounts)

            if "transactions" in products:
                def transactions():
                    added, modified, removed, cursor = provider.transactions_sync(
                        token, item["transactions_cursor"])
                    write_transactions(data, added, modified, removed)
                    data.commit()  # commit data before advancing the cursor
                    secrets.execute("UPDATE items SET transactions_cursor = ? WHERE item_id = ?",
                                    (cursor, item["item_id"]))
                    secrets.commit()
                step("transactions", transactions)

            if "liabilities" in products:
                step("liabilities", lambda: write_liabilities(data, provider.liabilities(token), today))

            if "investments" in products:
                def holdings():
                    h = provider.holdings(token)
                    write_securities(data, h["securities"])
                    write_holdings(data, account_ids, h["holdings"], today)
                step("holdings", holdings)

                def investment_transactions():
                    # Two years on first sync, then a 30-day overlap window.
                    marks = ",".join("?" * len(account_ids)) or "''"
                    have = data.execute(
                        f"SELECT 1 FROM investment_transactions WHERE account_id IN ({marks}) LIMIT 1",
                        account_ids).fetchone()
                    end = date.fromisoformat(today)
                    start = end - timedelta(days=30 if have else 730)
                    r = provider.investment_transactions(token, start, end)
                    write_securities(data, r["securities"])
                    write_investment_transactions(data, r["investment_transactions"])
                step("investment_transactions", investment_transactions)

            def status():
                err = (provider.item_status(token).get("error") or {}).get("error_code")
                if err:
                    steps["status"] = err
                    if err in RELINK_ERRORS:
                        raise ProviderError(err)
            step("status", status)
        except ProviderError:
            pass  # recorded in steps; the Item needs a relink, so stop here

        failed = {k: v for k, v in steps.items() if v != "ok"}
        if any(v in RELINK_ERRORS for v in failed.values()):
            status_, code = "needs_attention", next(v for v in failed.values() if v in RELINK_ERRORS)
        elif any(v not in SOFT_ERRORS for v in failed.values()):
            status_, code = "error", next(iter(failed.values()))
        else:
            status_, code = "ok", None

        data.execute(
            """INSERT OR REPLACE INTO connections
               (item_id, institution, products, status, error_code, last_synced_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (item["item_id"], item["institution"], item["products"], status_, code, _now()),
        )

    return {"item_id": item["item_id"], "institution": item["institution"],
            "status": status_, "steps": steps}


def run(provider=None, item_id=None, today=None):
    db.init()
    provider = provider or PlaidProvider()
    started = _now()
    with db.secrets_conn() as s:
        q = "SELECT * FROM items" + (" WHERE item_id = ?" if item_id else "")
        items = s.execute(q, (item_id,) if item_id else ()).fetchall()

    results = [sync_item(provider, dict(i), today=today) for i in items]

    overall = "ok" if all(r["status"] == "ok" for r in results) else "partial"
    with db.data_conn() as d:
        d.execute("INSERT INTO sync_runs (started_at, finished_at, status, detail) VALUES (?,?,?,?)",
                  (started, _now(), overall, json.dumps(results)))
    return results


if __name__ == "__main__":
    load_dotenv()
    p = argparse.ArgumentParser(description="Sync linked accounts into data.db")
    p.add_argument("--item", help="only sync this item_id")
    args = p.parse_args()
    for r in run(item_id=args.item):
        detail = ", ".join(f"{k}={v}" for k, v in r["steps"].items())
        print(f"{r['institution'] or r['item_id']:<24} {r['status']:<16} {detail}")
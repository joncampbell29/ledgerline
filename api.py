"""Local web app: link accounts, trigger syncs, read the digest.

    uvicorn api:app --reload

Routes
  GET    /                        Link page (index.html)
  POST   /create_link_token       ?products=transactions&products=liabilities
  POST   /exchange_public_token   saves the Item, then syncs it in the background
  GET    /items                   what's linked (no tokens)
  DELETE /items/{item_id}         remove at Plaid and delete local data
  POST   /sync                    sync everything now
  GET    /digest                  HTML report
  GET    /digest.json             same data as JSON

There is no login. Bind to localhost only (compose.yaml does) and never expose it publicly.
Two guards cover attacks from web pages open in your own browser:
  - Host must be in LEDGERLINE_ALLOWED_HOSTS (default localhost,127.0.0.1): blocks DNS rebinding.
  - Requests that change things must come from this app's own origin: blocks cross-site POSTs.
"""
import os
from datetime import date
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

load_dotenv()

import db  # noqa: E402
import metrics  # noqa: E402
import sync  # noqa: E402
from providers import PlaidProvider, ProviderError  # noqa: E402

db.init()
provider = PlaidProvider()
app = FastAPI(title="ledgerline")
INDEX = Path(__file__).resolve().parent / "index.html"


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    origin = request.headers.get("origin")
    if (request.method not in ("GET", "HEAD", "OPTIONS") and origin
            and urlparse(origin).netloc != request.headers.get("host")):
        return PlainTextResponse("cross-origin request blocked", status_code=403)
    return await call_next(request)


# Added last so it runs first.
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=os.environ.get("LEDGERLINE_ALLOWED_HOSTS", "localhost,127.0.0.1").split(","),
)


# ---- Linking -----------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(INDEX)


@app.post("/create_link_token")
def create_link_token(products: list[str] = Query(["transactions"])):
    try:
        return {"link_token": provider.create_link_token(products)}
    except ProviderError as e:
        raise HTTPException(400, e.code) from None


class ExchangeBody(BaseModel):
    public_token: str
    institution: str | None = None
    products: list[str]


@app.post("/exchange_public_token")
def exchange_public_token(body: ExchangeBody, background: BackgroundTasks):
    try:
        item_id, token = provider.exchange_public_token(body.public_token)
    except ProviderError as e:
        raise HTTPException(400, e.code) from None
    with db.secrets_conn() as s:
        s.execute(
            "INSERT OR REPLACE INTO items (item_id, access_token, institution, products) "
            "VALUES (?, ?, ?, ?)",
            (item_id, token, body.institution, ",".join(body.products)),
        )
    background.add_task(sync.run, provider, item_id)
    return {"item_id": item_id, "products": body.products, "sync": "started"}


@app.get("/items")
def list_items():
    with db.secrets_conn() as s:
        items = s.execute("SELECT item_id, institution, products, created_at FROM items").fetchall()
    health = {h["item_id"]: h for h in metrics.connection_health(conn=db.data_conn(readonly=True))}
    return [{**dict(i), "products": i["products"].split(","),
             "status": health.get(i["item_id"], {}).get("status"),
             "last_synced_at": health.get(i["item_id"], {}).get("last_synced_at")}
            for i in items]


@app.delete("/items/{item_id}")
def remove_item(item_id: str):
    with db.secrets_conn() as s:
        row = s.execute("SELECT access_token FROM items WHERE item_id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown item")
    try:
        provider.remove_item(row["access_token"])
    except ProviderError as e:
        if e.code not in sync.RELINK_ERRORS:  # already gone at Plaid is fine
            raise HTTPException(400, e.code) from None
    db.purge_item(item_id)
    return {"removed": item_id}


@app.post("/sync")
def sync_now():
    return sync.run(provider)


# ---- Digest ------------------------------------------------------------------

@app.get("/digest.json")
def digest_json(as_of: date | None = None):
    return metrics.digest(as_of)


@app.get("/digest", response_class=HTMLResponse)
def digest_html(as_of: date | None = None):
    return render_digest(metrics.digest(as_of))


def money(x, signed=False):
    if x is None:
        return "—"
    s = f"${abs(x):,.2f}"
    if signed:
        return ("+" if x > 0 else "−" if x < 0 else "") + s
    return ("−" if x < 0 else "") + s


def delta(x, invert=False):
    """Colored signed amount. invert=True when going up is bad (debt, spending)."""
    if x is None or x == 0:
        return '<span class="muted">—</span>'
    good = (x > 0) != invert
    return f'<span class="{"up" if good else "down"}">{money(x, signed=True)}</span>'


def pct(x):
    return "" if x is None else f"{x:+.1f}%"


def label(s):
    return escape((s or "").replace("_", " ").title())


def table(headers, rows, numeric=()):
    if not rows:
        return '<p class="muted">Nothing here.</p>'
    th = "".join(f'<th class="{"num" if i in numeric else ""}">{h}</th>' for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f'<td class="{"num" if i in numeric else ""}">{c}</td>'
                         for i, c in enumerate(r)) + "</tr>"
        for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def render_digest(d):
    nw, port, mtd, sp = d["net_worth"], d["portfolio"], d["month_to_date"], d["spending_7d"]
    sections = []

    alerts = [h for h in d["health"] if h["status"] != "ok" or h["stale"]]
    if alerts:
        items = "".join(
            f"<li><b>{escape(h['institution'] or h['item_id'])}</b>: "
            f"{escape(h['error_code'] or ('stale' if h['stale'] else h['status']))}</li>"
            for h in alerts)
        sections.append(f'<section class="alert"><h2>Connections need attention</h2><ul>{items}</ul></section>')

    stats = [
        ("Net worth", money(nw["net_worth"]), delta(nw["change"]) + " since yesterday"),
        ("Investments", money(port["total_value"]),
         delta(port["change"]) + f' {pct(port["pct_change"])}' if port["change"] is not None
         else '<span class="muted">no prior snapshot</span>'),
        ("Spent, last 7 days", money(sp["current_total"]),
         f'{pct(sp["pct_change"])} vs prior 7' if sp["pct_change"] is not None else ""),
        ("Month to date", money(mtd["net"]),
         f'{money(mtd["income"])} in · {money(mtd["spending"])} out'),
    ]
    sections.append('<section class="stats">' + "".join(
        f'<div class="stat"><div class="k">{k}</div><div class="v">{v}</div><div class="s">{s}</div></div>'
        for k, v, s in stats) + "</section>")

    if d["upcoming_payments"]:
        sections.append("<section><h2>Due in the next 14 days</h2>" + table(
            ["Due", "Account", "Amount due", "Statement balance"],
            [[f'{p["due_date"]} <span class="muted">({p["days_until"]}d)</span>'
              + (' <span class="down">overdue</span>' if p["is_overdue"] else ""),
              f'{escape(p["institution"] or "")} {escape(p["account"] or "")} ··{escape(p["mask"] or "")}',
              money(p["amount_due"]), money(p["statement_balance"])]
             for p in d["upcoming_payments"]], numeric=(2, 3)) + "</section>")

    if d["unusual"]:
        sections.append("<section><h2>Worth a look</h2>" + table(
            ["Date", "Merchant", "Account", "Amount", "Why"],
            [[u["date"], escape(u["merchant"] or ""), escape(f'{u["institution"]} {u["account"]}'),
              money(u["amount"]),
              ", ".join(label(x) for x in u["reasons"])
              + (f' <span class="muted">(avg {money(u["merchant_avg"])})</span>' if u["merchant_avg"] else "")]
             for u in d["unusual"]], numeric=(3,)) + "</section>")

    sections.append("<section><h2>Spending by category, last 7 days</h2>" + table(
        ["Category", "This week", "Prior week", "Change"],
        [[label(c["category"]), money(c["current"]), money(c["previous"]), pct(c["pct_change"])]
         for c in sp["categories"]], numeric=(1, 2, 3)) + "</section>")

    sections.append("<section><h2>Since yesterday</h2>" + table(
        ["Date", "Description", "Account", "Category", "Amount"],
        [[t["date"], escape(t["merchant_name"] or t["name"] or "")
          + (' <span class="muted">pending</span>' if t["pending"] else ""),
          escape(f'{t["institution"]} ··{t["mask"] or ""}'), label(t["category"]),
          delta(-t["amount"])]
         for t in d["yesterday"]], numeric=(4,)) + "</section>")

    sections.append("<section><h2>Accounts</h2>" + table(
        ["Institution", "Account", "Type", "Balance", "Change"],
        [[escape(b["institution"] or ""), f'{escape(b["name"] or "")} ··{escape(b["mask"] or "")}',
          label(b["subtype"] or b["type"]), money(b["current"]),
          delta(b["change"], invert=b["type"] in metrics.LIABILITY_TYPES)]
         for b in d["balances"]], numeric=(3, 4)) + "</section>")

    if port["top_movers"]:
        sections.append("<section><h2>Top movers</h2>" + table(
            ["Holding", "Value", "Change", "Price"],
            [[escape(m["ticker"] or m["name"] or ""), money(m["value"]), delta(m["change"]), pct(m["price_pct"])]
             for m in port["top_movers"]], numeric=(1, 2, 3)) + "</section>")

    inv = d["investment_activity_7d"]
    if inv["transactions"]:
        sections.append(
            "<section><h2>Investment activity, last 7 days</h2>"
            + (f'<p>Dividends: <b>{money(inv["dividends"])}</b></p>' if inv["dividends"] else "")
            + table(["Date", "Type", "Security", "Qty", "Amount"],
                    [[t["date"], label(t["subtype"] or t["type"]), escape(t["ticker"] or t["name"] or ""),
                      f'{t["quantity"]:g}' if t["quantity"] else "", delta(-(t["amount"] or 0))]
                     for t in inv["transactions"][:20]], numeric=(3, 4))
            + "</section>")

    loans = [x for x in d["liabilities"] if x["kind"] != "credit"]
    if loans:
        sections.append("<section><h2>Loans</h2>" + table(
            ["Account", "Balance", "Rate", "Next payment", "YTD interest"],
            [[f'{escape(ln["institution"] or "")} {escape(ln["name"] or "")}', money(ln["balance"]),
              f'{ln["interest_rate"]:.3f}%' if ln["interest_rate"] else "—",
              f'{money(ln["next_payment_amount"])} on {ln["next_payment_due_date"] or "—"}',
              money(ln["ytd_interest_paid"])]
             for ln in loans], numeric=(1, 2, 4)) + "</section>")

    return PAGE.format(as_of=escape(d["as_of"]), body="\n".join(sections))


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Daily digest</title>
<style>
:root {{ --bg:#fafaf9; --card:#fff; --fg:#1c1917; --muted:#78716c; --line:#e7e5e4;
        --up:#15803d; --down:#b91c1c; --warn-bg:#fef3c7; --warn-fg:#92400e; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0c0a09; --card:#1c1917; --fg:#f5f5f4; --muted:#a8a29e; --line:#292524;
          --up:#4ade80; --down:#f87171; --warn-bg:#422006; --warn-fg:#fcd34d; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--fg);
        font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:920px; margin:0 auto; padding:32px 16px 64px; }}
header {{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:24px; }}
h1 {{ font-size:22px; margin:0 }} h2 {{ font-size:14px; text-transform:uppercase;
     letter-spacing:.05em; color:var(--muted); margin:0 0 12px }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:18px 20px; margin-bottom:16px; overflow-x:auto }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:20px }}
.stat .k {{ color:var(--muted); font-size:13px }}
.stat .v {{ font-size:24px; font-weight:600; font-variant-numeric:tabular-nums }}
.stat .s {{ font-size:13px }}
.alert {{ background:var(--warn-bg); color:var(--warn-fg); border-color:transparent }}
.alert h2 {{ color:inherit }} .alert ul {{ margin:0; padding-left:18px }}
table {{ width:100%; border-collapse:collapse; font-size:14px }}
th {{ text-align:left; font-weight:500; color:var(--muted); border-bottom:1px solid var(--line); padding:6px 8px }}
td {{ border-bottom:1px solid var(--line); padding:7px 8px; vertical-align:top }}
tr:last-child td {{ border-bottom:0 }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap }}
.muted {{ color:var(--muted) }} .up {{ color:var(--up) }} .down {{ color:var(--down) }}
a {{ color:inherit }}
</style></head>
<body><main>
<header><h1>Daily digest</h1><span class="muted">{as_of} · <a href="/digest.json">json</a></span></header>
{body}
</main></body></html>"""
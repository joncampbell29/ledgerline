import os
import json
import sqlite3
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
import plaid
from plaid import ApiException
from plaid.api import plaid_api
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest

load_dotenv()

config = plaid.Configuration(
    host=plaid.Environment.Sandbox,
    api_key={
        "clientId": os.environ["PLAID_CLIENT_ID"],
        "secret": os.environ["PLAID_SECRET"],
    },
)
client = plaid_api.PlaidApi(plaid.ApiClient(config))

app = FastAPI()

db = sqlite3.connect("ledgerline.db", check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS items (
    item_id      TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    institution  TEXT,
    products     TEXT NOT NULL
)""")
db.commit()


def items_with(product: str):
    """Return (item_id, access_token, institution) for Items linked with `product`."""
    rows = db.execute(
        "SELECT item_id, access_token, institution, products FROM items"
    ).fetchall()
    return [(i, t, n) for i, t, n, p in rows if product in p.split(",")]


@app.get("/")
def index():
    return FileResponse("index.html")


@app.post("/create_link_token")
def create_link_token(products: list[str] = Query(["transactions"])):
    request = LinkTokenCreateRequest(
        products=[Products(p) for p in products],
        client_name="ledgerline",
        country_codes=[CountryCode("US")],
        language="en",
        user=LinkTokenCreateRequestUser(client_user_id="local-user"),
    )
    return client.link_token_create(request).to_dict()


class ExchangeBody(BaseModel):
    public_token: str
    institution: str | None = None
    products: list[str]


@app.post("/exchange_public_token")
def exchange_public_token(body: ExchangeBody):
    resp = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=body.public_token)
    )
    db.execute(
        "INSERT OR REPLACE INTO items (item_id, access_token, institution, products) "
        "VALUES (?, ?, ?, ?)",
        (resp.item_id, resp.access_token, body.institution, ",".join(body.products)),
    )
    db.commit()
    return {"item_id": resp.item_id, "products": body.products}


@app.get("/items")
def list_items():
    """What's linked, without exposing access tokens."""
    rows = db.execute("SELECT item_id, institution, products FROM items").fetchall()
    return [{"item_id": i, "institution": n, "products": p.split(",")} for i, n, p in rows]


@app.get("/transactions")
def transactions():
    out = []
    for item_id, token, inst in items_with("transactions"):
        try:
            r = client.transactions_sync(TransactionsSyncRequest(access_token=token))
            out.append({"institution": inst, "added": [t.to_dict() for t in r.added]})
        except ApiException as e:
            err = json.loads(e.body)
            out.append({"institution": inst, "error": err.get("error_code")})
    return out
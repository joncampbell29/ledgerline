import sqlite3
from plaid.model.item_remove_request import ItemRemoveRequest
from main import client

db = sqlite3.connect("ledgerline.db")
for item_id, token, inst in db.execute(
    "SELECT item_id, access_token, institution FROM items"
).fetchall():
    client.item_remove(ItemRemoveRequest(access_token=token))
    print(f"Removed {inst} ({item_id})")
db.close()
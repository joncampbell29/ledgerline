"""Remove every Item at Plaid and delete all local data. Use before switching environments."""
from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402
from providers import PlaidProvider, ProviderError  # noqa: E402

db.init()
provider = PlaidProvider()
with db.secrets_conn() as s:
    items = s.execute("SELECT item_id, access_token, institution FROM items").fetchall()

for i in items:
    try:
        provider.remove_item(i["access_token"])
    except ProviderError as e:
        print(f"  (Plaid said {e.code}; deleting locally anyway)")
    db.purge_item(i["item_id"])
    print(f"Removed {i['institution']} ({i['item_id']})")

print(f"Done. Delete {db.DATA_DB} and {db.SECRETS_DB} too if you want a completely clean slate.")
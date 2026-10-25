"""Thin wrapper over the Plaid SDK. Every method returns plain JSON-able dicts."""
import json
import os
from datetime import date

import plaid
from plaid import ApiException
from plaid.api import plaid_api
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
from plaid.model.investments_transactions_get_request import InvestmentsTransactionsGetRequest
from plaid.model.investments_transactions_get_request_options import (
    InvestmentsTransactionsGetRequestOptions,
)
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.link_token_transactions import LinkTokenTransactions
from plaid.model.products import Products
from plaid.model.transactions_sync_request import TransactionsSyncRequest

ENVIRONMENTS = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}


class ProviderError(Exception):
    def __init__(self, code, message=""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _plain(obj):
    """Plaid's to_dict() leaves date objects in; round-trip through JSON to get strings."""
    return json.loads(json.dumps(obj, default=str))


class PlaidProvider:
    name = "plaid"

    def __init__(self, client_id=None, secret=None, env=None):
        env = (env or os.environ.get("PLAID_ENV", "sandbox")).lower()
        config = plaid.Configuration(
            host=ENVIRONMENTS[env],
            api_key={
                "clientId": client_id or os.environ["PLAID_CLIENT_ID"],
                "secret": secret or os.environ["PLAID_SECRET"],
            },
        )
        self.client = plaid_api.PlaidApi(plaid.ApiClient(config))

    def _call(self, fn, request):
        try:
            return _plain(fn(request).to_dict())
        except ApiException as e:
            try:
                body = json.loads(e.body or "{}")
            except ValueError:
                body = {}
            raise ProviderError(body.get("error_code", f"HTTP_{e.status}"),
                                body.get("error_message", str(e.reason))) from None

    # ---- Linking -----------------------------------------------------------

    def create_link_token(self, products, user_id="local-user", client_name="ledgerline"):
        kwargs = dict(
            products=[Products(p) for p in products],
            client_name=client_name,
            country_codes=[CountryCode("US")],
            language="en",
            user=LinkTokenCreateRequestUser(client_user_id=user_id),
        )
        if "transactions" in products:
            # Default is 90 days; ask for the max so spending history is useful.
            kwargs["transactions"] = LinkTokenTransactions(days_requested=730)
        return self._call(self.client.link_token_create, LinkTokenCreateRequest(**kwargs))["link_token"]

    def exchange_public_token(self, public_token):
        r = self._call(self.client.item_public_token_exchange,
                       ItemPublicTokenExchangeRequest(public_token=public_token))
        return r["item_id"], r["access_token"]

    def remove_item(self, access_token):
        self._call(self.client.item_remove, ItemRemoveRequest(access_token=access_token))

    # ---- Data --------------------------------------------------------------

    def item_status(self, access_token):
        return self._call(self.client.item_get, ItemGetRequest(access_token=access_token))["item"]

    def accounts(self, access_token):
        return self._call(self.client.accounts_get,
                          AccountsGetRequest(access_token=access_token))["accounts"]

    def transactions_sync(self, access_token, cursor=None, max_restarts=3):
        """Page through /transactions/sync from `cursor`.

        Returns (added, modified, removed, next_cursor). If Plaid reports the data changed
        mid-pagination, restart from the original cursor as Plaid's docs recommend.
        """
        for _ in range(max_restarts):
            added, modified, removed, cur = [], [], [], cursor
            try:
                while True:
                    kwargs = {"access_token": access_token, "count": 500}
                    if cur:
                        kwargs["cursor"] = cur
                    r = self._call(self.client.transactions_sync, TransactionsSyncRequest(**kwargs))
                    added += r["added"]
                    modified += r["modified"]
                    removed += r["removed"]
                    cur = r["next_cursor"]
                    if not r["has_more"]:
                        return added, modified, removed, cur
            except ProviderError as e:
                if e.code != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                    raise
        raise ProviderError("TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", "gave up after restarts")

    def liabilities(self, access_token):
        """{"credit": [...], "mortgage": [...], "student": [...]}; missing kinds are None."""
        return self._call(self.client.liabilities_get,
                          LiabilitiesGetRequest(access_token=access_token))["liabilities"]

    def holdings(self, access_token):
        r = self._call(self.client.investments_holdings_get,
                       InvestmentsHoldingsGetRequest(access_token=access_token))
        return {"holdings": r["holdings"], "securities": r["securities"]}

    def investment_transactions(self, access_token, start: date, end: date):
        txs, securities, total = [], [], None
        while total is None or len(txs) < total:
            r = self._call(
                self.client.investments_transactions_get,
                InvestmentsTransactionsGetRequest(
                    access_token=access_token,
                    start_date=start,
                    end_date=end,
                    options=InvestmentsTransactionsGetRequestOptions(count=500, offset=len(txs)),
                ),
            )
            batch = r["investment_transactions"]
            if not batch:
                break
            txs += batch
            securities += r["securities"]
            total = r["total_investment_transactions"]
        return {"investment_transactions": txs, "securities": securities}
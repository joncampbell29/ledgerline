"""SQLite schema and connections.

Two files, on purpose:
  secrets.db  Plaid access tokens and sync cursors. Only sync.py and api.py open it.
  data.db     Everything else. Safe to open read-only for the digest and the MCP server.

Run `python db.py` to create both.
"""
import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.environ.get("LEDGERLINE_DATA_DIR", "."))
SECRETS_DB = DATA_DIR / "secrets.db"
DATA_DB = DATA_DIR / "data.db"

SECRETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id             TEXT PRIMARY KEY,
    access_token        TEXT NOT NULL,
    institution         TEXT,
    products            TEXT NOT NULL,          -- comma-separated, e.g. "transactions,liabilities"
    transactions_cursor TEXT,                   -- /transactions/sync cursor
    created_at          TEXT DEFAULT (datetime('now'))
);
"""

DATA_SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    item_id        TEXT PRIMARY KEY,
    institution    TEXT,
    products       TEXT,
    status         TEXT,                        -- ok | needs_attention | error
    error_code     TEXT,
    last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id    TEXT PRIMARY KEY,
    item_id       TEXT NOT NULL,
    institution   TEXT,
    name          TEXT,
    official_name TEXT,
    mask          TEXT,
    type          TEXT,                         -- depository | credit | loan | investment | other
    subtype       TEXT,
    currency      TEXT
);

-- One row per account per day. "Since yesterday" comes from here.
CREATE TABLE IF NOT EXISTS balance_snapshots (
    account_id   TEXT NOT NULL,
    date         TEXT NOT NULL,
    current      REAL,
    available    REAL,
    credit_limit REAL,
    PRIMARY KEY (account_id, date)
);

-- Plaid sign convention: amount > 0 is money out, amount < 0 is money in.
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id    TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL,
    date              TEXT NOT NULL,
    authorized_date   TEXT,
    name              TEXT,
    merchant_name     TEXT,
    amount            REAL NOT NULL,
    currency          TEXT,
    category          TEXT,                     -- personal_finance_category.primary
    category_detailed TEXT,
    pending           INTEGER DEFAULT 0,
    payment_channel   TEXT
);
CREATE INDEX IF NOT EXISTS idx_txn_date    ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_txn_account ON transactions(account_id);

CREATE TABLE IF NOT EXISTS securities (
    security_id       TEXT PRIMARY KEY,
    ticker            TEXT,
    name              TEXT,
    type              TEXT,
    close_price       REAL,
    close_price_as_of TEXT
);

CREATE TABLE IF NOT EXISTS holding_snapshots (
    account_id  TEXT NOT NULL,
    security_id TEXT NOT NULL,
    date        TEXT NOT NULL,
    quantity    REAL,
    price       REAL,
    value       REAL,
    cost_basis  REAL,
    PRIMARY KEY (account_id, security_id, date)
);

CREATE TABLE IF NOT EXISTS investment_transactions (
    investment_transaction_id TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL,
    security_id TEXT,
    date        TEXT NOT NULL,
    name        TEXT,
    type        TEXT,                           -- buy | sell | cash | fee | transfer | cancel
    subtype     TEXT,                           -- dividend, contribution, ...
    quantity    REAL,
    price       REAL,
    amount      REAL,
    fees        REAL
);
CREATE INDEX IF NOT EXISTS idx_inv_txn_date ON investment_transactions(date);

-- Credit cards and mortgages, one row per account per day.
CREATE TABLE IF NOT EXISTS liability_snapshots (
    account_id             TEXT NOT NULL,
    date                   TEXT NOT NULL,
    kind                   TEXT NOT NULL,       -- credit | mortgage | student
    last_statement_balance REAL,
    minimum_payment        REAL,
    next_payment_amount    REAL,
    next_payment_due_date  TEXT,
    is_overdue             INTEGER,
    apr                    REAL,
    interest_rate          REAL,
    escrow_balance         REAL,
    ytd_interest_paid      REAL,
    ytd_principal_paid     REAL,
    raw                    TEXT,                -- full Plaid object as JSON
    PRIMARY KEY (account_id, date)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    status      TEXT,
    detail      TEXT
);
"""


def _connect(path, readonly=False):
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def secrets_conn():
    return _connect(SECRETS_DB)


def data_conn(readonly=False):
    return _connect(DATA_DB, readonly=readonly)


def init():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with secrets_conn() as c:
        c.executescript(SECRETS_SCHEMA)
    os.chmod(SECRETS_DB, 0o600)
    with data_conn() as c:
        c.executescript(DATA_SCHEMA)


def purge_item(item_id):
    """Delete an Item's token and all data derived from it."""
    with secrets_conn() as s:
        s.execute("DELETE FROM items WHERE item_id = ?", (item_id,))
    with data_conn() as d:
        ids = [r[0] for r in d.execute("SELECT account_id FROM accounts WHERE item_id = ?", (item_id,))]
        for table in ("balance_snapshots", "transactions", "holding_snapshots",
                      "investment_transactions", "liability_snapshots"):
            d.executemany(f"DELETE FROM {table} WHERE account_id = ?", [(i,) for i in ids])
        d.execute("DELETE FROM accounts WHERE item_id = ?", (item_id,))
        d.execute("DELETE FROM connections WHERE item_id = ?", (item_id,))


if __name__ == "__main__":
    init()
    print(f"Initialized {SECRETS_DB} and {DATA_DB}")
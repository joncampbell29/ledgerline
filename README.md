# ledgerline

Self-hosted personal finance aggregator. ledgerline pulls your bank, credit card, brokerage and loan accounts from [Plaid](https://plaid.com) into a SQLite database on your own machine. It renders a daily digest from that data and serves it to an AI agent over a read-only [MCP](https://modelcontextprotocol.io) server, so you can ask things like *"what did I spend on food last month?"* or *"anything weird on my cards this week?"*.

- **Daily digest**: net worth and change since yesterday, balances, 7-day spending vs the prior 7 days, month-to-date cash flow, payments due soon, portfolio movers, unusual charges, and connections that need attention.
- **MCP server**: 15 read-only tools for your agent, from purpose-built metrics down to a sandboxed SQL `query` tool.
- **Local only**: no hosted backend and no telemetry. Your data lives in `data/` and your Plaid tokens live in `secrets/`.

## Privacy model

Read this before you connect real accounts.

- **Your data stays on your machine, except what your agent reads.** Every MCP tool result goes to whatever model your agent runs on (Anthropic, OpenAI, a local model, ...). Treat connecting the MCP server as sharing that data with that provider, under their data policies.
- **Tokens and data are split.** `secrets/secrets.db` holds the Plaid access tokens and is only opened by the web app and the sync job. `data/data.db` holds everything else. The MCP server never opens `secrets/` and never calls Plaid.
- **The MCP server is read-only.** It opens `data.db` in read-only mode. The `query` tool allows only `SELECT` statements through a SQLite authorizer, so `ATTACH`, `PRAGMA` and writes are blocked, and it caps rows and runtime. In Docker it runs with `--network none --read-only` and only `data/` mounted, read-only.
- **Every tool call is audited.** Each call appends the time, tool name, arguments, status and result size to `audit/audit.jsonl`. Results aren't logged.

## Requirements

- A Plaid account. **Sandbox** (fake banks) is free. **Production** (your real accounts) needs Plaid's approval and may cost money; see [Plaid pricing](https://plaid.com/pricing/).
- Docker, **or** [uv](https://docs.astral.sh/uv/) with Python 3.11+.

## Quick start (Docker)

```sh
git clone https://github.com/joncampbell29/ledgerline.git && cd ledgerline
cp .env.example .env            # paste your Plaid client ID and sandbox secret
mkdir -p data secrets audit     # create these yourself, or Docker makes them root-owned
docker compose up -d --build
```

1. Open http://127.0.0.1:8000, pick products and click **Link account**.
2. In sandbox, choose any bank and sign in with `user_good` / `pass_good`. If it asks for a phone number or code, use `415-555-0011` and `123456`.
3. The first sync starts automatically. Run one again any time with **Sync now** or `docker compose run --rm sync`.
4. Read your digest at http://127.0.0.1:8000/digest.

## Quick start (without Docker)

```sh
cp .env.example .env
uv sync
uv run uvicorn api:app          # http://127.0.0.1:8000
uv run sync.py                  # pull from Plaid now
```

Data goes into the same `data/`, `secrets/` and `audit/` folders as the Docker setup.

## Sync daily

Plaid updates data roughly once a day, so one sync each morning is enough. Example crontab line:

```cron
0 6 * * *  cd /path/to/ledgerline && docker compose run --rm sync >> sync.log 2>&1
```

(Without Docker: `cd /path/to/ledgerline && uv run sync.py`.)

## Connect an agent

The MCP server speaks stdio. Your client starts it on demand, so it doesn't need to be running.

**Claude Code, with uv:**

```sh
claude mcp add ledgerline -- uv --directory /path/to/ledgerline run mcp_server.py
```

**Claude Code, with Docker (recommended: no network, read-only):**

```sh
docker compose build
claude mcp add ledgerline -- docker run -i --rm --network none --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  -v /path/to/ledgerline/data:/data:ro -v /path/to/ledgerline/audit:/audit \
  ledgerline python mcp_server.py
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "ledgerline": {
      "command": "uv",
      "args": ["--directory", "/path/to/ledgerline", "run", "mcp_server.py"]
    }
  }
}
```

Use absolute paths everywhere. Any other MCP client works the same way.

### Tools

| Tool | What it returns |
| --- | --- |
| `daily_digest` | Everything in the daily report for one date. A good first call. |
| `account_balances` | Net worth and every account's balance and change over N days |
| `connection_health` | Which institutions are OK, stale, or need reconnecting |
| `spending_by_category` | Net spending per category between two dates |
| `spending_trend` | Last N days vs the N days before, per category |
| `cash_flow` | Income, spending and net between two dates |
| `top_merchants` | Merchants with the most spending between two dates |
| `search_transactions` | Filtered transactions, plus the count and total of all matches |
| `unusual_transactions` | Large charges, spikes, new merchants and duplicates |
| `upcoming_payments` | Card and loan payments due soon |
| `liabilities` | Balances, APRs, minimums, utilization, mortgage escrow |
| `portfolio` | Holdings with value, cost basis, gain and weight, plus movers |
| `investment_activity` | Buys, sells, dividends and fees between two dates |
| `describe_schema` | The database's `CREATE TABLE` statements |
| `query` | One read-only `SELECT`, for anything the tools above don't cover |

## Going to production

1. Get production access in the Plaid dashboard.
2. Run `uv run reset.py` (or `docker compose run --rm sync python reset.py`) to remove your sandbox items. They don't carry over.
3. Set `PLAID_ENV=production` and your production secret in `.env`, restart, and link your real accounts.

## Security notes

- **The web app has no login.** It's meant for one person on one machine. `compose.yaml` binds it to `127.0.0.1` only. Never expose port 8000 to a network or the internet. For remote access use something like Tailscale or an SSH tunnel, and add the hostname to `LEDGERLINE_ALLOWED_HOSTS`.
- The app rejects requests with an unknown `Host` header (DNS rebinding) and state-changing requests from other origins (CSRF from pages in your browser).
- `secrets/secrets.db` is created with mode `600`. Back it up like a password, or don't back it up at all, since you can always re-link.
- **Remove** in the web UI, or `DELETE /items/{item_id}`, revokes the item at Plaid and deletes all of its local data.

## Project layout

| File | Purpose |
| --- | --- |
| `api.py` | Web app: Link page, sync trigger, digest (HTML + JSON) |
| `sync.py` | Pulls everything from Plaid into `data.db`. Idempotent. |
| `metrics.py` | Deterministic metrics shared by the digest and the MCP tools |
| `mcp_server.py` | Read-only MCP server |
| `db.py` | Schema and connections; `python db.py` creates both databases |
| `providers/` | Data-source adapters. Plaid today; see `providers/__init__.py` to add SimpleFIN, Teller, ... |
| `reset.py` | Removes every item at Plaid and locally |

## Contributing

Issues and PRs are welcome. Run `uv run ruff check .` before you push; CI runs the same check plus a Docker build.

## License and disclaimer

MIT; see [LICENSE](LICENSE). ledgerline is not affiliated with or endorsed by Plaid. It's a personal tool, not financial advice. Numbers come from your institutions through Plaid and can be wrong or late, so check anything important with your bank.

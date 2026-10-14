# portfolio-dashboard

LAN-only ETF portfolio dashboard fed from the Scalable Capital MCP.

- `portfolio/`: FastAPI app (SQLite), pages for portfolio, ETF overview and per-ETF transactions.
- `fetcher/`: script run on the Hermes host; reads the MCP through Hermes' OAuth provider and pushes to `/api/ingest`.
- Image: `ghcr.io/umueller74/portfolio-dashboard` (tag `vX.Y.Z` builds `X.Y.Z`).

Money is `Decimal` end to end; cost basis is FIFO; fees come from `get_transaction_details`.

Crossover alerts: after every ingest, each held ETF's 20-day vs 50-day average of the closes (last completed
session) is compared with the side stored in `signals`; a cross up sends BUY, a cross down SELL, through the
Telegram Bot API when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set. First sight of an ETF is recorded silently.

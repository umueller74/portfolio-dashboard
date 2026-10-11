# portfolio-dashboard

LAN-only ETF portfolio dashboard fed from the Scalable Capital MCP.

- `portfolio/`: FastAPI app (SQLite), pages for portfolio, ETF overview and per-ETF transactions.
- `fetcher/`: script run on the Hermes host; reads the MCP through Hermes' OAuth provider and pushes to `/api/ingest`.
- Image: `ghcr.io/umueller74/portfolio-dashboard` (tag `vX.Y.Z` builds `X.Y.Z`).

Money is `Decimal` end to end; cost basis is FIFO; fees come from `get_transaction_details`.

# portfolio-dashboard

LAN-only ETF portfolio dashboard fed from the Scalable Capital MCP.

- `portfolio/`: FastAPI app (SQLite), pages for portfolio, ETF overview and per-ETF transactions.
- `fetcher/`: script run on the Hermes host; reads the MCP through Hermes' OAuth provider and pushes to `/api/ingest`.
- Image: `ghcr.io/umueller74/portfolio-dashboard` (tag `vX.Y.Z` builds `X.Y.Z`).

Money is `Decimal` end to end; cost basis is FIFO; fees come from `get_transaction_details`.

Crossover alerts: after every ingest, each held ETF's 20-day vs 50-day average of the closes (last completed
session) is compared with the side stored in `signals`; a cross up sends BUY, a cross down SELL, or DIP when
the close is still above its 200-day average (a pullback in an uptrend, a buying chance; SELL while fewer than
200 closes exist), through the Telegram Bot API when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set.
First sight of an ETF is recorded silently.

Price history: the MCP's chart is daily only for the current year, so after an ingest every ETF ever traded that
is not in `backfills` yet gets its daily EUR closes since launch from justETF (`portfolio/history.py`), stored in
`closes` for the days before its first broker close. Each ETF is fetched once (new ones on the ingest after their
first trade); a failed fetch is retried on the next ingest. The ETF price chart shows that whole history, the
value chart still starts at the first trade.

Every price: the fetcher reads the `timeframe` values of `get_security_chart` from the tool schema and sends every
point of every timeframe as `ticks`; the app also keeps each quote as a tick. The ETF price chart plots every price
point of the last month in the 1d, 1w and 1m ranges (one per Berlin minute; 1d is the last session), daily closes
for the longer ones. Its "Trendlines" button (off by default) adds an upper and a lower line for each of the last
day, week and month: from the window's earliest highest (lowest) price through the later price that keeps every
later price below (above) the line.

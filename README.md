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

The ETF price chart moves: the mouse wheel zooms the time axis around the pointer, a left drag pans it, ctrl+wheel
scales the y axis; it reaches 65 trading days (daily closes) or 20 sessions (price points) into the future, where
the trendlines continue. The range follows the wheel: it becomes the narrowest one that holds the shown days (zooming
out of 1W selects 1M, then 3M and so on, zooming in goes back down), with the cards, the table and the value chart.
The chart shows every price point while the window fits 1D, 1W or 1M and lies within the last month, the daily closes
otherwise, so zooming out past the first price point continues over the closes and zooming into the last month brings
the price points back. A range button or a double-click resets the window; a click without dragging maximizes.
Its title shows the price's change over the selected range, from the range's first price to the last one;
a dashed line marks that first price, and the prices (line and dots) above it are green, those below red.

Fetch now: with `FETCH_TRIGGER_URL` set (the fetcher host's listener, e.g. `http://raspi2:8770/fetch`), the page shows
a "Fetch now" button. It posts to `/fetch` (behind the page's basicAuth, not `/api`), the app calls the listener with
the ingest token, and the page polls `GET /fetch` until a newer ingest has arrived, then reloads. The listener,
`fetcher/trigger.py` (standard library, env `INGEST_TOKEN`, `FETCH_CMD`, `TRIGGER_PORT` default 8770), runs at most
one `FETCH_CMD` at a time and reports its exit code; a start while one runs queues one follow-up run (202 "queued"),
which begins as soon as the running one ends.

Watchlist: the "Watchlist" group on the portfolio page mirrors the Scalable watchlist both ways. Each fetch, after
the trades, applies the app's pending adds and removes (`/api/watchlist/actions`, read by `fetcher/run.py`), then
reads the list with `list_watchlist_items`, sends it as `watchlist` (with `watchlist_read_at`, when it read it) in
the ingest body and fetches prices for the watched ISINs too; a watched ISIN whose prices cannot be read is left
out of that run. The ✎ popup searches Scalable's securities and adds or removes entries (`/watchlist/search`,
`POST /watchlist`, `DELETE /watchlist/{isin}`, through the listener's `/search` and `/watchlist`), so it needs
`FETCH_TRIGGER_URL`; its remove list holds every Scalable entry, held ones too. Adding starts a fetch (or queues one
behind a running fetch), and an entry added after a running fetch read the list survives that fetch's ingest. A
held ETF always shows as Active, a watched one that was never traded gets a price-only view, and watched entries get
crossover alerts like held ones. Their history comes from justETF for ETFs, for other securities from Scalable's
coarser points before the first close (chart only).

The dashboard also edits the list itself (`portfolio/watch.py`): after a position is sold out (only sell-outs after
the first fetch that read the list count) its ISIN is added to the Scalable watchlist once, and removed again 3
calendar months after the sell-out if the dashboard added it. An entry that was already on the list, or that you
added or removed in the popup, is yours and never removed automatically; buying the ISIN again pauses the clock.

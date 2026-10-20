# Watchlist: watch ETFs, stocks and crypto before buying

## Intent

The user wants to watch securities they are thinking of buying in the same dashboard that shows what
they hold: the same price chart (ranges, trendlines, moving averages) and the same Telegram BUY / SELL /
DIP crossover alerts. The list of watched securities is the user's **Scalable watchlist**, kept in sync
both ways, so the dashboard and the Scalable app always agree.

## Goals

- A **Watchlist** group in the left list of the portfolio page, between Active and Inactive.
- An **edit button next to "Overview"** that opens an inline popup (not a new page):
  - a search box that searches Scalable (`search_securities`); hits show name, type (STOCK, ETF,
    CRYPTO_ETP, …), ISIN and current price; picking a hit adds it to the watchlist;
  - the current watchlist entries, each with a remove button.
- Adding or removing in the popup changes the **Scalable watchlist right away** (`add_watchlist_item`,
  `remove_watchlist_item`). After an add, a fetch starts on its own so the new entry's prices arrive
  without waiting for the timer *(our call)*.
- **Every fetch** (timer and "Fetch now") reads the Scalable watchlist (`list_watchlist_items`) and pulls
  chart points, closes and quotes for every watched ISIN, exactly like for held ones.
- Watched entries get the **same crossover alerts** (BUY / SELL / DIP via Telegram) as held ETFs.
- A watched entry's page shows the price chart like a held ETF. The info boxes show what makes sense
  without a position *(our call)*: last price, change since the last session, change over the selected
  range. No value, invested or P&L, and no empty value chart.

## Group rules (the user's, except where marked)

Each ISIN shows in exactly one group:

| Situation | Group |
|---|---|
| Currently held (quantity > 0) | **Active** — never listed twice, even if also on the Scalable watchlist |
| Traded before, sold out, last trade less than 3 months ago | **Watchlist** |
| Traded before, sold out, last trade 3 months ago or longer, not on the Scalable watchlist | **Inactive** |
| Never traded, on the Scalable watchlist | **Watchlist** |

Transitions the dashboard performs on Scalable:

- **Sell-out → added to the Scalable watchlist.** When a position goes to zero, the dashboard adds the
  ISIN to the Scalable watchlist (unless it is already there), so the sync keeps it in Watchlist.
- **New buy → Active again.** Whether it stays on the Scalable watchlist while held does not matter for
  the dashboard (held always wins); leave it there.
- **3 months without a trade after selling out → removed from the Scalable watchlist** and shown as
  Inactive. "3 months" counts from the last trade (the sell-out) to today.
- *(Our call)* The 3-month removal only applies to entries the dashboard itself added on sell-out. If the
  user adds an ISIN themselves (in the popup or in the Scalable app), it stays until they remove it,
  even if they traded it long ago. A manual add in the popup of an auto-added entry turns it into a
  manual one. If the ISIN was already on the Scalable watchlist before the sell-out, it is not
  auto-added and never auto-removed.
- Removed in the Scalable app → gone from Watchlist after the next fetch (Inactive if traded, otherwise
  not shown at all).
- *(Our call)* The sell-out add happens **once per sell-out**, not as a standing rule. The app records
  "added for the sell-out on day X"; the add is retried only until it succeeds. After that, grouping
  follows the Scalable list: if the user removes the entry (popup or Scalable app) before the 3 months
  are up, it goes to Inactive and is not added again. So the table's "sold out, under 3 months →
  Watchlist" row means "as long as it is on the Scalable watchlist". A new buy and a later sell-out
  start a new sell-out, so a new add.

## First release

- Only sell-outs **after the release** trigger the add (the user's decision). Positions sold out before
  it stay where they are today (Inactive) and nothing is written to the Scalable watchlist on deploy.
- *(Our call)* Everything already on the Scalable watchlist at release counts as added by the user, so
  it is never auto-removed.

## Detail view per entry *(our call)*

- An ISIN with trades (sold out, now in Watchlist) keeps today's sold-ETF view: value/invested chart,
  realized P&L, XIRR, trades table, price chart. Nothing about it changes but the group.
- An ISIN without trades gets the new watched view: price chart only; info boxes with last price,
  change since the last session and change over the selected range; no value chart, no trades table.
  `/view/{isin}` must accept such ISINs (today it 404s without trades).

## Facts checked on raspi2 (2026-10-20, read-only calls)

- The Scalable MCP has `search_securities(query)`, `list_watchlist_items()`, `add_watchlist_item(isin)`,
  `remove_watchlist_item(isin)` (all with an optional `portfolioId`).
- `search_securities` returns `results: [{isin, name, securityType, links, currentQuote: {midPrice,
  currency, timestampUtc, isOutdated}}]`; `list_watchlist_items` returns `items` of the same shape.
  Searching "nvidia" gave 8 hits (stock, CDR, leveraged ETPs); "bitcoin" gave crypto ETPs
  (`CRYPTO_ETP`) and stocks.
- **Crypto exists only as ETPs with ISINs.** There is no chart tool for raw coins (`get_security_chart`
  takes an ISIN). Watching "Bitcoin" means watching an ETP such as 21shares Bitcoin ETP. That is fine.
- The current Scalable watchlist has 4 entries: iShares Core DAX, Xtrackers Nasdaq 100, Amazon, NVIDIA.
- `get_security_chart` for a stock (NVIDIA): `max` is monthly points back to 2016, `one_year` roughly
  every other day, `year_to_date` daily.

## Price history

- ETFs (watched or traded): the existing justETF backfill (`portfolio/history.py`) applies to watched
  ISINs too, not only traded ones. justETF answers 404 for non-ETFs; that already marks them done.
- Stocks and crypto ETPs: no justETF. The chart shows what Scalable gives: daily for the current year,
  the coarser `one_year` and `max` points before that.
- Alerts use daily closes only, as today. Early in a year a stock may have fewer than 200 daily closes,
  so a fall below is SELL instead of DIP. Accepted.
- An ISIN that leaves the watchlist keeps its stored prices; it is only hidden. *(our call)*

## Constraints

- Only the fetcher host (raspi2) holds the Scalable login. The dashboard never talks to Scalable
  itself. Search, add and remove go through the listener on raspi2 (`fetcher/trigger.py`), called by the
  app with the ingest token like "Fetch now" (`portfolio/refresh.py`). The browser calls go through
  page routes behind basicAuth with the `X-Requested-With` guard, like `POST /fetch`, not `/api`.
- The fetcher and listener run on Hermes' interpreter: standard library plus what Hermes' venv has
  (`mcp`). No new dependencies there.
- Do not use port 8765 for anything (Hermes' OAuth callback).
- Money stays `Decimal`; existing behaviour for held and sold ETFs (FIFO, XIRR, charts, alerts) must not
  change except for the group an entry is listed in.
- The fetcher on raspi2 is deployed from a git tag by the homelab role `portfolio-fetcher`
  (`version` in its defaults); the app image by tag. Both need a release, and the homelab role needs the
  version bump. Any new listener path must work with the existing systemd unit. That unit runs the
  listener as the hermes user with the same env file, but starts `trigger.py` with `python -I` by path,
  so it needs the `sys.path` setup `fetcher/entry.py` has before importing the `fetcher` package.
- If the listener is not configured (`FETCH_TRIGGER_URL` unset), the edit button is hidden, like
  "Fetch now". The Watchlist group still shows what the last fetch reported.

## Non-goals

- No raw crypto coins, no derivatives, no order placing.
- No dashboard-only list: the Scalable watchlist is the only list.
- No price alerts at Scalable (`create_price_alert`); alerts stay the dashboard's Telegram ones.

## Left to the builder

- Where the sell-out add and 3-month removal run. The rules need the trades (in the app), the Scalable
  calls need the fetcher. For example the ingest response could return the watchlist actions, or the
  fetcher could ask the app for them before reading the watchlist. Whatever is chosen: an action that
  fails is retried on the next fetch, and the dashboard must remember which entries it auto-added.
- How the ingest carries the watchlist (names and types for never-traded ISINs: there are no trades to
  take the name from) and how the app stores it.
- How the listener runs MCP calls for search/add/remove (its own short MCP session, the same OAuth
  token store as the fetcher) and how it copes with a fetch running at the same time.
- Popup layout and behaviour details (debounce, empty and error states), in the style of the existing
  page.
- How a watched entry's view replaces the value chart.
- Tests in the existing style (`tests/`), README section.

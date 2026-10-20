# Watchlist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Watchlist group (synced both ways with the Scalable watchlist) whose entries get price charts and crossover alerts, plus an inline search/add/remove popup and the sell-out / 3-month rules.

**Architecture:** The fetcher on raspi2 is the only Scalable client. Each fetch first asks the app for pending watchlist actions (sell-out adds, 3-month removals) and applies them, then reads the watchlist and fetches prices for traded **and** watched ISINs, and pushes the watchlist in the ingest body. The app mirrors the watchlist in SQLite, runs the auto-add/remove state machine after each ingest, and groups entries. The popup's search/add/remove go browser → app page route → listener on raspi2 (new paths next to `/fetch`) → MCP.

**Tech Stack:** Python 3.12, FastAPI, SQLite, Jinja2, vanilla JS + Chart.js; fetcher/listener: standard library + Hermes' `mcp`.

**Spec:** `docs/superpowers/specs/2026-10-20-watchlist-design.md`

## Global Constraints

- The app never calls Scalable. Only `fetcher/` talks MCP.
- Fetcher and listener: standard library + Hermes' `mcp` only, no new dependencies.
- Never use port 8765.
- Browser-facing writes go through page routes (basicAuth) with the `X-Requested-With: fetch` guard, like `POST /fetch`; never under `/api`.
- Money is `Decimal`; FIFO/XIRR/charts/alerts for held and sold ETFs unchanged except the group they are listed in.
- Without `FETCH_TRIGGER_URL` the edit button is hidden; the Watchlist group still shows.
- Auto-add only for sell-outs on or after the release (first ingest that carries a watchlist). Never write to Scalable on deploy.
- "3 months" = 3 calendar months after the sell-out day (Berlin date of the last trade), clamped to month end.
- Run tests with `.venv/bin/pytest -q` (or `pytest -q` in the project venv); all must pass after every task.

## Review Focus

1. A fetcher run against an app that does not have `/api/watchlist/actions` yet (old image) or with the listener lacking new paths: the fetch must still deliver trades/prices (log and continue). → Task 2 test `test_collect_survives_actions_endpoint_failure`.
2. An ingest from an old fetcher (no `watchlist` key) must not wipe the stored watchlist or trigger auto-adds. → Task 3 test `test_ingest_without_watchlist_keeps_it`.
3. Re-buying and selling out again starts a new auto-add even though the old row is `done`. → Task 3 test `test_new_sellout_after_rebuy_adds_again`.
4. Search with an empty/1-char query or a listener that is down returns a clear error JSON, not a 500. → Task 4 test `test_search_short_query_and_listener_down`.
5. A never-traded ISIN with no closes yet (just added, fetch still running) must render without errors. → Task 5 test `test_watched_view_without_prices`.

---

### Task 1: Listener MCP paths (search, add, remove)

**Files:**
- Create: `fetcher/session.py`
- Modify: `fetcher/run.py` (`_main` uses `session.open_call`), `fetcher/trigger.py`
- Test: `tests/test_trigger.py`

**Interfaces:**
- Produces: `fetcher.session.open_call(url: str) -> AsyncContextManager[Call]` where `Call = async (tool: str, args: dict) -> dict` (moved out of `run._main`, same behaviour incl. `is_error` → `RuntimeError`).
- Produces: `trigger.make_server(host, port, token, runner, mcp=None)`; `mcp: Callable[[str, dict], dict] | None` (sync). Endpoints, all bearer-guarded, JSON:
  - `GET /search?q=<text>` → `200 {"results": [{"isin","name","type","price","currency"}]}` from `search_securities`; `400 {"error"}` if `q` stripped is shorter than 2 chars.
  - `POST /watchlist` body `{"isin": str, "action": "add"|"remove"}` → calls `add_watchlist_item` / `remove_watchlist_item` → `200 {"status": "ok"}`; `400` on bad body.
  - MCP failure → `502 {"error": str(e)}`; `mcp is None` → `404`.
  - `/fetch` unchanged.
- Produces: `trigger.mcp_caller(url: str) -> Callable[[str, dict], dict]` — runs `asyncio.run` of one `open_call` session per call. `main()` passes `mcp_caller(os.environ.get("MCP_URL", "https://mcp.scalable.capital/mcp"))` after `auth_hermes.bootstrap()`.

- [ ] **Step 1: Write failing tests** in `tests/test_trigger.py` using a fake `mcp` recording `(tool, args)`:
  - `test_search_maps_hits`: fake returns `{"results":[{"isin":"US67066G1040","name":"NVIDIA","securityType":"STOCK","currentQuote":{"midPrice":192.52,"currency":"EUR"}}]}`; `GET /search?q=nvidia` → 200, `results == [{"isin":"US67066G1040","name":"NVIDIA","type":"STOCK","price":"192.52","currency":"EUR"}]`, recorded `("search_securities", {"query":"nvidia"})`. Hits without `currentQuote` give `price: None`.
  - `test_search_rejects_short_query`: `q=a` → 400.
  - `test_watchlist_add_remove`: POST add → `("add_watchlist_item", {"isin": X})`, remove → `("remove_watchlist_item", {"isin": X})`, both 200; `action: "x"` → 400.
  - `test_mcp_error_is_502` and `test_unauthorized_new_paths` (401 without token).
- [ ] **Step 2:** `pytest tests/test_trigger.py -q` → new tests FAIL.
- [ ] **Step 3: Implement.** Route on `urllib.parse.urlsplit(self.path).path`; read body with `Content-Length`. Add at the top of `trigger.py` the same `sys.path.insert` as `fetcher/entry.py` so `python -I .../trigger.py` can import `fetcher.*` (import `session`/`auth_hermes` lazily inside `mcp_caller`/`main`, keeping tests free of `mcp`).
- [ ] **Step 4:** `pytest -q` → all PASS.
- [ ] **Step 5:** Commit `Add search and watchlist paths to the fetch listener`.

### Task 2: Fetcher applies actions, reads the watchlist, fetches watched ISINs

**Files:**
- Modify: `fetcher/run.py`, `fetcher/normalize.py`
- Test: `tests/test_run.py`, `tests/test_normalize.py`

**Interfaces:**
- Consumes: app endpoint from Task 3: `GET {base}/api/watchlist/actions` (bearer) → `{"add": [isin], "remove": [isin]}`, with `base` = `INGEST_URL` minus the trailing `/api/ingest`.
- Produces: `normalize.parse_watchlist(payload) -> list[dict]` → `[{"isin","name","type"}]` from `items`.
- Produces: `run.collect(call, state_dir, timeframes=..., actions=None) -> dict`; `actions: {"add": [...], "remove": [...]} | None`. Body gains `"watchlist": [{"isin","name","type"}]` (omitted when `list_watchlist_items` fails, so the app keeps the old list).
- Produces: `run.pending_actions(ingest_url, token, opener=urllib.request.urlopen) -> dict | None` — None on any error (logged).

- [ ] **Step 1: Write failing tests:**
  - `test_parse_watchlist` with the real shape (`{"portfolioId":..,"items":[{"isin":"US0231351067","name":"Amazon.com","securityType":"STOCK","currentQuote":{...}}]}`) → `[{"isin":"US0231351067","name":"Amazon.com","type":"STOCK"}]`.
  - `test_collect_applies_actions_then_reads_watchlist`: fake call handles `add_watchlist_item`, `remove_watchlist_item`, `list_watchlist_items` (returns one never-traded ISIN `US67066G1040`); `actions={"add":["IE0000000002"],"remove":["IE0000000003"]}`; assert the add/remove calls come before `list_watchlist_items` in the log, `body["watchlist"]` holds the item, and `get_security_chart`/`get_security_quote` were called for `US67066G1040` too.
  - `test_collect_action_failure_does_not_fail_run`: add raises → run still returns trades and watchlist.
  - `test_collect_survives_actions_endpoint_failure`: `pending_actions` with an opener raising `HTTPError(404)` → None; `collect(..., actions=None)` makes no add/remove calls.
  - `test_collect_without_watchlist_tool`: `list_watchlist_items` raises → no `"watchlist"` key, trades still there.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: Implement.** Order in `collect`: trades/details → actions (each in try/except, logged) → watchlist → prices for `sorted(traded | watched)`. `_main`: `actions = pending_actions(...)` before opening the session.
- [ ] **Step 4:** `pytest -q` → PASS.
- [ ] **Step 5:** Commit `Fetch the Scalable watchlist and apply pending watchlist actions`.

### Task 3: App stores the watchlist and runs the sell-out rules

**Files:**
- Create: `portfolio/watch.py`
- Modify: `portfolio/db.py`, `portfolio/ingest.py`
- Test: `tests/test_watch.py`

**Interfaces:**
- Schema (in `SCHEMA`):
  `create table if not exists watchlist(isin text primary key, name text not null, type text not null);`
  `create table if not exists watch_auto(isin text primary key, sold_on text not null, state text not null check (state in ('pending_add','added','pending_remove','done')));`
  meta key `watch_since` (ISO date) = Berlin date of the first ingest that carried `watchlist`.
- `IngestIn.watchlist: list[WatchIn] | None = None`, `WatchIn(isin: str, name: str, type: str)`. When not None: replace the `watchlist` table, set `watch_since` if missing, then `watch.reconcile(conn, today)` in the same request (cheap, before the background tasks).
- Produces in `portfolio/watch.py`:
  - `HOLD_MONTHS = 3`
  - `plus_months(d: date, n: int) -> date` (clamped to month end).
  - `sellouts(conn) -> dict[str, date]` — ISIN → Berlin day of its last trade, for every traded ISIN whose FIFO remaining quantity is 0 (`services.load_trades` + `fifo.replay`; `{}` on `OversellError`).
  - `reconcile(conn, today: date) -> None` — the state machine below.
  - `actions(conn) -> dict` → `{"add": [pending_add isins], "remove": [pending_remove isins]}` (sorted).
  - `watched(conn) -> dict[str, dict]` — ISINs shown in the Watchlist group if not held: rows of `watchlist` plus `watch_auto` rows in `pending_add`/`added`; value `{"name","type"}` (name from `watchlist`, else from trades).
  - `remember(conn, isin: str, name: str, type: str) -> None` — upsert into `watchlist`.
  - `manual(conn, isin: str, added: bool) -> None` — the popup changed the entry: set its `watch_auto` row (if any) to `done`; `added=False` also deletes it from `watchlist` (for `added=True` the caller calls `remember` first).
- Route `GET /api/watchlist/actions` (`require_token`) → `watch.actions(conn)`.

State machine in `reconcile` (`on` = ISIN in `watchlist` table, `since` = `watch_since`):
- Held ISINs: delete their `watch_auto` row.
- For each sell-out `(isin, day)` with `day >= since`: if no row or row's `sold_on != day` → insert/replace with `done` if `on` (already on the list: user's own) else `pending_add`.
- Then per row: `pending_add` & `on` → `added`; `added` & not `on` → `done`; `added` & `today >= plus_months(sold_on, 3)` → `pending_remove`; `pending_remove` & not `on` → `done`.

- [ ] **Step 1: Write failing tests** in `tests/test_watch.py` (seed via `client.post("/api/ingest", ...)` like `tests/test_services.seed`; call `reconcile` with explicit `today`):
  - `test_plus_months`: `plus_months(date(2026,11,30),3) == date(2027,2,28)`.
  - `test_sellout_before_release_is_ignored`: sold out 2026-09-01, first watchlist ingest 2026-10-20 → no row, `actions == {"add":[],"remove":[]}`.
  - `test_sellout_after_release_is_added_then_confirmed`: watchlist ingest (empty list) on day 1, sell-out ingest later → `actions["add"] == [ISIN]`; next ingest with the ISIN in `watchlist` → state `added`, actions empty, `ISIN in watched(conn)`.
  - `test_three_months_then_removed`: `added`, `reconcile(today=plus_months(sold_on,3) - 1 day)` stays; on the day → `actions["remove"] == [ISIN]`; ingest without it → `done`, not in `watched`.
  - `test_user_removal_sticks`: `added`, ingest without the ISIN before 3 months → `done`, not re-added.
  - `test_already_on_list_is_never_auto`: on the list at sell-out → `done`; after 3 months no remove action, still in `watched`.
  - `test_new_sellout_after_rebuy_adds_again`: `done` row, buy + sell later → new `pending_add`.
  - `test_ingest_without_watchlist_keeps_it`: ingest with list, then one without the key → table unchanged, `watch_since` unchanged.
  - `test_actions_endpoint_requires_token`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `pytest -q` → PASS.
- [ ] **Step 5:** Commit `Mirror the Scalable watchlist and auto-add sold-out positions`.

### Task 4: Page routes for search, add and remove

**Files:**
- Modify: `portfolio/refresh.py`, `portfolio/web.py`
- Test: `tests/test_web.py`, `tests/test_refresh.py` (create if missing)

**Interfaces:**
- `refresh.trigger_client(url, token, opener=...)` returns `call(method: str, path: str = "/fetch", body: dict | None = None) -> tuple[int, dict]`; `path` replaces the `/fetch` at the end of `url` (`http://raspi2:8770/fetch` + `/search?q=x` → `http://raspi2:8770/search?q=x`); `body` is sent as JSON. Existing `trigger("POST")`/`trigger("GET")` callers keep working.
- Page routes (guard `X-Requested-With: fetch` on all three; 404 `{"error"}` without trigger; `OSError` → 502 `{"error": "fetcher unreachable: ..."}`):
  - `GET /watchlist/search?q=` → listener `/search?q=` (URL-encoded); passes 400/502 through; on 200 adds `"state"` per hit: `"held"` / `"watched"` / `""` (from `services.overview` and `watch.watched`).
  - `POST /watchlist` body `{"isin","name","type"}` → listener `POST /watchlist {"isin","action":"add"}`; on 200: `watch.remember(...)`, `watch.manual(conn, isin, True)`, then `trigger("POST")` (409 = already running is fine) → `{"status":"ok"}`.
  - `DELETE /watchlist/{isin}` → listener remove; on 200 `watch.manual(conn, isin, False)` → `{"status":"ok"}`.

- [ ] **Step 1: Write failing tests** with a fake trigger `call(method, path="/fetch", body=None)` recording calls (pass via `create_app(..., trigger=fake)`):
  - `test_trigger_client_paths` (opener fake): path replacement and JSON body.
  - `test_search_marks_held_and_watched`.
  - `test_add_stores_entry_and_starts_fetch`: afterwards `watch.watched(conn)` has the ISIN and the fake saw `("POST", "/fetch")`.
  - `test_add_turns_auto_row_manual`: `added` row → `done` after add.
  - `test_remove_deletes_entry`.
  - `test_watchlist_routes_need_header` (403) and `test_search_short_query_and_listener_down` (listener 400 passed through; fake raising `OSError` → 502).
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `pytest -q` → PASS.
- [ ] **Step 5:** Commit `Add search, add and remove routes for the watchlist`.

### Task 5: Groups, watched views, alerts and history for watched ISINs

**Files:**
- Modify: `portfolio/web.py`, `portfolio/signals.py`, `portfolio/history.py`, `portfolio/services.py`
- Test: `tests/test_web.py`, `tests/test_signals.py`, `tests/test_history.py`

**Interfaces:**
- `web.etf_tabs(conn, ov) -> list[dict]`: each tab gains `"group": "active"|"watch"|"inactive"` (keep `"active": bool` = group == active) and `"traded": bool`. Order: active (by value), watch (by name), inactive (by name). Held wins; a watched ISIN without trades has `rows: []`.
- `view_data(conn, isin)` for an ISIN without trades: `labels/values/invested = []`, markers empty, `watch: True`; price part as today (closes, MAs, ticks). For every ISIN (traded or not), days before its first close get the last tick of each Berlin day prepended to `plabels`/`second`, with `None` for `ma20`/`ma50` and markers there (`services.coarse_closes(conn, isin) -> list[tuple[date, Decimal]]`).
- `card_texts`: for a never-traded ISIN per range `{"price": "<last close or quote> €", "session": "<±abs> € (<±pct> %)", "range": "<±abs> € (<±pct> %)"}` (`"n/a"` when missing). Traded ISINs keep today's cards.
- `/view/{isin}`: 404 only if the ISIN has neither trades nor `watch.watched` membership.
- `signals.check`: candidates = held ∪ (`watch.watched(conn)` not held), names from overview or `watched`. The deletion of stored sides applies only to ISINs in neither.
- `history.backfill`: pending ISINs = traded ∪ `watchlist` table ISINs not in `backfills`.

- [ ] **Step 1: Write failing tests:**
  - `test_tabs_groups`: held → active; sold out + `added` → watch (`traded: True`); sold out, no row, not listed → inactive; never traded on list → watch (`traded: False`); held and on list → only active.
  - `test_page_shows_watchlist_group_between`: HTML index of `>Active<` < `>Watchlist<` < `>Inactive<`.
  - `test_watched_view_without_prices` and `test_watched_view_with_closes` (`/view/{isin}` → 200, `view["watch"] is True`, `cards[range]["price"]`).
  - `test_coarse_ticks_prepended` (ticks on 2025-11-03 and 2025-12-01, first close 2026-01-02 → `plabels[:2] == ["2025-11-03","2025-12-01"]`, `ma20[:2] == [None, None]`).
  - `test_signal_for_watched_isin` (never-traded watched ISIN crossing → BUY sent with its watchlist name).
  - `test_backfill_includes_watched`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `pytest -q` → PASS.
- [ ] **Step 5:** Commit `List watched securities with their charts and alerts`.

### Task 6: Page: Watchlist group, edit popup, watched view

**Files:**
- Modify: `portfolio/templates/portfolio.html`
- Test: `tests/test_web.py`; manual check in a browser (run skill / local uvicorn on a port other than 8765)

**Interfaces:**
- Consumes: Task 4 routes, Task 5 tab `group`/`traded` and `view.watch`/cards.

Behaviour:
- Left list: `Overview` + an edit button (`id="watchEdit"`, `✎`, title "Edit watchlist") shown only when `fetch_enabled`; groups Active, Watchlist, Inactive (labels exactly these).
- Popup (`id="watchPop"`, anchored below the button, closes on Esc / outside click): search input (debounce 300 ms, ≥ 2 chars) → hits as rows "name · type · ISIN · price €" with an "Add" button, or a "held" / "on watchlist" label instead; below, the current Watchlist entries with "Remove". Calls carry `X-Requested-With: fetch`. Errors show inline in the popup. After add/remove: reload the page keeping `?etf=`; after add the existing "Fetch now" polling indicator runs (reuse its function) so the page reloads again when the fetch lands.
- Watched view (`view.watch`): value chart box hidden and price chart full width; trades table hidden; info boxes show Price / Session / Range change from the cards.

- [ ] **Step 1: Write failing tests:** `test_edit_button_only_with_trigger` (`id="watchEdit"` present with a trigger, absent without); `test_watchlist_entries_in_page_data` (`D.groups`/tabs JSON carries `group`).
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement in the page's style (existing CSS variables, `.btn`, `.grp`).
- [ ] **Step 4:** `pytest -q` → PASS; start the app locally with a seeded DB and a fake listener, check: popup search, add, remove, Watchlist group order, watched view, no console errors.
- [ ] **Step 5:** Commit `Add the watchlist popup and group to the portfolio page`.

### Task 7: Docs, release, deploy

**Files:**
- Modify: `README.md`; `~/Workspace/homelab/roles/portfolio-fetcher/defaults/main.yml` (`version`)

- [ ] **Step 1:** README: a "Watchlist" paragraph in the existing style (sync both ways, sell-out add after release, 3-month removal, listener paths `/search` and `/watchlist`, `/api/watchlist/actions`).
- [ ] **Step 2:** `pytest -q` → PASS. Commit, push branch `watchlist`, open PR, merge.
- [ ] **Step 3:** Tag `v0.20.0` on main, push the tag; wait for the image workflow to succeed (`gh run watch`).
- [ ] **Step 4:** k8s-homelab: set `image: ghcr.io/umueller74/portfolio-dashboard:0.20.0` in `apps/base/portfolio/deployment.yaml`, commit `portfolio: image 0.20.0, watchlist`, PR + merge; wait for Flux to roll it out.
- [ ] **Step 5:** homelab: bump `version: v0.20.0`, PR + merge, run `setup_host_raspi2.yml --tags portfolio`.
- [ ] **Step 6:** Verify live: "Fetch now" succeeds; the 4 Scalable watchlist entries appear in Watchlist; search "nvidia" in the popup returns hits; journal of `portfolio-fetcher` shows no errors.

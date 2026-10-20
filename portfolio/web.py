from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from portfolio import services, watch

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STALE_AFTER_HOURS = 36
RANGES = ["1d", "1w", "1m", "3m", "6m", "ytd", "1y", "all"]


def _eur(v) -> str:
    return "n/a" if v is None else f"{Decimal(v):,.2f}"


def _signed(v) -> str:
    return "n/a" if v is None else f"{Decimal(v):+,.2f}"


def _qty(v) -> str:
    return "n/a" if v is None else f"{Decimal(v).normalize():f}"


def _berlin_date(dt: datetime) -> str:
    return dt.astimezone(services.BERLIN).strftime("%Y-%m-%d")


templates.env.filters.update(eur=_eur, signed=_signed, qty=_qty, berlin_date=_berlin_date)
templates.env.tests["prime_buy"] = services.prime_buy
templates.env.globals["prime_note"] = services.PRIME_NOTE


def _last_ingest(conn) -> datetime | None:
    row = conn.execute("select value from meta where key='last_ingest'").fetchone()
    return datetime.fromisoformat(row["value"]) if row else None


def _ctx(request: Request, **extra) -> dict:
    last = _last_ingest(request.app.state.conn)
    age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600 if last else None
    return {"last_ingest": last, "stale": last is None or age_h > STALE_AFTER_HOURS,
            "fetch_enabled": request.app.state.trigger is not None, **extra}


def marker_series(markers: dict, labels: list[date], values: list[float]) -> dict:
    """One array per marker category, aligned with the chart labels (value where a trade happened, else None),
    plus the tooltip lines for each label."""
    out: dict = {c: [] for c in services.MARKER_CATEGORIES}
    out["tooltips"] = []
    for day, value in zip(labels, values):
        todays = markers.get(day, [])
        present = {m.category for m in todays}
        for c in services.MARKER_CATEGORIES:
            out[c].append(value if c in present else None)
        out["tooltips"].append([
            f"{m.name}: {m.side} {_qty(m.quantity)} · {_eur(m.amount)} €" for m in todays])
    return out


def etf_tabs(conn, ov) -> list[dict]:
    """One entry per ETF ever traded or watched, in its group: "active" (held, by value), "watch" (not held but
    on the watchlist, by name), "inactive" (the other traded ones, by name). Each carries its trade rows for
    the table (none for a never-traded one), whether it was ever traded, and whether it is on the Scalable
    watchlist (`listed`, so the popup can remove a held one too)."""
    held = [h.isin for h in ov.holdings]
    traded = {t.isin: t.name for t in services.load_trades(conn)}
    on = watch.watched(conn)
    listed = {r[0] for r in conn.execute("select isin from watchlist")}
    names = {**{i: e["name"] for i, e in on.items()}, **traded}   # a traded ETF keeps its trades' name
    by_name = lambda isins: sorted(isins, key=lambda i: names[i])
    groups = [("active", held), ("watch", by_name(i for i in on if i not in held)),
              ("inactive", by_name(i for i in traded if i not in held and i not in on))]
    return [{"isin": i, "name": names[i], "group": g, "active": g == "active", "traded": i in traded,
             "listed": i in listed, "rows": services.isin_detail(conn, i).rows if i in traded else []}
            for g, isins in groups for i in isins]


def _card_texts_for(conn, ov, today: date, isin: str | None) -> dict:
    series = services.value_series(conn, isin)
    change = services.last_session_change(conn, isin)
    session = "n/a" if change is None else f"{_signed(change[0])} € ({_signed(change[1])} %)"
    out = {}
    for key in RANGES:
        st = services.range_stats(conn, ov, key, today, isin, series)
        out[key] = {
            "value": f"{_eur(ov.total_value if isin is None else _held(ov, isin, 'value'))} €",
            "value_sub": "" if st.value_delta is None else f"{_signed(st.value_delta)} € in range",
            "invested": f"{_eur(ov.total_cost if isin is None else _held(ov, isin, 'cost'))} €",
            "invested_sub": "" if st.invested_delta is None else f"{_signed(st.invested_delta)} € in range",
            "pnl": f"{_signed(st.pnl_delta)} €" + ("" if st.pnl_pct is None else f" ({_signed(st.pnl_pct)} %)"),
            "pnl_sub": f"unrealized {_signed(st.unrealized)} · realized {_signed(st.realized)}",
            "pnl_pos": st.pnl_delta >= 0,
            "xirr": "n/a" if st.xirr is None else f"{st.xirr * 100:+.2f} %",
            "session": session}
    return out


def _held(ov, isin: str, attr: str):
    h = next((x for x in ov.holdings if x.isin == isin), None)
    return getattr(h, attr) if h else Decimal(0)


def _change(old: Decimal, new: Decimal) -> str:
    return f"{_signed(new - old)} € ({_signed((new - old) / old * 100 if old else None)} %)"


def _watch_cards(conn, today: date, isin: str) -> dict:
    """A never-traded security's info boxes per range: its price (the quote, else the last close), the change
    since the session before and the change from the range's first close (the one before the last for 1d) to
    that price."""
    rows = services._closes(conn).get(isin, [])
    quote = conn.execute("select price from quotes where isin=?", (isin,)).fetchone()
    price = Decimal(quote["price"]) if quote else rows[-1][1] if rows else None
    session = _change(rows[-2][1], rows[-1][1]) if len(rows) >= 2 else "n/a"
    days = [d for d, _ in rows]
    out = {}
    for key in RANGES:
        if key == "1d":
            start = rows[-2][1] if len(rows) >= 2 else None
        elif rows:
            i = services.range_start(days, key, today)
            start = rows[0 if i is None else min(i, len(rows) - 1)][1]
        else:
            start = None
        out[key] = {"price": "n/a" if price is None else f"{_eur(price)} €", "session": session,
                    "range": "n/a" if start is None or price is None else _change(start, price)}
    return out


def card_texts(conn, ov, today: date, isins: list[str]) -> dict:
    """The info boxes' texts per view ("" = whole portfolio, else an ISIN) and range, so the buttons can
    swap them without a reload. A never-traded (watched) ISIN gets its price boxes, even without holdings."""
    traded = {t.isin for t in services.load_trades(conn)}
    out = {} if ov.error or not ov.holdings else {
        i: _card_texts_for(conn, ov, today, i or None) for i in ["", *isins] if not i or i in traded}
    out.update({i: _watch_cards(conn, today, i) for i in isins if i not in traded})
    return out


def view_data(conn, isin: str | None) -> dict:
    """Chart data for the whole portfolio (isin None) or one ETF. `second` feeds the right-hand chart:
    profit/loss for the portfolio, the ETF's own price (plus `ma20`/`ma50`) for a single ETF. That chart has
    its own days (`plabels`, `pmarkers`): an ETF's price goes back to its oldest close, before the first trade,
    and before that to the last stored price of each earlier day (`services.coarse_closes`; no averages or
    markers there). A never-traded (watched) ETF has no value chart and is flagged `watch`."""
    series = services.value_series(conn, isin)   # the full history; the range buttons slice it in the browser
    days = [p.day for p in series]
    values = [p.value for p in series]
    out = {"labels": [d.isoformat() for d in days], "values": values, "invested": [p.invested for p in series],
           "markers": marker_series(services.trade_markers(conn, days, isin), days, values)}
    if isin is None:
        out.update(plabels=out["labels"], second=[round(p.value - p.invested, 2) for p in series],
                   pmarkers=out["markers"])
        return out
    if not any(t.isin == isin for t in services.load_trades(conn)):
        out["watch"] = True
    closes = services._closes(conn).get(isin, [])
    coarse = services.coarse_closes(conn, isin)
    pdays, prices = [d for d, _ in closes], [float(p) for _, p in closes]
    pad = [None] * len(coarse)
    pmarkers = marker_series(services.trade_markers(conn, pdays, isin), pdays, prices)
    pmarkers = {k: ([[] for _ in coarse] if k == "tooltips" else pad) + v for k, v in pmarkers.items()}
    out.update(plabels=[d.isoformat() for d, _ in coarse] + [d.isoformat() for d in pdays],
               second=[float(p) for _, p in coarse] + prices, pmarkers=pmarkers)
    # 20- and 50-day averages of the ETF's price, over its whole history (range-independent)
    out.update({f"ma{n}": pad + ma for n, ma in services.moving_averages(conn, isin, pdays).items()})
    out.update(tick_data(conn, isin))
    return out


TICK_DAYS = 31   # every stored price is shown for the short ranges (1d, 1w, 1m) only


def tick_data(conn, isin: str) -> dict:
    """Every price of the ETF's last month, one per Berlin minute (the later wins), for the price chart in the
    short ranges: `tlabels` (Berlin "YYYY-MM-DD HH:MM"), `tprices`, the day's averages and the trade markers."""
    rows = services.ticks(conn, isin)
    if not rows:
        return {"tlabels": [], "tprices": [], "tma20": [], "tma50": [], "tmarkers": marker_series({}, [], [])}
    since = rows[-1][0] - timedelta(days=TICK_DAYS)
    by_minute = {at.astimezone(services.BERLIN).replace(second=0, microsecond=0): float(p) for at, p in rows if at >= since}
    points, prices = list(by_minute), list(by_minute.values())
    markers = services.trade_markers(conn, points, isin, at=lambda t: t.executed_at)
    out = {"tlabels": [p.strftime("%Y-%m-%d %H:%M") for p in points], "tprices": prices,
           "tmarkers": marker_series(markers, points, prices)}
    out.update({f"tma{n}": ma for n, ma in services.moving_averages(conn, isin, [p.date() for p in points]).items()})
    return out


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/")
async def portfolio_page(request: Request, range: str = "1y", etf: str = ""):
    conn = request.app.state.conn
    today = date.today()
    ov = services.overview(conn, today)
    tabs = etf_tabs(conn, ov)
    # the portfolio view, plus the ETF the page opens on (`etf`, kept in the URL so a reload returns to it);
    # the page fetches the other ETFs' from /view/{isin} in the background
    etf = etf if etf in {t["isin"] for t in tabs} else ""
    views = {"": view_data(conn, None), **({etf: view_data(conn, etf)} if etf else {})}
    return templates.TemplateResponse(request, "portfolio.html", _ctx(
        request, ov=ov, range=range if range in RANGES else "1y", ranges=RANGES,
        range_days=services.RANGE_DAYS, today=today.isoformat(), etf=etf,
        tabs=tabs, views=views, cards=card_texts(conn, ov, today, [etf] if etf else [])))


@router.get("/view/{isin}")
def etf_view(request: Request, isin: str):
    """One ETF's chart data and info box texts, as the portfolio page holds them in `D.views` and `D.cards`."""
    conn = request.app.state.conn
    traded = conn.execute("select 1 from trades where isin=?", (isin,)).fetchone()
    if not traded and isin not in watch.watched(conn):
        return JSONResponse({"error": "unknown ETF"}, status_code=404)
    today = date.today()
    ov = services.overview(conn, today)
    return {"view": view_data(conn, isin), "cards": card_texts(conn, ov, today, [isin]).get(isin, {})}


@router.get("/etf")
async def etf_list(request: Request):
    ov = services.overview(request.app.state.conn, date.today())
    return templates.TemplateResponse(request, "etf_list.html", _ctx(request, ov=ov))


@router.get("/etf/{isin}")
async def etf_detail(request: Request, isin: str):
    detail = services.isin_detail(request.app.state.conn, isin)
    return templates.TemplateResponse(request, "etf_detail.html", _ctx(request, isin=isin, d=detail))


# "Fetch now": not under /api, so the page's basicAuth guards it; the header keeps cross-site forms out
@router.post("/fetch")
def fetch_now(request: Request, x_requested_with: str = Header(default="")):
    trigger = request.app.state.trigger
    if trigger is None:
        return JSONResponse({"error": "no fetch trigger configured"}, status_code=404)
    if x_requested_with != "fetch":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        code, body = trigger("POST")
    except OSError as e:
        return JSONResponse({"error": f"fetcher unreachable: {e}"}, status_code=502)
    return JSONResponse(body, status_code=code)


@router.get("/fetch")
def fetch_state(request: Request):
    """What the page polls after "Fetch now": the last ingest (a new one means done) and the fetcher's state."""
    last = _last_ingest(request.app.state.conn)
    out = {"last_ingest": last.isoformat() if last else None}
    trigger = request.app.state.trigger
    if trigger is None:
        return JSONResponse({"error": "no fetch trigger configured"}, status_code=404)
    try:
        out.update(trigger("GET")[1])
    except OSError as e:
        out["error"] = f"fetcher unreachable: {e}"
    return out


def _guard(request: Request, x_requested_with: str):
    """The error response for a watchlist call that is not allowed (no trigger, no script header), else None."""
    if request.app.state.trigger is None:
        return JSONResponse({"error": "no fetch trigger configured"}, status_code=404)
    if x_requested_with != "fetch":
        return JSONResponse({"error": "forbidden"}, status_code=403)


def _watchlist_call(request: Request, method: str, path: str, body: dict | None = None):
    """(listener's 200 reply, None), or (None, the error response) when the listener fails or is unreachable."""
    try:
        code, reply = request.app.state.trigger(method, path, body)
    except OSError as e:
        return None, JSONResponse({"error": f"fetcher unreachable: {e}"}, status_code=502)
    if code != 200:
        return None, JSONResponse(reply, status_code=code)
    return reply, None


@router.get("/watchlist/search")
def watchlist_search(request: Request, q: str = "", x_requested_with: str = Header(default="")):
    """The Scalable search for the popup; each hit is marked as held, already watched, or neither."""
    if err := _guard(request, x_requested_with):
        return err
    reply, err = _watchlist_call(request, "GET", f"/search?q={quote(q, safe='')}")
    if err:
        return err
    conn = request.app.state.conn
    held = {h.isin for h in services.overview(conn, date.today()).holdings}
    on = watch.watched(conn)
    for hit in reply.get("results", []):
        hit["state"] = "held" if hit["isin"] in held else "watched" if hit["isin"] in on else ""
    return reply


@router.post("/watchlist")
async def watchlist_add(request: Request, x_requested_with: str = Header(default="")):
    if err := _guard(request, x_requested_with):
        return err
    try:
        entry = await request.json()
        isin, name, kind = entry["isin"], entry["name"], entry.get("type", "")
    except (ValueError, KeyError, TypeError, AttributeError):
        return JSONResponse({"error": "isin and name needed"}, status_code=400)
    if not all(isinstance(v, str) for v in (isin, name, kind)) or not isin or not name:
        return JSONResponse({"error": "isin and name must be non-empty strings"}, status_code=400)
    _, err = _watchlist_call(request, "POST", "/watchlist", {"isin": isin, "action": "add"})
    if err:
        return err
    conn = request.app.state.conn
    watch.remember(conn, isin, name, kind)
    watch.manual(conn, isin, True)
    try:
        request.app.state.trigger("POST")   # while a fetch runs, the listener queues one more that sees the entry
    except OSError:
        pass                                # added on Scalable; the next scheduled fetch picks it up
    return {"status": "ok"}


@router.delete("/watchlist/{isin}")
def watchlist_remove(request: Request, isin: str, x_requested_with: str = Header(default="")):
    if err := _guard(request, x_requested_with):
        return err
    conn = request.app.state.conn
    auto = conn.execute("select state from watch_auto where isin=?", (isin,)).fetchone()
    listed = conn.execute("select 1 from watchlist where isin=?", (isin,)).fetchone()
    if not (auto and auto[0] == "pending_add" and not listed):   # an auto-add not sent yet: just cancel it
        _, err = _watchlist_call(request, "POST", "/watchlist", {"isin": isin, "action": "remove"})
        if err:
            return err
    watch.manual(conn, isin, False)
    return {"status": "ok"}


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request):
    conn = request.app.state.conn
    last = _last_ingest(conn)
    n = conn.execute("select count(*) from trades").fetchone()[0]
    return (
        "# TYPE portfolio_last_ingest_timestamp_seconds gauge\n"
        f"portfolio_last_ingest_timestamp_seconds {int(last.timestamp()) if last else 0}\n"
        "# TYPE portfolio_trades_total gauge\n"
        f"portfolio_trades_total {n}\n"
    )

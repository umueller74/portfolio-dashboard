from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio.fifo import Lot, OversellError, Trade, replay
from portfolio.xirr import xirr

BERLIN = ZoneInfo("Europe/Berlin")
RANGE_DAYS = {"1w": 7, "1m": 30, "3m": 91, "6m": 182, "1y": 365}
MIN_XIRR_AGE_DAYS = 90


def _day(dt: datetime) -> date:
    return dt.astimezone(BERLIN).date()


def load_trades(conn) -> list[Trade]:
    return [
        Trade(r["id"], r["isin"], r["name"], r["side"], r["kind"], datetime.fromisoformat(r["executed_at"]),
              Decimal(r["quantity"]), Decimal(r["price"]), Decimal(r["fee"]))
        for r in conn.execute("select * from trades order by executed_at, id")
    ]


def _closes(conn) -> dict[str, list[tuple[date, Decimal]]]:
    out: dict[str, list[tuple[date, Decimal]]] = {}
    for r in conn.execute("select isin, day, price from closes order by isin, day"):
        out.setdefault(r["isin"], []).append((date.fromisoformat(r["day"]), Decimal(r["price"])))
    return out


def _price_on(rows: list[tuple[date, Decimal]] | None, day: date) -> Decimal | None:
    if not rows:
        return None
    i = bisect_right(rows, day, key=lambda r: r[0])
    return rows[i - 1][1] if i else None


@dataclass
class Holding:
    isin: str
    name: str
    quantity: Decimal
    cost: Decimal
    price: Decimal | None
    value: Decimal
    pnl_abs: Decimal
    pnl_pct: Decimal | None
    share: Decimal
    avg_cost: Decimal
    xirr: float | None
    price_missing: bool


@dataclass
class Overview:
    holdings: list[Holding] = field(default_factory=list)
    total_value: Decimal = Decimal(0)
    total_cost: Decimal = Decimal(0)
    pnl_abs: Decimal = Decimal(0)
    pnl_pct: Decimal | None = None
    xirr: float | None = None
    error: str | None = None


def _pct(part: Decimal, whole: Decimal) -> Decimal | None:
    return part / whole * 100 if whole else None


def _xirr(flows: list[tuple[date, float]], today: date) -> float | None:
    if not flows or (today - min(d for d, _ in flows)).days < MIN_XIRR_AGE_DAYS:
        return None
    return xirr(flows)


def _flows(trades: list[Trade], terminal_value: Decimal, today: date) -> list[tuple[date, float]]:
    flows = []
    for t in trades:
        gross = t.quantity * t.price
        flows.append((_day(t.executed_at), float(-(gross + t.fee) if t.side == "BUY" else gross - t.fee)))
    flows.append((today, float(terminal_value)))
    return flows


def overview(conn, today: date) -> Overview:
    trades = load_trades(conn)
    if not trades:
        return Overview()
    try:
        lots, _ = replay(trades)
    except OversellError as e:
        return Overview(error=str(e))
    quotes = {r["isin"]: Decimal(r["price"]) for r in conn.execute("select isin, price from quotes")}
    closes = _closes(conn)
    names = {t.isin: t.name for t in trades}
    by_isin: dict[str, list[Lot]] = {}
    for lot in lots:
        if lot.remaining > 0:
            by_isin.setdefault(lot.trade.isin, []).append(lot)
    holdings: list[Holding] = []
    for isin, ls in by_isin.items():
        qty = sum((l.remaining for l in ls), Decimal(0))
        cost = sum((l.cost for l in ls), Decimal(0))
        price = quotes.get(isin) or (closes[isin][-1][1] if closes.get(isin) else None)
        value = qty * price if price is not None else cost
        flows = _flows([t for t in trades if t.isin == isin], value, today)
        holdings.append(Holding(isin, names[isin], qty, cost, price, value, value - cost, _pct(value - cost, cost),
                                Decimal(0), cost / qty, _xirr(flows, today), price is None))
    total_value = sum((h.value for h in holdings), Decimal(0))
    total_cost = sum((h.cost for h in holdings), Decimal(0))
    for h in holdings:
        h.share = _pct(h.value, total_value) or Decimal(0)
    holdings.sort(key=lambda h: h.value, reverse=True)
    return Overview(holdings, total_value, total_cost, total_value - total_cost,
                    _pct(total_value - total_cost, total_cost),
                    _xirr(_flows(trades, total_value, today), today))


@dataclass
class SeriesPoint:
    day: date
    value: float
    invested: float
    price: float | None = None    # per-ETF series only: the ETF's own close


def value_series(conn, isin: str | None = None) -> list[SeriesPoint]:
    """Daily value and invested amount, for the whole portfolio or (with `isin`) for one ETF."""
    trades = [t for t in load_trades(conn) if isin is None or t.isin == isin]
    if not trades:
        return []
    closes = _closes(conn)
    first = _day(trades[0].executed_at)
    sources = closes.values() if isin is None else [closes.get(isin, [])]
    days = sorted({d for rows in sources for d, _ in rows if d >= first})
    series: list[SeriesPoint] = []
    for day in days:
        upto = [t for t in trades if _day(t.executed_at) <= day]
        try:
            lots, _ = replay(upto)
        except OversellError:
            return []
        value = invested = Decimal(0)
        for lot in lots:
            if lot.remaining <= 0:
                continue
            price = _price_on(closes.get(lot.trade.isin), day)
            invested += lot.cost
            value += lot.remaining * price if price is not None else lot.cost
        price = _price_on(closes.get(isin), day) if isin else None
        series.append(SeriesPoint(day, float(value), float(invested), float(price) if price is not None else None))
    return series


def moving_averages(conn, isin: str, days: list[date], windows=(20, 50)) -> dict[int, list[float | None]]:
    """Simple moving average of the ETF's closes over the last n closes, for each chart day and window.
    Uses every stored close, so the averages are complete from the chart's first day whenever enough
    closes precede it; None where fewer than n closes exist yet."""
    rows = _closes(conn).get(isin, [])
    ends = [bisect_right(rows, day, key=lambda r: r[0]) for day in days]
    return {n: [float(sum(p for _, p in rows[i - n:i]) / n) if i >= n else None for i in ends] for n in windows}


def slice_range(series: list[SeriesPoint], key: str, today: date) -> list[SeriesPoint]:
    if key not in RANGE_DAYS:
        return series
    start = today - timedelta(days=RANGE_DAYS[key])
    return [p for p in series if p.day >= start]


def last_session_change(conn, only: str | None = None) -> tuple[Decimal, Decimal] | None:
    trades = [t for t in load_trades(conn) if only is None or t.isin == only]
    if not trades:
        return None
    try:
        lots, _ = replay(trades)
    except OversellError:
        return None
    closes = _closes(conn)
    qty: dict[str, Decimal] = {}
    for lot in lots:
        qty[lot.trade.isin] = qty.get(lot.trade.isin, Decimal(0)) + lot.remaining
    change = prev_value = Decimal(0)
    for isin, q in qty.items():
        rows = closes.get(isin, [])
        if q > 0 and len(rows) >= 2:
            change += q * (rows[-1][1] - rows[-2][1])
            prev_value += q * rows[-2][1]
    return (change, change / prev_value * 100) if prev_value else None


@dataclass
class TxRow:
    trade: Trade
    paid: Decimal            # cost for a buy, proceeds for a sell
    current_value: Decimal | None
    pnl_pct: Decimal | None
    remaining: Decimal | None
    realized: Decimal | None


@dataclass
class IsinDetail:
    name: str = ""
    rows: list[TxRow] = field(default_factory=list)
    realized_total: Decimal = Decimal(0)
    error: str | None = None


def isin_detail(conn, isin: str) -> IsinDetail:
    trades = [t for t in load_trades(conn) if t.isin == isin]
    if not trades:
        return IsinDetail()
    detail = IsinDetail(name=trades[0].name)
    try:
        lots, sales = replay(trades)
    except OversellError as e:
        detail.error = str(e)
        return detail
    row = conn.execute("select price from quotes where isin=?", (isin,)).fetchone()
    closes = _closes(conn).get(isin)
    price = Decimal(row["price"]) if row else (closes[-1][1] if closes else None)
    lot_by_id = {l.trade.id: l for l in lots}
    sale_by_id = {s.trade.id: s for s in sales}
    for t in trades:
        if t.side == "BUY":
            lot = lot_by_id[t.id]
            paid = t.quantity * t.price + t.fee
            value = lot.remaining * price if price is not None else None
            pct = _pct(value - lot.cost, lot.cost) if value is not None and lot.remaining > 0 else None
            detail.rows.append(TxRow(t, paid, value, pct, lot.remaining, None))
        else:
            s = sale_by_id[t.id]
            detail.rows.append(TxRow(t, s.proceeds, None, _pct(s.realized, s.cost_basis), None, s.realized))
            detail.realized_total += s.realized
    return detail


# Scalable FREE Broker: one-off purchases of these issuers' ETFs ("PRIME ETFs") cost nothing from €250
# (https://de.scalable.capital/en/trading-costs, footnote 1); sells and smaller buys pay €0.99.
PRIME_ISSUERS = ("amundi", "ishares", "vanguard", "xtrackers")
PRIME_NOTE = "PRIME ETF: one-off buys of €250 or more are free on the FREE Broker, smaller buys and all sells cost €0.99"


def prime_buy(t: Trade) -> bool:
    """A one-off buy of a PRIME ETF, i.e. one that is free from €250. Matched on the ETF name, so new ETFs count too."""
    return t.side == "BUY" and t.kind != "SAVINGS_PLAN" and any(i in t.name.lower() for i in PRIME_ISSUERS)


MARKER_CATEGORIES = ("plan_buy", "cash_buy", "sell")
# A trade newer than the last chart point (its close is not in yet) still gets a marker on that point,
# but only for a few days; older trades outside the chart's range are dropped.
MARKER_GRACE_DAYS = 3


@dataclass(frozen=True)
class Marker:
    category: str        # plan_buy | cash_buy | sell
    isin: str
    name: str
    side: str
    quantity: Decimal
    amount: Decimal      # cash moved: cost incl. fee for a buy, proceeds net of fee for a sale


def _category(t: Trade) -> str:
    if t.side == "SELL":
        return "sell"
    return "plan_buy" if t.kind == "SAVINGS_PLAN" else "cash_buy"   # unknown kinds count as one-off buys


def trade_markers(conn, days: list[date], isin: str | None = None) -> dict[date, list[Marker]]:
    """Trades grouped by the chart day they belong to: the last chart day on or before the trade date."""
    if not days:
        return {}
    days = sorted(days)
    out: dict[date, list[Marker]] = {}
    for t in load_trades(conn):
        if isin is not None and t.isin != isin:
            continue
        d = _day(t.executed_at)
        i = bisect_right(days, d) - 1
        if i < 0 or (i == len(days) - 1 and (d - days[i]).days > MARKER_GRACE_DAYS):
            continue
        gross = t.quantity * t.price
        amount = gross + t.fee if t.side == "BUY" else gross - t.fee
        out.setdefault(days[i], []).append(Marker(_category(t), t.isin, t.name, t.side, t.quantity, amount))
    return out


@dataclass
class RangeStats:
    value_delta: Decimal | None      # None when the range covers the whole history
    invested_delta: Decimal | None
    unrealized: Decimal              # change in the profit on what is still held
    realized: Decimal                # profit from sales in the range
    pnl_delta: Decimal               # unrealized + realized
    pnl_pct: Decimal | None
    xirr: float | None


def range_stats(conn, ov: Overview, key: str, today: date, isin: str | None = None,
                series: list[SeriesPoint] | None = None) -> RangeStats:
    """What the info boxes show for a range: the change since the chart's first point (or since the start of
    the history when the range covers it all). For the whole history this equals the overall figures.
    With `isin` everything is for that ETF alone; `series` may be passed in to avoid recomputing it."""
    if series is None:
        series = value_series(conn, isin)
    start = None
    if key in RANGE_DAYS and series:
        cutoff = today - timedelta(days=RANGE_DAYS[key])
        if series[0].day < cutoff:
            start = next((p for p in series if p.day >= cutoff), series[-1])
    v0, i0 = (Decimal(str(start.value)), Decimal(str(start.invested))) if start else (Decimal(0), Decimal(0))
    if isin is None:
        v1, i1 = ov.total_value, ov.total_cost
    else:
        h = next((x for x in ov.holdings if x.isin == isin), None)
        v1, i1 = (h.value, h.cost) if h else (Decimal(0), Decimal(0))
    trades = [t for t in load_trades(conn) if isin is None or t.isin == isin]
    _, sales = replay(trades)
    in_range = lambda t: start is None or _day(t.executed_at) > start.day
    realized = sum((s.realized for s in sales if in_range(s.trade)), Decimal(0))
    unrealized = (v1 - i1) - (v0 - i0)
    bought = sum((t.quantity * t.price + t.fee for t in trades if t.side == "BUY" and in_range(t)), Decimal(0))
    flows = [(start.day, float(-v0))] if start else []
    for t in trades:
        if in_range(t):
            gross = t.quantity * t.price
            flows.append((_day(t.executed_at), float(-(gross + t.fee) if t.side == "BUY" else gross - t.fee)))
    flows.append((today, float(v1)))
    total = unrealized + realized
    return RangeStats(
        value_delta=v1 - v0 if start else None, invested_delta=i1 - i0 if start else None,
        unrealized=unrealized, realized=realized, pnl_delta=total,
        pnl_pct=_pct(total, v0 + bought), xirr=_xirr(flows, today))

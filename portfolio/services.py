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
    i = bisect_right([d for d, _ in rows], day)
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


def slice_range(series: list[SeriesPoint], key: str, today: date) -> list[SeriesPoint]:
    if key not in RANGE_DAYS:
        return series
    start = today - timedelta(days=RANGE_DAYS[key])
    return [p for p in series if p.day >= start]


def last_session_change(conn) -> tuple[Decimal, Decimal] | None:
    trades = load_trades(conn)
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

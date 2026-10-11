from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates

from portfolio import services

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STALE_AFTER_HOURS = 36
RANGES = ["1w", "1m", "3m", "6m", "1y", "all"]


def _eur(v) -> str:
    return "n/a" if v is None else f"{Decimal(v):,.2f}"


def _signed(v) -> str:
    return "n/a" if v is None else f"{Decimal(v):+,.2f}"


def _qty(v) -> str:
    return "n/a" if v is None else f"{Decimal(v).normalize():f}"


def _berlin_date(dt: datetime) -> str:
    return dt.astimezone(services.BERLIN).strftime("%Y-%m-%d")


templates.env.filters.update(eur=_eur, signed=_signed, qty=_qty, berlin_date=_berlin_date)


def _last_ingest(conn) -> datetime | None:
    row = conn.execute("select value from meta where key='last_ingest'").fetchone()
    return datetime.fromisoformat(row["value"]) if row else None


def _ctx(request: Request, **extra) -> dict:
    last = _last_ingest(request.app.state.conn)
    age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600 if last else None
    return {"last_ingest": last, "stale": last is None or age_h > STALE_AFTER_HOURS, **extra}


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
    """One entry per ETF ever traded (held ones first, by value), with its trade rows for the table."""
    held = [h.isin for h in ov.holdings]
    names = {t.isin: t.name for t in services.load_trades(conn)}
    others = sorted((i for i in names if i not in held), key=lambda i: names[i])
    return [{"isin": i, "name": names[i], "rows": services.isin_detail(conn, i).rows} for i in held + others]


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/")
async def portfolio_page(request: Request, range: str = "1y"):
    conn = request.app.state.conn
    today = date.today()
    ov = services.overview(conn, today)
    series = services.value_series(conn)   # the full history; the range buttons slice it in the browser
    days = [p.day for p in series]
    values = [p.value for p in series]
    markers = marker_series(services.trade_markers(conn, days), days, values)
    return templates.TemplateResponse(request, "portfolio.html", _ctx(
        request, ov=ov, range=range if range in RANGES else "1y", ranges=RANGES,
        range_days=services.RANGE_DAYS, today=today.isoformat(),
        change=services.last_session_change(conn), tabs=etf_tabs(conn, ov),
        labels=[d.isoformat() for d in days], values=values,
        invested=[p.invested for p in series], markers=markers))


@router.get("/etf")
async def etf_list(request: Request):
    ov = services.overview(request.app.state.conn, date.today())
    return templates.TemplateResponse(request, "etf_list.html", _ctx(request, ov=ov))


@router.get("/etf/{isin}")
async def etf_detail(request: Request, isin: str):
    detail = services.isin_detail(request.app.state.conn, isin)
    return templates.TemplateResponse(request, "etf_detail.html", _ctx(request, isin=isin, d=detail))


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

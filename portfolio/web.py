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


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/")
async def portfolio_page(request: Request, range: str = "1y"):
    conn = request.app.state.conn
    today = date.today()
    ov = services.overview(conn, today)
    series = services.slice_range(services.value_series(conn), range, today)
    return templates.TemplateResponse(request, "portfolio.html", _ctx(
        request, ov=ov, range=range, ranges=RANGES, change=services.last_session_change(conn),
        labels=[p.day.isoformat() for p in series], values=[p.value for p in series],
        invested=[p.invested for p in series]))


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

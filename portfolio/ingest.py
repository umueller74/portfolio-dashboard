import hmac
import os
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from portfolio import history, signals

router = APIRouter(prefix="/api")


class TradeIn(BaseModel):
    id: str
    isin: str
    name: str
    side: str
    kind: str
    executed_at: datetime
    quantity: Decimal
    price: Decimal
    amount: Decimal
    fee: Decimal = Decimal(0)


class CloseIn(BaseModel):
    isin: str
    day: date
    price: Decimal


class QuoteIn(BaseModel):
    isin: str
    price: Decimal
    quoted_at: datetime
    outdated: bool


class TickIn(BaseModel):
    isin: str
    at: datetime
    price: Decimal


class IngestIn(BaseModel):
    fetched_at: datetime
    trades: list[TradeIn] = []
    closes: list[CloseIn] = []
    quotes: list[QuoteIn] = []
    ticks: list[TickIn] = []          # every chart point the fetcher saw


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def require_token(request: Request, authorization: str = Header(default="")):
    expected = f"Bearer {request.app.state.token}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


@router.post("/ingest", dependencies=[Depends(require_token)])
async def ingest(body: IngestIn, request: Request, tasks: BackgroundTasks):
    conn = request.app.state.conn
    with conn:
        for t in body.trades:
            conn.execute(
                "insert into trades(id,isin,name,side,kind,executed_at,quantity,price,amount,fee)"
                " values(?,?,?,?,?,?,?,?,?,?)"
                " on conflict(id) do update set isin=excluded.isin, name=excluded.name, side=excluded.side,"
                " kind=excluded.kind, executed_at=excluded.executed_at, quantity=excluded.quantity,"
                " price=excluded.price, amount=excluded.amount, fee=excluded.fee",
                (t.id, t.isin, t.name, t.side, t.kind, t.executed_at.isoformat(),
                 str(t.quantity), str(t.price), str(t.amount), str(t.fee)),
            )
        for c in body.closes:
            conn.execute(
                "insert into closes(isin,day,price) values(?,?,?)"
                " on conflict(isin,day) do update set price=excluded.price",
                (c.isin, c.day.isoformat(), str(c.price)),
            )
        for q in body.quotes:
            conn.execute(
                "insert into quotes(isin,price,quoted_at,outdated) values(?,?,?,?)"
                " on conflict(isin) do update set price=excluded.price, quoted_at=excluded.quoted_at,"
                " outdated=excluded.outdated",
                (q.isin, str(q.price), q.quoted_at.isoformat(), int(q.outdated)),
            )
        # every price seen is kept: the chart points plus each quote (one per fetch)
        conn.executemany("insert or ignore into ticks(isin,at,price) values(?,?,?)",
                         [(t.isin, _utc(t.at), str(t.price)) for t in body.ticks]
                         + [(q.isin, _utc(q.quoted_at), str(q.price)) for q in body.quotes])
        conn.execute(
            "insert into meta(key,value) values('last_ingest',?)"
            " on conflict(key) do update set value=excluded.value",
            (body.fetched_at.isoformat(),),
        )
    # after the response, in the threadpool so slow calls never block requests: first older closes for ETFs
    # not backfilled yet (so the 200-day average sees them), then crossover alerts
    tasks.add_task(history.backfill, conn, request.app.state.history)
    tasks.add_task(signals.check, conn, datetime.now(timezone.utc), request.app.state.notify)
    return {"trades": len(body.trades), "closes": len(body.closes), "quotes": len(body.quotes)}


@router.get("/backup", dependencies=[Depends(require_token)])
async def backup(request: Request):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # VACUUM INTO needs a non-existing target
    request.app.state.conn.execute("vacuum into ?", (path,))
    return FileResponse(path, media_type="application/octet-stream", filename="portfolio.db",
                        background=BackgroundTask(os.unlink, path))

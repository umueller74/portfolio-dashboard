"""Price history from before the broker's own closes. The MCP's chart is daily only for the current year, so
for every ETF ever traded or on the watchlist the daily closes since its launch are fetched once from justETF
and stored in `closes` for the days before the first close already there; from then on the broker's closes
take over."""
import json
import logging
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable

log = logging.getLogger("portfolio.history")
JUSTETF_URL = ("https://www.justetf.com/api/etfs/{isin}/performance-chart?locale=en&currency=EUR"
               "&valuesType=MARKET_VALUE&reduceData=false&includeDividends=false")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) portfolio-dashboard"

# ISIN -> daily closes, oldest first; None when the source does not know the ISIN. Raises on any other failure.
Source = Callable[[str], list[tuple[date, Decimal]] | None]


def justetf(isin: str, opener=urllib.request.urlopen) -> list[tuple[date, Decimal]] | None:
    """Daily EUR prices since launch. justETF repeats Friday's value on weekends; those days are dropped,
    so the series has one point per session like the broker's closes."""
    req = urllib.request.Request(JUSTETF_URL.format(isin=isin),
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with opener(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    points = ((date.fromisoformat(p["date"]), Decimal(str(p["value"]["raw"]))) for p in data["series"])
    return sorted((d, price) for d, price in points if d.weekday() < 5)


def backfill(conn, source: Source | None) -> None:
    """Fetch the history of each traded or watchlisted ETF not done yet. A failed fetch is retried on the next
    call; an ISIN the source does not know is marked done, so it is not asked again."""
    if source is None:
        return
    pending = [r["isin"] for r in conn.execute(
        "select isin from trades union select isin from watchlist"
        " except select isin from backfills order by isin")]
    for isin in pending:
        try:
            points = source(isin)
        except Exception:
            log.warning("price history for %s failed, retrying on the next ingest", isin, exc_info=True)
            continue
        first = conn.execute("select min(day) from closes where isin=?", (isin,)).fetchone()[0]
        rows = [(isin, d.isoformat(), str(p)) for d, p in points or [] if first is None or d.isoformat() < first]
        with conn:
            conn.executemany("insert or ignore into closes(isin,day,price) values(?,?,?)", rows)
            conn.execute("insert into backfills(isin,fetched_at,points) values(?,?,?)",
                         (isin, datetime.now(timezone.utc).isoformat(), len(rows)))
        log.info("price history for %s: %d closes added", isin, len(rows))

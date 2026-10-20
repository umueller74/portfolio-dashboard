import asyncio
import json
import logging
import os
import sys
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fetcher import normalize

log = logging.getLogger("fetcher")
MAX_PAGES = 50


async def collect(call, state_dir, timeframes=normalize.KNOWN_TIMEFRAMES) -> dict:
    """Trades (with fees and prices from the cached details), daily closes, quotes, and as `ticks` every
    chart point of every timeframe. year_to_date is always fetched and must succeed: it feeds the closes."""
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    cache_path = state / "details.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    trades, cursor = [], None
    for _ in range(MAX_PAGES):
        args = {"pageSize": 100}
        if cursor:
            args["cursor"] = cursor
        page, cursor = normalize.parse_transactions(await call("list_portfolio_transactions", args))
        trades += page
        if not cursor:
            break

    for t in trades:
        if t["id"] not in cache:
            details = await call("get_transaction_details", {"transactionId": t["id"]})
            fee = normalize.fee_from_details(details)
            price = normalize.price_from_details(details, Decimal(t["quantity"]))
            cache[t["id"]] = {"fee": format(fee.normalize(), "f"), "price": str(price) if price is not None else None}
        t["fee"] = cache[t["id"]]["fee"]
        if cache[t["id"]]["price"]:
            t["price"] = cache[t["id"]]["price"]
    cache_path.write_text(json.dumps(cache))

    closes, quotes, ticks = [], [], {}
    for isin in sorted({t["isin"] for t in trades}):
        chart = await call("get_security_chart", {"isin": isin, "timeframe": "year_to_date"})
        closes += [{"isin": isin, **c} for c in normalize.parse_chart(chart)]
        charts = [chart]
        for tf in timeframes:
            if tf == "year_to_date":
                continue
            try:
                charts.append(await call("get_security_chart", {"isin": isin, "timeframe": tf}))
            except Exception as e:   # finer prices are a bonus; the run still delivers trades and closes
                log.warning("chart %s for %s failed: %s", tf, isin, e)
        for c in charts:
            ticks.update({(t["isin"], t["at"]): t for t in normalize.parse_ticks(c, isin)})
        quotes.append(normalize.parse_quote(await call("get_security_quote", {"isin": isin})))
    return {"fetched_at": datetime.now(timezone.utc).isoformat(), "trades": trades, "closes": closes, "quotes": quotes,
            "ticks": sorted(ticks.values(), key=lambda t: (t["isin"], t["at"]))}


async def chart_timeframes(session) -> list[str]:
    """The timeframes get_security_chart accepts, from the server's tool list. Never fails the run: on any
    error the known ones are used. mcp 2.0 calls the schema `input_schema` (1.x: `inputSchema`)."""
    try:
        tools = (await session.list_tools()).tools
        return normalize.chart_timeframes([
            {"name": t.name, "inputSchema": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)}
            for t in tools])
    except Exception as e:
        log.warning("reading the chart timeframes failed, using the known ones: %s", e)
        return list(normalize.KNOWN_TIMEFRAMES)


def push(body: dict, ingest_url: str, ingest_token: str, opener=urllib.request.urlopen) -> None:
    """Standard library only: Hermes' interpreter has no `httpx`. HTTPError (non-2xx) propagates."""
    req = urllib.request.Request(
        ingest_url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {ingest_token}", "Content-Type": "application/json"})
    with opener(req, timeout=60) as resp:
        resp.read()


async def _main() -> None:
    from fetcher.auth_hermes import bootstrap

    bootstrap()  # must precede any `mcp` import
    from fetcher.session import open_call

    async with open_call(os.environ.get("MCP_URL", "https://mcp.scalable.capital/mcp")) as call:
        timeframes = await chart_timeframes(call.session)
        log.info("chart timeframes: %s", ", ".join(timeframes))
        body = await collect(call, os.environ["STATE_DIR"], timeframes)
    push(body, os.environ["INGEST_URL"], os.environ["INGEST_TOKEN"])
    log.info("pushed %d trades, %d closes, %d quotes, %d ticks",
             len(body["trades"]), len(body["closes"]), len(body["quotes"]), len(body["ticks"]))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(_main())
    except Exception:
        log.exception("fetch failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

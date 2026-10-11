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


async def collect(call, state_dir) -> dict:
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

    closes, quotes = [], []
    for isin in sorted({t["isin"] for t in trades}):
        chart = await call("get_security_chart", {"isin": isin, "timeframe": "year_to_date"})
        closes += [{"isin": isin, **c} for c in normalize.parse_chart(chart)]
        quotes.append(normalize.parse_quote(await call("get_security_quote", {"isin": isin})))
    return {"fetched_at": datetime.now(timezone.utc).isoformat(), "trades": trades, "closes": closes, "quotes": quotes}


def push(body: dict, ingest_url: str, ingest_token: str, opener=urllib.request.urlopen) -> None:
    """Standard library only: Hermes' interpreter has no `httpx`. HTTPError (non-2xx) propagates."""
    req = urllib.request.Request(
        ingest_url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {ingest_token}", "Content-Type": "application/json"})
    with opener(req, timeout=60) as resp:
        resp.read()


async def _main() -> None:
    from fetcher.auth_hermes import bootstrap, build_auth

    bootstrap()  # must precede any `mcp` import
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    url = os.environ.get("MCP_URL", "https://mcp.scalable.capital/mcp")
    http_client = create_mcp_http_client(auth=build_auth(url))
    async with http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()

                async def call(tool: str, args: dict) -> dict:
                    result = await session.call_tool(tool, args)
                    text = result.content[0].text
                    if result.is_error:
                        raise RuntimeError(f"{tool} failed: {text}")
                    return json.loads(text)

                body = await collect(call, os.environ["STATE_DIR"])
    push(body, os.environ["INGEST_URL"], os.environ["INGEST_TOKEN"])
    log.info("pushed %d trades, %d closes, %d quotes", len(body["trades"]), len(body["closes"]), len(body["quotes"]))


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

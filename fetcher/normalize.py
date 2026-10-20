import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
SIX_PLACES = Decimal("0.000001")
# get_security_chart timeframes known to work, used when the tool schema does not list them;
# year_to_date is the only daily one and feeds the closes
KNOWN_TIMEFRAMES = ("year_to_date", "one_month", "max")


def unwrap(payload):
    """Hermes prints {"result": "<json>"}; the MCP SDK yields the inner JSON text."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, dict) and set(payload) == {"result"} and isinstance(payload["result"], str):
        return json.loads(payload["result"])
    return payload


def parse_transactions(payload) -> tuple[list[dict], str | None]:
    data = unwrap(payload)
    trades = []
    for row in data["transactions"]:
        sec = row.get("security")
        if row.get("kind") != "security" or sec is None:
            continue
        if row.get("status") != "SETTLED" or row.get("isCancellation"):
            continue
        qty, amount = Decimal(sec["quantity"]), Decimal(sec["amount"])
        if qty == 0:
            continue
        trades.append({
            "id": row["id"], "isin": sec["isin"], "name": row["description"], "side": sec["side"],
            "kind": sec["transactionType"], "executed_at": row["lastEventAt"],
            "quantity": str(qty), "amount": str(amount),
            "price": str((abs(amount) / qty).quantize(SIX_PLACES, ROUND_HALF_UP)),
        })
    return trades, data["page"]["nextCursor"]


def parse_watchlist(payload) -> list[dict]:
    return [{"isin": i["isin"], "name": i["name"], "type": i["securityType"]} for i in unwrap(payload)["items"]]


def fee_from_details(payload) -> Decimal:
    trade = unwrap(payload)["transaction"]["securityTrade"]
    total = trade.get("totalAmount")
    market = (trade.get("tradeTransactionAmounts") or {}).get("marketValuation")
    if total is None or market is None:
        return Decimal(0)
    return abs(abs(Decimal(total)) - Decimal(market))


def price_from_details(payload, quantity: Decimal) -> Decimal | None:
    """Execution price without the fee. The list's `amount` is the broker total *after* fees, so
    `|amount| / quantity` would count the fee twice once the app adds it on top."""
    trade = unwrap(payload)["transaction"]["securityTrade"]
    market = (trade.get("tradeTransactionAmounts") or {}).get("marketValuation")
    if market is None or quantity == 0:
        return None
    return (Decimal(market) / quantity).quantize(SIX_PLACES, ROUND_HALF_UP)


def _find_points(obj):
    if isinstance(obj, list):
        if obj and all(isinstance(p, dict) and "midPrice" in p and "timestampUtc" in p for p in obj):
            return obj
        for item in obj:
            found = _find_points(item)
            if found:
                return found
    elif isinstance(obj, dict):
        for value in obj.values():
            found = _find_points(value)
            if found:
                return found
    return []


def parse_chart(payload) -> list[dict]:
    by_day: dict[str, str] = {}
    points = sorted(_find_points(unwrap(payload)), key=lambda p: p["timestampUtc"])
    for p in points:
        ts = datetime.fromisoformat(p["timestampUtc"].replace("Z", "+00:00"))
        by_day[ts.astimezone(BERLIN).date().isoformat()] = str(p["midPrice"])
    return [{"day": d, "price": price} for d, price in sorted(by_day.items())]


def parse_ticks(payload, isin: str) -> list[dict]:
    """Every chart point as it came, oldest first, with its instant in UTC."""
    ticks = []
    for p in _find_points(unwrap(payload)):
        at = datetime.fromisoformat(p["timestampUtc"].replace("Z", "+00:00")).astimezone(timezone.utc)
        ticks.append({"isin": isin, "at": at.isoformat(), "price": str(p["midPrice"])})
    return sorted(ticks, key=lambda t: t["at"])


def chart_timeframes(tools: list[dict]) -> list[str]:
    """The `timeframe` values `get_security_chart` accepts, read from its input schema."""
    for tool in tools:
        if tool.get("name") == "get_security_chart":
            enum = (((tool.get("inputSchema") or {}).get("properties") or {}).get("timeframe") or {}).get("enum")
            if enum:
                return list(enum)
    return list(KNOWN_TIMEFRAMES)


def parse_quote(payload) -> dict:
    sec = unwrap(payload)["security"]
    q = sec["quote"]
    return {"isin": sec["isin"], "price": str(q["midPrice"]), "quoted_at": q["timestampUtc"],
            "outdated": bool(q["isOutdated"])}

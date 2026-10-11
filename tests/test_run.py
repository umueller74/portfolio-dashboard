import json

from fetcher import run
from tests.test_normalize import LIST


def details(total="-2100", market="2100"):
    return {"transaction": {"securityTrade": {"totalAmount": total, "tradeTransactionAmounts": {"marketValuation": market}}}}


def fake_call(log):
    async def call(tool, args):
        log.append((tool, args))
        if tool == "list_portfolio_transactions":
            return LIST
        if tool == "get_transaction_details":
            return details("-2100.99", "2100") if args["transactionId"] == "i1" else details("-25.0000", "25.0000")
        if tool == "get_security_chart":
            return {"points": [{"midPrice": 100, "timestampUtc": "2026-10-09T20:59:56.000Z"}]}
        if tool == "get_security_quote":
            return {"security": {"isin": args["isin"], "quote": {"midPrice": 101, "timestampUtc": "2026-10-09T20:59:56Z", "isOutdated": False}}}
        raise AssertionError(tool)
    return call


async def test_collect_builds_ingest_body_and_caches_fees(tmp_path):
    log = []
    body = await run.collect(fake_call(log), tmp_path)
    assert [t["id"] for t in body["trades"]] == ["i1", "i2"]
    assert body["trades"][0]["fee"] == "0.99" and body["trades"][1]["fee"] == "0"
    # price excludes the fee: marketValuation / quantity, not |amount| / quantity
    assert body["trades"][0]["price"] == "700.000000" and body["trades"][1]["price"] == "84.249973"
    assert {c["isin"] for c in body["closes"]} == {"IE0000000001", "IE0000000002"}
    assert len(body["quotes"]) == 2
    assert {t for t, _ in log} <= {"list_portfolio_transactions", "get_transaction_details", "get_security_chart", "get_security_quote"}
    # second run: details come from the cache
    log.clear()
    await run.collect(fake_call(log), tmp_path)
    assert not any(t == "get_transaction_details" for t, _ in log)


async def test_collect_follows_cursor_and_stops(tmp_path):
    pages = [dict(LIST, page={"nextCursor": "c1", "totalCount": 9, "returnedCount": 4}), dict(LIST, page={"nextCursor": None})]
    seen = []

    async def call(tool, args):
        if tool == "list_portfolio_transactions":
            seen.append(args.get("cursor"))
            return pages.pop(0)
        return await fake_call([])(tool, args)

    await run.collect(call, tmp_path)
    assert seen == [None, "c1"]


def test_push_sends_bearer_and_propagates_errors():
    import io
    import urllib.error

    import pytest
    captured = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def ok(req, timeout):
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data)
        return Resp(b"{}")

    run.push({"trades": []}, "https://x/api/ingest", "tok", opener=ok)
    assert captured["auth"] == "Bearer tok" and captured["body"] == {"trades": []}

    def bad(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

    with pytest.raises(urllib.error.HTTPError):
        run.push({"trades": []}, "https://x/api/ingest", "tok", opener=bad)

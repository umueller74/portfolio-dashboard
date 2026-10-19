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
            if args["timeframe"] == "broken":
                raise RuntimeError("get_security_chart failed")
            if args["timeframe"] == "one_month":
                return {"points": [{"midPrice": 99, "timestampUtc": "2026-10-09T08:00:00.000Z"},
                                   {"midPrice": 100, "timestampUtc": "2026-10-09T20:59:56.000Z"}]}
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


async def test_collect_stores_every_chart_point_of_every_timeframe_as_ticks(tmp_path):
    log = []
    body = await run.collect(fake_call(log), tmp_path, ["year_to_date", "one_month", "broken"])
    one = [t for t in body["ticks"] if t["isin"] == "IE0000000001"]
    # the closing point comes from both timeframes and is sent once; a failing timeframe does not stop the run
    assert [(t["at"], t["price"]) for t in one] == [("2026-10-09T08:00:00+00:00", "99"), ("2026-10-09T20:59:56+00:00", "100")]
    assert [c["day"] for c in body["closes"] if c["isin"] == "IE0000000001"] == ["2026-10-09"]   # closes: year_to_date only
    assert ("get_security_chart", {"isin": "IE0000000001", "timeframe": "broken"}) in log


async def test_chart_timeframes_read_mcp_2_tools_and_fall_back_on_errors():
    from types import SimpleNamespace as NS
    schema = {"properties": {"timeframe": {"enum": ["one_day", "year_to_date"]}}}

    class Session:
        def __init__(self, tools=None, fail=False):
            self.tools, self.fail = tools, fail

        async def list_tools(self):
            if self.fail:
                raise RuntimeError("list_tools failed")
            return NS(tools=self.tools)

    # mcp 2.0 names it input_schema (1.x: inputSchema)
    assert await run.chart_timeframes(Session([NS(name="get_security_chart", input_schema=schema)])) == ["one_day", "year_to_date"]
    assert await run.chart_timeframes(Session([NS(name="get_security_chart", inputSchema=schema)])) == ["one_day", "year_to_date"]
    assert await run.chart_timeframes(Session(fail=True)) == list(run.normalize.KNOWN_TIMEFRAMES)

import json
import urllib.error

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
        if tool == "list_watchlist_items":
            return {"portfolioId": "p", "items": [{"isin": "US67066G1040", "name": "Nvidia", "securityType": "STOCK"}]}
        if tool in ("add_watchlist_item", "remove_watchlist_item"):
            return {}
        raise AssertionError(tool)
    return call


async def test_collect_builds_ingest_body_and_caches_fees(tmp_path):
    log = []
    body = await run.collect(fake_call(log), tmp_path)
    assert [t["id"] for t in body["trades"]] == ["i1", "i2"]
    assert body["trades"][0]["fee"] == "0.99" and body["trades"][1]["fee"] == "0"
    # price excludes the fee: marketValuation / quantity, not |amount| / quantity
    assert body["trades"][0]["price"] == "700.000000" and body["trades"][1]["price"] == "84.249973"
    assert {c["isin"] for c in body["closes"]} == {"IE0000000001", "IE0000000002", "US67066G1040"}
    assert len(body["quotes"]) == 3   # two traded, one watched
    assert {t for t, _ in log} <= {"list_portfolio_transactions", "get_transaction_details", "get_security_chart", "get_security_quote",
                            "list_watchlist_items"}
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


async def test_collect_applies_actions_then_reads_watchlist(tmp_path):
    log = []
    body = await run.collect(fake_call(log), tmp_path,
                             actions=lambda: {"add": ["IE0000000002"], "remove": ["IE0000000003"]})
    tools = [t for t, _ in log]
    assert ("add_watchlist_item", {"isin": "IE0000000002"}) in log
    assert ("remove_watchlist_item", {"isin": "IE0000000003"}) in log
    assert max(tools.index("add_watchlist_item"), tools.index("remove_watchlist_item")) < tools.index("list_watchlist_items")
    assert body["watchlist"] == [{"isin": "US67066G1040", "name": "Nvidia", "type": "STOCK"}]
    assert ("get_security_chart", {"isin": "US67066G1040", "timeframe": "year_to_date"}) in log
    assert ("get_security_quote", {"isin": "US67066G1040"}) in log
    assert "US67066G1040" in {q["isin"] for q in body["quotes"]}


async def test_collect_action_failure_does_not_fail_run(tmp_path):
    base = fake_call([])

    async def call(tool, args):
        if tool == "add_watchlist_item":
            raise RuntimeError("add failed")
        return await base(tool, args)

    body = await run.collect(call, tmp_path, actions=lambda: {"add": ["IE0000000002"], "remove": []})
    assert [t["id"] for t in body["trades"]] == ["i1", "i2"]
    assert body["watchlist"][0]["isin"] == "US67066G1040"


def test_pending_actions_returns_none_on_error_and_parses_ok():
    seen = []

    def bad(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)

    assert run.pending_actions("https://x/api/ingest", "tok", opener=bad) is None

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return b'{"add": ["A"], "remove": []}'

    def ok(req, timeout):
        seen.append((req.full_url, req.get_header("Authorization")))
        return Resp()

    assert run.pending_actions("https://x/api/ingest", "tok", opener=ok) == {"add": ["A"], "remove": []}
    assert seen == [("https://x/api/watchlist/actions", "Bearer tok")]


async def test_collect_survives_actions_endpoint_failure(tmp_path):
    log = []
    await run.collect(fake_call(log), tmp_path, actions=lambda: None)
    assert not any(t in ("add_watchlist_item", "remove_watchlist_item") for t, _ in log)


async def test_collect_without_watchlist_tool(tmp_path):
    base = fake_call([])

    async def call(tool, args):
        if tool == "list_watchlist_items":
            raise RuntimeError("unknown tool")
        return await base(tool, args)

    body = await run.collect(call, tmp_path)
    assert "watchlist" not in body and len(body["trades"]) == 2


async def test_collect_reads_the_actions_after_the_trades(tmp_path):
    log = []

    def actions():
        log.append(("actions", {}))
        return {"add": [], "remove": []}

    await run.collect(fake_call(log), tmp_path, actions=actions)
    tools = [t for t, _ in log]
    assert tools.count("actions") == 1
    assert max(i for i, t in enumerate(tools) if t in ("list_portfolio_transactions", "get_transaction_details")) \
        < tools.index("actions") < tools.index("list_watchlist_items")


async def test_collect_sends_when_it_read_the_watchlist(tmp_path):
    from datetime import datetime
    seen = {}
    base = fake_call([])

    async def call(tool, args):
        if tool == "list_watchlist_items":
            seen["at"] = datetime.now(run.timezone.utc)
        return await base(tool, args)

    before = datetime.now(run.timezone.utc)
    body = await run.collect(call, tmp_path)
    read_at = datetime.fromisoformat(body["watchlist_read_at"])
    assert before <= read_at <= seen["at"] and read_at.tzinfo is not None


async def test_collect_without_watchlist_sends_no_read_instant(tmp_path):
    base = fake_call([])

    async def call(tool, args):
        if tool == "list_watchlist_items":
            raise RuntimeError("unknown tool")
        return await base(tool, args)

    assert "watchlist_read_at" not in await run.collect(call, tmp_path)


WATCHED = [{"isin": "US67066G1040", "name": "Nvidia", "securityType": "STOCK"},
           {"isin": "US0231351067", "name": "Amazon", "securityType": "STOCK"},
           {"isin": "US5949181045", "name": "Microsoft", "securityType": "STOCK"}]


async def test_collect_skips_a_watched_isin_whose_chart_or_quote_fails(tmp_path):
    base = fake_call([])

    async def call(tool, args):
        isin = args.get("isin")
        if tool == "list_watchlist_items":
            return {"items": WATCHED}
        if tool == "get_security_chart" and isin == "US67066G1040":
            raise RuntimeError("get_security_chart failed: not found")
        if tool == "get_security_quote" and isin == "US0231351067":
            return {"security": {"isin": isin}}   # no quote
        return await base(tool, args)

    body = await run.collect(call, tmp_path)
    assert [t["id"] for t in body["trades"]] == ["i1", "i2"]
    assert {c["isin"] for c in body["closes"]} == {"IE0000000001", "IE0000000002", "US5949181045"}
    assert {q["isin"] for q in body["quotes"]} == {"IE0000000001", "IE0000000002", "US5949181045"}
    assert {t["isin"] for t in body["ticks"]} == {"IE0000000001", "IE0000000002", "US5949181045"}
    assert len(body["watchlist"]) == 3


async def test_collect_skips_a_watched_isin_whose_chart_is_garbage(tmp_path):
    base = fake_call([])

    async def call(tool, args):
        if tool == "get_security_chart" and args["isin"] == "US67066G1040":
            return "not json"
        return await base(tool, args)

    body = await run.collect(call, tmp_path)
    assert "US67066G1040" not in {c["isin"] for c in body["closes"]} | {q["isin"] for q in body["quotes"]}


async def test_collect_still_fails_when_a_traded_isins_daily_chart_fails(tmp_path):
    import pytest
    base = fake_call([])

    async def call(tool, args):
        if tool == "get_security_chart" and args["isin"] == "IE0000000001" and args["timeframe"] == "year_to_date":
            raise RuntimeError("get_security_chart failed")
        return await base(tool, args)

    with pytest.raises(RuntimeError):
        await run.collect(call, tmp_path)


async def test_collect_never_sends_a_quote_without_a_price(tmp_path):
    base = fake_call([])

    async def call(tool, args):
        if tool == "get_security_quote" and args["isin"] in ("IE0000000001", "US67066G1040"):
            return {"security": {"isin": args["isin"], "quote": {"midPrice": None, "timestampUtc": "2026-10-09T20:59:56Z",
                                                                 "isOutdated": False}}}
        return await base(tool, args)

    body = await run.collect(call, tmp_path)
    assert {q["isin"] for q in body["quotes"]} == {"IE0000000002"}
    assert "None" not in json.dumps(body["quotes"])
    assert {"IE0000000001", "US67066G1040"} <= {c["isin"] for c in body["closes"]}   # the closes stay


def test_pending_actions_rejects_a_malformed_payload():
    def opener_for(payload):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def read(self):
                return json.dumps(payload).encode()
        return lambda req, timeout: Resp()

    for bad in (None, [], "x", {"add": None}, {"add": "A"}, {"add": [1]}, {"remove": [None]}):
        assert run.pending_actions("https://x/api/ingest", "tok", opener=opener_for(bad)) is None, bad
    assert run.pending_actions("https://x/api/ingest", "tok", opener=opener_for({"add": ["A"]})) == {"add": ["A"]}

import json
import re

from portfolio import services
from portfolio.web import view_data
from tests.conftest import AUTH, payload
from tests.test_services import ISIN, seed, trade


def test_pages_render_when_empty(client):
    for url in ("/", "/etf", f"/etf/{ISIN}", "/healthz"):
        assert client.get(url).status_code == 200
    assert "No data yet" in client.get("/").text


def test_portfolio_page_shows_totals_and_ranges(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z")],
         closes=[{"isin": ISIN, "day": "2026-10-08", "price": "100"}, {"isin": ISIN, "day": "2026-10-09", "price": "110"}],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    html = client.get("/?range=3m").text
    assert "550.00" in html and "+10.00" in html
    for key in ("1d", "1w", "1m", "3m", "6m", "ytd", "1y", "all"):
        assert f'data-range="{key}">{key.upper()}<' in html
    assert 'id="pnlChart"' in html and 'id="valueChart"' in html


def test_etf_list_and_detail(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "SELL", "2", "120", "2026-02-05T10:00:00Z")],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    assert ISIN in client.get("/etf").text
    html = client.get(f"/etf/{ISIN}").text
    assert "2026-01-05" in html and "BUY" in html and "SELL" in html and "+40.00" in html


def test_oversell_shows_banner_not_500(client):
    seed(client, [trade("a", "BUY", "1", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "SELL", "2", "100", "2026-01-06T10:00:00Z")])
    for url in ("/", "/etf", f"/etf/{ISIN}"):
        r = client.get(url)
        assert r.status_code == 200 and "Data error" in r.text


def test_stale_banner_when_last_ingest_old(client):
    client.post("/api/ingest", headers=AUTH, json=payload(fetched_at="2020-01-01T00:00:00+00:00"))
    assert 'class="stale"' in client.get("/").text


def test_single_page_has_one_trades_table_with_a_button_per_etf(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "BUY", "1", "50", "2026-02-05T10:00:00Z", isin="IE0000000002"),
                  trade("c", "SELL", "1", "50", "2026-03-05T10:00:00Z", isin="IE0000000002")],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    html = client.get("/").text
    assert html.count("<table") == 1
    assert 'data-isin="IE0000000002" title' in html and f'data-isin="{ISIN}" title' in html   # the sold-out ETF too
    assert html.count("<th class=\"sort\"") == 11
    assert html.index("2026-03-05") < html.index("2026-02-05")      # server-side default: newest first
    assert 'class="btn active" data-isin=""' in html                # Overview is the default selection


def test_etf_buttons_grouped_into_active_and_inactive(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "BUY", "1", "50", "2026-02-05T10:00:00Z", isin="IE0000000002"),
                  trade("c", "SELL", "1", "50", "2026-03-05T10:00:00Z", isin="IE0000000002")],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    html = client.get("/").text
    active, inactive = html.index('class="grp">Active<'), html.index('class="grp">Inactive<')
    held, sold = html.index(f'data-isin="{ISIN}" title'), html.index('data-isin="IE0000000002" title')
    assert active < held < inactive < sold


def test_unknown_range_falls_back_to_one_year(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z")])
    assert 'class="btn active" data-range="1y"' in client.get("/?range=bogus").text


def test_per_etf_series_only_use_that_etfs_trades(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-06T10:00:00Z"),
                  trade("b", "BUY", "1", "50", "2026-01-06T11:00:00Z", isin="IE0000000002")],
         closes=[{"isin": ISIN, "day": "2026-01-06", "price": "110"},
                 {"isin": "IE0000000002", "day": "2026-01-06", "price": "60"}])
    one = services.value_series(client.app.state.conn, ISIN)
    assert [(p.value, p.invested, p.price) for p in one] == [(550.0, 500.0, 110.0)]
    assert services.value_series(client.app.state.conn)[0].value == 610.0
    m = services.trade_markers(client.app.state.conn, [p.day for p in one], ISIN)
    assert [x.isin for ms in m.values() for x in ms] == [ISIN]


def test_fee_column_marks_prime_etf_buys(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "SELL", "1", "120", "2026-02-05T10:00:00Z", fee="0.99"),
                  trade("c", "BUY", "1", "50", "2026-02-05T10:00:00Z", isin="IE0000000002")],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    conn = client.app.state.conn
    conn.execute("update trades set name='iShares Core MSCI World (Acc)' where isin=?", (ISIN,))
    conn.commit()
    for url in ("/", f"/etf/{ISIN}"):
        html = client.get(url).text
        assert html.count('class="prime"') == 1           # the iShares buy; not its sell, not the other ETF
        assert re.search(r'class="prime"[^>]*>★</span>0\.00</td>', html)   # star in front of the fee


def test_etf_view_has_20_and_50_day_averages_and_a_maximize_button(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-06T10:00:00Z")],
         closes=[{"isin": ISIN, "day": "2026-01-06", "price": "110"}])
    v = view_data(client.app.state.conn, ISIN)
    assert v["ma20"] == [None] and v["ma50"] == [None]
    assert "ma20" not in view_data(client.app.state.conn, None)
    html = client.get("/").text
    assert 'id="plMax"' in html
    assert "plBox.addEventListener('click'" in html and "cursor:zoom-in" in html   # a click into the chart maximizes too


def test_etf_price_chart_covers_closes_from_before_the_first_trade(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-06T10:00:00Z")],
         closes=[{"isin": ISIN, "day": d, "price": p} for d, p in
                 [("2019-03-01", "50"), ("2025-12-31", "98"), ("2026-01-06", "100"), ("2026-01-07", "104")]])
    v = view_data(client.app.state.conn, ISIN)
    assert v["labels"] == ["2026-01-06", "2026-01-07"]               # value chart: from the first trade
    assert v["plabels"] == ["2019-03-01", "2025-12-31", "2026-01-06", "2026-01-07"]   # price chart: all of it
    assert v["second"] == [50.0, 98.0, 100.0, 104.0] and len(v["ma20"]) == 4
    assert v["pmarkers"]["cash_buy"] == [None, None, 100.0, None]
    p = view_data(client.app.state.conn, None)
    assert p["plabels"] == p["labels"] and p["pmarkers"] == p["markers"]


def test_maximized_price_chart_has_its_own_range_buttons(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z")])
    html = client.get("/?range=3m").text
    header = html[html.index('class="maxranges"'):html.index('id="plMax"')]
    for key in ("1d", "1w", "1m", "3m", "6m", "1y", "all"):
        assert f'data-range="{key}"' in header
    assert html.count('class="btn active" data-range="3m"') == 2      # bar and header agree


def test_etf_view_has_every_price_point_of_the_last_month(client):
    ticks = [{"isin": ISIN, "at": at, "price": p} for at, p in [
        ("2026-09-01T08:00:00Z", "90"),                                    # older than a month before the last tick
        ("2026-10-08T07:00:00Z", "100"), ("2026-10-08T07:00:30Z", "100.5"),  # same Berlin minute: the later one wins
        ("2026-10-09T13:15:00Z", "104")]]
    seed(client, [trade("a", "BUY", "5", "100", "2026-10-08T07:30:00Z")],
         closes=[{"isin": ISIN, "day": "2026-10-08", "price": "101"}])
    client.post("/api/ingest", headers=AUTH, json=payload(trades=[], closes=[], quotes=[], ticks=ticks))
    v = view_data(client.app.state.conn, ISIN)
    assert v["tlabels"] == ["2026-10-08 09:00", "2026-10-09 15:15"]        # Berlin time
    assert v["tprices"] == [100.5, 104.0]
    assert v["tmarkers"]["cash_buy"] == [100.5, None]                      # the buy at 09:30 sits on the 09:00 point
    assert len(v["tma20"]) == 2 and "tlabels" not in view_data(client.app.state.conn, None)


def test_price_chart_has_a_trendline_button(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z")])
    html = client.get("/").text
    assert 'id="plTrend"' in html and "aria-pressed=\"false\"" in html     # off by default
    # the price chart moves: wheel zoom, drag pan, ctrl+wheel y scale, future slots for the trendlines
    assert "addEventListener('wheel'" in html and "e.ctrlKey" in html and "futureLabels" in html


def page_data(html: str) -> dict:
    return json.loads(re.search(r"const D = (\{.*?\});\n", html, re.S).group(1))


def test_page_carries_only_the_portfolio_view_and_serves_each_etf_on_request(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "BUY", "1", "50", "2026-02-05T10:00:00Z", isin="IE0000000002")],
         closes=[{"isin": ISIN, "day": "2026-10-09", "price": "110"},
                 {"isin": "IE0000000002", "day": "2026-10-09", "price": "60"}])
    d = page_data(client.get("/").text)
    assert list(d["views"]) == [""] and list(d["cards"]) == [""]
    assert d["isins"] == [ISIN, "IE0000000002"]                      # what the page loads in the background
    r = client.get(f"/view/{ISIN}")
    assert r.status_code == 200
    assert r.json()["view"] == json.loads(json.dumps(view_data(client.app.state.conn, ISIN)))
    assert r.json()["cards"]["1y"]["value"] == "550.00 €"
    assert client.get("/view/IE9999999999").status_code == 404


def test_page_opens_on_the_etf_in_the_url(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "BUY", "1", "50", "2026-02-05T10:00:00Z", isin="IE0000000002")],
         closes=[{"isin": ISIN, "day": "2026-10-09", "price": "110"}])
    d = page_data(client.get(f"/?range=3m&etf={ISIN}").text)
    assert list(d["views"]) == ["", ISIN] and list(d["cards"]) == ["", ISIN]   # no loading step after a reload
    assert d["etf"] == ISIN
    d = page_data(client.get("/?etf=IE9999999999").text)
    assert list(d["views"]) == [""] and d["etf"] == ""                          # unknown: the overview


def test_cached_rows_follow_every_write(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z")],
         closes=[{"isin": ISIN, "day": "2026-10-08", "price": "100"}])
    conn = client.app.state.conn
    assert len(services._closes(conn)[ISIN]) == 1 and len(services.load_trades(conn)) == 1
    client.post("/api/ingest", headers=AUTH, json=payload(
        trades=[trade("b", "BUY", "1", "100", "2026-02-05T10:00:00Z")],
        closes=[{"isin": ISIN, "day": "2026-10-09", "price": "110"}], quotes=[]))
    assert len(services._closes(conn)[ISIN]) == 2 and len(services.load_trades(conn)) == 2


def test_responses_are_gzipped(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z")],
         closes=[{"isin": ISIN, "day": "2026-10-09", "price": "110"}])
    for url in ("/", f"/view/{ISIN}"):
        assert client.get(url, headers={"Accept-Encoding": "gzip"}).headers.get("content-encoding") == "gzip"


class FakeTrigger:
    def __init__(self, post=(202, {"status": "started"}), get=(200, {"running": True, "last_exit": None}),
                 search=(200, {"results": []}), watchlist=(200, {"status": "ok"})):
        self.replies, self.calls, self.requests = {"POST": post, "GET": get}, [], []
        self.search, self.watchlist = search, watchlist

    def __call__(self, method, path="/fetch", body=None):
        self.requests.append((method, path, body))
        if path == "/fetch":
            self.calls.append(method)
            reply = self.replies[method]
        else:
            reply = self.search if path.startswith("/search") else self.watchlist
        if isinstance(reply, Exception):
            raise reply
        return reply


XHR = {"X-Requested-With": "fetch"}


def trigger_client(tmp_path, trigger):
    from fastapi.testclient import TestClient
    from portfolio.app import create_app
    return TestClient(create_app(tmp_path / "p.db", "test-token", trigger=trigger))


def test_fetch_button_only_with_a_trigger(client, tmp_path):
    assert 'id="fetchNow"' not in client.get("/").text
    assert client.post("/fetch", headers=XHR).status_code == 404
    assert 'id="fetchNow"' in trigger_client(tmp_path, FakeTrigger()).get("/").text


def test_fetch_starts_the_fetcher_and_reports_its_state(tmp_path):
    trig = FakeTrigger()
    c = trigger_client(tmp_path, trig)
    r = c.post("/fetch", headers=XHR)
    assert r.status_code == 202 and r.json() == {"status": "started"}
    c.post("/api/ingest", json=payload(), headers=AUTH)
    assert c.get("/fetch").json() == {"last_ingest": "2026-10-11T21:30:00+00:00", "running": True, "last_exit": None}
    assert trig.calls == ["POST", "GET"]


def test_fetch_already_running_is_passed_on(tmp_path):
    r = trigger_client(tmp_path, FakeTrigger(post=(409, {"status": "running"}))).post("/fetch", headers=XHR)
    assert r.status_code == 409 and r.json() == {"status": "running"}


def test_fetch_needs_the_script_header(tmp_path):
    trig = FakeTrigger()
    assert trigger_client(tmp_path, trig).post("/fetch").status_code == 403   # a cross-site form cannot set it
    assert trig.calls == []


def test_unreachable_fetcher_is_a_502(tmp_path):
    c = trigger_client(tmp_path, FakeTrigger(post=OSError("connection refused"), get=OSError("timed out")))
    r = c.post("/fetch", headers=XHR)
    assert r.status_code == 502 and "connection refused" in r.json()["error"]
    assert c.get("/fetch").json() == {"last_ingest": None, "error": "fetcher unreachable: timed out"}


def test_trigger_client_calls_the_listener_with_the_token():
    import io
    import urllib.error
    from portfolio.refresh import trigger_client as make
    assert make(None, "x") is None
    seen = []

    class Resp(io.BytesIO):
        status = 200

    def opener(req, timeout):
        seen.append((req.method, req.full_url, req.get_header("Authorization")))
        if req.method == "POST":
            raise urllib.error.HTTPError(req.full_url, 409, "busy", {}, io.BytesIO(b'{"status": "running"}'))
        return Resp(b'{"running": false, "last_exit": 0}')

    call = make("http://raspi2:8770/fetch", "tok", opener)
    assert call("POST") == (409, {"status": "running"})
    assert call("GET") == (200, {"running": False, "last_exit": 0})
    assert seen == [("POST", "http://raspi2:8770/fetch", "Bearer tok"), ("GET", "http://raspi2:8770/fetch", "Bearer tok")]


def test_trigger_client_paths():
    import io
    from portfolio.refresh import trigger_client as make
    seen = []

    class Resp(io.BytesIO):
        status = 200

    def opener(req, timeout):
        seen.append((req.method, req.full_url, req.data, req.get_header("Content-type")))
        return Resp(b"{}")

    call = make("http://raspi2:8770/fetch", "tok", opener)
    call("GET", "/search?q=x")
    call("POST", "/watchlist", {"isin": "I", "action": "add"})
    assert seen == [("GET", "http://raspi2:8770/search?q=x", None, None),
                    ("POST", "http://raspi2:8770/watchlist", b'{"isin": "I", "action": "add"}', "application/json")]


def test_trade_rows_carry_their_berlin_day_for_the_range_filter(client):
    # 23:30 UTC is already the next day in Berlin: the range buttons filter on the day the table shows
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T23:30:00Z")])
    html = client.get("/").text
    assert f'<tr data-isin="{ISIN}" data-day="2026-01-06">' in html
    assert "tradesFrom(rangeKey)" in html


def wl_client(tmp_path, trig):
    from tests.test_services import seed
    c = trigger_client(tmp_path, trig)
    seed(c, [trade("a", "BUY", "5", "100", "2026-08-03T10:00:00Z")])
    return c


HIT = {"name": "N", "type": "ETF", "price": "1.5", "currency": "EUR"}


def test_search_marks_held_and_watched(tmp_path):
    from portfolio import watch
    hits = [{"isin": ISIN, **HIT}, {"isin": "IE2", **HIT}, {"isin": "IE3", **HIT}]
    trig = FakeTrigger(search=(200, {"results": hits}))
    c = wl_client(tmp_path, trig)
    watch.remember(c.app.state.conn, "IE2", "N", "ETF")
    r = c.get("/watchlist/search", params={"q": "wor ld&x"}, headers=XHR)
    assert r.status_code == 200
    assert [h["state"] for h in r.json()["results"]] == ["held", "watched", ""]
    assert trig.requests == [("GET", "/search?q=wor%20ld%26x", None)]


def test_add_stores_entry_and_starts_fetch(tmp_path):
    from portfolio import watch
    trig = FakeTrigger()
    c = wl_client(tmp_path, trig)
    r = c.post("/watchlist", json={"isin": "IE2", "name": "Other", "type": "ETF"}, headers=XHR)
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert watch.watched(c.app.state.conn)["IE2"] == {"name": "Other", "type": "ETF"}
    assert trig.requests == [("POST", "/watchlist", {"isin": "IE2", "action": "add"}), ("POST", "/fetch", None)]


def test_add_with_a_running_fetch_is_fine(tmp_path):
    trig = FakeTrigger(post=(409, {"status": "running"}))
    c = wl_client(tmp_path, trig)
    r = c.post("/watchlist", json={"isin": "IE2", "name": "Other", "type": ""}, headers=XHR)
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert trig.requests[-1] == ("POST", "/fetch", None)   # attempted; its 409 is fine


def test_add_turns_auto_row_manual(tmp_path):
    from tests.test_watch import ENTRY, added
    c = trigger_client(tmp_path, FakeTrigger())
    conn = added(c)
    conn.execute("update watch_auto set state='pending_add'")
    conn.commit()
    assert c.post("/watchlist", json=ENTRY, headers=XHR).status_code == 200
    assert conn.execute("select state from watch_auto").fetchone()[0] == "done"


def test_remove_deletes_entry(tmp_path):
    from portfolio import watch
    trig = FakeTrigger()
    c = wl_client(tmp_path, trig)
    watch.remember(c.app.state.conn, "IE2", "Other", "ETF")
    r = c.delete("/watchlist/IE2", headers=XHR)
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert "IE2" not in watch.watched(c.app.state.conn)
    assert trig.requests == [("POST", "/watchlist", {"isin": "IE2", "action": "remove"})]


def test_failed_listener_changes_nothing(tmp_path):
    from portfolio import watch
    c = wl_client(tmp_path, FakeTrigger(watchlist=(502, {"error": "mcp"})))
    r = c.post("/watchlist", json={"isin": "IE2", "name": "Other", "type": ""}, headers=XHR)
    assert r.status_code == 502 and r.json() == {"error": "mcp"}
    assert "IE2" not in watch.watched(c.app.state.conn)


def test_watchlist_routes_need_header(tmp_path):
    trig = FakeTrigger()
    c = wl_client(tmp_path, trig)
    assert c.get("/watchlist/search", params={"q": "abc"}).status_code == 403
    assert c.post("/watchlist", json={"isin": "I", "name": "N", "type": ""}).status_code == 403
    assert c.delete("/watchlist/I").status_code == 403
    assert trig.requests == []


def test_watchlist_routes_need_a_trigger(client):
    assert client.get("/watchlist/search", params={"q": "abc"}, headers=XHR).status_code == 404
    assert client.delete("/watchlist/I", headers=XHR).status_code == 404


def test_search_short_query_and_listener_down(tmp_path):
    c = wl_client(tmp_path, FakeTrigger(search=(400, {"error": "query too short"})))
    r = c.get("/watchlist/search", params={"q": "a"}, headers=XHR)
    assert r.status_code == 400 and r.json() == {"error": "query too short"}
    (tmp_path / "x").mkdir()
    c = wl_client(tmp_path / "x", FakeTrigger(search=OSError("refused"), watchlist=OSError("refused")))
    r = c.get("/watchlist/search", params={"q": "abc"}, headers=XHR)
    assert r.status_code == 502 and "refused" in r.json()["error"]
    assert c.delete("/watchlist/I", headers=XHR).status_code == 502


def test_add_needs_a_trigger_and_a_valid_body(client, tmp_path):
    assert client.post("/watchlist", content=b"junk", headers=XHR).status_code == 404
    trig = FakeTrigger()
    c = wl_client(tmp_path, trig)
    assert c.post("/watchlist", content=b"junk").status_code == 403
    for bad in (b"junk", b"[1]", b'{"isin": "I"}', b'{"isin": "", "name": "N"}', b'{"isin": 5, "name": "N"}',
                b'{"isin": "I", "name": "N", "type": 3}'):
        r = c.post("/watchlist", content=bad, headers={**XHR, "Content-Type": "application/json"})
        assert r.status_code == 400, bad
    assert trig.requests == []


def test_add_with_unreachable_fetcher_is_a_502(tmp_path):
    c = wl_client(tmp_path, FakeTrigger(watchlist=OSError("refused")))
    r = c.post("/watchlist", json={"isin": "IE2", "name": "Other", "type": ""}, headers=XHR)
    assert r.status_code == 502 and "refused" in r.json()["error"]


def test_trigger_client_turns_a_non_json_reply_into_an_oserror():
    import io
    from portfolio.refresh import trigger_client as make

    class Resp(io.BytesIO):
        status = 502

    call = make("http://raspi2:8770/fetch", "tok", lambda req, timeout: Resp(b"<html>Bad Gateway</html>"))
    try:
        call("GET", "/search?q=abc")
    except OSError as e:
        assert "invalid reply" in str(e)
    else:
        raise AssertionError("expected OSError")

import json
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from fetcher import trigger

TOKEN = "t0k"


@pytest.fixture
def listener(tmp_path):
    def start(code="import time; time.sleep(0.3)"):
        runner = trigger.Runner([sys.executable, "-c", code])
        server = trigger.make_server("127.0.0.1", 0, TOKEN, runner)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/fetch", runner
    servers = []
    yield start
    for s in servers:
        s.shutdown()


def call(url, method="GET", token=TOKEN):
    req = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_idle(runner):
    for _ in range(100):
        if not runner.status()["running"]:
            return
        time.sleep(0.05)
    raise AssertionError("still running")


def test_post_starts_one_fetch_and_a_second_waits(listener):
    url, runner = listener()
    assert call(url, "POST") == (202, {"status": "started"})
    assert call(url, "POST") == (409, {"status": "running"})
    assert call(url)[1]["running"] is True
    wait_idle(runner)
    assert call(url) == (200, {"running": False, "last_exit": 0})
    assert call(url, "POST")[0] == 202   # free again


def test_reports_a_failed_fetch(listener):
    url, runner = listener("raise SystemExit(3)")
    assert call(url) == (200, {"running": False, "last_exit": None})
    call(url, "POST")
    wait_idle(runner)
    assert call(url)[1] == {"running": False, "last_exit": 3}


def test_wrong_token_or_path_is_refused(listener):
    url, runner = listener()
    assert call(url, "POST", token="nope")[0] == 401
    assert call(url, token="nope")[0] == 401
    assert call(url.replace("/fetch", "/other"), "POST")[0] == 404
    assert runner.status() == {"running": False, "last_exit": None}


@pytest.fixture
def mcp_listener():
    def start(reply=None, error=None):
        calls = []

        def mcp(tool, args):
            calls.append((tool, args))
            if error:
                raise RuntimeError(error)
            return reply or {}
        server = trigger.make_server("127.0.0.1", 0, TOKEN, trigger.Runner([sys.executable, "-c", "pass"]), mcp)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}", calls
    servers = []
    yield start
    for s in servers:
        s.shutdown()


def post(url, body, token=TOKEN):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_search_maps_hits(mcp_listener):
    base, calls = mcp_listener({"results": [
        {"isin": "US67066G1040", "name": "NVIDIA", "securityType": "STOCK",
         "currentQuote": {"midPrice": 192.52, "currency": "EUR"}},
        {"isin": "XX0000000000", "name": "Quoteless", "securityType": "ETF"}]})
    status, body = call(f"{base}/search?q=nvidia")
    assert status == 200
    assert body["results"] == [
        {"isin": "US67066G1040", "name": "NVIDIA", "type": "STOCK", "price": "192.52", "currency": "EUR"},
        {"isin": "XX0000000000", "name": "Quoteless", "type": "ETF", "price": None, "currency": None}]
    assert calls == [("search_securities", {"query": "nvidia"})]


def test_search_rejects_short_query(mcp_listener):
    base, calls = mcp_listener()
    assert call(f"{base}/search?q=a")[0] == 400
    assert call(f"{base}/search?q=%20a%20")[0] == 400
    assert call(f"{base}/search")[0] == 400
    assert calls == []


def test_watchlist_add_remove(mcp_listener):
    base, calls = mcp_listener()
    assert post(f"{base}/watchlist", {"isin": "US67066G1040", "action": "add"}) == (200, {"status": "ok"})
    assert post(f"{base}/watchlist", {"isin": "US67066G1040", "action": "remove"}) == (200, {"status": "ok"})
    assert calls == [("add_watchlist_item", {"isin": "US67066G1040"}),
                     ("remove_watchlist_item", {"isin": "US67066G1040"})]
    assert post(f"{base}/watchlist", {"isin": "US67066G1040", "action": "x"})[0] == 400
    assert post(f"{base}/watchlist", {"action": "add"})[0] == 400
    assert len(calls) == 2


def test_mcp_error_is_502(mcp_listener):
    base, _ = mcp_listener(error="boom")
    assert call(f"{base}/search?q=nvidia") == (502, {"error": "boom"})
    assert post(f"{base}/watchlist", {"isin": "US67066G1040", "action": "add"}) == (502, {"error": "boom"})


def test_unauthorized_new_paths(mcp_listener):
    base, calls = mcp_listener()
    assert call(f"{base}/search?q=nvidia", token="nope")[0] == 401
    assert post(f"{base}/watchlist", {"isin": "US67066G1040", "action": "add"}, token="nope")[0] == 401
    assert calls == []


def test_new_paths_are_404_without_mcp(listener):
    url, _ = listener()
    assert call(url.replace("/fetch", "/search?q=nvidia"))[0] == 404
    assert post(url.replace("/fetch", "/watchlist"), {"isin": "X", "action": "add"})[0] == 404

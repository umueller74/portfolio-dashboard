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

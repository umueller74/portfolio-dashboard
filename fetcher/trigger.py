"""HTTP listener on the fetcher host, so the dashboard's "Fetch now" button can start a fetch. Standard library
only (runs on Hermes' interpreter, started as a script). With the ingest token as bearer, POST /fetch starts
FETCH_CMD unless one still runs (409), GET /fetch tells whether one runs and how the last one ended. With an
`mcp` caller, GET /search?q= searches Scalable's securities and POST /watchlist adds or removes an ISIN."""
import hmac
import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # run by path: `python -I trigger.py`

log = logging.getLogger("trigger")


class Runner:
    """At most one fetch process at a time; remembers the exit code of the last one."""

    def __init__(self, cmd: list[str]):
        self.cmd, self.proc, self.last_exit = cmd, None, None
        self.lock = threading.Lock()

    def _reap(self) -> None:
        if self.proc is not None and self.proc.poll() is not None:
            self.last_exit, self.proc = self.proc.returncode, None

    def start(self) -> bool:
        with self.lock:
            self._reap()
            if self.proc is not None:
                return False
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.DEVNULL)
            return True

    def status(self) -> dict:
        with self.lock:
            self._reap()
            return {"running": self.proc is not None, "last_exit": self.last_exit}


def _hit(r: dict) -> dict:
    quote = r.get("currentQuote") or {}
    price = quote.get("midPrice")
    return {"isin": r.get("isin"), "name": r.get("name"), "type": r.get("securityType"),
            "price": str(price) if price is not None else None, "currency": quote.get("currency")}


def make_server(host: str, port: int, token: str, runner: Runner, mcp=None) -> ThreadingHTTPServer:
    """`mcp(tool, args) -> dict` (sync) enables /search and /watchlist; without it they are 404."""
    expected = f"Bearer {token}"
    routes = {("GET", "/fetch"), ("POST", "/fetch")} | ({("GET", "/search"), ("POST", "/watchlist")} if mcp else set())

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self, method: str) -> bool:
            if (method, urllib.parse.urlsplit(self.path).path) not in routes:
                self._reply(404, {"error": "not found"})
            elif not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                self._reply(401, {"error": "unauthorized"})
            else:
                return True
            return False

        def _mcp(self, tool: str, args: dict) -> dict | None:
            """The MCP result, or None after replying 502."""
            try:
                return mcp(tool, args)
            except Exception as e:
                log.warning("%s failed: %s", tool, e)
                self._reply(502, {"error": str(e)})
                return None

        def _search(self) -> None:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("q", [""])[0].strip()
            if len(q) < 2:
                return self._reply(400, {"error": "query too short"})
            found = self._mcp("search_securities", {"query": q})
            if found is not None:
                self._reply(200, {"results": [_hit(r) for r in found.get("results", [])]})

        def _watchlist(self) -> None:
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                isin, tool = body["isin"], {"add": "add_watchlist_item", "remove": "remove_watchlist_item"}[body["action"]]
                if not isinstance(isin, str) or not isin:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                return self._reply(400, {"error": "expected {isin, action: add|remove}"})
            if self._mcp(tool, {"isin": isin}) is not None:
                self._reply(200, {"status": "ok"})

        def do_GET(self):
            if self._allowed("GET"):
                if urllib.parse.urlsplit(self.path).path == "/search":
                    self._search()
                else:
                    self._reply(200, runner.status())

        def do_POST(self):
            if self._allowed("POST"):
                if urllib.parse.urlsplit(self.path).path == "/watchlist":
                    return self._watchlist()
                started = runner.start()
                if started:
                    log.info("fetch started for %s", self.client_address[0])
                self._reply(202, {"status": "started"}) if started else self._reply(409, {"status": "running"})

        def log_message(self, fmt, *args):   # one line per started fetch is enough in the journal
            pass

    return ThreadingHTTPServer((host, port), Handler)


def mcp_caller(url: str):
    """A sync `mcp(tool, args) -> dict` that opens one Scalable session per call. Needs `auth_hermes.bootstrap()`
    to have run; `mcp` is imported lazily so the module and its tests work without it."""
    import asyncio

    from fetcher.session import open_call

    async def once(tool: str, args: dict) -> dict:
        async with open_call(url) as call:
            return await call(tool, args)

    return lambda tool, args: asyncio.run(once(tool, args))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from fetcher.auth_hermes import bootstrap

    bootstrap()  # must precede any `mcp` import
    mcp = mcp_caller(os.environ.get("MCP_URL", "https://mcp.scalable.capital/mcp"))
    server = make_server(os.environ.get("TRIGGER_HOST", "0.0.0.0"), int(os.environ.get("TRIGGER_PORT", "8770")),
                         os.environ["INGEST_TOKEN"], Runner(shlex.split(os.environ["FETCH_CMD"])), mcp)
    log.info("listening on %s:%d", *server.server_address[:2])
    server.serve_forever()


if __name__ == "__main__":
    main()

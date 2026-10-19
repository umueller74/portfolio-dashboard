"""HTTP listener on the fetcher host, so the dashboard's "Fetch now" button can start a fetch. Standard library
only (runs on Hermes' interpreter, started as a script). With the ingest token as bearer, POST /fetch starts
FETCH_CMD unless one still runs (409), GET /fetch tells whether one runs and how the last one ended."""
import hmac
import json
import logging
import os
import shlex
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


def make_server(host: str, port: int, token: str, runner: Runner) -> ThreadingHTTPServer:
    expected = f"Bearer {token}"

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self) -> bool:
            if self.path != "/fetch":
                self._reply(404, {"error": "not found"})
            elif not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                self._reply(401, {"error": "unauthorized"})
            else:
                return True
            return False

        def do_GET(self):
            if self._allowed():
                self._reply(200, runner.status())

        def do_POST(self):
            if self._allowed():
                started = runner.start()
                if started:
                    log.info("fetch started for %s", self.client_address[0])
                self._reply(202, {"status": "started"}) if started else self._reply(409, {"status": "running"})

        def log_message(self, fmt, *args):   # one line per started fetch is enough in the journal
            pass

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    server = make_server(os.environ.get("TRIGGER_HOST", "0.0.0.0"), int(os.environ.get("TRIGGER_PORT", "8770")),
                         os.environ["INGEST_TOKEN"], Runner(shlex.split(os.environ["FETCH_CMD"])))
    log.info("listening on %s:%d", *server.server_address[:2])
    server.serve_forever()


if __name__ == "__main__":
    main()

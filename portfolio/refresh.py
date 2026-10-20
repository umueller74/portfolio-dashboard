import json
import urllib.error
import urllib.request


def trigger_client(url: str | None, token: str, opener=urllib.request.urlopen):
    """Calls the fetcher host's listener (`fetcher/trigger.py`) at `url`: `call("POST")` starts a fetch,
    `call("GET")` asks whether one runs. Other `path`s replace the `/fetch` at the end of `url` (`/search?q=x`,
    `/watchlist`); `body` is sent as JSON. Returns (status, body); network errors raise OSError. None without url."""
    if not url:
        return None

    base = url.removesuffix("/fetch")

    def call(method: str, path: str = "/fetch", body: dict | None = None) -> tuple[int, dict]:
        headers = {"Authorization": f"Bearer {token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(base + path, method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            try:
                with opener(req, timeout=10) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:   # 409 (already running) and 401 carry a JSON body too
                return e.code, json.loads(e.read() or b"{}")
        except ValueError as e:                   # e.g. an HTML error page from a proxy
            raise OSError(f"invalid reply from the listener: {e}") from e

    return call

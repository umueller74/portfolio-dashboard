import json
import urllib.error
import urllib.request


def trigger_client(url: str | None, token: str, opener=urllib.request.urlopen):
    """Calls the fetcher host's listener (`fetcher/trigger.py`) at `url`: `call("POST")` starts a fetch,
    `call("GET")` asks whether one runs. Returns (status, body); network errors raise OSError. None without url."""
    if not url:
        return None

    def call(method: str) -> tuple[int, dict]:
        req = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {token}"})
        try:
            with opener(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:   # 409 (already running) and 401 carry a JSON body too
            return e.code, json.loads(e.read() or b"{}")

    return call

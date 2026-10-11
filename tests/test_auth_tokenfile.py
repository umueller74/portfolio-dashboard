import json
import os
import stat

import httpx
import pytest

from fetcher.auth_tokenfile import get_access_token


def make_dir(tmp_path, expires_at):
    (tmp_path / "scalable.json").write_text(json.dumps({
        "access_token": "old", "token_type": "Bearer", "expires_in": 3600, "scope": "x",
        "refresh_token": "r1", "expires_at": expires_at, "hermes_issuer": "i"}))
    (tmp_path / "scalable.meta.json").write_text(json.dumps({"token_endpoint": "https://auth.example/token"}))
    (tmp_path / "scalable.client.json").write_text(json.dumps({"client_id": "cid"}))
    return tmp_path


def test_valid_token_makes_no_http_call(tmp_path):
    d = make_dir(tmp_path, expires_at=10_000)

    def boom(request):
        raise AssertionError("no http expected")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    assert get_access_token(d, now=lambda: 1000, client=client) == "old"


def test_expired_token_is_refreshed_and_persisted(tmp_path):
    d = make_dir(tmp_path, expires_at=1000)
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600, "refresh_token": "r2"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert get_access_token(d, now=lambda: 2000, client=client) == "new"
    assert "grant_type=refresh_token" in seen["body"] and "refresh_token=r1" in seen["body"] and "client_id=cid" in seen["body"]
    saved = json.loads((d / "scalable.json").read_text())
    assert saved["access_token"] == "new" and saved["refresh_token"] == "r2"
    assert saved["expires_at"] == 2000 + 3600 and saved["hermes_issuer"] == "i"   # foreign fields survive
    assert stat.S_IMODE(os.stat(d / "scalable.json").st_mode) == 0o600


def test_refresh_failure_raises(tmp_path):
    d = make_dir(tmp_path, expires_at=1000)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"})))
    with pytest.raises(httpx.HTTPStatusError):
        get_access_token(d, now=lambda: 2000, client=client)

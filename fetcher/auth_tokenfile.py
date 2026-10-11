"""Fallback: refresh the Hermes token file directly, under the same flock Hermes uses."""
import fcntl
import json
import os
import time
from pathlib import Path

import httpx

SKEW_SECONDS = 120


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def get_access_token(token_dir, name: str = "scalable", now=time.time, client=None) -> str:
    d = Path(token_dir)
    token_path = d / f"{name}.json"
    tok = _read(token_path)
    if tok["expires_at"] - SKEW_SECONDS > now():
        return tok["access_token"]
    meta, registered = _read(d / f"{name}.meta.json"), _read(d / f"{name}.client.json")
    with open(d / f"{name}.json.refresh.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        tok = _read(token_path)  # another process (Hermes) may have refreshed while we waited
        if tok["expires_at"] - SKEW_SECONDS > now():
            return tok["access_token"]
        post = (client or httpx).post
        resp = post(meta["token_endpoint"], data={
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
            "client_id": registered["client_id"]}, timeout=30)
        resp.raise_for_status()
        new = resp.json()
        tok.update(access_token=new["access_token"], expires_in=new["expires_in"],
                   expires_at=now() + new["expires_in"])
        if "refresh_token" in new:
            tok["refresh_token"] = new["refresh_token"]
        tmp = d / f"{name}.json.tmp"
        tmp.write_text(json.dumps(tok))
        os.chmod(tmp, 0o600)
        os.replace(tmp, token_path)
        return tok["access_token"]

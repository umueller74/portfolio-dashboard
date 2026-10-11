from tests.conftest import AUTH, payload


def test_rejects_missing_or_wrong_token(client):
    assert client.post("/api/ingest", json=payload()).status_code == 401
    assert client.post("/api/ingest", json=payload(), headers={"Authorization": "Bearer nope"}).status_code == 401


def test_ingest_is_idempotent(client):
    for _ in range(2):
        assert client.post("/api/ingest", json=payload(), headers=AUTH).status_code == 200
    conn = client.app.state.conn
    assert conn.execute("select count(*) from trades").fetchone()[0] == 1
    assert conn.execute("select count(*) from closes").fetchone()[0] == 1
    assert conn.execute("select value from meta where key='last_ingest'").fetchone()[0] == "2026-10-11T21:30:00+00:00"


def test_unknown_trade_kind_is_stored(client):
    body = payload()
    body["trades"][0]["kind"] = "SOMETHING_NEW"
    assert client.post("/api/ingest", json=body, headers=AUTH).status_code == 200


def test_fee_updates_on_second_ingest(client):
    client.post("/api/ingest", json=payload(), headers=AUTH)
    body = payload()
    body["trades"][0]["fee"] = "0.99"
    client.post("/api/ingest", json=body, headers=AUTH)
    row = client.app.state.conn.execute("select fee from trades").fetchone()
    assert row["fee"] == "0.99"


def test_backup_returns_sqlite_file(client):
    client.post("/api/ingest", json=payload(), headers=AUTH)
    r = client.get("/api/backup", headers=AUTH)
    assert r.status_code == 200 and r.content.startswith(b"SQLite format 3")
    assert client.get("/api/backup").status_code == 401

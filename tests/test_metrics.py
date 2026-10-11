from tests.conftest import AUTH, payload


def test_metrics_empty_then_after_ingest(client):
    text = client.get("/metrics").text
    assert "portfolio_last_ingest_timestamp_seconds 0" in text
    client.post("/api/ingest", headers=AUTH, json=payload(fetched_at="2026-10-11T21:30:00+00:00"))
    text = client.get("/metrics").text
    assert "portfolio_last_ingest_timestamp_seconds 1791754200" in text
    assert "portfolio_trades_total 1" in text

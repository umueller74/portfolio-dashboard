import pytest
from fastapi.testclient import TestClient

from portfolio.app import create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path / "p.db", TOKEN))


def payload(**over):
    body = {
        "fetched_at": "2026-10-11T21:30:00+00:00",
        "trades": [
            {"id": "t1", "isin": "IE0000000001", "name": "Test World ETF (Acc)", "side": "BUY",
             "kind": "SINGLE", "executed_at": "2026-09-14T09:00:48.260Z",
             "quantity": "3", "price": "700.00", "amount": "-2100.00"},
        ],
        "closes": [{"isin": "IE0000000001", "day": "2026-10-09", "price": "710.50"}],
        "quotes": [{"isin": "IE0000000001", "price": "710.50",
                    "quoted_at": "2026-10-09T20:59:56.615Z", "outdated": True}],
    }
    body.update(over)
    return body

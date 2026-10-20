import io
import json
import urllib.error
from datetime import date
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from portfolio import history
from portfolio.app import create_app
from tests.conftest import AUTH, TOKEN, payload
from tests.test_services import ISIN, trade

# Fri 2025-12-26 .. Mon 2025-12-29 as justETF sends it: weekends carry Friday's value
JUSTETF = {"series": [{"date": d, "value": {"raw": v, "localized": str(v)}} for d, v in [
    ("2025-12-26", 99.5), ("2025-12-27", 99.5), ("2025-12-28", 99.5), ("2025-12-29", 100.25)]]}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_justetf_parses_daily_eur_prices_without_weekends():
    seen = {}

    def opener(req, timeout):
        seen["url"] = req.full_url
        return Resp(json.dumps(JUSTETF).encode())

    assert history.justetf(ISIN, opener=opener) == [(date(2025, 12, 26), D("99.5")), (date(2025, 12, 29), D("100.25"))]
    assert f"/api/etfs/{ISIN}/performance-chart" in seen["url"] and "currency=EUR" in seen["url"]


def test_justetf_returns_none_for_an_unknown_isin_and_raises_otherwise():
    def missing(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    def limited(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "too many requests", {}, None)

    assert history.justetf(ISIN, opener=missing) is None
    with pytest.raises(urllib.error.HTTPError):
        history.justetf(ISIN, opener=limited)


class Source:
    """Fake history source: one call per ISIN is recorded; `fail` raises, an ISIN not in `data` is unknown."""
    def __init__(self, data, fail=False):
        self.data, self.fail, self.calls = data, fail, []

    def __call__(self, isin):
        self.calls.append(isin)
        if self.fail:
            raise OSError("network down")
        return self.data.get(isin)


@pytest.fixture
def conn(client):
    body = payload(trades=[trade("a", "BUY", "1", "100", "2026-03-02T10:00:00Z")],
                   closes=[{"isin": ISIN, "day": "2026-01-02", "price": "101"}])
    assert client.post("/api/ingest", json=body, headers=AUTH).status_code == 200
    return client.app.state.conn


def stored(conn, isin=ISIN):
    return [(r["day"], r["price"]) for r in conn.execute("select day, price from closes where isin=? order by day", (isin,))]


def test_backfill_adds_only_days_before_the_first_stored_close(conn):
    src = Source({ISIN: [(date(2025, 12, 31), D("99")), (date(2026, 1, 2), D("50")), (date(2026, 1, 5), D("51"))]})
    history.backfill(conn, src)
    # the broker's own closes stay the source from their first day on
    assert stored(conn) == [("2025-12-31", "99"), ("2026-01-02", "101")]


def test_backfill_runs_once_per_isin_and_includes_new_and_sold_etfs(conn):
    src = Source({ISIN: [(date(2025, 12, 31), D("99"))], "IE2": [(date(2025, 6, 2), D("7"))]})
    history.backfill(conn, src)
    history.backfill(conn, src)
    assert src.calls == [ISIN]
    with conn:   # a new ETF bought and sold again still gets its history on the next run
        for t in ("b", "BUY"), ("c", "SELL"):
            conn.execute("insert into trades(id,isin,name,side,kind,executed_at,quantity,price,amount)"
                         " values(?,?,?,?,?,?,?,?,?)", (t[0], "IE2", "Other", t[1], "SINGLE", "2026-04-01T10:00:00+00:00", "1", "7", "7"))
    history.backfill(conn, src)
    assert src.calls == [ISIN, "IE2"] and stored(conn, "IE2") == [("2025-06-02", "7")]


def test_backfill_retries_after_an_error_but_not_for_an_unknown_isin(conn):
    failing = Source({}, fail=True)
    history.backfill(conn, failing)
    history.backfill(conn, failing)
    assert failing.calls == [ISIN, ISIN] and stored(conn) == [("2026-01-02", "101")]
    unknown = Source({})
    history.backfill(conn, unknown)
    history.backfill(conn, unknown)
    assert unknown.calls == [ISIN]


def test_backfill_without_a_source_does_nothing(conn):
    history.backfill(conn, None)
    assert stored(conn) == [("2026-01-02", "101")]


def test_ingest_backfills_the_history_of_its_etfs(tmp_path):
    src = Source({ISIN: [(date(2025, 12, 31), D("99"))]})
    client = TestClient(create_app(tmp_path / "p.db", TOKEN, history=src))
    assert client.post("/api/ingest", json=payload(), headers=AUTH).status_code == 200
    assert src.calls == [ISIN] and stored(client.app.state.conn)[0] == ("2025-12-31", "99")


def test_backfill_includes_watched(conn):
    from portfolio import watch
    watch.remember(conn, "IE00000000W3", "Zeta ETF", "ETF")
    src = Source({"IE00000000W3": [(date(2025, 6, 2), D("7"))]})
    history.backfill(conn, src)
    history.backfill(conn, src)
    assert src.calls == [ISIN, "IE00000000W3"] and stored(conn, "IE00000000W3") == [("2025-06-02", "7")]

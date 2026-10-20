from datetime import date, timedelta

from portfolio import watch
from portfolio.watch import plus_months
from tests.conftest import AUTH, payload
from tests.test_services import ISIN, trade

BUY = trade("a", "BUY", "5", "100", "2026-08-03T10:00:00Z")
ENTRY = {"isin": ISIN, "name": "Test World ETF", "type": "ETF"}
NONE = {"add": [], "remove": []}


def ingest(client, day: str, trades=(), watchlist=None):
    body = payload(fetched_at=f"{day}T10:00:00+00:00", trades=list(trades), closes=[], quotes=[])
    if watchlist is not None:
        body["watchlist"] = watchlist
    r = client.post("/api/ingest", headers=AUTH, json=body)
    assert r.status_code == 200


def state(conn, isin=ISIN):
    row = conn.execute("select state, sold_on from watch_auto where isin=?", (isin,)).fetchone()
    return tuple(row) if row else None


def added(client):
    """Release on 2026-10-01, sell-out on 2026-10-05, confirmed on the list by the next fetch."""
    ingest(client, "2026-10-01", [BUY], watchlist=[])
    ingest(client, "2026-10-05", [trade("b", "SELL", "5", "120", "2026-10-05T10:00:00Z")], watchlist=[])
    ingest(client, "2026-10-06", watchlist=[ENTRY])
    return client.app.state.conn


def test_plus_months():
    assert plus_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert plus_months(date(2026, 10, 5), 3) == date(2027, 1, 5)


def test_sellout_before_release_is_ignored(client):
    ingest(client, "2026-09-01", [BUY, trade("b", "SELL", "5", "120", "2026-09-01T10:00:00Z")])
    ingest(client, "2026-10-20", watchlist=[])
    conn = client.app.state.conn
    assert state(conn) is None and watch.actions(conn) == NONE
    assert ISIN not in watch.watched(conn)


def test_sellout_after_release_is_added_then_confirmed(client):
    ingest(client, "2026-10-01", [BUY], watchlist=[])
    ingest(client, "2026-10-05", [trade("b", "SELL", "5", "120", "2026-10-05T10:00:00Z")], watchlist=[])
    conn = client.app.state.conn
    assert state(conn) == ("pending_add", "2026-10-05")
    assert watch.actions(conn) == {"add": [ISIN], "remove": []}
    assert ISIN in watch.watched(conn)  # shown while the add is still on its way
    ingest(client, "2026-10-06", watchlist=[ENTRY])
    assert state(conn)[0] == "added" and watch.actions(conn) == NONE
    assert watch.watched(conn)[ISIN] == {"name": "Test World ETF", "type": "ETF"}


def test_three_months_then_removed(client):
    conn = added(client)
    due = plus_months(date(2026, 10, 5), 3)
    watch.reconcile(conn, due - timedelta(days=1))
    assert state(conn)[0] == "added" and watch.actions(conn) == NONE
    watch.reconcile(conn, due)
    assert watch.actions(conn) == {"add": [], "remove": [ISIN]}
    ingest(client, "2027-01-06", watchlist=[])
    assert state(conn)[0] == "done" and watch.actions(conn) == NONE
    assert ISIN not in watch.watched(conn)


def test_user_removal_sticks(client):
    conn = added(client)
    ingest(client, "2026-10-10", watchlist=[])
    assert state(conn)[0] == "done"
    ingest(client, "2026-10-11", watchlist=[])
    assert state(conn)[0] == "done" and watch.actions(conn) == NONE
    assert ISIN not in watch.watched(conn)


def test_already_on_list_is_never_auto(client):
    ingest(client, "2026-10-01", [BUY], watchlist=[ENTRY])
    ingest(client, "2026-10-05", [trade("b", "SELL", "5", "120", "2026-10-05T10:00:00Z")], watchlist=[ENTRY])
    conn = client.app.state.conn
    assert state(conn)[0] == "done" and watch.actions(conn) == NONE
    ingest(client, "2027-03-01", watchlist=[ENTRY])
    assert state(conn)[0] == "done" and watch.actions(conn) == NONE
    assert ISIN in watch.watched(conn)


def test_new_sellout_after_rebuy_adds_again(client):
    conn = added(client)
    ingest(client, "2026-10-10", watchlist=[])
    assert state(conn)[0] == "done"
    ingest(client, "2026-11-02", [trade("c", "BUY", "2", "100", "2026-11-02T10:00:00Z")], watchlist=[])
    assert state(conn) is None  # held again
    ingest(client, "2026-11-20", [trade("d", "SELL", "2", "110", "2026-11-20T10:00:00Z")], watchlist=[])
    assert state(conn) == ("pending_add", "2026-11-20")
    assert watch.actions(conn) == {"add": [ISIN], "remove": []}


def test_ingest_without_watchlist_keeps_it(client):
    ingest(client, "2026-10-01", watchlist=[ENTRY])
    ingest(client, "2026-10-02")
    conn = client.app.state.conn
    assert [tuple(r) for r in conn.execute("select isin, name, type from watchlist")] == [
        (ISIN, "Test World ETF", "ETF")]
    assert conn.execute("select value from meta where key='watch_since'").fetchone()[0] == "2026-10-01"
    ingest(client, "2026-10-03", watchlist=[])
    assert conn.execute("select value from meta where key='watch_since'").fetchone()[0] == "2026-10-01"


def test_manual_and_remember(client):
    conn = added(client)
    watch.manual(conn, ISIN, added=False)
    assert state(conn)[0] == "done" and ISIN not in watch.watched(conn)
    watch.remember(conn, "IE2", "Other", "STOCK")
    watch.manual(conn, "IE2", added=True)
    assert watch.watched(conn)["IE2"] == {"name": "Other", "type": "STOCK"}


def test_actions_endpoint_requires_token(client):
    assert client.get("/api/watchlist/actions").status_code == 401
    r = client.get("/api/watchlist/actions", headers=AUTH)
    assert r.status_code == 200 and r.json() == NONE

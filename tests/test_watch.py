from datetime import date, timedelta

from portfolio import watch
from portfolio.watch import plus_months
from tests.conftest import AUTH, payload
from tests.test_services import ISIN, trade

BUY = trade("a", "BUY", "5", "100", "2026-08-03T10:00:00Z")
ENTRY = {"isin": ISIN, "name": "Test World ETF", "type": "ETF"}
NONE = {"add": [], "remove": []}


def ingest(client, day: str, trades=(), watchlist=None, at="10:00:00+00:00"):
    body = payload(fetched_at=f"{day}T{at}", trades=list(trades), closes=[], quotes=[])
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


SINCE = "2026-10-01T10:00:00+00:00"


def test_ingest_without_watchlist_keeps_it(client):
    ingest(client, "2026-10-01", watchlist=[ENTRY])
    ingest(client, "2026-10-02")
    conn = client.app.state.conn
    assert [tuple(r) for r in conn.execute("select isin, name, type from watchlist")] == [
        (ISIN, "Test World ETF", "ETF")]
    assert conn.execute("select value from meta where key='watch_since'").fetchone()[0] == SINCE
    ingest(client, "2026-10-03", watchlist=[])
    assert conn.execute("select value from meta where key='watch_since'").fetchone()[0] == SINCE


def test_manual_and_remember(client):
    conn = added(client)
    watch.manual(conn, ISIN, added=False)
    assert state(conn)[0] == "done" and ISIN not in watch.watched(conn)
    watch.remember(conn, "IE2", "Other", "STOCK")
    watch.manual(conn, "IE2", added=True)
    assert watch.watched(conn)["IE2"] == {"name": "Other", "type": "STOCK"}


def test_sellout_earlier_on_release_day_is_ignored(client):
    ingest(client, "2026-10-20", [BUY, trade("b", "SELL", "5", "120", "2026-10-20T08:00:00Z")], watchlist=[])
    conn = client.app.state.conn
    assert state(conn) is None and watch.actions(conn) == NONE
    ingest(client, "2026-10-20", [trade("c", "BUY", "1", "100", "2026-10-20T11:00:00Z"),
                                  trade("d", "SELL", "1", "100", "2026-10-20T12:00:00Z")], watchlist=[],
           at="13:00:00+00:00")
    assert state(conn) == ("pending_add", "2026-10-20")


def test_last_trade_is_latest_by_time(client):
    # same instant written with different offsets: load order (by text) differs from time order
    ingest(client, "2026-10-01", [BUY], watchlist=[])
    ingest(client, "2026-10-06", [trade("b", "SELL", "2", "120", "2026-10-05T23:30:00+02:00"),
                                  trade("c", "SELL", "3", "120", "2026-10-05T22:00:00-03:00")], watchlist=[])
    assert watch.sellouts(client.app.state.conn) == {ISIN: date(2026, 10, 6)}


def test_rebuy_of_auto_added_keeps_ownership(client):
    conn = added(client)
    ingest(client, "2026-11-02", [trade("c", "BUY", "2", "100", "2026-11-02T10:00:00Z")], watchlist=[ENTRY])
    assert state(conn) == ("added", "2026-10-05")
    watch.reconcile(conn, date(2027, 1, 5))  # held: the old clock does not run
    assert state(conn) == ("added", "2026-10-05")
    ingest(client, "2027-01-20", [trade("d", "SELL", "2", "110", "2027-01-20T10:00:00Z")], watchlist=[ENTRY])
    assert state(conn) == ("added", "2027-01-20") and watch.actions(conn) == NONE
    due = plus_months(date(2027, 1, 20), 3)
    watch.reconcile(conn, due - timedelta(days=1))
    assert watch.actions(conn) == NONE
    watch.reconcile(conn, due)
    assert watch.actions(conn) == {"add": [], "remove": [ISIN]}


def test_rebuy_while_pending_remove_returns_to_added(client):
    conn = added(client)
    watch.reconcile(conn, date(2027, 1, 5))
    assert state(conn)[0] == "pending_remove"
    ingest(client, "2027-01-06", [trade("c", "BUY", "2", "100", "2027-01-06T09:00:00Z")], watchlist=[ENTRY])
    assert state(conn) == ("added", "2026-10-05") and watch.actions(conn) == NONE
    ingest(client, "2027-02-01", [trade("d", "SELL", "2", "110", "2027-02-01T10:00:00Z")], watchlist=[])
    assert state(conn) == ("pending_add", "2027-02-01")


def test_rebuy_cancels_pending_add(client):
    ingest(client, "2026-10-01", [BUY], watchlist=[])
    ingest(client, "2026-10-05", [trade("b", "SELL", "5", "120", "2026-10-05T10:00:00Z")], watchlist=[])
    conn = client.app.state.conn
    assert watch.actions(conn) == {"add": [ISIN], "remove": []}
    ingest(client, "2026-10-06", [trade("c", "BUY", "1", "100", "2026-10-06T09:00:00Z")], watchlist=[])
    assert state(conn) is None and watch.actions(conn) == NONE


def test_reconcile_uses_fetched_at_day(client):
    conn = added(client)  # due 2027-01-05; Berlin is UTC+1 in January
    ingest(client, "2027-01-04", watchlist=[ENTRY], at="22:30:00+00:00")  # 23:30 Berlin, Jan 4
    assert watch.actions(conn) == NONE
    ingest(client, "2027-01-04", watchlist=[ENTRY], at="23:30:00+00:00")  # 00:30 Berlin, Jan 5
    assert watch.actions(conn) == {"add": [], "remove": [ISIN]}


def test_old_fetcher_sellout_creates_no_row(client):
    ingest(client, "2026-10-01", [BUY], watchlist=[])
    ingest(client, "2026-10-05", [trade("b", "SELL", "5", "120", "2026-10-05T10:00:00Z")])
    conn = client.app.state.conn
    assert state(conn) is None and watch.actions(conn) == NONE
    ingest(client, "2026-10-06", watchlist=[])
    assert state(conn) == ("pending_add", "2026-10-05")


def test_pending_remove_stays_while_listed(client):
    conn = added(client)
    watch.reconcile(conn, date(2027, 1, 5))
    ingest(client, "2027-01-10", watchlist=[ENTRY])
    assert state(conn)[0] == "pending_remove"
    assert watch.actions(conn) == {"add": [], "remove": [ISIN]}


def test_oversell_changes_nothing(client):
    conn = added(client)
    ingest(client, "2026-10-07", [trade("x", "SELL", "1", "100", "2026-10-07T10:00:00Z")], watchlist=[])
    assert state(conn) == ("added", "2026-10-05")  # would be done (removed by the user) with sane trades
    assert watch.actions(conn) == NONE and watch.sellouts(conn) == {}


def test_actions_endpoint_requires_token(client):
    assert client.get("/api/watchlist/actions").status_code == 401
    r = client.get("/api/watchlist/actions", headers=AUTH)
    assert r.status_code == 200 and r.json() == NONE

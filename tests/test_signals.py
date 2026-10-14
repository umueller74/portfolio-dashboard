import json
from datetime import date, datetime, timedelta, timezone

import pytest

from portfolio import signals
from tests.conftest import AUTH
from tests.test_services import ISIN, seed, trade

START = date(2026, 1, 1)
EVENING = datetime(2026, 12, 31, 22, 30, tzinfo=timezone.utc)      # long after every seeded close


def closes(prices, isin=ISIN, start=START):
    return [{"isin": isin, "day": (start + timedelta(days=i)).isoformat(), "price": str(p)} for i, p in enumerate(prices)]


UP = [100] * 50 + [110] * 10              # MA 20d 105 > MA 50d 102
DOWN = UP + [90] * 30                     # MA 20d 90 < MA 50d ~96


class Outbox(list):
    def __call__(self, text):
        self.append(text)


def held(client, prices, isin=ISIN):
    seed(client, [trade("a" + isin, "BUY", "1", "100", "2026-01-01T10:00:00Z", isin=isin)], closes=closes(prices, isin))


def put_closes(client, prices, isin=ISIN):
    """Closes straight into the DB: going through ingest would run (and record) a check of its own."""
    conn = client.app.state.conn
    with conn:
        conn.executemany("insert or replace into closes(isin,day,price) values(:isin,:day,:price)", closes(prices, isin))


def state(client, isin=ISIN):
    row = client.app.state.conn.execute("select state from signals where isin=?", (isin,)).fetchone()
    return row["state"] if row else None


def test_first_check_records_the_state_silently(client):
    held(client, UP)
    out = Outbox()
    assert signals.check(client.app.state.conn, EVENING, out) == []
    assert out == [] and state(client) == "above"


def test_crossing_below_sends_one_sell_then_crossing_above_one_buy(client):
    conn = client.app.state.conn
    held(client, UP)
    signals.check(conn, EVENING, Outbox())
    put_closes(client, DOWN)
    out = Outbox()
    sent = signals.check(conn, EVENING, out)
    assert [s.action for s in sent] == ["SELL"] and len(out) == 1
    assert "SELL" in out[0] and "ETF " + ISIN in out[0] and "below" in out[0]
    assert signals.check(conn, EVENING, out) == [] and len(out) == 1          # no repeat while it stays below
    put_closes(client, DOWN + [120] * 20)
    assert [s.action for s in signals.check(conn, EVENING, out)] == ["BUY"] and "above" in out[-1]


def test_failed_send_keeps_the_old_state_so_the_next_check_retries(client):
    conn = client.app.state.conn
    held(client, UP)
    signals.check(conn, EVENING, Outbox())
    put_closes(client, DOWN)

    def broken(text):
        raise OSError("telegram down")

    assert signals.check(conn, EVENING, broken) == [] and state(client) == "above"
    assert [s.action for s in signals.check(conn, EVENING, Outbox())] == ["SELL"]


def test_without_a_sender_crossings_are_recorded_silently(client):
    conn = client.app.state.conn
    held(client, UP)
    signals.check(conn, EVENING, None)
    put_closes(client, DOWN)
    assert signals.check(conn, EVENING, None) == [] and state(client) == "below"


def test_todays_close_counts_only_after_the_session_ends(client):
    conn = client.app.state.conn
    held(client, DOWN)
    signals.check(conn, EVENING, Outbox())
    put_closes(client, DOWN + [2000])               # a spike, but only on the latest day
    today = START + timedelta(days=len(DOWN))
    morning = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=7)  # 09:00 Berlin
    assert signals.check(conn, morning, Outbox()) == [] and state(client) == "below"
    night = morning + timedelta(hours=14)                                                             # 23:00 Berlin
    assert [s.action for s in signals.check(conn, night, Outbox())] == ["BUY"]


def test_only_held_etfs_with_50_closes_count_and_selling_out_forgets_the_state(client):
    conn = client.app.state.conn
    held(client, UP[:49], isin="IE0000000002")                     # too short for a 50-day average
    held(client, UP)
    signals.check(conn, EVENING, Outbox())
    assert state(client, "IE0000000002") is None and state(client) == "above"
    seed(client, [trade("b", "SELL", "1", "100", "2026-03-01T10:00:00Z")], closes=closes(DOWN))
    assert signals.check(conn, EVENING, Outbox()) == [] and state(client) is None
    seed(client, [trade("c", "BUY", "1", "100", "2026-03-02T10:00:00Z")])
    out = Outbox()
    assert signals.check(conn, EVENING, out) == [] and out == [] and state(client) == "below"   # re-bought: silent again


def test_ingest_runs_the_check(tmp_path):
    from fastapi.testclient import TestClient
    from portfolio.app import create_app

    out = Outbox()
    client = TestClient(create_app(tmp_path / "p.db", "test-token", notify=out))
    held(client, UP)
    assert state(client) == "above"
    seed(client, [], closes=closes(DOWN))
    assert len(out) == 1 and "SELL" in out[0]


def test_telegram_sender_posts_to_the_bot_api():
    calls = []

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{"ok":true}'

    def opener(req, timeout):
        calls.append((req.full_url, json.loads(req.data), timeout))
        return Resp()

    send = signals.telegram_sender("123:abc", "42", opener=opener)
    send("hello")
    assert calls == [("https://api.telegram.org/bot123:abc/sendMessage", {"chat_id": "42", "text": "hello"}, 10)]


def test_telegram_sender_is_none_without_credentials():
    assert signals.telegram_sender("", "42") is None and signals.telegram_sender("123:abc", None) is None

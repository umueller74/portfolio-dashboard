from datetime import date, timedelta
from decimal import Decimal as D

from portfolio import services
from portfolio.services import SeriesPoint
from tests.conftest import AUTH, payload

ISIN = "IE0000000001"


def seed(client, trades, closes=(), quotes=()):
    r = client.post("/api/ingest", headers=AUTH, json=payload(trades=trades, closes=list(closes), quotes=list(quotes)))
    assert r.status_code == 200


def trade(id, side, qty, price, ts, fee="0", isin=ISIN, kind="SINGLE"):
    sign = "-" if side == "BUY" else ""
    return {"id": id, "isin": isin, "name": "ETF " + isin, "side": side, "kind": kind, "executed_at": ts,
            "quantity": qty, "price": price, "amount": f"{sign}{D(qty) * D(price)}", "fee": fee}


def test_overview_values_holding_with_quote(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z")],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    o = services.overview(client.app.state.conn, date(2026, 10, 11))
    h = o.holdings[0]
    assert (h.quantity, h.cost, h.value, h.pnl_abs) == (D(5), D(500), D(550), D(50))
    assert h.pnl_pct == D(10) and o.total_value == D(550)
    assert h.xirr is not None


def test_overview_without_quote_falls_back_to_last_close_then_cost(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z")],
         closes=[{"isin": ISIN, "day": "2026-10-09", "price": "120"}])
    h = services.overview(client.app.state.conn, date(2026, 10, 11)).holdings[0]
    assert h.value == D(600)
    seed(client, [trade("b", "BUY", "1", "100", "2026-10-10T10:00:00Z", isin="IE2")])
    h2 = [x for x in services.overview(client.app.state.conn, date(2026, 10, 11)).holdings if x.isin == "IE2"][0]
    assert h2.price_missing and h2.value == D(100)


def test_sold_out_isin_hidden_but_has_detail(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "SELL", "5", "120", "2026-02-05T10:00:00Z")])
    conn = client.app.state.conn
    assert services.overview(conn, date(2026, 10, 11)).holdings == []
    d = services.isin_detail(conn, ISIN)
    assert d.realized_total == D(100) and len(d.rows) == 2


def test_oversell_sets_error_instead_of_raising(client):
    seed(client, [trade("a", "BUY", "1", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "SELL", "2", "100", "2026-01-06T10:00:00Z")])
    assert services.overview(client.app.state.conn, date(2026, 10, 11)).error
    assert services.isin_detail(client.app.state.conn, ISIN).error


def test_empty_database(client):
    conn = client.app.state.conn
    o = services.overview(conn, date(2026, 10, 11))
    assert o.holdings == [] and o.total_value == D(0) and o.xirr is None
    assert services.value_series(conn) == [] and services.last_session_change(conn) is None


def test_value_series_uses_forward_filled_closes(client):
    seed(client, [trade("a", "BUY", "2", "100", "2026-01-05T10:00:00Z")],
         closes=[{"isin": ISIN, "day": "2026-01-05", "price": "100"},
                 {"isin": ISIN, "day": "2026-01-06", "price": "110"}])
    s = services.value_series(client.app.state.conn)
    assert [(p.day, p.value, p.invested) for p in s] == [
        (date(2026, 1, 5), 200.0, 200.0), (date(2026, 1, 6), 220.0, 200.0)]


def test_slice_range():
    s = [SeriesPoint(date(2026, 1, 1) + timedelta(days=i), 1.0, 1.0) for i in range(300)]
    today = s[-1].day
    assert len(services.slice_range(s, "1w", today)) == 8
    assert len(services.slice_range(s, "all", today)) == 300


def test_last_session_change(client):
    seed(client, [trade("a", "BUY", "2", "100", "2026-01-05T10:00:00Z")],
         closes=[{"isin": ISIN, "day": "2026-10-08", "price": "100"},
                 {"isin": ISIN, "day": "2026-10-09", "price": "110"}])
    abs_, pct = services.last_session_change(client.app.state.conn)
    assert abs_ == D(20) and pct == D(10)


def test_xirr_hidden_for_young_position(client):
    seed(client, [trade("a", "BUY", "1", "100", "2026-10-01T10:00:00Z")],
         quotes=[{"isin": ISIN, "price": "101", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    assert services.overview(client.app.state.conn, date(2026, 10, 11)).holdings[0].xirr is None


def test_fee_trades_match_broker_totals(client):
    # BUY 8 @ 5.25 + 0.99 fee = 42.99 total; SELL 4 @ 11 - 0.99 fee = 43.01 total.
    seed(client, [trade("a", "BUY", "8", "5.25", "2026-01-05T10:00:00Z", fee="0.99"),
                  trade("b", "SELL", "4", "11", "2026-02-05T10:00:00Z", fee="0.99")])
    d = services.isin_detail(client.app.state.conn, ISIN)
    assert d.rows[0].paid == D("42.99") and d.rows[1].paid == D("43.01")
    assert d.realized_total == D("43.01") - D("4") * D("42.99") / D("8")


def _rows(client):
    return {r.trade.id: r for r in services.isin_detail(client.app.state.conn, ISIN).rows}


def test_sell_is_matched_to_the_one_off_buy_of_the_same_quantity(client):
    seed(client, [trade("p1", "BUY", "1", "100", "2026-01-01T10:00:00Z", kind="SAVINGS_PLAN"),
                  trade("a", "BUY", "2", "100", "2026-01-02T10:00:00Z", fee="1"),
                  trade("p2", "BUY", "1", "100", "2026-01-03T10:00:00Z", kind="SAVINGS_PLAN"),
                  trade("b", "SELL", "2", "120", "2026-01-04T10:00:00Z", fee="1")],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    r = _rows(client)
    assert r["a"].match is r["b"].trade and r["b"].match is r["a"].trade
    assert r["a"].realized == r["b"].realized == D(38)               # 239 - 201
    assert r["a"].pnl_pct == r["b"].pnl_pct == D(38) / D(201) * 100
    assert r["a"].remaining == 0 and r["a"].current_value is None
    # plan buys keep their FIFO view: p1 was consumed by the sale, p2 is still held
    assert r["p1"].remaining == 0 and r["p1"].pnl_pct is None and r["p1"].match is None
    assert (r["p2"].remaining, r["p2"].current_value, r["p2"].pnl_pct) == (D(1), D(110), D(10))


def test_trades_without_a_matching_opposite_get_no_pnl(client):
    seed(client, [trade("p", "BUY", "5", "100", "2026-01-01T10:00:00Z", kind="SAVINGS_PLAN"),
                  trade("a", "BUY", "3", "100", "2026-01-02T10:00:00Z"),
                  trade("b", "SELL", "2", "120", "2026-01-03T10:00:00Z"),
                  trade("c", "BUY", "1", "100", "2026-01-04T10:00:00Z"),
                  trade("d", "SELL", "1", "120", "2026-01-05T10:00:00Z")],   # c is later: only earlier buys match
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    r = _rows(client)
    assert r["d"].match is r["c"].trade
    for id in ("a", "b"):
        assert r[id].match is None and r[id].realized is None and r[id].pnl_pct is None
    assert (r["a"].remaining, r["a"].current_value) == (D(3), D(330))


def test_sell_matches_the_nearest_earlier_buy_and_each_buy_only_once(client):
    seed(client, [trade("a", "BUY", "2", "100", "2026-01-01T10:00:00Z"),
                  trade("b", "BUY", "2", "110", "2026-01-02T10:00:00Z"),
                  trade("c", "SELL", "2", "120", "2026-01-03T10:00:00Z"),
                  trade("d", "SELL", "2", "130", "2026-01-04T10:00:00Z")])
    r = _rows(client)
    assert r["c"].match is r["b"].trade and r["d"].match is r["a"].trade
    assert r["c"].realized == D(20) and r["d"].realized == D(60)


def test_range_stats_whole_history_matches_overview_and_short_range_is_a_delta(client):
    today = date(2026, 10, 11)
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "BUY", "1", "100", "2026-10-05T10:00:00Z")],
         closes=[{"isin": ISIN, "day": d, "price": p} for d, p in
                 (("2026-01-05", "100"), ("2026-10-01", "110"), ("2026-10-05", "110"), ("2026-10-09", "120"))],
         quotes=[{"isin": ISIN, "price": "120", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    conn = client.app.state.conn
    ov = services.overview(conn, today)
    full = services.range_stats(conn, ov, "all", today)
    assert full.value_delta is None and full.pnl_delta == ov.pnl_abs and full.pnl_pct == ov.pnl_pct
    assert full.xirr is not None
    week = services.range_stats(conn, ov, "1w", today)   # first chart point 2026-10-05: 6 shares at 110
    assert week.value_delta == D(720) - D(660) and week.invested_delta == D(600) - D(600)
    assert week.pnl_delta == D(60) and week.xirr is None   # (720-600) - (660-600); a week is too short for XIRR


def test_range_stats_include_realized_profit(client):
    today = date(2026, 10, 11)
    seed(client, [trade("a", "BUY", "10", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "SELL", "4", "130", "2026-03-05T10:00:00Z", fee="1")],
         closes=[{"isin": ISIN, "day": "2026-01-05", "price": "100"}, {"isin": ISIN, "day": "2026-10-09", "price": "110"}],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    conn = client.app.state.conn
    ov = services.overview(conn, today)
    st = services.range_stats(conn, ov, "all", today)
    assert ov.pnl_abs == D(60) and st.unrealized == D(60)       # 6 shares held: 660 - 600
    assert st.realized == D(119)                                # 4 * 130 - 1 fee - 4 * 100
    assert st.pnl_delta == D(179)
    assert services.range_stats(conn, ov, "1w", today).realized == D(0)   # the sale is outside the last week


def test_range_stats_for_one_etf_ignore_the_others(client):
    today = date(2026, 10, 11)
    seed(client, [trade("a", "BUY", "10", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "BUY", "5", "20", "2026-01-05T11:00:00Z", isin="IE2"),
                  trade("c", "SELL", "5", "30", "2026-02-05T10:00:00Z", isin="IE2")],
         closes=[{"isin": ISIN, "day": "2026-01-05", "price": "100"}, {"isin": ISIN, "day": "2026-10-09", "price": "110"}],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    conn = client.app.state.conn
    ov = services.overview(conn, today)
    one = services.range_stats(conn, ov, "all", today, ISIN)
    assert (one.unrealized, one.realized) == (D(100), D(0))
    sold = services.range_stats(conn, ov, "all", today, "IE2")          # sold out: all profit is realized
    assert (sold.unrealized, sold.realized) == (D(0), D(50))

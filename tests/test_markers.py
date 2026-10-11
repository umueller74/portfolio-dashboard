from datetime import date
from decimal import Decimal as D

from portfolio import services, web
from tests.test_services import ISIN, seed, trade


def _seed(client):
    seed(client, [
        trade("a", "BUY", "2", "100", "2026-01-10T10:00:00Z", kind="SAVINGS_PLAN"),
        trade("b", "BUY", "1", "50", "2026-01-10T11:00:00Z", isin="IE2", kind="SAVINGS_PLAN"),
        trade("c", "BUY", "5", "100", "2026-02-05T10:00:00Z", kind="SINGLE"),
        trade("d", "SELL", "1", "120", "2026-03-05T10:00:00Z", kind="SINGLE"),
        trade("e", "BUY", "1", "10", "2026-03-06T10:00:00Z", kind="SOMETHING_NEW"),
    ])


def test_markers_group_by_day_and_category(client):
    _seed(client)
    days = [date(2026, 1, 10), date(2026, 2, 5), date(2026, 3, 5), date(2026, 3, 6), date(2026, 4, 1)]
    m = services.trade_markers(client.app.state.conn, days)
    assert sorted(m) == days[:4]
    plans = m[date(2026, 1, 10)]
    assert {x.category for x in plans} == {"plan_buy"} and len(plans) == 2
    assert m[date(2026, 2, 5)][0].category == "cash_buy"
    assert m[date(2026, 3, 5)][0].category == "sell"
    assert m[date(2026, 3, 6)][0].category == "cash_buy"        # unknown kinds count as one-off buys
    assert plans[0].amount == D("200") and plans[0].quantity == D("2")


def test_markers_only_for_requested_days(client):
    _seed(client)
    assert services.trade_markers(client.app.state.conn, [date(2026, 2, 5)]).keys() == {date(2026, 2, 5)}


def test_marker_series_aligns_with_chart_labels(client):
    _seed(client)
    labels = [date(2026, 1, 10), date(2026, 2, 5), date(2026, 3, 5)]
    values = [200.0, 700.0, 580.0]
    m = services.trade_markers(client.app.state.conn, labels)
    out = web.marker_series(m, labels, values)
    assert out["plan_buy"] == [200.0, None, None]
    # the Mar 6 trade of an unknown kind is newer than the last label but inside the grace window,
    # so it is drawn on the last point as a one-off buy
    assert out["cash_buy"] == [None, 700.0, 580.0]
    assert out["sell"] == [None, None, 580.0]
    tip = out["tooltips"][0]
    assert len(tip) == 2 and "2" in tip[0] and "ETF" in tip[0]


def test_no_trades_means_empty_series(client):
    out = web.marker_series({}, [date(2026, 1, 1)], [1.0])
    assert out["plan_buy"] == [None] and out["tooltips"] == [[]]


def test_portfolio_page_renders_marker_legend(client):
    _seed(client)
    seed(client, [], closes=[{"isin": ISIN, "day": "2026-01-10", "price": "100"}])
    html = client.get("/?range=all").text
    for label in ("Savings plan buy", "One-off buy", "Sell"):
        assert label in html

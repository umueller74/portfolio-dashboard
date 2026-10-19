import re

from portfolio import services
from portfolio.web import view_data
from tests.conftest import AUTH, payload
from tests.test_services import ISIN, seed, trade


def test_pages_render_when_empty(client):
    for url in ("/", "/etf", f"/etf/{ISIN}", "/healthz"):
        assert client.get(url).status_code == 200
    assert "No data yet" in client.get("/").text


def test_portfolio_page_shows_totals_and_ranges(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z")],
         closes=[{"isin": ISIN, "day": "2026-10-08", "price": "100"}, {"isin": ISIN, "day": "2026-10-09", "price": "110"}],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    html = client.get("/?range=3m").text
    assert "550.00" in html and "+10.00" in html
    for key in ("1d", "1w", "1m", "3m", "6m", "1y", "all"):
        assert f'data-range="{key}"' in html
    assert 'id="pnlChart"' in html and 'id="valueChart"' in html


def test_etf_list_and_detail(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "SELL", "2", "120", "2026-02-05T10:00:00Z")],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    assert ISIN in client.get("/etf").text
    html = client.get(f"/etf/{ISIN}").text
    assert "2026-01-05" in html and "BUY" in html and "SELL" in html and "+40.00" in html


def test_oversell_shows_banner_not_500(client):
    seed(client, [trade("a", "BUY", "1", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "SELL", "2", "100", "2026-01-06T10:00:00Z")])
    for url in ("/", "/etf", f"/etf/{ISIN}"):
        r = client.get(url)
        assert r.status_code == 200 and "Data error" in r.text


def test_stale_banner_when_last_ingest_old(client):
    client.post("/api/ingest", headers=AUTH, json=payload(fetched_at="2020-01-01T00:00:00+00:00"))
    assert 'class="stale"' in client.get("/").text


def test_single_page_has_one_trades_table_with_a_button_per_etf(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "BUY", "1", "50", "2026-02-05T10:00:00Z", isin="IE0000000002"),
                  trade("c", "SELL", "1", "50", "2026-03-05T10:00:00Z", isin="IE0000000002")],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    html = client.get("/").text
    assert html.count("<table") == 1
    assert 'data-isin="IE0000000002" title' in html and f'data-isin="{ISIN}" title' in html   # the sold-out ETF too
    assert html.count("<th class=\"sort\"") == 11
    assert html.index("2026-03-05") < html.index("2026-02-05")      # server-side default: newest first
    assert 'class="btn active" data-isin=""' in html                # Overview is the default selection


def test_etf_buttons_grouped_into_active_and_inactive(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "BUY", "1", "50", "2026-02-05T10:00:00Z", isin="IE0000000002"),
                  trade("c", "SELL", "1", "50", "2026-03-05T10:00:00Z", isin="IE0000000002")],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    html = client.get("/").text
    active, inactive = html.index('class="grp">Active<'), html.index('class="grp">Inactive<')
    held, sold = html.index(f'data-isin="{ISIN}" title'), html.index('data-isin="IE0000000002" title')
    assert active < held < inactive < sold


def test_unknown_range_falls_back_to_one_year(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z")])
    assert 'class="btn active" data-range="1y"' in client.get("/?range=bogus").text


def test_per_etf_series_only_use_that_etfs_trades(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-06T10:00:00Z"),
                  trade("b", "BUY", "1", "50", "2026-01-06T11:00:00Z", isin="IE0000000002")],
         closes=[{"isin": ISIN, "day": "2026-01-06", "price": "110"},
                 {"isin": "IE0000000002", "day": "2026-01-06", "price": "60"}])
    one = services.value_series(client.app.state.conn, ISIN)
    assert [(p.value, p.invested, p.price) for p in one] == [(550.0, 500.0, 110.0)]
    assert services.value_series(client.app.state.conn)[0].value == 610.0
    m = services.trade_markers(client.app.state.conn, [p.day for p in one], ISIN)
    assert [x.isin for ms in m.values() for x in ms] == [ISIN]


def test_fee_column_marks_prime_etf_buys(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "SELL", "1", "120", "2026-02-05T10:00:00Z", fee="0.99"),
                  trade("c", "BUY", "1", "50", "2026-02-05T10:00:00Z", isin="IE0000000002")],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    conn = client.app.state.conn
    conn.execute("update trades set name='iShares Core MSCI World (Acc)' where isin=?", (ISIN,))
    conn.commit()
    for url in ("/", f"/etf/{ISIN}"):
        html = client.get(url).text
        assert html.count('class="prime"') == 1           # the iShares buy; not its sell, not the other ETF
        assert re.search(r'class="prime"[^>]*>★</span>0\.00</td>', html)   # star in front of the fee


def test_etf_view_has_20_and_50_day_averages_and_a_maximize_button(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-06T10:00:00Z")],
         closes=[{"isin": ISIN, "day": "2026-01-06", "price": "110"}])
    v = view_data(client.app.state.conn, ISIN)
    assert v["ma20"] == [None] and v["ma50"] == [None]
    assert "ma20" not in view_data(client.app.state.conn, None)
    html = client.get("/").text
    assert 'id="plMax"' in html
    assert "plBox.addEventListener('click'" in html and "cursor:zoom-in" in html   # a click into the chart maximizes too


def test_etf_price_chart_covers_closes_from_before_the_first_trade(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-06T10:00:00Z")],
         closes=[{"isin": ISIN, "day": d, "price": p} for d, p in
                 [("2019-03-01", "50"), ("2025-12-31", "98"), ("2026-01-06", "100"), ("2026-01-07", "104")]])
    v = view_data(client.app.state.conn, ISIN)
    assert v["labels"] == ["2026-01-06", "2026-01-07"]               # value chart: from the first trade
    assert v["plabels"] == ["2019-03-01", "2025-12-31", "2026-01-06", "2026-01-07"]   # price chart: all of it
    assert v["second"] == [50.0, 98.0, 100.0, 104.0] and len(v["ma20"]) == 4
    assert v["pmarkers"]["cash_buy"] == [None, None, 100.0, None]
    p = view_data(client.app.state.conn, None)
    assert p["plabels"] == p["labels"] and p["pmarkers"] == p["markers"]


def test_maximized_price_chart_has_its_own_range_buttons(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z")])
    html = client.get("/?range=3m").text
    header = html[html.index('class="maxranges"'):html.index('id="plMax"')]
    for key in ("1d", "1w", "1m", "3m", "6m", "1y", "all"):
        assert f'data-range="{key}"' in header
    assert html.count('class="btn active" data-range="3m"') == 2      # bar and header agree


def test_etf_view_has_every_price_point_of_the_last_month(client):
    ticks = [{"isin": ISIN, "at": at, "price": p} for at, p in [
        ("2026-09-01T08:00:00Z", "90"),                                    # older than a month before the last tick
        ("2026-10-08T07:00:00Z", "100"), ("2026-10-08T07:00:30Z", "100.5"),  # same Berlin minute: the later one wins
        ("2026-10-09T13:15:00Z", "104")]]
    seed(client, [trade("a", "BUY", "5", "100", "2026-10-08T07:30:00Z")],
         closes=[{"isin": ISIN, "day": "2026-10-08", "price": "101"}])
    client.post("/api/ingest", headers=AUTH, json=payload(trades=[], closes=[], quotes=[], ticks=ticks))
    v = view_data(client.app.state.conn, ISIN)
    assert v["tlabels"] == ["2026-10-08 09:00", "2026-10-09 15:15"]        # Berlin time
    assert v["tprices"] == [100.5, 104.0]
    assert v["tmarkers"]["cash_buy"] == [100.5, None]                      # the buy at 09:30 sits on the 09:00 point
    assert len(v["tma20"]) == 2 and "tlabels" not in view_data(client.app.state.conn, None)


def test_price_chart_has_a_trendline_button(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z")])
    html = client.get("/").text
    assert 'id="plTrend"' in html and "aria-pressed=\"false\"" in html     # off by default

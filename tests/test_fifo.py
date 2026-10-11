from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from portfolio.fifo import OversellError, Trade, replay


def t(id, side, qty, price, day, fee="0"):
    return Trade(id, "IE1", "ETF", side, "SINGLE",
                 datetime(2026, 1, day, tzinfo=timezone.utc), D(qty), D(price), D(fee))


def test_sale_consumes_oldest_lot_first():
    lots, sales = replay([t("a", "BUY", "10", "100", 1), t("b", "BUY", "10", "110", 2),
                          t("c", "SELL", "15", "120", 3)])
    assert [l.remaining for l in lots] == [D(0), D(5)]
    assert sales[0].cost_basis == D("1550")          # 10*100 + 5*110
    assert sales[0].realized == D("250")              # 15*120 - 1550


def test_fee_raises_cost_and_lowers_proceeds():
    lots, sales = replay([t("a", "BUY", "10", "100", 1, fee="1"), t("b", "SELL", "10", "110", 2, fee="1")])
    assert lots[0].unit_cost == D("100.1")
    assert sales[0].realized == D("1099") - D("1001")


def test_fractional_quantities_stay_exact():
    lots, _ = replay([t("a", "BUY", "0.296736", "84.25", 1)])
    assert lots[0].cost.quantize(D("0.0001")) == D("25.0000")


def test_oversell_raises():
    with pytest.raises(OversellError):
        replay([t("a", "BUY", "1", "100", 1), t("b", "SELL", "2", "100", 2)])


def test_order_is_by_time_not_input_order():
    lots, sales = replay([t("b", "SELL", "5", "120", 3), t("a", "BUY", "5", "100", 1)])
    assert sales[0].realized == D("100")


def test_sale_only_consumes_lots_of_the_same_isin():
    def tt(id, isin, side, qty, price, day):
        return Trade(id, isin, "ETF", side, "SINGLE", datetime(2026, 1, day, tzinfo=timezone.utc), D(qty), D(price))

    lots, sales = replay([tt("a", "IE1", "BUY", "10", "100", 1), tt("b", "IE2", "BUY", "10", "50", 2),
                          tt("c", "IE2", "SELL", "4", "60", 3)])
    remaining = {l.trade.isin: l.remaining for l in lots}
    assert remaining == {"IE1": D(10), "IE2": D(6)}      # IE1 untouched although it is the older lot
    assert sales[0].cost_basis == D("200") and sales[0].realized == D("40")

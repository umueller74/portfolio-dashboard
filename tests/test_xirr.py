from datetime import date

import pytest

from portfolio.xirr import xirr


def test_simple_one_year_ten_percent():
    assert xirr([(date(2026, 1, 1), -1000), (date(2027, 1, 1), 1100)]) == pytest.approx(0.10, abs=1e-6)


def test_multiple_contributions():
    flows = [(date(2026, 1, 1), -100), (date(2026, 7, 1), -100), (date(2027, 1, 1), 230)]
    # 100 + 100/(1+r)^(181/365) == 230/(1+r)  ->  r = 20.26 %
    assert xirr(flows) == pytest.approx(0.2026, abs=1e-4)


def test_no_sign_change_returns_none():
    assert xirr([(date(2026, 1, 1), -100), (date(2026, 6, 1), -50)]) is None


def test_single_flow_returns_none():
    assert xirr([(date(2026, 1, 1), -100)]) is None

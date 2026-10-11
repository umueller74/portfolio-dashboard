import json
from decimal import Decimal as D

from fetcher import normalize as n

LIST = {"portfolioId": "p", "page": {"nextCursor": None, "totalCount": 4, "returnedCount": 4}, "transactions": [
    {"id": "i1", "kind": "security", "status": "SETTLED", "isCancellation": False, "currency": "EUR",
     "lastEventAt": "2026-09-14T09:00:48.260Z", "description": "Test World ETF (Acc)",
     "security": {"isin": "IE0000000001", "transactionType": "SINGLE", "quantity": "3", "amount": "-2100",
                  "side": "BUY", "limitPrice": "735", "stopPrice": None}, "cash": None},
    {"id": "i2", "kind": "security", "status": "SETTLED", "isCancellation": False, "currency": "EUR",
     "lastEventAt": "2026-09-10T12:13:15.145Z", "description": "Test Emerging ETF (Acc)",
     "security": {"isin": "IE0000000002", "transactionType": "SAVINGS_PLAN", "quantity": "0.296736",
                  "amount": "-25.0000", "side": "BUY", "limitPrice": None, "stopPrice": None}, "cash": None},
    {"id": "i3", "kind": "security", "status": "CANCELLED", "isCancellation": False, "currency": "EUR",
     "lastEventAt": "2026-10-05T07:24:03.452Z", "description": "Test Ex-US ETF (Acc)",
     "security": {"isin": "IE0000000003", "transactionType": "SINGLE", "quantity": "486", "amount": "0",
                  "side": "BUY", "limitPrice": "6.15", "stopPrice": None}, "cash": None},
    {"id": "c1", "kind": "cash", "status": "SETTLED", "isCancellation": False, "currency": "EUR",
     "lastEventAt": "2026-10-08T00:00:00.000Z", "description": "Sparpläne", "security": None,
     "cash": {"relatedIsin": None, "transactionType": "DEPOSIT", "amount": "250"}},
]}


def test_parse_transactions_keeps_only_settled_security_trades():
    trades, cursor = n.parse_transactions(LIST)
    assert [t["id"] for t in trades] == ["i1", "i2"] and cursor is None
    assert trades[0]["price"] == "700.000000"          # abs(amount)/quantity, not limitPrice 735
    # The broker rounds amount to 4 and quantity to 6 decimals, so the derived price is 84.249973
    # Deriving keeps quantity * price equal to the broker's own total.
    assert trades[1]["kind"] == "SAVINGS_PLAN" and trades[1]["price"] == "84.249973"


def test_unwrap_handles_hermes_result_wrapper():
    wrapped = {"result": json.dumps(LIST)}
    assert n.unwrap(wrapped) == LIST and n.unwrap(json.dumps(LIST)) == LIST


def details(total, market):
    return {"transaction": {"securityTrade": {"totalAmount": total,
            "tradeTransactionAmounts": {"marketValuation": market}}}}


def test_fee_from_details():
    assert n.fee_from_details(details("-2100", "2100")) == D(0)
    assert n.fee_from_details(details("-2100.99", "2100")) == D("0.99")
    assert n.fee_from_details(details("-25.0000", None)) == D(0)
    assert n.fee_from_details({"transaction": {"securityTrade": {"totalAmount": "0", "tradeTransactionAmounts": None}}}) == D(0)


def test_parse_chart_one_point_per_berlin_day_last_wins():
    payload = {"points": [
        {"midPrice": 100.0, "timestampUtc": "2026-10-08T07:29:59.000Z"},
        {"midPrice": 101.5, "timestampUtc": "2026-10-08T20:59:57.000Z"},
        {"midPrice": 102.0, "timestampUtc": "2026-10-09T20:59:56.000Z"}]}
    assert n.parse_chart(payload) == [{"day": "2026-10-08", "price": "101.5"}, {"day": "2026-10-09", "price": "102.0"}]


def test_parse_chart_finds_points_under_any_key():
    nested = {"security": {"chart": {"series": [{"midPrice": 1, "timestampUtc": "2026-01-02T21:59:30.000Z"}]}}}
    assert n.parse_chart(nested) == [{"day": "2026-01-02", "price": "1"}]


def test_parse_quote():
    payload = {"security": {"isin": "IE0000000001", "quote": {"midPrice": 710.50, "bidPrice": 709.90,
               "timestampUtc": "2026-10-09T20:59:56.615Z", "isOutdated": True}}}
    assert n.parse_quote(payload) == {"isin": "IE0000000001", "price": "710.5",
                                      "quoted_at": "2026-10-09T20:59:56.615Z", "outdated": True}


def test_price_from_details_excludes_the_fee():
    # BUY 8 units, total -42.99 incl. 0.99 fee, marketValuation 42.00 -> price 5.25
    assert n.price_from_details(details("-42.99", "42.00"), D("8")) == D("5.250000")
    assert n.price_from_details(details("-25.0000", "25.0000"), D("0.296736")) == D("84.249973")
    assert n.price_from_details(details("-1", None), D("1")) is None

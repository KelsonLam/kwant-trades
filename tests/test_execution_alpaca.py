from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from kwanttrades.config import BrokerConfig
from kwanttrades.execution import alpaca as alp
from kwanttrades.execution.alpaca import (
    AlpacaBroker, AlpacaConfigError, LiveTradingNotAllowed, alpaca_limit_price, build_order_spec, to_sdk_request,
)
from kwanttrades.types import Broker, OrderStatus

from tests.test_execution_factories import bull_call_spread, bull_put_spread, chain_for, long_call


class FakeClient:
    def __init__(self):
        self.requests = []
        self.fill_price = 2.05
        self.status = "new"

    def submit_order(self, req):
        self.requests.append(req)
        return SimpleNamespace(id=f"alp-{len(self.requests)}", status="accepted")

    def get_order_by_id(self, oid):
        return SimpleNamespace(id=oid, status=self.status, filled_avg_price=self.fill_price)

    def get_account(self):
        return SimpleNamespace(equity="10100.5", last_equity="10000", cash="9000", buying_power="18000", options_buying_power="9000")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("KWANT_ALLOW_LIVE", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)


def paper_broker(client=None, **kw):
    return AlpacaBroker(BrokerConfig(mode="alpaca_paper"), client=client or FakeClient(), **kw)


def test_spec_single_leg():
    spec = build_order_spec(long_call(), 3, 2.0449)
    assert spec == {"order_class": "simple", "symbol": "SPY261218C00100000", "qty": 3, "side": "buy",
                    "time_in_force": "day", "limit_price": 2.04, "position_intent": "buy_to_open"}
    close = build_order_spec(long_call(), 3, 2.9, opening=False)
    assert close["side"] == "sell" and close["position_intent"] == "sell_to_close" and close["limit_price"] == 2.9


def test_spec_multi_leg_debit_and_credit():
    d = build_order_spec(bull_call_spread(), 2, 1.1)
    assert d["order_class"] == "mleg" and d["qty"] == 2 and d["time_in_force"] == "day" and d["limit_price"] == 1.1
    assert d["legs"] == [
        {"symbol": "SPY261218C00100000", "ratio_qty": 1, "side": "buy", "position_intent": "buy_to_open"},
        {"symbol": "SPY261218C00105000", "ratio_qty": 1, "side": "sell", "position_intent": "sell_to_open"},
    ]
    c = build_order_spec(bull_put_spread(), 1, -0.9)
    assert c["limit_price"] == -0.9  # credit => negative net price (convention isolated in alpaca_limit_price)
    assert [l["side"] for l in c["legs"]] == ["sell", "buy"]
    closing = build_order_spec(bull_put_spread(), 1, -0.8, opening=False)  # pay up to 0.80 to close
    assert closing["limit_price"] == 0.8
    assert [(l["side"], l["position_intent"]) for l in closing["legs"]] == [("buy", "buy_to_close"), ("sell", "sell_to_close")]


def test_limit_price_function_conventions():
    assert alpaca_limit_price(1.234, True, True) == 1.23
    assert alpaca_limit_price(-0.905, True, True) in (-0.9, -0.91)
    assert alpaca_limit_price(-0.9, True, False) == 0.9
    assert alpaca_limit_price(0.9, False, True) == -0.9


def test_sdk_request_objects():
    pytest.importorskip("alpaca.trading.requests")
    from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent, TimeInForce

    single = to_sdk_request(build_order_spec(long_call(), 2, 2.05))
    assert single.symbol == "SPY261218C00100000" and single.qty == 2 and single.side == OrderSide.BUY
    assert single.time_in_force == TimeInForce.DAY and single.limit_price == 2.05
    assert single.position_intent == PositionIntent.BUY_TO_OPEN
    multi = to_sdk_request(build_order_spec(bull_put_spread(), 2, -0.9))
    assert multi.order_class == OrderClass.MLEG and multi.qty == 2 and multi.limit_price == -0.9
    assert [l.symbol for l in multi.legs] == ["SPY261218P00100000", "SPY261218P00095000"]
    assert [l.side for l in multi.legs] == [OrderSide.SELL, OrderSide.BUY]
    assert all(l.ratio_qty == 1 for l in multi.legs)


def test_submit_sends_request_and_tracks_fill_and_close():
    pytest.importorskip("alpaca.trading.requests")
    client = FakeClient()
    b = paper_broker(client)
    assert isinstance(b, Broker)
    o = b.submit(long_call(), 2, 2.05)
    assert o.status == OrderStatus.NEW and o.id == "alp-1"
    assert len(client.requests) == 1 and client.requests[0].qty == 2
    assert b.positions() == []  # not filled yet
    client.status = "filled"
    (pos,) = b.positions()
    assert pos.entry_price == pytest.approx(2.05) and pos.contracts == 2
    b.mark_to_market({"SPY": chain_for(long_call(), [3.0])})
    assert pos.mark == pytest.approx(3.0)
    c = b.close(pos.id, 2.9, "take_profit")
    assert c.action == "close" and client.requests[-1].side.value == "sell"
    client.fill_price = 2.95
    assert b.positions() == [] and pos.exit_price == pytest.approx(2.95)
    assert pos.realized_pnl == pytest.approx((2.95 - 2.05) * 200)


def test_submit_credit_fill_keeps_negative_sign():
    pytest.importorskip("alpaca.trading.requests")
    client = FakeClient()
    client.fill_price = 0.9
    b = paper_broker(client)
    b.submit(bull_put_spread(), 1, -0.9)
    client.status = "filled"
    assert b.positions()[0].entry_price == pytest.approx(-0.9)


def test_broker_exception_becomes_rejection():
    pytest.importorskip("alpaca.trading.requests")

    class Boom(FakeClient):
        def submit_order(self, req):
            raise RuntimeError("403 forbidden")

    o = paper_broker(Boom()).submit(long_call(), 1, 2.05)
    assert o.status == OrderStatus.REJECTED and "403" in o.reason


def test_account_mapping():
    a = paper_broker().account()
    assert a.equity == 10100.5 and a.day_pnl == pytest.approx(100.5) and a.buying_power == 9000.0


def test_live_mode_refused_without_env(monkeypatch):
    with pytest.raises(LiveTradingNotAllowed):
        AlpacaBroker(BrokerConfig(mode="alpaca_live"), client=FakeClient())
    monkeypatch.setenv("KWANT_ALLOW_LIVE", "yes")
    with pytest.raises(LiveTradingNotAllowed):
        AlpacaBroker(BrokerConfig(mode="alpaca_live"), client=FakeClient())


def test_live_mode_allowed_with_env_and_warns(monkeypatch, caplog):
    monkeypatch.setenv("KWANT_ALLOW_LIVE", "YES_I_ACCEPT_THE_RISK")
    with caplog.at_level(logging.WARNING, logger="kwanttrades.alpaca"):
        b = AlpacaBroker(BrokerConfig(mode="alpaca_live"), client=FakeClient())
    assert b.paper is False and "LIVE" in caplog.text


def test_bad_mode_and_missing_keys():
    with pytest.raises(ValueError):
        AlpacaBroker(BrokerConfig(mode="paper"), client=FakeClient())
    with pytest.raises(AlpacaConfigError, match=".env"):
        AlpacaBroker(BrokerConfig(mode="alpaca_paper"))


def test_paper_mode_builds_paper_client_without_network(monkeypatch):
    pytest.importorskip("alpaca.trading.client")
    import alpaca.trading.client as tc

    seen = {}

    class Recorder:
        def __init__(self, key, secret, paper=True):
            seen.update(key=key, secret=secret, paper=paper)

    monkeypatch.setattr(tc, "TradingClient", Recorder)
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    b = AlpacaBroker(BrokerConfig(mode="alpaca_paper"))
    assert seen["paper"] is True and b.paper is True
    monkeypatch.setenv("KWANT_ALLOW_LIVE", "YES_I_ACCEPT_THE_RISK")
    AlpacaBroker(BrokerConfig(mode="alpaca_live"))
    assert seen["paper"] is False


def test_module_constants():
    assert alp.LIVE_ENV == "KWANT_ALLOW_LIVE" and alp.LIVE_ACK == "YES_I_ACCEPT_THE_RISK"

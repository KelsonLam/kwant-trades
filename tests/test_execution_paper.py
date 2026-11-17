from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from kwanttrades.config import BrokerConfig
from kwanttrades.execution.journal import Journal
from kwanttrades.execution.paper import PaperBroker
from kwanttrades.types import NY, Broker, OrderStatus

from tests.test_execution_factories import (
    EXPIRY, NOW, bull_call_spread, bull_put_spread, chain_for, iron_condor, long_call,
)


class Clock:
    def __init__(self, t: datetime = NOW):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def make(equity: float = 10_000.0, **kw):
    cfg = BrokerConfig(starting_equity=equity, db_path=":memory:", **kw)
    clock = Clock()
    return PaperBroker(cfg, clock=clock), clock


def test_implements_broker_protocol():
    b, _ = make()
    assert isinstance(b, Broker)


def test_debit_fill_price_and_limit_rejection():
    b, _ = make()
    idea = long_call()
    rej = b.submit(idea, 1, 2.04)
    assert rej.status == OrderStatus.REJECTED and rej.reason == "limit not marketable"
    assert b.positions() == []
    ok = b.submit(idea, 1, 2.05)
    assert ok.status == OrderStatus.FILLED
    assert ok.fill_price == pytest.approx(2.05)
    assert ok.fees == pytest.approx(0.65)


def test_credit_fill_semantics():
    b, _ = make()
    idea = bull_put_spread()  # mid -1.00, half-spread 0.20, fill -0.90
    r = b.submit(idea, 1, -1.00)  # insisting on 1.00 credit: not available
    assert r.status == OrderStatus.REJECTED and r.reason == "limit not marketable"
    r = b.submit(idea, 1, -0.95)
    assert r.status == OrderStatus.REJECTED
    r = b.submit(idea, 1, -0.91)
    assert r.status == OrderStatus.REJECTED
    r = b.submit(idea, 1, -0.90)
    assert r.status == OrderStatus.FILLED and r.fill_price == pytest.approx(-0.90)
    # a limit of -0.80 (willing to take less credit) fills at the modelled price, not the limit
    b2, _ = make()
    r = b2.submit(idea, 1, -0.80)
    assert r.fill_price == pytest.approx(-0.90)


def test_zero_slippage_equity_unchanged_except_fees():
    for idea in (long_call(), bull_call_spread(), bull_put_spread(), iron_condor()):
        b, _ = make(slippage_frac_of_half_spread=0.0)
        n_legs = len(idea.legs)
        o = b.submit(idea, 3, idea.net_price)
        assert o.status == OrderStatus.FILLED
        assert o.fees == pytest.approx(0.65 * 3 * n_legs)
        assert b.account().equity == pytest.approx(10_000 - o.fees)


def test_debit_roundtrip_exact_accounting():
    b, clock = make()
    idea = long_call()
    o = b.submit(idea, 2, 2.05)
    assert b.account().cash == pytest.approx(10_000 - 410 - 1.30)
    # equity right after fill: marked at mid 2.00 => lose slippage 10 and fees 1.30
    assert b.account().equity == pytest.approx(10_000 - 10 - 1.30)
    chain = chain_for(idea, [3.0])
    b.mark_to_market({"SPY": chain})
    pos = b.positions()[0]
    assert pos.mark == pytest.approx(3.0)
    assert pos.unrealized_pnl == pytest.approx((3.0 - 2.05) * 100 * 2)
    assert b.account().equity == pytest.approx(10_000 - 411.30 + 600)
    c = b.close(pos.id, 2.90, "take_profit")
    assert c.status == OrderStatus.FILLED and c.fill_price == pytest.approx(2.95)
    assert pos.exit_reason == "take_profit" and not pos.is_open
    assert pos.realized_pnl == pytest.approx((2.95 - 2.05) * 200 - 2.60)
    acct = b.account()
    assert acct.cash == pytest.approx(10_000 + pos.realized_pnl)
    assert acct.equity == pytest.approx(acct.cash)
    assert b.positions() == [] and len(b.positions(include_closed=True)) == 1
    assert o.position_id == pos.id


def test_credit_roundtrip_exact_accounting_and_collateral():
    b, _ = make()
    idea = bull_put_spread()
    b.submit(idea, 2, -0.90)
    fees = 0.65 * 2 * 2
    acct = b.account()
    assert acct.cash == pytest.approx(10_000 + 180 - fees)
    assert acct.buying_power == pytest.approx(10_000 + 180 - fees - 800)  # max_loss collateral held
    assert acct.equity == pytest.approx(10_000 - 20 - fees)
    assert acct.open_risk == pytest.approx(800)
    b.mark_to_market({"SPY": chain_for(idea, [0.9, 0.2])})  # mark = -0.9 + 0.2 = -0.70
    pos = b.positions()[0]
    assert pos.mark == pytest.approx(-0.70)
    assert pos.unrealized_pnl == pytest.approx(40.0)
    assert b.close(pos.id, -0.75, "x").status == OrderStatus.REJECTED  # exit would be -0.80 < -0.75
    assert pos.is_open
    c = b.close(pos.id, -0.85, "take_profit")
    assert c.status == OrderStatus.FILLED and c.fill_price == pytest.approx(-0.80)
    assert pos.realized_pnl == pytest.approx((-0.80 + 0.90) * 200 - 2 * fees)
    acct = b.account()
    assert acct.cash == pytest.approx(10_000 + pos.realized_pnl)
    assert acct.buying_power == pytest.approx(acct.cash)


def test_close_without_quotes_is_rejected_and_unknown_raises():
    b, _ = make()
    o = b.submit(bull_call_spread(), 1, 1.10)
    pos = b.positions()[0]
    r = b.close(pos.id, -100.0)
    assert r.status == OrderStatus.REJECTED and "no quote" in r.reason
    assert pos.is_open
    with pytest.raises(KeyError):
        b.close("nope", 0.0)
    b.mark_to_market({"SPY": chain_for(bull_call_spread(), [2.0, 1.0])})
    assert b.close(pos.id, 0.0).status == OrderStatus.FILLED
    assert b.close(pos.id, 0.0).status == OrderStatus.REJECTED  # already closed
    assert o.status == OrderStatus.FILLED


def test_insufficient_cash_rejected():
    b, _ = make(equity=300.0)
    r = b.submit(long_call(), 2, 2.05)
    assert r.status == OrderStatus.REJECTED and "insufficient cash" in r.reason
    b2, _ = make(equity=500.0)
    r = b2.submit(bull_put_spread(), 2, -0.90)  # collateral 800 > 500
    assert r.status == OrderStatus.REJECTED and "insufficient cash" in r.reason
    assert b2.account().cash == 500.0
    r = b2.submit(bull_put_spread(), 1, -0.90)  # collateral 400 + fees fits
    assert r.status == OrderStatus.FILLED


def test_collateral_reduces_buying_power_for_next_trade():
    b, _ = make(equity=1_000.0)
    assert b.submit(bull_put_spread(), 2, -0.90).status == OrderStatus.FILLED  # holds 800
    r = b.submit(bull_put_spread(), 1, -0.90)  # needs 400, bp ~ 1000+180-2.6-800 = 377
    assert r.status == OrderStatus.REJECTED


def test_expiry_settlement_debit_and_credit():
    b, _ = make()
    b.submit(long_call(), 1, 2.05)
    b.submit(bull_put_spread(), 1, -0.90)
    assert b.settle_expired({"SPY": 110.0}, EXPIRY - timedelta(days=1)) == []
    settled = b.settle_expired({"SPY": 110.0}, EXPIRY)
    assert len(settled) == 2 and all(p.exit_reason == "expired" for p in settled)
    by = {p.idea.strategy: p for p in settled}
    assert by["long_call"].exit_price == pytest.approx(10.0)
    assert by["long_call"].realized_pnl == pytest.approx((10 - 2.05) * 100 - 0.65)
    assert by["bull_put_spread"].exit_price == pytest.approx(0.0)
    assert by["bull_put_spread"].realized_pnl == pytest.approx(90 - 1.30)
    assert b.positions() == []
    expected_cash = 10_000 + by["long_call"].realized_pnl + by["bull_put_spread"].realized_pnl
    assert b.account().cash == pytest.approx(expected_cash)
    assert b.account().equity == pytest.approx(expected_cash)


def test_expiry_settlement_credit_spread_max_loss_and_condor():
    b, _ = make()
    b.submit(bull_put_spread(), 1, -0.90)
    b.submit(iron_condor(), 1, -1.80)
    # condor credit 2.00 mid, half spread 0.40 -> fill -1.80
    spots = {"SPY": 90.0}
    settled = {p.idea.strategy: p for p in b.settle_expired(spots, EXPIRY)}
    assert settled["bull_put_spread"].exit_price == pytest.approx(-5.0)
    assert settled["bull_put_spread"].realized_pnl == pytest.approx((-5.0 + 0.90) * 100 - 1.30)
    assert settled["iron_condor"].exit_price == pytest.approx(-5.0)
    assert settled["iron_condor"].realized_pnl == pytest.approx((-5.0 + 1.80) * 100 - 2.60)
    # missing spot leaves position open
    b3, _ = make()
    b3.submit(long_call(), 1, 2.05)
    assert b3.settle_expired({}, EXPIRY) == []
    assert len(b3.positions()) == 1


def test_restart_persistence(tmp_path):
    path = str(tmp_path / "sub" / "j.sqlite")
    cfg = BrokerConfig(db_path=path)
    clock = Clock()
    b = PaperBroker(cfg, clock=clock)
    idea = bull_put_spread()
    b.submit(idea, 2, -0.90)
    b.mark_to_market({"SPY": chain_for(idea, [0.9, 0.2])})
    before = b.account()
    pos_before = b.positions()[0]
    b.journal.close()

    b2 = PaperBroker(cfg, clock=clock)
    after = b2.account()
    assert after.cash == pytest.approx(before.cash)
    assert after.equity == pytest.approx(before.equity)
    assert after.day_pnl == pytest.approx(before.day_pnl)
    (pos,) = b2.positions()
    assert pos == pos_before
    assert b2.journal.trades_today(NOW) == 1
    # closing still works after another mark_to_market
    b2.mark_to_market({"SPY": chain_for(idea, [0.9, 0.2])})
    assert b2.close(pos.id, -0.9).status == OrderStatus.FILLED
    assert len(b2.journal.closed_positions()) == 1


def test_day_pnl_resets_each_ny_day():
    b, clock = make()
    b.submit(long_call(), 1, 2.05)
    a = b.account()
    assert a.day_pnl == pytest.approx(-5 - 0.65)
    clock.t = NOW + timedelta(days=1)
    assert b.account().day_pnl == pytest.approx(0.0)
    b.mark_to_market({"SPY": chain_for(long_call(), [3.0])})
    assert b.account().day_pnl == pytest.approx(100.0)  # 2.00 -> 3.00 marks, vs next-day start
    curve = b.journal.equity_curve()
    assert len(curve) >= 2


def test_rejects_zero_contracts_and_journals_orders():
    b, _ = make()
    assert b.submit(long_call(), 0, 5.0).status == OrderStatus.REJECTED
    assert len(b.journal.orders()) == 1

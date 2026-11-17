from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from kwanttrades.config import RiskConfig
from kwanttrades.execution.risk import RiskManager, is_intraday, uncovered_short_legs
from kwanttrades.types import NY, Leg, Right, Side

from tests.test_execution_factories import (
    EXPIRY, NOW, account, bull_call_spread, bull_put_spread, iron_condor, long_call, position_of, q,
)


def run(idea=None, contracts=1, acct=None, positions=(), now=NOW, trades=0, pdt=0, cfg=None):
    idea = idea or bull_call_spread()
    return RiskManager(cfg or RiskConfig()).check(idea, contracts, acct or account(), list(positions), now, trades, pdt)


def blocked(d, text):
    assert not d.passed
    assert any(text in r for r in d.reasons), d.reasons


def test_clean_pass_for_all_structures():
    big = account(100_000)
    rich_credit = replace(bull_put_spread(), net_price=-1.25, max_loss=375.0, max_profit=125.0)
    for idea in (long_call(), bull_call_spread(), rich_credit, iron_condor()):
        d = run(idea, 1, big)
        assert d.passed and d.reasons == [], (idea.strategy, d.reasons)


def test_per_trade_cap():
    assert run(contracts=1).passed  # 100 == 1% of 10k
    blocked(run(contracts=2), "per-trade cap")


def test_daily_loss_limit_message_and_boundary():
    d = run(acct=account(9_680, day_pnl=-320))
    blocked(d, "BLOCK: daily loss limit hit (-3.2% <= -3.0%)")
    blocked(run(acct=account(97_000, day_pnl=-3_000)), "daily loss limit hit (-3.0% <= -3.0%)")
    ok = run(acct=account(97_100, day_pnl=-2_900))  # -2.9%
    assert ok.passed, ok.reasons


def test_max_trades_per_day():
    assert run(trades=5).passed
    blocked(run(trades=6), "max trades per day")


def test_max_open_positions_and_per_symbol():
    others = [position_of(bull_call_spread(sym=s)) for s in ("AAA", "BBB", "CCC", "DDD", "EEE")]
    blocked(run(acct=account(100_000), positions=others), "max open positions")
    assert run(acct=account(100_000), positions=others[:4]).passed
    same = [position_of(bull_call_spread(sym="SPY"))]
    blocked(run(acct=account(100_000), positions=same), "per-symbol cap reached for SPY")
    closed = position_of(bull_call_spread(sym="SPY"))
    closed.closed = NOW
    assert run(acct=account(100_000), positions=[closed]).passed


def test_portfolio_risk_cap():
    heavy = replace(bull_call_spread(sym="QQQ"), max_loss=1_401.0)
    blocked(run(acct=account(10_000), positions=[position_of(heavy)]), "portfolio risk")
    light = replace(bull_call_spread(sym="QQQ"), max_loss=1_300.0)
    assert not any("portfolio risk" in r for r in run(acct=account(10_000), positions=[position_of(light)]).reasons)


def test_reward_risk_and_credit_width():
    poor = replace(bull_call_spread(), max_profit=50.0)
    blocked(run(poor), "reward:risk")
    assert run(replace(bull_call_spread(), max_profit=100.0)).passed
    assert run(long_call(), 1, account(100_000)).passed  # unbounded profit
    thin = replace(bull_put_spread(), net_price=-0.2, max_loss=480.0, max_profit=20.0)
    blocked(run(thin, 1, account(100_000)), "credit/width")
    ok = replace(bull_put_spread(), net_price=-1.25, max_loss=375.0, max_profit=125.0)
    assert run(ok, 1, account(100_000)).passed


@pytest.mark.parametrize("hh,mm,ok", [(9, 44, False), (9, 45, True), (15, 30, True), (15, 31, False), (11, 0, True)])
def test_entry_window(hh, mm, ok):
    d = run(now=datetime(2026, 12, 1, hh, mm, tzinfo=NY))
    assert d.passed is ok
    if not ok:
        blocked(d, "outside entry window")


def test_buying_power():
    blocked(run(acct=account(10_000, bp=99.0)), "insufficient buying power")
    assert run(acct=account(10_000, bp=100.0)).passed
    blocked(run(bull_put_spread(), 1, account(100_000, bp=399.0)), "insufficient buying power")  # collateral 400


def test_defined_risk():
    naked = replace(long_call(), legs=[Leg(q(100, Right.CALL, 2.0), Side.SELL)], net_price=-2.0, max_loss=1.0, max_profit=200.0)
    blocked(run(naked, 1, account(100_000)), "undefined risk")
    assert uncovered_short_legs(naked) == [f"C {EXPIRY.isoformat()}"]
    cross = replace(bull_call_spread(), legs=[
        Leg(q(100, Right.PUT, 2.0), Side.BUY), Leg(q(105, Right.CALL, 1.0), Side.SELL)])
    blocked(run(cross), "undefined risk")  # long put does not cover short call
    other_exp = replace(bull_call_spread(), legs=[
        Leg(q(100, Right.CALL, 2.0, expiry=EXPIRY.replace(month=12, day=31)), Side.BUY), Leg(q(105, Right.CALL, 1.0), Side.SELL)])
    blocked(run(other_exp), "undefined risk")  # calendar short leg uncovered by same expiry
    assert uncovered_short_legs(iron_condor()) == [] and uncovered_short_legs(long_call()) == []
    off = RiskConfig(defined_risk_only=False)
    assert not any("undefined" in r for r in run(naked, 1, account(100_000), cfg=off).reasons)


def test_pdt_guard():
    today_idea = bull_call_spread(expiry=NOW.date())
    blocked(run(today_idea, pdt=3), "PDT guard")
    assert run(today_idea, pdt=2).passed
    assert run(bull_call_spread(), pdt=3).passed  # not expiring today, not intraday
    tagged = replace(bull_call_spread(), rationale=["Intraday momentum"])
    blocked(run(tagged, pdt=4), "PDT guard")
    assert run(today_idea, acct=account(30_000), pdt=3).passed
    assert run(today_idea, pdt=3, cfg=RiskConfig(pdt_guard=False)).passed


def test_is_intraday():
    assert is_intraday(replace(long_call(), strategy="intraday_orb_call"))
    assert is_intraday(replace(long_call(), rationale=["x", "INTRADAY breakout"]))
    assert not is_intraday(long_call())


def test_returns_all_failing_reasons():
    d = run(contracts=1, acct=account(10_000, bp=0.0, day_pnl=-400), trades=9, now=datetime(2026, 12, 1, 8, 0, tzinfo=NY))
    assert not d.passed and len(d.reasons) >= 4
    assert all(r.startswith("BLOCK:") for r in d.reasons)

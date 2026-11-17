from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from kwanttrades.config import RiskConfig
from kwanttrades.execution.exits import evaluate_exit, structure_mark
from kwanttrades.types import NY, DirectionalSignal, OptionChain

from tests.test_execution_factories import (
    EXPIRY, NOW, bull_call_spread, bull_put_spread, chain_for, iron_condor, long_call, position_of,
)

CFG = RiskConfig()


def ev(idea, mids, now=NOW, direction=None, entry=None, kill=False, contracts=1):
    pos = position_of(idea, contracts, entry)
    chain = chain_for(idea, mids, asof=now)
    return evaluate_exit(pos, chain, direction, now, CFG, kill_switch=kill)


def test_structure_mark_sign_convention():
    spread = bull_call_spread()
    assert structure_mark(position_of(spread), chain_for(spread, [3.0, 1.5])) == pytest.approx(1.5)
    credit = bull_put_spread()
    assert structure_mark(position_of(credit), chain_for(credit, [0.9, 0.2])) == pytest.approx(-0.7)
    partial = OptionChain("SPY", 100.0, NOW, chain_for(spread, [3.0, 1.5]).quotes[:1])
    assert structure_mark(position_of(spread), partial) is None


def test_hold():
    assert ev(bull_call_spread(), [2.0, 1.0]) is None


def test_stop_loss_and_take_profit_debit_spread():
    s = bull_call_spread()  # entry 1.00, max loss 100, max profit 400
    assert ev(s, [1.7, 1.1]) == "stop_loss"  # mark 0.60 -> loss 40
    # mark 0.66 -> loss 34 < 35 hold, mark 0.65 -> loss 35 -> stop
    assert ev(s, [1.76, 1.10]) is None
    assert ev(s, [1.75, 1.10]) == "stop_loss"
    assert ev(s, [4.20, 1.20]) == "take_profit"  # mark 3.00 -> gain 200
    assert ev(s, [4.10, 1.20]) is None  # mark 2.90


def test_take_profit_unbounded_uses_debit():
    c = long_call()  # entry 2.00, tp 50% of debit => mark 3.00
    assert ev(c, [3.0]) == "take_profit"
    assert ev(c, [2.9]) is None
    assert ev(c, [1.3]) == "stop_loss"  # loss 70 >= .35*200


def test_credit_spread_exits():
    cs = bull_put_spread()  # entry -1.00, max loss 400, max profit 100
    assert ev(cs, [0.8, 0.3]) == "take_profit"  # mark -0.5 -> gain 50
    assert ev(cs, [0.9, 0.3]) is None  # mark -0.6
    assert ev(cs, [1.9, 0.5]) is None  # mark -1.4 -> loss 40 < 140
    assert ev(cs, [2.6, 0.2]) == "stop_loss"  # mark -2.4 -> loss 140 == .35*400


def test_time_flatten_expiring_today_and_intraday():
    s = bull_call_spread(expiry=NOW.date())
    late = datetime(2026, 12, 1, 15, 55, tzinfo=NY)
    assert ev(s, [2.0, 1.0], now=late) == "time_flatten"
    assert ev(s, [2.0, 1.0], now=datetime(2026, 12, 1, 15, 54, tzinfo=NY)) is None
    tagged = replace(long_call(), rationale=["intraday ORB"])
    assert ev(tagged, [2.0], now=late) == "time_flatten"
    assert ev(long_call(), [2.0], now=late) is None


def test_dte_exit_for_credit_structures_only():
    cs = bull_put_spread()
    day_before = datetime.combine(EXPIRY - timedelta(days=1), NOW.time(), tzinfo=NY)
    assert ev(cs, [1.4, 0.4], now=day_before) == "dte_exit"  # mark -1.0 flat
    two = datetime.combine(EXPIRY - timedelta(days=2), NOW.time(), tzinfo=NY)
    assert ev(cs, [1.4, 0.4], now=two) is None
    assert ev(iron_condor(), [1.5, 0.5, 1.5, 0.5], now=day_before) == "dte_exit"
    assert ev(bull_call_spread(), [2.0, 1.0], now=day_before) is None  # debit: no dte exit


def test_signal_invalidated():
    c = long_call()
    bear = DirectionalSignal("SPY", "composite", -0.4, 0.6)
    bull = DirectionalSignal("SPY", "composite", 0.4, 0.6)
    failed = DirectionalSignal("SPY", "orb", 0.3, 0.5, {"state": "failed", "failed": True})
    assert ev(c, [2.0], direction=bear) == "signal_invalidated"
    assert ev(c, [2.0], direction=bull) is None
    assert ev(c, [2.0], direction=failed) == "signal_invalidated"
    nested = DirectionalSignal("SPY", "composite", 0.3, 0.5, {"orb": {"state": "failed"}})
    assert ev(c, [2.0], direction=nested) == "signal_invalidated"
    put = replace(c, strategy="long_put")
    assert ev(put, [2.0], direction=bull) == "signal_invalidated"
    # non-directional structure ignores direction
    assert ev(iron_condor(), [1.5, 0.5, 1.5, 0.5], direction=bear) is None
    assert ev(c, [2.0], direction=None) is None


def test_priority_order():
    late = datetime(2026, 12, 1, 15, 56, tzinfo=NY)
    bear = DirectionalSignal("SPY", "composite", -1.0, 1.0)
    s = bull_call_spread(expiry=NOW.date())
    # everything firing: stop loss + time flatten + signal flip + kill switch
    assert ev(s, [1.0, 0.9], now=late, direction=bear, kill=True) == "kill_switch"
    assert ev(s, [1.0, 0.9], now=late, direction=bear) == "time_flatten"
    assert ev(s, [1.0, 0.9], now=NOW, direction=bear) == "stop_loss"
    # take profit beats signal flip
    assert ev(s, [4.2, 1.2], now=NOW, direction=bear) == "take_profit"
    # signal flip beats dte exit
    cs = bull_put_spread()
    day_before = datetime.combine(EXPIRY - timedelta(days=1), NOW.time(), tzinfo=NY)
    assert ev(cs, [1.4, 0.4], now=day_before, direction=bear) == "signal_invalidated"
    assert ev(cs, [1.4, 0.4], now=day_before) == "dte_exit"


def test_falls_back_to_position_mark_without_chain():
    pos = position_of(bull_call_spread(), mark=0.5)
    assert evaluate_exit(pos, None, None, NOW, CFG) == "stop_loss"
    pos.mark = None
    assert evaluate_exit(pos, None, None, NOW, CFG) is None

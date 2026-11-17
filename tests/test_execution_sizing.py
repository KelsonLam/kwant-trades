from __future__ import annotations

import math
from dataclasses import replace

import pytest

from kwanttrades.config import RiskConfig
from kwanttrades.execution.sizing import kelly_fraction_full, size_position

from tests.test_execution_factories import account, bull_call_spread, bull_put_spread, long_call


def test_risk_budget_is_binding_by_default():
    # max_loss 100, 1% of 10k = 100 -> 1 contract (kelly would allow 9)
    assert size_position(bull_call_spread(), account(10_000), RiskConfig()) == 1
    assert size_position(bull_call_spread(), account(50_000), RiskConfig()) == 5


def test_risk_multiplier_scales_budget():
    cfg = RiskConfig(max_loss_per_trade_pct=0.10)
    full = size_position(bull_call_spread(), account(10_000), cfg, 1.0)
    half = size_position(bull_call_spread(), account(10_000), cfg, 0.5)
    assert half < full
    assert size_position(bull_call_spread(), account(10_000), cfg, 0.0) == 0


def test_kelly_is_binding():
    cfg = RiskConfig(max_loss_per_trade_pct=0.5, max_contracts_per_trade=100)
    # p=.5, b=4 -> f=.375, *.25 = .09375 -> 9.375 contracts of $100 on $10k
    assert size_position(bull_call_spread(), account(10_000), cfg) == 9


def test_max_contracts_binding():
    cfg = RiskConfig(max_loss_per_trade_pct=0.5, kelly_fraction=1.0, max_contracts_per_trade=3)
    assert size_position(bull_call_spread(), account(10_000), cfg) == 3


def test_no_edge_means_zero():
    idea = replace(bull_call_spread(), pop=0.15)  # p - (1-p)/b < 0
    assert kelly_fraction_full(idea) == 0.0
    cfg = RiskConfig(max_loss_per_trade_pct=0.5)
    assert size_position(idea, account(10_000), cfg) == 0


def test_kelly_b_capped_at_ten():
    idea = replace(bull_call_spread(), max_profit=5_000.0, pop=0.3)
    assert kelly_fraction_full(idea) == pytest.approx(0.3 - 0.7 / 10.0)


def test_unbounded_profit_uses_ev_consistent_b():
    idea = long_call()  # p=.4, ev=50, L=200
    b = (50 + 0.6 * 200) / (0.4 * 200)
    assert math.isfinite(b)
    assert kelly_fraction_full(idea) == pytest.approx(0.4 - 0.6 / b)
    cfg = RiskConfig(max_loss_per_trade_pct=1.0, kelly_fraction=0.5, max_contracts_per_trade=1000)
    expected = math.floor((0.4 - 0.6 / b) * 0.5 * 10_000 / 200)
    assert size_position(idea, account(10_000), cfg) == expected


def test_not_affordable_is_zero():
    cfg = RiskConfig(max_loss_per_trade_pct=0.5, kelly_fraction=1.0)
    assert size_position(bull_call_spread(), account(10_000, bp=50.0), cfg) == 0
    assert size_position(bull_put_spread(), account(10_000, bp=399.0), cfg) == 0  # collateral 400
    assert size_position(bull_call_spread(), account(0.0), cfg) == 0
    assert size_position(replace(bull_call_spread(), max_loss=0.0), account(10_000), cfg) == 0

"""Position sizing: risk budget, fractional Kelly and a hard contract cap."""
from __future__ import annotations

import math

from kwanttrades.config import RiskConfig
from kwanttrades.types import MULTIPLIER, AccountState, TradeIdea

_EPS = 1e-9


def kelly_fraction_full(idea: TradeIdea) -> float:
    """Full Kelly fraction of capital-at-risk using a two-outcome approximation."""
    p = min(max(idea.pop, 0.0), 1.0)
    loss = idea.max_loss
    if p <= 0 or loss <= 0:
        return 0.0
    if math.isinf(idea.max_profit):
        b = (idea.expected_value + (1.0 - p) * loss) / (p * loss)
    else:
        b = idea.max_profit / loss
    b = min(b, 10.0)
    if b <= 0:
        return 0.0
    return max(0.0, p - (1.0 - p) / b)


def _required_cash_per_set(idea: TradeIdea) -> float:
    if idea.net_price >= 0:
        return idea.net_price * MULTIPLIER
    return idea.max_loss  # collateral for credit structures


def size_position(idea: TradeIdea, account: AccountState, cfg: RiskConfig, risk_multiplier: float = 1.0) -> int:
    """Contracts to trade: min(risk budget, Kelly, max_contracts_per_trade, affordability); 0 if any is 0."""
    loss = idea.max_loss
    if loss <= 0 or account.equity <= 0 or risk_multiplier <= 0:
        return 0
    budget = math.floor(account.equity * cfg.max_loss_per_trade_pct * risk_multiplier / loss + _EPS)
    f = kelly_fraction_full(idea) * cfg.kelly_fraction
    kelly = math.floor(f * account.equity / loss + _EPS)
    caps = [budget, kelly, int(cfg.max_contracts_per_trade)]
    need = _required_cash_per_set(idea)
    if need > 0:
        caps.append(math.floor(max(account.buying_power, 0.0) / need + _EPS))
    return max(0, min(caps))

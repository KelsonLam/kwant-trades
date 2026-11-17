"""Exit rules for open positions."""
from __future__ import annotations

import math
from datetime import datetime, time

from kwanttrades.config import RiskConfig
from kwanttrades.types import MULTIPLIER, NY, DirectionalSignal, OptionChain, Position, Side, TradeIdea

_BULLISH = {"long_call", "bull_call_spread", "bull_put_spread"}
_BEARISH = {"long_put", "bear_put_spread", "bear_call_spread"}


def parse_hhmm(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def is_intraday(idea: TradeIdea) -> bool:
    """True if the strategy name or any rationale line is tagged 'intraday'."""
    if "intraday" in idea.strategy.lower():
        return True
    return any("intraday" in str(r).lower() for r in idea.rationale)


def idea_direction(idea: TradeIdea) -> int:
    """+1 bullish, -1 bearish, 0 non-directional."""
    if idea.strategy in _BULLISH:
        return 1
    if idea.strategy in _BEARISH:
        return -1
    return 0


def structure_mark(position: Position, chain: OptionChain) -> float | None:
    """Mid mark per share of the position's structure: sum(+mid BUY, -mid SELL) * ratio.
    Same sign convention as entry (credit structures are negative). None if a leg is missing."""
    total = 0.0
    for leg in position.idea.legs:
        q = chain.find(leg.quote.expiry, leg.quote.strike, leg.quote.right)
        if q is None:
            return None
        sign = 1.0 if leg.side == Side.BUY else -1.0
        total += sign * q.mid * leg.ratio
    return total


def _failed(detail: dict) -> bool:
    if not isinstance(detail, dict):
        return False
    if detail.get("failed") is True or detail.get("state") == "failed":
        return True
    for v in detail.values():
        if isinstance(v, dict):
            if v.get("failed") is True or v.get("state") == "failed":
                return True
            inner = v.get("detail")
            if isinstance(inner, dict) and (inner.get("failed") is True or inner.get("state") == "failed"):
                return True
    return False


def evaluate_exit(
    position: Position,
    chain: OptionChain | None,
    direction: DirectionalSignal | None,
    now: datetime,
    cfg: RiskConfig,
    kill_switch: bool = False,
) -> str | None:
    """Return the exit reason, or None to hold.

    Priority: kill_switch, time_flatten, stop_loss, take_profit, signal_invalidated, dte_exit.
    """
    idea = position.idea
    now = now.astimezone(NY)
    if kill_switch:
        return "kill_switch"

    expires_today = idea.expiry <= now.date()
    if (expires_today or is_intraday(idea)) and now.time() >= parse_hhmm(cfg.flatten_time):
        return "time_flatten"

    mark = structure_mark(position, chain) if chain is not None else None
    if mark is None:
        mark = position.mark
    if mark is not None:
        pnl_set = (mark - position.entry_price) * MULTIPLIER  # $ per contract-set
        eps = 1e-9
        if idea.max_loss > 0 and -pnl_set >= idea.stop_loss_pct * idea.max_loss - eps:
            return "stop_loss"
        if math.isinf(idea.max_profit):
            debit = max(position.entry_price, 0.0) * MULTIPLIER
            target = idea.take_profit_pct * debit
        else:
            target = idea.take_profit_pct * idea.max_profit
        if target > 0 and pnl_set >= target - eps:
            return "take_profit"

    if direction is not None:
        d = idea_direction(idea)
        if d != 0 and (direction.direction * d < 0 or _failed(direction.detail)):
            return "signal_invalidated"

    if idea.is_credit and (idea.expiry - now.date()).days <= 1:
        return "dte_exit"
    return None

"""Pre-trade risk gate. `check` returns every failing rule, not just the first."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from kwanttrades.config import RiskConfig
from kwanttrades.execution.exits import is_intraday, parse_hhmm
from kwanttrades.types import MULTIPLIER, NY, AccountState, Position, Side, TradeIdea

__all__ = ["RiskDecision", "RiskManager", "is_intraday", "uncovered_short_legs"]

PDT_EQUITY = 25_000.0


@dataclass
class RiskDecision:
    passed: bool
    reasons: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return "PASS" if self.passed else "; ".join(self.reasons)


def uncovered_short_legs(idea: TradeIdea) -> list[str]:
    """Descriptions of (right, expiry) groups where SELL quantity exceeds BUY quantity."""
    buys: dict = defaultdict(int)
    sells: dict = defaultdict(int)
    for l in idea.legs:
        key = (l.quote.right, l.quote.expiry)
        (buys if l.side == Side.BUY else sells)[key] += l.ratio
    out = []
    for key, qty in sells.items():
        if qty > buys.get(key, 0):
            out.append(f"{key[0].value} {key[1].isoformat()}")
    return out


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def check(
        self,
        idea: TradeIdea,
        contracts: int,
        account: AccountState,
        positions: list[Position],
        now: datetime,
        trades_today: int,
        day_trades_5d: int = 0,
    ) -> RiskDecision:
        cfg = self.cfg
        now = now.astimezone(NY)
        reasons: list[str] = []
        open_pos = [p for p in positions if p.is_open]
        equity = account.equity
        eps = 1e-9

        if contracts < 1:
            reasons.append(f"BLOCK: contracts must be >= 1 (got {contracts})")

        trade_risk = idea.max_loss * max(contracts, 0)

        # per-trade loss cap
        cap = equity * cfg.max_loss_per_trade_pct
        if trade_risk > cap + eps:
            reasons.append(
                f"BLOCK: trade risk ${trade_risk:,.2f} exceeds per-trade cap ${cap:,.2f} "
                f"({cfg.max_loss_per_trade_pct:.1%} of equity)"
            )

        # daily loss limit / kill switch
        start_equity = equity - account.day_pnl
        if start_equity > 0:
            day_pct = account.day_pnl / start_equity
            if day_pct <= -cfg.daily_loss_limit_pct + eps:
                reasons.append(
                    f"BLOCK: daily loss limit hit ({day_pct:.1%} <= -{cfg.daily_loss_limit_pct:.1%})"
                )

        if trades_today >= cfg.max_trades_per_day:
            reasons.append(f"BLOCK: max trades per day reached ({trades_today} >= {cfg.max_trades_per_day})")

        if len(open_pos) >= cfg.max_open_positions:
            reasons.append(f"BLOCK: max open positions reached ({len(open_pos)} >= {cfg.max_open_positions})")

        same = sum(1 for p in open_pos if p.idea.symbol == idea.symbol)
        if same >= cfg.max_positions_per_symbol:
            reasons.append(
                f"BLOCK: per-symbol cap reached for {idea.symbol} ({same} >= {cfg.max_positions_per_symbol})"
            )

        # portfolio risk
        open_risk = sum(p.capital_at_risk for p in open_pos)
        total_risk = open_risk + trade_risk
        pcap = equity * cfg.max_portfolio_risk_pct
        if total_risk > pcap + eps:
            reasons.append(
                f"BLOCK: portfolio risk ${total_risk:,.2f} exceeds cap ${pcap:,.2f} "
                f"({cfg.max_portfolio_risk_pct:.1%} of equity)"
            )

        # reward:risk or credit:width
        if idea.is_credit:
            credit = -idea.net_price
            width = idea.max_loss / MULTIPLIER + credit
            ratio = credit / width if width > 0 else 0.0
            if ratio < cfg.min_credit_to_width - eps:
                reasons.append(
                    f"BLOCK: credit/width {ratio:.2f} below minimum {cfg.min_credit_to_width:.2f}"
                )
        else:
            rr = math.inf if math.isinf(idea.max_profit) else (idea.max_profit / idea.max_loss if idea.max_loss > 0 else math.inf)
            if rr < cfg.min_reward_risk - eps:
                reasons.append(f"BLOCK: reward:risk {rr:.2f} below minimum {cfg.min_reward_risk:.2f}")

        # entry window
        start, end = parse_hhmm(cfg.entry_window_start), parse_hhmm(cfg.entry_window_end)
        if not (start <= now.time().replace(second=0, microsecond=0) <= end):
            reasons.append(
                f"BLOCK: outside entry window ({now:%H:%M} not in {cfg.entry_window_start}-{cfg.entry_window_end} NY)"
            )

        # cash / buying power
        need = max(idea.net_price, 0.0) * MULTIPLIER * max(contracts, 0) if not idea.is_credit else idea.max_loss * max(contracts, 0)
        if need > account.buying_power + eps:
            reasons.append(
                f"BLOCK: insufficient buying power (${need:,.2f} needed, ${account.buying_power:,.2f} available)"
            )

        # defined risk
        if cfg.defined_risk_only:
            bad = uncovered_short_legs(idea)
            if bad:
                reasons.append("BLOCK: undefined risk, short leg not covered by a long leg of the same right and expiry (" + ", ".join(bad) + ")")

        # pattern day trader guard
        if cfg.pdt_guard and equity < PDT_EQUITY and day_trades_5d >= 3:
            if idea.expiry == now.date() or is_intraday(idea):
                reasons.append(
                    f"BLOCK: PDT guard (equity ${equity:,.0f} < $25,000 and {day_trades_5d} day trades in 5 sessions)"
                )

        return RiskDecision(passed=not reasons, reasons=reasons)

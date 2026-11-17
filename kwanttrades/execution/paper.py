"""Built-in paper broker with a conservative fill model and journal-backed persistence.

Conventions: structure value per share V = sum(+mid BUY, -mid SELL) * ratio. Opening pays V
(negative V = credit received). Closing receives V. Limit prices use the TradeIdea.net_price
sign convention for both directions:
  open  fills iff limit_price >= fill_price   (fill = V + slip * half_spread, always worse for us)
  close fills iff limit_price <= exit_price   (exit = V - slip * half_spread, always worse for us)
so a close limit is the minimum structure value we accept receiving (float("-inf") = market).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Callable

from kwanttrades.config import BrokerConfig
from kwanttrades.execution.exits import structure_mark
from kwanttrades.execution.journal import Journal
from kwanttrades.types import (
    MULTIPLIER,
    NY,
    AccountState,
    Leg,
    OptionChain,
    OptionQuote,
    Order,
    OrderStatus,
    Position,
    Right,
    Side,
    TradeIdea,
    now_ny,
)

_TOL = 1e-9


def _sign(leg: Leg) -> float:
    return 1.0 if leg.side == Side.BUY else -1.0


class PaperBroker:
    def __init__(self, cfg: BrokerConfig, journal: Journal | None = None, clock: Callable[[], datetime] | None = None):
        self.cfg = cfg
        self.journal = journal if journal is not None else Journal(cfg.db_path)
        self._clock = clock or now_ny
        self._quotes: dict[str, OptionQuote] = {}
        self._positions: dict[str, Position] = {p.id: p for p in self.journal.positions(include_closed=True)}
        stored = self.journal.get_float("cash")
        self._cash = stored if stored is not None else float(cfg.starting_equity)
        if stored is None:
            self.journal.set_value("cash", self._cash)
        self._ensure_day_snapshot()

    # ------------------------------------------------------------------ helpers
    def _now(self) -> datetime:
        return self._clock().astimezone(NY)

    def _set_cash(self, value: float) -> None:
        self._cash = value
        self.journal.set_value("cash", value)

    def _open(self) -> list[Position]:
        return [p for p in self._positions.values() if p.is_open]

    def _collateral(self) -> float:
        return sum(p.idea.max_loss * p.contracts for p in self._open() if p.idea.is_credit)

    def _equity(self) -> float:
        total = self._cash
        for p in self._open():
            v = p.mark if p.mark is not None else p.entry_price
            total += v * MULTIPLIER * p.contracts
        return total

    def _account_now(self, day_pnl: float) -> AccountState:
        equity = self._equity()
        return AccountState(
            equity=equity,
            cash=self._cash,
            buying_power=self._cash - self._collateral(),
            day_pnl=day_pnl,
            open_risk=sum(p.capital_at_risk for p in self._open()),
        )

    def _ensure_day_snapshot(self) -> float:
        """Journal an equity snapshot at first use each NY day; return the day's starting equity."""
        now = self._now()
        start = self.journal.day_start_equity(now.date())
        if start is None:
            acct = self._account_now(0.0)
            self.journal.record_equity(acct, now)
            start = acct.equity
        return start

    def _leg_quote(self, leg: Leg) -> OptionQuote:
        return self._quotes.get(leg.quote.occ_symbol, leg.quote)

    @staticmethod
    def _fees_for(cfg: BrokerConfig, idea: TradeIdea, contracts: int) -> float:
        return cfg.commission_per_contract * contracts * sum(l.ratio for l in idea.legs)

    @staticmethod
    def _value_and_half_spread(legs: list[tuple[Leg, OptionQuote]]) -> tuple[float, float]:
        v = sum(_sign(l) * q.mid * l.ratio for l, q in legs)
        hs = sum(0.5 * q.spread * l.ratio for l, q in legs)
        return v, hs

    def _new_order(self, idea: TradeIdea, contracts: int, limit: float, action: str, position_id: str | None) -> Order:
        return Order(
            id="ord-" + uuid.uuid4().hex[:12], idea=idea, contracts=contracts, limit_price=limit,
            action=action, submitted=self._now(), position_id=position_id,
        )

    def _reject(self, order: Order, reason: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.reason = reason
        self.journal.save_order(order)
        return order

    # ------------------------------------------------------------------ Broker API
    def account(self) -> AccountState:
        start = self._ensure_day_snapshot()
        return self._account_now(self._equity() - start)

    def positions(self, include_closed: bool = False) -> list[Position]:
        ps = list(self._positions.values()) if include_closed else self._open()
        return sorted(ps, key=lambda p: p.opened)

    def mark_to_market(self, chains: dict[str, OptionChain]) -> None:
        for chain in chains.values():
            for q in chain.quotes:
                self._quotes[q.occ_symbol] = q
        for p in self._open():
            chain = chains.get(p.idea.symbol)
            if chain is None:
                continue
            m = structure_mark(p, chain)
            if m is not None:
                p.mark = m
                self.journal.save_position(p)

    def submit(self, idea: TradeIdea, contracts: int, limit_price: float) -> Order:
        order = self._new_order(idea, contracts, limit_price, "open", None)
        if contracts < 1:
            return self._reject(order, "contracts must be >= 1")
        legs = [(l, self._leg_quote(l)) for l in idea.legs]
        mid, hs = self._value_and_half_spread(legs)
        fill = mid + self.cfg.slippage_frac_of_half_spread * hs
        if limit_price < fill - _TOL:
            return self._reject(order, "limit not marketable")
        fees = self._fees_for(self.cfg, idea, contracts)
        credit = fill < 0 or idea.is_credit
        if credit:
            required = idea.max_loss * contracts + fees
        else:
            required = fill * MULTIPLIER * contracts + fees
        available = self._cash - self._collateral()
        if required > available + _TOL:
            return self._reject(order, f"insufficient cash (need {required:.2f}, have {available:.2f})")

        now = self._now()
        self._ensure_day_snapshot()
        pos = Position(
            id="pos-" + uuid.uuid4().hex[:12], idea=idea, contracts=contracts, entry_price=fill,
            opened=now, mark=mid, fees=fees,
        )
        self._positions[pos.id] = pos
        self._set_cash(self._cash - fill * MULTIPLIER * contracts - fees)
        order.status = OrderStatus.FILLED
        order.fill_price = fill
        order.fees = fees
        order.filled = now
        order.position_id = pos.id
        self.journal.save_position(pos)
        self.journal.save_order(order)
        return order

    def close(self, position_id: str, limit_price: float, reason: str = "") -> Order:
        pos = self._positions.get(position_id)
        if pos is None:
            raise KeyError(f"unknown position {position_id}")
        order = self._new_order(pos.idea, pos.contracts, limit_price, "close", position_id)
        order.reason = reason
        if not pos.is_open:
            return self._reject(order, "position not open")
        legs = []
        for l in pos.idea.legs:
            q = self._quotes.get(l.quote.occ_symbol)
            if q is None:
                return self._reject(order, f"no quote for {l.quote.occ_symbol}")
            legs.append((l, q))
        mid, hs = self._value_and_half_spread(legs)
        exit_price = mid - self.cfg.slippage_frac_of_half_spread * hs
        if limit_price > exit_price + _TOL:
            return self._reject(order, "limit not marketable")
        fees = self._fees_for(self.cfg, pos.idea, pos.contracts)
        self._finish_close(pos, exit_price, fees, reason, self._now())
        order.status = OrderStatus.FILLED
        order.fill_price = exit_price
        order.fees = fees
        order.filled = pos.closed
        self.journal.save_order(order)
        return order

    def _finish_close(self, pos: Position, exit_price: float, fees: float, reason: str, when: datetime) -> None:
        self._ensure_day_snapshot()
        pos.fees += fees
        pos.exit_price = exit_price
        pos.exit_reason = reason
        pos.closed = when
        pos.mark = exit_price
        pos.realized_pnl = (exit_price - pos.entry_price) * MULTIPLIER * pos.contracts - pos.fees
        self._set_cash(self._cash + exit_price * MULTIPLIER * pos.contracts - fees)
        self.journal.save_position(pos)
        eq = self._equity()
        start = self.journal.day_start_equity(when.date())
        self.journal.record_equity(self._account_now(eq - (eq if start is None else start)), when)

    def settle_expired(self, spot_by_symbol: dict[str, float], today: date) -> list[Position]:
        """Settle positions whose earliest expiry is <= `today` at intrinsic value (no fees).
        Call after the 16:00 close. Positions whose underlying has no spot are left open."""
        settled = []
        for pos in list(self._open()):
            if pos.idea.expiry > today:
                continue
            spot = spot_by_symbol.get(pos.idea.symbol)
            if spot is None:
                continue
            value = 0.0
            for l in pos.idea.legs:
                q = l.quote
                if q.expiry <= today:
                    intrinsic = max(spot - q.strike, 0.0) if q.right == Right.CALL else max(q.strike - spot, 0.0)
                    value += _sign(l) * intrinsic * l.ratio
                else:  # later-dated leg: use its latest mid
                    value += _sign(l) * self._leg_quote(l).mid * l.ratio
            when = datetime.combine(today, datetime.min.time().replace(hour=16), tzinfo=NY)
            self._finish_close(pos, value, 0.0, "expired", when)
            settled.append(pos)
        return settled

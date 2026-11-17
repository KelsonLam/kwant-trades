"""Shared factories for the execution tests (no tests in this module)."""
from __future__ import annotations

from datetime import date, datetime

from kwanttrades.types import (
    NY,
    AccountState,
    Leg,
    OptionChain,
    OptionQuote,
    Position,
    Right,
    Side,
    TradeIdea,
)

EXPIRY = date(2026, 12, 18)
NOW = datetime(2026, 12, 1, 10, 30, tzinfo=NY)  # Tuesday
INF = float("inf")


def q(strike: float, right: Right, mid: float, half: float = 0.10, expiry: date = EXPIRY, sym: str = "SPY") -> OptionQuote:
    return OptionQuote(sym, expiry, strike, right, bid=round(mid - half, 4), ask=round(mid + half, 4))


def _idea(strategy, legs, net, max_loss, max_profit, pop=0.5, ev=20.0, **kw) -> TradeIdea:
    symbol = legs[0].quote.underlying
    return TradeIdea(
        symbol=symbol, strategy=strategy, legs=legs, net_price=net, entry_limit=net, max_loss=max_loss,
        max_profit=max_profit, pop=pop, expected_value=ev, score=0.1, created=NOW, **kw,
    )


def long_call(expiry: date = EXPIRY, sym: str = "SPY") -> TradeIdea:
    """Buy 100C at mid 2.00 (half-spread 0.10)."""
    return _idea("long_call", [Leg(q(100, Right.CALL, 2.0, expiry=expiry, sym=sym), Side.BUY)], 2.0, 200.0, INF, pop=0.4, ev=50.0)


def bull_call_spread(expiry: date = EXPIRY, sym: str = "SPY") -> TradeIdea:
    """Buy 100C (2.00), sell 105C (1.00): debit 1.00, half-spread 0.20, max loss 100, max profit 400."""
    legs = [Leg(q(100, Right.CALL, 2.0, expiry=expiry, sym=sym), Side.BUY), Leg(q(105, Right.CALL, 1.0, expiry=expiry, sym=sym), Side.SELL)]
    return _idea("bull_call_spread", legs, 1.0, 100.0, 400.0)


def bull_put_spread(expiry: date = EXPIRY, sym: str = "SPY") -> TradeIdea:
    """Sell 100P (1.50), buy 95P (0.50): credit 1.00, half-spread 0.20, max loss 400, max profit 100."""
    legs = [Leg(q(100, Right.PUT, 1.5, expiry=expiry, sym=sym), Side.SELL), Leg(q(95, Right.PUT, 0.5, expiry=expiry, sym=sym), Side.BUY)]
    return _idea("bull_put_spread", legs, -1.0, 400.0, 100.0, pop=0.7)


def iron_condor(expiry: date = EXPIRY, sym: str = "SPY") -> TradeIdea:
    """Bull put 100/95 plus bear call 110/115: credit 2.00, max loss 300, max profit 200."""
    legs = [
        Leg(q(100, Right.PUT, 1.5, expiry=expiry, sym=sym), Side.SELL), Leg(q(95, Right.PUT, 0.5, expiry=expiry, sym=sym), Side.BUY),
        Leg(q(110, Right.CALL, 1.5, expiry=expiry, sym=sym), Side.SELL), Leg(q(115, Right.CALL, 0.5, expiry=expiry, sym=sym), Side.BUY),
    ]
    return _idea("iron_condor", legs, -2.0, 300.0, 200.0, pop=0.65)


def chain_for(idea: TradeIdea, mids: list[float], half: float = 0.10, asof: datetime = NOW) -> OptionChain:
    """Chain quoting each leg of `idea` at the given mids (same order as idea.legs)."""
    quotes = [q(l.quote.strike, l.quote.right, m, half, l.quote.expiry, l.quote.underlying) for l, m in zip(idea.legs, mids)]
    return OptionChain(idea.symbol, 100.0, asof, quotes)


def account(equity: float = 10_000.0, cash: float | None = None, bp: float | None = None, day_pnl: float = 0.0) -> AccountState:
    return AccountState(equity=equity, cash=equity if cash is None else cash, buying_power=equity if bp is None else bp, day_pnl=day_pnl)


def position_of(idea: TradeIdea, contracts: int = 1, entry: float | None = None, mark: float | None = None) -> Position:
    return Position(id="p-" + idea.symbol + idea.strategy, idea=idea, contracts=contracts,
                    entry_price=idea.net_price if entry is None else entry, opened=NOW, mark=mark)

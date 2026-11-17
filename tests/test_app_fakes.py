"""Shared fakes for the app/backtest tests (no tests in this module)."""
from __future__ import annotations

import dataclasses
from datetime import date, datetime
from types import SimpleNamespace

from kwanttrades.app.session import ManualClock, TradingSession
from kwanttrades.config import Config
from kwanttrades.data.synthetic import SyntheticProvider
from kwanttrades.execution.journal import Journal
from kwanttrades.execution.paper import PaperBroker
from kwanttrades.types import NY, Leg, OptionChain, Right, Side, TradeIdea

START = date(2026, 11, 2)  # simulated sessions 2026-11-02 .. 2026-11-13 (n_days=10)
THU = datetime(2026, 11, 12, 10, 30, tzinfo=NY)


def long_call_idea(chain: OptionChain, symbol: str, otm: float = 0.02, max_loss_mult: float = 1.0,
                   stop_loss_pct: float = 0.35, intraday: bool = True, score: float = 0.5, min_dte: int = 1, max_dte: int = 999) -> TradeIdea:
    exp = [e for e in chain.expiries() if min_dte <= (e - chain.asof.date()).days <= max_dte][0]
    qs = [q for q in chain.for_expiry(exp) if q.right == Right.CALL and q.bid >= 0.2]
    q = max((x for x in qs if x.mid <= 0.6), key=lambda x: x.mid)  # cheap enough for 1% risk sizing
    return TradeIdea(
        symbol=symbol, strategy="long_call", legs=[Leg(q, Side.BUY, 1)], net_price=q.mid, entry_limit=round(q.ask, 2),
        max_loss=q.mid * 100 * max_loss_mult, max_profit=float("inf"), pop=0.45, expected_value=20.0, score=score,
        rationale=["fake idea", "intraday directional"] if intraday else ["fake idea"], stop_loss_pct=stop_loss_pct)


class FakeEngine:
    """Implements the StrategyEngine interface with a configurable idea factory."""

    def __init__(self, idea_fn=None):
        self.idea_fn = idea_fn or (lambda analysis: [long_call_idea(analysis.chain, analysis.symbol)])
        self.analyzed: list[str] = []

    def analyze(self, symbol, chain, daily_bars, direction, intraday_bars=None, earnings_date=None, risk_multiplier=1.0):
        self.analyzed.append(symbol)
        return SimpleNamespace(symbol=symbol, chain=chain, surface=None, vol_view=None, mispricings=[],
                               direction=direction, risk_multiplier=risk_multiplier, earnings_date=earnings_date, notes=[])

    def generate(self, analysis):
        return self.idea_fn(analysis)


class ScaledProvider:
    """Wraps a provider and scales every option quote of one symbol (to force take-profit / stop paths)."""

    def __init__(self, base, factor: float = 1.0, fail_symbols: tuple[str, ...] = ()):
        self.base, self.factor, self.fail = base, factor, set(fail_symbols)
        self.name = "scaled"

    def __getattr__(self, item):
        return getattr(self.base, item)

    def chain(self, symbol, min_dte=0, max_dte=45):
        if symbol in self.fail:
            raise RuntimeError("simulated provider outage")
        ch = self.base.chain(symbol, min_dte, max_dte)
        if self.factor == 1.0:
            return ch
        qs = [dataclasses.replace(q, bid=round(q.bid * self.factor, 2), ask=round(q.ask * self.factor, 2),
                                  last=q.last * self.factor) for q in ch.quotes]
        return dataclasses.replace(ch, quotes=qs)


def build(mode="paper", symbols=("SPY",), engine=None, when=THU, cfg: Config | None = None, factor=1.0, fail=(),
          broker=True, analyze_when_blocked=True):
    cfg = cfg or Config()
    clock = ManualClock(when)
    base = SyntheticProvider.simulated(list(symbols), n_days=10, start_date=START, clock=clock)
    provider = ScaledProvider(base, factor, fail)
    journal = Journal(":memory:")
    b = PaperBroker(cfg.broker, journal, clock=clock) if (broker and mode in ("paper",)) else None
    s = TradingSession(cfg, provider, b, engine or FakeEngine(), journal, mode=mode, clock=clock,
                       analyze_when_blocked=analyze_when_blocked)
    s.universe = list(symbols)
    return SimpleNamespace(session=s, clock=clock, provider=provider, base=base, journal=journal, broker=b, cfg=cfg)

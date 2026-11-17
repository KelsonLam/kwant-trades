"""Real-data fixtures for the strategy engine, plus the shared idea-invariant checker."""
from __future__ import annotations

import dataclasses
import json
import math
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from kwanttrades.config import BrokerConfig, Config
from kwanttrades.execution.journal import Journal
from kwanttrades.execution.paper import PaperBroker
from kwanttrades.execution.risk import RiskManager, is_intraday, uncovered_short_legs
from kwanttrades.strategy import STALE_LINE, StrategyEngine
from kwanttrades.strategy.structures import VOL, liquid
from kwanttrades.types import NY, AccountState, DirectionalSignal, OptionChain, OptionQuote, OrderStatus, Right, Side, TradeIdea

FIXTURES = Path(__file__).parent / "fixtures"


# ----------------------------------------------------------------------------- loaders


def load_chain(name: str) -> OptionChain:
    """Load ``tests/fixtures/<name>_chain_real.json`` into an OptionChain."""
    d = json.loads((FIXTURES / f"{name}_chain_real.json").read_text(encoding="utf-8"))
    cols = d["columns"]
    ix = {c: i for i, c in enumerate(cols)}
    quotes = []
    for r in d["quotes"]:
        iv = r[ix["vendor_iv"]]
        quotes.append(
            OptionQuote(
                underlying=d["underlying"],
                expiry=date.fromisoformat(r[ix["expiry"]]),
                strike=float(r[ix["strike"]]),
                right=Right(r[ix["right"]]),
                bid=float(r[ix["bid"]] or 0.0),
                ask=float(r[ix["ask"]] or 0.0),
                last=float(r[ix["last"]] or 0.0),
                volume=int(r[ix["volume"]] or 0),
                open_interest=int(r[ix["open_interest"]] or 0),
                vendor_iv=None if iv is None else float(iv),
            )
        )
    asof = datetime.fromisoformat(d["asof"]).astimezone(NY)
    return OptionChain(d["underlying"], float(d["spot"]), asof, quotes, float(d["risk_free"]), float(d["div_yield"]))


def load_bars(name: str) -> pd.DataFrame:
    """Load a bars CSV (first column = tz-aware index) as a NY-indexed float DataFrame."""
    df = pd.read_csv(FIXTURES / name, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(NY)
    return df[["open", "high", "low", "close", "volume"]].astype(float)


# ----------------------------------------------------------------------------- invariants

NOW_OPEN = datetime(2026, 11, 16, 11, 0, tzinfo=NY)


def assert_idea_ok(idea: TradeIdea, cfg: Config, engine: StrategyEngine | None = None) -> None:
    """Every invariant a returned idea must satisfy (risk gate, paper fill, filters, tags)."""
    # defined risk and the risk gate's reward:risk / credit:width rules
    assert uncovered_short_legs(idea) == []
    acct = AccountState(equity=1e7, cash=1e7, buying_power=1e7)
    dec = RiskManager(cfg.risk).check(idea, 1, acct, [], NOW_OPEN, 0)
    assert dec.passed, dec.reasons
    # entry_limit is filled by the paper broker at the chain's quotes
    broker = PaperBroker(dataclasses.replace(cfg.broker, starting_equity=1e7, db_path=":memory:"), Journal(":memory:"), clock=lambda: NOW_OPEN)
    order = broker.submit(idea, 1, idea.entry_limit)
    assert order.status == OrderStatus.FILLED, order.reason
    assert abs(round(idea.entry_limit * 100) - idea.entry_limit * 100) < 1e-6  # whole cents
    # filters
    assert idea.expected_value >= cfg.scan.min_expected_value - 1e-9
    assert idea.score >= cfg.scan.min_score - 1e-9
    assert 0.05 <= idea.pop <= 0.95
    assert math.isfinite(idea.max_loss) and idea.max_loss > 0
    frac = engine.max_edge_frac if engine else 0.30
    assert idea.expected_value <= frac * idea.max_loss + 0.01
    assert idea.rationale and all(isinstance(r, str) and r for r in idea.rationale)
    assert set(idea.greeks) >= {"delta", "gamma", "vega", "theta"}
    assert all(math.isfinite(v) for v in idea.greeks.values())
    # tagging: directional ideas are intraday, vol ideas are not
    assert is_intraday(idea) == (idea.strategy not in VOL)
    # liquidity (volume waived only if the chain has no volume at all)
    for l in idea.legs:
        assert liquid(l.quote, cfg.scan, waive_volume=True)
    # sign convention
    if idea.strategy in {"bull_put_spread", "bear_call_spread", "iron_condor"}:
        assert idea.net_price < 0 and idea.entry_limit < 0
    else:
        assert idea.net_price > 0 and idea.entry_limit > 0


# ----------------------------------------------------------------------------- real data


SIGNALS = {
    "bullish": (0.6, 0.7),
    "bearish": (-0.6, 0.7),
    "neutral": (0.05, 0.5),
}


@pytest.fixture(scope="module")
def daily() -> pd.DataFrame:
    return load_bars("spy_daily_real.csv")


@pytest.fixture(scope="module")
def intraday() -> pd.DataFrame:
    return load_bars("spy_1m_real.csv")


def test_loaders_shapes(daily, intraday):
    ch = load_chain("spy")
    assert ch.underlying == "SPY" and ch.spot > 0 and len(ch.quotes) > 1000
    assert ch.asof.tzinfo is not None
    assert len(daily) > 300 and str(daily.index.tz) == "America/New_York"
    assert len(intraday) > 1000
    aapl = load_chain("aapl")
    assert aapl.underlying == "AAPL" and len(aapl.quotes) > 100


@pytest.mark.parametrize("name", ["spy", "aapl"])
def test_real_chain_end_to_end(name, daily, intraday):
    cfg = Config()
    eng = StrategyEngine(cfg)
    chain = load_chain(name)
    t0 = time.perf_counter()
    analysis = eng.analyze(name.upper(), chain, daily, DirectionalSignal(name.upper(), "composite", 0.6, 0.7), intraday_bars=intraday)
    assert analysis.vol_view.horizon_days >= 14
    assert eng.is_stale(chain)  # fixture was captured on a Sunday night
    total = 0
    for label, (d, c) in SIGNALS.items():
        analysis.direction = DirectionalSignal(name.upper(), "composite", d, c)
        ideas = eng.generate(analysis)
        assert len(ideas) <= cfg.scan.max_ideas_per_symbol
        scores = [i.score for i in ideas]
        assert scores == sorted(scores, reverse=True)
        for idea in ideas:
            assert_idea_ok(idea, cfg, eng)
            assert STALE_LINE in idea.rationale
            if label == "bullish":
                assert idea.strategy in {"long_call", "bull_call_spread", "bull_put_spread"}
            elif label == "bearish":
                assert idea.strategy in {"long_put", "bear_put_spread", "bear_call_spread"}
            else:
                assert idea.strategy in VOL
        total += len(ideas)
    elapsed = time.perf_counter() - t0
    assert elapsed < 8.0, f"{name}: analyze + 3x generate took {elapsed:.1f}s"


def test_real_spy_produces_something(daily):
    """SPY's real chain with the fixture's own vol view yields at least one idea for one of the
    three signals (guards against a silently over-tight filter stack)."""
    eng = StrategyEngine(Config())
    a = eng.analyze("SPY", load_chain("spy"), daily, DirectionalSignal("SPY", "composite", 0.6, 0.7))
    n = 0
    for d, c in SIGNALS.values():
        a.direction = DirectionalSignal("SPY", "composite", d, c)
        n += len(eng.generate(a))
    assert n >= 1

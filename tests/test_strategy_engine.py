"""StrategyEngine: structure matrix, filters, costs, tagging, earnings, stale quotes, selection."""
from __future__ import annotations

import dataclasses
import math
from datetime import datetime, timedelta

import numpy as np
import pytest

from kwanttrades.config import Config
from kwanttrades.execution.risk import is_intraday
from kwanttrades.quant.surface import build_surface
from kwanttrades.strategy import DRIFT_K, STALE_LINE, VOL_SHRINK, StrategyEngine, SymbolAnalysis
from kwanttrades.types import NY, DirectionalSignal, Mispricing, OptionChain, Right, Side, VolView
from tests.test_quant_synth import expiry_for, make_chain
from tests.test_quant_volforecast import gbm_bars
from tests.test_strategy_fixtures import assert_idea_ok

STRIKES = np.arange(340.0, 460.01, 1.0)
BULL = {"long_call", "bull_call_spread", "bull_put_spread"}
BEAR = {"long_put", "bear_put_spread", "bear_call_spread"}


@pytest.fixture(scope="module")
def chain() -> OptionChain:
    return make_chain(days=(7, 30), spot=400.0, strikes=STRIKES)


@pytest.fixture(scope="module")
def surface(chain):
    return build_surface(chain, Config().scan)


def rich_cfg() -> Config:
    """Synthetic quotes are fair to the model, so 0.30-delta credit spreads collect ~0.23-0.27
    of width at the limit and modest EV; loosen the credit:width, EV and score floors to test
    the ROUTING (min_expected_value is pinned at 5 so the test does not depend on the default)."""
    cfg = Config()
    cfg.risk.min_credit_to_width = 0.20
    cfg.scan.min_score = 0.02
    cfg.scan.min_expected_value = 5.0
    return cfg


def analysis(chain, surface, d, c, V, mis=None, earnings=None, rm=1.0) -> SymbolAnalysis:
    atm = surface.atm_iv_at(30)
    vv = VolView("X", 30, atm, atm * (1 + V), V)
    return SymbolAnalysis("X", chain, surface, vv, list(mis or []), DirectionalSignal("X", "composite", d, c), rm, earnings)


def run(cfg, a):
    eng = StrategyEngine(cfg)
    ideas = eng.generate(a)
    for i in ideas:
        assert_idea_ok(i, cfg, eng)
    return eng, ideas


# ----------------------------------------------------------------------------- matrix


def test_constants_exposed():
    eng = StrategyEngine(Config())
    assert VOL_SHRINK == 0.5 and DRIFT_K == 0.35
    assert eng.vol_shrink == VOL_SHRINK and eng.drift_k == DRIFT_K
    assert 0 < eng.min_confidence < 1 and 0 < eng.max_edge_frac < 1


def test_grading():
    eng = StrategyEngine(Config())
    s = lambda d, c: DirectionalSignal("X", "composite", d, c)  # noqa: E731
    assert eng.grade(s(0.6, 0.7)) == "bullish"
    assert eng.grade(s(-0.6, 0.7)) == "bearish"
    assert eng.grade(s(0.1, 0.9)) == "neutral"
    assert eng.grade(s(0.6, 0.05)) == "unclear"


def test_bullish_cheap(chain, surface):
    _, ideas = run(Config(), analysis(chain, surface, 0.6, 0.7, 0.4))
    assert ideas and {i.strategy for i in ideas} <= {"long_call", "bull_call_spread"}
    assert all(is_intraday(i) and i.expiry == expiry_for(7) for i in ideas)


def test_bearish_cheap(chain, surface):
    _, ideas = run(Config(), analysis(chain, surface, -0.6, 0.7, 0.4))
    assert ideas and {i.strategy for i in ideas} <= {"long_put", "bear_put_spread"}


def test_bullish_rich(chain, surface):
    cfg = rich_cfg()
    eng, ideas = run(cfg, analysis(chain, surface, 0.6, 0.7, -0.6))
    assert ideas and {i.strategy for i in ideas} == {"bull_put_spread"}


def test_bearish_rich(chain, surface):
    _, ideas = run(rich_cfg(), analysis(chain, surface, -0.6, 0.7, -0.45))
    assert ideas and {i.strategy for i in ideas} == {"bear_call_spread"}


def test_rich_debit_vertical_financed_by_flagged_rich_strike(chain, surface):
    """In the rich row a debit vertical is only built when its short strike is flagged rich."""
    eng = StrategyEngine(Config())
    a = analysis(chain, surface, 0.6, 0.7, -0.45)
    assert not any(c.strategy == "bull_call_spread" for c, _ in eng.candidates(a))
    from kwanttrades.strategy import structures as st
    ctx = st.expiry_context(chain, surface, expiry_for(7), Config().scan)
    plain = st.debit_verticals(ctx, Right.CALL)
    short_q = plain[-1].legs[1].quote
    flag = Mispricing(short_q, "svi_residual", short_q.mid - 0.1, short_q.mid, -0.1, -0.05, -3.0)
    a2 = analysis(chain, surface, 0.6, 0.7, -0.45, mis=[flag])
    fin = [c for c, _ in eng.candidates(a2) if c.strategy == "bull_call_spread"]
    assert fin and all(any(l.quote.occ_symbol == short_q.occ_symbol and l.side == Side.SELL for l in c.legs) for c in fin)


def test_neutral_rich_iron_condor(chain, surface):
    _, ideas = run(rich_cfg(), analysis(chain, surface, 0.05, 0.5, -0.45))
    assert ideas and {i.strategy for i in ideas} == {"iron_condor"}
    assert all(not is_intraday(i) and i.expiry == expiry_for(30) for i in ideas)


def test_neutral_strong_cheap_long_vol(chain, surface):
    _, ideas = run(Config(), analysis(chain, surface, 0.05, 0.5, 0.5))
    assert ideas and {i.strategy for i in ideas} <= {"long_straddle", "long_strangle"}
    assert all(not is_intraday(i) for i in ideas)


@pytest.mark.parametrize("d,c,V", [(0.1, 0.5, 0.05), (0.1, 0.5, -0.05), (0.05, 0.5, 0.12), (0.6, 0.05, 0.4), (0.0, 0.0, 0.0)])
def test_weak_views_give_no_ideas(chain, surface, d, c, V):
    eng = StrategyEngine(Config())
    assert eng.generate(analysis(chain, surface, d, c, V)) == []


# ----------------------------------------------------------------------------- filters / rules


def test_scores_costs_and_rationale(chain, surface):
    cfg = Config()
    eng, ideas = run(cfg, analysis(chain, surface, 0.05, 0.5, 0.5, rm=0.8))
    for i in ideas:
        conf = min(1.0, 0.5 / 0.30)
        assert i.score == pytest.approx(i.expected_value / i.max_loss * conf * 0.8, rel=1e-3, abs=1e-4)
        hs = sum(0.5 * l.quote.spread for l in i.legs)
        assert i.entry_limit >= i.net_price + cfg.broker.slippage_frac_of_half_spread * hs - 1e-9
        assert i.entry_limit - (i.net_price + cfg.broker.slippage_frac_of_half_spread * hs) < 0.01 + 1e-9
        text = " | ".join(i.rationale)
        for word in ("Signal", "Vol view", "Mispriced legs", "EV", "POP", "breakevens"):
            assert word in text
        assert STALE_LINE not in i.rationale  # Monday 10:00 with volume


def test_ev_clipping(chain, surface):
    cfg = Config()
    eng = StrategyEngine(cfg)
    eng.max_edge_frac = 0.08
    a = analysis(chain, surface, 0.6, 0.7, 1.0)
    ideas = eng.generate(a)
    assert ideas
    for i in ideas:
        assert_idea_ok(i, cfg, eng)
        assert i.expected_value == pytest.approx(0.08 * i.max_loss, abs=0.01)
        assert any("clipped" in r for r in i.rationale)


def test_pop_bounds(chain, surface):
    eng = StrategyEngine(Config())
    eng.pop_bounds = (0.5, 0.95)  # long options here have POP < 0.5
    assert eng.generate(analysis(chain, surface, 0.6, 0.7, 0.4)) == []
    assert any("POP" in why for _, why in eng.last_rejections)


def test_min_ev_and_score_filters(chain, surface):
    cfg = Config()
    cfg.scan.min_expected_value = 1e6
    assert StrategyEngine(cfg).generate(analysis(chain, surface, 0.05, 0.5, 0.5)) == []
    cfg = Config()
    cfg.scan.min_score = 10.0
    assert StrategyEngine(cfg).generate(analysis(chain, surface, 0.05, 0.5, 0.5)) == []


def test_earnings_blocks_short_premium(chain, surface):
    cfg = rich_cfg()
    a = analysis(chain, surface, 0.6, 0.7, -0.6, earnings=expiry_for(3))
    eng = StrategyEngine(cfg)
    ideas = eng.generate(a)
    assert not any(i.is_credit for i in ideas)
    assert any("earnings" in why for _, why in eng.last_rejections)
    # condor through earnings blocked too
    assert StrategyEngine(Config()).generate(analysis(chain, surface, 0.05, 0.5, -0.45, earnings=expiry_for(30))) == []
    # earnings already passed -> allowed
    past = chain.asof.date() - timedelta(days=5)
    assert StrategyEngine(cfg).generate(analysis(chain, surface, 0.6, 0.7, -0.6, earnings=past))
    # long premium through earnings: allowed, with a warning
    ideas = StrategyEngine(Config()).generate(analysis(chain, surface, 0.6, 0.7, 0.4, earnings=expiry_for(3)))
    assert ideas and all(any("earnings" in r for r in i.rationale) for i in ideas)


def test_liquidity_filter_blocks_everything(chain, surface):
    cfg = Config()
    cfg.scan.max_spread_pct = 0.001
    assert StrategyEngine(cfg).generate(analysis(chain, surface, 0.6, 0.7, 0.4)) == []
    cfg = Config()
    cfg.scan.min_open_interest = 10_000
    assert StrategyEngine(cfg).generate(analysis(chain, surface, 0.05, 0.5, 0.5)) == []


def test_max_ideas_and_diversity(chain, surface):
    cfg = Config()
    cfg.scan.max_ideas_per_symbol = 1
    assert len(StrategyEngine(cfg).generate(analysis(chain, surface, 0.05, 0.5, 0.5))) == 1
    cfg = Config()
    cfg.scan.max_ideas_per_symbol = 6
    cfg.risk.min_credit_to_width = 0.15
    cfg.scan.min_score = 0.0
    cfg.scan.min_expected_value = 0.0
    _, ideas = run(cfg, analysis(chain, surface, -0.6, 0.7, -0.6))
    assert len(ideas) >= 2
    legs = [(l.quote.occ_symbol, l.side) for i in ideas for l in i.legs]
    # the second-pass fill never reuses a leg of an already chosen idea of another strategy
    by_strat = {}
    for i in ideas:
        by_strat.setdefault(i.strategy, []).append(i)
    for group in by_strat.values():
        seen = set()
        for i in group:
            ls = {(l.quote.occ_symbol, l.side) for l in i.legs}
            assert not (ls & seen)
            seen |= ls
    assert len(legs) >= 4


def test_select_dedupes_identical_and_overlapping():
    from tests.test_execution_factories import bull_call_spread, long_call
    a = bull_call_spread()
    b = dataclasses.replace(a, score=a.score - 0.01)
    c = long_call()
    a.score, b.score, c.score = 0.5, 0.4, 0.3
    out = StrategyEngine.select([b, a, c], 5)
    assert out[0] is a and b not in out and c in out


def test_stale_chain_rationale(chain, surface):
    sat = datetime(2026, 11, 14, 12, 0, tzinfo=NY)
    eve = datetime(2026, 11, 16, 18, 30, tzinfo=NY)
    for asof in (sat, eve):
        stale = dataclasses.replace(chain, asof=asof)
        assert StrategyEngine.is_stale(stale)
    zero_vol = dataclasses.replace(chain, quotes=[dataclasses.replace(q, volume=0) for q in chain.quotes])
    assert StrategyEngine.is_stale(zero_vol)
    assert not StrategyEngine.is_stale(chain)
    _, ideas = run(Config(), analysis(zero_vol, surface, 0.05, 0.5, 0.5))
    assert ideas and all(STALE_LINE in i.rationale for i in ideas)
    # the stale line never makes a vol idea look intraday
    assert all(not is_intraday(i) for i in ideas)


def test_greeks_signs(chain, surface):
    _, ideas = run(Config(), analysis(chain, surface, 0.05, 0.5, 0.5))
    for i in ideas:  # long vol
        assert i.greeks["vega"] > 0 and i.greeks["gamma"] > 0 and i.greeks["theta"] < 0
    _, ideas = run(Config(), analysis(chain, surface, 0.05, 0.5, -0.45))
    for i in ideas:  # iron condor
        assert i.greeks["vega"] < 0 and i.greeks["theta"] > 0
    _, ideas = run(Config(), analysis(chain, surface, 0.6, 0.7, 0.4))
    for i in ideas:
        assert i.greeks["delta"] > 0


def test_drift_tilt_direction(chain, surface):
    """A bullish tilt raises a long call's EV relative to a bearish one, all else equal."""
    eng = StrategyEngine(Config())
    from kwanttrades.strategy import structures as st
    ctx = st.expiry_context(chain, surface, expiry_for(7), Config().scan)
    up = eng._dist(analysis(chain, surface, 1.0, 1.0, 0.0), ctx)[0]
    dn = eng._dist(analysis(chain, surface, -1.0, 1.0, 0.0), ctx)[0]
    flat = eng._dist(analysis(chain, surface, 0.0, 1.0, 0.0), ctx)[0]
    # session-clock daily sigma: sqrt(w_phys / sessions) for this expiry
    sig_d = eng.expiry_view(analysis(chain, surface, 0.0, 1.0, 0.0), ctx)["sigma_daily"]
    assert math.log(up.mean / flat.mean) == pytest.approx(DRIFT_K * sig_d, rel=1e-6)
    assert math.log(flat.mean / dn.mean) == pytest.approx(DRIFT_K * sig_d, rel=1e-6)
    assert flat.mean == pytest.approx(ctx.forward, rel=1e-9)


def test_analyze_synthetic(chain):
    eng = StrategyEngine(Config())
    bars = gbm_bars(n_days=300, sigma=0.30, s0=400.0)
    a = eng.analyze("X", chain, bars, DirectionalSignal("X", "composite", 0.05, 0.5), earnings_date=None, risk_multiplier=0.5)
    assert a.vol_view.horizon_days == 30
    assert a.risk_multiplier == 0.5 and a.symbol == "X"
    assert expiry_for(30) in a.surface.slices
    assert isinstance(a.mispricings, list)
    assert any("vol expiry" in n for n in a.notes)
    for i in eng.generate(a):
        assert_idea_ok(i, eng.cfg, eng)

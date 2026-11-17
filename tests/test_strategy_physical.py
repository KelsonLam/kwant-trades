"""physical_dist: market-implied skew shape at our vol level -> no edge from skew alone."""
from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest

from kwanttrades.config import Config, ScanConfig
from kwanttrades.quant.distribution import TerminalDist, evaluate_structure, payoff_at_expiry
from kwanttrades.quant.surface import build_surface
from kwanttrades.strategy import StrategyEngine, SymbolAnalysis
from kwanttrades.strategy import structures as st
from kwanttrades.strategy.physical import SOURCE_TEXT, physical_dist
from kwanttrades.types import DirectionalSignal, OptionChain, Right, VolView
from tests.test_quant_synth import expiry_for, make_chain

SKEW = dict(a=0.015, b=0.15, rho=-0.7, m=0.02, sigma=0.12)  # steep put skew
STRIKES = np.arange(300.0, 500.01, 1.0)


@pytest.fixture(scope="module")
def zero_rate():
    """Skewed chain with r = q = 0, so mid prices are undiscounted expectations."""
    ch = make_chain(days=(30,), spot=400.0, strikes=STRIKES, base=SKEW, r=0.0, q=0.0)
    s = build_surface(ch, ScanConfig())
    ctx = st.expiry_context(ch, s, expiry_for(30), ScanConfig())
    return ch, s, ctx


@pytest.fixture(scope="module")
def with_rate():
    ch = make_chain(days=(30,), spot=400.0, strikes=STRIKES, base=SKEW)
    s = build_surface(ch, ScanConfig())
    ctx = st.expiry_context(ch, s, expiry_for(30), ScanConfig())
    return ch, s, ctx


def all_candidates(ctx):
    return (
        st.long_option(ctx, Right.CALL) + st.long_option(ctx, Right.PUT)
        + st.debit_verticals(ctx, Right.CALL) + st.debit_verticals(ctx, Right.PUT)
        + st.credit_verticals(ctx, Right.PUT) + st.credit_verticals(ctx, Right.CALL)
        + st.iron_condor(ctx) + st.long_straddle(ctx) + st.long_strangle(ctx)
    )


def test_market_view_gives_zero_ev_for_every_structure(zero_rate):
    """KEY PROPERTY: vol_scale = 1 and no shift reproduce the market, so no structure priced at
    mid with zero costs has edge - in particular none from the skew."""
    _, _, ctx = zero_rate
    dist = physical_dist(ctx.slice, ctx.forward, ctx.T, 1.0, 0.0)
    assert dist.source == "implied"
    cands = all_candidates(ctx)
    assert len(cands) >= 15
    for c in cands:
        ev = evaluate_structure(c.legs, c.net_mid, dist)["ev"]
        assert abs(ev) < 0.10, (c.strategy, [l.quote.strike for l in c.legs], ev)


def test_market_view_with_rates_matches_discounted_prices(with_rate):
    """With r > 0 the undiscounted expected payoff equals mid * e^{rT}."""
    ch, _, ctx = with_rate
    dist = physical_dist(ctx.slice, ctx.forward, ctx.T, 1.0, 0.0)
    g = math.exp(ch.risk_free * ctx.T)
    for c in all_candidates(ctx):
        pay = dist.expect(lambda S: payoff_at_expiry(c.legs, S))
        assert abs(pay / g - c.net_mid) * 100 < 0.05


def test_put_and_call_side_symmetric_unlike_lognormal(zero_rate):
    """Same-delta put and call spreads: ~0 and equal EV under the implied view, while the old
    symmetric lognormal shows a skew-driven bias (put debit spreads, financed by the rich OTM
    put, look profitable and call debit spreads look like losers; credit spreads diverge)."""
    _, _, ctx = zero_rate
    new = physical_dist(ctx.slice, ctx.forward, ctx.T, 1.0, 0.0)
    old = TerminalDist.lognormal(ctx.forward, ctx.T, ctx.slice.atm_iv, kurtosis_mix=0.1)
    ev = lambda c, d: evaluate_structure(c.legs, c.net_mid, d)["ev"]  # noqa: E731
    bps = st.credit_verticals(ctx, Right.PUT, short_targets=(0.30,))[0]
    bcs = st.credit_verticals(ctx, Right.CALL, short_targets=(0.30,))[0]
    assert abs(ev(bps, new)) < 0.10 and abs(ev(bcs, new)) < 0.10
    assert abs(ev(bps, new) - ev(bcs, new)) < 0.10
    assert abs(ev(bps, old) - ev(bcs, old)) > 2.0
    bear = st.debit_verticals(ctx, Right.PUT)[-1]
    bull = st.debit_verticals(ctx, Right.CALL)[-1]
    assert ev(bear, old) > 20.0 and ev(bull, old) < -20.0  # the old skew bias
    assert abs(ev(bear, new)) < 0.10 and abs(ev(bull, new)) < 0.10


@pytest.mark.parametrize("shift", [0.0, 0.01, -0.02])
@pytest.mark.parametrize("scale", [0.8, 1.0, 1.3])
def test_mean_and_normalisation(zero_rate, shift, scale):
    _, _, ctx = zero_rate
    d = physical_dist(ctx.slice, ctx.forward, ctx.T, scale, shift)
    assert d.source == "implied"
    assert d.probs.sum() == pytest.approx(1.0, abs=1e-12)
    assert np.all(d.probs >= 0) and np.all(np.diff(d.prices) > 0)
    assert d.mean == pytest.approx(ctx.forward * math.exp(shift), rel=1e-10)


def test_vol_scale_moves_level(zero_rate):
    _, _, ctx = zero_rate
    (strad,) = st.long_straddle(ctx)
    ic = st.iron_condor(ctx)[0]
    ev = lambda c, s: evaluate_structure(c.legs, c.net_mid, physical_dist(ctx.slice, ctx.forward, ctx.T, s, 0.0))["ev"]  # noqa: E731
    assert ev(strad, 1.2) > ev(strad, 1.0) + 50 > ev(strad, 0.8) + 100
    assert ev(ic, 1.2) < ev(ic, 1.0) < ev(ic, 0.8)
    # higher vol keeps the skew: the scaled density is still left-skewed
    d = physical_dist(ctx.slice, ctx.forward, ctx.T, 1.2, 0.0)
    x = np.log(d.prices / d.mean)
    m = np.dot(x, d.probs)
    assert np.dot((x - m) ** 3, d.probs) < 0


def test_fallback_paths(zero_rate):
    ch, s, ctx = zero_rate
    F, T, sl = ctx.forward, ctx.T, ctx.slice
    cases = {
        "flat": dataclasses.replace(sl, method="flat", params={"atm_iv": sl.atm_iv}),
        "points": dataclasses.replace(sl, n_points=5),
        "error": dataclasses.replace(sl, table=sl.table.iloc[0:0]),
    }
    for name, bad in cases.items():
        d = physical_dist(bad, F, T, 1.1, 0.01)
        assert d.source == "lognormal" and d.reason, name
        assert d.mean == pytest.approx(F * math.exp(0.01), rel=1e-9)
        assert d.probs.sum() == pytest.approx(1.0)
    d = physical_dist(None, F, T, 1.0, 0.0)
    assert d.source == "lognormal"


def test_butterfly_check_on_fitted_range(zero_rate, monkeypatch):
    """A slice flagged for butterfly arbitrage is still trusted when the violation lies outside
    the fitted strike range; a violation inside it forces the fallback."""
    import kwanttrades.strategy.physical as ph
    _, _, ctx = zero_rate
    flagged = dataclasses.replace(ctx.slice, butterfly_arbitrage=True)
    assert ph.physical_dist(flagged, ctx.forward, ctx.T, 1.0, 0.0).source == "implied"
    monkeypatch.setattr(ph, "butterfly_check", lambda sl, kr: -0.05)
    d = ph.physical_dist(flagged, ctx.forward, ctx.T, 1.0, 0.0)
    assert d.source == "lognormal" and "butterfly" in d.reason
    assert ph.physical_dist(ctx.slice, ctx.forward, ctx.T, 1.0, 0.0).source == "implied"  # unflagged


def test_real_spy_slices_use_implied_density():
    from tests.test_strategy_fixtures import load_chain
    ch = load_chain("spy")
    s = build_surface(ch, ScanConfig())
    used = [physical_dist(sl, sl.forward, sl.T, 1.0, 0.0).source for sl in s.slices.values()]
    assert used.count("implied") >= len(used) - 2


def test_negative_density_falls_back(zero_rate, monkeypatch):
    """More than 2% negative mass -> lognormal; a little negative mass is folded away."""
    import kwanttrades.strategy.physical as ph
    _, _, ctx = zero_rate
    K = np.linspace(300, 500, 201)
    bad = np.full(201, 1.0 / 190)
    bad[100:105] = -0.01  # 5% negative
    monkeypatch.setattr(ph, "implied_density", lambda *a, **k: (K, bad / bad.sum(), 0.05))
    d = ph.physical_dist(ctx.slice, ctx.forward, ctx.T, 1.0, 0.0)
    assert d.source == "lognormal" and "negative" in d.reason
    ok = np.full(201, 1.0 / 201)
    ok[100] -= 0.001
    ok[99] += 0.001
    monkeypatch.setattr(ph, "implied_density", lambda *a, **k: (K, ok, 0.001))
    d = ph.physical_dist(ctx.slice, ctx.forward, ctx.T, 1.0, 0.0)
    assert d.source == "implied" and np.all(d.probs >= 0)
    assert d.mean == pytest.approx(ctx.forward, rel=1e-12)


def test_engine_reports_distribution_used(zero_rate):
    ch, s, ctx = zero_rate
    atm = s.atm_iv_at(30)
    a = SymbolAnalysis("X", ch, s, VolView("X", 30, atm, atm * 1.5, 0.5), [], DirectionalSignal("X", "composite", 0.05, 0.5))
    ideas = StrategyEngine(Config()).generate(a)
    assert ideas
    for i in ideas:
        assert any(r.startswith("Distribution: ") and SOURCE_TEXT["implied"] in r for r in i.rationale)
    # flat slice -> the fallback is stated
    flat = dataclasses.replace(s, slices={k: dataclasses.replace(v, method="flat", params={"atm_iv": v.atm_iv}) for k, v in s.slices.items()})
    a2 = dataclasses.replace(a, surface=flat)
    for i in StrategyEngine(Config()).generate(a2):
        assert any(SOURCE_TEXT["lognormal"] in r for r in i.rationale)

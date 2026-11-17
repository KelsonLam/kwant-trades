import dataclasses

import numpy as np
import pytest

from kwanttrades.config import ScanConfig
from kwanttrades.quant import bs
from kwanttrades.quant.mispricing import scan_chain, vol_view
from kwanttrades.quant.surface import build_surface
from kwanttrades.types import OptionChain, OptionQuote, Right, VolView
from tests.test_quant_synth import ASOF, Q, R, SPOT, expiry_for, make_chain
from tests.test_quant_volforecast import gbm_bars

CFG = ScanConfig()


def shift(chain: OptionChain, moves: dict) -> OptionChain:
    """moves: {(days, strike, right): price shift applied to bid and ask}."""
    qs = []
    for q in chain.quotes:
        for (d, K, right), dx in moves.items():
            if q.expiry == expiry_for(d) and q.strike == K and q.right == right:
                q = dataclasses.replace(q, bid=max(q.bid + dx, 0.0), ask=q.ask + dx)
        qs.append(q)
    return OptionChain(chain.underlying, chain.spot, chain.asof, qs, chain.risk_free, chain.div_yield)


@pytest.fixture(scope="module")
def base_chain():
    return make_chain(tick=0.01)


def test_unperturbed_chain_has_no_flags(base_chain):
    s = build_surface(base_chain)
    flags = scan_chain(base_chain, s, CFG)
    assert [f for f in flags if f.model == "svi_residual"] == []
    assert flags == []


@pytest.mark.parametrize("noise,seed", [(0.0, 0), (0.0015, 4)])
def test_perturbed_quotes_are_flagged_with_correct_sign(noise, seed):
    ch = make_chain(tick=0.01, noise_vol=noise, seed=seed)
    cheap, rich = (30, 98.0, Right.PUT), (30, 103.0, Right.CALL)
    pert = shift(ch, {cheap: -0.25, rich: +0.25})
    for q in pert.quotes:  # perturbation is much larger than the spread
        if (q.expiry, q.strike, q.right) in ((expiry_for(30), 98.0, Right.PUT), (expiry_for(30), 103.0, Right.CALL)):
            assert q.spread < 0.1
    s = build_surface(pert)
    flags = scan_chain(pert, s, CFG)
    svi = [f for f in flags if f.model == "svi_residual"]
    got = {(f.quote.strike, f.quote.right): f for f in svi}
    assert set(got) == {(98.0, Right.PUT), (103.0, Right.CALL)}
    c, r = got[(98.0, Right.PUT)], got[(103.0, Right.CALL)]
    assert c.edge > 0 and c.edge_after_costs > 0 and c.zscore >= CFG.min_svi_zscore
    assert r.edge < 0 and r.edge_after_costs < 0 and r.zscore <= -CFG.min_svi_zscore
    assert c.fair_value > c.quote.ask and r.fair_value < r.quote.bid
    assert c.edge == pytest.approx(0.25, abs=0.03) and r.edge == pytest.approx(-0.25, abs=0.03)
    assert c.edge_after_costs == pytest.approx(c.fair_value - c.quote.ask)
    assert c.fair_iv > c.market_iv and r.fair_iv < r.market_iv
    z = [abs(f.zscore) for f in flags]
    assert z == sorted(z, reverse=True)


def test_inside_spread_perturbation_is_not_flagged(base_chain):
    # even with a low z threshold, a quote whose local fair value stays inside its bid/ask
    # is never flagged (edge would not survive crossing the spread)
    pert = shift(base_chain, {(60, 103.0, Right.CALL): 0.03})
    s = build_surface(pert)
    flags = [f for f in scan_chain(pert, s, ScanConfig(min_svi_zscore=0.3)) if f.model == "svi_residual"]
    assert all(f.fair_value > f.quote.ask or f.fair_value < f.quote.bid for f in flags)
    assert not [f for f in flags if f.quote.strike == 103.0 and f.quote.expiry == expiry_for(60)]


def test_smooth_wing_bias_relative_to_fit_is_not_flagged():
    # the whole put wing is offset from the SVI surface by a smooth bias (up to +2 vol points,
    # far more than the half-spread in vol terms): model misfit, not a mispricing
    from tests.test_quant_synth import true_iv

    clean = make_chain(tick=0.01)
    surf = build_surface(clean)
    biased_base = make_chain(tick=0.01)
    qs = []
    for q in biased_base.quotes:
        T = clean.T(q.expiry)
        F = SPOT * np.exp((R - Q) * T)
        k = np.log(q.strike / F)
        bias = 0.02 / (1.0 + np.exp(k / 0.02))  # ~0 for calls side, ~+2 vol pts deep in the put wing
        iv = float(true_iv(q.strike, T, F)) + bias
        px = float(bs.price(SPOT, q.strike, T, R, Q, iv, bs.cp_of(q.right)))
        h = float(0.01 + 0.015 * px) if px * 0.015 + 0.01 < 0.15 else 0.15
        qs.append(dataclasses.replace(q, bid=max(np.floor((px - h) * 100 + 1e-9) / 100, 0.0), ask=np.ceil((px + h) * 100 - 1e-9) / 100))
    biased = OptionChain("X", SPOT, ASOF, qs, R, Q)
    # old-style residual vs the (clean) parametric fit is large on the wing ...
    sl = surf.slices[expiry_for(60)]
    k90 = np.log(90.0 / sl.forward)
    assert 0.015 < 0.02 / (1 + np.exp(k90 / 0.02))
    # ... yet neither the clean-surface scan nor the self-fitted scan flags anything
    assert [f for f in scan_chain(biased, surf, CFG) if f.model == "svi_residual"] == []
    assert [f for f in scan_chain(biased, build_surface(biased), CFG) if f.model == "svi_residual"] == []


def test_parity_violation_flagged(base_chain):
    pert = shift(base_chain, {(60, 100.0, Right.CALL): +0.40})
    s = build_surface(pert)
    flags = scan_chain(pert, s, CFG)
    par = [f for f in flags if f.model == "parity"]
    assert len(par) == 1
    f = par[0]
    assert f.quote.expiry == expiry_for(60) and f.quote.strike == 100.0 and f.quote.right == Right.CALL
    assert f.edge < 0 and f.edge_after_costs < 0
    assert f.edge == pytest.approx(-0.40, abs=0.02)


def test_american_quotes_do_not_trigger_parity_flags():
    # American-style prices (r=6%: material put early-exercise premium on ITM puts) obey
    # parity only as an inequality; the early-exercise tolerance must absorb that.
    from kwanttrades.quant import american
    from tests.test_quant_synth import half_spread, true_iv

    r, q, e = 0.06, 0.0, expiry_for(60)
    T = OptionChain("X", SPOT, ASOF, [], r, q).T(e)
    F = SPOT * np.exp((r - q) * T)
    K = np.arange(85.0, 116.0, 2.5)
    iv = true_iv(K, T, F)
    quotes = []
    for cp, right in ((1, Right.CALL), (-1, Right.PUT)):
        px = american.price(SPOT, K, T, r, q, iv, cp, steps=100)
        hs = half_spread(px)
        for k_, p_, h_ in zip(K, px, hs):
            quotes.append(OptionQuote("X", e, float(k_), right, float(p_ - h_), float(p_ + h_), volume=50, open_interest=500))
    ch = OptionChain("X", SPOT, ASOF, quotes, r, q)
    # sanity: some deep ITM puts really are above European parity by more than the spread
    eu = np.asarray(bs.price(SPOT, K, T, r, q, iv, -1))
    am = american.price(SPOT, K, T, r, q, iv, -1, steps=100)
    assert np.max(am - eu) > 0.05
    flags = scan_chain(ch, build_surface(ch), CFG)
    assert [f for f in flags if f.model in ("parity", "butterfly", "calendar")] == []


def test_butterfly_and_calendar_static_arbitrage(base_chain):
    # body of a call fly priced far above its wings' average
    fly = shift(base_chain, {(14, 100.0, Right.CALL): +1.0})
    flags = scan_chain(fly, build_surface(fly), CFG)
    bf = [f for f in flags if f.model == "butterfly"]
    assert any(f.quote.strike == 100.0 and f.quote.right == Right.CALL and f.edge < 0 and f.edge_after_costs < 0 for f in bf)
    # far-dated OTM call offered below the near-dated bid
    near = base_chain.find(expiry_for(14), 101.0, Right.CALL)
    far = base_chain.find(expiry_for(30), 101.0, Right.CALL)
    cal = shift(base_chain, {(30, 101.0, Right.CALL): (near.bid - 0.10) - far.ask})
    flags = scan_chain(cal, build_surface(cal), CFG)
    cf = [f for f in flags if f.model == "calendar"]
    assert len(cf) == 1 and cf[0].quote.expiry == expiry_for(30) and cf[0].edge > 0
    assert cf[0].edge_after_costs == pytest.approx(0.10, abs=1e-9)


def test_scan_survives_ugly_chain():
    e = expiry_for(20)
    quotes = [
        OptionQuote("X", e, 100.0, Right.CALL, 0.0, 0.0),
        OptionQuote("X", e, 100.0, Right.PUT, 3.0, 1.0),
        OptionQuote("X", e, 105.0, Right.CALL, 1.0, 1.05, volume=10, open_interest=1000),
    ]
    ch = OptionChain("X", SPOT, ASOF, quotes, R, Q)
    assert isinstance(scan_chain(ch, build_surface(ch), CFG), list)
    empty = OptionChain("X", SPOT, ASOF, [], R, Q)
    assert scan_chain(empty, build_surface(empty), CFG) == []


def test_vol_view(base_chain):
    s = build_surface(base_chain)
    bars = gbm_bars(n_days=300, sigma=0.32, steps=60, seed=9)
    v = vol_view(base_chain, s, bars, horizon_days=30)
    assert isinstance(v, VolView) and v.horizon_days == 30
    assert v.atm_iv == pytest.approx(s.atm_iv_at(30))
    assert v.forecast_rv == pytest.approx(0.32, rel=0.2)
    assert v.vol_edge == pytest.approx((v.forecast_rv - v.atm_iv) / v.atm_iv)
    assert v.vol_edge > 0.2  # realised ~32% vs implied ~20% => options cheap
    assert {"ewma", "har", "garch", "yang_zhang", "ensemble", "atm_iv"} <= set(v.components)
    # no usable data => neutral edge, no exception
    v0 = vol_view(base_chain, s, bars.iloc[:2], horizon_days=21)
    assert v0.vol_edge == 0.0 and np.isnan(v0.forecast_rv)

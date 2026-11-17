import math
from datetime import timedelta

import numpy as np
import pytest

from kwanttrades.quant.surface import SmileSlice, build_surface, butterfly_check, durrleman_g, svi_derivatives, svi_total_var
from kwanttrades.types import OptionChain, OptionQuote, Right
from tests.test_quant_synth import ASOF, Q, R, SPOT, expiry_for, make_chain, svi_params, true_iv


@pytest.fixture(scope="module")
def clean_surface():
    ch = make_chain()
    return ch, build_surface(ch)


def test_svi_recovers_known_parameters_noiseless(clean_surface):
    ch, s = clean_surface
    assert len(s.slices) == 3
    for e, sl in s.slices.items():
        assert sl.method == "svi"
        assert sl.rmse < 1e-6
        F_true = SPOT * math.exp((R - Q) * sl.T)
        assert sl.forward == pytest.approx(F_true, rel=1e-9)
        assert s.diagnostics["slices"][e]["forward_source"] == "parity"
        p_true = svi_params(sl.T)
        k = np.linspace(-0.2, 0.2, 41)
        w_fit = sl.total_var(k)
        w_true = svi_total_var(k, *p_true.values())
        assert np.max(np.abs(np.sqrt(w_fit / sl.T) - np.sqrt(w_true / sl.T))) < 1e-5
        assert sl.params["rho"] == pytest.approx(p_true["rho"], abs=0.02)
        assert sl.params["m"] == pytest.approx(p_true["m"], abs=0.01)
        assert sl.params["sigma"] == pytest.approx(p_true["sigma"], rel=0.05)
        assert sl.params["b"] == pytest.approx(p_true["b"], rel=0.05)
        assert sl.params["a"] == pytest.approx(p_true["a"], rel=0.05, abs=1e-5)
        # table columns
        for col in ("quote", "market_iv", "bid_iv", "ask_iv", "fit_iv", "in_fit"):
            assert col in sl.table.columns
        assert sl.table["in_fit"].sum() == sl.n_points
        assert np.all(sl.table.loc[sl.table.in_fit, "bid_iv"] < sl.table.loc[sl.table.in_fit, "ask_iv"])
    d = s.diagnostics
    assert d["calendar_arbitrage"] is False and d["butterfly_any"] is False
    assert set(d["rmse"]) == set(s.slices) and set(d["n_points"]) == set(s.slices)


def test_surface_queries(clean_surface):
    ch, s = clean_surface
    e = expiry_for(30)
    sl = s.slices[e]
    assert s.iv(e, 95.0) == pytest.approx(true_iv(95.0, sl.T, sl.forward), abs=1e-5)
    assert s.atm_iv(e) == pytest.approx(true_iv(sl.forward, sl.T, sl.forward), abs=1e-5)
    assert s.forward(e) == pytest.approx(sl.forward)
    arr = s.iv(e, np.array([90.0, 100.0, 110.0]))
    assert arr.shape == (3,)
    # interpolated expiry sits between neighbours in total variance
    e45 = expiry_for(45)
    s30, s60 = s.slices[expiry_for(30)], s.slices[expiry_for(60)]
    T45 = ch.T(e45)
    w = s.iv(e45, s.forward(e45)) ** 2 * T45
    x = (T45 - s30.T) / (s60.T - s30.T)
    assert w == pytest.approx(s30.total_var(0.0) + x * (s60.total_var(0.0) - s30.total_var(0.0)), rel=1e-9)
    assert s30.forward < s.forward(e45) < s60.forward


def test_atm_iv_at_total_variance_interpolation_and_flat_extrapolation(clean_surface):
    _, s = clean_surface
    sl = sorted(s.slices.values(), key=lambda x: x.T)
    # at a slice's own maturity
    assert s.atm_iv_at(sl[1].T * 365) == pytest.approx(sl[1].atm_iv, rel=1e-9)
    # between slices: linear in total variance
    Tm = 0.5 * (sl[0].T + sl[1].T)
    w_expected = 0.5 * (sl[0].atm_iv**2 * sl[0].T + sl[1].atm_iv**2 * sl[1].T)
    assert s.atm_iv_at(Tm * 365) == pytest.approx(math.sqrt(w_expected / Tm), rel=1e-9)
    # flat outside
    assert s.atm_iv_at(1) == pytest.approx(sl[0].atm_iv, rel=1e-9)
    assert s.atm_iv_at(400) == pytest.approx(sl[-1].atm_iv, rel=1e-9)


def test_noisy_chain_is_robust():
    ch = make_chain(noise_vol=0.004, tick=0.01, seed=3)
    s = build_surface(ch)
    for e, sl in s.slices.items():
        assert sl.method == "svi"
        assert sl.rmse < 0.008
        fitted = sl.table[sl.table.in_fit]
        K = np.linspace(fitted.strike.min(), fitted.strike.max(), 25)
        err = np.abs(sl.iv(K) - true_iv(K, sl.T, sl.forward))
        assert np.max(err) < 0.01
        assert abs(sl.atm_iv - true_iv(sl.forward, sl.T, sl.forward)) < 0.004
        assert not sl.butterfly_arbitrage


def test_sparse_chain():
    ch = make_chain(days=(30, 60), strikes=np.arange(80.0, 121.0, 5.0), tick=0.01)
    s = build_surface(ch)
    assert len(s.slices) == 2
    for e, sl in s.slices.items():
        assert sl.method == ("svi" if sl.n_points >= 5 else "quadratic")
        err = abs(sl.atm_iv - true_iv(sl.forward, sl.T, sl.forward))
        assert err < 0.005
    assert s.slices[expiry_for(60)].method == "svi"


def test_fallbacks_quadratic_and_flat():
    ch4 = make_chain(days=(30,), strikes=np.array([96.0, 98.0, 102.0, 104.0]))
    sl = build_surface(ch4).slices[expiry_for(30)]
    assert sl.method == "quadratic" and sl.n_points == 4
    assert abs(sl.atm_iv - true_iv(sl.forward, sl.T, sl.forward)) < 0.003
    # flat extrapolation outside the quoted k-range
    assert sl.iv(50.0) == pytest.approx(sl.iv(sl.forward * math.exp(sl.params["kmin"])), rel=1e-12)
    ch1 = make_chain(days=(30,), strikes=np.array([105.0]))
    sl1 = build_surface(ch1).slices[expiry_for(30)]
    assert sl1.method == "flat" and sl1.n_points == 1
    assert sl1.iv(80.0) == sl1.iv(120.0) == pytest.approx(true_iv(105.0, sl1.T, sl1.forward), abs=1e-6)


def _q(expiry, K, right, bid, ask, **kw):
    return OptionQuote("X", expiry, K, right, bid, ask, **kw)


def test_ugly_data_never_raises():
    e1, e2, e3, e4 = (expiry_for(d) for d in (7, 21, 35, 49))
    quotes = [
        _q(e1, 100.0, Right.CALL, 0.0, 0.0),  # dead
        _q(e1, 100.0, Right.PUT, 2.0, 1.0),  # crossed
        _q(e2, 95.0, Right.PUT, 0.0, 0.5),  # one-sided only expiry
        _q(e2, 105.0, Right.CALL, 0.0, 0.4),
        _q(e3, 100.0, Right.CALL, 2.0, 2.2),  # single strike
        _q(e3, 100.0, Right.PUT, 1.8, 2.0),
        _q(e4, 90.0, Right.PUT, float("nan"), 1.0),
        _q(e4, 110.0, Right.CALL, 500.0, 600.0),  # above no-arbitrage bound
        _q(e4, -5.0, Right.CALL, 1.0, 1.1),
    ]
    ch = OptionChain("X", SPOT, ASOF, quotes, R, Q)
    s = build_surface(ch)
    assert e1 in s.diagnostics["skipped"]
    assert s.slices[e2].method == "flat"  # ask-only quotes give a level
    assert s.slices[e3].method == "flat" and s.slices[e3].forward == pytest.approx(100 + math.exp(R * ch.T(e3)) * 0.2)
    assert e4 in s.diagnostics["skipped"]
    assert np.isfinite(s.atm_iv_at(30))
    # empty chain and zero spot
    assert build_surface(OptionChain("X", SPOT, ASOF, [], R, Q)).slices == {}
    assert np.isnan(build_surface(OptionChain("X", SPOT, ASOF, [], R, Q)).atm_iv_at(30))
    s0 = build_surface(OptionChain("X", 0.0, ASOF, quotes, R, Q))
    assert s0.slices == {}


def test_durrleman_detects_known_arbitrage():
    # Axel Vogt's SVI example (T=1) has butterfly arbitrage
    p = dict(a=-0.0410, b=0.1331, rho=0.3060, m=0.3586, sigma=0.4153)
    k = np.linspace(-1.5, 1.5, 301)
    w, w1, w2 = svi_derivatives(k, *p.values())
    assert np.min(durrleman_g(k, w, w1, w2)) < 0
    sl = SmileSlice(expiry=expiry_for(365), T=1.0, forward=100.0, params=p, rmse=0.0, n_points=10, method="svi")
    assert butterfly_check(sl, (-1.5, 1.5)) < 0
    # and a clean slice is arbitrage free
    sl_ok = SmileSlice(expiry=expiry_for(30), T=0.1, forward=100.0, params=svi_params(0.1), rmse=0.0, n_points=10, method="svi")
    assert butterfly_check(sl_ok, (-1.0, 1.0)) > 0


def test_calendar_violation_is_reported():
    near = make_chain(days=(30,))
    low = dict(a=0.02 * 0.4, b=0.12 * 0.4, rho=-0.5, m=0.02, sigma=0.15)
    far = make_chain(days=(60,), base=low)
    ch = OptionChain("X", SPOT, ASOF, near.quotes + far.quotes, R, Q)
    s = build_surface(ch)
    d = s.diagnostics
    assert d["calendar_arbitrage"] is True
    v = d["calendar_violations"][0]
    assert v["near"] == expiry_for(30) and v["far"] == expiry_for(60) and v["dw"] < 0

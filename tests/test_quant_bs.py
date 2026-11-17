import math

import numpy as np
import pytest

from kwanttrades.quant import bs
from kwanttrades.types import Right

S0 = 100.0
KS = S0 * np.exp(np.linspace(-2.5, 2.5, 41))
TS = np.array([1e-4, 1 / 365, 0.1, 1.0, 5.0])
SIGS = np.array([0.01, 0.05, 0.2, 1.0, 3.0])


def test_cp_of():
    assert bs.cp_of(Right.CALL) == 1
    assert bs.cp_of(Right.PUT) == -1
    assert bs.cp_of("P") == -1


def test_reference_values():
    assert bs.price(100, 100, 1, 0.05, 0, 0.2, 1) == pytest.approx(10.450583572185565, abs=1e-12)
    assert bs.price(100, 100, 1, 0.05, 0, 0.2, -1) == pytest.approx(5.573526022256971, abs=1e-12)
    # Hull: S=42, K=40, r=10%, sigma=20%, T=0.5 -> call 4.76, put 0.81
    assert bs.price(42, 40, 0.5, 0.10, 0, 0.2, 1) == pytest.approx(4.7594, abs=1e-4)
    assert bs.price(42, 40, 0.5, 0.10, 0, 0.2, -1) == pytest.approx(0.8086, abs=1e-4)


def test_scalar_and_vector_returns():
    assert isinstance(bs.price(100, 100, 1, 0.0, 0.0, 0.2, 1), float)
    p = bs.price(100, KS, 1, 0.0, 0.0, 0.2, 1)
    assert p.shape == KS.shape
    g = bs.greeks(100, KS, 0.5, 0.01, 0.0, 0.2, -1)
    assert all(np.shape(v) == KS.shape for v in g.values())
    cp = np.where(KS > 100, 1, -1)
    assert bs.price(100, KS, 1, 0.0, 0.0, 0.2, cp).shape == KS.shape


@pytest.mark.parametrize("r,q", [(0.05, 0.0), (0.03, 0.02), (-0.01, 0.04)])
def test_put_call_parity(r, q):
    K, T, s = np.meshgrid(KS, TS, SIGS, indexing="ij")
    c = bs.price(S0, K, T, r, q, s, 1)
    p = bs.price(S0, K, T, r, q, s, -1)
    assert np.allclose(c - p, S0 * np.exp(-q * T) - K * np.exp(-r * T), atol=1e-9 * S0 + 1e-9 * K)


def test_stability_extremes():
    K, T, s = np.meshgrid(KS, TS, SIGS, indexing="ij")
    for cp in (1, -1):
        p = bs.price(S0, K, T, 0.03, 0.01, s, cp)
        assert np.all(np.isfinite(p))
        lower = np.maximum(cp * (S0 * np.exp(-0.01 * T) - K * np.exp(-0.03 * T)), 0)
        upper = np.where(cp > 0, S0 * np.exp(-0.01 * T), K * np.exp(-0.03 * T))
        assert np.all(p >= lower - 1e-12) and np.all(p <= upper + 1e-9)
    # T -> 0 and sigma -> 0 give intrinsic; huge sigma gives the upper bound
    assert bs.price(100, 90, 0.0, 0.05, 0, 0.2, 1) == pytest.approx(10.0)
    assert bs.price(100, 110, 1e-12, 0.05, 0, 0.2, -1) == pytest.approx(10.0, abs=1e-6)
    assert bs.price(100, 90, 1.0, 0.05, 0, 0.0, 1) == pytest.approx(100 - 90 * math.exp(-0.05))
    assert bs.price(100, 100, 1.0, 0.0, 0, 50.0, 1) == pytest.approx(100.0, abs=1e-8)
    g = bs.greeks(100, 90, 1.0, 0.05, 0.0, 0.0, 1)
    assert g["delta"] == pytest.approx(1.0) and g["gamma"] == 0.0 and g["vega"] == 0.0


@pytest.mark.parametrize("cp", [1, -1])
def test_greeks_vs_finite_differences(cp):
    K = np.array([60.0, 85.0, 100.0, 115.0, 150.0])
    T = np.array([[0.05], [0.5], [2.0]])
    r, q, s = 0.03, 0.015, 0.3
    g = bs.greeks(S0, K, T, r, q, s, cp)
    f = lambda **kw: bs.price(kw.get("S", S0), K, kw.get("T", T), kw.get("r", r), q, kw.get("s", s), cp)  # noqa: E731
    h = 1e-3
    delta = (f(S=S0 + h) - f(S=S0 - h)) / (2 * h)
    gamma = (f(S=S0 + h) - 2 * f() + f(S=S0 - h)) / h**2
    vega = (f(s=s + 1e-5) - f(s=s - 1e-5)) / 2e-5
    theta = -(f(T=T + 1e-6) - f(T=T - 1e-6)) / 2e-6 / 365.0
    rho = (f(r=r + 1e-6) - f(r=r - 1e-6)) / 2e-6
    assert np.allclose(g["delta"], delta, atol=1e-6)
    assert np.allclose(g["gamma"], gamma, atol=1e-5)
    assert np.allclose(g["vega"], vega, atol=1e-5)
    assert np.allclose(g["theta"], theta, atol=1e-6)
    assert np.allclose(g["rho"], rho, atol=1e-5)


@pytest.mark.parametrize("cp", [1, -1])
def test_implied_vol_round_trip_wide_grid(cp):
    K, T, s = np.meshgrid(KS, TS, SIGS, indexing="ij")
    r, q = 0.03, 0.01
    p = bs.price(S0, K, T, r, q, s, cp)
    iv = bs.implied_vol(p, S0, K, T, r, q, cp)
    lower = np.maximum(cp * (S0 * np.exp(-q * T) - K * np.exp(-r * T)), 0)
    tv = p - lower
    # whenever there is resolvable time value we must return a vol
    assert np.all(np.isfinite(iv[tv > 1e-10 * K]))
    ok = np.isfinite(iv)
    p2 = bs.price(S0, K, T, r, q, np.where(ok, iv, 0.2), cp)
    assert np.max(np.abs(p2 - p)[ok]) < 1e-6
    # vol itself accurate to 1e-6 where it is identifiable (non-negligible vega)
    vega = bs.greeks(S0, K, T, r, q, s, cp)["vega"]
    ident = ok & (vega > 1e-4)
    assert ident.sum() > 300
    assert np.max(np.abs(iv - s)[ident]) < 1e-6


def test_implied_vol_scalar_and_bounds():
    p = bs.price(100, 105, 0.25, 0.04, 0.01, 0.37, 1)
    assert bs.implied_vol(p, 100, 105, 0.25, 0.04, 0.01, 1) == pytest.approx(0.37, abs=1e-10)
    # below intrinsic, at intrinsic, above upper bound, negative, T=0, NaN input
    assert math.isnan(bs.implied_vol(9.0, 100, 90, 0.5, 0.0, 0.0, 1))
    assert math.isnan(bs.implied_vol(10.0, 100, 90, 0.5, 0.0, 0.0, 1))
    assert math.isnan(bs.implied_vol(100.5, 100, 90, 0.5, 0.0, 0.0, 1))
    assert math.isnan(bs.implied_vol(95.0, 100, 95, 0.5, 0.0, 0.0, -1))
    assert math.isnan(bs.implied_vol(-1.0, 100, 90, 0.5, 0.0, 0.0, -1))
    assert math.isnan(bs.implied_vol(1.0, 100, 90, 0.0, 0.0, 0.0, -1))
    assert math.isnan(bs.implied_vol(float("nan"), 100, 90, 0.5, 0.0, 0.0, -1))
    arr = bs.implied_vol(np.array([1.0, -1.0, 5.0]), 100, 100, 0.5, 0.0, 0.0, 1)
    assert np.isfinite(arr[0]) and np.isnan(arr[1]) and np.isfinite(arr[2])


def test_implied_vol_deep_wings_and_tiny_t():
    # deep OTM put with a tiny price, and a 1-hour option
    p = bs.price(100, 30, 0.1, 0.02, 0.0, 0.6, -1)
    assert 0 < p < 1e-6
    assert bs.implied_vol(p, 100, 30, 0.1, 0.02, 0.0, -1) == pytest.approx(0.6, abs=1e-7)
    T = 1 / (365 * 24)
    p = bs.price(100, 100.2, T, 0.05, 0.0, 0.25, 1)
    assert bs.implied_vol(p, 100, 100.2, T, 0.05, 0.0, 1) == pytest.approx(0.25, abs=1e-8)

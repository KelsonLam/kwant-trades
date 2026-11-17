import math

import numpy as np
import pytest

from kwanttrades.quant import american, bs


@pytest.mark.parametrize("S,K,T,r,sigma", [(100, 100, 1, 0.05, 0.2), (100, 80, 0.3, 0.02, 0.5), (50, 70, 2.0, 0.08, 0.3), (100, 100, 0.01, 0.0, 0.15)])
def test_call_without_dividends_equals_european(S, K, T, r, sigma):
    assert american.price(S, K, T, r, 0.0, sigma, 1) == pytest.approx(bs.price(S, K, T, r, 0.0, sigma, 1), abs=1e-10)


def test_reference_values():
    # S=K=100, T=1, r=5%, q=0, sigma=20% American put ~ 6.0904 (high-resolution lattice value)
    assert american.price(100, 100, 1, 0.05, 0.0, 0.2, -1) == pytest.approx(6.0904, abs=2e-3)
    # Hull: S=K=50, r=10%, sigma=40%, T=5 months American put -> 4.284 (converged binomial)
    assert american.price(50, 50, 5 / 12, 0.10, 0.0, 0.40, -1) == pytest.approx(4.284, abs=2e-3)


def test_american_dominates_european_and_premium_non_negative():
    K = np.array([70.0, 90.0, 100.0, 110.0, 140.0])
    for cp in (1, -1):
        for r, q in ((0.05, 0.0), (0.02, 0.04), (0.08, 0.03)):
            am = american.price(100, K, 0.75, r, q, 0.3, cp, steps=100)
            eu = bs.price(100, K, 0.75, r, q, 0.3, cp)
            assert np.all(am >= eu - 1e-12)
            assert np.all(am >= np.maximum(cp * (100 - K), 0) - 1e-12)
            eep = american.early_exercise_premium(100, K, 0.75, r, q, 0.3, cp, steps=100)
            assert np.all(eep >= 0)
    # deep ITM put with high rates has a material premium; call with high yield too
    assert american.early_exercise_premium(100, 150, 1.0, 0.08, 0.0, 0.2, -1) > 0.5
    assert american.early_exercise_premium(100, 60, 1.0, 0.01, 0.08, 0.2, 1) > 0.5
    assert american.price(100, 200, 1.0, 0.08, 0.0, 0.2, -1) == pytest.approx(100.0, abs=1e-5)


def test_degenerate_inputs():
    assert american.price(100, 90, 0.0, 0.05, 0, 0.2, -1) == 0.0
    assert american.price(100, 110, 0.0, 0.05, 0, 0.2, -1) == pytest.approx(10.0)
    assert american.price(100, 110, 1.0, 0.05, 0, 0.0, -1) == pytest.approx(10.0)
    assert math.isnan(american.price(-1, 110, 1.0, 0.05, 0, 0.2, -1))


def test_implied_vol_round_trip_and_bounds():
    for K, cp in ((90.0, -1), (110.0, -1), (105.0, 1)):
        p = american.price(100, K, 0.5, 0.05, 0.01, 0.33, cp)
        assert american.implied_vol(p, 100, K, 0.5, 0.05, 0.01, cp) == pytest.approx(0.33, abs=1e-5)
    assert math.isnan(american.implied_vol(9.0, 100, 110, 0.5, 0.05, 0.0, -1))  # below intrinsic
    assert math.isnan(american.implied_vol(120.0, 100, 110, 0.5, 0.05, 0.0, -1))  # above K
    v = american.implied_vol(np.array([5.0, 6.0]), 100, 100, 1.0, 0.05, 0.0, -1)
    assert v.shape == (2,) and np.all(np.isfinite(v)) and v[1] > v[0]


def test_convergence_in_steps():
    a = american.price(100, 100, 1, 0.05, 0.0, 0.2, -1, steps=101)
    b = american.price(100, 100, 1, 0.05, 0.0, 0.2, -1, steps=401)
    assert abs(a - b) < 2e-3

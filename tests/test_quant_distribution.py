import math

import numpy as np
import pytest
from scipy.stats import norm

from kwanttrades.quant import bs
from kwanttrades.quant.distribution import TerminalDist, evaluate_structure, payoff_at_expiry
from kwanttrades.types import Leg, OptionQuote, Right, Side
from tests.test_quant_synth import expiry_for

E = expiry_for(30)


def leg(K, right, side, ratio=1):
    return Leg(OptionQuote("X", E, float(K), right, 1.0, 1.1), side, ratio)


C, P, B, S_ = Right.CALL, Right.PUT, Side.BUY, Side.SELL


@pytest.mark.parametrize("mix", [0.0, 0.1, 0.3])
@pytest.mark.parametrize("drift", [0.0, 0.15, -0.2])
def test_lognormal_moments(mix, drift):
    spot, T, sigma = 100.0, 0.25, 0.3
    d = TerminalDist.lognormal(spot, T, sigma, drift=drift, kurtosis_mix=mix)
    assert d.probs.sum() == pytest.approx(1.0, abs=1e-12)
    assert np.all(d.probs >= 0) and np.all(np.diff(d.prices) > 0)
    assert d.mean == pytest.approx(spot * math.exp(drift * T), rel=1e-12)
    x = np.log(d.prices)
    mu = np.dot(x, d.probs)
    var = np.dot((x - mu) ** 2, d.probs)
    assert math.sqrt(var / T) == pytest.approx(sigma, rel=0.01)
    kurt = np.dot((x - mu) ** 4, d.probs) / var**2
    if mix == 0:
        assert kurt == pytest.approx(3.0, abs=0.02)
    else:
        assert kurt > 3.3
    assert d.expect(lambda s: np.ones_like(s)) == pytest.approx(1.0)
    assert 0 < d.prob(lambda s: s > spot) < 1


def test_degenerate_distribution():
    d = TerminalDist.lognormal(100, 0.0, 0.2, drift=0.1)
    assert d.prices.tolist() == [100.0] and d.probs.tolist() == [1.0]


def test_payoff_at_expiry():
    S = np.array([80.0, 95.0, 100.0, 105.0, 120.0])
    legs = [leg(95, P, B), leg(100, P, S_), leg(105, C, S_, 2)]
    expected = np.maximum(95 - S, 0) - np.maximum(100 - S, 0) - 2 * np.maximum(S - 105, 0)
    assert np.allclose(payoff_at_expiry(legs, S), expected)


DIST = TerminalDist.lognormal(100.0, 30 / 365, 0.25, drift=0.0, kurtosis_mix=0.0)


def test_long_call_by_hand():
    r = evaluate_structure([leg(100, C, B)], 3.0, DIST)
    assert r["max_loss"] == pytest.approx(300.0)
    assert r["max_profit"] == math.inf
    assert r["breakevens"] == [pytest.approx(103.0)]


def test_debit_vertical_by_hand():
    r = evaluate_structure([leg(100, C, B), leg(105, C, S_)], 2.0, DIST)
    assert r["max_loss"] == pytest.approx(200.0)
    assert r["max_profit"] == pytest.approx(300.0)
    assert r["breakevens"] == [pytest.approx(102.0)]


def test_credit_vertical_by_hand():
    r = evaluate_structure([leg(100, P, S_), leg(95, P, B)], -1.5, DIST)
    assert r["max_profit"] == pytest.approx(150.0)
    assert r["max_loss"] == pytest.approx(350.0)
    assert r["breakevens"] == [pytest.approx(98.5)]
    # pop = P(S > 98.5) under the lognormal (grid-discretised)
    T, s = 30 / 365, 0.25
    exact = 1 - norm.cdf((math.log(98.5 / 100) + 0.5 * s * s * T) / (s * math.sqrt(T)))
    assert r["pop"] == pytest.approx(exact, abs=5e-3)


def test_iron_condor_by_hand():
    legs = [leg(90, P, B), leg(95, P, S_), leg(105, C, S_), leg(110, C, B)]
    r = evaluate_structure(legs, -2.0, DIST)
    assert r["max_profit"] == pytest.approx(200.0)
    assert r["max_loss"] == pytest.approx(300.0)
    assert r["breakevens"] == [pytest.approx(93.0), pytest.approx(107.0)]
    r2 = evaluate_structure(legs, -2.0, DIST, cost_per_share=0.05)
    assert r2["ev"] == pytest.approx(r["ev"] - 5.0)
    assert r2["breakevens"] == [pytest.approx(93.05), pytest.approx(106.95)]


def test_naked_short_call_unbounded_loss():
    r = evaluate_structure([leg(110, C, S_)], -1.0, DIST)
    assert r["max_loss"] == math.inf and r["max_profit"] == pytest.approx(100.0)
    assert r["breakevens"] == [pytest.approx(111.0)]


def test_ev_consistent_with_black_scholes():
    spot, T, sigma = 100.0, 45 / 365, 0.3
    d = TerminalDist.lognormal(spot, T, sigma, drift=0.0, kurtosis_mix=0.0)
    px = lambda K, cp: bs.price(spot, K, T, 0.0, 0.0, sigma, cp)  # noqa: E731
    structures = [
        ([leg(100, C, B)], px(100, 1)),
        ([leg(95, P, B)], px(95, -1)),
        ([leg(100, C, B), leg(108, C, S_)], px(100, 1) - px(108, 1)),
        ([leg(100, P, S_), leg(92, P, B)], -(px(100, -1) - px(92, -1))),
        ([leg(88, P, B), leg(94, P, S_), leg(106, C, S_), leg(112, C, B)], -(px(94, -1) - px(88, -1) + px(106, 1) - px(112, 1))),
    ]
    for legs, net in structures:
        r = evaluate_structure(legs, net, d)
        assert abs(r["ev"]) < 0.2, (legs, r["ev"])  # < 0.2 cents per share
    # a positive drift makes a long call positive EV, a fat-tailed view helps a long strangle
    up = TerminalDist.lognormal(spot, T, sigma, drift=0.5, kurtosis_mix=0.0)
    assert evaluate_structure([leg(100, C, B)], px(100, 1), up)["ev"] > 50

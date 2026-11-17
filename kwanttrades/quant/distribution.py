"""Terminal price distributions under our (physical) view, and structure evaluation.

Conventions (types.py): prices per share, dollar amounts per contract-set (x 100),
``net_price > 0`` is a debit paid, ``< 0`` a credit received.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
from scipy.special import ndtr

from kwanttrades.quant.bs import cp_of
from kwanttrades.types import MULTIPLIER, Leg, Side

_HI_VOL_RATIO = 2.0  # vol of the fat-tail component relative to the core component


@dataclass
class TerminalDist:
    """Discrete distribution of the terminal price: ``prices`` (ascending) with ``probs``
    summing to 1."""

    prices: np.ndarray
    probs: np.ndarray

    @classmethod
    def lognormal(cls, spot: float, T: float, sigma: float, drift: float = 0.0, kurtosis_mix: float = 0.1, n: int = 2001) -> "TerminalDist":
        """Two-component lognormal mixture with mean ``spot * exp(drift T)``.

        With weight p = ``kurtosis_mix`` a high-vol component (vol = 2 x core vol) and with
        weight 1-p the core component; the core vol s1 is chosen so that the mixture keeps the
        requested total log-variance: (1-p) s1^2 + p (2 s1)^2 = sigma^2. Each component is
        lognormal with ln S_T ~ N(ln S + drift T - s_i^2 T/2, s_i^2 T), so each (hence the
        mixture) has mean S e^{drift T}. p = 0 gives a plain lognormal. ``drift`` is the
        annualised expected return under our view.

        Discretisation: n nodes uniform in ln S over +-8 standard deviations of the widest
        component; node probability = mixture CDF mass of its cell (midpoint boundaries, the
        outer cells extend to 0 and infinity) so probabilities sum to exactly 1. Prices are then
        rescaled by a (tiny) factor so that the discrete mean matches spot e^{drift T} exactly.
        """
        spot = float(spot)
        T = max(float(T), 0.0)
        sigma = max(float(sigma), 0.0)
        p = float(np.clip(kurtosis_mix, 0.0, 1.0))
        mean = spot * math.exp(drift * T)
        if T <= 0 or sigma <= 0 or not np.isfinite(sigma):
            return cls(np.array([mean]), np.array([1.0]))
        s1 = sigma / math.sqrt((1.0 - p) + p * _HI_VOL_RATIO**2)
        comps = [(1.0 - p, s1), (p, _HI_VOL_RATIO * s1)]
        comps = [(w, s) for w, s in comps if w > 0]
        sd_max = max(s for _, s in comps) * math.sqrt(T)
        center = math.log(mean)
        x = np.linspace(center - 8.0 * sd_max, center + 8.0 * sd_max, int(n))
        edges = np.concatenate([[-np.inf], 0.5 * (x[1:] + x[:-1]), [np.inf]])
        cdf = np.zeros_like(edges)
        for w, s in comps:
            mu = math.log(spot) + drift * T - 0.5 * s * s * T
            with np.errstate(invalid="ignore"):
                cdf += w * ndtr((edges - mu) / (s * math.sqrt(T)))
        probs = np.diff(cdf)
        probs = np.maximum(probs, 0.0)
        probs /= probs.sum()
        prices = np.exp(x)
        prices *= mean / float(np.dot(prices, probs))
        return cls(prices, probs)

    def expect(self, fn: Callable[[np.ndarray], np.ndarray]) -> float:
        """E[fn(S_T)] where fn maps an array of prices to an array of values."""
        return float(np.dot(np.asarray(fn(self.prices), dtype=float), self.probs))

    def prob(self, mask_fn: Callable[[np.ndarray], np.ndarray]) -> float:
        """P(mask_fn(S_T)) for a boolean-valued function of prices."""
        return float(np.sum(self.probs[np.asarray(mask_fn(self.prices), dtype=bool)]))

    @property
    def mean(self) -> float:
        return float(np.dot(self.prices, self.probs))


def _leg_sign(leg: Leg) -> float:
    return 1.0 if leg.side == Side.BUY else -1.0


def payoff_at_expiry(legs: list[Leg], prices) -> np.ndarray:
    """Per-share expiry payoff: sum_legs sign * ratio * max(cp (S - K), 0), sign = +1 BUY,
    -1 SELL. Does not include the premium paid/received."""
    S = np.asarray(prices, dtype=float)
    total = np.zeros_like(S)
    for leg in legs:
        cp = cp_of(leg.quote.right)
        total = total + _leg_sign(leg) * leg.ratio * np.maximum(cp * (S - leg.quote.strike), 0.0)
    return total


def evaluate_structure(legs: list[Leg], net_price: float, dist: TerminalDist, cost_per_share: float = 0.0) -> dict:
    """Evaluate a structure held to expiry.

    Per-share P&L at expiry: ``pnl(S) = payoff(S) - net_price - cost_per_share``.

    Returns (dollars per contract-set = per-share x 100):
      * ``ev``          E[pnl] x 100 under ``dist``
      * ``pop``         P(pnl > 0) under ``dist``
      * ``max_loss``    -min_S pnl(S) x 100 (>= 0; inf if unbounded)
      * ``max_profit``  max_S pnl(S) x 100 (>= 0; inf if unbounded)
      * ``breakevens``  sorted list of S where pnl crosses zero
    The payoff is piecewise linear with kinks at the strikes, so extremes and breakevens are
    computed exactly from the kink values and the slopes at S = 0 and S -> infinity (not
    from the grid). Also returned: ``ev_per_share``.
    """
    strikes = sorted({float(l.quote.strike) for l in legs})
    nodes = np.array([0.0] + strikes, dtype=float)
    base = float(net_price) + float(cost_per_share)

    def pnl(S):
        return payoff_at_expiry(legs, S) - base

    vals = pnl(nodes)
    # slope as S -> infinity: only calls contribute
    slope_inf = sum(_leg_sign(l) * l.ratio for l in legs if cp_of(l.quote.right) > 0)
    max_p = float(vals.max())
    min_p = float(vals.min())
    max_profit = math.inf if slope_inf > 1e-12 else max(max_p, 0.0) * MULTIPLIER
    max_loss = math.inf if slope_inf < -1e-12 else max(-min_p, 0.0) * MULTIPLIER

    # breakevens on each linear segment
    be: list[float] = []
    tol = 1e-12
    for i in range(len(nodes) - 1):
        x0, x1, y0, y1 = nodes[i], nodes[i + 1], vals[i], vals[i + 1]
        if abs(y0) <= tol and i > 0:
            be.append(float(x0))
        if (y0 < -tol and y1 > tol) or (y0 > tol and y1 < -tol):
            be.append(float(x0 + (x1 - x0) * (-y0) / (y1 - y0)))
    xl, yl = nodes[-1], vals[-1]
    if abs(yl) <= tol and len(nodes) > 1:
        be.append(float(xl))
    if (yl < -tol and slope_inf > tol) or (yl > tol and slope_inf < -tol):
        be.append(float(xl - yl / slope_inf))
    breakevens = sorted({round(b, 10) for b in be})

    ev_ps = dist.expect(pnl)
    pop = dist.prob(lambda S: pnl(S) > 0)
    return {
        "ev": ev_ps * MULTIPLIER,
        "ev_per_share": ev_ps,
        "pop": pop,
        "max_loss": max_loss,
        "max_profit": max_profit,
        "breakevens": breakevens,
    }

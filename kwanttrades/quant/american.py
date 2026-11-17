"""American option pricing on a Leisen-Reimer binomial tree with a European control variate.

Method
------
* Leisen-Reimer (1996) tree with Peizer-Pratt inversion (method 2); the number of steps is
  forced odd (``steps`` rounded up), which gives smooth, monotone, ~O(1/n^2) convergence.
* Control variate: ``V_amer = V_BS + (V_tree_amer - V_tree_euro)``. The same tree prices the
  European option, and the tree error is removed using the exact Black-Scholes value. For a
  call with q = 0 (r >= 0) early exercise is never optimal, the two tree values coincide and
  the result equals ``bs.price`` to machine precision.
* The tree early-exercise premium converges roughly like O(1/n) (exercise boundary), so it
  is Richardson-extrapolated from trees with n and 2n+1 steps:
  ``eep = ((2n+1) eep_{2n+1} - n eep_n) / (n+1)``. Accuracy ~1e-4 per $100 at n = 201.
* The result is floored at ``max(intrinsic, V_BS)`` (American >= European, >= exercise now).

Conventions follow :mod:`kwanttrades.quant.bs` (cp = +1 call / -1 put, continuous r and q).
All public functions accept scalars or arrays (evaluated element-wise); they return a
``float`` for scalar input.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import brentq

from kwanttrades.quant import bs


def _pp2(z: float, n: int) -> float:
    """Peizer-Pratt inversion (method 2): binomial probability matching N(z) for n steps."""
    if z == 0.0:
        return 0.5
    t = z / (n + 1.0 / 3.0 + 0.1 / (n + 1.0))
    return 0.5 + math.copysign(0.5, z) * math.sqrt(max(0.0, 1.0 - math.exp(-t * t * (n + 1.0 / 6.0))))


def _lr_tree(S: float, K: float, T: float, r: float, q: float, sigma: float, cp: int, n: int) -> tuple[float, float]:
    """Return (american, european) values from one Leisen-Reimer tree with n (odd) steps."""
    dt = T / n
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q) * T + 0.5 * v * v) / v
    d2 = d1 - v
    pu = _pp2(d2, n)
    pstar = _pp2(d1, n)
    growth = math.exp((r - q) * dt)
    if 1e-10 < pu < 1 - 1e-10 and 1e-10 < pstar < 1 - 1e-10:
        u = growth * pstar / pu
        d = (growth - pu * u) / (1.0 - pu)
    else:
        # extreme moneyness: LR probabilities saturate, use a drift-centred CRR lattice
        sdt = sigma * math.sqrt(dt)
        u = growth * math.exp(sdt)
        d = growth * math.exp(-sdt)
        pu = (growth - d) / (u - d)
    disc = math.exp(-r * dt)
    a, b = disc * pu, disc * (1.0 - pu)
    j = np.arange(n + 1, dtype=float)
    ST = S * np.exp(j * math.log(u) + (n - j) * math.log(d))
    payoff = np.maximum(cp * (ST - K), 0.0)
    Va = payoff.copy()
    Ve = payoff
    for _ in range(n):
        ST = ST[:-1] / d
        Ve = a * Ve[1:] + b * Ve[:-1]
        Va = np.maximum(a * Va[1:] + b * Va[:-1], cp * (ST - K))
    return float(Va[0]), float(Ve[0])


def _price_scalar(S, K, T, r, q, sigma, cp, steps) -> float:
    cp = 1 if cp >= 0 else -1
    if not all(np.isfinite([S, K, T, r, q, sigma])) or S <= 0 or K <= 0:
        return float("nan")
    intrinsic = max(cp * (S - K), 0.0)
    if T <= 0:
        return intrinsic
    euro = bs.price(S, K, T, r, q, max(sigma, 0.0), cp)
    if sigma <= 0 or sigma * math.sqrt(T) < 1e-8:
        # deterministic limit: compare exercising now with holding to expiry
        return max(intrinsic, euro)
    n = int(steps)
    n = max(n, 3)
    if n % 2 == 0:
        n += 1
    a1, e1 = _lr_tree(S, K, T, r, q, sigma, cp, n)
    m = 2 * n + 1
    a2, e2 = _lr_tree(S, K, T, r, q, sigma, cp, m)
    # early-exercise premium on the tree converges ~O(1/n): Richardson-extrapolate it
    eep = (m * (a2 - e2) - n * (a1 - e1)) / (m - n)
    val = euro + eep
    return max(val, intrinsic, euro)


def _vectorize(fn, args, steps):
    scalar = all(np.ndim(a) == 0 for a in args)
    arrs = np.broadcast_arrays(*[np.asarray(a, dtype=float) for a in args])
    out = np.empty(arrs[0].shape)
    it = np.nditer(arrs, flags=["multi_index", "refs_ok"])
    for vals in it:
        out[it.multi_index] = fn(*[float(x) for x in vals], steps)
    return float(out.reshape(-1)[0]) if scalar else out


def price(S, K, T, r, q, sigma, cp, steps: int = 200):
    """American option price per share (Leisen-Reimer tree + control variate, see module doc)."""
    return _vectorize(_price_scalar, (S, K, T, r, q, sigma, cp), steps)


def early_exercise_premium(S, K, T, r, q, sigma, cp, steps: int = 200):
    """American minus European (BSM) value, floored at 0."""
    am = price(S, K, T, r, q, sigma, cp, steps)
    eu = bs.price(S, K, T, r, q, sigma, cp)
    res = np.maximum(np.asarray(am) - np.asarray(eu), 0.0)
    return float(res) if np.ndim(res) == 0 else res


def _iv_scalar(p, S, K, T, r, q, cp, steps) -> float:
    cp = 1 if cp >= 0 else -1
    if not all(np.isfinite([p, S, K, T, r, q])) or T <= 0 or S <= 0 or K <= 0:
        return float("nan")
    intrinsic = max(cp * (S - K), 0.0)
    upper = S if cp > 0 else K
    lo_s, hi_s = 1e-4, 5.0
    f = lambda s: _price_scalar(S, K, T, r, q, s, cp, steps) - p  # noqa: E731
    if p <= intrinsic or p >= upper:
        return float("nan")
    f_lo, f_hi = f(lo_s), f(hi_s)
    if f_lo > 0 or f_hi < 0:
        return float("nan")
    return float(brentq(f, lo_s, hi_s, xtol=1e-8, rtol=1e-10, maxiter=200))


def implied_vol(price_, S, K, T, r, q, cp, steps: int = 200):
    """Implied vol that reproduces an American price (Brent's method on sigma in [1e-4, 5]).

    NaN when the price is at/below immediate-exercise intrinsic, at/above the upper bound
    (S for calls, K for puts), or not attainable within the sigma bracket.
    """
    return _vectorize(_iv_scalar, (price_, S, K, T, r, q, cp), steps)

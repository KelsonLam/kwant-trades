"""Black-Scholes-Merton pricing, Greeks and implied volatility (vectorised).

Conventions
-----------
* ``cp`` is +1 for calls and -1 for puts (scalar or array).
* Continuous risk-free rate ``r`` and continuous dividend/borrow yield ``q``.
* ``T`` in years (ACT/365), ``sigma`` annualised.
* All functions broadcast numpy-style over every argument and return a Python ``float``
  when every input is a scalar, else an ``ndarray``.

Formulas
--------
With forward ``F = S e^{(r-q)T}``, discount ``D = e^{-rT}`` and total vol ``v = sigma sqrt(T)``::

    d1 = (ln(F/K) + v^2/2) / v,   d2 = d1 - v
    V  = cp * (S e^{-qT} N(cp d1) - K e^{-rT} N(cp d2))

When ``v`` is (numerically) zero the price is the discounted forward intrinsic
``max(cp (S e^{-qT} - K e^{-rT}), 0)``.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.special import log_ndtr, ndtr

from kwanttrades.types import Right

_LOG_SQRT_2PI = 0.5 * math.log(2.0 * math.pi)
_TINY_V = 1e-12  # total vol below which the option is treated as worth its intrinsic


def cp_of(right: Right) -> int:
    """+1 for a call, -1 for a put. Accepts ``Right`` or its string value ("C"/"P")."""
    value = right.value if isinstance(right, Right) else str(right).upper()
    if value in ("C", "CALL"):
        return 1
    if value in ("P", "PUT"):
        return -1
    raise ValueError(f"unknown option right: {right!r}")


def _prep(*args):
    scalar = all(np.ndim(a) == 0 for a in args)
    arrs = np.broadcast_arrays(*[np.asarray(a, dtype=float) for a in args])
    return scalar, [np.array(a, dtype=float) for a in arrs]


def _ret(x, scalar):
    if scalar:
        return float(np.asarray(x).reshape(-1)[0])
    return x


def price(S, K, T, r, q, sigma, cp):
    """Black-Scholes-Merton price per share (see module docstring)."""
    scalar, (S, K, T, r, q, sigma, cp) = _prep(S, K, T, r, q, sigma, cp)
    cp = np.where(cp >= 0, 1.0, -1.0)
    T = np.maximum(T, 0.0)
    sigma = np.maximum(sigma, 0.0)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        dfr = np.exp(-r * T)
        dfq = np.exp(-q * T)
        v = sigma * np.sqrt(T)
        tiny = v < _TINY_V
        vs = np.where(tiny, 1.0, v)
        d1 = (np.log(S / K) + (r - q) * T + 0.5 * vs * vs) / vs
        d2 = d1 - vs
        p = cp * (S * dfq * ndtr(cp * d1) - K * dfr * ndtr(cp * d2))
        intrinsic = np.maximum(cp * (S * dfq - K * dfr), 0.0)
        p = np.where(tiny, intrinsic, np.maximum(p, intrinsic))
    return _ret(p, scalar)


def greeks(S, K, T, r, q, sigma, cp) -> dict:
    """Analytic BSM Greeks.

    Keys and units:
      * ``delta`` = dV/dS = cp e^{-qT} N(cp d1)
      * ``gamma`` = e^{-qT} n(d1) / (S v)
      * ``vega``  = S e^{-qT} n(d1) sqrt(T), per 1.00 (=100 vol points) of sigma
      * ``theta`` = -dV/dT per CALENDAR DAY (annual theta / 365):
        [-S e^{-qT} n(d1) sigma / (2 sqrt T) - cp r K e^{-rT} N(cp d2) + cp q S e^{-qT} N(cp d1)] / 365
      * ``rho``   = dV/dr = cp K T e^{-rT} N(cp d2), per 1.00 of rate
    """
    scalar, (S, K, T, r, q, sigma, cp) = _prep(S, K, T, r, q, sigma, cp)
    cp = np.where(cp >= 0, 1.0, -1.0)
    T = np.maximum(T, 0.0)
    sigma = np.maximum(sigma, 0.0)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        dfr = np.exp(-r * T)
        dfq = np.exp(-q * T)
        sqT = np.sqrt(T)
        v = sigma * sqT
        tiny = v < _TINY_V
        vs = np.where(tiny, 1.0, v)
        d1 = (np.log(S / K) + (r - q) * T + 0.5 * vs * vs) / vs
        d2 = d1 - vs
        pdf = np.exp(-0.5 * d1 * d1 - _LOG_SQRT_2PI)
        Nd1 = ndtr(cp * d1)
        Nd2 = ndtr(cp * d2)
        itm = (cp * (S * dfq - K * dfr) > 0).astype(float)
        pdf = np.where(tiny, 0.0, pdf)
        Nd1 = np.where(tiny, itm, Nd1)
        Nd2 = np.where(tiny, itm, Nd2)
        delta = cp * dfq * Nd1
        gamma = np.where(tiny, 0.0, dfq * pdf / (S * vs))
        vega = S * dfq * pdf * sqT
        decay = np.where(tiny | (sqT == 0), 0.0, -S * dfq * pdf * sigma / (2.0 * np.where(sqT == 0, 1.0, sqT)))
        theta_y = decay - cp * r * K * dfr * Nd2 + cp * q * S * dfq * Nd1
        rho = cp * K * T * dfr * Nd2
    return {
        "delta": _ret(delta, scalar),
        "gamma": _ret(gamma, scalar),
        "vega": _ret(vega, scalar),
        "theta": _ret(theta_y / 365.0, scalar),
        "rho": _ret(rho, scalar),
    }


# ----------------------------------------------------------------------------- implied vol


def _log1mexp(z):
    """log(1 - e^z) for z <= 0, numerically stable."""
    z = np.minimum(z, -1e-300)
    return np.where(z > -math.log(2.0), np.log(-np.expm1(z)), np.log1p(-np.exp(z)))


def _log_norm_otm(x, v, theta):
    """log of the normalised OTM Black price b(x, v) = theta[e^{x/2} N(theta d1) - e^{-x/2} N(theta d2)],
    with x = ln(F/K), d1,2 = x/v +- v/2 and theta*x <= 0 (out of the money). Price = D sqrt(FK) b."""
    d1 = x / v + 0.5 * v
    d2 = d1 - v
    A = np.where(theta > 0, 0.5 * x + log_ndtr(d1), -0.5 * x + log_ndtr(-d2))
    B = np.where(theta > 0, -0.5 * x + log_ndtr(d2), 0.5 * x + log_ndtr(-d1))
    return A + _log1mexp(B - A)


def _log_norm_vega(x, v):
    """log of db/dv = e^{x/2} n(x/v + v/2)."""
    d1 = x / v + 0.5 * v
    return 0.5 * x - 0.5 * d1 * d1 - _LOG_SQRT_2PI


def implied_vol(price_, S, K, T, r, q, cp, max_iter: int = 100):
    """Black-Scholes-Merton implied volatility.

    Returns NaN where the price violates the no-arbitrage bounds
    ``max(cp (S e^{-qT} - K e^{-rT}), 0) < price < (S e^{-qT} if call else K e^{-rT})``
    (prices exactly at intrinsic carry no volatility information and also give NaN), or
    where inputs are non-finite / T <= 0.

    Method: the time value ``price - intrinsic`` is, by put-call parity, exactly the price
    of the out-of-the-money option at the same strike, so we always solve on the OTM option
    (no cancellation for deep ITM quotes). In normalised Black form
    ``b(x, v) = tv / (D sqrt(F K))`` with ``x = ln(F/K)`` and total vol ``v = sigma sqrt(T)``,
    we run Newton on ``ln b(v) - ln b_target`` (log-space keeps deep OTM wings well
    conditioned, evaluated with ``log_ndtr``), safeguarded by a bracket that falls back to
    bisection whenever a Newton step leaves it. Converges to ~1e-14 relative in v.
    """
    scalar, (P, S, K, T, r, q, cp) = _prep(price_, S, K, T, r, q, cp)
    cp = np.where(cp >= 0, 1.0, -1.0)
    out = np.full(P.shape, np.nan)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore", under="ignore"):
        valid = np.isfinite(P) & np.isfinite(S) & np.isfinite(K) & np.isfinite(T) & np.isfinite(r) & np.isfinite(q)
        valid &= (T > 0) & (S > 0) & (K > 0)
        D = np.exp(-r * T)
        F = S * np.exp((r - q) * T)
        intrinsic = np.maximum(cp * D * (F - K), 0.0)
        upper = np.where(cp > 0, D * F, D * K)
        tv = P - intrinsic
        x = np.log(F / K)
        theta = np.where(x > 0, -1.0, 1.0)  # OTM right: call if K >= F, put if K < F
        beta = tv / (D * np.sqrt(F * K))
        bmax = np.exp(-0.5 * np.abs(x))
        valid &= (tv > 0) & (P < upper) & (beta < bmax) & np.isfinite(beta)
        if not np.any(valid):
            return _ret(out, scalar)
        xv, th, lb = x[valid], theta[valid], np.log(beta[valid])

        # bracket [lo, hi] with f(lo) < 0 < f(hi)
        lo = np.zeros_like(xv)
        hi = np.maximum(1.0, 2.0 * np.sqrt(2.0 * np.abs(xv)))
        for _ in range(80):
            f_hi = _log_norm_otm(xv, hi, th) - lb
            need = ~(f_hi > 0)
            if not np.any(need):
                break
            lo = np.where(need, hi, lo)
            hi = np.where(need, hi * 2.0, hi)
        # start at the vega-maximising point sqrt(2|x|) (or a small-v ATM guess)
        v = np.sqrt(2.0 * np.abs(xv))
        v = np.where(v < 1e-8, np.exp(lb) * math.sqrt(2 * math.pi), v)
        v = np.where((v > lo) & (v < hi), v, 0.5 * (lo + hi))
        done = np.zeros_like(xv, dtype=bool)
        for _ in range(max_iter):
            lbv = _log_norm_otm(xv, v, th)
            f = lbv - lb
            lo = np.where(f < 0, v, lo)
            hi = np.where(f > 0, v, hi)
            fp = np.exp(_log_norm_vega(xv, v) - lbv)
            step = f / fp
            vn = v - step
            exact = np.abs(f) < 1e-15
            conv = exact | (np.abs(step) <= 1e-15 * v) | ((hi - lo) <= 4e-16 * np.maximum(hi, 1e-300))
            bad = ~np.isfinite(vn) | ~(vn >= lo) | ~(vn <= hi)
            vn = np.where(bad, 0.5 * (lo + hi), vn)
            vn = np.where(conv, v, vn)
            v = np.where(done, v, vn)
            done |= conv
            if np.all(done):
                break
        sig = v / np.sqrt(T[valid])
        out[valid] = np.where(np.isfinite(sig) & (sig > 0), sig, np.nan)
    return _ret(out, scalar)

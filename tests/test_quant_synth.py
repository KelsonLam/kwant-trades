"""Synthetic option-chain generator shared by the quant tests (plus self-checks).

Quotes are generated from a known raw-SVI surface w(k, T) = T * w1(k): European Black
prices on the carry forward F = S e^{(r-q)T}, with symmetric bid/ask around the model price
(so the mid is exactly on the model when ``tick=None``).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np

from kwanttrades.quant import bs
from kwanttrades.quant.surface import svi_total_var
from kwanttrades.types import NY, OptionChain, OptionQuote, Right

ASOF = datetime(2026, 11, 16, 10, 0, tzinfo=NY)
SPOT = 100.0
R = 0.04
Q = 0.01
# per-year raw SVI parameters; slice params = (a T, b T, rho, m, s)
SVI1 = dict(a=0.02, b=0.12, rho=-0.5, m=0.02, sigma=0.15)


def svi_params(T: float, base: dict | None = None) -> dict:
    p = dict(base or SVI1)
    return dict(a=p["a"] * T, b=p["b"] * T, rho=p["rho"], m=p["m"], sigma=p["sigma"])


def true_iv(K, T: float, F: float, base: dict | None = None):
    p = svi_params(T, base)
    k = np.log(np.asarray(K, dtype=float) / F)
    return np.sqrt(svi_total_var(k, p["a"], p["b"], p["rho"], p["m"], p["sigma"]) / T)


def half_spread(price: np.ndarray) -> np.ndarray:
    return np.clip(0.01 + 0.015 * price, 0.01, 0.15)


def make_chain(
    days=(14, 30, 60),
    strikes=None,
    noise_vol: float = 0.0,
    seed: int = 0,
    tick: float | None = None,
    base: dict | None = None,
    spot: float = SPOT,
    r: float = R,
    q: float = Q,
) -> OptionChain:
    """Build an OptionChain from the known SVI surface. ``noise_vol`` adds iid N(0, noise_vol)
    vol noise to the mid; ``tick`` rounds bid down / ask up to the tick."""
    rng = np.random.default_rng(seed)
    strikes = np.arange(80.0, 121.0, 1.0) if strikes is None else np.asarray(strikes, dtype=float)
    quotes = []
    for d in days:
        expiry = (ASOF + timedelta(days=d)).date()
        T = OptionChain("X", spot, ASOF, [], r, q).T(expiry)
        F = spot * np.exp((r - q) * T)
        iv = true_iv(strikes, T, F, base)
        if noise_vol:
            iv = iv + rng.normal(0, noise_vol, iv.shape)
        for cp, right in ((1, Right.CALL), (-1, Right.PUT)):
            px = np.asarray(bs.price(spot, strikes, T, r, q, iv, cp))
            hs = half_spread(px)
            bid = px - hs
            ask = px + hs
            if tick:
                bid = np.floor(bid / tick + 1e-9) * tick
                ask = np.ceil(ask / tick - 1e-9) * tick
            bid = np.maximum(bid, 0.0)
            for K, b, a in zip(strikes, bid, ask):
                quotes.append(OptionQuote("X", expiry, float(K), right, float(b), float(a), volume=50, open_interest=500))
    return OptionChain("X", spot, ASOF, quotes, r, q)


def expiry_for(days: int) -> date:
    return (ASOF + timedelta(days=days)).date()


# ----------------------------------------------------------------------------- self-checks


def test_synthetic_mid_is_on_model():
    ch = make_chain(days=(30,))
    e = expiry_for(30)
    T = ch.T(e)
    F = SPOT * np.exp((R - Q) * T)
    for q in ch.for_expiry(e):
        if q.bid > 0:
            iv = bs.implied_vol(q.mid, SPOT, q.strike, T, R, Q, bs.cp_of(q.right))
            assert abs(iv - true_iv(q.strike, T, F)) < 1e-8


def test_synthetic_surface_has_no_calendar_arbitrage():
    k = np.linspace(-0.5, 0.5, 51)
    w = [svi_total_var(k, *svi_params(T).values()) for T in (0.05, 0.1, 0.2)]
    assert np.all(np.diff(np.array(w), axis=0) > 0)

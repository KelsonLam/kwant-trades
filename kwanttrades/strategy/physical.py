"""Physical terminal distribution that keeps the market's skew SHAPE.

Why: a symmetric lognormal view always "finds" edge in selling OTM puts, because the market
prices crash risk into the put wing (skew). That is a systematic model bias, not an edge. Here
our view is allowed to differ from the market only in volatility LEVEL (``vol_scale``) and
DIRECTION (``mean_shift_log``); the shape of the distribution is the market's own.

Method (``physical_dist``)
--------------------------
1. Strike grid uniform in K over ``ln(K/F)`` in +-L, L = 8 x the widest scaled vol x sqrt(T)
   (capped at 3), ``N_GRID`` points.
2. IV at each grid strike = slice IV at log-moneyness clipped to the slice's FITTED strike range
   (wing IV held flat beyond the observed strikes: no extrapolated smile), times ``vol_scale``
   (= sigma_phys / atm_iv) so the whole smile moves to our level while keeping its shape.
3. Undiscounted Black call prices on the forward (``bs.price(F, K, T, 0, 0, iv, +1)``) and the
   Breeden-Litzenberger density: node mass = second difference of C in K times the spacing; the
   two end nodes take the tail masses 1 + C'(K_min) and -C'(K_max), so masses sum to exactly 1.
4. Usable only if the fitted slice passes the butterfly check (Durrleman, on the fitted strike
   range we actually use - see ``fitted_range_butterfly_violation``), has >= ``MIN_POINTS`` fitted
   points, method "svi" or "quadratic", and total negative mass <= ``MAX_NEG_MASS``; tiny
   negatives are then clipped and the masses renormalised.
5. Prices are rescaled so the mean equals F exactly, then multiplied by exp(mean_shift_log), so
   the mean is F * exp(mean_shift_log).
Otherwise ``TerminalDist.lognormal(F, T, atm_iv * vol_scale, drift=mean_shift_log / T)``
(same mean). The path used is recorded on the returned object as ``dist.source`` ("implied" or
"lognormal") and ``dist.reason`` (why the fallback was taken, "" otherwise).
"""
from __future__ import annotations

import math

import numpy as np

from kwanttrades.quant import bs
from kwanttrades.quant.distribution import TerminalDist
from kwanttrades.quant.surface import SmileSlice, butterfly_check
from kwanttrades.strategy.structures import fitted_range

N_GRID = 4001
MIN_POINTS = 8
MAX_NEG_MASS = 0.02
WING_DECAY = 0.10  # log-moneyness over which the wing slope fades to flat beyond the fitted range
SOURCE_TEXT = {
    "implied": "market-implied skew, scaled to our volatility forecast",
    "lognormal": "lognormal fallback",
}


def _tag(dist: TerminalDist, source: str, reason: str = "") -> TerminalDist:
    dist.source = source  # type: ignore[attr-defined]
    dist.reason = reason  # type: ignore[attr-defined]
    return dist


def lognormal_fallback(slice_: SmileSlice | None, forward: float, T: float, vol_scale: float, mean_shift_log: float, reason: str, kurtosis_mix: float = 0.1, atm_iv: float | None = None) -> TerminalDist:
    atm = atm_iv if atm_iv is not None else (float(slice_.atm_iv) if slice_ is not None else float("nan"))
    sigma = max(atm * vol_scale, 1e-6) if np.isfinite(atm) else 0.2
    drift = mean_shift_log / T if T > 0 else 0.0
    return _tag(TerminalDist.lognormal(forward, T, sigma, drift=drift, kurtosis_mix=kurtosis_mix), "lognormal", reason)


def wing_flat_iv(slice_: SmileSlice, k: np.ndarray, k_lo: float, k_hi: float, w: float = WING_DECAY) -> np.ndarray:
    """Slice IV inside [k_lo, k_hi]; beyond it the IV goes flat, smoothly.

    A hard flat clip puts a slope kink in the smile at the last observed strike, and a kink is
    a delta function of NEGATIVE density in Breeden-Litzenberger (clipping it then fattens the
    tail and inflates every option's value). Beyond the range the wing slope s therefore decays
    exponentially over ``w`` of log-moneyness: iv = iv_edge + s*w*(1 - exp(-|dk|/w)) * sign,
    which is C1 at the edge and flat (at iv_edge + |s| w) further out."""
    k = np.asarray(k, dtype=float)
    out = np.asarray(slice_.iv_k(np.clip(k, k_lo, k_hi)), dtype=float).copy()
    h = 1e-4
    for edge, side in ((k_lo, -1.0), (k_hi, 1.0)):
        mask = (k < edge) if side < 0 else (k > edge)
        if not mask.any():
            continue
        s = (float(slice_.iv_k(edge + h)) - float(slice_.iv_k(edge - h))) / (2 * h)  # dIV/dk at edge
        x = np.abs(k[mask] - edge)
        out[mask] = float(slice_.iv_k(edge)) + side * s * w * (1.0 - np.exp(-x / w))
    return np.maximum(out, 1e-4)


def _fold_negatives(mass: np.ndarray, max_pass: int = 200) -> np.ndarray:
    """Remove small negative masses by spreading each equally onto its two neighbours: total
    mass and mean are preserved exactly on the uniform grid (variance changes by m h^2), unlike
    clip-and-renormalise which adds tail mass. Any remainder is clipped."""
    m = mass.copy()
    for _ in range(max_pass):
        neg = np.where(m[1:-1] < 0)[0] + 1
        if neg.size == 0:
            break
        half = 0.5 * m[neg]
        m[neg] = 0.0
        np.add.at(m, neg - 1, half)
        np.add.at(m, neg + 1, half)
    return np.maximum(m, 0.0)


def fitted_range_butterfly_violation(slice_: SmileSlice) -> bool:
    """Durrleman g(k) < 0 anywhere on the FITTED strike range - the only part of the smile used
    here (beyond it the wings are flattened). ``slice.butterfly_arbitrage`` checks the k-range of
    every quote, including far, illiquid strikes where the SVI is pure extrapolation; on the real
    SPY fixture that flags the 1-4 DTE slices even though the fitted part is arbitrage-free.
    A slice whose own flag is clear is accepted directly."""
    if not slice_.butterfly_arbitrage:
        return False
    lo, hi = fitted_range(slice_)
    if not (np.isfinite(lo) and np.isfinite(hi) and hi > lo):
        return True
    g = butterfly_check(slice_, (math.log(lo / slice_.forward), math.log(hi / slice_.forward)))
    return not (np.isfinite(g) and g >= -1e-6)


def implied_density(slice_: SmileSlice, forward: float, T: float, vol_scale: float, n: int = N_GRID):
    """(strikes, masses, negative_mass) from Breeden-Litzenberger on the scaled smile."""
    lo, hi = fitted_range(slice_)
    if not (np.isfinite(lo) and np.isfinite(hi) and hi > lo):
        raise ValueError("no fitted strike range")
    k_lo, k_hi = math.log(lo / forward), math.log(hi / forward)
    wing = max(float(slice_.iv_k(k_lo)), float(slice_.iv_k(k_hi)), float(slice_.atm_iv)) * vol_scale
    L = min(8.0 * wing * math.sqrt(T), 3.0)
    K = np.linspace(forward * math.exp(-L), forward * math.exp(L), int(n))
    iv = wing_flat_iv(slice_, np.log(K / forward), k_lo, k_hi) * vol_scale
    if not np.all(np.isfinite(iv)) or np.any(iv <= 0):
        raise ValueError("non-finite smile")
    C = np.asarray(bs.price(forward, K, T, 0.0, 0.0, iv, 1.0), dtype=float)
    dC = np.diff(C) / np.diff(K)  # slope on each cell, -1..0
    mass = np.empty_like(K)
    mass[1:-1] = dC[1:] - dC[:-1]
    mass[0] = 1.0 + dC[0]
    mass[-1] = -dC[-1]
    neg = float(-mass[mass < 0].sum())
    return K, mass, neg


def physical_dist(slice_: SmileSlice | None, spot_or_forward: float, T: float, vol_scale: float, mean_shift_log: float, kurtosis_mix: float = 0.1) -> TerminalDist:
    """Terminal distribution with the market's skew shape at our vol level and drift.

    ``spot_or_forward`` is the price the (unshifted) distribution is centred on - pass the
    slice FORWARD so a zero view reproduces the market (EV of any structure at mid ~ 0)."""
    F = float(spot_or_forward)
    vol_scale = float(vol_scale) if np.isfinite(vol_scale) and vol_scale > 0 else 1.0
    try:
        if slice_ is None:
            return lognormal_fallback(None, F, T, vol_scale, mean_shift_log, "no slice", kurtosis_mix, atm_iv=0.2)
        if slice_.method not in ("svi", "quadratic"):
            return lognormal_fallback(slice_, F, T, vol_scale, mean_shift_log, f"slice method {slice_.method}", kurtosis_mix)
        if slice_.n_points < MIN_POINTS:
            return lognormal_fallback(slice_, F, T, vol_scale, mean_shift_log, f"only {slice_.n_points} fitted points", kurtosis_mix)
        if fitted_range_butterfly_violation(slice_):
            return lognormal_fallback(slice_, F, T, vol_scale, mean_shift_log, "slice fails butterfly check", kurtosis_mix)
        K, mass, neg = implied_density(slice_, F, T, vol_scale)
        if neg > MAX_NEG_MASS:
            return lognormal_fallback(slice_, F, T, vol_scale, mean_shift_log, f"negative density mass {neg:.1%}", kurtosis_mix)
        mass = _fold_negatives(mass)
        tot = mass.sum()
        if not np.isfinite(tot) or tot <= 0:
            return lognormal_fallback(slice_, F, T, vol_scale, mean_shift_log, "empty density", kurtosis_mix)
        probs = mass / tot
        keep = probs > 0
        prices, probs = K[keep], probs[keep]
        prices = prices * (F / float(np.dot(prices, probs))) * math.exp(mean_shift_log)
        return _tag(TerminalDist(prices, probs), "implied")
    except Exception as exc:  # noqa: BLE001 - never fail a symbol on a density problem
        return lognormal_fallback(slice_, F, T, vol_scale, mean_shift_log, f"error: {exc!r}", kurtosis_mix)


__all__ = ["MAX_NEG_MASS", "MIN_POINTS", "N_GRID", "SOURCE_TEXT", "implied_density", "lognormal_fallback", "physical_dist"]

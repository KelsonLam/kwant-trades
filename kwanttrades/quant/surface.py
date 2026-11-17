"""Implied volatility surface: per-expiry raw-SVI smiles with arbitrage diagnostics.

Pipeline per expiry
-------------------
1. **Forward.** ``F = median(K + e^{rT}(C_mid - P_mid))`` over the (up to 5) strikes nearest
   the carry forward ``S e^{(r-q)T}`` where both call and put have two-sided markets.
   Falls back to the carry forward when no such pair exists or the implied value is more
   than 10% away from it (``diagnostics["forward_source"]``).
2. **Quotes.** Implied vols (European Black on the forward: ``bs.implied_vol`` with
   ``S = F, q = r``) from the mid, bid and ask of every quote. Only out-of-the-money quotes
   enter the fit (puts with K < F, calls with K >= F) and they are liquidity filtered:
   two-sided market, bid >= ``cfg.min_bid``, spread/mid <= max(4 * ``cfg.max_spread_pct``, 0.5).
   If that leaves fewer than 5 points, any two-sided OTM quote is admitted; if there are no
   two-sided quotes at all, one-sided OTM quotes (mid = ask/last) are used for a flat level.
3. **Fit.** Raw SVI total variance ``w(k) = a + b(rho(k-m) + sqrt((k-m)^2 + s^2))`` with
   ``k = ln(K/F)``, fitted by bounded weighted least squares on implied vols:
   residual_i = (iv_svi(k_i) - iv_mid_i) * vega_i / spread_i, i.e. the price error in units of
   the bid/ask spread (weights ~ vega/spread, clipped to [0.05, 20] x median). A soft-L1 loss
   (scale = one spread) makes the fit robust to stale/crossed outliers. Parametrisation uses
   ``wmin = a + b s sqrt(1-rho^2) >= 0`` (non-negative variance) with box bounds on
   (wmin, b, rho, m, s); soft penalties enforce Durrleman g(k) >= 0 on a grid and Lee's wing
   bound b(1+|rho|) <= 2. Multi-start (several rho/m/s seeds), best cost wins.
4. **Checks.** Butterfly: Durrleman
   ``g(k) = (1 - k w'/(2w))^2 - (w'^2/4)(1/w + 1/4) + w''/2 >= 0`` on a k-grid.
   Calendar: total variance non-decreasing in T at fixed k between consecutive expiries.
5. **Fallbacks.** < 5 usable points: quadratic-in-k implied vol (needs >= 3 points; flat
   extrapolation outside the quoted k-range), else flat ATM vol (1-2 points). An SVI fit that
   fails or is non-finite also falls back to the quadratic. Nothing in here raises on bad
   data: an expiry with no usable quote at all is skipped and reported in diagnostics.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd
from scipy.optimize import least_squares

from kwanttrades.config import ScanConfig
from kwanttrades.quant import bs
from kwanttrades.types import OptionChain, OptionQuote, Right, year_fraction

_MIN_SVI_POINTS = 5
_G_TOL = -1e-6  # Durrleman g below this => butterfly arbitrage flagged
_CAL_TOL = 1e-6  # total-variance decrease beyond this => calendar arbitrage flagged


# ----------------------------------------------------------------------------- SVI helpers


def svi_total_var(k, a, b, rho, m, s):
    """Raw SVI total implied variance w(k) = a + b(rho(k-m) + sqrt((k-m)^2 + s^2))."""
    k = np.asarray(k, dtype=float)
    d = k - m
    return a + b * (rho * d + np.sqrt(d * d + s * s))


def svi_derivatives(k, a, b, rho, m, s):
    """(w, w', w'') of raw SVI at k."""
    k = np.asarray(k, dtype=float)
    d = k - m
    root = np.sqrt(d * d + s * s)
    w = a + b * (rho * d + root)
    w1 = b * (rho + d / root)
    w2 = b * s * s / root**3
    return w, w1, w2


def durrleman_g(k, w, w1, w2):
    """Durrleman's butterfly condition g(k) = (1 - k w'/(2w))^2 - (w'^2/4)(1/w + 1/4) + w''/2.

    g >= 0 everywhere <=> the implied risk-neutral density is non-negative (no butterfly
    arbitrage). Inputs are total variance and its first two derivatives in k."""
    k = np.asarray(k, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (1.0 - k * w1 / (2.0 * w)) ** 2 - 0.25 * w1 * w1 * (1.0 / w + 0.25) + 0.5 * w2


# ----------------------------------------------------------------------------- data classes


@dataclass
class SmileSlice:
    """One expiry's fitted smile.

    ``params``: for "svi" {a, b, rho, m, sigma}; for "quadratic" {c0, c1, c2, kmin, kmax}
    (iv(k) = c0 + c1 k + c2 k^2 with k clipped to [kmin, kmax]); for "flat" {atm_iv}.
    ``table``: one row per quote of the expiry with columns quote, strike, right, cp, k, bid,
    ask, mid, otm, in_fit, market_iv (from mid), bid_iv, ask_iv, fit_iv, weight.
    ``rmse``: root-mean-square of (fit_iv - market_iv) over the fitted points, vol units.
    """

    expiry: date
    T: float
    forward: float
    params: dict
    rmse: float
    n_points: int
    method: str  # "svi" | "quadratic" | "flat"
    rate: float = 0.0
    table: pd.DataFrame = field(default_factory=pd.DataFrame, repr=False)
    min_g: float = float("nan")
    butterfly_arbitrage: bool = False

    @property
    def quotes_table(self) -> pd.DataFrame:
        return self.table

    def total_var(self, k):
        """Total implied variance w(k) = iv(k)^2 T at log-moneyness k = ln(K/F)."""
        scalar = np.ndim(k) == 0
        k = np.asarray(k, dtype=float)
        p = self.params
        if self.method == "svi":
            w = svi_total_var(k, p["a"], p["b"], p["rho"], p["m"], p["sigma"])
            w = np.maximum(w, 1e-12)
        elif self.method == "quadratic":
            kc = np.clip(k, p["kmin"], p["kmax"])
            iv = np.maximum(p["c0"] + p["c1"] * kc + p["c2"] * kc * kc, 1e-4)
            w = iv * iv * self.T
        else:
            w = np.full(k.shape, p["atm_iv"] ** 2 * self.T)
        return float(w) if scalar else w

    def iv_k(self, k):
        w = self.total_var(k)
        return np.sqrt(np.asarray(w) / self.T) if np.ndim(w) else math.sqrt(w / self.T)

    def iv(self, strike):
        """Model implied vol at a strike (scalar or array)."""
        scalar = np.ndim(strike) == 0
        with np.errstate(divide="ignore", invalid="ignore"):
            k = np.log(np.asarray(strike, dtype=float) / self.forward)
        out = np.sqrt(np.asarray(self.total_var(k)) / self.T)
        return float(out) if scalar else out

    @property
    def atm_iv(self) -> float:
        return float(math.sqrt(self.total_var(0.0) / self.T))

    def price(self, strike, cp):
        """European model price per share: discounted Black on the forward at the model vol."""
        return bs.price(self.forward, strike, self.T, self.rate, self.rate, self.iv(strike), cp)


@dataclass
class VolSurface:
    """Collection of smile slices plus interpolation helpers.

    Interpolation between expiries is linear in total variance at fixed log-moneyness
    k = ln(K/F(T)); outside the quoted expiry range the vol is extrapolated flat. The forward
    for an unquoted expiry uses the implied carry ln(F/S)/T interpolated linearly in T.
    """

    spot: float
    asof: datetime
    rate: float
    div_yield: float
    slices: dict[date, SmileSlice] = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)

    # -- helpers
    def _sorted(self) -> list[SmileSlice]:
        return sorted(self.slices.values(), key=lambda s: s.T)

    def _T(self, expiry: date) -> float:
        if expiry in self.slices:
            return self.slices[expiry].T
        return year_fraction(self.asof, expiry)

    def forward(self, expiry: date) -> float:
        if expiry in self.slices:
            return self.slices[expiry].forward
        return self._forward_T(self._T(expiry))

    def _forward_T(self, T: float) -> float:
        sl = self._sorted()
        if not sl:
            return self.spot * math.exp((self.rate - self.div_yield) * T)
        Ts = np.array([s.T for s in sl])
        carry = np.array([math.log(s.forward / self.spot) / s.T for s in sl])
        c = float(np.interp(T, Ts, carry))
        return self.spot * math.exp(c * T)

    def _total_var_T(self, T: float, k):
        """Total variance at time T and log-moneyness k (array)."""
        sl = self._sorted()
        k = np.asarray(k, dtype=float)
        if not sl:
            return np.full(k.shape, np.nan)
        if T <= sl[0].T:
            return np.asarray(sl[0].total_var(k)) * T / sl[0].T
        if T >= sl[-1].T:
            return np.asarray(sl[-1].total_var(k)) * T / sl[-1].T
        for lo, hi in zip(sl[:-1], sl[1:]):
            if lo.T <= T <= hi.T:
                wl, wh = np.asarray(lo.total_var(k)), np.asarray(hi.total_var(k))
                x = (T - lo.T) / (hi.T - lo.T)
                return wl + x * (wh - wl)
        return np.full(k.shape, np.nan)  # pragma: no cover

    # -- public API
    def iv(self, expiry: date, strike) -> float:
        """Model implied vol for any expiry (interpolated if not a fitted slice) and strike."""
        scalar = np.ndim(strike) == 0
        if expiry in self.slices:
            return self.slices[expiry].iv(strike)
        T = self._T(expiry)
        F = self._forward_T(T)
        with np.errstate(divide="ignore", invalid="ignore"):
            k = np.log(np.asarray(strike, dtype=float) / F)
            out = np.sqrt(self._total_var_T(T, k) / T)
        return float(out) if scalar else out

    def atm_iv(self, expiry: date) -> float:
        """Implied vol at the forward (k = 0)."""
        return float(self.iv(expiry, self.forward(expiry)))

    def atm_iv_at(self, days: float) -> float:
        """ATM implied vol for a horizon in calendar days (T = days/365), linear in ATM total
        variance between fitted expiries, flat vol outside the fitted range."""
        T = max(float(days), 1e-6) / 365.0
        w = self._total_var_T(T, 0.0)
        return float(math.sqrt(float(w) / T)) if np.isfinite(w) else float("nan")


# ----------------------------------------------------------------------------- fitting


def _carry_forward(chain: OptionChain, T: float) -> float:
    return chain.spot * math.exp((chain.risk_free - chain.div_yield) * T)


def implied_forward(quotes: list[OptionQuote], spot_fwd: float, r: float, T: float, n_near: int = 5):
    """Median of K + e^{rT}(C_mid - P_mid) over the n_near strikes nearest ``spot_fwd`` with
    two-sided call and put markets. Returns (forward, source)."""
    calls = {q.strike: q for q in quotes if q.right == Right.CALL and q.has_two_sided_market}
    puts = {q.strike: q for q in quotes if q.right == Right.PUT and q.has_two_sided_market}
    common = sorted(set(calls) & set(puts), key=lambda K: abs(K - spot_fwd))[:n_near]
    if not common:
        return spot_fwd, "carry"
    growth = math.exp(r * T)
    est = [K + growth * (calls[K].mid - puts[K].mid) for K in common]
    F = float(np.median(est))
    if not np.isfinite(F) or F <= 0 or abs(F / spot_fwd - 1.0) > 0.10:
        return spot_fwd, "carry"
    return F, "parity"


def _quote_table(quotes: list[OptionQuote], F: float, T: float, r: float) -> pd.DataFrame:
    n = len(quotes)
    strike = np.array([q.strike for q in quotes], dtype=float)
    cp = np.array([bs.cp_of(q.right) for q in quotes], dtype=float)
    bid = np.array([q.bid for q in quotes], dtype=float)
    ask = np.array([q.ask for q in quotes], dtype=float)
    mid = np.array([q.mid for q in quotes], dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        k = np.log(strike / F)
    two = np.array([q.has_two_sided_market for q in quotes], dtype=bool)
    otm = np.where(cp > 0, strike >= F, strike < F)
    miv = bs.implied_vol(mid, F, strike, T, r, r, cp) if n else np.array([])
    biv = bs.implied_vol(np.where(bid > 0, bid, np.nan), F, strike, T, r, r, cp) if n else np.array([])
    aiv = bs.implied_vol(np.where(ask > 0, ask, np.nan), F, strike, T, r, r, cp) if n else np.array([])
    return pd.DataFrame(
        {
            "quote": quotes,
            "strike": strike,
            "right": [q.right.value for q in quotes],
            "cp": cp,
            "k": k,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "two_sided": two,
            "otm": otm,
            "market_iv": np.asarray(miv, dtype=float).reshape(n),
            "bid_iv": np.asarray(biv, dtype=float).reshape(n),
            "ask_iv": np.asarray(aiv, dtype=float).reshape(n),
            "in_fit": np.zeros(n, dtype=bool),
            "fit_iv": np.full(n, np.nan),
            "weight": np.zeros(n),
        }
    )


def _select_fit_points(tab: pd.DataFrame, cfg: ScanConfig) -> np.ndarray:
    if tab.empty:
        return np.zeros(0, dtype=bool)
    not_crossed = ~((tab["bid"].to_numpy() > 0) & (tab["ask"].to_numpy() > 0) & (tab["bid"].to_numpy() >= tab["ask"].to_numpy()))
    base = tab["otm"].to_numpy() & np.isfinite(tab["market_iv"].to_numpy()) & np.isfinite(tab["k"].to_numpy()) & not_crossed
    two = tab["two_sided"].to_numpy()
    mid = tab["mid"].to_numpy()
    spread_pct = np.where(mid > 0, (tab["ask"].to_numpy() - tab["bid"].to_numpy()) / np.where(mid > 0, mid, 1), np.inf)
    strict = base & two & (tab["bid"].to_numpy() >= cfg.min_bid) & (spread_pct <= max(4 * cfg.max_spread_pct, 0.5))
    if strict.sum() >= _MIN_SVI_POINTS:
        return strict
    loose = base & two
    if loose.sum() >= max(strict.sum(), 1):
        return loose
    if strict.sum() > 0:
        return strict
    return base & (mid > 0)


def _fit_weights(tab: pd.DataFrame, sel: np.ndarray, F: float, T: float, r: float) -> np.ndarray:
    sub = tab[sel]
    vega = bs.greeks(F, sub["strike"].to_numpy(), T, r, r, sub["market_iv"].to_numpy(), sub["cp"].to_numpy())["vega"]
    vega = np.maximum(np.asarray(vega, dtype=float), 1e-8)
    spread = (sub["ask"] - sub["bid"]).to_numpy()
    spread = np.where(sub["two_sided"].to_numpy(), spread, np.nan)
    floor = np.maximum(1e-3, 1e-3 * sub["mid"].to_numpy())
    spread = np.where(np.isfinite(spread) & (spread > 0), np.maximum(spread, floor), np.maximum(0.1 * sub["mid"].to_numpy(), floor))
    w = vega / spread
    med = np.median(w)
    if not np.isfinite(med) or med <= 0:
        return np.ones(len(w))
    return np.clip(w, 0.05 * med, 20.0 * med)


def fit_svi(k: np.ndarray, iv: np.ndarray, T: float, weights: np.ndarray | None = None):
    """Fit raw SVI to implied vols. Returns (params dict, success flag).

    Minimises sum_i rho_soft( weights_i * (sqrt(w(k_i)/T) - iv_i) ) + arbitrage penalties
    (see module docstring) with scipy least_squares (trf, soft_l1, f_scale = 1)."""
    k = np.asarray(k, dtype=float)
    iv = np.asarray(iv, dtype=float)
    wts = np.ones_like(k) if weights is None else np.asarray(weights, dtype=float)
    wmkt = iv * iv * T
    kmin, kmax = float(k.min()), float(k.max())
    span = max(kmax - kmin, 0.05)
    order = np.argsort(k)
    w_atm = float(np.interp(0.0, k[order], wmkt[order]))
    g_grid = np.linspace(kmin - 0.5 * span, kmax + 0.5 * span, 41)

    def unpack(x):
        wmin, b, rho, m, s = x
        a = wmin - b * s * math.sqrt(max(1.0 - rho * rho, 0.0))
        return a, b, rho, m, s

    def resid(x):
        a, b, rho, m, s = unpack(x)
        w = svi_total_var(k, a, b, rho, m, s)
        model = np.sqrt(np.maximum(w, 1e-12) / T)
        r_fit = wts * (model - iv)
        wg, w1, w2 = svi_derivatives(g_grid, a, b, rho, m, s)
        g = durrleman_g(g_grid, np.maximum(wg, 1e-12), w1, w2)
        pen_g = 10.0 * np.maximum(-np.nan_to_num(g, nan=-1.0), 0.0)
        pen_lee = np.array([10.0 * max(b * (1 + abs(rho)) - 2.0, 0.0)])
        return np.concatenate([r_fit, pen_g, pen_lee])

    wmax = float(max(wmkt.max(), 1e-6))
    lb = np.array([0.0, 0.0, -0.995, kmin - span, 1e-3])
    ub = np.array([2.0 * wmax, 2.0, 0.995, kmax + span, max(1.0, 2.0 * span)])

    # wing-slope based seed
    left = k < np.minimum(-0.25 * span, 0.0) if (k < 0).any() else np.zeros_like(k, bool)
    right = k > np.maximum(0.25 * span, 0.0) if (k > 0).any() else np.zeros_like(k, bool)
    sL = np.polyfit(k[left], wmkt[left], 1)[0] if left.sum() >= 2 else -0.5 * w_atm / span
    sR = np.polyfit(k[right], wmkt[right], 1)[0] if right.sum() >= 2 else 0.2 * w_atm / span
    b0 = float(np.clip((sR - sL) / 2.0, 1e-3, 1.9))
    rho0 = float(np.clip((sR + sL) / max(sR - sL, 1e-9), -0.9, 0.9))
    k_min_iv = float(k[np.argmin(wmkt)])
    seeds = [(rho0, 0.0, 0.2 * span, b0)]
    for rho_s in (-0.7, -0.2, 0.3):
        for s_s in (0.1 * span, 0.5 * span):
            seeds.append((rho_s, k_min_iv if rho_s > -0.5 else 0.0, s_s, b0))
    def run(x0, nfev):
        try:
            res = least_squares(resid, x0, bounds=(lb, ub), loss="soft_l1", f_scale=1.0, method="trf", max_nfev=nfev, x_scale="jac")
        except Exception:  # noqa: BLE001 - never raise on ugly data
            return None
        return res if np.isfinite(res.cost) else None

    # stage 1: short runs from every seed; stage 2: polish the two best to convergence
    coarse = []
    for rho_s, m_s, s_s, b_s in seeds:
        x0 = np.array([max(0.8 * min(wmkt.min(), w_atm), 1e-8), b_s, rho_s, m_s, max(s_s, 2e-3)])
        x0 = np.clip(x0, lb + 1e-12, ub - 1e-12)
        res = run(x0, 25)
        if res is not None:
            coarse.append(res)
    coarse.sort(key=lambda r: r.cost)
    best, best_cost = None, np.inf
    for c in coarse[:2]:
        res = run(np.clip(c.x, lb + 1e-12, ub - 1e-12), 500) or c
        if res.cost < best_cost:
            best, best_cost = res, res.cost
    if best is None:
        return {}, False
    a, b, rho, m, s = unpack(best.x)
    params = {"a": float(a), "b": float(b), "rho": float(rho), "m": float(m), "sigma": float(s)}
    w_fit = svi_total_var(k, a, b, rho, m, s)
    ok = bool(np.all(np.isfinite(w_fit)) and np.all(w_fit > 0))
    return params, ok


def _fit_quadratic(k: np.ndarray, iv: np.ndarray, wts: np.ndarray) -> dict:
    deg = min(2, len(k) - 1)
    coef = np.polyfit(k, iv, deg, w=np.sqrt(wts))[::-1]
    c = np.zeros(3)
    c[: len(coef)] = coef
    return {"c0": float(c[0]), "c1": float(c[1]), "c2": float(c[2]), "kmin": float(k.min()), "kmax": float(k.max())}


def butterfly_check(sl: SmileSlice, k_range: tuple[float, float] = (-0.5, 0.5), n_grid: int = 201) -> float:
    """Minimum of Durrleman's g(k) over a k-grid (g < 0 anywhere => butterfly arbitrage).

    Analytic derivatives for SVI slices, central finite differences otherwise. For the
    quadratic fallback the grid is restricted to its quoted k-range (the flat extrapolation
    beyond it has kinks that are an artefact of the fallback, not of the market)."""
    lo, hi = k_range
    if sl.method == "quadratic":
        lo, hi = max(lo, sl.params["kmin"] + 1e-3), min(hi, sl.params["kmax"] - 1e-3)
        if hi <= lo:
            return float("nan")
    grid = np.linspace(lo, hi, n_grid)
    if sl.method == "svi":
        p = sl.params
        w, w1, w2 = svi_derivatives(grid, p["a"], p["b"], p["rho"], p["m"], p["sigma"])
    else:
        h = 1e-4
        w = np.asarray(sl.total_var(grid))
        wp, wm = np.asarray(sl.total_var(grid + h)), np.asarray(sl.total_var(grid - h))
        w1 = (wp - wm) / (2 * h)
        w2 = (wp - 2 * w + wm) / (h * h)
    g = durrleman_g(grid, np.maximum(w, 1e-12), w1, w2)
    return float(np.nanmin(g)) if np.any(np.isfinite(g)) else float("nan")


def _fit_slice(expiry: date, quotes: list[OptionQuote], chain: OptionChain, cfg: ScanConfig):
    T = chain.T(expiry)
    r = chain.risk_free
    F, fsrc = implied_forward(quotes, _carry_forward(chain, T), r, T)
    tab = _quote_table(quotes, F, T, r)
    sel = _select_fit_points(tab, cfg)
    n = int(sel.sum())
    info = {"forward": F, "forward_source": fsrc, "T": T, "n_quotes": len(quotes), "n_points": n}
    if n == 0:
        return None, info
    k = tab.loc[sel, "k"].to_numpy()
    iv = tab.loc[sel, "market_iv"].to_numpy()
    wts = _fit_weights(tab, sel, F, T, r)
    method, params = "flat", None
    if n >= _MIN_SVI_POINTS:
        params, ok = fit_svi(k, iv, T, wts)
        method = "svi" if ok else "quadratic"
        if not ok:
            info["svi_failed"] = True
    if method != "svi" and n >= 3:
        try:
            params = _fit_quadratic(k, iv, wts)
            method = "quadratic"
        except Exception:  # noqa: BLE001
            method = "flat"
    if method == "flat":
        order = np.argsort(k)
        params = {"atm_iv": float(np.interp(0.0, k[order], iv[order]))}
    sl = SmileSlice(expiry=expiry, T=T, forward=F, params=params, rmse=float("nan"), n_points=n, method=method, rate=r)
    fit_iv = np.asarray(sl.iv(tab["strike"].to_numpy()), dtype=float)
    tab["fit_iv"] = fit_iv
    tab.loc[sel, "in_fit"] = True
    tab.loc[sel, "weight"] = wts
    sl.table = tab
    sl.rmse = float(np.sqrt(np.mean((fit_iv[sel] - iv) ** 2)))
    kq = tab["k"].to_numpy()
    kq = kq[np.isfinite(kq)]
    sl.min_g = butterfly_check(sl, (min(kq.min(), -0.05), max(kq.max(), 0.05)) if kq.size else (-0.5, 0.5))
    sl.butterfly_arbitrage = bool(np.isfinite(sl.min_g) and sl.min_g < _G_TOL)
    info.update({"method": method, "rmse": sl.rmse, "min_g": sl.min_g, "butterfly_arbitrage": sl.butterfly_arbitrage, "atm_iv": sl.atm_iv})
    return sl, info


def build_surface(chain: OptionChain, cfg: ScanConfig | None = None) -> VolSurface:
    """Fit a :class:`VolSurface` to an option chain (see module docstring). Never raises on
    malformed quotes; problems are reported in ``surface.diagnostics``.

    diagnostics keys: ``rmse``, ``n_points``, ``method``, ``min_g``, ``butterfly_arbitrage``
    (dicts keyed by expiry date), ``slices`` (full per-expiry info), ``skipped`` (expiry ->
    reason), ``calendar_violations`` (list of dicts), ``calendar_arbitrage`` (bool),
    ``butterfly_any`` (bool)."""
    cfg = cfg or ScanConfig()
    surf = VolSurface(spot=float(chain.spot), asof=chain.asof, rate=float(chain.risk_free), div_yield=float(chain.div_yield))
    diag: dict = {"slices": {}, "skipped": {}, "rmse": {}, "n_points": {}, "method": {}, "min_g": {}, "butterfly_arbitrage": {}}
    for expiry in chain.expiries():
        quotes = [
            q
            for q in chain.for_expiry(expiry)
            if np.isfinite(q.strike) and q.strike > 0 and np.isfinite(q.bid) and np.isfinite(q.ask)
        ]
        try:
            if chain.spot <= 0 or not np.isfinite(chain.spot):
                raise ValueError("invalid spot")
            sl, info = _fit_slice(expiry, quotes, chain, cfg)
        except Exception as exc:  # noqa: BLE001 - surface building must never raise
            diag["skipped"][expiry] = f"error: {exc!r}"
            continue
        diag["slices"][expiry] = info
        if sl is None:
            diag["skipped"][expiry] = "no usable quotes"
            continue
        surf.slices[expiry] = sl
        diag["rmse"][expiry] = sl.rmse
        diag["n_points"][expiry] = sl.n_points
        diag["method"][expiry] = sl.method
        diag["min_g"][expiry] = sl.min_g
        diag["butterfly_arbitrage"][expiry] = sl.butterfly_arbitrage
    diag["butterfly_any"] = any(diag["butterfly_arbitrage"].values())
    viol = calendar_check(surf)
    diag["calendar_violations"] = viol
    diag["calendar_arbitrage"] = bool(viol)
    surf.diagnostics = diag
    return surf


def calendar_check(surface: VolSurface, n_grid: int = 41) -> list[dict]:
    """Check total variance is non-decreasing in T at fixed k for consecutive slices.

    The k-grid spans the union of the two slices' quoted log-moneyness. Returns a list of
    {"near", "far", "k", "dw"} for each violating pair (dw = w_far - w_near at the worst k)."""
    out = []
    sl = surface._sorted()
    for a, b in zip(sl[:-1], sl[1:]):
        ks = []
        for s in (a, b):
            if not s.table.empty:
                kk = s.table["k"].to_numpy()
                ks.append(kk[np.isfinite(kk)])
        kk = np.concatenate(ks) if ks else np.array([0.0])
        lo, hi = (min(kk.min(), 0.0), max(kk.max(), 0.0)) if kk.size else (-0.2, 0.2)
        grid = np.linspace(lo, hi, n_grid)
        dw = np.asarray(b.total_var(grid)) - np.asarray(a.total_var(grid))
        i = int(np.argmin(dw))
        if dw[i] < -_CAL_TOL * max(1.0, float(np.asarray(a.total_var(grid[i])))):
            out.append({"near": a.expiry, "far": b.expiry, "k": float(grid[i]), "dw": float(dw[i])})
    return out

"""Realised-volatility estimators and volatility forecasts.

All outputs are annualised with 252 trading days. Inputs are daily bars DataFrames with
columns open, high, low, close (volume ignored); rows with NaN/non-positive prices are
dropped per estimator, so missing data degrades estimates rather than raising.

Estimators (per-day variance, averaged over the window, then x 252):
  * close:        sample variance of log close-to-close returns (ddof = 1)
  * parkinson:    ln(H/L)^2 / (4 ln 2)
  * garman_klass: 0.5 ln(H/L)^2 - (2 ln 2 - 1) ln(C/O)^2
  * yang_zhang:   s_o^2 + k s_c^2 + (1-k) s_rs^2, with overnight o = ln(O_t/C_{t-1}),
                  open-to-close c = ln(C/O), Rogers-Satchell rs = ln(H/C)ln(H/O) + ln(L/C)ln(L/O),
                  k = 0.34 / (1.34 + (n+1)/(n-1)). Includes the overnight gap.
Parkinson and Garman-Klass measure the open-to-close (trading-session) variance only.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.signal import lfilter

ANN = 252.0
_LN2 = math.log(2.0)


def _clean_ohlc(df: pd.DataFrame, cols=("open", "high", "low", "close")) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=list(cols))
    d = df[[c for c in cols if c in df.columns]].astype(float)
    d = d.replace([np.inf, -np.inf], np.nan).dropna()
    d = d[(d > 0).all(axis=1)]
    return d


def _log_returns(daily_bars: pd.DataFrame) -> np.ndarray:
    if daily_bars is None or len(daily_bars) == 0 or "close" not in daily_bars:
        return np.array([])
    c = daily_bars["close"].astype(float).replace([np.inf, -np.inf], np.nan)
    c = c[c > 0].dropna()
    r = np.diff(np.log(c.to_numpy()))
    return r[np.isfinite(r)]


def _daily_variance_components(d: pd.DataFrame):
    """Per-day (overnight, open-close, rogers-satchell, parkinson, gk) variance pieces."""
    o, h, l, c = (d[x].to_numpy() for x in ("open", "high", "low", "close"))
    hl = np.log(h / l)
    co = np.log(c / o)
    rs = np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o)
    on = np.full(len(d), np.nan)
    if len(d) > 1:
        on[1:] = np.log(o[1:] / c[:-1])
    return on, co, rs, hl


def realized_vol(daily_bars: pd.DataFrame, window: int = 21, method: str = "yang_zhang") -> float:
    """Annualised realised vol over the last ``window`` bars (see module docstring).

    Returns NaN when fewer than 2 usable observations exist. Uses whatever is available
    when the history is shorter than ``window``."""
    method = method.lower()
    if method == "close":
        r = _log_returns(daily_bars)[-window:]
        if len(r) < 2:
            return float("nan")
        return float(math.sqrt(np.var(r, ddof=1) * ANN))
    d = _clean_ohlc(daily_bars)
    if method in ("parkinson", "garman_klass"):
        d = d.iloc[-window:]
        if len(d) < 1:
            return float("nan")
        _, co, _, hl = _daily_variance_components(d)
        if method == "parkinson":
            var = np.mean(hl * hl) / (4.0 * _LN2)
        else:
            var = np.mean(0.5 * hl * hl - (2.0 * _LN2 - 1.0) * co * co)
        return float(math.sqrt(max(var, 0.0) * ANN))
    if method == "yang_zhang":
        d = d.iloc[-(window + 1):]  # one extra bar for the first overnight return
        on, co, rs, _ = _daily_variance_components(d)
        on, co, rs = on[1:], co[1:], rs[1:]
        n = len(co)
        if n < 2:
            return float("nan")
        k = 0.34 / (1.34 + (n + 1.0) / (n - 1.0))
        var = np.var(on, ddof=1) + k * np.var(co, ddof=1) + (1.0 - k) * np.mean(rs)
        return float(math.sqrt(max(var, 0.0) * ANN))
    raise ValueError(f"unknown realized_vol method {method!r}")


def daily_variance_proxy(daily_bars: pd.DataFrame) -> pd.Series:
    """Per-day realised-variance proxy: overnight^2 + Rogers-Satchell (drift-robust range
    estimator of the session variance). Indexed like the cleaned bars (first day has no
    overnight term and uses RS only)."""
    d = _clean_ohlc(daily_bars)
    if len(d) == 0:
        return pd.Series(dtype=float)
    on, co, rs, _ = _daily_variance_components(d)
    v = np.nan_to_num(on, nan=0.0) ** 2 + np.maximum(rs, 0.0)
    return pd.Series(v, index=d.index)


def _intraday_rv(intraday_bars: pd.DataFrame) -> pd.Series:
    """Sum of squared intraday log returns per session date (session variance)."""
    c = intraday_bars["close"].astype(float)
    c = c[c > 0].dropna()
    if len(c) < 3:
        return pd.Series(dtype=float)
    idx = c.index
    days = pd.Index(idx.date if hasattr(idx, "date") else idx)
    out = {}
    for day in pd.unique(days):
        cc = c.to_numpy()[days == day]
        if len(cc) >= 3:
            r = np.diff(np.log(cc))
            out[day] = float(np.sum(r * r))
    return pd.Series(out, dtype=float)


# ----------------------------------------------------------------------------- GARCH(1,1)


def _garch_filter(eps2: np.ndarray, omega: float, alpha: float, beta: float, h0: float) -> np.ndarray:
    """Conditional variances h_t = omega + alpha eps_{t-1}^2 + beta h_{t-1}, h_0 = h0.
    Returns h of length n+1 (h[n] is the one-step-ahead forecast)."""
    x = np.concatenate([[0.0], eps2])  # x_t = eps_{t-1}^2 (x_0 unused)
    # h_t - beta h_{t-1} = omega + alpha x_t  for t >= 1
    u = omega + alpha * x[1:]
    zi = np.array([beta * h0])
    h1, _ = lfilter([1.0], [1.0, -beta], u, zi=zi)
    return np.concatenate([[h0], h1])


def fit_garch(returns: np.ndarray) -> dict:
    """Gaussian GARCH(1,1) by maximum likelihood on demeaned daily returns.

    Model: eps_t = r_t - mean(r), eps_t | F_{t-1} ~ N(0, h_t), h_t = omega + alpha eps_{t-1}^2 +
    beta h_{t-1}, with omega > 0, alpha, beta >= 0, alpha + beta < 0.999; h_0 = sample variance.
    Returns are scaled to percent internally for conditioning. Output dict: omega, alpha,
    beta (decimal-return units), persistence, uncond_var (daily), h_next (one-step-ahead daily
    variance), loglik, success.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 30:
        raise ValueError("need at least 30 returns for GARCH")
    scale = 100.0
    eps = (r - r.mean()) * scale
    eps2 = eps * eps
    v0 = float(np.var(eps))
    n = len(eps)

    def nll(theta):
        omega, alpha, beta = theta
        if omega <= 0 or alpha < 0 or beta < 0 or alpha + beta >= 0.999:
            return 1e12
        h = _garch_filter(eps2, omega, alpha, beta, v0)[:-1]
        if np.any(h <= 0) or not np.all(np.isfinite(h)):
            return 1e12
        return 0.5 * float(np.sum(np.log(h) + eps2 / h)) + 0.5 * n * math.log(2 * math.pi)

    best = None
    for a0, b0 in ((0.05, 0.90), (0.10, 0.80), (0.03, 0.95), (0.15, 0.6)):
        x0 = np.array([v0 * (1 - a0 - b0), a0, b0])
        res = minimize(
            nll,
            x0,
            method="SLSQP",
            bounds=[(1e-8 * v0, 10 * v0), (0.0, 0.5), (0.0, 0.998)],
            constraints=[{"type": "ineq", "fun": lambda t: 0.998 - t[1] - t[2]}],
            options={"maxiter": 300, "ftol": 1e-10},
        )
        if np.isfinite(res.fun) and (best is None or res.fun < best.fun):
            best = res
    omega, alpha, beta = (float(x) for x in best.x)
    h = _garch_filter(eps2, omega, alpha, beta, v0)
    pers = alpha + beta
    s2 = scale * scale
    return {
        "omega": omega / s2,
        "alpha": alpha,
        "beta": beta,
        "persistence": pers,
        "uncond_var": (omega / (1 - pers)) / s2 if pers < 1 else float("nan"),
        "h_next": float(h[-1]) / s2,
        "loglik": -float(best.fun) + n * math.log(scale),  # in decimal-return units
        "success": bool(best.success),
    }


def garch_forecast(params: dict, horizon_days: int) -> float:
    """Annualised vol from the GARCH multi-step forecast averaged over the horizon:
    E[h_{t+j}] = var_inf + (alpha+beta)^{j-1} (h_{t+1} - var_inf), j = 1..H."""
    H = max(int(horizon_days), 1)
    p = params["persistence"]
    vinf = params["uncond_var"]
    j = np.arange(H)
    path = vinf + p**j * (params["h_next"] - vinf)
    return float(math.sqrt(max(np.mean(path), 0.0) * ANN))


# ----------------------------------------------------------------------------- HAR-RV


def har_forecast(rv: np.ndarray, horizon_days: int) -> float:
    """Direct multi-horizon HAR-RV (Corsi 2009) on daily realised variances ``rv``:

        mean(RV_{t+1..t+H}) = b0 + bd RV_t + bw mean(RV_{t-4..t}) + bm mean(RV_{t-21..t})

    estimated by OLS on the available history; returns the annualised vol forecast.
    Falls back to the 22-day mean RV if the regression is ill-posed or forecasts <= 0."""
    rv = np.asarray(rv, dtype=float)
    rv = rv[np.isfinite(rv)]
    H = max(int(horizon_days), 1)
    n = len(rv)
    fallback = float(np.mean(rv[-22:])) if n else float("nan")
    if n < 22 + H + 10:
        return float(math.sqrt(max(fallback, 0.0) * ANN)) if n else float("nan")
    cs = np.concatenate([[0.0], np.cumsum(rv)])

    def mean_back(t, L):  # mean of rv[t-L+1 .. t]
        return (cs[t + 1] - cs[t + 1 - L]) / L

    ts = np.arange(21, n - H)
    X = np.column_stack([np.ones(len(ts)), rv[ts], mean_back(ts, 5), mean_back(ts, 22)])
    y = (cs[ts + 1 + H] - cs[ts + 1]) / H
    try:
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return float(math.sqrt(max(fallback, 0.0) * ANN))
    t = n - 1
    x_now = np.array([1.0, rv[t], mean_back(t, 5), mean_back(t, 22)])
    pred = float(x_now @ beta)
    if not np.isfinite(pred) or pred <= 0:
        pred = fallback
    return float(math.sqrt(max(pred, 0.0) * ANN))


# ----------------------------------------------------------------------------- ensemble


def ewma_vol(returns: np.ndarray, lam: float = 0.94) -> float:
    """RiskMetrics EWMA: s2_t = lam s2_{t-1} + (1-lam) r_{t-1}^2 (zero-mean), seeded with the
    variance of the first 20 returns; annualised. The forecast is flat in the horizon."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 2:
        return float("nan")
    s2 = float(np.mean(r[: min(20, len(r))] ** 2))
    y, _ = lfilter([1.0 - lam], [1.0, -lam], r * r, zi=np.array([lam * s2]))
    return float(math.sqrt(y[-1] * ANN))


_ENSEMBLE_WEIGHTS = {"garch": 0.30, "har": 0.30, "ewma": 0.20, "yang_zhang": 0.20}


def forecast_vol(daily_bars: pd.DataFrame, horizon_days: int, intraday_bars: pd.DataFrame | None = None) -> dict[str, float]:
    """Annualised volatility forecasts for the next ``horizon_days`` trading days.

    Keys:
      * ``ewma``       RiskMetrics lambda = 0.94 on close-to-close log returns
      * ``har``        HAR-RV direct H-step forecast on the daily variance proxy
                       (overnight^2 + Rogers-Satchell); days covered by ``intraday_bars``
                       use overnight^2 + sum of squared intraday returns instead
      * ``garch``      GARCH(1,1) MLE, multi-step forecast averaged over the horizon
      * ``yang_zhang`` 21-day Yang-Zhang realised vol (flat forecast)
      * ``ensemble``   robust blend: components outside [0.5, 2] x their median are dropped,
                       remaining ones averaged with weights garch .3, har .3, ewma .2, yz .2
    Degrades gracefully: GARCH needs >= 60 returns and HAR ~ 32 + H days, otherwise they fall
    back to the EWMA value; with < 3 returns everything is NaN. Never raises on bad data.
    """
    nan = float("nan")
    out = {"ewma": nan, "har": nan, "garch": nan, "yang_zhang": nan, "ensemble": nan}
    try:
        r = _log_returns(daily_bars)
    except Exception:  # noqa: BLE001
        return out
    if len(r) < 3:
        return out
    H = max(int(horizon_days), 1)
    out["ewma"] = ewma_vol(r)
    try:
        out["yang_zhang"] = realized_vol(daily_bars, window=21, method="yang_zhang")
    except Exception:  # noqa: BLE001
        pass
    if not np.isfinite(out["yang_zhang"]):
        out["yang_zhang"] = realized_vol(daily_bars, window=21, method="close")
    # HAR
    try:
        rvs = daily_variance_proxy(daily_bars)
        if intraday_bars is not None and len(intraday_bars) > 0 and len(rvs) > 0:
            iv = _intraday_rv(intraday_bars)
            if len(iv):
                d = _clean_ohlc(daily_bars)
                on, *_ = _daily_variance_components(d)
                on2 = pd.Series(np.nan_to_num(on, nan=0.0) ** 2, index=d.index)
                dates = pd.Index([ix.date() if hasattr(ix, "date") else ix for ix in rvs.index])
                for day, val in iv.items():
                    hit = np.where(dates == day)[0]
                    if len(hit):
                        rvs.iloc[hit[-1]] = on2.iloc[hit[-1]] + val
        out["har"] = har_forecast(rvs.to_numpy(), H) if len(rvs) >= 22 + H + 10 else out["ewma"]
    except Exception:  # noqa: BLE001
        out["har"] = out["ewma"]
    # GARCH
    try:
        if len(r) >= 60:
            g = fit_garch(r)
            out["garch"] = garch_forecast(g, H)
        else:
            out["garch"] = out["ewma"]
    except Exception:  # noqa: BLE001
        out["garch"] = out["ewma"]
    for key in ("har", "garch"):
        if not np.isfinite(out[key]):
            out[key] = out["ewma"]
    vals = {k: out[k] for k in _ENSEMBLE_WEIGHTS if np.isfinite(out[k]) and out[k] > 0}
    if vals:
        med = float(np.median(list(vals.values())))
        keep = {k: v for k, v in vals.items() if 0.5 * med <= v <= 2.0 * med} or vals
        wsum = sum(_ENSEMBLE_WEIGHTS[k] for k in keep)
        out["ensemble"] = float(sum(_ENSEMBLE_WEIGHTS[k] * v for k, v in keep.items()) / wsum)
    return out

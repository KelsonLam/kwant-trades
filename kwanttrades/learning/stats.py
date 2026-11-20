"""Small, dependency-light statistics used by the learning rules and the graduation checklist.

Every function is pure and returns plain floats/ints so the evidence can be stored as JSON.
"""
from __future__ import annotations

import math
import warnings
from collections.abc import Sequence

import numpy as np
from scipy import stats as _st


def _clean(xs: Sequence[float]) -> np.ndarray:
    a = np.asarray([float(x) for x in xs if x is not None], dtype=float)
    return a[np.isfinite(a)]


def mean_ci(xs: Sequence[float], level: float = 0.80) -> tuple[float, float, float, int]:
    """(mean, lower, upper, n) of a two-sided Student-t confidence interval for the mean.
    With fewer than 2 values the interval is (-inf, +inf), i.e. "no evidence"."""
    a = _clean(xs)
    n = int(a.size)
    if n == 0:
        return float("nan"), float("-inf"), float("inf"), 0
    m = float(a.mean())
    if n < 2:
        return m, float("-inf"), float("inf"), n
    sd = float(a.std(ddof=1))
    if sd == 0.0:
        return m, m, m, n
    t = float(_st.t.ppf(0.5 + level / 2.0, n - 1))
    hw = t * sd / math.sqrt(n)
    return m, m - hw, m + hw, n


def proportion_ci(k: int, n: int, level: float = 0.90) -> tuple[float, float]:
    """Exact (Clopper-Pearson) two-sided confidence interval for a proportion k/n."""
    if n <= 0:
        return 0.0, 1.0
    a = 1.0 - level
    lo = 0.0 if k <= 0 else float(_st.beta.ppf(a / 2.0, k, n - k + 1))
    hi = 1.0 if k >= n else float(_st.beta.ppf(1.0 - a / 2.0, k + 1, n - k))
    return lo, hi


def binom_p_less(k: int, n: int, p: float) -> float:
    """One-sided exact binomial test p-value for "true rate < p" given k successes out of n."""
    if n <= 0:
        return 1.0
    p = min(max(float(p), 1e-9), 1 - 1e-9)
    return float(_st.binomtest(int(k), int(n), p, alternative="less").pvalue)


def welch_p_less(a: Sequence[float], b: Sequence[float]) -> float:
    """One-sided Welch t-test p-value for "mean(a) < mean(b)". 1.0 when there is not enough data."""
    x, y = _clean(a), _clean(b)
    if x.size < 2 or y.size < 2:
        return 1.0
    if float(np.ptp(x)) < 1e-12 and float(np.ptp(y)) < 1e-12:  # both constant: the means decide
        return 0.0 if x.mean() < y.mean() else 1.0
    with warnings.catch_warnings():  # near-constant samples: scipy warns about precision, the answer is still fine
        warnings.simplefilter("ignore", RuntimeWarning)
        res = _st.ttest_ind(x, y, equal_var=False, alternative="less")
    p = float(res.pvalue)
    return p if math.isfinite(p) else 1.0


def profit_factor(pnls: Sequence[float]) -> float | None:
    """Gross profit / gross loss. None with no trades; inf with gains and no losses."""
    a = _clean(pnls)
    if a.size == 0:
        return None
    gains, losses = float(a[a > 0].sum()), float(-a[a < 0].sum())
    if losses == 0.0:
        return float("inf") if gains > 0 else None
    return gains / losses


def max_drawdown(pnls_in_order: Sequence[float], start_equity: float) -> float:
    """Largest peak-to-trough drop of start_equity + cumulative P&L, as a fraction of start_equity."""
    if start_equity <= 0:
        return 0.0
    eq = start_equity + np.cumsum(_clean(pnls_in_order)) if len(pnls_in_order) else np.array([start_equity])
    eq = np.concatenate([[start_equity], eq])
    peak = np.maximum.accumulate(eq)
    return float(np.max(peak - eq) / start_equity)


def finite_or_none(x: float | None) -> float | None:
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None

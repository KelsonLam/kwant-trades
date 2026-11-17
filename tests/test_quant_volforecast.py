import math

import numpy as np
import pandas as pd
import pytest

from kwanttrades.quant import volforecast as vf
from kwanttrades.types import NY


def gbm_bars(n_days=500, sigma=0.25, steps=390, overnight_frac=0.0, seed=0, s0=100.0):
    """Daily OHLC from a simulated GBM path (zero drift) with `steps` intraday increments.
    `overnight_frac` of the daily variance is realised as an overnight gap."""
    rng = np.random.default_rng(seed)
    dvar = sigma**2 / 252
    on = rng.normal(-0.5 * dvar * overnight_frac, math.sqrt(dvar * overnight_frac), n_days) if overnight_frac else np.zeros(n_days)
    sd = math.sqrt(dvar * (1 - overnight_frac) / steps)
    incr = rng.normal(-0.5 * sd * sd, sd, (n_days, steps))
    rows = []
    prev_close = s0
    for d in range(n_days):
        o = prev_close * math.exp(on[d])
        path = o * np.exp(np.concatenate([[0.0], np.cumsum(incr[d])]))
        rows.append((o, path.max(), path.min(), path[-1], 1e6))
        prev_close = path[-1]
    idx = pd.date_range("2024-01-02 16:00", periods=n_days, freq="B", tz=NY)
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close", "volume"])


@pytest.fixture(scope="module")
def bars_intraday_only():
    return gbm_bars(n_days=500, sigma=0.25, overnight_frac=0.0, seed=1)


@pytest.fixture(scope="module")
def bars_with_gaps():
    return gbm_bars(n_days=500, sigma=0.30, overnight_frac=0.25, seed=2)


@pytest.mark.parametrize("method", ["close", "parkinson", "garman_klass", "yang_zhang"])
def test_estimators_recover_gbm_vol(bars_intraday_only, method):
    est = vf.realized_vol(bars_intraday_only, window=500, method=method)
    assert est == pytest.approx(0.25, rel=0.08)


@pytest.mark.parametrize("method", ["close", "yang_zhang"])
def test_close_and_yang_zhang_include_overnight(bars_with_gaps, method):
    assert vf.realized_vol(bars_with_gaps, window=500, method=method) == pytest.approx(0.30, rel=0.08)
    # range-only estimators see just the session part (75% of the variance)
    assert vf.realized_vol(bars_with_gaps, window=500, method="parkinson") == pytest.approx(0.30 * math.sqrt(0.75), rel=0.08)


def simulate_garch(n, omega, alpha, beta, seed=0):
    rng = np.random.default_rng(seed)
    h = omega / (1 - alpha - beta)
    r = np.empty(n)
    for t in range(n):
        r[t] = math.sqrt(h) * rng.standard_normal()
        h = omega + alpha * r[t] ** 2 + beta * h
    return r


def test_garch_mle_recovers_parameters_and_mean_reverts():
    omega, alpha, beta = 2e-6, 0.08, 0.90
    r = simulate_garch(4000, omega, alpha, beta, seed=7)
    g = vf.fit_garch(r)
    assert g["alpha"] == pytest.approx(alpha, abs=0.03)
    assert g["beta"] == pytest.approx(beta, abs=0.04)
    assert g["uncond_var"] == pytest.approx(omega / (1 - alpha - beta), rel=0.3)
    uncond_vol = math.sqrt(g["uncond_var"] * 252)
    hi = dict(g, h_next=4 * g["uncond_var"])
    lo = dict(g, h_next=0.25 * g["uncond_var"])
    f1, f20, f500 = (vf.garch_forecast(hi, h) for h in (1, 20, 500))
    assert f1 > f20 > f500 > uncond_vol
    assert f1 == pytest.approx(2 * uncond_vol, rel=1e-9)
    l1, l500 = vf.garch_forecast(lo, 1), vf.garch_forecast(lo, 500)
    assert l1 < l500 < uncond_vol
    assert abs(vf.garch_forecast(hi, 5000) - uncond_vol) < 0.1 * uncond_vol


def test_forecast_vol_on_gbm(bars_with_gaps):
    f = vf.forecast_vol(bars_with_gaps, horizon_days=10)
    assert set(f) == {"ewma", "har", "garch", "yang_zhang", "ensemble"}
    for k, v in f.items():
        assert np.isfinite(v)
        assert v == pytest.approx(0.30, rel=0.3), k
    assert f["ensemble"] == pytest.approx(0.30, rel=0.15)


def test_forecast_vol_with_intraday_bars(bars_intraday_only):
    rng = np.random.default_rng(5)
    day = bars_intraday_only.index[-1].normalize()
    idx = pd.date_range(day + pd.Timedelta(hours=9, minutes=31), periods=390, freq="1min")
    px = bars_intraday_only["open"].iloc[-1] * np.exp(np.cumsum(rng.normal(0, 0.25 / math.sqrt(252 * 390), 390)))
    intr = pd.DataFrame({"open": px, "high": px, "low": px, "close": px, "volume": 1.0}, index=idx)
    f = vf.forecast_vol(bars_intraday_only, 5, intraday_bars=intr)
    assert all(np.isfinite(v) for v in f.values())
    assert f["har"] == pytest.approx(0.25, rel=0.3)


def test_short_history_and_nans_degrade_gracefully():
    b = gbm_bars(n_days=61, sigma=0.2, steps=50, seed=3)
    f = vf.forecast_vol(b, 21)
    assert all(np.isfinite(v) and v > 0 for v in f.values())
    b2 = b.copy()
    b2.iloc[[5, 17, 30], :] = np.nan
    b2.iloc[40, b2.columns.get_loc("high")] = np.nan
    f2 = vf.forecast_vol(b2, 5)
    assert all(np.isfinite(v) and v > 0 for v in f2.values())
    assert np.isfinite(vf.realized_vol(b2, method="yang_zhang"))
    tiny = vf.forecast_vol(b.iloc[:10], 5)
    assert np.isfinite(tiny["ensemble"]) and tiny["garch"] == tiny["ewma"]
    empty = vf.forecast_vol(b.iloc[:2], 5)
    assert all(np.isnan(v) for v in empty.values())
    assert np.isnan(vf.realized_vol(b.iloc[:1], method="close"))
    assert all(np.isnan(v) for v in vf.forecast_vol(pd.DataFrame(), 5).values())


def test_har_regression_on_persistent_variance():
    # variance with a regime switch: HAR forecast should track the recent regime
    rng = np.random.default_rng(11)
    rv = np.concatenate([np.full(300, 1e-4), np.full(60, 4e-4)]) * rng.lognormal(0, 0.3, 360)
    fc = vf.har_forecast(rv, 5)
    assert math.sqrt(2e-4 * 252) < fc < math.sqrt(6e-4 * 252)

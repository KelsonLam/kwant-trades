"""Synthetic market-data builders for the signals tests (no tests in this module)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from kwanttrades.types import NY

DAY = date(2025, 3, 12)  # a Wednesday


def ts(d: date, hh: int, mm: int, ss: int = 0) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(d, time(hh, mm, ss), tzinfo=NY))


def path(keys: dict[int, float], n: int = 390) -> np.ndarray:
    xs = sorted(keys)
    return np.interp(np.arange(n), xs, [keys[x] for x in xs])


# Scenario key-frames, minute index since 09:30 -> close price. Opening range is ~[99.7, 100.3] (mid ~100.0).
BASE_RANGE = {0: 100.0, 4: 100.25, 9: 99.75, 14: 100.1, 19: 100.2}
SCENARIOS: dict[str, dict[int, float]] = {
    # first 5m close above OR-high is the 09:50 bar (minutes 20..24), closing at 101.0
    "up": {**BASE_RANGE, 24: 101.0, 120: 101.2, 389: 101.2},
    "down": {**BASE_RANGE, 24: 99.0, 120: 98.8, 389: 98.8},
    # breakout at 09:50 bar, then slides back through the midpoint by the 09:55 bar close
    "fail": {**BASE_RANGE, 24: 101.0, 29: 99.8, 120: 99.8, 389: 99.8},
    "no_breakout": {**BASE_RANGE, 120: 100.0, 389: 100.0},
    # breakout then runs to +2.5: reaches target (~102.0) well before any close below the midpoint
    "target": {**BASE_RANGE, 24: 101.0, 29: 101.6, 34: 102.5, 120: 102.5, 389: 102.5},
    # wide range (about +-3)
    "wide": {0: 100.0, 4: 103.0, 9: 97.0, 14: 100.0, 19: 100.2, 24: 105.0, 120: 105.0, 389: 105.0},
}


def make_session(d: date, keys: dict[int, float], rng: np.random.Generator, *, vol: float = 1000.0,
                 or_vol_mult: float = 1.0, noise: float = 0.01, pre_post: bool = True,
                 prev_close: float | None = None, n: int = 390) -> pd.DataFrame:
    """One day of 1-minute bars (regular session, optional pre/post rows). Closes follow ``keys``."""
    close = path(keys, n) + rng.normal(0.0, noise, n)
    open_ = np.concatenate([[prev_close if prev_close is not None else close[0]], close[:-1]])
    wick = np.abs(rng.normal(0.0, noise, n)) + 0.005
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    v = vol * (1.0 + 0.1 * rng.random(n))
    v[:15] *= or_vol_mult
    idx = pd.date_range(ts(d, 9, 30), periods=n, freq="1min")
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": v}, index=idx)
    if pre_post:
        pre_idx = pd.date_range(ts(d, 8, 0), ts(d, 9, 29), freq="1min")
        pre = pd.DataFrame({"open": 90.0, "high": 140.0, "low": 60.0, "close": 90.0, "volume": 5.0e6}, index=pre_idx)
        post_idx = pd.date_range(ts(d, 16, 0), ts(d, 16, 30), freq="1min")
        post = pd.DataFrame({"open": 80.0, "high": 150.0, "low": 50.0, "close": 80.0, "volume": 5.0e6}, index=post_idx)
        df = pd.concat([pre, df, post])
    return df


def prior_days(end_before: date, n: int, rng: np.random.Generator, *, vol: float = 1000.0, level: float = 100.0,
               pre_post: bool = True) -> pd.DataFrame:
    """n quiet weekday sessions ending the weekday before ``end_before``, closing at ``level``."""
    days: list[date] = []
    d = end_before
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    frames = [make_session(x, {0: level, 14: level + 0.1, 389: level}, rng, vol=vol, pre_post=pre_post)
              for x in sorted(days)]
    return pd.concat(frames)


def daily_bars(end_before: date, n: int = 30, atr: float = 2.0, close: float = 100.0, volume: float | None = None,
               today_row: bool = False) -> pd.DataFrame:
    """Constant-close daily bars whose true range is exactly ``atr`` => Wilder ATR == atr."""
    days: list[date] = []
    d = end_before
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    days = sorted(days)
    if today_row:
        days.append(end_before)
    idx = pd.DatetimeIndex([pd.Timestamp(x) for x in days])
    df = pd.DataFrame({"open": close, "high": close + atr / 2, "low": close - atr / 2, "close": close,
                       "volume": volume if volume is not None else 1.0e6}, index=idx)
    if today_row:  # absurd partial bar that must be excluded from ATR
        df.iloc[-1] = [close, close + 50, close - 50, close, 1.0]
    return df


def build(scenario: str | dict[int, float], *, seed: int = 7, n_prior: int = 4, or_vol_mult: float = 3.0,
          atr: float = 2.0, d: date = DAY, pre_post: bool = True, today_daily: bool = False,
          prev_close: float | None = 100.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(bars_1m with prior days + today, daily_bars) for a scripted scenario."""
    rng = np.random.default_rng(seed)
    keys = SCENARIOS[scenario] if isinstance(scenario, str) else scenario
    today = make_session(d, keys, rng, or_vol_mult=or_vol_mult, pre_post=pre_post, prev_close=prev_close)
    parts = [prior_days(d, n_prior, rng, pre_post=pre_post)] if n_prior else []
    bars = pd.concat(parts + [today])
    return bars, daily_bars(d, atr=atr, today_row=today_daily)

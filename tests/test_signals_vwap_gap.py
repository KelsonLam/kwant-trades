from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from kwanttrades.signals.gap import gap_signal
from kwanttrades.signals.vwap import vwap_signal
from tests.test_signals_synth import DAY, build, daily_bars, make_session, ts


# ------------------------------------------------------------------ VWAP
def hand_frame():
    idx = pd.date_range(ts(DAY, 9, 30), periods=6, freq="1min")
    hi = [10.5, 10.8, 11.0, 11.2, 11.1, 11.4]
    lo = [9.9, 10.2, 10.5, 10.8, 10.6, 11.0]
    cl = [10.2, 10.6, 10.9, 11.0, 10.9, 11.3]
    vol = [100.0, 250.0, 175.0, 300.0, 50.0, 125.0]
    return pd.DataFrame({"open": cl, "high": hi, "low": lo, "close": cl, "volume": vol}, index=idx), hi, lo, cl, vol


def test_vwap_numeric_matches_hand_computation():
    df, hi, lo, cl, vol = hand_frame()
    tp = [(h + l + c) / 3 for h, l, c in zip(hi, lo, cl)]
    sv = sum(vol)
    vwap = sum(t * v for t, v in zip(tp, vol)) / sv
    var = sum(v * (t - vwap) ** 2 for t, v in zip(tp, vol)) / sv
    sig = vwap_signal("SPY", df)
    assert sig.detail["vwap"] == pytest.approx(vwap, rel=1e-12)
    assert sig.detail["upper"] == pytest.approx(vwap + math.sqrt(var), rel=1e-9)
    assert sig.detail["lower"] == pytest.approx(vwap - math.sqrt(var), rel=1e-9)
    assert sig.detail["price"] == cl[-1]
    # price 11.3 above vwap => bullish
    assert sig.direction > 0 and 0 < sig.confidence <= 1
    # price below vwap => bearish
    df2 = df.copy()
    df2.iloc[-1, df2.columns.get_loc("close")] = 9.0
    df2.iloc[-1, df2.columns.get_loc("low")] = 9.0
    assert vwap_signal("SPY", df2).direction < 0


def test_vwap_ignores_pre_post_and_other_days_and_uses_session_only():
    bars, _ = build("up")
    s = vwap_signal("SPY", bars, now=ts(DAY, 11, 0))
    day = bars[(bars.index >= ts(DAY, 9, 30)) & (bars.index < ts(DAY, 11, 0))]
    tp = (day.high + day.low + day.close) / 3
    expected = float((tp * day.volume).sum() / day.volume.sum())
    assert s.detail["vwap"] == pytest.approx(expected, rel=1e-12)
    assert s.detail["session_date"] == DAY.isoformat()
    assert s.detail["lower"] < s.detail["vwap"] < s.detail["upper"]


def test_vwap_slope_contributes():
    # flat price vs rising VWAP drift: build a steadily rising session, price at VWAP-ish top
    idx = pd.date_range(ts(DAY, 9, 30), periods=60, freq="1min")
    px = 100 + 0.02 * np.arange(60)
    up = pd.DataFrame({"open": px, "high": px + 0.01, "low": px - 0.01, "close": px, "volume": 1000.0}, index=idx)
    dn = up.copy()
    for c in ("open", "high", "low", "close"):
        dn[c] = 200.0 - up[c]
    su, sd = vwap_signal("X", up), vwap_signal("X", dn)
    assert su.detail["slope_pct"] > 0 > sd.detail["slope_pct"]
    assert su.direction > 0.3 and sd.direction < -0.3


def test_vwap_no_lookahead():
    bars, _ = build("up")
    for hh, mm in [(9, 36), (9, 45), (10, 0), (12, 17), (15, 59)]:
        now = ts(DAY, hh, mm)
        full = vwap_signal("SPY", bars, now=now)
        trunc = bars[(bars.index + pd.Timedelta(minutes=1)) <= now]
        assert vwap_signal("SPY", trunc) == full
        assert vwap_signal("SPY", trunc, now=now) == full
    mutated = bars.copy()
    fut = mutated.index >= ts(DAY, 10, 0)
    mutated.loc[fut, ["open", "high", "low", "close"]] = 300.0
    assert vwap_signal("SPY", mutated, now=ts(DAY, 10, 0)) == vwap_signal("SPY", bars, now=ts(DAY, 10, 0))


def test_vwap_neutral_on_bad_data():
    df, *_ = hand_frame()
    for b in (None, pd.DataFrame(), df.iloc[:3], df.assign(volume=0.0), df[["close"]]):
        s = vwap_signal("SPY", b)
        assert s.direction == 0.0 and s.confidence == 0.0 and s.detail["reason"]
    s = vwap_signal("SPY", df, now=ts(DAY, 9, 33))  # only 3 complete bars
    assert s.confidence == 0.0
    json.dumps(vwap_signal("SPY", df).detail)


# ------------------------------------------------------------------ gap
def gap_case(open_px: float, atr: float = 2.0):
    rng = np.random.default_rng(3)
    bars = make_session(DAY, {0: open_px, 389: open_px}, rng, prev_close=open_px, noise=0.001)
    return gap_signal("SPY", bars, daily_bars(DAY, atr=atr, close=100.0))


def test_gap_normalisation_arithmetic():
    s = gap_case(101.0)
    d = s.detail
    assert d["prev_close"] == 100.0 and d["open"] == 101.0
    assert d["gap_pct"] == pytest.approx(0.01)
    assert d["atr_pct"] == pytest.approx(0.02)
    assert d["gap_atr"] == pytest.approx(0.5)
    assert d["regime"] == "small"
    assert s.direction == pytest.approx(0.5) and s.confidence == pytest.approx(0.7)
    json.dumps(d)


def test_gap_regimes_and_direction():
    assert gap_case(100.05).confidence == 0.0 and gap_case(100.05).detail["regime"] == "none"
    small = gap_case(100.4)  # 0.2 ATR
    assert 0 < small.confidence < gap_case(101.0).confidence
    assert gap_case(99.0).direction < 0 < gap_case(101.0).direction
    mid = gap_case(102.0)  # 1 ATR
    large = gap_case(103.0)  # 1.5 ATR
    extreme = gap_case(106.0)  # 3 ATR
    assert large.detail["regime"] == "large" and extreme.detail["regime"] == "extreme"
    assert mid.confidence > large.confidence > extreme.confidence
    assert extreme.confidence <= 0.2 and abs(extreme.direction) <= 0.3
    assert gap_case(94.0).direction < 0  # gap-down extreme keeps sign


def test_gap_uses_prior_close_and_excludes_today_daily_bar():
    rng = np.random.default_rng(3)
    bars = make_session(DAY, {0: 101.0, 389: 101.0}, rng, prev_close=101.0, noise=0.001)
    s = gap_signal("SPY", bars, daily_bars(DAY, atr=2.0, today_row=True))
    assert s.detail["prev_close"] == 100.0 and s.detail["atr"] == pytest.approx(2.0)


def test_gap_neutral_on_missing_data():
    rng = np.random.default_rng(3)
    bars = make_session(DAY, {0: 101.0, 389: 101.0}, rng, prev_close=101.0)
    daily = daily_bars(DAY)
    for b, d in [(None, daily), (pd.DataFrame(), daily), (bars, None), (bars, daily.iloc[:5]), (bars, pd.DataFrame())]:
        s = gap_signal("SPY", b, d)
        assert s.direction == 0.0 and s.confidence == 0.0 and s.detail["reason"]
    s = gap_signal("SPY", bars, daily, now=ts(DAY, 9, 20))  # before the open
    assert s.confidence == 0.0


def test_gap_no_lookahead_only_needs_open():
    bars, daily = build("up")
    a = gap_signal("SPY", bars, daily, now=ts(DAY, 9, 31))
    b = gap_signal("SPY", bars, daily, now=ts(DAY, 15, 0))
    assert a == b

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from kwanttrades.config import SignalConfig
from kwanttrades.signals.opening_range import opening_range, orb_signal
from kwanttrades.types import DirectionalSignal
from tests.test_signals_synth import DAY, build, daily_bars, make_session, ts

CFG = SignalConfig()
REQUIRED = {"or_high", "or_low", "or_mid", "entry", "stop", "target", "risk_per_share", "rel_volume",
            "breakout_time", "state"}


def run(scn, now=None, **kw):
    bars, daily = build(scn, **kw)
    return orb_signal("SPY", bars, daily, CFG, now=now), bars, daily


def or_levels(bars):
    r = bars[(bars.index >= ts(DAY, 9, 30)) & (bars.index < ts(DAY, 9, 45))]
    return float(r.high.max()), float(r.low.min()), float(r.volume.sum())


def assert_neutral(sig, state=None):
    assert isinstance(sig, DirectionalSignal)
    assert sig.direction == 0.0 and sig.confidence == 0.0
    assert sig.detail.get("reason")
    if state:
        assert sig.detail["state"] == state


# ------------------------------------------------------------------ opening range
def test_opening_range_regular_session_only_and_multiday():
    bars, _ = build("up")
    r = opening_range(bars)  # default: date of last bar
    hi, lo, vol = or_levels(bars)
    assert r is not None
    assert r.high == pytest.approx(hi) and r.low == pytest.approx(lo)
    assert r.mid == pytest.approx((hi + lo) / 2) and r.volume == pytest.approx(vol)
    assert r.start == ts(DAY, 9, 30) and r.end == ts(DAY, 9, 45)
    assert r.high < 101  # pre-market spikes to 140 are excluded
    prev = bars.index[bars.index.date < DAY][-1].date()
    r_prev = opening_range(bars, session_date=prev)
    assert r_prev is not None and r_prev.end.date() == prev and r_prev.high < 101


def test_opening_range_incomplete_or_missing():
    bars, _ = build("up")
    early = bars[bars.index < ts(DAY, 9, 40)]
    assert opening_range(early) is None
    assert opening_range(pd.DataFrame()) is None
    assert opening_range(None) is None
    assert opening_range(early.iloc[:0]) is None
    assert opening_range(bars[bars.index < ts(DAY, 9, 45)]) is not None  # 09:30..09:44 present


# ------------------------------------------------------------------ scenarios
def test_upside_breakout_levels():
    sig, bars, _ = run("up")
    hi, lo, vol = or_levels(bars)
    d = sig.detail
    assert REQUIRED <= set(d)
    assert d["state"] == "triggered" and sig.direction > 0 and sig.confidence > 0
    assert d["or_high"] == pytest.approx(hi) and d["or_low"] == pytest.approx(lo)
    assert d["or_mid"] == pytest.approx((hi + lo) / 2)
    assert d["breakout_time"] == ts(DAY, 9, 50).isoformat()
    close_0954 = float(bars.loc[ts(DAY, 9, 54), "close"])
    assert d["entry"] == pytest.approx(close_0954)  # close of the first breakout 5m bar
    assert close_0954 > hi
    assert d["stop"] == pytest.approx(d["or_mid"])
    assert d["risk_per_share"] == pytest.approx(d["entry"] - d["or_mid"])
    assert d["target"] == pytest.approx(d["entry"] + d["risk_per_share"])
    assert d["rel_volume"] == pytest.approx(3.0, rel=0.15) and d["rel_volume_source"] == "prior_days"
    assert d["failed"] is False
    assert 0 < sig.confidence <= 1 and 0 < sig.direction <= 1


def test_downside_breakout_levels():
    sig, bars, _ = run("down")
    d = sig.detail
    assert d["state"] == "triggered" and sig.direction < 0 and sig.confidence > 0
    assert d["entry"] < d["or_low"]
    assert d["stop"] == pytest.approx(d["or_mid"])
    assert d["risk_per_share"] == pytest.approx(d["or_mid"] - d["entry"])
    assert d["target"] == pytest.approx(d["entry"] - d["risk_per_share"])
    assert d["target"] < d["entry"] < d["stop"]


def test_breakout_fails_back_through_midpoint():
    sig, _, _ = run("fail")
    assert_neutral(sig, "failed")
    assert sig.detail["failed"] is True
    assert sig.detail["entry"] is not None and sig.detail["breakout_time"] is not None
    # sticky: later bars do not revive it
    sig2, _, _ = run("fail", now=ts(DAY, 15, 0))
    assert sig2.detail["state"] == "failed"


def test_fail_is_not_triggered_before_the_failing_bar_completes():
    # now 09:54:00 -> 09:55 bar not yet started; breakout bar complete -> still triggered
    sig, _, _ = run("fail", now=ts(DAY, 9, 55))
    assert sig.detail["state"] == "triggered" and sig.direction > 0
    # 09:59:00: the 09:55 bar still lacks its 09:59 1m bar -> not complete -> cannot fail yet
    sig, _, _ = run("fail", now=ts(DAY, 9, 59))
    assert sig.detail["state"] == "triggered"
    sig, _, _ = run("fail", now=ts(DAY, 10, 0))
    assert sig.detail["state"] == "failed"


def test_no_breakout_waiting():
    sig, bars, _ = run("no_breakout")
    assert_neutral(sig, "waiting")
    assert sig.detail["reason"] == "no_breakout_yet"
    assert sig.detail["or_high"] is not None and sig.detail["entry"] is None


def test_target_hit():
    sig, _, _ = run("target")
    assert_neutral(sig, "target_hit")
    d = sig.detail
    assert d["bias"] == 1.0 and d["target"] == pytest.approx(d["entry"] + d["risk_per_share"])
    # at 09:58 the move has not yet printed the target on a completed bar
    mid_sig, _, _ = run("target", now=ts(DAY, 9, 55))
    assert mid_sig.detail["state"] == "triggered"


def test_over_wide_range_filtered():
    sig, _, _ = run("wide")
    assert_neutral(sig, "waiting")
    assert sig.detail["reason"] == "range_too_wide" and sig.detail["wide_range"] is True
    assert sig.detail["range_atr_ratio"] > 1.5
    # same scenario but ATR large enough => tradable
    sig2, _, _ = run("wide", atr=8.0)
    assert sig2.detail.get("reason") != "range_too_wide"


def test_before_0945_and_exactly_0945():
    bars, daily = build("up")
    for now in (ts(DAY, 9, 20), ts(DAY, 9, 31), ts(DAY, 9, 44, 30)):
        s = orb_signal("SPY", bars, daily, CFG, now=now)
        assert_neutral(s, "waiting")
        assert s.detail["reason"] in ("opening_range_incomplete", "no_session_bars")
    s = orb_signal("SPY", bars, daily, CFG, now=ts(DAY, 9, 45))  # range just complete, no 5m bar yet
    assert_neutral(s, "waiting")
    assert s.detail["reason"] == "no_breakout_yet"
    assert s.detail["or_high"] is not None


def test_missing_and_bad_data_is_neutral():
    bars, daily = build("up")
    for b, dl in [(None, daily), (pd.DataFrame(), daily), (bars, None), (bars, pd.DataFrame()),
                  (bars, daily.iloc[:10]), (bars.iloc[:0], daily), (bars[["close"]], daily),
                  (bars.reset_index(drop=True), daily)]:
        s = orb_signal("SPY", b, dl, CFG)
        assert_neutral(s)
        assert REQUIRED <= set(s.detail)
    # now on a date with no bars
    s = orb_signal("SPY", bars, daily, CFG, now=ts(DAY.replace(day=15), 10, 30))
    assert_neutral(s)


def test_today_partial_daily_bar_excluded_from_atr():
    sig, _, _ = run("up", today_daily=True)
    assert sig.detail["atr"] == pytest.approx(2.0)
    assert sig.detail["state"] == "triggered"


def test_pre_post_rows_ignored():
    a, _, _ = run("up", pre_post=True)
    b, _, _ = run("up", pre_post=False)
    assert a.detail["or_high"] == pytest.approx(b.detail["or_high"])  # same seeds => same regular bars? (rng stream differs)
    assert a.detail["state"] == b.detail["state"] == "triggered"


def test_rel_volume_drives_confidence():
    hi, _, _ = run("up", or_vol_mult=3.0)
    lo, _, _ = run("up", or_vol_mult=0.5)
    assert lo.detail["rel_volume"] < 1.2 <= hi.detail["rel_volume"]
    assert lo.confidence < hi.confidence


def test_rel_volume_fallback_to_daily_share_and_assumed():
    # no prior intraday days -> daily average volume share
    bars, daily = build("up", n_prior=0)
    s = orb_signal("SPY", bars, daily_bars(DAY, volume=1.0e6), CFG)
    assert s.detail["rel_volume_source"] == "daily_share" and s.detail["rel_volume"] > 0
    nov = daily.drop(columns=["volume"])
    s = orb_signal("SPY", bars, nov, CFG)
    assert s.detail["rel_volume_source"] == "assumed" and s.detail["rel_volume"] == 1.0


def test_gap_alignment_changes_confidence():
    bars, daily = build("up")
    daily_up = daily.copy()
    daily_up[["open", "high", "low", "close"]] -= 1.0  # prior close 99 => gap +0.5 ATR vs open ~100
    daily_dn = daily.copy()
    daily_dn[["open", "high", "low", "close"]] += 1.0  # prior close 101 => gap down
    a = orb_signal("SPY", bars, daily_up, CFG)
    o = orb_signal("SPY", bars, daily_dn, CFG)
    assert a.detail["gap_alignment"] == "aligned" and o.detail["gap_alignment"] == "opposed"
    assert a.confidence > o.confidence


def test_overextension_penalised():
    base = {0: 100.0, 4: 100.25, 9: 99.75, 14: 100.1, 19: 100.2}
    near = {**base, 24: 100.55, 389: 100.55}
    far = {**base, 24: 101.3, 389: 101.3}  # > 1 ATR (0.8) beyond the range high
    s1, _, _ = run(near, atr=0.8)
    s2, _, _ = run(far, atr=0.8)
    assert s1.detail["state"] == s2.detail["state"] == "triggered"
    assert s2.detail["extension_atr"] > 1.0 > s1.detail["extension_atr"]
    assert s2.confidence < s1.confidence


# ------------------------------------------------------------------ no look-ahead
NOWS = [(9, 31), (9, 44), (9, 45), (9, 49), (9, 50), (9, 53), (9, 54), (9, 55), (9, 59), (10, 0), (10, 5),
        (10, 30), (12, 0), (15, 59)]


@pytest.mark.parametrize("scn", ["up", "fail", "target", "down", "no_breakout"])
def test_no_lookahead_equals_truncated_frame(scn):
    bars, daily = build(scn)
    for hh, mm in NOWS:
        now = ts(DAY, hh, mm)
        full = orb_signal("SPY", bars, daily, CFG, now=now)
        trunc = bars[(bars.index + pd.Timedelta(minutes=1)) <= now]
        assert orb_signal("SPY", trunc, daily, CFG, now=now) == full
        assert orb_signal("SPY", trunc, daily, CFG) == full  # now=None on truncated frame


def test_no_lookahead_future_mutation_does_not_matter():
    bars, daily = build("up")
    now = ts(DAY, 9, 52)  # 09:50 5m bar incomplete
    base = orb_signal("SPY", bars, daily, CFG, now=now)
    mutated = bars.copy()
    fut = mutated.index >= now
    mutated.loc[fut, ["open", "high", "low", "close"]] = 500.0
    mutated.loc[fut, "volume"] = 1e9
    assert orb_signal("SPY", mutated, daily, CFG, now=now) == base
    assert base.detail["state"] == "waiting"  # breakout 5m bar not complete at 09:52


def test_incomplete_5m_bar_is_not_used():
    # at 09:53 the 09:50 bar has 1m bars 09:50..09:52 only; even though they close above the range, not complete
    bars, daily = build("up")
    s = orb_signal("SPY", bars, daily, CFG, now=ts(DAY, 9, 53))
    assert s.detail["state"] == "waiting"
    s = orb_signal("SPY", bars, daily, CFG, now=ts(DAY, 9, 55))
    assert s.detail["state"] == "triggered"


def test_missing_last_minute_of_5m_bar_means_incomplete():
    bars, daily = build("up")
    gapped = bars.drop(ts(DAY, 9, 54))
    s = orb_signal("SPY", gapped, daily, CFG, now=ts(DAY, 10, 5))
    assert s.detail["breakout_time"] != ts(DAY, 9, 50).isoformat()


# ------------------------------------------------------------------ misc contract
@pytest.mark.parametrize("scn", ["up", "down", "fail", "no_breakout", "target", "wide"])
def test_json_serialisable_and_clamped(scn):
    sig, _, _ = run(scn)
    json.dumps(sig.detail)
    assert -1 <= sig.direction <= 1 and 0 <= sig.confidence <= 1
    assert sig.source == "orb" and sig.symbol == "SPY"
    assert REQUIRED <= set(sig.detail)
    for v in sig.detail.values():
        assert not isinstance(v, (np.generic, pd.Timestamp))


def test_naive_index_is_treated_as_new_york():
    bars, daily = build("up", pre_post=False)
    naive = bars.copy()
    naive.index = naive.index.tz_localize(None)
    assert orb_signal("SPY", naive, daily, CFG) == orb_signal("SPY", bars, daily, CFG)


def test_utc_index_is_converted():
    bars, daily = build("up")
    utc = bars.copy()
    utc.index = utc.index.tz_convert("UTC")
    assert orb_signal("SPY", utc, daily, CFG) == orb_signal("SPY", bars, daily, CFG)


def test_single_session_frame_without_priors_works():
    rng = np.random.default_rng(1)
    from tests.test_signals_synth import SCENARIOS
    bars = make_session(DAY, SCENARIOS["up"], rng, pre_post=False)
    s = orb_signal("SPY", bars, daily_bars(DAY), CFG)
    assert s.detail["state"] == "triggered" and s.detail["rel_volume_source"] == "daily_share"

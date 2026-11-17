"""Opening-range breakout (ORB) signal.

Rules
-----
* Opening range = regular-session 1-minute bars stamped 09:30 .. 09:30+minutes-1 (09:30..09:44).
* Nothing is decided until the range is complete (``now`` >= 09:45).
* Breakout = the FIRST completed breakout-timeframe (5 minute) bar after the range whose CLOSE is above
  OR-high (long) or below OR-low (short). A 5-minute bar is complete only when its last 1-minute bar
  exists (and, because only complete 1-minute bars are visible, ends at or before ``now``).
* Entry = close of the breakout bar, stop = range midpoint, target = entry +/- 1R (R = |entry - stop|).
* Later completed bars: a close back through the midpoint => state "failed" (direction 0, sticky);
  otherwise a bar reaching the target => "target_hit" (trade complete, direction 0, bias kept in detail).
* Ranges wider than 1.5 x the 14-day Wilder ATR are not traded.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from kwanttrades.config import SignalConfig
from kwanttrades.types import DirectionalSignal

from ._common import (ONE_MIN, atr_wilder, clamp, clean_daily, make_signal, neutral, normalize_bars,
                      resolve_date, session_bars, session_start, to_ny, visible_bars)

SOURCE = "orb"
MAX_RANGE_ATR = 1.5
DAILY_VOLUME_OR_SHARE = 0.10  # assumed share of daily volume traded in the first 15 minutes
MIN_PRIOR_DAYS = 2
MAX_PRIOR_DAYS = 10


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float
    mid: float
    volume: float
    start: datetime
    end: datetime
    n_bars: int = 0

    @property
    def width(self) -> float:
        return self.high - self.low


def _range_from_session(sess: pd.DataFrame, minutes: int, d: date, eff_now: pd.Timestamp) -> OpeningRange | None:
    """Opening range from (already visible) session bars; None while the range is still incomplete."""
    start = session_start(d)
    end = start + pd.Timedelta(minutes=minutes)
    if sess.empty or eff_now < end:
        return None
    rng = sess[(sess.index >= start) & (sess.index < end)]
    if rng.empty:
        return None
    hi, lo = float(rng["high"].max()), float(rng["low"].min())
    return OpeningRange(high=hi, low=lo, mid=0.5 * (hi + lo), volume=float(rng["volume"].sum()),
                        start=start.to_pydatetime(), end=end.to_pydatetime(), n_bars=int(len(rng)))


def opening_range(bars_1m: pd.DataFrame, minutes: int = 15, session_date: date | None = None) -> OpeningRange | None:
    """High/low/mid/volume of the 09:30..09:30+minutes NY regular-session range, or None when the data
    is missing or the range is not complete yet (last visible bar ends before 09:45)."""
    try:
        bars = normalize_bars(bars_1m)
        if bars is None:
            return None
        d = resolve_date(bars, None, session_date)
        sess = session_bars(bars, d)
        if sess.empty:
            return None
        return _range_from_session(sess, minutes, d, sess.index[-1] + ONE_MIN)
    except Exception:  # noqa: BLE001 - public API must never raise on bad data
        return None


# ---------------------------------------------------------------------------- helpers
def _bars_n_min(vis: pd.DataFrame, origin: pd.Timestamp, n: int) -> list[dict[str, Any]]:
    """Completed n-minute bars (anchored at ``origin``) built from visible 1-minute bars."""
    post = vis[vis.index >= origin]
    if post.empty:
        return []
    step = pd.Timedelta(minutes=n)
    k = ((post.index - origin) // step).to_numpy()
    out: list[dict[str, Any]] = []
    for kk in np.unique(k):
        g = post[k == kk]
        b_start = origin + step * int(kk)
        if g.index[-1] != b_start + step - ONE_MIN:  # last 1-minute bar missing => bar not complete
            continue
        out.append({"start": b_start, "end": b_start + step, "open": float(g["open"].iloc[0]),
                    "high": float(g["high"].max()), "low": float(g["low"].min()),
                    "close": float(g["close"].iloc[-1]), "volume": float(g["volume"].sum())})
    return out


def _relative_volume(bars: pd.DataFrame, d: date, minutes: int, or_volume: float,
                     daily: pd.DataFrame | None) -> tuple[float, str, int]:
    prior_dates = sorted({ts.date() for ts in bars.index if ts.date() < d})[-MAX_PRIOR_DAYS:]
    vols: list[float] = []
    for pd_ in prior_dates:
        s = session_bars(bars, pd_)
        if s.empty:
            continue
        r = _range_from_session(s, minutes, pd_, s.index[-1] + ONE_MIN)
        if r is not None and r.volume > 0:
            vols.append(r.volume)
    if len(vols) >= MIN_PRIOR_DAYS:
        return or_volume / float(np.mean(vols)), "prior_days", len(vols)
    if daily is not None and "volume" in daily.columns:
        v = daily["volume"].tail(20)
        v = v[v > 0]
        if len(v) >= 5:
            return or_volume / (float(v.mean()) * DAILY_VOLUME_OR_SHARE), "daily_share", int(len(v))
    return 1.0, "assumed", 0


def _levels(state: str, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"state": state, "or_high": None, "or_low": None, "or_mid": None, "entry": None,
                         "stop": None, "target": None, "risk_per_share": None, "rel_volume": None,
                         "breakout_time": None, "failed": False}
    d.update(extra)
    return d


# ---------------------------------------------------------------------------- signal
def orb_signal(symbol: str, bars_1m: pd.DataFrame, daily_bars: pd.DataFrame, cfg: SignalConfig,
               now: datetime | None = None) -> DirectionalSignal:
    try:
        return _orb(symbol, bars_1m, daily_bars, cfg, now)
    except Exception as exc:  # noqa: BLE001 - never raise from a signal
        return neutral(symbol, SOURCE, f"error: {type(exc).__name__}: {exc}", **_levels("waiting"))


def _orb(symbol: str, bars_1m: Any, daily_bars: Any, cfg: SignalConfig, now: datetime | None) -> DirectionalSignal:
    bars = normalize_bars(bars_1m)
    if bars is None:
        return neutral(symbol, SOURCE, "no_intraday_data", **_levels("waiting"))
    now_ts = to_ny(now)
    d = resolve_date(bars, now_ts)
    sess = session_bars(bars, d)
    vis = visible_bars(sess, now_ts)
    if vis.empty:
        return neutral(symbol, SOURCE, "no_session_bars", session_date=d, **_levels("waiting"))
    eff_now = now_ts if now_ts is not None else vis.index[-1] + ONE_MIN
    minutes, bm = int(cfg.opening_range_minutes), int(cfg.breakout_bar_minutes)
    start = session_start(d)
    or_end = start + pd.Timedelta(minutes=minutes)

    orng = _range_from_session(vis, minutes, d, eff_now)
    if orng is None:
        return neutral(symbol, SOURCE, "opening_range_incomplete", session_date=d, **_levels("waiting"))

    base = dict(session_date=d, or_high=orng.high, or_low=orng.low, or_mid=orng.mid, or_volume=orng.volume)
    daily = clean_daily(daily_bars, before=d)  # drops today's partial daily bar
    atr = atr_wilder(daily, 14)
    if atr is None:
        return neutral(symbol, SOURCE, "insufficient_daily_history", **_levels("waiting", **base))
    width = orng.high - orng.low
    base.update(atr=atr, range_atr_ratio=width / atr)
    if width <= 0:
        return neutral(symbol, SOURCE, "degenerate_range", **_levels("waiting", **base))
    if width > MAX_RANGE_ATR * atr:
        return neutral(symbol, SOURCE, "range_too_wide", **_levels("waiting", wide_range=True, **base))

    rel_vol, rv_src, rv_n = _relative_volume(bars, d, minutes, orng.volume, daily)
    base.update(rel_volume=rel_vol, rel_volume_source=rv_src, rel_volume_days=rv_n)

    mid, hi, lo = orng.mid, orng.high, orng.low
    side, state = 0, "waiting"
    entry = stop = target = risk = None
    breakout: dict[str, Any] | None = None
    for b in _bars_n_min(vis, or_end, bm):
        if side == 0:
            if b["close"] > hi:
                side = 1
            elif b["close"] < lo:
                side = -1
            if side:
                breakout, entry, stop = b, b["close"], mid
                risk = abs(entry - stop)
                target = entry + side * risk
                state = "triggered"
            continue
        if (side == 1 and b["close"] < mid) or (side == -1 and b["close"] > mid):
            state = "failed"
            break
        if (side == 1 and b["high"] >= target) or (side == -1 and b["low"] <= target):
            state = "target_hit"
            break

    levels = _levels(state, **base)
    if breakout is None:
        return neutral(symbol, SOURCE, "no_breakout_yet", **levels)

    levels.update(entry=entry, stop=stop, target=target, risk_per_share=risk,
                  breakout_time=breakout["start"].isoformat(), breakout_bar_end=breakout["end"].isoformat(),
                  side="long" if side == 1 else "short")
    if state == "failed":
        levels["failed"] = True
        return neutral(symbol, SOURCE, "failed_breakout", **levels)
    if state == "target_hit":
        levels["bias"] = float(side)
        return neutral(symbol, SOURCE, "target_hit", **levels)

    # ---- confidence (state == "triggered")
    min_rv = max(float(cfg.min_relative_volume), 1e-9)
    f_v = 0.5 * rel_vol / min_rv if rel_vol < min_rv else 0.5 + 0.5 * clamp(rel_vol - min_rv, 0.0, 1.0)
    f_v = clamp(f_v, 0.0, 1.0)
    last_close = _bars_last_close(vis, or_end, bm, breakout)
    edge = hi if side == 1 else lo
    ext = side * (last_close - edge) / atr
    ext = max(ext, 0.0)
    f_e = 1.0 - 0.2 * ext if ext <= 1.0 else max(0.1, 0.8 - 0.6 * (ext - 1.0))
    gap_atr, alignment, f_g = _gap_alignment(vis, daily, atr, side)
    conf = 0.85 * f_v * f_e * f_g
    direction = side * clamp(0.5 + 0.5 * f_v * f_e, 0.0, 1.0)
    levels.update(extension_atr=ext, gap_atr=gap_atr, gap_alignment=alignment, last_price=float(vis["close"].iloc[-1]),
                  conf_factors={"rel_volume": f_v, "extension": f_e, "gap": f_g})
    return make_signal(symbol, SOURCE, direction, conf, levels)


def _bars_last_close(vis: pd.DataFrame, origin: pd.Timestamp, n: int, fallback: dict[str, Any]) -> float:
    bars = _bars_n_min(vis, origin, n)
    return float(bars[-1]["close"]) if bars else float(fallback["close"])


def _gap_alignment(vis: pd.DataFrame, daily: pd.DataFrame | None, atr: float, side: int) -> tuple[float | None, str, float]:
    if daily is None or daily.empty:
        return None, "unknown", 0.9
    prev_close = float(daily["close"].iloc[-1])
    gap_atr = (float(vis["open"].iloc[0]) - prev_close) / atr
    if abs(gap_atr) < 0.1:
        return gap_atr, "none", 0.9
    if np.sign(gap_atr) == side:
        return gap_atr, "aligned", 1.0
    return gap_atr, "opposed", 0.7

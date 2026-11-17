"""Overnight gap signal, normalised by ATR%.

gap_atr = (open - prior_close) / ATR14.  |gap_atr| < 0.1 is noise (neutral); up to 1 ATR the gap is a
continuation bias that strengthens with size; 1-2 ATR confidence decays; beyond 2 ATR the gap is treated as
fade-risk (low confidence, small direction).
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from kwanttrades.types import DirectionalSignal

from ._common import (atr_wilder, clamp, clean_daily, make_signal, neutral, normalize_bars, resolve_date,
                      session_bars, to_ny, visible_bars)

SOURCE = "gap"


def gap_signal(symbol: str, bars_1m: pd.DataFrame, daily_bars: pd.DataFrame,
               now: datetime | None = None) -> DirectionalSignal:
    try:
        return _gap(symbol, bars_1m, daily_bars, now)
    except Exception as exc:  # noqa: BLE001
        return neutral(symbol, SOURCE, f"error: {type(exc).__name__}: {exc}")


def _gap(symbol: str, bars_1m: object, daily_bars: object, now: datetime | None) -> DirectionalSignal:
    bars = normalize_bars(bars_1m)
    if bars is None:
        return neutral(symbol, SOURCE, "no_intraday_data")
    now_ts = to_ny(now)
    d = resolve_date(bars, now_ts)
    vis = visible_bars(session_bars(bars, d), now_ts)
    if vis.empty:
        return neutral(symbol, SOURCE, "no_session_bars")
    daily = clean_daily(daily_bars, before=d)
    if daily is None:
        return neutral(symbol, SOURCE, "no_daily_data")
    atr = atr_wilder(daily, 14)
    if atr is None:
        return neutral(symbol, SOURCE, "insufficient_daily_history")
    prev_close = float(daily["close"].iloc[-1])
    if prev_close <= 0:
        return neutral(symbol, SOURCE, "bad_prior_close")
    open_px = float(vis["open"].iloc[0])
    gap = open_px - prev_close
    gap_pct, atr_pct = gap / prev_close, atr / prev_close
    g = gap / atr
    a = abs(g)
    sign = float(np.sign(g))
    if a < 0.1:
        regime, direction, conf = "none", 0.0, 0.0
    elif a <= 1.0:
        regime, direction, conf = "small", sign * min(a, 1.0), 0.7 * clamp(a / 0.5, 0.0, 1.0)
    elif a <= 2.0:
        regime, direction, conf = "large", sign * (1.0 - 0.5 * (a - 1.0)), 0.7 - 0.4 * (a - 1.0)
    else:
        regime, direction, conf = "extreme", sign * 0.3, 0.15
    detail = {"gap_pct": gap_pct, "atr_pct": atr_pct, "gap_atr": g, "prev_close": prev_close, "open": open_px,
              "atr": atr, "regime": regime, "session_date": d}
    return make_signal(symbol, SOURCE, direction, conf, detail)

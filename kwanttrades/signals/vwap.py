"""Session VWAP signal.

VWAP uses the typical price (H+L+C)/3 weighted by volume, cumulated from 09:30 over COMPLETE 1-minute bars.
Bands are +/- 1 volume-weighted standard deviation of the typical price around VWAP.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from kwanttrades.types import DirectionalSignal

from ._common import (ONE_MIN, clamp, make_signal, neutral, normalize_bars, resolve_date, session_bars,
                      to_ny, visible_bars)

SOURCE = "vwap"
MIN_BARS = 5
SLOPE_MINUTES = 15


def vwap_series(sess: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Cumulative VWAP and volume-weighted std of typical price for session bars."""
    tp = (sess["high"] + sess["low"] + sess["close"]) / 3.0
    v = sess["volume"]
    cv = v.cumsum().replace(0.0, np.nan)
    vw = (tp * v).cumsum() / cv
    var = ((tp * tp * v).cumsum() / cv - vw * vw).clip(lower=0.0)
    return vw, np.sqrt(var)


def vwap_signal(symbol: str, bars_1m: pd.DataFrame, now: datetime | None = None) -> DirectionalSignal:
    try:
        return _vwap(symbol, bars_1m, now)
    except Exception as exc:  # noqa: BLE001
        return neutral(symbol, SOURCE, f"error: {type(exc).__name__}: {exc}")


def _vwap(symbol: str, bars_1m: object, now: datetime | None) -> DirectionalSignal:
    bars = normalize_bars(bars_1m)
    if bars is None:
        return neutral(symbol, SOURCE, "no_intraday_data")
    now_ts = to_ny(now)
    d = resolve_date(bars, now_ts)
    vis = visible_bars(session_bars(bars, d), now_ts)
    if len(vis) < MIN_BARS:
        return neutral(symbol, SOURCE, "insufficient_bars", n_bars=int(len(vis)))
    vw, sd = vwap_series(vis)
    vw_ok = vw.dropna()
    if vw_ok.empty:
        return neutral(symbol, SOURCE, "no_volume")
    vwap, std = float(vw_ok.iloc[-1]), float(sd.loc[vw_ok.index[-1]])
    price = float(vis["close"].iloc[-1])
    z = (price - vwap) / std if std > 1e-12 else 0.0

    t_ref = vis.index[-1] - pd.Timedelta(minutes=SLOPE_MINUTES)
    ref = vw_ok[vw_ok.index <= t_ref]
    slope_pct = float((vwap - ref.iloc[-1]) / vwap) if len(ref) else 0.0
    slope_score = float(np.tanh(slope_pct / 0.0005))

    direction = 0.7 * float(np.tanh(z / 1.5)) + 0.3 * slope_score
    sufficiency = 0.4 + 0.6 * min(1.0, len(vis) / 30.0)
    agree = 1.0 if (slope_pct == 0.0 or z == 0.0 or np.sign(z) == np.sign(slope_pct)) else 0.6
    conf = min(0.9, min(abs(z), 2.0) / 2.0 * sufficiency * agree)
    detail = {
        "vwap": vwap, "upper": vwap + std, "lower": vwap - std, "std": std, "price": price, "z": z,
        "slope_pct": slope_pct, "n_bars": int(len(vis)), "session_date": d,
        "asof": (vis.index[-1] + ONE_MIN).isoformat(),
    }
    return make_signal(symbol, SOURCE, clamp(direction, -1, 1), conf, detail)

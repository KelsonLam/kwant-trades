"""Market regime signal: SPY trend, VIX level percentile and VIX/VIX3M term structure.

detail["risk_multiplier"] in [0.25, 1.0] scales position size.  VIX backwardation (VIX/VIX3M > 1) or
VIX > 30 caps it at 0.5 (down to 0.25 for extreme readings).  When data is missing the signal is neutral
and risk_multiplier is 1.0 (no information, no haircut).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from kwanttrades.types import DirectionalSignal

from ._common import clamp, clean_daily, make_signal, neutral

SOURCE = "regime"
SYMBOL = "MARKET"
MIN_SPY = 50
MIN_VIX = 20


def _linear(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if x1 == x0:
        return y1
    t = clamp((x - x0) / (x1 - x0), 0.0, 1.0)
    return y0 + t * (y1 - y0)


def regime_signal(spy_daily: pd.DataFrame, vix_daily: pd.DataFrame,
                  vix3m_daily: pd.DataFrame | None = None) -> DirectionalSignal:
    try:
        return _regime(spy_daily, vix_daily, vix3m_daily)
    except Exception as exc:  # noqa: BLE001
        return neutral(SYMBOL, SOURCE, f"error: {type(exc).__name__}: {exc}", risk_multiplier=1.0)


def _regime(spy_daily: object, vix_daily: object, vix3m_daily: object) -> DirectionalSignal:
    spy = clean_daily(spy_daily)
    vix = clean_daily(vix_daily)
    if spy is None or len(spy) < MIN_SPY:
        return neutral(SYMBOL, SOURCE, "insufficient_spy_history", risk_multiplier=1.0)
    if vix is None or len(vix) < MIN_VIX:
        return neutral(SYMBOL, SOURCE, "insufficient_vix_history", risk_multiplier=1.0)

    close = spy["close"]
    c = float(close.iloc[-1])
    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
    trend = clamp(0.5 * np.tanh((c / ema20 - 1.0) / 0.02) + 0.5 * np.tanh((c / ema50 - 1.0) / 0.04), -1.0, 1.0)
    below_both = c < ema20 and c < ema50

    vclose = vix["close"].tail(252)
    v = float(vclose.iloc[-1])
    pctile = float(((vclose < v).sum() + 0.5 * (vclose == v).sum()) / len(vclose))  # mid-rank percentile

    ratio = None
    term = "unknown"
    v3 = clean_daily(vix3m_daily) if vix3m_daily is not None else None
    if v3 is not None:
        aligned = pd.concat([vix["close"], v3["close"]], axis=1, keys=["v", "v3"]).dropna()
        if len(aligned):
            last = aligned.iloc[-1]
            if last["v3"] > 0:
                ratio = float(last["v"] / last["v3"])
                term = "backwardation" if ratio > 1.0 else "contango"

    vix_m = 1.0 if v <= 20 else _linear(v, 20, 30, 1.0, 0.5) if v <= 30 else _linear(v, 30, 40, 0.5, 0.25)
    if ratio is None:
        term_m = 1.0
    elif ratio <= 0.9:
        term_m = 1.0
    elif ratio <= 1.0:
        term_m = _linear(ratio, 0.9, 1.0, 1.0, 0.5)
    else:
        term_m = _linear(ratio, 1.0, 1.15, 0.5, 0.25)
    pct_f = 1.0 if pctile <= 0.8 else _linear(pctile, 0.8, 1.0, 1.0, 0.9)
    trend_f = 0.85 if below_both else 1.0
    risk_mult = clamp(min(vix_m, term_m) * pct_f * trend_f, 0.25, 1.0)

    direction = clamp(0.8 * trend - 0.4 * (1.0 - risk_mult), -1.0, 1.0)
    conf = clamp(0.2 + 0.6 * abs(direction), 0.0, 0.8)
    detail = {
        "trend": trend, "spy_close": c, "ema20": ema20, "ema50": ema50, "below_both_emas": below_both,
        "vix": v, "vix_percentile": pctile, "vix_ratio": ratio, "term_structure": term,
        "risk_multiplier": risk_mult, "risk_off": bool(risk_mult < 0.5 or term == "backwardation"),
        "asof": spy.index[-1].date().isoformat(),
    }
    return make_signal(SYMBOL, SOURCE, direction, conf, detail)

"""Shared helpers for the signal modules (private).

Everything here is pure and defensive: bad input yields ``None`` / neutral signals, never an exception.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
import pandas as pd

from kwanttrades.types import MARKET_CLOSE, MARKET_OPEN, NY, DirectionalSignal

ONE_MIN = pd.Timedelta(minutes=1)


# ------------------------------------------------------------------ numeric / json helpers
def clamp(x: Any, lo: float, hi: float) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        v = 0.0
    if math.isnan(v):
        v = 0.0
    return float(min(max(v, lo), hi))


def to_jsonable(obj: Any) -> Any:
    """Recursively convert to plain JSON-serialisable python (NaN/inf -> None, timestamps -> ISO)."""
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, np.generic):
        obj = obj.item()
        if obj is None or isinstance(obj, (str, bool)):
            return obj
    if isinstance(obj, (int,)):
        return int(obj)
    if isinstance(obj, float):
        return float(obj) if math.isfinite(obj) else None
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [to_jsonable(v) for v in obj.tolist()]
    return str(obj)


def make_signal(symbol: str, source: str, direction: float, confidence: float,
                detail: dict[str, Any] | None = None) -> DirectionalSignal:
    return DirectionalSignal(
        symbol=symbol, source=source,
        direction=clamp(direction, -1.0, 1.0), confidence=clamp(confidence, 0.0, 1.0),
        detail=to_jsonable(detail or {}),
    )


def neutral(symbol: str, source: str, reason: str, **detail: Any) -> DirectionalSignal:
    d = {"reason": reason}
    d.update(detail)
    return make_signal(symbol, source, 0.0, 0.0, d)


# ------------------------------------------------------------------ time helpers
def to_ny(ts: Any) -> pd.Timestamp | None:
    if ts is None:
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize(NY)
    return t.tz_convert(NY)


def session_start(d: date, t: time = MARKET_OPEN) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(d, t, tzinfo=NY))


def session_end(d: date) -> pd.Timestamp:
    return session_start(d, MARKET_CLOSE)


# ------------------------------------------------------------------ bars
def normalize_bars(bars: Any) -> pd.DataFrame | None:
    """Return a clean copy: NY tz-aware sorted unique index, lower-case float OHLCV, no NaN OHLC rows."""
    if not isinstance(bars, pd.DataFrame) or bars.empty:
        return None
    df = bars.copy()
    df.columns = [str(c).lower() for c in df.columns]
    if not {"open", "high", "low", "close"}.issubset(df.columns):
        return None
    if "volume" not in df.columns:
        df["volume"] = 0.0
    if not isinstance(df.index, pd.DatetimeIndex):
        return None
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize(NY, ambiguous="NaT", nonexistent="NaT")
    else:
        idx = idx.tz_convert(NY)
    df.index = idx
    df = df[~df.index.isna()]
    df = df[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
    df["volume"] = df["volume"].fillna(0.0)
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return None if df.empty else df


def session_bars(bars: pd.DataFrame, d: date) -> pd.DataFrame:
    """Regular-session (09:30 <= t < 16:00 NY) rows of session date ``d``."""
    s, e = session_start(d), session_end(d)
    return bars[(bars.index >= s) & (bars.index < e)]


def visible_bars(sess: pd.DataFrame, now: pd.Timestamp | None) -> pd.DataFrame:
    """Only 1-minute bars that are complete at ``now`` (stamp + 1 minute <= now)."""
    if now is None:
        return sess
    return sess[(sess.index + ONE_MIN) <= now]


def resolve_date(bars: pd.DataFrame, now: pd.Timestamp | None, session_date: date | None = None) -> date:
    if session_date is not None:
        return session_date.date() if isinstance(session_date, datetime) else session_date
    if now is not None:
        return now.date()
    return bars.index[-1].date()


# ------------------------------------------------------------------ daily bars / ATR
def clean_daily(daily: Any, before: date | None = None) -> pd.DataFrame | None:
    """Daily bars with a naive normalised-date index, optionally only dates strictly before ``before``."""
    if isinstance(daily, pd.Series):
        daily = daily.to_frame("close")
    if not isinstance(daily, pd.DataFrame) or daily.empty:
        return None
    df = daily.copy()
    df.columns = [str(c).lower() for c in df.columns]
    if "close" not in df.columns or not isinstance(df.index, pd.DatetimeIndex):
        return None
    idx = df.index
    if idx.tz is not None:
        idx = idx.tz_convert(NY).tz_localize(None)
    df.index = idx.normalize()
    df = df.apply(pd.to_numeric, errors="coerce")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if before is not None:
        df = df[df.index < pd.Timestamp(before)]
    df = df.dropna(subset=["close"])
    return None if df.empty else df


def atr_wilder(daily: pd.DataFrame | None, n: int = 14) -> float | None:
    """Wilder ATR over ``n`` days; needs n+1 rows (the first has no prior close). None if unavailable."""
    if daily is None or not {"high", "low", "close"}.issubset(daily.columns):
        return None
    d = daily.dropna(subset=["high", "low", "close"])
    if len(d) < n + 1:
        return None
    h, l, c = d["high"].to_numpy(float), d["low"].to_numpy(float), d["close"].to_numpy(float)
    pc = c[:-1]
    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - pc), np.abs(l[1:] - pc)])
    atr = float(np.mean(tr[:n]))
    for x in tr[n:]:
        atr = (atr * (n - 1) + float(x)) / n
    return atr if math.isfinite(atr) and atr > 0 else None

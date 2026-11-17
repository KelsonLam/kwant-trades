"""Trading-time clock: how much variance is left until an option's expiry.

Option IVs are annualised on CALENDAR time (ACT/365 to 16:00 NY), but variance accrues mostly
while the market trades. A Monday-expiry option quoted on Sunday night has a 16-hour calendar
life yet a full session of variance ahead of it, so its annualised IV looks "high" purely as a
clock artefact. We therefore compare TOTAL variance to expiry, measuring our forecast in
regular sessions:

* A session is a weekday that is not an NYSE holiday (static 2026-2027 list; early closes are
  treated as full sessions).
* Each session carries one unit of variance, split into an overnight share ``on`` (the
  close-to-open gap, realised at 09:30, regardless of how many non-trading days precede it) and
  an intraday share ``1 - on`` accruing linearly from 09:30 to 16:00.
* ``trading_sessions(asof, expiry, on)`` sums the variance units still ahead between ``asof``
  and 16:00 on the expiry date; the current session counts fractionally.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from kwanttrades.types import NY

OPEN = time(9, 30)
CLOSE = time(16, 0)
SESSION_MINUTES = 390.0
DEFAULT_OVERNIGHT_SHARE = 0.25
OVERNIGHT_BOUNDS = (0.10, 0.50)

US_MARKET_HOLIDAYS = frozenset(
    date.fromisoformat(d)
    for d in (
        # 2026
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
        "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
        # 2027
        "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
        "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    )
)


def is_session(d: date) -> bool:
    return d.weekday() < 5 and d not in US_MARKET_HOLIDAYS


def trading_sessions(asof: datetime, expiry: date, overnight_share: float = DEFAULT_OVERNIGHT_SHARE) -> float:
    """Variance units (sessions) remaining from ``asof`` to the 16:00 close on ``expiry``."""
    on = float(np.clip(overnight_share, 0.0, 1.0))
    t = asof.astimezone(NY) if asof.tzinfo else asof.replace(tzinfo=NY)
    total = 0.0
    d = t.date()
    while d <= expiry:
        if is_session(d):
            open_dt = datetime.combine(d, OPEN, tzinfo=NY)
            close_dt = datetime.combine(d, CLOSE, tzinfo=NY)
            if t < open_dt:
                total += 1.0
            elif t < close_dt:
                total += (1.0 - on) * (close_dt - t).total_seconds() / (SESSION_MINUTES * 60.0)
        d += timedelta(days=1)
    return total


def overnight_share(daily_bars: pd.DataFrame | None, lookback: int = 252) -> float:
    """var(ln(open / prev close)) / var(ln(close / prev close)) over the last ``lookback`` bars,
    clipped to [0.10, 0.50]; 0.25 when it cannot be estimated (< 20 usable bars)."""
    try:
        if daily_bars is None or len(daily_bars) < 21:
            return DEFAULT_OVERNIGHT_SHARE
        df = daily_bars[["open", "close"]].astype(float).tail(lookback + 1)
        prev = df["close"].shift(1)
        r_on = np.log(df["open"] / prev)
        r_cc = np.log(df["close"] / prev)
        ok = np.isfinite(r_on) & np.isfinite(r_cc)
        if ok.sum() < 20:
            return DEFAULT_OVERNIGHT_SHARE
        v_cc = float(np.var(r_cc[ok]))
        if not np.isfinite(v_cc) or v_cc <= 0:
            return DEFAULT_OVERNIGHT_SHARE
        share = float(np.var(r_on[ok])) / v_cc
        if not np.isfinite(share):
            return DEFAULT_OVERNIGHT_SHARE
        return float(np.clip(share, *OVERNIGHT_BOUNDS))
    except Exception:  # noqa: BLE001
        return DEFAULT_OVERNIGHT_SHARE


__all__ = [
    "DEFAULT_OVERNIGHT_SHARE", "OVERNIGHT_BOUNDS", "US_MARKET_HOLIDAYS", "is_session",
    "overnight_share", "trading_sessions",
]

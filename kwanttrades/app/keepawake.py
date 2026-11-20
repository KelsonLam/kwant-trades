"""Keep Windows awake during US market hours only.

Uses the standard SetThreadExecutionState call that media players and download managers use: while the
market is about to open, open, or just closed, the PC is asked not to go to sleep; outside that window
the request is released and the PC sleeps on its normal schedule. No system or power settings are
changed. On non-Windows systems this is a no-op.
"""
from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime, timedelta
from typing import Callable

from kwanttrades.app.session import market_state, next_open
from kwanttrades.types import now_ny

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

PRE_OPEN = timedelta(minutes=20)   # stay awake from 20 minutes before the open
LINGER = timedelta(minutes=15)     # and 15 minutes after the close, for end-of-day settlement


def should_stay_awake(now: datetime, last_open_seen: datetime | None) -> bool:
    """True from PRE_OPEN before the open, through the session, until LINGER after the close."""
    state = market_state(now)
    if state in ("opening_range", "open"):
        return True
    if last_open_seen is not None and now - last_open_seen <= LINGER:
        return True
    return timedelta(0) <= next_open(now) - now <= PRE_OPEN


def _windows_setter(flags: int) -> bool:
    import ctypes

    return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))


class KeepAwake:
    """Background thread that holds the "system required" flag only while should_stay_awake() is true.

    The flag belongs to the thread that set it, so the same thread sets and clears it.
    """

    def __init__(self, clock: Callable[[], datetime] = now_ny, check_seconds: float = 30.0,
                 setter: Callable[[int], bool] | None = None):
        self.clock = clock
        self.check_seconds = check_seconds
        self.setter = setter if setter is not None else (_windows_setter if sys.platform == "win32" else None)
        self.awake = False
        self._last_open_seen: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> bool:
        """One check; returns whether the PC is being kept awake afterwards."""
        now = self.clock()
        if market_state(now) in ("opening_range", "open"):
            self._last_open_seen = now
        want = should_stay_awake(now, self._last_open_seen)
        if want != self.awake and self.setter is not None:
            flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if want else ES_CONTINUOUS
            if self.setter(flags):
                self.awake = want
                log.info("keep-awake %s", "ON (market hours)" if want else "OFF (PC may sleep normally)")
            else:
                log.warning("keep-awake request was refused by Windows")
        return self.awake

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:  # never let this thread kill anything else
                    log.exception("keep-awake check failed")
                self._stop.wait(self.check_seconds)
        finally:
            if self.awake and self.setter is not None:
                self.setter(ES_CONTINUOUS)
                self.awake = False

    def start(self) -> "KeepAwake":
        if self.setter is None:
            log.info("keep-awake is only available on Windows; skipped")
            return self
        self._thread = threading.Thread(target=self._run, name="keep-awake", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

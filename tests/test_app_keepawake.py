"""Keep-awake window: only around US market hours, released otherwise."""
from datetime import datetime

from kwanttrades.app import keepawake
from kwanttrades.app.keepawake import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake, should_stay_awake
from kwanttrades.types import NY


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


# 2026-11-18 is a Wednesday (trading day); 2026-11-21 is a Saturday; 2026-11-26 is Thanksgiving.
def test_window_on_a_trading_day():
    assert not should_stay_awake(at(2026, 11, 18, 8, 0), None)
    assert not should_stay_awake(at(2026, 11, 18, 8, 59), None)
    assert should_stay_awake(at(2026, 11, 18, 9, 0), None)       # 30 minutes before the open
    assert should_stay_awake(at(2026, 11, 18, 9, 5), None)       # the scheduled wake-up time
    assert should_stay_awake(at(2026, 11, 18, 9, 35), None)      # opening range
    assert should_stay_awake(at(2026, 11, 18, 15, 59), None)
    assert not should_stay_awake(at(2026, 11, 18, 18, 0), at(2026, 11, 18, 15, 59))


def test_lingers_briefly_after_the_close():
    last_open = at(2026, 11, 18, 15, 59)
    assert should_stay_awake(at(2026, 11, 18, 16, 10), last_open)
    assert not should_stay_awake(at(2026, 11, 18, 16, 20), last_open)


def test_weekend_and_holiday_stay_asleep():
    assert not should_stay_awake(at(2026, 11, 21, 10, 0), None)
    assert not should_stay_awake(at(2026, 11, 26, 10, 0), None)


def test_tick_sets_and_releases_the_flag():
    calls = []
    now = {"t": at(2026, 11, 18, 8, 50)}
    ka = KeepAwake(clock=lambda: now["t"], setter=lambda f: calls.append(f) or True)
    assert ka.tick() is False and calls == []
    now["t"] = at(2026, 11, 18, 9, 45)
    assert ka.tick() is True
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]
    now["t"] = at(2026, 11, 18, 15, 59)
    assert ka.tick() is True
    now["t"] = at(2026, 11, 18, 16, 5)
    assert ka.tick() is True            # lingering for end-of-day settlement
    now["t"] = at(2026, 11, 18, 16, 30)
    assert ka.tick() is False
    assert calls[-1] == ES_CONTINUOUS   # flag released; normal sleep resumes


def test_refused_request_is_not_reported_as_awake():
    ka = KeepAwake(clock=lambda: at(2026, 11, 18, 10, 0), setter=lambda f: False)
    assert ka.tick() is False


def test_non_windows_is_a_no_op(monkeypatch):
    monkeypatch.setattr(keepawake.sys, "platform", "linux")
    ka = KeepAwake(clock=lambda: at(2026, 11, 18, 10, 0))
    assert ka.setter is None
    assert ka.start() is ka and ka.tick() is False

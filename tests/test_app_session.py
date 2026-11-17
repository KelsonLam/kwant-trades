from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from kwanttrades.app.session import (
    ManualClock,
    effective_time,
    is_trading_day,
    market_state,
    next_open,
    session_close_dt,
)
from kwanttrades.config import Config
from kwanttrades.types import NY

from tests.test_app_fakes import THU, FakeEngine, build, long_call_idea


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


# ------------------------------------------------------------------ calendar
def test_calendar_states_and_holidays():
    assert market_state(at(2026, 11, 12, 9, 0)) == "pre"
    assert market_state(at(2026, 11, 12, 9, 40)) == "opening_range"
    assert market_state(at(2026, 11, 12, 12, 0)) == "open"
    assert market_state(at(2026, 11, 12, 16, 0)) == "closed"
    assert market_state(at(2026, 11, 14, 12, 0)) == "closed"  # Saturday
    assert not is_trading_day(date(2026, 11, 26))  # Thanksgiving
    assert not is_trading_day(date(2026, 7, 3))
    assert market_state(at(2026, 11, 27, 13, 0)) == "closed"  # early close day
    assert market_state(at(2026, 11, 27, 12, 59)) == "open"
    assert session_close_dt(date(2026, 11, 27)).hour == 13
    assert next_open(at(2026, 11, 13, 17, 0)) == at(2026, 11, 16, 9, 30)
    assert effective_time(at(2026, 11, 15, 12, 0)) == at(2026, 11, 13, 16, 0)


# ------------------------------------------------------------------ entries
def test_pass_idea_sends_order_and_opens_position():
    h = build()
    res = h.session.step(THU)
    assert res.market_state == "open"
    assert len(res.orders) == 1 and res.orders[0]["status"] == "filled"
    pos = h.broker.positions()
    assert len(pos) == 1 and pos[0].idea.symbol == "SPY" and pos[0].contracts >= 1
    dec = h.journal.decisions()
    assert dec and dec[0]["decision"] == "PASS"
    assert res.ideas[0].action == "sent"


def test_block_is_journaled_and_nothing_sent():
    eng = FakeEngine(lambda a: [long_call_idea(a.chain, a.symbol, max_loss_mult=60.0)])  # far above the 1% cap
    h = build(engine=eng)
    res = h.session.step(THU)
    assert res.orders == [] and h.broker.positions() == []
    d = h.journal.decisions()
    assert d[0]["decision"] == "BLOCK" and d[0]["reasons"]
    assert not res.ideas[0].decision.passed


def _sticky(a):
    i = long_call_idea(a.chain, a.symbol, stop_loss_pct=1.0)
    i.take_profit_pct = 100.0
    i.strategy = "long_straddle"  # non-directional: never invalidated by a signal flip
    return [i]


def test_one_position_per_symbol():
    h = build(engine=FakeEngine(_sticky))
    h.session.step(THU)
    h.clock.set(at(2026, 11, 12, 10, 35))
    res = h.session.step()
    assert len(h.broker.positions()) == 1
    assert res.orders == []
    assert any("already has an open position" in r for r in res.ideas[0].decision.reasons)


class Boom:
    def __getattr__(self, name):
        raise AssertionError(f"broker touched: {name}")


def test_scan_never_touches_a_broker():
    h = build(mode="scan", broker=False)
    h.session.broker = None
    s = type(h.session)(h.cfg, h.provider, Boom(), h.session.engine, h.journal, mode="scan", clock=h.clock)
    s.universe = ["SPY"]
    res = s.step(THU)
    assert s.broker is None
    assert res.orders == [] and res.ideas and res.ideas[0].decision.passed
    assert h.journal.orders() == []


def test_shadow_journals_marked_orders_and_sends_nothing():
    h = build(mode="shadow", broker=False)
    s = type(h.session)(h.cfg, h.provider, Boom(), h.session.engine, h.journal, mode="shadow", clock=h.clock)
    s.universe = ["SPY"]
    res = s.step(THU)
    assert len(res.orders) == 1 and res.orders[0]["shadow"] is True
    orders = h.journal.orders()
    assert len(orders) == 1 and orders[0].id.startswith("shadow-") and "shadow" in orders[0].reason
    assert h.journal.open_positions() == []
    h.clock.set(at(2026, 11, 12, 10, 40))
    assert s.step().orders == []  # same symbol is not "traded" twice


# ------------------------------------------------------------------ exits
def _open_then(factor, when=at(2026, 11, 12, 10, 45), **kw):
    h = build(**kw)
    h.session.step(THU)
    pos = h.broker.positions()
    assert len(pos) == 1
    h.provider.factor = factor
    h.clock.set(when)
    return h, h.session.step()


def test_take_profit_exit_realises_pnl():
    h, res = _open_then(1.8)
    assert [x["reason"] for x in res.exits] == ["take_profit"]
    p = h.broker.positions(include_closed=True)[0]
    assert not p.is_open and p.realized_pnl > 0 and p.exit_reason == "take_profit"
    assert h.journal.closed_positions()[0].realized_pnl == pytest.approx(p.realized_pnl)


def test_stop_loss_exit_realises_loss():
    h, res = _open_then(0.5)
    assert res.exits[0]["reason"] == "stop_loss"
    p = h.broker.positions(include_closed=True)[0]
    assert p.realized_pnl < 0


def test_time_flatten_at_flatten_time():
    h, res = _open_then(1.0, when=at(2026, 11, 12, 15, 55))
    assert res.exits and res.exits[0]["reason"] == "time_flatten"
    assert not h.broker.positions()


def test_expired_positions_settle_after_close():
    eng = FakeEngine(lambda a: [long_call_idea(a.chain, a.symbol, intraday=False, min_dte=1, max_dte=1)])
    h = build(engine=eng)
    h.session.step(THU)
    pos = h.broker.positions()[0]
    assert pos.idea.expiry == date(2026, 11, 13)
    h.clock.set(at(2026, 11, 13, 16, 0))
    res = h.session.step()
    assert not pos.is_open and pos.exit_reason == "expired" and res.settled


# ------------------------------------------------------------------ kill switch
def test_kill_switch_closes_blocks_and_resets_next_session():
    cfg = Config()
    cfg.risk.daily_loss_limit_pct = 0.001
    eng = FakeEngine(lambda a: [long_call_idea(a.chain, a.symbol, stop_loss_pct=1.0)])
    h = build(engine=eng, cfg=cfg)
    h.session.step(THU)
    h.provider.factor = 0.6
    h.clock.set(at(2026, 11, 12, 11, 0))
    res = h.session.step()
    assert res.kill_switch and "daily loss" in (res.kill_reason or "")
    assert res.exits and res.exits[0]["reason"] == "kill_switch"
    assert not h.broker.positions()
    h.clock.set(at(2026, 11, 12, 11, 30))
    res2 = h.session.step()
    assert res2.kill_switch and res2.orders == []
    assert any("kill switch" in r for i in res2.ideas for r in i.decision.reasons)
    h.provider.factor = 1.0
    h.clock.set(at(2026, 11, 13, 10, 30))
    res3 = h.session.step()
    assert not res3.kill_switch and len(res3.orders) == 1


# ------------------------------------------------------------------ market closed / errors / snapshot
def test_closed_market_scans_but_sends_nothing():
    h = build(when=at(2026, 11, 15, 11, 0))
    res = h.session.step()
    assert res.market_state == "closed" and res.orders == []
    assert res.ideas and all(not i.decision.passed for i in res.ideas)
    assert any("market is closed" in r for r in res.ideas[0].decision.reasons)
    assert any("stale" in n for n in res.notes)
    assert h.journal.decisions()  # still journaled
    assert h.broker.positions() == []


def test_failing_symbol_does_not_break_the_step():
    h = build(symbols=("SPY", "QQQ"), fail=("QQQ",))
    res = h.session.step(THU)
    assert any(e["symbol"] == "QQQ" for e in res.errors)
    assert len(res.orders) == 1 and res.orders[0]["symbol"] == "SPY"
    assert "QQQ" in res.symbols and res.symbols["QQQ"].error


def test_failing_engine_for_one_symbol_is_isolated():
    class Flaky(FakeEngine):
        def analyze(self, symbol, *a, **k):
            if symbol == "QQQ":
                raise ValueError("bad analysis")
            return super().analyze(symbol, *a, **k)

    h = build(symbols=("SPY", "QQQ"), engine=Flaky())
    res = h.session.step(THU)
    assert any(e["symbol"] == "QQQ" and e["stage"] == "analysis" for e in res.errors)
    assert len(res.orders) == 1


def test_snapshot_is_json_serialisable():
    h = build()
    h.session.step(THU)
    snap = h.session.snapshot()
    text = json.dumps(snap, allow_nan=False)
    back = json.loads(text)
    assert back["mode"] == "paper" and back["symbols"][0]["symbol"] == "SPY"
    idea = back["ideas"][0]
    assert idea["payoff"]["prices"] and idea["legs"][0]["occ"].startswith("SPY") and idea["max_profit"] is None
    assert back["positions"]["open"] and back["market"]["state"] == "open"
    assert set(back["symbols"][0]["components"]) == {"orb", "vwap", "gap", "news", "regime"}


def test_run_loop_steps_and_stops():
    h = build()
    clock = h.clock
    clock.set(at(2026, 11, 12, 10, 0))

    def fake_sleep(seconds):
        clock.set(clock.now.replace(minute=clock.now.minute + 5))

    n = h.session.run(max_steps=3, sleep=fake_sleep)
    assert n == 3 and h.session.last is not None


def test_closed_market_reuses_analysis_and_trims_expiries():
    h = build(when=at(2026, 11, 15, 11, 0))
    eng = h.session.engine
    h.session.step()
    n = len(eng.analyzed)
    assert n == 1
    h.clock.set(at(2026, 11, 15, 11, 5))
    res = h.session.step()
    assert len(eng.analyzed) == n  # quotes cannot change while closed: analysis reused, no refetch
    assert res.ideas and res.symbols["SPY"].composite is not None
    # the engine only ever sees the few expiries it can trade
    assert len(h.session.chains["SPY"].expiries()) <= 5 if "SPY" in h.session.chains else True

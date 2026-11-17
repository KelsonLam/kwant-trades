from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from kwanttrades.execution.journal import Journal, idea_from_json, idea_to_json
from kwanttrades.execution.risk import RiskDecision
from kwanttrades.types import NY, AccountState, DirectionalSignal, Order, OrderStatus, Position

from tests.test_execution_factories import NOW, bull_call_spread, bull_put_spread, iron_condor, long_call


def test_idea_roundtrip_including_inf():
    for idea in (long_call(), bull_call_spread(), bull_put_spread(), iron_condor()):
        idea.rationale = ["a", "b"]
        idea.greeks = {"delta": 0.3, "vega": float("nan")}
        s = idea_to_json(idea)
        assert "Infinity" not in s and "NaN" not in s
        back = idea_from_json(s)
        assert math.isnan(back.greeks.pop("vega"))
        idea.greeks.pop("vega")
        assert back == idea
    assert math.isinf(idea_from_json(idea_to_json(long_call())).max_profit)


def test_order_and_position_roundtrip():
    j = Journal(":memory:")
    idea = long_call()
    o = Order(id="o1", idea=idea, contracts=2, limit_price=float("-inf"), action="close", status=OrderStatus.FILLED,
              fill_price=2.5, fees=1.3, submitted=NOW, filled=NOW + timedelta(seconds=3), reason="take_profit", position_id="p1")
    j.save_order(o)
    assert j.get_order("o1") == o
    assert j.orders() == [o]
    p = Position(id="p1", idea=idea, contracts=2, entry_price=2.05, opened=NOW, mark=2.9)
    j.save_position(p)
    assert j.positions() == [p] and j.open_positions() == [p] and j.closed_positions() == []
    p.closed, p.exit_price, p.realized_pnl, p.fees, p.exit_reason = NOW + timedelta(hours=1), 2.95, 177.4, 2.6, "x"
    j.save_position(p)
    assert j.open_positions() == []
    assert j.closed_positions() == [p]


def test_file_db_creates_parents_and_persists(tmp_path):
    path = tmp_path / "a" / "b" / "j.sqlite"
    j = Journal(path)
    j.set_value("cash", 123.456)
    j.close()
    assert Journal(path).get_float("cash") == pytest.approx(123.456)


def test_decisions_signals_equity():
    j = Journal(":memory:")
    idea = bull_call_spread()
    j.record_decision(idea, RiskDecision(False, ["BLOCK: x", "BLOCK: y"]), 0, ts=NOW)
    j.record_decision(idea, "PASS", 3, ts=NOW + timedelta(minutes=1))
    d = j.decisions(limit=10)
    assert [x["decision"] for x in d] == ["PASS", "BLOCK"]
    assert d[1]["reasons"] == ["BLOCK: x", "BLOCK: y"] and d[0]["idea"] == idea and d[0]["contracts"] == 3
    assert len(j.decisions(limit=1)) == 1
    j.record_signal(DirectionalSignal("SPY", "orb", 0.5, 0.7, {"state": "triggered", "entry": 100.5}))
    (s,) = j.signals()
    assert s.source == "orb" and s.detail["state"] == "triggered"
    j.record_equity(AccountState(10_000, 9_000, 8_000, 0.0, 100.0), NOW)
    j.record_equity(AccountState(10_100, 9_000, 8_000, 100.0, 100.0), NOW + timedelta(hours=1))
    curve = j.equity_curve()
    assert list(curve["equity"]) == [10_000, 10_100]
    assert j.day_start_equity(NOW.date()) == 10_000
    assert j.day_start_equity((NOW + timedelta(days=1)).date()) is None
    assert Journal(":memory:").equity_curve().empty


def _filled(j: Journal, oid: str, when: datetime, action: str = "open") -> None:
    j.save_order(Order(id=oid, idea=long_call(), contracts=1, limit_price=2.0, action=action,
                       status=OrderStatus.FILLED, submitted=when, filled=when))


def test_trades_today_counts_only_filled_opens_today():
    j = Journal(":memory:")
    _filled(j, "a", NOW)
    _filled(j, "b", NOW + timedelta(hours=2))
    _filled(j, "c", NOW, action="close")
    _filled(j, "d", NOW - timedelta(days=1))
    j.save_order(Order(id="e", idea=long_call(), contracts=1, limit_price=1.0, status=OrderStatus.REJECTED, submitted=NOW))
    assert j.trades_today(NOW) == 2


def test_day_trades_last_5_sessions():
    j = Journal(":memory:")
    idea = long_call()

    def pos(pid, opened, closed):
        j.save_position(Position(pid, idea, 1, 2.0, opened, closed=closed, exit_price=2.1))

    pos("same_today", NOW, NOW + timedelta(hours=2))
    pos("same_3d_ago", NOW - timedelta(days=3), NOW - timedelta(days=3) + timedelta(hours=1))
    pos("overnight", NOW - timedelta(days=1), NOW)  # not a day trade
    pos("old", NOW - timedelta(days=9), NOW - timedelta(days=9) + timedelta(hours=1))  # outside 5 sessions
    j.save_position(Position("open", idea, 1, 2.0, NOW))
    assert j.day_trades_last_5_sessions(NOW) == 2

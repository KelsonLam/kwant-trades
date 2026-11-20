"""Post-mortems: cause classification, evidence numbers, mark paths and post-exit tracking until expiry."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kwanttrades.learning import Learner, LearningHub
from kwanttrades.learning import postmortem as pm
from kwanttrades.types import NY

from tests.test_app_fakes import THU, FakeEngine, build, long_call_idea


def at(d, hh, mm):
    return datetime(2026, 11, d, hh, mm, tzinfo=NY)


# --------------------------------------------------------------------------- classification (pure)
BASE_F = {"direction_call": 1, "is_credit": False, "vol_edge": 0.0, "cost_per_set": 4.0}
BASE_O = {"realized_pnl": -40.0, "exit_reason": "stop_loss", "move_pct": -0.014, "direction_right": False,
          "gross_pnl": -36.0, "cost_total": 4.0, "gap_sigma": None, "recovered": False, "best_post_pnl": None,
          "realized_vs_model": 1.0}


def cls(strategy="bull_call_spread", symbol="NVDA", f=None, o=None):
    return pm.classify(strategy, symbol, {**BASE_F, **(f or {})}, {**BASE_O, **(o or {})})


def test_direction_wrong_sentence_is_plain():
    cause, text = cls()
    assert cause == "direction_wrong"
    assert text == "Bet NVDA would rise; it fell 1.4% instead."
    cause, text = cls(f={"direction_call": -1}, o={"move_pct": 0.021})
    assert cause == "direction_wrong" and text == "Bet NVDA would fall; it rose 2.1% instead."


def test_too_slow_when_direction_right_but_long_premium_lost():
    cause, text = cls(o={"move_pct": 0.002, "direction_right": True, "exit_reason": "time_flatten"})
    assert cause == "too_slow" and "time decay" in text and "0.2%" in text


def test_vol_overestimated_for_long_vol():
    cause, text = cls("long_straddle", "SPY", f={"direction_call": 0},
                      o={"move_pct": 0.003, "direction_right": None, "realized_vs_model": 0.4})
    assert cause == "vol_over" and "only 0.3%" in text and "40%" in text
    assert pm.CAUSES[cause].startswith("The stock moved less than the model expected")


def test_vol_underestimated_for_short_vol_addition():
    cause, text = cls("iron_condor", "QQQ", f={"direction_call": 0, "is_credit": True},
                      o={"move_pct": 0.025, "direction_right": None, "realized_vs_model": 2.0})
    assert cause == "vol_under" and "range" in text


def test_costs_ate_the_edge():
    cause, text = cls(o={"realized_pnl": -6.0, "gross_pnl": 1.5, "cost_total": 7.5})
    assert cause == "costs" and "spreads and commissions" in text


def test_gap_has_priority_and_stopped_recovered_next():
    cause, text = cls(o={"gap_sigma": 3.1, "recovered": True})
    assert cause == "gap" and "overnight" in text
    cause, text = cls(o={"recovered": True, "best_post_pnl": 25.0})
    assert cause == "stopped_recovered" and "came back" in text
    # recovery after a non-stop exit is not "stopped out, then it recovered"
    assert cls(o={"recovered": True, "exit_reason": "signal_invalidated"})[0] == "direction_wrong"


def test_unknown_when_nothing_was_recorded_and_wins_get_a_summary():
    cause, text = cls(f={"direction_call": 1}, o={"move_pct": None, "direction_right": None, "gross_pnl": None})
    assert cause == "unknown" and "Not enough was recorded" in text
    cause, text = cls(o={"realized_pnl": 55.0, "move_pct": 0.008, "direction_right": True, "exit_reason": "take_profit"})
    assert cause == "win" and text.startswith("Bet NVDA would rise; it rose 0.8%. Took profit")


def test_every_cause_has_a_label():
    for c in ("direction_wrong", "too_slow", "vol_over", "stopped_recovered", "costs", "gap", "vol_under", "unknown", "win"):
        assert pm.CAUSES[c]


# --------------------------------------------------------------------------- evidence helpers
def test_sessions_between_counts_market_minutes_and_overnight():
    assert pm.sessions_between(at(12, 10, 0), at(12, 13, 15)) == pytest.approx(195 / 390)
    two = pm.sessions_between(at(12, 15, 0), at(13, 10, 30))  # 60 min + overnight + 60 min
    assert two == pytest.approx(120 / 390 + pm.OVERNIGHT_SESSIONS)
    assert pm.sessions_between(at(12, 10, 0), at(12, 10, 0)) > 0


def test_overnight_gap_against_the_position():
    marks = [{"ts": at(12, 15, 50), "underlying": 100.0, "phase": "open", "mark": 1.0},
             {"ts": at(13, 9, 31), "underlying": 96.0, "phase": "open", "mark": 0.5}]
    g = pm.overnight_gap(marks, at(12, 10, 0), at(13, 10, 0), 1, "long_call", 0.01)
    assert g == pytest.approx(-100 * __import__("math").log(0.96) / 1.0, rel=1e-6)
    assert pm.overnight_gap(marks, at(12, 10, 0), at(13, 10, 0), -1, "long_put", 0.01) < 0  # helped a bearish bet
    assert pm.overnight_gap(marks, at(12, 10, 0), at(13, 10, 0), 0, "long_straddle", 0.01) == 0.0


# --------------------------------------------------------------------------- session integration
def _hub_session(stop_loss_pct=0.35):
    sent = {"n": 0}

    def ideas(a):
        if sent["n"]:
            return []
        sent["n"] += 1
        return [long_call_idea(a.chain, a.symbol, stop_loss_pct=stop_loss_pct, intraday=False)]

    h = build(engine=FakeEngine(ideas))
    learner = Learner(h.cfg, None, h.journal)
    h.hub = LearningHub(h.cfg, h.journal, learner)
    h.session.learning = h.hub
    return h


def test_entry_features_marks_and_post_mortem_on_close():
    h = _hub_session()
    h.session.step(THU)
    pos = h.broker.positions()[0]
    f = h.journal.entry_features(pos.id)
    for k in ("strategy", "symbol", "dte_entry", "composite_direction", "composite_confidence", "vol_edge",
              "risk_multiplier", "orb_state", "entry_bucket_min", "model_ev", "pop", "score", "entry_iv", "cost_frac"):
        assert k in f, k
    assert f["recorded"] is True and f["entry_bucket_min"] == 45 and f["entry_spot"] > 0
    h.provider.factor = 0.3  # crash the option prices -> stop-loss
    h.clock.set(at(12, 10, 45))
    res = h.session.step()
    assert [e["reason"] for e in res.exits] == ["stop_loss"]
    marks = h.journal.marks(pos.id)  # marked at the start of the step, before the exit
    assert len(marks) == 1 and marks[0]["phase"] == "open" and marks[0]["underlying"] > 0
    row = h.journal.post_mortem(pos.id)
    assert row is not None and row["mode"] == "paper" and row["r"] < 0 and not row["win"]
    o = row["outcome"]
    for k in ("realized_pnl", "max_loss_total", "exit_reason", "hold_minutes", "move_sigma", "direction_right",
              "realized_vs_implied", "cost_total"):
        assert k in o, k
    assert row["cause"] in pm.CAUSES and row["cause_text"]


def test_post_exit_tracking_until_expiry_and_reclassification():
    h = _hub_session()
    h.session.step(THU)
    pos = h.broker.positions()[0]
    expiry = pos.idea.expiry
    h.provider.factor = 0.3
    h.clock.set(at(12, 10, 45))
    h.session.step()
    assert h.journal.post_mortem(pos.id)["cause"] != "stopped_recovered"
    h.provider.factor = 3.0  # it comes back strongly after the stop
    h.clock.set(at(12, 11, 30))
    h.session.step()
    post = [m for m in h.journal.marks(pos.id) if m["phase"] == "post"]
    assert len(post) == 1 and post[0]["mark"] > pos.entry_price
    row = h.journal.post_mortem(pos.id)
    assert row["cause"] == "stopped_recovered" and row["outcome"]["recovered"] is True
    assert row["tracking_done"] is False
    # keeps tracking on later steps until the original expiry, then stops
    assert expiry >= THU.date()
    end = datetime.combine(expiry, datetime.min.time(), tzinfo=NY).replace(hour=16, minute=30)
    h.clock.set(end)
    h.session.step()
    n = len(h.journal.marks(pos.id))
    assert h.journal.post_mortem(pos.id)["tracking_done"] is True
    h.clock.set(end + timedelta(minutes=40))
    h.session.step()
    assert len(h.journal.marks(pos.id)) == n  # no marks after expiry


def test_scan_and_shadow_sessions_record_nothing():
    h = build(mode="shadow", broker=False)
    learner = Learner(h.cfg, None, h.journal)
    h.session.learning = LearningHub(h.cfg, h.journal, learner)
    h.session.step(THU)
    assert h.journal.post_mortems() == []
    assert h.journal._query("SELECT COUNT(*) AS n FROM mark_paths")[0]["n"] == 0
    assert h.journal._query("SELECT COUNT(*) AS n FROM entry_features")[0]["n"] == 0

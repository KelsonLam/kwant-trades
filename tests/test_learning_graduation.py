"""Graduation (paper -> alpaca_paper), the stage switch and its safety fallback, --start-session auto, the learning and
graduation endpoints, and the journal migration on an old database. All offline: Alpaca is always a fake."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from kwanttrades.app.session import ManualClock, is_trading_day
from kwanttrades.config import Config, GraduationConfig
from kwanttrades.data.synthetic import SyntheticProvider
from kwanttrades.execution.journal import _SCHEMA, Journal, idea_to_json
from kwanttrades.execution.paper import PaperBroker
from kwanttrades.learning import StageStore, evaluate, learned_path_for, stage_path_for
from kwanttrades.learning import learner as lm
from kwanttrades.types import NY, Order, OrderStatus
from kwanttrades.web import server as srvmod
from kwanttrades.web.app import WebApp

from tests.test_app_fakes import START, THU, FakeEngine, ScaledProvider
from tests.test_execution_factories import long_call
from tests.test_web_server import NUM, Srv, check


def tdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = tdays(date(2026, 6, 1), 120)
PATTERN = (0.3, 0.3, -0.2, 0.3, -0.2)  # 60% winners, profit factor 2.25


def pms(n, *, r_fn=None, per_day=2, mode="paper", day0=0, ml=100.0):
    out = []
    for i in range(n):
        r = r_fn(i) if r_fn else PATTERN[i % len(PATTERN)]
        d = DAYS[day0 + i // per_day]
        o = datetime(d.year, d.month, d.day, 10, 30 + i % per_day, tzinfo=NY)
        out.append({"position_id": f"{mode}-{day0}-{i}", "mode": mode, "symbol": "SPY", "strategy": "bull_call_spread",
                    "opened": o, "closed": o + timedelta(minutes=45), "realized_pnl": r * ml, "max_loss_total": ml,
                    "r": r, "win": r > 0, "exit_reason": "take_profit" if r > 0 else "stop_loss",
                    "cause": "win" if r > 0 else "direction_wrong",
                    "cause_text": "Bet SPY would rise; it rose 0.5%." if r > 0 else "Bet SPY would rise; it fell 0.6% instead.",
                    "features": {"pop": 0.5, "direction_call": 1}, "outcome": {"direction_right": r > 0},
                    "tracking_done": True})
    return out


G = GraduationConfig()


def crit(res, cid):
    return next(c for c in res["criteria"] if c["id"] == cid)


# --------------------------------------------------------------------------- criteria
def test_all_criteria_pass_on_a_consistent_record():
    res = evaluate(pms(46), G, 10_000.0, None)
    assert res["pass"] is True and res["progress"] == 1.0
    assert {c["id"] for c in res["criteria"]} == {"closed_trades", "trading_days", "profit_factor", "max_drawdown",
                                                 "profitable_weeks", "edge_ci", "out_of_sample"}
    assert crit(res, "profit_factor")["value"] == pytest.approx((28 * 0.3) / (18 * 0.2), rel=1e-3)  # 28 wins, 18 losses
    for c in res["criteria"]:
        assert c["display_value"] and c["display_target"] and 0 <= c["progress"] <= 1


def test_each_criterion_can_fail():
    few = evaluate(pms(30), G, 10_000.0, None)
    assert not crit(few, "closed_trades")["pass"] and not crit(few, "trading_days")["pass"] and not few["pass"]
    assert 0 < crit(few, "closed_trades")["progress"] < 1
    flat = evaluate(pms(46, r_fn=lambda i: 0.2 if i % 2 else -0.2), G, 10_000.0, None)
    assert not crit(flat, "profit_factor")["pass"] and not crit(flat, "edge_ci")["pass"]
    crowded = evaluate(pms(46, per_day=4), G, 10_000.0, None)  # 12 trading days, under 4 weeks
    assert not crit(crowded, "trading_days")["pass"] and not crit(crowded, "profitable_weeks")["pass"]


def test_drawdown_counts_against_starting_equity():
    trades = pms(46)
    trades[20]["realized_pnl"] = -1_200.0  # one awful trade: 12% of $10k
    trades[20]["r"] = -0.8
    res = evaluate(trades, G, 10_000.0, None)
    dd = crit(res, "max_drawdown")
    assert not dd["pass"] and dd["value"] == pytest.approx(0.12 + 0.002, abs=0.005) and not res["pass"]
    assert evaluate(trades, G, 100_000.0, None)["criteria"][3]["pass"] is True  # same loss, bigger account: fine


def test_out_of_sample_requirement():
    trades = pms(60)
    since = trades[39]["closed"]  # a learning change after trade 40: only 20 fresh trades
    res = evaluate(trades, G, 10_000.0, since)
    oos = crit(res, "out_of_sample")
    assert not oos["pass"] and res["out_of_sample"]["trades"] == 20 and not res["pass"]
    assert all(crit(res, c)["pass"] for c in ("closed_trades", "profit_factor", "edge_ci"))
    # enough fresh trades, but they do not pass the same tests
    trades = pms(50, r_fn=lambda i: 0.5 if i % 4 else -0.1) + pms(26, r_fn=lambda i: 0.2 if i % 2 else -0.2, day0=25)
    since = trades[49]["closed"]
    res = evaluate(trades, G, 10_000.0, since)
    assert res["out_of_sample"]["trades"] == 26 and not res["out_of_sample"]["pass"] and not res["pass"]
    assert crit(res, "profit_factor")["pass"]  # the full record still looks fine


def test_stage_store_only_knows_paper_and_alpaca_paper(tmp_path):
    p = tmp_path / "stage.json"
    p.write_text(json.dumps({"stage": "alpaca_live"}), encoding="utf-8")
    st = StageStore(p)
    assert st.stage == "paper"
    with pytest.raises(ValueError):
        st.set("alpaca_live", THU, "x", "y")
    st.set("alpaca_paper", THU, "graduated", "ok", {"a": 1})
    assert json.loads(p.read_text(encoding="utf-8"))["stage"] == "alpaca_paper" and StageStore(p).stage == "alpaca_paper"


def test_memory_journal_never_writes_learning_files():
    j = Journal(":memory:")
    assert learned_path_for(j) is None and stage_path_for(j) is None


# --------------------------------------------------------------------------- web app with fakes
class FakeAlpaca(PaperBroker):
    """Stands in for AlpacaBroker in paper mode: simulated fills, positions always agree."""

    def reconcile(self):
        return []


class RejectingAlpaca(FakeAlpaca):
    def submit(self, idea, contracts, limit_price):
        return Order(id="rej-" + str(time.monotonic_ns()), idea=idea, contracts=contracts, limit_price=limit_price,
                     status=OrderStatus.REJECTED, reason="broker error: test rejection")


def make_app(tmp_path, *, alpaca=FakeAlpaca, symbols=("SPY", "QQQ", "IWM"), poll=1):
    cfg = Config()
    cfg.scan.universe = list(symbols)
    cfg.sentiment.enabled = False
    cfg.poll_seconds = poll
    clock = ManualClock(THU)
    base = SyntheticProvider.simulated(list(symbols), n_days=10, start_date=START, clock=clock)
    journal = Journal(Path(tmp_path) / "kwanttrades.sqlite")
    broker = PaperBroker(cfg.broker, journal, clock=clock)
    app = WebApp(cfg, ScaledProvider(base), provider_name="synthetic", journal=journal,
                 engine_factory=lambda c: FakeEngine(), clock=clock, config_path=Path(tmp_path) / "config.toml",
                 paper_broker=broker, alpaca_factory=lambda: alpaca(cfg.broker, journal, clock=clock))
    return app


def wait_for(pred, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-not-real")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret-not-real")


@pytest.fixture
def nokeys(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)


def seed(app, rows):
    for r in rows:
        app.journal.save_post_mortem(r)


def test_graduation_switches_the_running_paper_session_to_alpaca_paper(tmp_path, keys):
    app = make_app(tmp_path)
    try:
        seed(app, pms(46))
        app.start_session("paper")
        assert wait_for(lambda: app.status()["mode"] == "alpaca_paper" and app.status()["running"]), app.status()
        assert app.stage.stage == "alpaca_paper"
        saved = json.loads((Path(tmp_path) / "stage.json").read_text(encoding="utf-8"))
        assert saved["stage"] == "alpaca_paper" and saved["events"][-1]["kind"] == "graduated"
        assert saved["events"][-1]["evidence"]["closed_trades"]["value"] == 46
        ev = app.journal.events("stage_switch")
        assert ev and "Alpaca PAPER" in ev[0]["message"]
        g = app.graduation()
        assert g["stage"] == "alpaca_paper" and g["last_event"]["kind"] == "graduated"
        assert g["live_readiness"] is not None and "owner's decision" in g["live_readiness"]["note"]
        assert "KWANT_ALLOW_LIVE" in g["live_readiness"]["how"]
        assert app.status()["mode"] != "alpaca_live"
    finally:
        app.close()


def test_ready_but_no_keys_shows_what_is_needed(tmp_path, nokeys):
    app = make_app(tmp_path)
    try:
        seed(app, pms(46))
        app.start_session("paper")
        assert wait_for(lambda: app.stage.data.get("ready") is True)
        assert app.status()["mode"] == "paper" and app.stage.stage == "paper"
        g = app.graduation()
        assert g["ready"] is True and g["needs"] and "ALPACA_API_KEY" in g["needs"][0]
    finally:
        app.close()


def test_not_ready_stays_on_paper(tmp_path, keys):
    app = make_app(tmp_path)
    try:
        seed(app, pms(30))
        app.start_session("paper")
        time.sleep(0.5)
        assert app.status()["mode"] == "paper" and app.stage.stage == "paper" and not app.stage.data["ready"]
    finally:
        app.close()


def test_three_alpaca_rejections_fall_back_to_paper(tmp_path, keys):
    app = make_app(tmp_path, alpaca=RejectingAlpaca)
    try:
        app.stage.set("alpaca_paper", THU, "graduated", "test")
        app.start_session("alpaca_paper")
        assert wait_for(lambda: app.status()["mode"] == "paper" and app.status()["running"]), app.status()
        assert app.stage.stage == "paper"
        st = app.status()
        assert st["stage"] == "paper" and "rejected or failed at Alpaca" in st["stage_alert"]
        assert app.stage.last_event()["kind"] == "fallback"
        assert app.journal.events("stage_fallback")
        assert app.graduation()["alert"] and app._oos_since() is not None  # re-graduation needs fresh trades
    finally:
        app.close()


def test_position_disagreement_falls_back(tmp_path, keys):
    app = make_app(tmp_path)
    calls = []
    app._switch = lambda mode: calls.append(mode)

    class Stub:
        mode, order_failures = "alpaca_paper", 0

        class broker:  # noqa: N801
            @staticmethod
            def reconcile():
                return ["SPY261113C00600000: journal +1, Alpaca +0"]

    s = Stub()
    app._alpaca_safety(s, THU)
    assert calls == [] and app.stage.stage == "paper"  # one strike is not enough
    app.stage.set("alpaca_paper", THU, "graduated", "x")
    app._alpaca_safety(s, THU)
    assert calls == ["paper"] and app.stage.stage == "paper" and "disagree" in app.stage.data["alert"]


# --------------------------------------------------------------------------- --start-session auto
def test_start_session_auto_follows_the_stage_and_never_live(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    assert srvmod.resolve_start_mode(app, "auto") == "paper"
    app.stage.set("alpaca_paper", THU, "graduated", "x")
    assert srvmod.resolve_start_mode(app, "auto") == "paper"  # stage says alpaca, but there are no keys
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    assert srvmod.resolve_start_mode(app, "auto") == "alpaca_paper"
    assert srvmod.resolve_start_mode(app, "alpaca_live") == "paper"
    (Path(tmp_path) / "stage.json").write_text(json.dumps({"stage": "alpaca_live"}), encoding="utf-8")
    app.stage = StageStore(Path(tmp_path) / "stage.json")
    assert srvmod.resolve_start_mode(app, "auto") == "paper"
    assert srvmod.auto_start(app, "auto") == "paper" and app.status()["mode"] == "paper"
    app.close()
    assert "alpaca_live" not in srvmod.START_SESSION_CHOICES and "auto" in srvmod.START_SESSION_CHOICES


def test_cli_web_accepts_auto_and_passes_it_on(monkeypatch):
    from kwanttrades import cli

    seen = {}
    monkeypatch.setattr(srvmod, "main", lambda argv: seen.setdefault("argv", argv) and 0)
    args = cli.build_parser().parse_args(["web", "--start-session", "auto", "--no-browser"])
    cli.cmd_web(args, Config(), None, None)
    i = seen["argv"].index("--start-session")
    assert seen["argv"][i + 1] == "auto"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["web", "--start-session", "alpaca_live"])


# --------------------------------------------------------------------------- endpoints
PM = {"position_id": str, "symbol": str, "strategy": str, "strategy_label": str, "mode": str, "opened": str,
      "closed": str, "pnl": NUM, "r": NUM, "win": bool, "exit_reason": str, "cause": str, "cause_label": str,
      "cause_text": str, "evidence": dict}
SETTING = {"key": str, "label": str, "value": NUM, "default": NUM, "display_value": str, "display_default": str,
           "bounds": [NUM], "changed": bool}
HIST = {"id": str, "kind": str, "ts": str, "label": str, "reason": str, "evidence": dict, "status": str,
        "can_revert": bool, "display_before": str, "display_after": str}
LEARNING = {"frozen": bool, "active": bool, "postmortems": [PM], "settings": [SETTING], "changed": [SETTING],
            "history": [HIST], "summary": {"closed_trades": int, "min_trades": int, "frozen": bool}}
CRIT = {"id": str, "label": str, "plain": str, "display_value": str, "display_target": str, "pass": bool, "progress": NUM}
GRAD = {"stage": str, "ready": bool, "auto_switch": bool, "keys_present": bool, "progress": NUM, "criteria": [CRIT],
        "out_of_sample": {"trades": int, "required": int, "since": (str, None), "criteria": [CRIT], "pass": bool},
        "needs": [str], "last_event": (dict, None), "alert": (str, None), "live_readiness": (dict, None)}


@pytest.fixture
def srv(tmp_path, nokeys):
    app = make_app(tmp_path, symbols=("SPY", "QQQ"))
    seed(app, pms(24))
    s = Srv(app)
    yield s
    s.close()


def test_learning_and_graduation_shapes(srv):
    s, d = srv.get("/api/learning")
    assert s == 200
    check(d, LEARNING)
    assert len(d["postmortems"]) == 24 and d["postmortems"][0]["closed"] > d["postmortems"][-1]["closed"]  # newest first
    assert d["frozen"] is False and d["changed"] == []
    s, g = srv.get("/api/graduation")
    assert s == 200
    check(g, GRAD)
    assert g["stage"] == "paper" and 0 < g["progress"] < 1 and g["live_readiness"] is None


def test_learning_controls_and_guards(srv):
    app = srv.app
    ch = app.learner.apply(lm.proposal("min_vol_edge", 0.10, 0.15, "test", "Needs a bigger edge.", {"n": 24}), THU, 24)
    s, d = srv.get("/api/learning")
    assert d["history"][0]["id"] == ch["id"] and d["history"][0]["can_revert"] is True
    assert [x["key"] for x in d["changed"]] == ["min_vol_edge"]
    assert srv.post("/api/learning/freeze", {"frozen": True})[1]["frozen"] is True
    assert srv.post("/api/learning/freeze", {"frozen": "yes"})[0] == 400
    assert srv.post("/api/learning/freeze", {"frozen": True, "x": 1})[0] == 400
    assert srv.post("/api/learning/revert", {"change_id": "chg-unknown"})[0] == 404
    assert srv.post("/api/learning/revert", {"change_id": "bad id!"})[0] == 400
    s, d = srv.post("/api/learning/revert", {"change_id": ch["id"]})
    assert s == 200 and d["changed"] == [] and d["history"][0]["kind"] == "revert"
    assert srv.post("/api/learning/revert", {"change_id": ch["id"]})[0] == 409
    assert srv.post("/api/learning/reset", {"all": True})[0] == 400
    s, d = srv.post("/api/learning/reset")
    assert s == 200 and d["history"][0]["kind"] == "reset"
    # guards: same-origin POST, method checks
    assert srv.post("/api/learning/freeze", {"frozen": False}, origin=None)[0] == 403
    assert srv.post("/api/learning/reset", origin="http://evil.example")[0] == 403
    assert srv.get("/api/learning/freeze")[0] == 405
    assert srv.post("/api/learning")[0] == 405
    assert srv.post("/api/graduation")[0] == 405


def test_closed_positions_carry_their_cause(tmp_path, nokeys):
    app = make_app(tmp_path, symbols=("SPY", "QQQ"))
    s = Srv(app)
    try:
        s.post("/api/session/start", {"mode": "paper"})
        pos = s.wait("/api/positions", None, lambda d: len(d["open"]) >= 1)
        s.post("/api/session/stop")
        pid = pos["open"][0]["id"]
        assert s.post(f"/api/positions/{pid}/close")[1]["ok"] is True
        closed = [p for p in s.get("/api/positions")[1]["closed"] if p["id"] == pid][0]
        assert closed["postmortem"]["cause_text"] and closed["postmortem"]["cause_label"]
        d = s.get("/api/learning")[1]
        assert [p["position_id"] for p in d["postmortems"]] == [pid]
        assert d["postmortems"][0]["evidence"]["recorded"] is True
    finally:
        s.close()


# --------------------------------------------------------------------------- migration
def test_journal_migration_on_an_old_database(tmp_path):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)  # the original tables only
    con.execute("CREATE TABLE post_mortems (position_id TEXT PRIMARY KEY, symbol TEXT)")  # a half-made older table
    idea = long_call()
    con.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("pos-old", idea_to_json(idea), 1, 2.05, THU.isoformat(), 2.1, (THU + timedelta(hours=1)).isoformat(),
                 2.2, 13.7, 1.3, "take_profit"))
    con.commit()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 0
    con.close()
    j = Journal(path)
    assert j._query("PRAGMA user_version")[0][0] == 2
    cols = {r[1] for r in j._query("PRAGMA table_info(post_mortems)")}
    assert {"cause", "cause_text", "features", "outcome", "tracking_done", "r"} <= cols
    old = j.positions()
    assert len(old) == 1 and old[0].id == "pos-old" and old[0].realized_pnl == 13.7
    j.record_mark("pos-old", THU, 2.1, 600.0)
    j.save_post_mortem({**pms(1)[0], "position_id": "pos-old"})
    assert j.post_mortem("pos-old")["cause"] == "win" and len(j.marks("pos-old")) == 1
    j.close()
    j2 = Journal(path)  # re-opening a migrated journal is a no-op
    assert j2.post_mortem("pos-old") is not None
    j2.close()

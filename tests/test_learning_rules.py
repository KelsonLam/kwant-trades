"""Supervised learning: bounded overrides, evidence rules, one-knob/step/cooldown, auto-revert, owner controls, and
the guarantee that risk caps can never be loosened."""
from __future__ import annotations

import dataclasses
import json
import random
from datetime import date, datetime, timedelta

import pytest

from kwanttrades.app.session import is_trading_day
from kwanttrades.config import Config
from kwanttrades.learning import Learner, LearningError, LearningHub
from kwanttrades.learning import learner as lm
from kwanttrades.strategy.engine import DRIFT_K, VOL_SHRINK, StrategyEngine
from kwanttrades.types import NY

from tests.test_app_fakes import build


# --------------------------------------------------------------------------- synthetic closed trades
def tdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = tdays(date(2026, 6, 1), 200)


def trade(i, r, *, strategy="bull_call_spread", pop=0.5, dcall=1, right=None, bucket=60, per_day=3, mode="paper",
          day0=0, exit_reason="time_flatten"):
    d = DAYS[day0 + i // per_day]
    opened = datetime(d.year, d.month, d.day, 9, 45, tzinfo=NY) + timedelta(minutes=bucket)
    closed = opened + timedelta(minutes=30 + i % per_day)
    return {"position_id": f"p{day0}-{i}", "mode": mode, "symbol": "SPY", "strategy": strategy, "opened": opened,
            "closed": closed, "realized_pnl": r * 100.0, "max_loss_total": 100.0, "r": r, "win": r > 0,
            "exit_reason": exit_reason,
            "features": {"pop": pop, "direction_call": dcall, "entry_bucket_min": bucket, "max_loss": 100.0,
                         "max_profit": 100.0, "intraday": False},
            "outcome": {"direction_right": right, "entry_price": 1.0}}


def noisy(n, mean, sd, seed=1):
    rng = random.Random(seed)
    return [mean + rng.gauss(0, sd) for _ in range(n)]


def after(trades, days=1):
    last = max(t["closed"] for t in trades)
    return last + timedelta(days=days)


def learner(cfg=None, tmp=None):
    return Learner(cfg or Config(), None if tmp is None else tmp / "learned.json")


# --------------------------------------------------------------------------- overrides: applied and bounded
def test_overrides_are_bounded_and_applied_to_engine_without_touching_config(tmp_path):
    cfg = Config()
    p = tmp_path / "learned.json"
    p.write_text(json.dumps({"values": {"drift_k": 5.0, "vol_shrink": -3, "min_direction": 0.31, "min_vol_edge": "x",
                                        "weight:long_call": 2.0, "weight:long_put": 0.0, "max_loss_per_trade_pct": 0.5,
                                        "stop_loss_pct": 0.45, "no_entry_before": "10:30"}}), encoding="utf-8")
    L = Learner(cfg, p)
    v = L.values()
    assert v["drift_k"] == 0.50 and v["vol_shrink"] == 0.30 and v["min_direction"] == 0.31
    assert v["min_vol_edge"] == cfg.scan.min_vol_edge  # garbage ignored
    assert "max_loss_per_trade_pct" not in L.overrides()  # risk caps are not knobs
    assert L.weights() == {"long_call": 1.0, "long_put": 0.0}
    eng = StrategyEngine(cfg)
    L.apply_engine(eng)
    assert eng.drift_k == 0.50 and eng.vol_shrink == 0.30
    assert eng.cfg.scan.min_direction == 0.31 and cfg.scan.min_direction == 0.25  # base config untouched
    assert eng.cfg.scan.universe is cfg.scan.universe and eng.cfg.risk is cfg.risk  # everything else stays live
    # the learned 10:30 start can only make the window later
    assert L.entry_start("09:45") == "10:30" and L.entry_start("11:00") == "11:00"
    # ideas: learned exits and the pause
    from tests.test_execution_factories import long_call

    i = long_call()
    assert L.adjust_idea(i) is None and i.stop_loss_pct == 0.45
    assert "paused" in L.adjust_idea(dataclasses.replace(long_call(), strategy="long_put"))
    assert not (tmp_path / "config.toml").exists()


def test_revert_restores_engine_defaults():
    L = learner()
    eng = StrategyEngine(Config())
    L.listeners.append(lambda: L.apply_engine(eng))
    ch = L.apply(lm.proposal("drift_k", DRIFT_K, DRIFT_K - 0.05, "direction", "x", {}), datetime(2026, 7, 1, 12, tzinfo=NY), 30)
    assert eng.drift_k == pytest.approx(DRIFT_K - 0.05)
    L.revert(ch["id"], datetime(2026, 7, 2, 12, tzinfo=NY))
    assert eng.drift_k == DRIFT_K and eng.vol_shrink == VOL_SHRINK


def test_session_entry_window_and_risk_caps_with_learned_start():
    h = build()
    L = Learner(h.cfg, None, h.journal)
    L.store.data["values"] = {"no_entry_before": "10:15"}
    h.session.learning = LearningHub(h.cfg, h.journal, L)
    r = h.session._risk_cfg_for(datetime(2026, 11, 12, 10, 0, tzinfo=NY))
    assert r.entry_window_start == "10:15"
    base = dataclasses.asdict(h.cfg.risk)
    got = dataclasses.asdict(r)
    assert {k: v for k, v in got.items() if k != "entry_window_start"} == {k: v for k, v in base.items() if k != "entry_window_start"}
    res = h.session.step(datetime(2026, 11, 12, 10, 0, tzinfo=NY))
    assert res.orders == []  # before the learned start: no entries
    assert any("outside entry window" in x for x in res.ideas[0].decision.reasons)


# --------------------------------------------------------------------------- minimum samples
def test_no_change_before_twenty_closed_trades():
    L = learner()
    trades = [trade(i, -0.6 - 0.01 * i, dcall=1, right=False, pop=0.8) for i in range(19)]
    assert L.review(trades, after(trades)) is None
    assert "19 of 20" in L.last_review["note"] and L.overrides() == {}


# --------------------------------------------------------------------------- strategy pause / raise
def test_strategy_pause_after_ten_of_its_own_trades():
    rs = noisy(10, -0.30, 0.1)
    trades = [trade(i, rs[i // 2] if i % 2 else 0.3, strategy="long_call" if i % 2 else "bull_put_spread", dcall=0)
              for i in range(20)]
    L = learner()
    ch = L.review(trades, after(trades))
    assert ch and ch["rule"] == "strategy_pause" and ch["knob"] == "weight:long_call" and ch["after"] == 0.0
    assert ch["evidence"]["n"] == 10 and ch["evidence"]["ci80"][1] < 0
    assert L.strategy_weight("long_call") == 0.0


def test_no_pause_when_the_interval_includes_zero():
    rs = noisy(10, -0.05, 0.5, seed=3)
    trades = [trade(i, rs[i // 2] if i % 2 else 0.3, strategy="long_call" if i % 2 else "bull_put_spread", dcall=0)
              for i in range(20)]
    assert lm.rule_strategy_pause(trades, {}, {}) is None


def test_strategy_raise_only_for_reduced_weights_with_positive_evidence():
    trades = [trade(i, 0.2 + 0.01 * (i % 3), strategy="iron_condor", dcall=0) for i in range(12)]
    assert lm.rule_strategy_raise(trades, {}, {}) is None  # weight 1: nothing to raise
    p = lm.rule_strategy_raise(trades, {"iron_condor": 0.5}, {})
    assert p and p["after"] == 0.75
    assert lm.rule_strategy_raise(trades, {"iron_condor": 0.0}, {}) is None  # paused stays paused


# --------------------------------------------------------------------------- calibration
def test_calibration_overconfident_moves_vol_shrink_toward_market():
    trades = [trade(i, 0.3 if i < 8 else -0.2, pop=0.7, dcall=0) for i in range(25)]
    L = learner()
    ch = L.review(trades, after(trades))
    assert ch and ch["rule"] == "calibration" and ch["knob"] == "vol_shrink"
    assert ch["after"] == pytest.approx(VOL_SHRINK - 0.05)
    e = ch["evidence"]
    assert e["n"] == 25 and e["wins"] == 8 and e["p_value"] < 0.1 and e["bins"]


def test_calibration_well_calibrated_does_nothing_and_floor_falls_back_to_drift():
    ok = [trade(i, 0.3 if i < 17 else -0.2, pop=0.7, dcall=0) for i in range(25)]
    v = learner().values()
    assert lm.rule_calibration(ok, v, lm.knob_table(), {}) is None
    bad = [trade(i, 0.3 if i < 8 else -0.2, pop=0.7, dcall=0) for i in range(25)]
    p = lm.rule_calibration(bad, {**v, "vol_shrink": 0.30}, lm.knob_table(), {})
    assert p["knob"] == "drift_k"


# --------------------------------------------------------------------------- direction hit rate
def test_direction_hit_rate_below_half_lowers_drift_then_raises_min_direction():
    trades = [trade(i, 0.1 if i % 2 else -0.08, dcall=1, right=i < 6, pop=None) for i in range(24)]
    for t in trades:
        t["features"]["pop"] = None
    L = learner()
    ch = L.review(trades, after(trades))
    assert ch and ch["rule"] == "direction" and ch["knob"] == "drift_k" and ch["evidence"]["ci90"][1] < 0.5
    v = {**L.values(), "drift_k": 0.15}
    p = lm.rule_direction(trades, v, lm.knob_table(), {})
    assert p["knob"] == "min_direction" and p["after"] == pytest.approx(0.30)
    fair = [trade(i, 0.1, dcall=1, right=i % 2 == 0) for i in range(24)]
    assert lm.rule_direction(fair, L.values(), lm.knob_table(), {}) is None


# --------------------------------------------------------------------------- one knob, step, cooldown
def test_one_knob_per_review_small_steps_and_cooldown():
    # evidence for several rules at once: a losing strategy, a bad direction hit rate and overconfidence
    trades = [trade(i, -0.3 - 0.01 * (i % 5), strategy="long_call", dcall=1, right=False, pop=0.8) for i in range(24)]
    L = learner()
    ch = L.review(trades, after(trades))
    assert ch is not None and len(L.history()) == 1  # exactly one change
    # same day again: cooldown
    assert L.review(trades, after(trades)) is None and "Cooling down" in L.last_review["note"]
    for h in L.history():
        if h["knob"] != "exits" and not h["knob"].startswith("weight:"):
            kn = L.knob(h["knob"])
            assert abs(h["after"] - h["before"]) <= kn.max_step + 1e-9
    # a proposal that tries to jump is clipped to a quarter of the range
    big = L.apply(lm.proposal("min_vol_edge", 0.10, 0.25, "test", "x", {}), datetime(2027, 1, 4, 12, tzinfo=NY), 99)
    assert big["after"] == pytest.approx(0.10 + (0.25 - 0.05) / 4)


def test_cooldown_counts_trading_days():
    assert lm.trading_days_after(date(2026, 11, 20), date(2026, 11, 27)) == 4  # Thanksgiving excluded
    assert lm.trading_days_after(date(2026, 11, 20), date(2026, 11, 30)) == 5


# --------------------------------------------------------------------------- exits (walk-forward replay)
T0 = datetime(2026, 8, 3, 10, 0, tzinfo=NY)


def path_trade(i, kind):
    """A debit trade (entry 1.00, max loss = max profit = $100). 'dip_recover': -37% (stopped at 35%) then +55%.
    'dip_crash': -37% then -60%."""
    d = DAYS[40 + i]
    o = datetime(d.year, d.month, d.day, 10, 0, tzinfo=NY)
    t = trade(i, -0.37, dcall=0, exit_reason="stop_loss")
    t.update(position_id=f"x{i}", opened=o, closed=o + timedelta(minutes=10))
    marks = [{"ts": o + timedelta(minutes=5), "mark": 0.80, "phase": "open"},
             {"ts": o + timedelta(minutes=10), "mark": 0.63, "phase": "open"}]
    if kind == "dip_recover":
        marks += [{"ts": o + timedelta(minutes=15), "mark": 0.95, "phase": "post"},
                  {"ts": o + timedelta(minutes=20), "mark": 1.55, "phase": "post"}]
    else:
        marks += [{"ts": o + timedelta(minutes=15), "mark": 0.40, "phase": "post"},
                  {"ts": o + timedelta(minutes=20), "mark": 0.30, "phase": "post"}]
    return t, marks


def test_replay_trade_follows_the_path():
    t, m = path_trade(0, "dip_recover")
    assert lm.replay_trade(t, m, 0.35, 0.50) == pytest.approx(-0.37)
    assert lm.replay_trade(t, m, 0.40, 0.50) == pytest.approx(0.55)
    t2 = dict(t, exit_reason="signal_invalidated")  # a fixed exit happens when it really did
    assert lm.replay_trade(t2, m, 0.40, 0.50) == pytest.approx(-0.37)
    assert lm.replay_trade(t, [x for x in m if x["phase"] == "open"], 0.40, 0.50) is None  # no tracking -> unknown


def test_exit_rule_adopts_when_better_in_and_out_of_sample():
    rows = [path_trade(i, "dip_recover") for i in range(30)]
    trades, paths = [r[0] for r in rows], {r[0]["position_id"]: r[1] for r in rows}
    for i, t in enumerate(trades):  # spread over strategies so no single strategy has 10 losers to pause
        t["strategy"] = list(lm.STRATEGY_NAMES)[i % 4]
        t["features"]["pop"] = None  # isolate the exit rule (0 of 30 winners would also trip calibration)
    L = learner()
    p = lm.rule_exits(trades, paths, L.values(), L.knobs)
    assert p and p["after"]["stop_loss_pct"] == pytest.approx(0.40)
    ev = p["evidence"]
    assert ev["n"] == 30 and ev["train_n"] == 20 and ev["test_n"] == 10 and ev["train_gain"] > 0.03 and ev["test_gain"] >= 0
    ch = L.review(trades, after(trades), paths=paths)
    assert ch["knob"] == "exits" and L.values()["stop_loss_pct"] == pytest.approx(0.40)


def test_exit_rule_rejects_when_the_recent_third_gets_worse():
    rows = [path_trade(i, "dip_recover" if i < 20 else "dip_crash") for i in range(30)]
    trades, paths = [r[0] for r in rows], {r[0]["position_id"]: r[1] for r in rows}
    L = learner()
    assert lm.rule_exits(trades, paths, L.values(), L.knobs) is None
    few = rows[:24]
    assert lm.rule_exits([r[0] for r in few], {r[0]["position_id"]: r[1] for r in few}, L.values(), L.knobs) is None


# --------------------------------------------------------------------------- time of day
def test_time_of_day_rule_moves_start_in_fifteen_minute_steps():
    early = noisy(15, -0.30, 0.1, seed=5)
    late = noisy(20, 0.10, 0.1, seed=6)
    trades = [trade(i, early[i], bucket=5, dcall=0) for i in range(15)] + \
             [trade(15 + i, late[i], bucket=90, dcall=0) for i in range(20)]
    for i, t in enumerate(trades):  # spread over strategies so no single strategy has 10 losers to pause
        t["strategy"] = list(lm.STRATEGY_NAMES)[i % 5]
    L = learner()
    ch = L.review(trades, after(trades))
    assert ch and ch["rule"] == "time_of_day" and L.entry_start("09:45") == "10:00"
    assert ch["evidence"]["n_early"] == 15 and ch["evidence"]["p_value"] < 0.1
    v = L.values()
    assert v["no_entry_before"] == 600
    assert lm.rule_time_of_day(trades, v, L.knobs) is None  # the 10:00-10:15 bucket has no losing evidence
    few = trades[:14] + trades[15:]
    assert lm.rule_time_of_day(few, learner().values(), L.knobs) is None


# --------------------------------------------------------------------------- auto-revert
def _changed_learner(change_day: date):
    L = learner()
    ts = datetime(change_day.year, change_day.month, change_day.day, 17, 0, tzinfo=NY)
    ch = L.apply(lm.proposal("min_vol_edge", 0.10, 0.15, "test", "x", {}), ts, 20)
    return L, ch


def test_auto_revert_when_clearly_worse_after_a_change():
    before = [trade(i, 0.2 + 0.02 * (i % 3), dcall=0) for i in range(20)]
    change_day = before[-1]["closed"].date()
    after_ = [trade(i, -0.3 + 0.02 * (i % 3), dcall=0, day0=10) for i in range(15)]
    L, ch = _changed_learner(change_day)
    out = L.review(before + after_, after(after_))
    assert out and out["kind"] == "revert" and out["reverts"] == ch["id"] and out["by"] == "auto"
    assert L.values()["min_vol_edge"] == 0.10 and L.history()[0]["status"] == "reverted"
    assert L.history()[0]["evaluation"]["result"] == "reverted"


def test_change_is_kept_when_not_worse():
    before = [trade(i, 0.2, dcall=0) for i in range(20)]
    after_ = [trade(i, 0.25, dcall=0, day0=10) for i in range(15)]
    L, ch = _changed_learner(before[-1]["closed"].date())
    L.review(before + after_, after(after_))
    assert L.history()[0]["status"] == "active" and L.history()[0]["evaluation"]["result"] == "kept"


# --------------------------------------------------------------------------- owner controls
def test_freeze_revert_reset(tmp_path):
    L = learner(tmp=tmp_path)
    L.set_frozen(True)
    trades = [trade(i, -0.4, strategy="long_call", dcall=1, right=False) for i in range(24)]
    assert L.review(trades, after(trades)) is None and "frozen" in L.last_review["note"]
    L.set_frozen(False)
    now = datetime(2026, 9, 1, 12, tzinfo=NY)
    a = L.apply(lm.proposal("min_vol_edge", 0.10, 0.15, "test", "x", {}), now, 20)
    b = L.apply(lm.proposal("min_vol_edge", 0.15, 0.20, "test", "y", {}), now + timedelta(days=8), 30)
    with pytest.raises(LearningError) as e:
        L.revert(a["id"])
    assert e.value.status == 409  # newer change to the same setting
    L.revert(b["id"])
    assert L.values()["min_vol_edge"] == pytest.approx(0.15)
    with pytest.raises(LearningError):
        L.revert(b["id"])
    with pytest.raises(LearningError) as e:
        L.revert("chg-nope")
    assert e.value.status == 404
    L.reset()
    assert L.overrides() == {} and all(h.get("status") != "active" for h in L.history() if h["kind"] == "change")
    saved = json.loads((tmp_path / "learned.json").read_text(encoding="utf-8"))
    assert saved["values"] == {} and saved["history"][-1]["kind"] == "reset" and saved["rev"] >= 4
    assert Learner(Config(), tmp_path / "learned.json").history()[-1]["kind"] == "reset"  # survives a restart


def test_learning_never_runs_for_live():
    trades = [trade(i, -0.4, strategy="long_call", dcall=1, right=False, mode="alpaca_live") for i in range(30)]
    L = learner()
    assert L.review(trades, after(trades), mode="alpaca_live") is None
    assert L.review(trades, after(trades), mode="paper") is None  # live trades are never evidence
    assert L.overrides() == {}


# --------------------------------------------------------------------------- property: risk caps never loosen
RISK_CAPS = ("max_loss_per_trade_pct", "daily_loss_limit_pct", "max_trades_per_day", "max_open_positions",
             "max_positions_per_symbol", "max_portfolio_risk_pct", "min_reward_risk", "min_credit_to_width",
             "entry_window_end", "flatten_time", "kelly_fraction", "max_contracts_per_trade", "defined_risk_only",
             "pdt_guard")


def test_property_random_reviews_never_loosen_risk_caps_and_stay_in_bounds():
    rng = random.Random(20261119)
    strategies = list(lm.STRATEGY_NAMES)
    cfg = Config()
    base_risk = dataclasses.asdict(cfg.risk)
    h = build()
    for it in range(150):
        L = Learner(cfg, None)
        if rng.random() < 0.5:  # start from random (possibly hostile) stored values
            L.store.data["values"] = {k: rng.uniform(-5, 5) for k in ("drift_k", "vol_shrink", "min_direction",
                                                                       "min_vol_edge", "stop_loss_pct", "take_profit_pct")}
            L.store.data["values"]["no_entry_before"] = rng.choice(["08:00", "09:00", "10:30", "13:00"])
        n = rng.randint(0, 90)
        trades = []
        for i in range(n):
            t = trade(i, rng.uniform(-1, 1), strategy=rng.choice(strategies), pop=rng.uniform(0.05, 0.95),
                      dcall=rng.choice([-1, 0, 1]), right=rng.choice([True, False, None]), bucket=rng.randint(0, 300),
                      per_day=rng.randint(1, 6))
            trades.append(t)
        now = after(trades) if trades else datetime(2026, 7, 1, tzinfo=NY)
        for k in range(4):
            L.review(trades, now + timedelta(days=8 * k), mode=rng.choice(["paper", "alpaca_paper"]))
        v = L.values()
        for key, kn in L.knobs.items():
            assert kn.lo - 1e-9 <= v[key] <= kn.hi + 1e-9, (key, v[key])
        for w in L.weights().values():
            assert 0.0 <= w <= 1.0
        assert set(L.overrides()) <= set(L.knobs) | {f"weight:{s}" for s in strategies}
        h.session.learning = LearningHub(cfg, h.journal, L)
        eff = dataclasses.asdict(h.session._risk_cfg_for(datetime(2026, 11, 12, 11, 0, tzinfo=NY)))
        for cap in RISK_CAPS:
            assert eff[cap] == base_risk[cap], cap
        assert eff["entry_window_start"] >= base_risk["entry_window_start"]
        assert dataclasses.asdict(cfg.risk) == base_risk  # the shared config itself is never modified


# --------------------------------------------------------------------------- news weight
def news_trades(n, hits, **kw):
    ts = [trade(i, 0.1 if i % 2 else -0.1, dcall=0, **kw) for i in range(n)]
    for i, t in enumerate(ts):
        t["outcome"]["news_right"] = i < hits
    return ts


def test_news_rule_turns_news_down_when_headlines_keep_pointing_the_wrong_way():
    L = learner()
    p = lm.rule_news(news_trades(30, 7), L.values(), lm.knob_table(), {})
    assert p["knob"] == "news_weight" and p["before"] == 1.0 and p["after"] == 0.75
    assert "7 of 30" in p["reason"] and p["evidence"]["ci90"][1] < 0.5


def test_news_rule_turns_news_up_when_headlines_keep_being_right():
    L = learner()
    p = lm.rule_news(news_trades(30, 24), L.values(), lm.knob_table(), {})
    assert p["after"] == 1.25 and "more" in p["reason"]


def test_news_rule_needs_enough_trades_and_clear_evidence():
    L = learner()
    assert lm.rule_news(news_trades(19, 2), L.values(), lm.knob_table(), {}) is None   # too few
    assert lm.rule_news(news_trades(30, 15), L.values(), lm.knob_table(), {}) is None  # a coin flip: no change
    no_opinion = news_trades(30, 0)
    for t in no_opinion:
        t["outcome"]["news_right"] = None                                               # news had no real opinion
    assert lm.rule_news(no_opinion, L.values(), lm.knob_table(), {}) is None


def test_news_rule_respects_its_bounds():
    ks = lm.knob_table()
    assert lm.rule_news(news_trades(30, 3), {"news_weight": 0.0}, ks, {}) is None       # already ignoring news
    assert lm.rule_news(news_trades(30, 27), {"news_weight": 1.5}, ks, {}) is None      # already at the ceiling
    assert lm.rule_news(news_trades(30, 3), {"news_weight": 0.1}, ks, {})["after"] == 0.0


def test_news_rule_only_counts_trades_after_its_last_change():
    ts = news_trades(30, 7)
    since = {"news_weight": ts[20]["closed"]}
    assert lm.rule_news(ts, learner().values(), lm.knob_table(), since) is None


def test_learned_news_weight_scales_only_the_news_signal():
    L = learner()
    base = {"orb": 0.40, "vwap": 0.15, "gap": 0.05, "news": 0.25, "regime": 0.15}
    assert L.signal_weights(base) is base                      # default: untouched
    ts = news_trades(30, 7)
    entry = L.review(ts, now=after(ts))
    assert entry is not None and entry["knob"] == "news_weight"
    w = L.signal_weights(base)
    assert w["news"] == pytest.approx(0.1875) and w["orb"] == 0.40 and base["news"] == 0.25
    assert L.fmt("news_weight", 0.75) == "0.75×"
    assert any(r["key"] == "news_weight" and r["changed"] for r in L.settings())


def test_news_weight_never_touches_risk_caps():
    cfg = Config()
    L = learner(cfg)
    ts = news_trades(30, 7)
    L.review(ts, now=after(ts))
    assert dataclasses.asdict(cfg.risk) == dataclasses.asdict(Config().risk)

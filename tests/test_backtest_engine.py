from __future__ import annotations

import math
import statistics
from datetime import date

import pytest

from kwanttrades.backtest import Backtester, compute_metrics, max_drawdown, sharpe_daily
from kwanttrades.config import Config
from tests.test_app_fakes import FakeEngine, long_call_idea

SIM_START = date(2026, 11, 2)


def engine_factory(cfg):
    return FakeEngine(lambda a: [long_call_idea(a.chain, a.symbol)])


def run(days, seed=7, step=60):
    return Backtester(Config(), engine_factory=engine_factory, step_minutes=step).run(
        ["SPY"], days=days, source="simulated", seed=seed, start_date=SIM_START)


# ------------------------------------------------------------------ metrics by hand
def test_metrics_arithmetic_on_a_tiny_trade_list():
    trades = [{"pnl": 100.0, "strategy": "a", "exit_reason": "take_profit"},
              {"pnl": -50.0, "strategy": "a", "exit_reason": "stop_loss"},
              {"pnl": 200.0, "strategy": "b", "exit_reason": "take_profit"},
              {"pnl": -25.0, "strategy": "b", "exit_reason": "time_flatten"},
              {"pnl": 75.0, "strategy": "a", "exit_reason": "time_flatten"}]
    day_end = [10100.0, 10050.0, 10250.0, 10225.0, 10300.0]
    m = compute_metrics(trades, day_end, 10000.0)
    assert m["trades"] == 5 and m["wins"] == 3 and m["losses"] == 2
    assert m["win_rate"] == pytest.approx(0.6)
    assert m["avg_win"] == pytest.approx(125.0)      # (100+200+75)/3
    assert m["avg_loss"] == pytest.approx(-37.5)     # (-50-25)/2
    assert m["profit_factor"] == pytest.approx(5.0)  # 375 / 75
    assert m["expectancy"] == pytest.approx(60.0)    # 300 / 5
    assert m["total_pnl"] == pytest.approx(300.0)
    # peak 10100 -> trough 10050 = 50; peak 10250 -> 10225 = 25
    assert m["max_drawdown"] == pytest.approx(50.0)
    assert m["max_drawdown_pct"] == pytest.approx(50.0 / 10100.0)
    rets = [100 / 10000, -50 / 10100, 200 / 10050, -25 / 10250, 75 / 10225]
    expect = statistics.mean(rets) / statistics.stdev(rets) * math.sqrt(252)
    assert m["sharpe"] == pytest.approx(expect)
    by = {b["strategy"]: b for b in m["by_strategy"]}
    assert by["a"]["trades"] == 3 and by["a"]["pnl"] == pytest.approx(125.0) and by["b"]["pnl"] == pytest.approx(175.0)
    ex = {b["reason"]: b for b in m["by_exit"]}
    assert ex["take_profit"]["pnl"] == pytest.approx(300.0) and ex["stop_loss"]["trades"] == 1


def test_metrics_edge_cases():
    m = compute_metrics([], [], 10000.0)
    assert m["trades"] == 0 and m["win_rate"] == 0 and m["sharpe"] == 0 and m["max_drawdown"] == 0
    only_wins = compute_metrics([{"pnl": 10.0}, {"pnl": 5.0}], [10015.0], 10000.0)
    assert math.isinf(only_wins["profit_factor"])
    assert max_drawdown([100, 120, 90, 130, 100]) == (30.0, pytest.approx(30 / 120))
    assert sharpe_daily([10100.0], 10000.0) == 0.0


# ------------------------------------------------------------------ backtester
def test_backtest_is_deterministic_for_a_seed():
    a, b = run(3), run(3)
    key = lambda r: [(t["symbol"], t["strategy"], t["opened"], t["closed"], round(t["pnl"], 6), t["exit_reason"]) for t in r.trades]
    assert key(a) == key(b) and a.metrics["total_pnl"] == b.metrics["total_pnl"]
    assert [e for _, e in a.equity] == [e for _, e in b.equity]
    assert a.trades, "the fake engine should have produced trades"
    c = run(3, seed=99)
    assert key(c) != key(a)


def test_no_look_ahead_earlier_days_unchanged_when_later_data_appended():
    short, long_ = run(3), run(6)
    cutoff = short.sessions[-1]

    def early(res):
        return [(t["opened"], t["closed"], round(t["pnl"], 6), t["exit_reason"]) for t in res.trades
                if date.fromisoformat(t["closed"][:10]) <= cutoff and t["exit_reason"] != "end_of_backtest"]

    assert early(short) and early(short) == early(long_)
    eq_s = {t: e for t, e in short.equity if t.date() <= cutoff and t.hour < 16}
    eq_l = {t: e for t, e in long_.equity if t.date() <= cutoff and t.hour < 16}
    assert eq_s and all(eq_l[t] == pytest.approx(e) for t, e in eq_s.items() if t in eq_l)


def test_caveats_always_state_synthetic_option_prices():
    r = run(2)
    text = " ".join(r.caveats).lower()
    assert "option prices are synthetic" in text
    assert "simulated prices - says nothing about real profitability" in text
    d = r.to_dict()
    assert d["caveats"] and d["summary"]["trades"] == r.metrics["trades"]
    import json
    json.dumps(d, allow_nan=False)
    assert r.sessions[0] == SIM_START


def test_real_bars_source_uses_one_minute_bars_and_caps_days():
    import pandas as pd
    from tests.test_app_fakes import START
    from kwanttrades.app.session import ManualClock
    from kwanttrades.data.synthetic import SyntheticProvider

    sim = SyntheticProvider.simulated(["SPY"], n_days=10, start_date=START, clock=ManualClock())

    class Fetch:  # offline stand-in for YahooProvider: "real" bars and daily history
        def intraday_bars(self, s, interval, days):
            assert interval == "1m"
            return sim._m1[s].copy()

        def daily_bars(self, s, years=2.0):
            if s == "^VIX3M":
                raise RuntimeError("no data")
            d = sim._daily["SPY"].copy() if s.startswith("^") else sim._daily[s].copy()
            d.index = pd.DatetimeIndex([pd.Timestamp(x) for x in d.index])
            if s.startswith("^"):
                d[["open", "high", "low", "close"]] = 18.0
            return d

    r = Backtester(Config(), engine_factory=engine_factory, step_minutes=60).run(
        ["SPY"], days=20, source="real_bars", fetch_provider=Fetch())
    assert len(r.sessions) == 7
    text = " ".join(r.caveats)
    assert "only the last 7" in text and "1-minute" in text and "Option prices are synthetic" in text


def test_fast_mode_produces_identical_trades_to_the_unoptimised_path():
    """Caches and idle-symbol skipping must not change results (engine ideas depend only on the signal)."""
    class Gated(FakeEngine):
        def __init__(self):
            super().__init__(lambda a: [long_call_idea(a.chain, a.symbol)] if abs(a.direction.direction) >= 0.25 else [])

        def grade(self, signal):
            d = signal.direction
            return "neutral" if abs(d) < 0.25 else ("bullish" if d > 0 else "bearish")

    engines = {}

    def make(fast):
        def factory(cfg):
            engines[fast] = Gated()
            return engines[fast]
        return Backtester(Config(), engine_factory=factory, step_minutes=30, fast=fast).run(
            ["SPY"], days=4, source="simulated", seed=11, start_date=SIM_START)

    slow, fast = make(False), make(True)
    key = lambda r: [(t["opened"], t["closed"], round(t["pnl"], 6), t["exit_reason"]) for t in r.trades]
    assert key(slow) == key(fast)
    assert [round(e, 6) for _, e in slow.equity] == [round(e, 6) for _, e in fast.equity]
    assert len(engines[True].analyzed) < len(engines[False].analyzed)  # the skipping really happened

"""Offline tests for the dashboard server: every endpoint's status and field shapes, jobs, security guards."""
from __future__ import annotations

import http.client
import json
import math
import threading
import time
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from kwanttrades.app.session import ManualClock
from kwanttrades.config import Config
from kwanttrades.data.synthetic import SyntheticProvider
from kwanttrades.execution.journal import Journal
from kwanttrades.execution.paper import PaperBroker
from kwanttrades.types import NY, NewsItem
from kwanttrades.web import server as srvmod
from kwanttrades.web.app import WebApp
from kwanttrades.web.serialize import idea_breakevens, idea_payoff, jsonsafe, position_extras
from kwanttrades.web.server import create_server, resolve_static
from tests.test_app_fakes import START, THU, FakeEngine, ScaledProvider, long_call_idea

SUN = datetime(2026, 11, 15, 12, 0, tzinfo=NY)

# --------------------------------------------------------------------------- shape checker
NUM = (int, float)
ISO = str
SYM = str


def check(obj, spec, path="$"):
    """spec: type | tuple of types/None (alternatives) | dict (required keys, extras allowed) | [spec] (list of)."""
    if isinstance(spec, dict):
        assert isinstance(obj, dict), f"{path}: expected object, got {type(obj).__name__}"
        for k, sub in spec.items():
            assert k in obj, f"{path}: missing key {k!r} (have {sorted(obj)})"
            check(obj[k], sub, f"{path}.{k}")
    elif isinstance(spec, list):
        assert isinstance(obj, list), f"{path}: expected list, got {type(obj).__name__}"
        for i, v in enumerate(obj):
            check(v, spec[0], f"{path}[{i}]")
    elif isinstance(spec, tuple):
        if obj is None:
            assert None in spec, f"{path}: null not allowed"
            return
        errs = []
        for alt in (t for t in spec if t is not None):
            try:
                check(obj, alt, path)
                return
            except AssertionError as exc:
                errs.append(str(exc))
        raise AssertionError(f"{path}: no alternative matched: {errs}")
    else:
        assert isinstance(obj, spec) and not (isinstance(obj, bool) and spec is not bool), \
            f"{path}: expected {spec.__name__}, got {type(obj).__name__} ({obj!r})"


N = (int, float, None)
LEG = {"occ": str, "side": str, "ratio": int, "right": str, "strike": NUM, "expiry": str, "bid": NUM, "ask": NUM,
       "mid": NUM, "flag": (str, None)}
IDEA = {"id": str, "symbol": str, "strategy": str, "strategy_label": str, "legs": [LEG], "net_price": NUM,
        "entry_limit": NUM, "max_loss": NUM, "max_profit": N, "pop": NUM, "expected_value": NUM, "score": NUM,
        "contracts": int, "rationale": [str], "greeks": {"delta": N, "gamma": N, "vega": N, "theta": N},
        "breakevens": [NUM], "payoff": {"prices": [NUM], "pnl": [NUM]}, "spot": N, "intraday": bool,
        "decision": (dict, None)}
POS = {"id": str, "symbol": str, "strategy": str, "strategy_label": str, "contracts": int, "entry_price": NUM,
       "mark": N, "unrealized_pnl": NUM, "realized_pnl": NUM, "max_loss": NUM, "max_profit": N, "opened": str,
       "closed": (str, None), "exit_reason": str, "legs": [LEG], "expiry": str, "progress_to_target": NUM,
       "progress_to_stop": NUM, "target_value": N, "stop_value": N}
STATUS = {"mode": str, "running": bool, "now": str,
          "market": {"state": str, "next_open": str, "next_close": str, "opening_range_ends": (str, None)},
          "account": {"equity": NUM, "cash": NUM, "buying_power": NUM, "day_pnl": NUM, "open_risk": NUM},
          "kill_switch": bool, "provider": str, "data_delay_note": str, "universe": [str], "last_scan": (str, None),
          "trades_today": int, "max_trades_per_day": int, "alpaca_keys_present": bool, "sentiment_engine": str,
          "limits": {"max_portfolio_risk": NUM, "daily_loss_limit": NUM, "max_loss_per_trade": NUM}}
COMP = ({"direction": NUM, "confidence": NUM, "detail": dict}, None)
SIGNAL = {"symbol": str, "spot": N, "change_pct": N,
          "composite": ({"direction": NUM, "confidence": NUM, "agreement": NUM}, None),
          "components": {"orb": COMP, "vwap": COMP, "gap": COMP, "news": COMP, "regime": COMP},
          "orb": (dict, None), "vol": ({"atm_iv": N, "forecast_rv": N, "vol_edge": N, "components": dict}, None),
          "sparkline": [NUM]}
SURFACE = {"symbol": str, "spot": NUM, "asof": str,
           "slices": [{"expiry": str, "dte": int, "forward": NUM, "method": str, "rmse": N,
                       "points": [{"strike": NUM, "right": str, "market_iv": NUM, "bid_iv": N, "ask_iv": N, "fit_iv": N,
                                   "flag": (str, None), "zscore": N}],
                       "fit_curve": {"strikes": [NUM], "iv": [NUM]}}],
           "mispricings": [{"occ": str, "model": str, "edge": NUM, "edge_after_costs": NUM, "zscore": NUM,
                            "market_iv": N, "fair_iv": N, "notes": str}]}
SCAN_STATUS = {"job_id": (str, None), "state": str, "progress": [{"symbol": str, "state": str, "message": str}],
               "started": (str, None), "finished": (str, None)}
BT = {"state": str, "progress": NUM, "summary": ({"trades": int, "win_rate": NUM, "profit_factor": N, "expectancy": NUM,
                                                  "sharpe": N, "max_drawdown": NUM, "total_pnl": NUM}, None),
      "equity": [{"t": str, "equity": NUM}], "by_strategy": [{"strategy": str, "trades": int, "pnl": NUM, "win_rate": NUM}],
      "by_exit": [{"reason": str, "trades": int, "pnl": NUM}],
      "trades": [{"symbol": str, "strategy": str, "opened": str, "closed": str, "pnl": NUM, "exit_reason": str}],
      "caveats": [str], "request": (dict, None)}
CONFIG = {"risk": {"max_trades_per_day": int, "max_loss_per_trade_pct": NUM, "defined_risk_only": bool},
          "scan": {"universe": [str], "directional_dte": [int], "vol_dte": [int], "min_expected_value": NUM},
          "signals": {"opening_range_minutes": int}, "broker": {"mode": str, "starting_equity": NUM}}


# --------------------------------------------------------------------------- harness
class Srv:
    def __init__(self, app: WebApp):
        self.app = app
        self.server = create_server(app, 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def raw(self, method, path, body=None, headers=None, origin="auto"):
        h = {}
        if origin == "auto":
            h["Origin"] = f"http://127.0.0.1:{self.port}"
        elif origin:
            h["Origin"] = origin
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=data, headers=h)
            r = conn.getresponse()
            return r.status, r.read(), dict(r.getheaders())
        finally:
            conn.close()

    def req(self, method, path, body=None, **kw):
        status, data, hdrs = self.raw(method, path, body, **kw)
        try:
            return status, json.loads(data)
        except ValueError:
            return status, data

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.req("POST", path, {} if body is None else body, **kw)

    def wait(self, path, key, pred, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            s, d = self.get(path)
            assert s == 200
            if pred(d[key] if key else d):
                return d
            time.sleep(0.1)
        raise AssertionError(f"timeout waiting on {path}: {d}")

    def close(self):
        self.app.close()
        self.server.shutdown()
        self.server.server_close()


def make_app(tmp_path, *, fake=False, symbols=("SPY", "QQQ"), when=THU, fail=(), news=None, provider=None):
    cfg = Config()
    cfg.scan.universe = list(symbols)
    cfg.sentiment.enabled = False  # lexicon scoring only: no network, no key use
    clock = ManualClock(when)
    base = SyntheticProvider.simulated(list(symbols), n_days=10, start_date=START, clock=clock)
    prov = provider(base) if provider else ScaledProvider(base, 1.0, fail)
    journal = Journal(":memory:")
    broker = PaperBroker(cfg.broker, journal, clock=clock)
    factory = (lambda c: FakeEngine()) if fake else None
    return WebApp(cfg, prov, provider_name="synthetic", journal=journal, engine_factory=factory, news=news, clock=clock,
                  config_path=Path(tmp_path) / "config.toml", paper_broker=broker)


class FakeNews:
    def headlines(self, symbol, lookback_hours=24):
        t = THU - timedelta(hours=2)
        return [NewsItem(symbol, f"{symbol} beats earnings estimates and raises guidance", t, "Wire", "https://example.com/a"),
                NewsItem(symbol, f"{symbol} faces lawsuit over product recall", t - timedelta(hours=1), "Daily", "https://example.com/b")]


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    s = Srv(make_app(tmp_path_factory.mktemp("real"), symbols=("SPY", "QQQ", "IWM"), news=FakeNews()))
    yield s
    s.close()


@pytest.fixture(scope="module")
def fake(tmp_path_factory):
    s = Srv(make_app(tmp_path_factory.mktemp("fake"), fake=True))
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _no_alpaca(monkeypatch):
    """No test may ever construct a real Alpaca broker or see real keys."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    def boom(*a, **k):
        raise AssertionError("AlpacaBroker must not be constructed in tests")

    monkeypatch.setattr("kwanttrades.execution.alpaca.AlpacaBroker", boom)


def check_ideas(ideas):
    for i in ideas:
        n = len(i["payoff"]["prices"])
        assert 55 <= n <= 70 and n == len(i["payoff"]["pnl"])
        assert i["payoff"]["prices"] == sorted(i["payoff"]["prices"])
        assert i["decision"] and isinstance(i["decision"]["passed"], bool) and isinstance(i["decision"]["reasons"], list)
        assert i["strategy_label"] and i["strategy_label"] != i["strategy"]
        strikes = [l["strike"] for l in i["legs"]]
        assert i["payoff"]["prices"][0] < min(strikes) and i["payoff"]["prices"][-1] > max(strikes)
        assert all(be > 0 for be in i["breakevens"])
        assert min(i["payoff"]["pnl"]) >= -1.05 * i["max_loss"] - 1  # (fake ideas price max_loss at mid, entry at ask)
    assert [i["score"] for i in ideas] == sorted((i["score"] for i in ideas), reverse=True)


# --------------------------------------------------------------------------- idle endpoints (before any scan)
def test_idle_endpoints_have_contract_shapes(real):
    s, st = real.get("/api/status")
    assert s == 200
    check(st, STATUS)
    assert st["mode"] == "idle" and st["running"] is False and st["provider"] == "synthetic"
    assert st["limits"] == {"max_portfolio_risk": 1500.0, "daily_loss_limit": 300.0, "max_loss_per_trade": 100.0}
    assert st["market"]["state"] == "open" and st["alpaca_keys_present"] is False and st["sentiment_engine"] == "lexicon"
    s, d = real.get("/api/scan/status")
    assert s == 200
    check(d, SCAN_STATUS)
    assert d["state"] == "idle" and d["job_id"] is None
    assert real.get("/api/ideas") == (200, {"asof": None, "ideas": []})
    s, d = real.get("/api/signals")
    assert s == 200 and d["symbols"] == [] and d["asof"] is None
    s, d = real.get("/api/positions")
    assert s == 200
    check(d, {"open": [POS], "closed": [POS]})
    s, d = real.get("/api/equity")
    assert s == 200
    check(d, {"points": [{"t": str, "equity": NUM}], "starting_equity": NUM})
    s, d = real.get("/api/decisions?limit=5")
    assert s == 200 and d == {"decisions": []}
    s, d = real.get("/api/backtest/result")
    assert s == 200
    check(d, BT)
    assert d["state"] == "idle" and d["summary"] is None


def test_config_get_shape_and_no_secrets(real):
    s, d = real.get("/api/config")
    assert s == 200
    check(d, CONFIG)
    assert set(d) == {"risk", "scan", "signals", "broker"}
    assert set(d["broker"]) == {"mode", "starting_equity"}
    assert d["scan"]["universe"] == ["SPY", "QQQ", "IWM"]


def test_status_reports_keys_only_as_boolean(real, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "FAKEKEY123456")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "FAKESECRET987654")
    status, raw, _ = real.raw("GET", "/api/status")
    assert status == 200
    text = raw.decode()
    assert "FAKEKEY" not in text and "FAKESECRET" not in text
    assert json.loads(text)["alpaca_keys_present"] is True
    assert json.loads(text)["sentiment_engine"] == "lexicon"


# --------------------------------------------------------------------------- scan -> ideas / signals / surface / news
def test_scan_runs_and_feeds_ideas_signals_surface(real):
    s, d = real.post("/api/scan", {"symbols": None})
    assert s == 200 and isinstance(d["job_id"], str)
    done = real.wait("/api/scan/status", None, lambda d: d["state"] in ("done", "error"))
    check(done, SCAN_STATUS)
    assert done["state"] == "done" and done["job_id"] == d["job_id"] and done["finished"]
    assert {p["symbol"] for p in done["progress"]} == {"SPY", "QQQ", "IWM"}
    assert all(p["state"] == "done" for p in done["progress"])

    s, ideas = real.get("/api/ideas")
    assert s == 200 and ideas["asof"]
    check(ideas, {"asof": str, "ideas": [IDEA]})
    check_ideas(ideas["ideas"])  # whether the real engine finds an idea in this simulated market is not asserted

    s, sg = real.get("/api/signals")
    assert s == 200
    check(sg, {"asof": str, "regime": {"direction": NUM, "confidence": NUM, "risk_multiplier": NUM, "detail": dict},
               "symbols": [SIGNAL]})
    assert {x["symbol"] for x in sg["symbols"]} == {"SPY", "QQQ", "IWM"}
    assert all(len(x["sparkline"]) <= 80 for x in sg["symbols"])

    s, sf = real.get("/api/surface?symbol=SPY")
    assert s == 200
    check(sf, SURFACE)
    assert sf["slices"] and sf["slices"][0]["points"] and sf["slices"][0]["fit_curve"]["strikes"]
    assert all(sl["method"] in ("svi", "quadratic", "flat") for sl in sf["slices"])

    s, st = real.get("/api/status")
    check(st, STATUS)
    assert st["last_scan"] == ideas["asof"]

    s, dec = real.get("/api/decisions?limit=100")
    assert s == 200
    check(dec, {"decisions": [{"t": str, "symbol": str, "strategy": str, "passed": bool, "reasons": [str],
                               "contracts": int, "score": NUM, "expected_value": NUM, "idea_id": str}]})
    assert {i["id"] for i in ideas["ideas"]} <= {x["idea_id"] for x in dec["decisions"]}  # every idea was journaled


def test_surface_on_demand_for_unscanned_symbol_and_errors(tmp_path):
    srv = Srv(make_app(tmp_path))
    try:
        s, sf = srv.get("/api/surface?symbol=qqq")  # never scanned: analysed on demand
        assert s == 200 and sf["symbol"] == "QQQ"
        check(sf, SURFACE)
        assert srv.get("/api/surface")[0] == 400
        assert srv.get("/api/surface?symbol=%3Cscript%3E")[0] == 400
        s, d = srv.get("/api/surface?symbol=ZZZZ")  # provider has no such symbol: a clean 502, not a crash
        assert s == 502 and "error" in d
    finally:
        srv.close()


def test_news_endpoint(real):
    s, d = real.get("/api/news?symbol=SPY")
    assert s == 200
    item = {"symbol": str, "headline": str, "source": str, "url": str, "published": str,
            "sentiment": N, "relevance": N, "materiality": N}
    check(d, {"items": [item]})
    assert d["items"] and all(i["symbol"] == "SPY" for i in d["items"])
    assert any(i["url"].startswith("https://example.com/") for i in d["items"])
    assert any(i["sentiment"] > 0 for i in d["items"]) and any(i["sentiment"] < 0 for i in d["items"])
    got = real.wait("/api/news?symbol=ALL", "items", lambda items: {i["symbol"] for i in items} >= {"SPY", "QQQ"}, timeout=15)
    check(got, {"items": [item]})
    assert real.get("/api/news?symbol=bad%20sym")[0] == 400


def test_news_empty_without_provider(tmp_path):
    srv = Srv(make_app(tmp_path))
    try:
        assert srv.get("/api/news?symbol=SPY") == (200, {"items": []})
        assert srv.get("/api/news") == (200, {"items": []})
    finally:
        srv.close()


# --------------------------------------------------------------------------- sessions, positions, close, flatten
def test_session_start_stop_and_busy(real):
    s, st = real.post("/api/session/start", {"mode": "shadow"})
    assert s == 200
    check(st, STATUS)
    assert st["running"] is True and st["mode"] == "shadow"
    assert real.post("/api/session/start", {"mode": "shadow"})[0] == 409
    s, st = real.post("/api/session/stop")
    assert s == 200 and st["running"] is False and st["mode"] == "idle"
    assert real.post("/api/session/stop")[0] == 200  # idempotent
    assert real.post("/api/session/start", {"mode": "nonsense"})[0] == 400
    assert real.post("/api/session/start", {})[0] == 400
    assert real.post("/api/session/start", {"mode": "paper", "extra": 1})[0] == 400


def test_alpaca_live_is_refused_and_alpaca_paper_needs_keys(real):
    s, d = real.post("/api/session/start", {"mode": "alpaca_live"})
    assert s == 400 and "cannot be started" in d["error"] and "web dashboard" in d["error"]
    s, d = real.post("/api/session/start", {"mode": "alpaca_paper"})  # no keys in the environment
    assert s == 400 and "Alpaca" in d["error"]
    assert real.get("/api/status")[1]["running"] is False


def test_paper_session_positions_close_and_flatten(fake):
    s, st = fake.post("/api/session/start", {"mode": "paper"})
    assert s == 200 and st["mode"] == "paper" and st["running"] is True
    pos = fake.wait("/api/positions", None, lambda d: len(d["open"]) >= 1)
    check(pos, {"open": [POS], "closed": [POS]})
    p = pos["open"][0]
    assert p["target_value"] is not None and p["stop_value"] is not None
    assert p["max_profit"] is None  # long call: unbounded -> null
    assert p["stop_value"] < p["entry_price"] < p["target_value"]
    s, st = fake.post("/api/session/stop")
    assert s == 200 and st["running"] is False
    s, st = fake.get("/api/status")
    assert st["account"]["open_risk"] > 0 and st["trades_today"] >= 1
    s, ideas = fake.get("/api/ideas")
    assert s == 200 and ideas["ideas"] and ideas["ideas"][0]["max_profit"] is None  # inf -> null, valid JSON
    check(ideas, {"asof": str, "ideas": [IDEA]})
    check_ideas(ideas["ideas"])
    assert ideas["ideas"][0]["legs"][0]["flag"] in (None, "cheap", "rich")
    eq = fake.get("/api/equity")[1]
    assert eq["points"], "a paper session journals equity"
    assert fake.get("/api/decisions?limit=10")[1]["decisions"][0]["passed"] is True

    # close one position
    assert fake.post("/api/positions/does-not-exist/close")[0] == 404
    assert fake.post("/api/positions/bad%20id%21/close")[0] == 400
    s, r = fake.post(f"/api/positions/{p['id']}/close")
    assert s == 200
    check(r, {"ok": bool, "message": str})
    assert r["ok"] is True, r
    pos = fake.get("/api/positions")[1]
    assert not [x for x in pos["open"] if x["id"] == p["id"]]
    assert [x for x in pos["closed"] if x["id"] == p["id"]]
    assert fake.post(f"/api/positions/{p['id']}/close")[0] == 404  # already closed

    # open another, then flatten everything
    fake.post("/api/session/start", {"mode": "paper"})
    fake.wait("/api/positions", None, lambda d: len(d["open"]) >= 1)
    fake.post("/api/session/stop")
    s, r = fake.post("/api/session/flatten")
    assert s == 200
    check(r, {"ok": bool, "message": str})
    assert r["ok"] is True, r
    assert fake.get("/api/positions")[1]["open"] == []
    s, r = fake.post("/api/session/flatten")
    assert s == 200 and r["ok"] is True and "no open" in r["message"].lower()


def test_closed_market_note_and_scan_still_works(tmp_path):
    srv = Srv(make_app(tmp_path, fake=True, when=SUN))
    try:
        s, st = srv.get("/api/status")
        assert st["market"]["state"] == "closed"
        assert "last close" in st["data_delay_note"] and "review" in st["data_delay_note"]
        assert srv.post("/api/scan", {"symbols": ["SPY"]})[0] == 200
        done = srv.wait("/api/scan/status", None, lambda d: d["state"] in ("done", "error"))
        assert done["state"] == "done"
        ideas = srv.get("/api/ideas")[1]
        assert ideas["ideas"] and all(i["decision"]["passed"] is False for i in ideas["ideas"])
        assert any("market is closed" in r.lower() for r in ideas["ideas"][0]["decision"]["reasons"])
    finally:
        srv.close()


# --------------------------------------------------------------------------- jobs: failures and busy states
def test_provider_errors_are_per_symbol_not_500(tmp_path):
    srv = Srv(make_app(tmp_path, fake=True, fail=("QQQ",)))
    try:
        assert srv.post("/api/scan", {"symbols": ["SPY", "QQQ"]})[0] == 200
        done = srv.wait("/api/scan/status", None, lambda d: d["state"] in ("done", "error"))
        assert done["state"] == "done"
        by = {p["symbol"]: p for p in done["progress"]}
        assert by["SPY"]["state"] == "done" and by["QQQ"]["state"] == "error" and "outage" in by["QQQ"]["message"]
        s, sg = srv.get("/api/signals")
        assert s == 200
        q = [x for x in sg["symbols"] if x["symbol"] == "QQQ"][0]
        assert q["error"] and q["spot"] is None
        assert srv.get("/api/ideas")[0] == 200 and srv.get("/api/status")[0] == 200
    finally:
        srv.close()


class _Dead:
    """A provider where everything raises."""
    name = "dead"

    def __getattr__(self, item):
        def fail(*a, **k):
            raise RuntimeError("provider is down")
        return fail


def test_dead_provider_does_not_crash_server(tmp_path):
    srv = Srv(make_app(tmp_path, fake=True, provider=lambda base: _Dead()))
    try:
        assert srv.post("/api/scan", {})[0] == 200
        done = srv.wait("/api/scan/status", None, lambda d: d["state"] in ("done", "error"))
        assert done["state"] in ("done", "error")
        assert all(p["state"] == "error" for p in done["progress"])
        for path in ("/api/status", "/api/ideas", "/api/signals", "/api/positions", "/api/equity", "/api/config"):
            assert srv.get(path)[0] == 200, path
        assert srv.get("/api/surface?symbol=SPY")[0] == 502
    finally:
        srv.close()


def test_scan_busy_returns_409(tmp_path):
    gate = threading.Event()

    class Gated(ScaledProvider):
        def chain(self, symbol, min_dte=0, max_dte=45):
            assert gate.wait(20)
            return super().chain(symbol, min_dte, max_dte)

    srv = Srv(make_app(tmp_path, fake=True, provider=lambda base: Gated(base)))
    try:
        assert srv.post("/api/scan", {"symbols": ["SPY"]})[0] == 200
        s, d = srv.get("/api/scan/status")
        assert d["state"] == "running" and d["progress"][0]["symbol"] == "SPY"
        s, d = srv.post("/api/scan", {"symbols": ["SPY"]})
        assert s == 409 and "already" in d["error"]
        assert srv.get("/api/status")[1]["mode"] == "scan"
        gate.set()
        done = srv.wait("/api/scan/status", None, lambda d: d["state"] != "running")
        assert done["state"] == "done"
        assert srv.post("/api/scan", {"symbols": ["SPY"]})[0] == 200  # a new one may start now
        srv.wait("/api/scan/status", None, lambda d: d["state"] != "running")
    finally:
        gate.set()
        srv.close()


def test_scan_validation(real):
    assert real.post("/api/scan", {"symbols": []})[0] == 400
    assert real.post("/api/scan", {"symbols": ["SPY", "bad sym!"]})[0] == 400
    assert real.post("/api/scan", {"symbols": "SPY"})[0] == 400
    assert real.post("/api/scan", {"nope": 1})[0] == 400


def test_backtest_real_run_on_simulated_prices(tmp_path):
    srv = Srv(make_app(tmp_path))
    try:
        req = {"symbols": ["SPY"], "days": 1, "source": "simulated"}
        s, d = srv.post("/api/backtest", req)
        assert s == 200 and isinstance(d["job_id"], str)
        res = srv.wait("/api/backtest/result", None, lambda d: d["state"] in ("done", "error"), timeout=120)
        check(res, BT)
        assert res["state"] == "done", res.get("error")
        assert res["request"] == req and res["progress"] == 1.0
        assert res["summary"] is not None and res["caveats"]
        assert any("synthetic" in c.lower() or "simulated" in c.lower() for c in res["caveats"])
        assert 0 <= res["summary"]["max_drawdown"] <= 1  # a fraction, as the dashboard formats it
    finally:
        srv.close()


def test_backtest_busy_validation_and_error_capture(tmp_path, monkeypatch):
    gate = threading.Event()
    calls = []

    class FakeBT:
        def __init__(self, cfg, engine_factory=None, step_minutes=15):
            pass

        def run(self, symbols, days=5, source="simulated", fetch_provider=None, on_progress=None, **kw):
            calls.append((symbols, days, source))
            on_progress(0.5)
            gate.wait(20)
            if symbols == ["QQQ"]:
                raise RuntimeError("boom")
            return type("R", (), {"to_dict": lambda self_: {
                "summary": {"trades": 0, "win_rate": 0.0, "profit_factor": float("inf"), "expectancy": 0.0,
                            "sharpe": float("nan"), "max_drawdown": 12.0, "total_pnl": 0.0},
                "metrics": {"max_drawdown_pct": 0.0012}, "equity": [], "by_strategy": [], "by_exit": [],
                "trades": [], "caveats": ["x"], "errors": []}})()

    monkeypatch.setattr("kwanttrades.backtest.Backtester", FakeBT)
    srv = Srv(make_app(tmp_path))
    try:
        for bad in ({"symbols": [], "days": 5}, {"symbols": ["SPY"], "days": 0}, {"symbols": ["SPY"], "days": 61},
                    {"symbols": ["SPY"], "days": 2.5}, {"symbols": ["SPY"], "source": "magic"},
                    {"symbols": ["SPY"], "zzz": 1}, {"symbols": ["../x"]}):
            assert srv.post("/api/backtest", bad)[0] == 400, bad
        assert srv.post("/api/backtest", {"symbols": ["spy"], "days": 3, "source": "real_bars"})[0] == 200
        s, d = srv.get("/api/backtest/result")
        assert d["state"] == "running" and d["progress"] == 0.5 and d["request"] == {"symbols": ["SPY"], "days": 3, "source": "real_bars"}
        assert srv.post("/api/backtest", {"symbols": ["SPY"]})[0] == 409
        gate.set()
        res = srv.wait("/api/backtest/result", None, lambda d: d["state"] == "done")
        check(res, BT)
        assert res["summary"]["profit_factor"] is None and res["summary"]["sharpe"] is None  # inf/NaN -> null
        assert res["summary"]["max_drawdown"] == 0.0012
        # a crash is captured in the status, the server keeps serving
        assert srv.post("/api/backtest", {"symbols": ["QQQ"], "days": 1})[0] == 200
        res = srv.wait("/api/backtest/result", None, lambda d: d["state"] == "error")
        assert "boom" in res["error"] and srv.get("/api/status")[0] == 200
    finally:
        gate.set()
        srv.close()


# --------------------------------------------------------------------------- config
def test_config_post_validation_and_persistence(tmp_path):
    srv = Srv(make_app(tmp_path))
    path = Path(tmp_path) / "config.toml"
    try:
        assert not path.exists()
        s, d = srv.post("/api/config", {"risk": {"max_trades_per_day": 4, "entry_window_start": "09:50"},
                                        "scan": {"min_expected_value": 12.5, "directional_dte": [1, 10],
                                                 "universe": ["spy", "QQQ", "spy"]}})
        assert s == 200
        check(d, CONFIG)
        assert d["risk"]["max_trades_per_day"] == 4 and d["scan"]["directional_dte"] == [1, 10]
        assert d["scan"]["universe"] == ["SPY", "QQQ"]
        assert srv.app.cfg.risk.max_trades_per_day == 4 and srv.app.cfg.scan.directional_dte == (1, 10)
        assert srv.get("/api/config")[1] == d
        saved = tomllib.loads(path.read_text(encoding="utf-8"))
        assert saved == {"risk": {"max_trades_per_day": 4, "entry_window_start": "09:50"},
                         "scan": {"min_expected_value": 12.5, "directional_dte": [1, 10], "universe": ["SPY", "QQQ"]}}
        assert srv.get("/api/status")[1]["max_trades_per_day"] == 4
        # a second change merges into the file; untouched sections stay
        assert srv.post("/api/config", {"signals": {"opening_range_minutes": 20}})[0] == 200
        saved2 = tomllib.loads(path.read_text(encoding="utf-8"))
        assert saved2["signals"] == {"opening_range_minutes": 20} and saved2["risk"] == saved["risk"]
        # the file loads with the project's own loader
        from kwanttrades.config import load_config

        again = load_config(path)
        assert again.risk.max_trades_per_day == 4 and again.scan.directional_dte == (1, 10)
        assert srv.post("/api/config", {})[0] == 200  # nothing to change is fine
    finally:
        srv.close()


@pytest.mark.parametrize("body,field", [
    ({"risk": {"nope": 1}}, "risk.nope"),
    ({"scan": {"min_score": 1, "bogus": 2}}, "scan.bogus"),
    ({"mystery": {"a": 1}}, "mystery"),
    ({"broker": {"mode": "alpaca_live"}}, "broker"),
    ({"risk": {"max_trades_per_day": 1000}}, "risk.max_trades_per_day"),
    ({"risk": {"max_trades_per_day": -1}}, "risk.max_trades_per_day"),
    ({"risk": {"max_trades_per_day": 2.5}}, "risk.max_trades_per_day"),
    ({"risk": {"max_trades_per_day": True}}, "risk.max_trades_per_day"),
    ({"risk": {"max_loss_per_trade_pct": 0.5}}, "risk.max_loss_per_trade_pct"),
    ({"risk": {"max_loss_per_trade_pct": 0}}, "risk.max_loss_per_trade_pct"),
    ({"risk": {"max_loss_per_trade_pct": "1%"}}, "risk.max_loss_per_trade_pct"),
    ({"risk": {"kelly_fraction": 1.5}}, "risk.kelly_fraction"),
    ({"risk": {"flatten_time": "25:00"}}, "risk.flatten_time"),
    ({"risk": {"entry_window_start": "16:00"}}, "risk.entry_window_start"),
    ({"risk": {"defined_risk_only": False}}, "risk.defined_risk_only"),
    ({"risk": {"pdt_guard": "yes"}}, "risk.pdt_guard"),
    ({"scan": {"universe": []}}, "scan.universe"),
    ({"scan": {"universe": ["SPY", "DROP TABLE"]}}, "scan.universe"),
    ({"scan": {"directional_dte": [10, 1]}}, "scan.directional_dte"),
    ({"scan": {"vol_dte": [1]}}, "scan.vol_dte"),
    ({"scan": {"min_direction": 2}}, "scan.min_direction"),
    ({"scan": {"min_expected_value": float("nan")}}, "scan.min_expected_value"),
    ({"risk": "oops"}, "risk"),
    ([1, 2], "object"),
])
def test_config_post_rejects_bad_input_naming_the_field(tmp_path, body, field):
    srv = Srv(make_app(tmp_path))
    try:
        before = srv.get("/api/config")[1]
        raw = json.dumps(body, allow_nan=True).encode()
        s, d = srv.req("POST", "/api/config", raw)
        assert s == 400, d
        assert field in d["error"], d
        assert srv.get("/api/config")[1] == before  # nothing applied
        assert not (Path(tmp_path) / "config.toml").exists()  # nothing written
    finally:
        srv.close()


def test_config_persist_failure_changes_nothing(tmp_path):
    bad = Path(tmp_path) / "config.toml"
    bad.write_text("this is [not toml", encoding="utf-8")
    srv = Srv(make_app(tmp_path))
    try:
        before = srv.get("/api/config")[1]
        s, d = srv.post("/api/config", {"risk": {"max_trades_per_day": 2}})
        assert s == 500 and "nothing was changed" in d["error"]
        assert srv.get("/api/config")[1] == before
        assert bad.read_text(encoding="utf-8") == "this is [not toml"  # a broken file is never overwritten
    finally:
        srv.close()


# --------------------------------------------------------------------------- HTTP guards
def test_host_header_guard(real):
    for host in ("evil.com", "127.0.0.1", f"127.0.0.1:{real.port + 1}", "localhost", f"attacker.localhost:{real.port}",
                 f"127.0.0.1.evil.com:{real.port}", f"[::1]:{real.port}"):
        for path in ("/api/status", "/", "/js/main.js"):
            s, _, _ = real.raw("GET", path, headers={"Host": host})
            assert s == 403, (host, path, s)
    for host in (f"127.0.0.1:{real.port}", f"localhost:{real.port}", f"LOCALHOST:{real.port}"):
        assert real.raw("GET", "/api/status", headers={"Host": host})[0] == 200


def test_origin_guard_on_posts(real):
    port = real.port
    ok = real.raw("POST", "/api/session/stop", {}, origin=f"http://localhost:{port}")
    assert ok[0] == 200
    for origin in ("http://evil.com", f"http://127.0.0.1:{port + 1}", f"https://127.0.0.1:{port}", "null",
                   f"http://127.0.0.1:{port}.evil.com", f"http://127.0.0.1.evil.com:{port}", f"http://user@127.0.0.1:{port}"):
        s, _, _ = real.raw("POST", "/api/session/stop", {}, origin=origin)
        assert s == 403, origin
    assert real.raw("POST", "/api/session/stop", {}, origin=None)[0] == 403  # neither Origin nor Referer
    # Referer is only consulted when Origin is absent
    assert real.raw("POST", "/api/session/stop", {}, origin=None, headers={"Referer": f"http://127.0.0.1:{port}/settings"})[0] == 200
    assert real.raw("POST", "/api/session/stop", {}, origin=None, headers={"Referer": "http://evil.com/x"})[0] == 403
    assert real.raw("POST", "/api/session/stop", {}, origin="http://evil.com", headers={"Referer": f"http://127.0.0.1:{port}/"})[0] == 403
    # a rejected POST must not have done anything
    s, _, _ = real.raw("POST", "/api/session/start", {"mode": "shadow"}, origin="http://evil.com")
    assert s == 403 and real.get("/api/status")[1]["running"] is False


def test_post_body_rules(real):
    s, d = real.req("POST", "/api/scan", b'{"symbols": ["SPY"]}', headers={"Content-Type": "text/plain"})
    assert s == 415
    s, d = real.req("POST", "/api/scan", b"{not json")
    assert s == 400
    s, d = real.req("POST", "/api/scan", b"[1,2]")
    assert s == 400
    big = b'{"x": "' + b"a" * (70 * 1024) + b'"}'
    s, d = real.req("POST", "/api/config", big)
    assert s == 413
    assert real.raw("PUT", "/api/status", {})[0] == 405
    assert real.raw("DELETE", "/api/positions", {})[0] == 405
    assert real.raw("OPTIONS", "/api/status")[0] == 405
    assert real.raw("POST", "/api/status", {})[0] == 405
    assert real.raw("GET", "/api/scan")[0] == 405
    assert real.raw("GET", "/api/nothing")[0] == 404
    assert real.raw("POST", "/api/nothing", {})[0] == 404
    assert real.raw("POST", "/", {})[0] == 404


def test_static_files_headers_and_mime(real):
    s, body, h = real.raw("GET", "/")
    assert s == 200 and b"<title>Kwant Trades</title>" in body and h["Content-Type"].startswith("text/html")
    assert b"<script>" not in body  # no inline script
    for path, mime in (("/js/main.js", "text/javascript"), ("/css/app.css", "text/css"), ("/js/theme.js", "text/javascript")):
        s, body, h = real.raw("GET", path)
        assert s == 200 and h["Content-Type"].startswith(mime), (path, h["Content-Type"])
    s, body, h = real.raw("GET", "/js/screens/overview.js")
    assert s == 200 and h["Content-Type"].startswith("text/javascript")
    for hdr in ("Cache-Control", "Content-Security-Policy", "X-Content-Type-Options"):
        assert hdr in h
    assert h["Cache-Control"] == "no-store"
    csp = h["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
    s, body, h = real.raw("GET", "/api/status")
    assert h["Content-Type"].startswith("application/json") and h["Cache-Control"] == "no-store"
    assert real.raw("HEAD", "/")[0] == 200


def test_every_resource_the_page_requests_exists(real):
    """The dashboard must load without a single 404: every script, style and module import resolves."""
    import re as _re

    root = srvmod.STATIC_ROOT
    index = (root / "index.html").read_text(encoding="utf-8")
    wanted = set(_re.findall(r'(?:src|href)="([^"#:]+)"', index))
    for f in (root / "js").rglob("*.js"):
        for m in _re.findall(r'from\s+"(\.{1,2}/[^"]+)"|import\("(\.{1,2}/[^"]+)"\)', f.read_text(encoding="utf-8")):
            rel = (m[0] or m[1])
            wanted.add(str((f.parent / rel).resolve().relative_to(root.resolve())).replace("\\", "/"))
    assert wanted
    for w in sorted(wanted):
        assert real.raw("GET", "/" + w.lstrip("/"))[0] == 200, w


def test_path_traversal_and_dotfiles(tmp_path, monkeypatch):
    root = tmp_path / "static"
    root.mkdir()
    (root / "index.html").write_text("<title>ok</title>", encoding="utf-8")
    (root / "app.js").write_text("export {}", encoding="utf-8")
    (root / ".env").write_text("SECRET=CANARY", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("CANARY", encoding="utf-8")
    (tmp_path / ".env").write_text("ALPACA_SECRET_KEY=CANARY", encoding="utf-8")
    monkeypatch.setattr(srvmod, "STATIC_ROOT", root)
    srv = Srv(make_app(tmp_path))
    try:
        assert srv.raw("GET", "/app.js")[0] == 200
        attempts = ["/..%2f", "/..%2fsecret.txt", "/%2e%2e/secret.txt", "/%2e%2e%2fsecret.txt", "/../secret.txt",
                    "/static/../../.env", "/static/../secret.txt", "/js/../../.env", "/..\\secret.txt", "/%5c..%5csecret.txt",
                    "/....//secret.txt", "/.env", "/static/.env", "/%2eenv", "//secret.txt", f"/{tmp_path.as_posix()}/secret.txt",
                    "/C:/Windows/win.ini", "/%252e%252e/secret.txt", "/app.js%00.html", "/app.js::$DATA", "/.%2e/secret.txt",
                    "/%c0%ae%c0%ae/secret.txt", "/index.html/../../secret.txt"]
        for a in attempts:
            s, body, _ = srv.raw("GET", a)
            assert s in (403, 404), (a, s)
            assert b"CANARY" not in body, a
        # unit level, including absolute paths
        for bad in ("/../secret.txt", "/a/../../b", "/..%2fx"):
            with pytest.raises(srvmod._Forbidden):
                resolve_static(root, bad)
        assert resolve_static(root, "/.env") is None
        assert resolve_static(root, "/missing.js") is None
        assert resolve_static(root, "/") == (root / "index.html").resolve()
        assert resolve_static(root, "/static/app.js") == (root / "app.js").resolve()
    finally:
        srv.close()


def test_real_static_root_never_serves_env_or_python(real):
    for p in ("/.env", "/.env.example", "/../.env", "/..%2f.env", "/static/../../.env", "/../../config.toml",
              "/../server.py", "/server.py", "/../app.py", "/%2e%2e/%2e%2e/.env", "/..%2f..%2f.env"):
        s, body, _ = real.raw("GET", p)
        assert s in (403, 404), (p, s)
        assert b"API_KEY" not in body and b"import " not in body
    assert real.raw("GET", "/.env")[0] == 404
    for ext_path in ("/js", "/js/", "/css"):  # directories are never listed
        assert real.raw("GET", ext_path)[0] == 404


def test_server_binds_loopback_only(real):
    assert real.server.server_address[0] == "127.0.0.1"
    with pytest.raises(SystemExit):
        srvmod.main(["--bind", "0.0.0.0"])  # there is no option to change the bind address
    assert srvmod.main(["--host", "0.0.0.0", "--no-browser", "--port", "0"]) == 2  # only with --remote (and a password)


# --------------------------------------------------------------------------- pure helpers
def test_jsonsafe_nan_inf_and_times():
    out = jsonsafe({"a": float("nan"), "b": float("inf"), "c": -float("inf"), "d": np.float64("nan"), "e": np.int64(3),
                    "f": [float("nan"), 1.5], "g": datetime(2026, 11, 12, 15, 30, tzinfo=NY), "h": np.array([1.0, np.nan])})
    assert out["a"] is None and out["b"] is None and out["c"] is None and out["d"] is None
    assert out["e"] == 3 and out["f"] == [None, 1.5] and out["h"] == [1.0, None]
    assert out["g"] == "2026-11-12T15:30:00-05:00"
    json.dumps(out, allow_nan=False)
    from datetime import timezone

    assert jsonsafe(datetime(2026, 11, 12, 20, 30, tzinfo=timezone.utc)) == "2026-11-12T15:30:00-05:00"


def test_payoff_breakevens_and_position_extras():
    from tests.test_app_fakes import build

    h = build()
    chain = h.provider.chain("SPY", 0, 30)
    idea = long_call_idea(chain, "SPY")
    pay = idea_payoff(idea, chain.spot, 0.2)
    assert 55 <= len(pay["prices"]) <= 70 and pay["prices"] == sorted(set(pay["prices"]))
    k = idea.legs[0].quote.strike
    pnl = dict(zip(pay["prices"], pay["pnl"]))
    assert math.isclose(pnl[k], -idea.entry_limit * 100, abs_tol=0.01)  # at/below the strike: lose the premium
    (be,) = idea_breakevens(idea)
    assert math.isclose(be, k + idea.entry_limit, abs_tol=0.01)
    from kwanttrades.types import Position

    pos = Position("p1", idea, 1, idea.entry_limit, THU)
    ex = position_extras(pos)
    assert math.isclose(ex["target_value"], idea.entry_limit * (1 + idea.take_profit_pct), abs_tol=1e-3)
    assert math.isclose(ex["stop_value"], idea.entry_limit - idea.stop_loss_pct * idea.max_loss / 100, abs_tol=1e-3)

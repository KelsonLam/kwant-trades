"""WebApp: owns config, provider, journal, engine and the TradingSessions behind the dashboard's JSON API.

Threads: one scan job, one session loop (shadow / paper / alpaca_paper) and one backtest job at a time. A crash
inside a job is stored in that job's status and never reaches the HTTP layer. `alpaca_live` cannot be started here.
"""
from __future__ import annotations

import dataclasses
import logging
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable

from kwanttrades.app.session import (
    TradingSession,
    effective_time,
    idea_dict,
    idea_id,
    market_state,
    next_close,
    next_open,
    opening_range_end,
    position_dict,
    strategy_label,
)
from kwanttrades.config import Config
from kwanttrades.execution.journal import Journal
from kwanttrades.learning import (
    Learner,
    LearningError,
    LearningHub,
    StageStore,
    learned_path_for,
    stage_path_for,
)
from kwanttrades.learning import evaluate as graduation_evaluate
from kwanttrades.learning.graduation import LIVE_HOW, LIVE_NOTE
from kwanttrades.learning.postmortem import CAUSES
from kwanttrades.types import NY, DirectionalSignal, now_ny
from kwanttrades.web import configio
from kwanttrades.web.serialize import (
    idea_breakevens,
    idea_payoff,
    jsonsafe,
    num,
    position_extras,
    surface_payload,
)

log = logging.getLogger("kwanttrades.web")

SYMBOL_RE = re.compile(r"^[A-Za-z\^][A-Za-z0-9.\-\^]{0,9}$")
ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,120}$")
NEWS_TTL = 600.0
SESSION_MODES = ("shadow", "paper", "alpaca_paper")
LIVE_REFUSAL = ("Live trading (real money) cannot be started from the web dashboard. It can only be enabled by the "
                "owner from the command line with the explicit safety setting described in the README.")
MAX_ORDER_FAILURES = 3  # consecutive Alpaca rejections/errors before falling back to the paper simulator
RECONCILE_STRIKES = 2  # consecutive steps on which Alpaca's positions must disagree with the journal


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def clean_symbol(s: Any) -> str:
    if not isinstance(s, str) or not SYMBOL_RE.match(s.strip()):
        raise HttpError(400, "symbol: expected a ticker like SPY")
    return s.strip().upper()


# --------------------------------------------------------------------------- scan progress plumbing
class _Progress:
    def __init__(self, symbols: list[str]):
        self._lock = threading.Lock()
        self._items = {s: {"symbol": s, "state": "queued", "message": ""} for s in symbols}

    def set(self, sym: str, state: str, message: str = "", only_if: tuple[str, ...] | None = None) -> None:
        with self._lock:
            it = self._items.get(sym)
            if it is None or (only_if is not None and it["state"] not in only_if):
                return
            it["state"], it["message"] = state, message[:240]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(v) for v in self._items.values()]


class _ProgressProvider:
    """Delegates to the real provider and reports which symbol the scan is working on."""

    def __init__(self, inner: Any, app: "WebApp"):
        self._inner = inner
        self._app = app

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def chain(self, symbol: str, *a: Any, **k: Any) -> Any:
        prog = self._app._progress
        if prog is not None:
            prog.set(symbol, "running", "fetching option chain", only_if=("queued",))
        try:
            return self._inner.chain(symbol, *a, **k)
        except Exception as exc:
            if prog is not None:
                prog.set(symbol, "error", f"{type(exc).__name__}: {exc}")
            raise


class _ProgressEngine:
    def __init__(self, inner: Any, app: "WebApp"):
        self._inner = inner
        self._app = app

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def analyze(self, symbol: str, *a: Any, **k: Any) -> Any:
        prog = self._app._progress
        if prog is not None:
            prog.set(symbol, "running", "analysing")
        return self._inner.analyze(symbol, *a, **k)

    def generate(self, analysis: Any) -> Any:
        ideas = self._inner.generate(analysis)
        prog = self._app._progress
        if prog is not None:
            prog.set(getattr(analysis, "symbol", ""), "done", f"{len(ideas or [])} idea(s)")
        return ideas


# --------------------------------------------------------------------------- the app
class WebApp:
    def __init__(self, cfg: Config, provider: Any, *, provider_name: str | None = None, journal: Journal | None = None,
                 engine_factory: Callable[[Config], Any] | None = None, news: Any = None,
                 clock: Callable[[], datetime] | None = None, config_path: str | os.PathLike = "config.toml",
                 paper_broker: Any = None, alpaca_factory: Callable[[], Any] | None = None):
        self.cfg = cfg
        self.provider = provider
        self.provider_name = provider_name or getattr(provider, "name", None) or type(provider).__name__
        self.journal = journal if journal is not None else Journal(cfg.broker.db_path)
        if engine_factory is None:
            from kwanttrades.strategy.engine import StrategyEngine

            engine_factory = StrategyEngine
        self.engine_factory = engine_factory
        self.engine = engine_factory(cfg)
        self.news = news
        self.clock = clock
        self.config_path = str(config_path)
        if paper_broker is None:
            from kwanttrades.execution.paper import PaperBroker

            paper_broker = PaperBroker(dataclasses.replace(cfg.broker, mode="paper"), self.journal, clock=clock)
        self.paper_broker = paper_broker
        self.active_broker: Any = paper_broker
        self.active_mode = "paper"
        self.alpaca_factory = alpaca_factory  # tests inject a fake; default builds AlpacaBroker in paper mode

        # supervised learning + graduation (files next to the journal; memory only for an in-memory journal)
        self.learner = Learner(cfg, learned_path_for(self.journal), self.journal, clock)
        self.hub = LearningHub(cfg, self.journal, self.learner)
        self.learner.apply_engine(self.engine)
        self.learner.listeners.append(lambda: self.learner.apply_engine(self.engine))
        self.stage = StageStore(stage_path_for(self.journal))
        self.hub.callbacks.append(self._after_step)
        self._switching = False
        self._recon_strikes = 0
        self._grad_checked = False

        self._lock = threading.RLock()
        self._cfg_lock = threading.Lock()
        self.scan_session: TradingSession | None = None
        self.run_session: TradingSession | None = None
        self._run_thread: threading.Thread | None = None
        self._stop_requested = False
        self._session_error: str | None = None
        self._flatten_session: TradingSession | None = None
        self._progress: _Progress | None = None
        self._scan: dict[str, Any] = {"job_id": None, "state": "idle", "started": None, "finished": None, "error": None,
                                      "progress": []}
        self._scan_stamp = 0.0
        self._run_stamp = 0.0
        self._seen_run_last: Any = None
        self._bt: dict[str, Any] = {"state": "idle", "progress": 0.0, "result": None, "error": None, "request": None}
        self._news_cache: dict[str, tuple[float, list[dict]]] = {}
        self._news_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ small helpers
    def now(self) -> datetime:
        return (self.clock or now_ny)().astimezone(NY)

    @staticmethod
    def alpaca_keys_present() -> bool:
        return bool(os.environ.get("ALPACA_API_KEY")) and bool(os.environ.get("ALPACA_SECRET_KEY"))

    def sentiment_engine(self) -> str:
        return "lexicon"

    def _running(self) -> bool:
        t = self._run_thread
        return bool(t is not None and t.is_alive() and not self._stop_requested)

    def _latest(self) -> TradingSession | None:
        """The session whose snapshot is the most recent one (a finished scan or the running loop's last step)."""
        with self._lock:
            rs = self.run_session
            if rs is not None and rs.last is not self._seen_run_last:
                self._seen_run_last = rs.last
                self._run_stamp = time.monotonic()
            cands = []
            if self.scan_session is not None and self.scan_session.snapshot():
                cands.append((self._scan_stamp, self.scan_session))
            if rs is not None and rs.snapshot():
                cands.append((self._run_stamp, rs))
            return max(cands, key=lambda c: c[0])[1] if cands else None

    def _snap(self) -> dict:
        s = self._latest()
        return s.snapshot() if s is not None else {}

    # ------------------------------------------------------------------ status
    def _account(self) -> dict:
        rs = self.run_session
        try:
            if self._running() and rs is not None and rs.broker is None:
                acct = rs._virtual_account()
            else:
                acct = self.active_broker.account()
            return {"equity": acct.equity, "cash": acct.cash, "buying_power": acct.buying_power,
                    "day_pnl": acct.day_pnl, "open_risk": acct.open_risk}
        except Exception as exc:
            log.warning("account unavailable: %s", exc)
            eq = float(self.cfg.broker.starting_equity)
            return {"equity": eq, "cash": eq, "buying_power": eq, "day_pnl": 0.0, "open_risk": 0.0}

    def status(self) -> dict:
        now = self.now()
        state = market_state(now)
        snap = self._snap()
        acct = self._account()
        running = self._running()
        rs = self.run_session
        mode = rs.mode if (running and rs is not None) else ("scan" if self._scan["state"] == "running" else "idle")
        if rs is not None and running:
            kill, trades = rs.killed, int(rs.last.trades_today if rs.last else 0)
        else:
            kd = self.journal.get_value("kill_switch_day")
            kill = kd == now.date().isoformat()
            trades = self._trades_today(now)
        data_now = effective_time(now)
        if state in ("closed", "pre"):
            note = (f"Market is {'closed' if state == 'closed' else 'not open yet'}: quotes are from the last close "
                    f"({data_now:%a %b %d, %H:%M} NY). Scans still work, for review only; no opening orders are sent.")
        elif state == "opening_range":
            note = "Opening range still forming (first 15 minutes). Free data feeds are delayed by about 15 minutes."
        else:
            note = "Free data feeds are delayed by about 15 minutes."
        if self.provider_name == "synthetic":
            note += " Data source: SIMULATED test data, not real quotes."
        eq, day = acct["equity"], acct["day_pnl"]
        r = self.cfg.risk
        return jsonsafe({
            "mode": mode, "running": running, "now": now,
            "market": {"state": state, "next_open": next_open(now), "next_close": next_close(now),
                       "opening_range_ends": opening_range_end(now) if state in ("pre", "opening_range") else None},
            "account": acct, "kill_switch": bool(kill), "provider": self.provider_name,
            "data_delay_note": note, "universe": list(self.cfg.scan.universe), "last_scan": snap.get("asof"),
            "trades_today": trades, "max_trades_per_day": r.max_trades_per_day,
            "alpaca_keys_present": self.alpaca_keys_present(), "sentiment_engine": self.sentiment_engine(),
            "limits": {"max_portfolio_risk": r.max_portfolio_risk_pct * eq,
                       "daily_loss_limit": r.daily_loss_limit_pct * max(eq - day, 0.0),
                       "max_loss_per_trade": r.max_loss_per_trade_pct * eq},
            "session_error": self._session_error,
            "stage": self.stage.stage, "stage_ready": bool(self.stage.data.get("ready")),
            "stage_alert": self.stage.data.get("alert"),
        })

    def _trades_today(self, now: datetime) -> int:
        try:
            return int(self.journal.trades_today(now))
        except Exception:
            return 0

    # ------------------------------------------------------------------ scan job
    def _ensure_scan_session(self) -> TradingSession:
        if self.scan_session is None:
            self.scan_session = TradingSession(
                self.cfg, _ProgressProvider(self.provider, self), None, _ProgressEngine(self.engine, self), self.journal,
                news=self.news, mode="scan", clock=self.clock, data_name=self.provider_name)
            self.scan_session.learning = self.hub
        return self.scan_session

    def start_scan(self, symbols: Any) -> dict:
        if symbols is None:
            syms = list(self.cfg.scan.universe)
        else:
            if not isinstance(symbols, list) or not symbols or len(symbols) > 60:
                raise HttpError(400, "symbols: expected null or a list of 1 to 60 tickers")
            syms = list(dict.fromkeys(clean_symbol(s) for s in symbols))
        with self._lock:
            if self._scan["state"] == "running":
                raise HttpError(409, "A scan is already running.")
            job_id = uuid.uuid4().hex[:12]
            self._progress = _Progress(syms)
            self._scan.update(job_id=job_id, state="running", started=self.now(), finished=None, error=None,
                              progress=self._progress.snapshot())
            threading.Thread(target=self._scan_main, args=(job_id, syms), name="kwanttrades-scan", daemon=True).start()
        return {"job_id": job_id}

    def _scan_main(self, job_id: str, syms: list[str]) -> None:
        error: str | None = None
        try:
            session = self._ensure_scan_session()
            session.universe = list(self.cfg.scan.universe)
            res = session.step(self.now(), symbols=syms)
            prog = self._progress
            for sym in syms:
                rec = res.symbols.get(sym)
                if rec is None:
                    prog.set(sym, "error", "not analysed")
                elif rec.error:
                    prog.set(sym, "error", rec.error)
                else:
                    prog.set(sym, "done", f"{rec.n_ideas} idea(s)")
            fatal = [e for e in res.errors if e.get("stage") == "step"]
            if fatal:
                error = fatal[0]["message"]
            self._scan_stamp = time.monotonic()
        except Exception as exc:  # never kill the server
            log.exception("scan failed")
            error = f"{type(exc).__name__}: {exc}"[:300]
        with self._lock:
            if self._progress is not None:
                self._scan["progress"] = self._progress.snapshot()
            self._scan.update(state="error" if error else "done", error=error, finished=self.now())

    def scan_status(self) -> dict:
        with self._lock:
            s = dict(self._scan)
            if s["state"] == "running" and self._progress is not None:
                s["progress"] = self._progress.snapshot()
            return jsonsafe(s)

    # ------------------------------------------------------------------ ideas / signals
    def ideas(self) -> dict:
        snap_owner = self._latest()
        if snap_owner is None or snap_owner.last is None:
            return {"asof": None, "ideas": []}
        res = snap_owner.last
        out = []
        for rec in res.ideas:
            try:
                srec = res.symbols.get(rec.idea.symbol)
                sigma = getattr(getattr(srec, "vol_view", None), "atm_iv", None)
                d = idea_dict(rec.idea, rec.contracts, rec.decision, rec.spot, rec.flags,
                              action=rec.action, order_id=rec.order_id, order_status=rec.order_status)
                d["payoff"] = idea_payoff(rec.idea, rec.spot, sigma)
                d["breakevens"] = idea_breakevens(rec.idea)
                d["strategy_label"] = strategy_label(rec.idea.strategy)
                d["greeks"] = {k: num((rec.idea.greeks or {}).get(k)) for k in ("delta", "gamma", "vega", "theta")}
                out.append(d)
            except Exception as exc:
                log.warning("could not render idea %s: %s", getattr(rec.idea, "symbol", "?"), exc)
        return jsonsafe({"asof": res.now, "ideas": out})

    def signals(self) -> dict:
        snap = self._snap()
        if not snap:
            return {"asof": None, "regime": None, "symbols": []}
        return jsonsafe({"asof": snap.get("asof"), "regime": snap.get("regime"), "symbols": snap.get("symbols", []),
                         "errors": snap.get("errors", []), "notes": snap.get("notes", [])})

    # ------------------------------------------------------------------ surface
    def _find_analysis(self, sym: str) -> Any:
        rs = self.run_session
        for s in (self._latest(), self.scan_session, rs):
            if s is not None and sym in s.analyses:
                return s, s.analyses[sym]
        return None, None

    def surface(self, symbol: str) -> dict:
        sym = clean_symbol(symbol)
        owner, analysis = self._find_analysis(sym)
        if analysis is None or getattr(analysis, "surface", None) is None:
            analysis = self._analyse_now(sym)
            owner = None
        flags = TradingSession._flags(analysis)
        return jsonsafe(surface_payload(analysis, flags))

    def _analyse_now(self, sym: str) -> Any:
        try:
            now = self.now()
            chain = self.provider.chain(sym, 0, 60)
            daily = self.provider.daily_bars(sym, 2.0)
            return self.engine.analyze(sym, chain, daily, DirectionalSignal(sym, "composite", 0.0, 0.0, {}))
        except HttpError:
            raise
        except Exception as exc:
            raise HttpError(502, f"Could not build the volatility surface for {sym}: {type(exc).__name__}: {exc}"[:300]) from None

    # ------------------------------------------------------------------ positions / equity / decisions
    def _broker_positions(self) -> tuple[list, list]:
        for attempt in range(3):
            try:
                allp = self.active_broker.positions(include_closed=True)
                break
            except RuntimeError:  # dict mutated by the session thread; retry
                time.sleep(0.02)
        else:
            allp = []
        return [p for p in allp if p.is_open], [p for p in allp if not p.is_open]

    def positions(self) -> dict:
        rs = self.run_session
        if rs is not None and rs.broker is None and self._running():
            open_ps, closed_ps = list(rs._shadow_book), []
        else:
            open_ps, closed_ps = self._broker_positions()
        closed_ps = sorted(closed_ps, key=lambda p: p.closed or p.opened, reverse=True)[:50]

        def render(p: Any) -> dict:
            d = position_dict(p)
            d.update(position_extras(p))
            if not p.is_open:
                pm = None
                try:
                    pm = self.journal.post_mortem(p.id)
                except Exception:  # pragma: no cover
                    pm = None
                d["postmortem"] = None if pm is None else {
                    "cause": pm["cause"], "cause_label": CAUSES.get(pm["cause"], pm["cause"]), "cause_text": pm["cause_text"]}
            return d

        return jsonsafe({"open": [render(p) for p in open_ps], "closed": [render(p) for p in closed_ps]})

    def equity(self) -> dict:
        df = self.journal.equity_curve()
        pts = [{"t": ts.to_pydatetime(), "equity": float(v)} for ts, v in df["equity"].items()]
        if len(pts) > 1500:
            step = math.ceil(len(pts) / 1500)
            pts = pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else [])
        return jsonsafe({"points": pts, "starting_equity": self.cfg.broker.starting_equity})

    def decisions(self, limit: int) -> dict:
        rows = self.journal.decisions(limit)
        out = []
        for r in rows:
            idea = r["idea"]
            out.append({"t": r["ts"], "symbol": r["symbol"], "strategy": r["strategy"], "passed": r["decision"] == "PASS",
                        "reasons": r["reasons"], "contracts": r["contracts"], "score": idea.score,
                        "expected_value": idea.expected_value, "idea_id": idea_id(idea)})
        return jsonsafe({"decisions": out})

    # ------------------------------------------------------------------ news
    def _news_scored(self, sym: str) -> list[dict]:
        from kwanttrades.data.sentiment import score_news

        items = self.news.headlines(sym, self.cfg.sentiment.lookback_hours)
        by_head = {it.headline[:140]: it for it in items}
        sig = score_news(sym, items, self.cfg.sentiment, now=self.now())
        out = []
        for di in sig.detail.get("items", []):
            src = by_head.get(di.get("headline"))
            out.append({"symbol": sym, "headline": di.get("headline", ""), "source": getattr(src, "source", "") or "",
                        "url": getattr(src, "url", "") or "", "published": di.get("published"),
                        "sentiment": num(di.get("sentiment")), "relevance": num(di.get("relevance")),
                        "materiality": num(di.get("materiality"))})
        return out

    def _news_refresh(self, sym: str) -> list[dict]:
        try:
            items = self._news_scored(sym) if self.news is not None else []
            ttl_ok = True
        except Exception as exc:
            log.warning("news fetch failed for %s: %s", sym, exc)
            items, ttl_ok = [], False
        # a failed fetch is retried after a minute rather than after the full TTL
        self._news_cache[sym] = (time.monotonic() - (0 if ttl_ok else NEWS_TTL - 60.0), items)
        return items

    def _news_fresh(self, sym: str) -> bool:
        hit = self._news_cache.get(sym)
        return hit is not None and time.monotonic() - hit[0] < NEWS_TTL

    def _news_from_snapshot(self, sym: str) -> list[dict]:
        out = []
        for s in self._snap().get("symbols", []):
            if s.get("symbol") != sym:
                continue
            comp = (s.get("components") or {}).get("news") or {}
            for di in (comp.get("detail") or {}).get("items", []):
                out.append({"symbol": sym, "headline": di.get("headline", ""), "source": "", "url": "",
                            "published": di.get("published"), "sentiment": num(di.get("sentiment")),
                            "relevance": num(di.get("relevance")), "materiality": num(di.get("materiality"))})
        return out

    def _news_worker(self, syms: list[str]) -> None:
        try:
            for sym in syms:
                if not self._news_fresh(sym):
                    self._news_refresh(sym)
        finally:
            self._news_thread = None

    def news_items(self, symbol: str) -> dict:
        if symbol.upper() == "ALL":
            syms = list(self.cfg.scan.universe)
            stale = [s for s in syms if not self._news_fresh(s)]
            if stale and self.news is not None:
                with self._lock:
                    if self._news_thread is None:
                        self._news_thread = threading.Thread(target=self._news_worker, args=(stale,),
                                                             name="kwanttrades-news", daemon=True)
                        self._news_thread.start()
            items: list[dict] = []
            for s in syms:
                hit = self._news_cache.get(s)
                items += hit[1] if hit and hit[1] else self._news_from_snapshot(s)
        else:
            sym = clean_symbol(symbol)
            if self.news is not None and not self._news_fresh(sym):
                self._news_refresh(sym)
            hit = self._news_cache.get(sym)
            items = hit[1] if hit and hit[1] else self._news_from_snapshot(sym)
        items.sort(key=lambda i: i.get("published") or "", reverse=True)
        return jsonsafe({"items": items[:80]})

    # ------------------------------------------------------------------ config
    def get_config(self) -> dict:
        c = self.cfg
        return jsonsafe({"risk": dataclasses.asdict(c.risk), "scan": dataclasses.asdict(c.scan),
                         "signals": dataclasses.asdict(c.signals),
                         "broker": {"mode": c.broker.mode, "starting_equity": c.broker.starting_equity}})

    def update_config(self, body: Any) -> dict:
        try:
            updates = configio.validate_update(body)
        except configio.ConfigError as exc:
            raise HttpError(400, str(exc)) from None
        with self._cfg_lock:
            try:
                configio.check_consistency(self.cfg, updates)
            except configio.ConfigError as exc:
                raise HttpError(400, str(exc)) from None
            if updates:
                try:
                    configio.persist_updates(self.config_path, updates)
                except Exception as exc:
                    log.error("could not save %s: %s", self.config_path, exc)
                    raise HttpError(500, f"Could not save the settings file ({type(exc).__name__}); nothing was changed.") from None
                for sec, vals in updates.items():
                    obj = getattr(self.cfg, sec)
                    for k, v in vals.items():
                        setattr(obj, k, v)
                self._sync_universe()
        return self.get_config()

    def _sync_universe(self) -> None:
        for s in (self.scan_session, self.run_session, self._flatten_session):
            if s is not None:
                s.universe = list(self.cfg.scan.universe)

    # ------------------------------------------------------------------ session control
    def start_session(self, mode: Any) -> dict:
        if mode == "alpaca_live":
            raise HttpError(400, LIVE_REFUSAL)
        if mode not in SESSION_MODES:
            raise HttpError(400, f"mode: choose one of {', '.join(SESSION_MODES)}")
        with self._lock:
            if self._running() and self.run_session is not None and self.run_session.mode == mode:
                raise HttpError(409, f"A {mode} session is already running.")
        if self._running():
            self.stop_session()
        with self._lock:
            broker = None
            if mode == "paper":
                broker = self.paper_broker
            elif mode == "alpaca_paper":
                broker = self._make_alpaca()
            session = TradingSession(self.cfg, self.provider, broker, self.engine, self.journal, news=self.news,
                                     mode=mode, clock=self.clock, data_name=self.provider_name)
            session.universe = list(self.cfg.scan.universe)
            session.learning = self.hub
            self._recon_strikes = 0
            self.run_session = session
            self._seen_run_last = None
            if broker is not None:
                self.active_broker, self.active_mode = broker, mode
            self._stop_requested = False
            self._session_error = None
            self._run_thread = threading.Thread(target=self._session_main, args=(session,), name=f"kwanttrades-{mode}", daemon=True)
            self._run_thread.start()
        return self.status()

    def _make_alpaca(self) -> Any:
        if not self.alpaca_keys_present():
            raise HttpError(400, "No Alpaca paper keys were found on this computer. Add them to the .env file "
                                 "(see the README), restart the dashboard, then try again.")
        try:
            if self.alpaca_factory is not None:
                return self.alpaca_factory()
            from kwanttrades.execution.alpaca import AlpacaBroker

            return AlpacaBroker(dataclasses.replace(self.cfg.broker, mode="alpaca_paper"), journal=self.journal)
        except Exception as exc:
            raise HttpError(400, f"Could not start the Alpaca paper session: {type(exc).__name__}: {exc}"[:300]) from None

    def _session_main(self, session: TradingSession) -> None:
        try:
            session.run()
        except Exception as exc:  # never kill the server
            log.exception("session loop crashed")
            self._session_error = f"{type(exc).__name__}: {exc}"[:300]

    def stop_session(self) -> dict:
        with self._lock:
            rs, t = self.run_session, self._run_thread
            if rs is not None and t is not None and t.is_alive():
                self._stop_requested = True
                rs.stop()
        if t is not None and t.is_alive():
            t.join(timeout=15)
        return self.status()

    def _broker_session(self) -> TradingSession | None:
        """A TradingSession that can close positions on the active broker (None when only a shadow session is live)."""
        with self._lock:
            rs = self.run_session
            if rs is not None and rs.broker is not None:
                return rs
            if self._running() and rs is not None and rs.broker is None:
                return None
            if self._flatten_session is None or self._flatten_session.broker is not self.active_broker:
                self._flatten_session = TradingSession(
                    self.cfg, self.provider, self.active_broker, self.engine, self.journal, mode=self.active_mode,
                    clock=self.clock, data_name=self.provider_name)
                self._flatten_session.learning = self.hub
            return self._flatten_session

    def close_position(self, position_id: str) -> dict:
        if not ID_RE.match(position_id or ""):
            raise HttpError(400, "invalid position id")
        sess = self._broker_session()
        if sess is None:
            return {"ok": False, "message": "A shadow session only records what it would trade; there is nothing to close. "
                                            "Stop the session to clear it."}
        open_ids = {p.id for p in sess.broker.positions() if p.is_open}
        if position_id not in open_ids:
            raise HttpError(404, "Position not found or already closed.")
        out = sess.close_position(position_id, self.now())
        self._postmortems_now(sess)
        ex = out.get("exit") or {}
        if out.get("ok"):
            pnl = ex.get("pnl")
            msg = f"Closed {ex.get('symbol', '')} {strategy_label(ex.get('strategy', ''))}" + (
                f": result {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f}." if isinstance(pnl, (int, float)) else ".")
        else:
            msg = {"no_chain": "Could not close: no option quotes are available right now.",
                   "no_quote": "Could not close: a leg has no current quote.",
                   "error": "The broker rejected the closing order."}.get(out.get("message"), f"Not closed ({out.get('message')}).")
        return jsonsafe({"ok": bool(out.get("ok")), "message": msg})

    def flatten(self) -> dict:
        sess = self._broker_session()
        if sess is None:
            return {"ok": False, "message": "A shadow session holds no positions to close. Stop the session to clear it."}
        n_open = len([p for p in sess.broker.positions() if p.is_open])
        if n_open == 0:
            return {"ok": True, "message": "There are no open positions."}
        exits = sess.flatten_all(self.now())
        self._postmortems_now(sess)
        done = sum(1 for e in exits if e.get("status") == "filled")
        ok = done == n_open
        return jsonsafe({"ok": ok, "message": f"Closed {done} of {n_open} position{'s' if n_open != 1 else ''}."
                         + ("" if ok else " The rest could not be priced or were rejected; try again.")})

    # ------------------------------------------------------------------ backtest job
    def start_backtest(self, body: Any) -> dict:
        if not isinstance(body, dict):
            raise HttpError(400, "the request body must be a JSON object")
        extra = set(body) - {"symbols", "days", "source"}
        if extra:
            raise HttpError(400, f"{sorted(extra)[0]}: unknown field")
        syms = body.get("symbols")
        if not isinstance(syms, list) or not syms or len(syms) > 12:
            raise HttpError(400, "symbols: expected a list of 1 to 12 tickers")
        syms = list(dict.fromkeys(clean_symbol(s) for s in syms))
        days = body.get("days", 5)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 60:
            raise HttpError(400, "days: expected a whole number from 1 to 60")
        source = body.get("source", "real_bars")
        if source not in ("real_bars", "simulated"):
            raise HttpError(400, "source: expected real_bars or simulated")
        with self._lock:
            if self._bt["state"] == "running":
                raise HttpError(409, "A backtest is already running.")
            job_id = uuid.uuid4().hex[:12]
            req = {"symbols": syms, "days": days, "source": source}
            self._bt = {"state": "running", "progress": 0.0, "result": None, "error": None, "request": req, "job_id": job_id}
            threading.Thread(target=self._bt_main, args=(job_id, req), name="kwanttrades-backtest", daemon=True).start()
        return {"job_id": job_id}

    def _bt_main(self, job_id: str, req: dict) -> None:
        def progress(f: float) -> None:
            self._bt["progress"] = float(f)

        try:
            from kwanttrades.backtest import Backtester

            bt = Backtester(self.cfg, engine_factory=self.engine_factory, step_minutes=30)
            fetch = self.provider if (req["source"] == "real_bars" and self.provider_name != "synthetic") else None
            res = bt.run(req["symbols"], days=req["days"], source=req["source"], fetch_provider=fetch, on_progress=progress)
            out = res.to_dict()
            summary = dict(out["summary"])
            summary["max_drawdown_usd"] = summary.get("max_drawdown")
            summary["max_drawdown"] = out["metrics"].get("max_drawdown_pct")  # the dashboard shows a fraction
            result = {"summary": summary, "equity": out["equity"], "by_strategy": out["by_strategy"],
                      "by_exit": out["by_exit"], "caveats": out["caveats"], "errors": out["errors"],
                      "trades": [{"symbol": t["symbol"], "strategy": t["strategy"], "opened": t["opened"],
                                  "closed": t["closed"], "pnl": t["pnl"], "exit_reason": t["exit_reason"]}
                                 for t in out["trades"]]}
            with self._lock:
                if self._bt.get("job_id") == job_id:
                    self._bt.update(state="done", progress=1.0, result=result)
        except Exception as exc:  # never kill the server
            log.exception("backtest failed")
            with self._lock:
                if self._bt.get("job_id") == job_id:
                    self._bt.update(state="error", error=f"{type(exc).__name__}: {exc}"[:300])

    def backtest_result(self) -> dict:
        with self._lock:
            b = dict(self._bt)
        r = b.get("result") or {}
        return jsonsafe({
            "state": b["state"], "progress": b["progress"], "summary": r.get("summary"), "equity": r.get("equity", []),
            "by_strategy": r.get("by_strategy", []), "by_exit": r.get("by_exit", []), "trades": r.get("trades", []),
            "caveats": r.get("caveats", []), "errors": r.get("errors", []), "error": b.get("error"),
            "request": b.get("request"),
        })

    # ------------------------------------------------------------------ learning
    def _postmortems_now(self, sess: TradingSession) -> None:
        """Write post-mortems for positions just closed by hand (instead of waiting for the next session step)."""
        try:
            with sess._lock:
                self.hub.after_step(sess, self.now())
        except Exception as exc:  # pragma: no cover
            log.warning("post-mortem after a manual close failed: %s", exc)

    def _pm_dict(self, pm: dict) -> dict:
        f, o = pm.get("features") or {}, pm.get("outcome") or {}
        keep_o = ("move_pct", "move_sigma", "direction_right", "realized_vs_implied", "realized_vs_model", "hold_minutes",
                  "sessions_held", "entry_spot", "exit_spot", "cost_total", "gross_pnl", "gap_sigma", "best_post_pnl",
                  "recovered", "entry_price", "exit_price", "news_right")
        keep_f = ("direction_call", "composite_direction", "composite_confidence", "vol_edge", "risk_multiplier",
                  "orb_state", "entry_bucket_min", "dte_entry", "model_ev", "pop", "score", "entry_iv", "cost_frac",
                  "intraday", "stop_loss_pct", "take_profit_pct", "recorded", "news_direction", "news_confidence")
        return {
            "position_id": pm["position_id"], "symbol": pm["symbol"], "strategy": pm["strategy"],
            "strategy_label": strategy_label(pm["strategy"] or ""), "mode": pm["mode"], "opened": pm["opened"],
            "closed": pm["closed"], "pnl": pm["realized_pnl"], "r": pm["r"], "win": pm["win"],
            "exit_reason": pm["exit_reason"], "cause": pm["cause"], "cause_label": CAUSES.get(pm["cause"], pm["cause"]),
            "cause_text": pm["cause_text"], "tracking_done": pm["tracking_done"],
            "evidence": {**{k: f.get(k) for k in keep_f}, **{k: o.get(k) for k in keep_o}},
        }

    def _history_dict(self, h: dict, hist: list[dict]) -> dict:
        knob = h.get("knob")
        can = h.get("kind") == "change" and h.get("status") == "active" and not any(
            x is not h and x.get("kind") == "change" and x.get("status") == "active" and x.get("knob") == knob
            and str(x.get("ts")) > str(h.get("ts")) for x in hist)
        fmt = (lambda v: self.learner.fmt(knob, v)) if knob else (lambda v: "defaults" if not v else f"{len(v)} setting(s)")
        return {"id": h.get("id"), "kind": h.get("kind"), "ts": h.get("ts"), "knob": knob, "label": h.get("label"),
                "before": h.get("before"), "after": h.get("after"), "display_before": fmt(h.get("before")),
                "display_after": fmt(h.get("after")), "rule": h.get("rule"), "reason": h.get("reason"),
                "evidence": h.get("evidence") or {}, "status": h.get("status"), "by": h.get("by"),
                "reverted_at": h.get("reverted_at"), "revert_reason": h.get("revert_reason"),
                "evaluation": h.get("evaluation"), "can_revert": bool(can)}

    def learning(self) -> dict:
        trades = self.hub.trades()
        hist = self.learner.history()
        rs = self.run_session
        mode = rs.mode if (self._running() and rs is not None) else None
        settings = self.learner.settings()
        return jsonsafe({
            "frozen": self.learner.frozen,
            "active": mode in ("paper", "alpaca_paper"),
            "mode": mode,
            "postmortems": [self._pm_dict(t) for t in reversed(trades)][:200],
            "settings": settings,
            "changed": [x for x in settings if x["changed"]],
            "history": [self._history_dict(h, hist) for h in reversed(hist)],
            "summary": self.learner.status(len(trades), self.now()),
        })

    def learning_freeze(self, body: Any) -> dict:
        if not isinstance(body, dict) or set(body) != {"frozen"} or not isinstance(body["frozen"], bool):
            raise HttpError(400, "frozen: expected true or false")
        self.learner.set_frozen(body["frozen"], self.now())
        return self.learning()

    def learning_revert(self, body: Any) -> dict:
        if not isinstance(body, dict) or set(body) != {"change_id"} or not isinstance(body["change_id"], str) \
                or not ID_RE.match(body["change_id"]):
            raise HttpError(400, "change_id: expected the id of a change")
        try:
            self.learner.revert(body["change_id"], self.now())
        except LearningError as exc:
            raise HttpError(exc.status, exc.message) from None
        return self.learning()

    def learning_reset(self, body: Any) -> dict:
        if body not in ({}, None):
            raise HttpError(400, "reset takes no fields")
        self.learner.reset(self.now())
        return self.learning()

    # ------------------------------------------------------------------ graduation (paper -> alpaca_paper)
    def _oos_since(self) -> datetime | None:
        ts = [t for t in (self.learner.last_change_ts(), self.stage.last_fallback_ts()) if t is not None]
        return max(ts) if ts else None

    def graduation_eval(self) -> dict:
        return graduation_evaluate(self.journal.post_mortems("paper"), self.cfg.graduation,
                                   float(self.cfg.broker.starting_equity), self._oos_since())

    def stage_session_mode(self) -> str:
        """The session `--start-session auto` starts: alpaca_paper once graduated (and keys exist), else paper.
        Never live: the stage file can only hold "paper" or "alpaca_paper"."""
        return "alpaca_paper" if self.stage.stage == "alpaca_paper" and self.alpaca_keys_present() else "paper"

    def graduation(self) -> dict:
        res = self.graduation_eval()
        g = self.cfg.graduation
        stage = self.stage.stage
        keys = self.alpaca_keys_present()
        needs = []
        if stage == "paper" and res["pass"]:
            if not keys:
                needs.append("Add your Alpaca PAPER keys (ALPACA_API_KEY and ALPACA_SECRET_KEY) to the .env file yourself, "
                             "then restart the dashboard. Kwant Trades never writes keys.")
            if not g.auto_switch:
                needs.append("Automatic switching is off ([graduation] auto_switch = false in config.toml): start an "
                             "Alpaca paper session yourself from Settings.")
        live = None
        if stage == "alpaca_paper":
            grad = self.stage.last_event("graduated")
            since = [t for t in (self._oos_since(), datetime.fromisoformat(grad["ts"]) if grad else None) if t is not None]
            ev = graduation_evaluate(self.journal.post_mortems("alpaca_paper"), g, float(self.cfg.broker.starting_equity),
                                     max(since) if since else None)
            live = {**ev, "note": LIVE_NOTE, "how": LIVE_HOW}
        return jsonsafe({
            "stage": stage, "ready": bool(res["pass"]), "auto_switch": bool(g.auto_switch), "keys_present": keys,
            "progress": res["progress"], "criteria": res["criteria"], "out_of_sample": res["out_of_sample"],
            "needs": needs, "last_event": self.stage.last_event(), "alert": self.stage.data.get("alert"),
            "events": list(reversed(self.stage.data.get("events") or []))[:10], "live_readiness": live,
        })

    def _after_step(self, session: TradingSession, now: datetime, new: list[dict]) -> None:
        """Called by the learning hub at the end of every session step."""
        if session is not self.run_session or self._switching:
            return
        if session.mode == "alpaca_paper":
            self._alpaca_safety(session, now)
        elif session.mode == "paper" and (new or not self._grad_checked):
            self._grad_checked = True
            self._graduation_check(now)

    def _graduation_check(self, now: datetime) -> None:
        res = self.graduation_eval()
        self.stage.mark_ready(res["pass"])
        if not (res["pass"] and self.stage.stage == "paper" and self.cfg.graduation.auto_switch):
            return
        if not self.alpaca_keys_present():
            return  # "ready": the dashboard shows what is needed
        evidence = {c["id"]: {"value": c["value"], "target": c["target"], "display": c["display_value"]} for c in res["criteria"]}
        msg = ("Graduated to Alpaca paper: every check passed on the built-in simulator, including "
               f"{res['out_of_sample']['trades']} fresh trades since the last learning change. Orders now go to your "
               "Alpaca PAPER (practice) account. No real money is used.")
        ev = self.stage.set("alpaca_paper", now, "graduated", msg, evidence)
        self.journal.record_event("stage_switch", msg, ev, now)
        log.warning("GRADUATION: switching the running session from paper to alpaca_paper")
        self._switch("alpaca_paper")

    def _alpaca_safety(self, session: TradingSession, now: datetime) -> None:
        if session.order_failures >= MAX_ORDER_FAILURES:
            self._fallback(now, f"{session.order_failures} orders in a row were rejected or failed at Alpaca.")
            return
        fn = getattr(session.broker, "reconcile", None)
        if fn is None:
            return
        try:
            diffs = fn()
        except Exception as exc:  # pragma: no cover
            log.warning("reconcile failed: %s", exc)
            diffs = None
        if diffs is None:
            return
        self._recon_strikes = self._recon_strikes + 1 if diffs else 0
        if self._recon_strikes >= RECONCILE_STRIKES:
            self._fallback(now, "Alpaca's positions disagree with the journal (" + "; ".join(diffs[:3]) + ").")

    def _fallback(self, now: datetime, reason: str) -> None:
        msg = f"Moved back to the built-in paper simulator: {reason} Check the Alpaca dashboard before trusting it again."
        ev = self.stage.set("paper", now, "fallback", msg, {"reason": reason}, alert=msg)
        self.journal.record_event("stage_fallback", msg, ev, now)
        log.error("ALPACA PAPER FALLBACK: %s", reason)
        self._switch("paper")

    def _switch(self, mode: str) -> None:
        """Stop the running session and start `mode`, from a separate thread (the caller is the session thread)."""
        self._switching = True

        def run() -> None:
            try:
                self.stop_session()
                try:
                    self.start_session(mode)
                except HttpError as exc:
                    if mode != "alpaca_paper":
                        raise
                    msg = f"Could not start Alpaca paper ({exc.message}); stayed on the built-in paper simulator."
                    ev = self.stage.set("paper", self.now(), "fallback", msg, {"reason": exc.message}, alert=msg)
                    self.journal.record_event("stage_fallback", msg, ev)
                    self.start_session("paper")
            except Exception as exc:  # never kill the server
                log.exception("stage switch failed")
                self._session_error = f"stage switch failed: {type(exc).__name__}: {exc}"[:300]
            finally:
                self._switching = False
                self._grad_checked = False

        self._switch_thread = threading.Thread(target=run, name="kwanttrades-stage-switch", daemon=True)
        self._switch_thread.start()

    # ------------------------------------------------------------------ shutdown
    def close(self) -> None:
        try:
            self.stop_session()
        except Exception:  # pragma: no cover
            pass

"""Backtester: drives `TradingSession.step` over historical sessions with a PaperBroker on ":memory:".

Honesty rules
-------------
* Strictly no look-ahead: provider, broker and session all read one ManualClock; the provider only
  reveals completed bars and previous-close vol levels.
* Option prices are model-generated from the underlying (Black-Scholes on a parametric surface)
  unless a ReplayProvider with recorded REAL snapshots is used. Results always say so in `caveats`.
* Real-bar backtests use 1-minute bars only (<= 7 sessions): the signal functions assume 1-minute
  bars (their completed-bar guard treats every bar as 1 minute long), so 5-minute input leaks the
  still-forming bar. Verified empirically; see README.
"""
from __future__ import annotations

import copy
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from kwanttrades.app.session import (
    EARLY_CLOSES,
    ManualClock,
    TradingSession,
    session_close_dt,
    session_open_dt,
)
from kwanttrades.backtest.metrics import compute_metrics
from kwanttrades.config import Config
from kwanttrades.data.recorder import ReplayProvider
from kwanttrades.data.synthetic import (
    INDEX_FOR,
    OPTION_CAVEAT,
    SIM_CAVEAT,
    SyntheticProvider,
    trading_days_before,
    trading_days_from,
)
from kwanttrades.execution.journal import Journal
from kwanttrades.execution.paper import PaperBroker
from kwanttrades.types import NY

log = logging.getLogger("kwanttrades.backtest")

SOURCES = ("simulated", "real_bars", "replay")
MAX_REAL_DAYS = 7


def _jsonable(o: Any) -> Any:
    from kwanttrades.signals._common import to_jsonable

    if isinstance(o, float) and math.isinf(o):
        return None
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    return to_jsonable(o)


@dataclass
class BacktestResult:
    source: str
    symbols: list[str]
    sessions: list[date]
    step_minutes: int
    starting_equity: float
    trades: list[dict]
    equity: list[tuple[datetime, float]]
    day_end_equity: list[float]
    metrics: dict
    caveats: list[str]
    decisions: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> dict:
        m = self.metrics
        return {k: m[k] for k in ("trades", "win_rate", "profit_factor", "expectancy", "sharpe", "max_drawdown", "total_pnl")}

    def to_dict(self) -> dict:
        return _jsonable({
            "source": self.source, "symbols": self.symbols, "sessions": [d.isoformat() for d in self.sessions],
            "step_minutes": self.step_minutes, "starting_equity": self.starting_equity,
            "summary": self.summary, "metrics": self.metrics, "trades": self.trades,
            "equity": [{"t": t.isoformat(), "equity": e} for t, e in self.equity],
            "by_strategy": self.metrics["by_strategy"], "by_exit": self.metrics["by_exit"],
            "caveats": self.caveats, "decisions": self.decisions, "errors": self.errors,
        })

    def format_text(self) -> str:
        m = self.metrics
        pf = "inf" if math.isinf(m["profit_factor"]) else f"{m['profit_factor']:.2f}"
        first = self.sessions[0] if self.sessions else "-"
        last = self.sessions[-1] if self.sessions else "-"
        lines = [
            f"Backtest ({self.source}): {', '.join(self.symbols)}  {first} .. {last}  "
            f"({len(self.sessions)} sessions, step {self.step_minutes} min)",
            f"  trades {m['trades']}  win rate {m['win_rate']:.0%}  avg win ${m['avg_win']:.2f}  avg loss ${m['avg_loss']:.2f}",
            f"  profit factor {pf}  expectancy ${m['expectancy']:.2f}/trade  total P&L ${m['total_pnl']:.2f}",
            f"  Sharpe (daily) {m['sharpe']:.2f}  max drawdown ${m['max_drawdown']:.2f} ({m['max_drawdown_pct']:.1%})",
        ]
        if m["by_strategy"]:
            lines.append("  by strategy: " + "; ".join(f"{b['strategy']} {b['trades']}x ${b['pnl']:.0f}" for b in m["by_strategy"]))
        if m["by_exit"]:
            lines.append("  by exit: " + "; ".join(f"{b['reason']} {b['trades']}x ${b['pnl']:.0f}" for b in m["by_exit"]))
        lines.append("CAVEATS:")
        lines += [f"  - {c}" for c in self.caveats]
        return "\n".join(lines)


def step_times(d: date, step_minutes: int, flatten_hhmm: str = "15:55") -> list[datetime]:
    """Session step schedule: 09:45 (opening range done) every `step_minutes`, the flatten time, the close."""
    close = session_close_dt(d)
    h, m = (int(x) for x in flatten_hhmm.split(":"))
    flat = datetime(d.year, d.month, d.day, h, m, tzinfo=NY)
    if d in EARLY_CLOSES:
        flat = min(flat, close - timedelta(minutes=5))
    out = []
    t = datetime(d.year, d.month, d.day, 9, 45, tzinfo=NY)
    while t < flat:
        out.append(t)
        t += timedelta(minutes=max(1, step_minutes))
    out += [flat, close]
    return out


class Backtester:
    def __init__(self, cfg: Config, engine_factory: Callable[[Config], Any] | None = None, step_minutes: int = 15,
                 fast: bool = True):
        self.cfg = copy.deepcopy(cfg)
        self.cfg.broker.mode = "paper"
        self.cfg.broker.db_path = ":memory:"
        self.step_minutes = int(step_minutes)
        self.fast = bool(fast)  # speed-ups: per-day caches, idle-symbol skipping, one expiry per bucket
        self.engine_factory = engine_factory or self._default_engine

    @staticmethod
    def _default_engine(cfg: Config):
        try:
            from kwanttrades.strategy.engine import StrategyEngine
        except ImportError as exc:
            raise RuntimeError("the strategy engine (kwanttrades/strategy/engine.py) is not available") from exc
        return StrategyEngine(cfg)

    # ------------------------------------------------------------------ inputs
    def _real_inputs(self, symbols: list[str], days: int, fetch: Any, caveats: list[str]):
        days_eff = min(int(days), MAX_REAL_DAYS)
        if days > MAX_REAL_DAYS:
            caveats.append(f"Requested {days} days but real 1-minute bars exist for only the last {MAX_REAL_DAYS}; "
                           f"using {MAX_REAL_DAYS}.")
        syms = list(dict.fromkeys(list(symbols) + ["SPY"]))
        bars, daily, iv = {}, {}, {}
        for s in syms:
            bars[s] = fetch.intraday_bars(s, "1m", MAX_REAL_DAYS)
            daily[s] = fetch.daily_bars(s, 2.0)
        for idx in ("^VIX", "^VIX3M"):
            try:
                daily[idx] = fetch.daily_bars(idx, 2.0)
            except Exception as exc:
                caveats.append(f"{idx} unavailable ({type(exc).__name__}); regime signal degrades to neutral.")
        for s in symbols:
            ix = INDEX_FOR.get(s)
            if ix:
                try:
                    df = daily[ix] if ix in daily else fetch.daily_bars(ix, 2.0)
                    daily.setdefault(ix, df)
                    iv[s] = df["close"].astype(float) / 100.0
                except Exception as exc:
                    caveats.append(f"{ix} unavailable for {s} ({type(exc).__name__}); using trailing realised vol x premium.")
        common = None
        for s in symbols:
            ds = {i.date() for i in bars[s].index}
            common = ds if common is None else common & ds
        today = datetime.now(NY)
        done = [d for d in sorted(common or []) if not (d == today.date() and today < session_close_dt(d))]
        sessions = done[-days_eff:]  # a session still in progress is never part of a backtest
        caveats.append("Underlying prices are real Yahoo 1-minute bars; 5-minute bars are not used because the signal "
                       "functions assume 1-minute bars and leak the forming bar otherwise.")
        caveats.append("ATM implied vol tracks a real volatility index where one exists (SPY ^VIX, QQQ ^VXN, IWM ^RVX), "
                       "otherwise trailing realised vol times a volatility risk premium of 1.15.")
        return bars, daily, iv, sessions

    # ------------------------------------------------------------------ run
    def run(self, symbols: list[str], days: int = 5, source: str = "simulated", seed: int = 7,
            fetch_provider: Any = None, replay_dir: str | None = None, news: Any = None,
            start_date: date | None = None,
            on_progress: Callable[[float], None] | None = None) -> BacktestResult:
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}; choose from {', '.join(SOURCES)}")
        symbols = [s.upper() for s in symbols]
        caveats: list[str] = []
        clock = ManualClock(datetime.now(NY))
        replay_times: dict[date, list[datetime]] | None = None
        if source == "simulated":
            sessions = (trading_days_from(start_date, max(1, int(days))) if start_date
                        else trading_days_before(date.today(), max(1, int(days))))
            provider = SyntheticProvider.simulated(symbols, n_days=len(sessions), start_date=sessions[0], seed=seed, clock=clock)
            caveats += [SIM_CAVEAT[0].upper() + SIM_CAVEAT[1:] + ".", OPTION_CAVEAT]
        else:
            if fetch_provider is None:
                from kwanttrades.data.yahoo import YahooProvider
                fetch_provider = YahooProvider()
            bars, daily, iv, sessions = self._real_inputs(symbols, days, fetch_provider, caveats)
            synth = SyntheticProvider(list(bars), bars=bars, daily=daily, iv_series=iv, seed=seed, clock=clock)
            if source == "real_bars":
                provider = synth
                caveats.append(OPTION_CAVEAT)
            else:
                if not replay_dir:
                    raise ValueError("source 'replay' needs replay_dir (recorded snapshots)")
                provider = ReplayProvider(replay_dir, clock=clock, bars_provider=synth)
                snap = provider.snapshot_times(symbols[0])
                replay_times = {}
                for t in snap:
                    replay_times.setdefault(t.date(), []).append(t)
                sessions = [d for d in sessions if d in replay_times]
                caveats.append(f"Option quotes are RECORDED REAL snapshots ({len(snap)} for {symbols[0]}); "
                               "fills are still modelled by the paper broker.")
        if not sessions:
            raise RuntimeError("no sessions available for this backtest (not enough data)")
        if self.fast:
            caveats.append("Speed mode: the engine sees the nearest directional expiry and the expiry closest to 30 DTE only, "
                           "and a symbol with no idea and an unchanged signal grade/opening-range state is not re-analysed "
                           "until either changes.")
        caveats.append(f"Exits are evaluated every {self.step_minutes} minutes (plus the 15:55 flatten and the close), "
                       "coarser than live polling; fills use the paper broker's slippage model.")

        clock.set(session_open_dt(sessions[0]) + timedelta(minutes=15))
        journal = Journal(":memory:")
        broker = PaperBroker(self.cfg.broker, journal, clock=clock)
        engine = self.engine_factory(self.cfg)
        sess = TradingSession(self.cfg, provider, broker, engine, journal, news=news, mode="paper", clock=clock,
                              analyze_when_blocked=False, data_name=getattr(provider, "name", "synthetic"),
                              max_workers=1, cache_enabled=self.fast, skip_idle=self.fast,
                              dir_expiries=1 if self.fast else 3, vol_expiries=1 if self.fast else 2)
        sess.universe = list(symbols)
        errors: list[str] = []
        total = sum(len(replay_times[d]) if replay_times is not None
                    else len(step_times(d, self.step_minutes, self.cfg.risk.flatten_time)) for d in sessions)
        done = 0
        last_t = None
        for d in sessions:
            if replay_times is not None:
                spaced, prev = [], None
                for t in replay_times[d]:
                    if t.time() < time(9, 45):
                        continue
                    if prev is None or (t - prev).total_seconds() >= self.step_minutes * 60:
                        spaced.append(t)
                        prev = t
                times = spaced + [session_close_dt(d)]
            else:
                times = step_times(d, self.step_minutes, self.cfg.risk.flatten_time)
            for t in times:
                clock.set(t)
                res = sess.step(t)
                last_t = t
                for e in res.errors:
                    if len(errors) < 50:
                        errors.append(f"{t:%m-%d %H:%M} {e.get('symbol')} {e.get('stage')}: {e.get('message')}")
                done += 1
                if on_progress:
                    on_progress(min(1.0, done / max(total, 1)))
        if last_t is not None:  # liquidate what is still open at the last mark
            clock.set(last_t)
            sess.flatten_all(last_t, "end_of_backtest")
            try:
                journal.record_equity(broker.account(), last_t)
            except Exception:  # pragma: no cover
                pass
        return self._result(source, symbols, sessions, journal, caveats, errors)

    def _result(self, source, symbols, sessions, journal: Journal, caveats: list[str], errors: list[str]) -> BacktestResult:
        start = float(self.cfg.broker.starting_equity)
        trades = [{
            "symbol": p.idea.symbol, "strategy": p.idea.strategy, "contracts": p.contracts,
            "opened": p.opened.isoformat(), "closed": p.closed.isoformat(), "entry_price": p.entry_price,
            "exit_price": p.exit_price, "pnl": p.realized_pnl, "fees": p.fees, "exit_reason": p.exit_reason,
        } for p in journal.closed_positions()]
        curve = journal.equity_curve()
        equity = [(ts.to_pydatetime(), float(v)) for ts, v in curve["equity"].items()]
        by_day: dict[date, float] = {}
        for t, v in equity:
            by_day[t.date()] = v
        day_end = [by_day[d] for d in sorted(by_day)]
        metrics = compute_metrics(trades, day_end, start, [v for _, v in equity])
        if metrics["trades"] < 30:
            caveats.append(f"Only {metrics['trades']} trades: far too few for any statistical conclusion.")
        decisions = [{"t": r["ts"].isoformat(), "symbol": r["symbol"], "strategy": r["strategy"],
                      "passed": r["decision"] == "PASS", "reasons": r["reasons"], "contracts": r["contracts"],
                      "score": r["idea"].score, "expected_value": r["idea"].expected_value,
                      "max_loss": r["idea"].max_loss, "pop": r["idea"].pop}
                     for r in journal.decisions(500)]
        return BacktestResult(source, symbols, list(sessions), self.step_minutes, start, trades, equity, day_end,
                              metrics, caveats, decisions, errors)

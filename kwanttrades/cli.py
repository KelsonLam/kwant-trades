"""Command line: python -m kwanttrades scan|run|shadow|backtest|record|report|status|web."""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from kwanttrades.app.session import (
    BROKER_MODES,
    ManualClock,
    StepResult,
    TradingSession,
    default_provider_name,
    effective_time,
    make_broker,
    make_provider,
    market_state,
    session_open_dt,
)
from kwanttrades.config import Config, load_config
from kwanttrades.types import NY, now_ny

log = logging.getLogger("kwanttrades.cli")
SECRET_ENV = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")
DEFAULT_LOG = "data/kwanttrades.log"


class CliError(Exception):
    """An expected, user-fixable problem: printed as one line, exit code 2."""


class _RedactFilter(logging.Filter):
    """Mask the value of any secret environment variable in log output."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            for k in SECRET_ENV:
                v = os.environ.get(k)
                if v and len(v) >= 6 and v in msg:
                    msg = msg.replace(v, "***")
            record.msg, record.args = msg, ()
        except Exception:  # pragma: no cover
            pass
        return True


def setup_logging(verbose: int = 0, log_file: str | None = DEFAULT_LOG) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_kwanttrades", False):
            root.removeHandler(h)
            h.close()
    root.setLevel(logging.DEBUG if verbose > 1 else logging.INFO)
    flt = _RedactFilter()
    con = logging.StreamHandler(sys.stderr)
    con.setLevel(logging.DEBUG if verbose > 1 else logging.INFO if verbose else logging.WARNING)
    handlers: list[logging.Handler] = [con]
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(logging.INFO)
            handlers.append(fh)
        except OSError as exc:
            print(f"warning: cannot write log file {log_file}: {exc}", file=sys.stderr)
    for h in handlers:
        h._kwanttrades = True  # type: ignore[attr-defined]
        h.addFilter(flt)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(h)
    for noisy in ("urllib3", "peewee", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # it logs routine 404s (e.g. ETFs have no earnings) as ERROR


# ------------------------------------------------------------------ helpers
def _out(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:  # pragma: no cover - legacy consoles
        print(text.encode("ascii", "replace").decode())


def _load_cfg(args) -> Config:
    path = Path(args.config)
    try:
        cfg = load_config(path)
    except tomllib.TOMLDecodeError as exc:
        raise CliError(f"cannot read {path}: {exc}") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise CliError(f"cannot load configuration {path}: {exc}") from exc
    if args.db:
        cfg.broker.db_path = args.db
    if cfg.broker.mode not in ("paper", "alpaca_paper", "alpaca_live"):
        raise CliError(f"config.toml: broker.mode must be paper, alpaca_paper or alpaca_live (got {cfg.broker.mode!r})")
    return cfg


def _symbols(args, cfg: Config) -> list[str]:
    if getattr(args, "symbols", None):
        raw = []
        for item in args.symbols:
            raw += [s for s in item.replace(",", " ").split() if s]
        syms = [s.upper() for s in raw]
        for s in syms:
            if not s.replace("^", "").replace(".", "").replace("-", "").isalnum() or len(s) > 10:
                raise CliError(f"bad symbol {s!r}")
        return list(dict.fromkeys(syms))
    return list(cfg.scan.universe)


def _engine(cfg: Config, factory: Callable[[Config], Any] | None):
    if factory is not None:
        return factory(cfg)
    try:
        from kwanttrades.strategy.engine import StrategyEngine
    except ImportError as exc:
        raise CliError("the strategy engine (kwanttrades/strategy/engine.py) is missing") from exc
    return StrategyEngine(cfg)


def _provider(args, symbols: list[str], clock=None):
    name = args.provider or default_provider_name()
    try:
        return make_provider(name, symbols=symbols, clock=clock), name
    except ValueError as exc:
        raise CliError(str(exc)) from exc


def _news(args, cfg: Config, news_factory):
    if getattr(args, "no_news", False) or not cfg.sentiment.lookback_hours:
        return None
    if getattr(args, "provider", None) == "synthetic":
        return None
    if news_factory is not None:
        return news_factory()
    from kwanttrades.data.news import CompositeNewsProvider

    return CompositeNewsProvider()


def _synthetic_clock(now: datetime) -> ManualClock:
    """Synthetic data is simulated, so scan it inside the entry window of the most recent session."""
    if market_state(now) in ("opening_range", "open"):
        return ManualClock(now)
    return ManualClock(effective_time(now).replace(hour=11, minute=0))


# ------------------------------------------------------------------ formatting
def _legs_text(idea) -> str:
    parts = [f"{'+' if l.side.value == 'buy' else '-'}{l.ratio}{l.quote.right.value}{l.quote.strike:g}" for l in idea.legs]
    return " ".join(parts) + f" {idea.expiry:%m/%d}"


def _fmt_pct(x: float | None) -> str:
    return "  n/a" if x is None or not math.isfinite(x) else f"{x * 100:5.1f}%"


def format_scan(res: StepResult, source: str, top: int = 15) -> str:
    L: list[str] = []
    L.append("== Signals and volatility ==")
    L.append(f"{'SYMBOL':7}{'SPOT':>9}{'CHG':>8}  {'COMPOSITE':>11}  {'ORB':9}{'VWAP':>6}{'GAP':>6}{'NEWS':>6}  {'ATM IV':>7}{'FCST RV':>8}{'EDGE':>7}")
    for rec in res.symbols.values():
        if rec.error and rec.composite is None:
            L.append(f"{rec.symbol:7} unavailable: {rec.error[:90]}")
            continue
        comp = rec.composite
        c = rec.components

        def d(k: str) -> str:
            s = c.get(k)
            return "  n/a" if s is None else f"{s.direction:+.2f}"

        orb = c.get("orb")
        vv = rec.vol_view
        spot = "" if rec.spot is None else f"{rec.spot:9.2f}"
        chg = "" if rec.change_pct is None else f"{rec.change_pct * 100:+7.2f}%"
        compt = "" if comp is None else f"{comp.direction:+.2f}/{comp.confidence:.2f}"
        L.append(f"{rec.symbol:7}{spot:>9}{chg:>8}  {compt:>11}  {str(orb.detail.get('state', '-') if orb else '-'):9}"
                 f"{d('vwap'):>6}{d('gap'):>6}{d('news'):>6}  "
                 f"{_fmt_pct(getattr(vv, 'atm_iv', None)):>7}{_fmt_pct(getattr(vv, 'forecast_rv', None)):>8}"
                 f"{('  n/a' if vv is None else f'{vv.vol_edge:+.2f}'):>7}")
    reg = res.regime
    if reg is not None:
        L.append(f"Regime: direction {reg.direction:+.2f}, confidence {reg.confidence:.2f}, "
                 f"risk multiplier {reg.detail.get('risk_multiplier', 1.0):.2f}")
    L.append("")
    L.append("== Ranked ideas ==")
    ideas = sorted(res.ideas, key=lambda r: r.idea.score, reverse=True)[:top]
    if not ideas:
        L.append("No ideas passed the strategy filters (no positive-EV structure with enough confidence).")
    else:
        L.append(f"{'#':>2} {'SYM':6}{'STRATEGY':17}{'LEGS':34}{'NET':>9}{'MAXLOSS':>9}{'POP':>5}{'EV':>8}{'SCORE':>7}  GATE")
        for i, r in enumerate(ideas, 1):
            idea = r.idea
            net = f"{abs(idea.net_price):.2f}{'cr' if idea.is_credit else 'db'}"
            gate = "PASS" if r.decision.passed else "BLOCK: " + (r.decision.reasons[0].replace("BLOCK: ", "")[:70] if r.decision.reasons else "")
            L.append(f"{i:>2} {idea.symbol:6}{idea.strategy:17}{_legs_text(idea)[:33]:34}{net:>9}{idea.max_loss:>9.0f}"
                     f"{idea.pop * 100:>4.0f}%{idea.expected_value:>8.1f}{idea.score:>7.3f}  {gate}")
        L.append("")
        for i, r in enumerate(ideas[:5], 1):
            L.append(f"  #{i} {r.idea.symbol} {r.idea.strategy} (x{r.contracts}):")
            for line in r.idea.rationale[:6]:
                L.append(f"      - {line}")
    L.append("")
    for e in res.errors:
        L.append(f"note: {e.get('symbol') or 'session'} {e.get('stage')}: {e.get('message')}"[:200])
    L.append(f"Data source: {source}. Quotes from free feeds are delayed (typically ~15 min). "
             + ("" if res.market_state in ("open", "opening_range") else
                f"Market is {res.market_state}: quotes are STALE (last session close {res.data_now:%Y-%m-%d %H:%M} NY)."))
    L.append("Scan only: nothing was traded. Not financial advice.")
    return "\n".join(L)


# ------------------------------------------------------------------ commands
def cmd_scan(args, cfg: Config, engine_factory, news_factory) -> int:
    from kwanttrades.execution.journal import Journal

    syms = _symbols(args, cfg)
    now = now_ny()
    clock = _synthetic_clock(now) if args.provider == "synthetic" else None
    provider, name = _provider(args, syms, clock)
    now = clock.now if clock else now
    journal = Journal(":memory:")
    session = TradingSession(cfg, provider, None, _engine(cfg, engine_factory), journal, news=_news(args, cfg, news_factory),
                             mode="scan", clock=clock, data_name=name)
    res = session.step(now, symbols=syms)
    if args.json:
        _out(json.dumps(session.snapshot(), indent=1))
    else:
        _out(format_scan(res, name, args.top))
    return 0


def _live_banner(mode: str) -> None:
    if mode == "alpaca_live":
        _out("!" * 72)
        _out("LIVE TRADING: real money is at risk. Orders go to your Alpaca live account.")
        _out("!" * 72)


def cmd_session(args, cfg: Config, engine_factory, news_factory, shadow: bool) -> int:
    from kwanttrades.execution.journal import Journal

    syms = _symbols(args, cfg)
    provider, name = _provider(args, syms, _synthetic_clock(now_ny()) if args.provider == "synthetic" else None)
    clock = getattr(provider, "_clock", None) if args.provider == "synthetic" else None
    journal = Journal(cfg.broker.db_path)
    mode = "shadow" if shadow else cfg.broker.mode
    broker = None
    if mode in BROKER_MODES:
        try:
            broker = make_broker(cfg, journal, clock)
        except Exception as exc:
            raise CliError(f"cannot start {mode}: {exc}") from exc
    _live_banner(mode)
    session = TradingSession(cfg, provider, broker, _engine(cfg, engine_factory), journal, news=_news(args, cfg, news_factory),
                             mode=mode, clock=clock, data_name=name)
    session.universe = syms
    _out(f"Kwant Trades {mode} session: {len(syms)} symbols, data source {name}. Press Ctrl+C to stop.")
    try:
        if args.once:
            res = session.step(clock.now if clock else now_ny())
            _out(format_scan(res, name, 10))
            if res.orders:
                for o in res.orders:
                    _out(f"order: {o['symbol']} {o['strategy']} x{o['contracts']} limit {o['limit']:.2f} -> {o['status']}")
            if res.exits:
                for x in res.exits:
                    _out(f"exit: {x['symbol']} {x['strategy']} ({x['reason']}) -> {x['status']}")
        else:
            session.run()
    except KeyboardInterrupt:
        _out("Stopped.")
    return 0


def cmd_backtest(args, cfg: Config, engine_factory, news_factory) -> int:
    from kwanttrades.backtest import Backtester
    from kwanttrades.report import report_from_backtest

    syms = _symbols(args, cfg) if args.symbols else ["SPY"]
    source = {"real": "real_bars"}.get(args.source, args.source)
    bt = Backtester(cfg, engine_factory=(lambda c: _engine(c, engine_factory)), step_minutes=args.step_minutes)
    _out(f"Running {source} backtest on {', '.join(syms)} ({args.days} days, step {args.step_minutes} min); this can take a few minutes...")
    try:
        res = bt.run(syms, days=args.days, source=source, seed=args.seed, replay_dir=args.replay_dir)
    except (RuntimeError, ValueError) as exc:
        raise CliError(f"backtest failed: {exc}") from exc
    _out(res.format_text())
    if args.json:
        Path(args.json).write_text(json.dumps(res.to_dict(), indent=1), encoding="utf-8")
        _out(f"Wrote {args.json}")
    if args.report:
        Path(args.report).write_text(report_from_backtest(res), encoding="utf-8")
        _out(f"Wrote {args.report}")
    return 0


def cmd_record(args, cfg: Config, engine_factory, news_factory) -> int:
    import time as _t

    from kwanttrades.data.recorder import Recorder

    syms = _symbols(args, cfg)
    provider, name = _provider(args, syms)
    rec = Recorder(provider, args.out, args.max_dte)
    n = 0
    while True:
        files, errors = rec.record_once(syms)
        n += 1
        _out(f"[{now_ny():%H:%M:%S}] recorded {len(files)} snapshot(s) to {args.out}" +
             (f"; failed: {', '.join(f'{s} ({e})' for s, e in errors.items())}" if errors else ""))
        if not args.every or (args.count and n >= args.count):
            break
        try:
            _t.sleep(args.every)
        except KeyboardInterrupt:
            break
    return 0 if n else 1


def cmd_report(args, cfg: Config, engine_factory, news_factory) -> int:
    from kwanttrades.execution.journal import Journal
    from kwanttrades.report import report_from_journal

    if not Path(cfg.broker.db_path).exists():
        raise CliError(f"no journal at {cfg.broker.db_path}: run a paper or shadow session first")
    html = report_from_journal(Journal(cfg.broker.db_path), cfg.broker.starting_equity)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    _out(f"Wrote {out}")
    return 0


def cmd_status(args, cfg: Config, engine_factory, news_factory) -> int:
    from kwanttrades.execution.journal import Journal

    now = now_ny()
    _out(f"Mode: {cfg.broker.mode}   Journal: {cfg.broker.db_path}")
    if cfg.broker.mode == "paper" and not Path(cfg.broker.db_path).exists():
        _out(f"No journal yet: nothing traded. Starting equity ${cfg.broker.starting_equity:,.2f}.")
        return 0
    journal = Journal(cfg.broker.db_path)
    try:
        broker = make_broker(cfg, journal)
        acct = broker.account()
        positions = [p for p in broker.positions() if p.is_open]
    except Exception as exc:
        raise CliError(f"cannot read account: {exc}") from exc
    _out(f"Equity ${acct.equity:,.2f}  cash ${acct.cash:,.2f}  buying power ${acct.buying_power:,.2f}  open risk ${acct.open_risk:,.2f}")
    _out(f"Today's P&L: ${acct.day_pnl:+,.2f}")
    kd = journal.get_value("kill_switch_day")
    killed = kd == now.date().isoformat()
    _out("Kill switch: " + (f"ACTIVE since {kd} (no new entries until the next session)" if killed else "off"))
    _out(f"Trades today: {journal.trades_today(now)} (max {cfg.risk.max_trades_per_day})")
    if not positions:
        _out("Open positions: none")
    else:
        _out("Open positions:")
        for p in positions:
            mark = "n/a" if p.mark is None else f"{p.mark:.2f}"
            _out(f"  {p.idea.symbol:6}{p.idea.strategy:18}x{p.contracts:<3} entry {p.entry_price:7.2f} mark {mark:>7} "
                 f"uPnL ${p.unrealized_pnl:+8.2f} max loss ${p.idea.max_loss * p.contracts:,.0f} expiry {p.idea.expiry}")
    return 0


def cmd_web(args, cfg: Config, engine_factory, news_factory) -> int:
    try:
        from kwanttrades.web import server
    except ImportError as exc:
        raise CliError("the web dashboard is not installed yet (kwanttrades/web/server.py missing)") from exc
    fn = getattr(server, "main", None)
    if fn is None:
        raise CliError("kwanttrades.web.server has no main(); the dashboard is still being built")
    argv = ["--port", str(args.port), "--config", str(args.config)]
    if getattr(args, "provider", None):
        argv += ["--provider", args.provider]
    if getattr(args, "no_browser", False):
        argv.append("--no-browser")
    if getattr(args, "db", None):
        argv += ["--db", args.db]
    if getattr(args, "remote", False):
        argv.append("--remote")
    if getattr(args, "host", None):
        argv += ["--host", args.host]
    if getattr(args, "behind_proxy", False):
        argv.append("--behind-proxy")
    for name in getattr(args, "allowed_host", None) or []:
        argv += ["--allowed-host", name]
    return int(fn(argv) or 0)


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m kwanttrades",
        description="Kwant Trades: scan option chains for mispriced options and trade them through a risk gate. "
                    "Default mode is the built-in paper simulator. Not financial advice.")
    p.add_argument("--config", default="config.toml", help="config file (default config.toml; defaults used if missing)")
    p.add_argument("--db", help="journal database path (overrides broker.db_path)")
    p.add_argument("--log-file", default=DEFAULT_LOG, help=f"log file (default {DEFAULT_LOG})")
    p.add_argument("-v", "--verbose", action="count", default=0, help="more console logging (-vv for debug)")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def common(sp, provider=True, symbols=True):
        if provider:
            sp.add_argument("--provider", choices=["yahoo", "openbb", "crosscheck", "synthetic"],
                            help="market data source (default: crosscheck if OpenBB is installed, else yahoo; "
                                 "synthetic = simulated test data)")
        if symbols:
            sp.add_argument("--symbols", nargs="+", metavar="SYM", help="symbols (space or comma separated); default: config universe")

    s = sub.add_parser("scan", help="rank trade ideas now (safe at any time; never trades)")
    common(s)
    s.add_argument("--top", type=int, default=15, help="rows in the ranked table (default 15)")
    s.add_argument("--no-news", action="store_true", help="skip the news sentiment signal")
    s.add_argument("--json", action="store_true", help="print the full result as JSON instead of the table")
    s.set_defaults(func=lambda a, c, e, n: cmd_scan(a, c, e, n))

    s = sub.add_parser("run", help="trading loop in the mode set by config.toml (paper by default)")
    common(s)
    s.add_argument("--once", action="store_true", help="run a single cycle and exit")
    s.add_argument("--no-news", action="store_true", help="skip the news sentiment signal")
    s.set_defaults(func=lambda a, c, e, n: cmd_session(a, c, e, n, False))

    s = sub.add_parser("shadow", help="like run, but only journal the orders that would be sent")
    common(s)
    s.add_argument("--once", action="store_true", help="run a single cycle and exit")
    s.add_argument("--no-news", action="store_true", help="skip the news sentiment signal")
    s.set_defaults(func=lambda a, c, e, n: cmd_session(a, c, e, n, True))

    s = sub.add_parser("backtest", help="replay the strategy over past sessions (option prices are synthetic unless replaying recordings)")
    s.add_argument("--source", choices=["simulated", "real_bars", "real", "replay"], default="simulated",
                   help="simulated = seeded fake prices (plumbing test only); real_bars = real 1-minute underlying "
                        "(last 7 sessions) with model-priced options; replay = recorded real chains")
    s.add_argument("--days", type=int, default=5, help="sessions (real_bars max 7)")
    s.add_argument("--symbols", nargs="+", metavar="SYM", help="symbols (default SPY)")
    s.add_argument("--step-minutes", type=int, default=30, help="minutes between decisions (default 30)")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--replay-dir", default="data/snapshots", help="snapshot folder for --source replay")
    s.add_argument("--json", metavar="FILE", help="also write the result as JSON")
    s.add_argument("--report", metavar="FILE", help="also write an HTML report")
    s.set_defaults(func=lambda a, c, e, n: cmd_backtest(a, c, e, n))

    s = sub.add_parser("record", help="snapshot real option chains to data/snapshots for honest later backtests")
    common(s)
    s.add_argument("--out", default="data/snapshots", help="snapshot folder")
    s.add_argument("--max-dte", type=int, default=45)
    s.add_argument("--every", type=int, default=0, metavar="SECONDS", help="repeat every N seconds (default: once)")
    s.add_argument("--count", type=int, default=0, help="stop after N rounds when --every is used")
    s.set_defaults(func=lambda a, c, e, n: cmd_record(a, c, e, n))

    s = sub.add_parser("report", help="write a self-contained HTML report from the journal")
    s.add_argument("--out", default="data/report.html", help="output file (default data/report.html)")
    s.set_defaults(func=lambda a, c, e, n: cmd_report(a, c, e, n))

    s = sub.add_parser("status", help="account, open positions, today's P&L and kill-switch state")
    s.set_defaults(func=lambda a, c, e, n: cmd_status(a, c, e, n))

    s = sub.add_parser("web", help="start the dashboard (127.0.0.1 only unless --remote)")
    common(s, symbols=False)
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--no-browser", action="store_true", help="do not open the default browser")
    s.add_argument("--remote", action="store_true",
                   help="opt-in: password login (KWANT_WEB_PASSWORD in .env) and listen on your network, e.g. for your phone")
    s.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], help="bind address (0.0.0.0 needs --remote)")
    s.add_argument("--behind-proxy", action="store_true", help="trust X-Forwarded-* headers from a reverse proxy (remote mode)")
    s.add_argument("--allowed-host", action="append", default=[], metavar="NAME",
                   help="extra Host name accepted in remote mode, e.g. a VPN/tunnel name (repeatable)")
    s.set_defaults(func=lambda a, c, e, n: cmd_web(a, c, e, n))
    return p


def main(argv: list[str] | None = None, *, engine_factory: Callable[[Config], Any] | None = None,
         news_factory: Callable[[], Any] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    setup_logging(args.verbose, args.log_file)
    try:
        cfg = _load_cfg(args)
        return int(args.func(args, cfg, engine_factory, news_factory) or 0)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        log.exception("unexpected failure")
        print(f"error: {type(exc).__name__}: {exc} (details in {args.log_file})", file=sys.stderr)
        return 1

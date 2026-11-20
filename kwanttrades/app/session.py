"""The trading-session loop: signals -> strategy engine -> sizing -> risk gate -> broker.

Everything time-related comes from the `now` argument of `TradingSession.step` (or an injectable
clock), never from the wall clock directly, so the backtester can drive the very same code.

Modes
-----
scan          rank ideas, never construct or touch a broker
shadow        journal the orders that WOULD be sent (marked "shadow"), send nothing
paper         built-in PaperBroker
alpaca_paper  AlpacaBroker against Alpaca's paper endpoint
alpaca_live   AlpacaBroker live (only through AlpacaBroker's own environment gate)
"""
from __future__ import annotations

import copy
import dataclasses
import logging
import math
import threading
import time as _time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

import numpy as np

from kwanttrades.config import Config, RiskConfig
from kwanttrades.execution.exits import evaluate_exit, is_intraday, parse_hhmm, structure_mark
from kwanttrades.execution.journal import Journal
from kwanttrades.execution.risk import RiskDecision, RiskManager
from kwanttrades.execution.sizing import size_position
from kwanttrades.quant.distribution import payoff_at_expiry
from kwanttrades.signals import combine, gap_signal, orb_signal, regime_signal, vwap_signal
from kwanttrades.signals._common import clean_daily, session_bars, to_jsonable, visible_bars
from kwanttrades.types import (
    MULTIPLIER,
    NY,
    AccountState,
    DirectionalSignal,
    OptionChain,
    Order,
    OrderStatus,
    Position,
    Right,
    Side,
    TradeIdea,
    now_ny,
)

log = logging.getLogger("kwanttrades.session")

MODES = ("scan", "shadow", "paper", "alpaca_paper", "alpaca_live")
BROKER_MODES = ("paper", "alpaca_paper", "alpaca_live")
AUTO_FLATTEN_MODES = ("paper", "alpaca_paper")  # kill switch closes positions only in paper modes

# --------------------------------------------------------------------------- market calendar
# NYSE full-day closures and 13:00 early closes for 2026-2027 (static list; weekends are implicit).
HOLIDAYS: dict[date, str] = {
    date(2026, 1, 1): "New Year's Day", date(2026, 1, 19): "Martin Luther King Jr. Day",
    date(2026, 2, 16): "Washington's Birthday", date(2026, 4, 3): "Good Friday",
    date(2026, 5, 25): "Memorial Day", date(2026, 6, 19): "Juneteenth",
    date(2026, 7, 3): "Independence Day (observed)", date(2026, 9, 7): "Labor Day",
    date(2026, 11, 26): "Thanksgiving Day", date(2026, 12, 25): "Christmas Day",
    date(2027, 1, 1): "New Year's Day", date(2027, 1, 18): "Martin Luther King Jr. Day",
    date(2027, 2, 15): "Washington's Birthday", date(2027, 3, 26): "Good Friday",
    date(2027, 5, 31): "Memorial Day", date(2027, 6, 18): "Juneteenth (observed)",
    date(2027, 7, 5): "Independence Day (observed)", date(2027, 9, 6): "Labor Day",
    date(2027, 11, 25): "Thanksgiving Day", date(2027, 12, 24): "Christmas Day (observed)",
}
EARLY_CLOSES: dict[date, str] = {
    date(2026, 11, 27): "day after Thanksgiving", date(2026, 12, 24): "Christmas Eve",
    date(2027, 11, 26): "day after Thanksgiving",
}
OPEN_TIME = time(9, 30)
OPENING_RANGE_END = time(9, 45)
NORMAL_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def session_close_time(d: date) -> time:
    return EARLY_CLOSE if d in EARLY_CLOSES else NORMAL_CLOSE


def session_open_dt(d: date) -> datetime:
    return datetime.combine(d, OPEN_TIME, tzinfo=NY)


def session_close_dt(d: date) -> datetime:
    return datetime.combine(d, session_close_time(d), tzinfo=NY)


def prev_trading_day(d: date) -> date:
    """The trading day strictly before `d`."""
    d = d - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(d: date) -> date:
    """The trading day strictly after `d`."""
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def market_state(now: datetime) -> str:
    """"pre" (trading day before the open), "opening_range" (09:30-09:45), "open", or "closed"
    (after the close, weekends, holidays)."""
    now = now.astimezone(NY)
    d = now.date()
    if not is_trading_day(d):
        return "closed"
    t = now.time()
    if t < OPEN_TIME:
        return "pre"
    if t < OPENING_RANGE_END:
        return "opening_range"
    if t < session_close_time(d):
        return "open"
    return "closed"


def market_is_open(now: datetime) -> bool:
    return market_state(now) in ("opening_range", "open")


def effective_time(now: datetime) -> datetime:
    """The moment the data is "as of": `now` while the market is open, otherwise the close of the most
    recent session (so a weekend scan analyses Friday's session)."""
    now = now.astimezone(NY)
    state = market_state(now)
    if state in ("opening_range", "open"):
        return now
    d = now.date()
    if state == "closed" and is_trading_day(d):
        return session_close_dt(d)
    return session_close_dt(prev_trading_day(d) if is_trading_day(d) else _last_trading_day_before(d))


def _last_trading_day_before(d: date) -> date:
    return prev_trading_day(d)


def next_open(now: datetime) -> datetime:
    now = now.astimezone(NY)
    d = now.date()
    if is_trading_day(d) and now.time() < OPEN_TIME:
        return session_open_dt(d)
    return session_open_dt(next_trading_day(d))


def next_close(now: datetime) -> datetime:
    """Close of the current session if one is running or still to come today, else the next session's."""
    now = now.astimezone(NY)
    d = now.date()
    if is_trading_day(d) and now < session_close_dt(d):
        return session_close_dt(d)
    return session_close_dt(next_trading_day(d))


def opening_range_end(now: datetime) -> datetime | None:
    d = now.astimezone(NY).date()
    return datetime.combine(d, OPENING_RANGE_END, tzinfo=NY) if is_trading_day(d) else None


class ManualClock:
    """A controllable clock: call it for the current time, `set()` to move it."""

    def __init__(self, start: datetime | None = None):
        self._now = (start or now_ny()).astimezone(NY)

    def __call__(self) -> datetime:
        return self._now

    @property
    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when.astimezone(NY)


# --------------------------------------------------------------------------- result records
def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _iso(d: datetime | date | None) -> str | None:
    return None if d is None else d.isoformat()


def signal_dict(sig: DirectionalSignal | None) -> dict | None:
    if sig is None:
        return None
    return {
        "direction": _num(sig.direction) or 0.0, "confidence": _num(sig.confidence) or 0.0,
        "detail": to_jsonable(sig.detail),
    }


@dataclass
class SymbolRecord:
    symbol: str
    spot: float | None = None
    change_pct: float | None = None
    composite: DirectionalSignal | None = None
    components: dict[str, DirectionalSignal] = field(default_factory=dict)
    vol_view: Any = None
    mispricings: list[dict] = field(default_factory=list)
    sparkline: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    n_ideas: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        comp = self.composite
        orb = self.components.get("orb")
        orb_d = None
        if orb is not None:
            d = orb.detail
            orb_d = {k: to_jsonable(d.get(k)) for k in
                     ("state", "or_high", "or_low", "or_mid", "entry", "stop", "target", "rel_volume")}
        vv = self.vol_view
        vol = None
        if vv is not None:
            vol = {"atm_iv": _num(getattr(vv, "atm_iv", None)), "forecast_rv": _num(getattr(vv, "forecast_rv", None)),
                   "vol_edge": _num(getattr(vv, "vol_edge", None)),
                   "components": {k: _num(v) for k, v in (getattr(vv, "components", {}) or {}).items()}}
        return {
            "symbol": self.symbol, "spot": self.spot, "change_pct": self.change_pct,
            "composite": None if comp is None else {
                "direction": _num(comp.direction) or 0.0, "confidence": _num(comp.confidence) or 0.0,
                "agreement": _num(comp.detail.get("agreement")) or 0.0},
            "components": {k: signal_dict(self.components.get(k)) for k in ("orb", "vwap", "gap", "news", "regime")},
            "orb": orb_d, "vol": vol, "sparkline": list(self.sparkline), "mispricings": self.mispricings,
            "notes": list(self.notes), "n_ideas": self.n_ideas, "error": self.error,
        }


@dataclass
class IdeaRecord:
    idea: TradeIdea
    contracts: int
    decision: RiskDecision
    spot: float | None = None
    flags: dict[str, str] = field(default_factory=dict)  # occ -> "cheap" | "rich"
    action: str = "none"  # "sent" | "shadow" | "blocked" | "send_failed"
    order_id: str | None = None
    order_status: str | None = None

    def to_dict(self) -> dict:
        return idea_dict(self.idea, self.contracts, self.decision, self.spot, self.flags,
                         action=self.action, order_id=self.order_id, order_status=self.order_status)


@dataclass
class StepResult:
    now: datetime
    mode: str
    market_state: str = "closed"
    data_now: datetime | None = None
    regime: DirectionalSignal | None = None
    symbols: dict[str, SymbolRecord] = field(default_factory=dict)
    ideas: list[IdeaRecord] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    exits: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    kill_switch: bool = False
    kill_reason: str | None = None
    account: dict | None = None
    notes: list[str] = field(default_factory=list)
    trades_today: int = 0
    settled: list[dict] = field(default_factory=list)

    @property
    def passed_ideas(self) -> list[IdeaRecord]:
        return [r for r in self.ideas if r.decision.passed]

    def to_dict(self) -> dict:
        reg = self.regime
        return to_jsonable({
            "mode": self.mode, "asof": self.now.isoformat(), "data_asof": _iso(self.data_now),
            "market_state": self.market_state,
            "regime": None if reg is None else {**signal_dict(reg), "risk_multiplier": _num(reg.detail.get("risk_multiplier")) or 1.0},
            "symbols": [s.to_dict() for s in self.symbols.values()],
            "ideas": [r.to_dict() for r in self.ideas],
            "orders": self.orders, "exits": self.exits, "settled": self.settled, "errors": self.errors,
            "kill_switch": self.kill_switch, "kill_reason": self.kill_reason, "account": self.account,
            "notes": self.notes, "trades_today": self.trades_today,
        })


def leg_dict(leg, flags: dict[str, str] | None = None) -> dict:
    q = leg.quote
    return {
        "occ": q.occ_symbol, "side": leg.side.value, "ratio": int(leg.ratio), "right": q.right.value,
        "strike": float(q.strike), "expiry": q.expiry.isoformat(), "bid": float(q.bid), "ask": float(q.ask),
        "mid": float(q.mid), "flag": (flags or {}).get(q.occ_symbol),
    }


STRATEGY_LABELS = {
    "long_call": "Long call", "long_put": "Long put", "bull_call_spread": "Bull call spread",
    "bear_put_spread": "Bear put spread", "bull_put_spread": "Bull put spread",
    "bear_call_spread": "Bear call spread", "iron_condor": "Iron condor", "long_straddle": "Long straddle",
    "long_strangle": "Long strangle",
}


def strategy_label(strategy: str) -> str:
    return STRATEGY_LABELS.get(strategy, strategy.replace("_", " ").capitalize())


def idea_id(idea: TradeIdea) -> str:
    strikes = "-".join(f"{l.quote.strike:g}" for l in idea.legs)
    return f"{idea.symbol}-{idea.strategy}-{idea.expiry:%y%m%d}-{strikes}"


def payoff_curve(idea: TradeIdea, spot: float | None, n: int = 61) -> dict:
    """P&L per contract-set at expiry on a price grid (premium included)."""
    strikes = [l.quote.strike for l in idea.legs]
    ref = spot if spot and spot > 0 else float(np.mean(strikes))
    lo = min(min(strikes), ref * 0.92) * 0.98
    hi = max(max(strikes), ref * 1.08) * 1.02
    prices = np.linspace(lo, hi, n)
    pnl = (payoff_at_expiry(idea.legs, prices) - idea.net_price) * MULTIPLIER
    return {"prices": [round(float(p), 4) for p in prices], "pnl": [round(float(v), 2) for v in pnl]}


def _breakevens(idea: TradeIdea, spot: float | None) -> list[float]:
    pc = payoff_curve(idea, spot, 401)
    xs, ys = pc["prices"], pc["pnl"]
    out = []
    for i in range(len(xs) - 1):
        if ys[i] == 0 or (ys[i] < 0) != (ys[i + 1] < 0):
            t = 0.0 if ys[i + 1] == ys[i] else -ys[i] / (ys[i + 1] - ys[i])
            out.append(round(xs[i] + t * (xs[i + 1] - xs[i]), 2))
    return sorted(set(out))


def idea_dict(idea: TradeIdea, contracts: int, decision: RiskDecision | None, spot: float | None,
              flags: dict[str, str] | None = None, **extra: Any) -> dict:
    mp = idea.max_profit
    d = {
        "id": idea_id(idea), "symbol": idea.symbol, "strategy": idea.strategy,
        "strategy_label": strategy_label(idea.strategy),
        "legs": [leg_dict(l, flags) for l in idea.legs],
        "net_price": float(idea.net_price), "entry_limit": float(idea.entry_limit),
        "max_loss": float(idea.max_loss), "max_profit": None if (mp is None or math.isinf(mp)) else float(mp),
        "pop": float(idea.pop), "expected_value": float(idea.expected_value), "score": float(idea.score),
        "contracts": int(contracts), "rationale": list(idea.rationale),
        "greeks": {k: _num(v) for k, v in idea.greeks.items()},
        "breakevens": _breakevens(idea, spot),
        "payoff": payoff_curve(idea, spot), "spot": spot, "intraday": bool(is_intraday(idea)),
        "decision": None if decision is None else {"passed": bool(decision.passed), "reasons": list(decision.reasons)},
    }
    d.update(extra)
    return d


def position_dict(pos: Position) -> dict:
    idea = pos.idea
    per_set = ((pos.mark - pos.entry_price) * MULTIPLIER) if (pos.mark is not None and pos.is_open) else 0.0
    if math.isinf(idea.max_profit):
        target = idea.take_profit_pct * max(pos.entry_price, 0.0) * MULTIPLIER
    else:
        target = idea.take_profit_pct * idea.max_profit
    stop = idea.stop_loss_pct * idea.max_loss
    return to_jsonable({
        "id": pos.id, "symbol": idea.symbol, "strategy": idea.strategy, "strategy_label": strategy_label(idea.strategy),
        "contracts": pos.contracts, "entry_price": pos.entry_price, "mark": pos.mark,
        "unrealized_pnl": pos.unrealized_pnl, "realized_pnl": pos.realized_pnl, "max_loss": idea.max_loss,
        "max_profit": None if math.isinf(idea.max_profit) else idea.max_profit,
        "opened": pos.opened.isoformat(), "closed": _iso(pos.closed), "exit_reason": pos.exit_reason,
        "legs": [leg_dict(l) for l in idea.legs], "expiry": idea.expiry.isoformat(),
        "progress_to_target": max(0.0, per_set / target) if target > 0 else 0.0,
        "progress_to_stop": max(0.0, -per_set / stop) if stop > 0 else 0.0,
    })


# --------------------------------------------------------------------------- helpers
def find_attr(obj: Any, name: str, _depth: int = 0) -> Callable | None:
    """Locate a callable attribute on a provider or on one it wraps (primary/fallback/secondary)."""
    fn = getattr(obj, name, None)
    if callable(fn):
        return fn
    if _depth < 3:
        for inner in ("primary", "fallback", "secondary", "base"):
            sub = getattr(obj, inner, None)
            if sub is not None:
                fn = find_attr(sub, name, _depth + 1)
                if fn is not None:
                    return fn
    return None


def structure_quotes_value(pos: Position, chain: OptionChain) -> tuple[float, float] | None:
    """(mid value, summed half-spread) of the structure in `chain`, or None if a leg is missing."""
    mid = hs = 0.0
    for l in pos.idea.legs:
        q = chain.find(l.quote.expiry, l.quote.strike, l.quote.right)
        if q is None:
            return None
        sign = 1.0 if l.side == Side.BUY else -1.0
        mid += sign * q.mid * l.ratio
        hs += 0.5 * q.spread * l.ratio
    return mid, hs


# --------------------------------------------------------------------------- the session
class TradingSession:
    def __init__(self, cfg: Config, data: Any, broker: Any, engine: Any, journal: Journal, news: Any = None,
                 mode: str = "paper", clock: Callable[[], datetime] | None = None,
                 analyze_when_blocked: bool = True, data_name: str | None = None,
                 max_workers: int | None = None, cache_enabled: bool = True, skip_idle: bool = False,
                 dir_expiries: int = 3, vol_expiries: int = 2):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; choose one of {', '.join(MODES)}")
        if mode in BROKER_MODES and broker is None:
            raise ValueError(f"mode {mode!r} needs a broker")
        self.cfg = cfg
        self.data = data
        self.engine = engine
        self.journal = journal
        self.news = news
        self.mode = mode
        # scan and shadow never touch a broker, even if one was handed in
        self.broker = broker if mode in BROKER_MODES else None
        self._clock = clock or now_ny
        self.analyze_when_blocked = analyze_when_blocked
        self.data_name = data_name or getattr(data, "name", type(data).__name__)
        self.universe: list[str] = list(cfg.scan.universe)
        self.last: StepResult | None = None
        self.analyses: dict[str, Any] = {}
        self.chains: dict[str, OptionChain] = {}
        self._snapshot: dict = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._day: date | None = None
        self._sent_today = 0
        self._shadow_book: list[Position] = []
        self._pending: list[Order] = []
        self._news_cache: dict[str, tuple[datetime, DirectionalSignal | None]] = {}
        self._earn_cache: dict[tuple[str, date], date | None] = {}
        self._housekeeping_key: Any = None
        # speed knobs (additive; defaults suit live use)
        self.max_workers = max_workers if max_workers is not None else (1 if self.data_name in ("synthetic", "scaled") else 6)
        self.cache_enabled = cache_enabled  # per-day daily-bar/regime caches and closed-market analysis reuse
        self.fetch_timeout = 90.0  # seconds allowed for the whole parallel fetch phase
        self.skip_idle = skip_idle  # backtests: skip re-analysis of idle (idea-less, unchanged) symbols
        self.dir_expiries = max(1, int(dir_expiries))  # nearest expiries inside directional_dte given to the engine
        self.vol_expiries = max(1, int(vol_expiries))  # expiries inside vol_dte closest to the target DTE
        self._daily_cache: dict[tuple[str, date], Any] = {}
        self._regime_cache: tuple[Any, DirectionalSignal] | None = None
        self._analysis_cache: dict[str, dict] = {}
        self._idle: dict[str, tuple] = {}
        kd = self.journal.get_value("kill_switch_day")
        self._kill_day: date | None = date.fromisoformat(kd) if kd else None
        self._kill_reason: str | None = None
        # supervised learning (kwanttrades.learning.LearningHub), attached by the owner of the session; optional
        self.learning: Any = None
        self.order_failures = 0  # consecutive broker rejections/errors (opening or closing orders)
        self.last_settle_spots: dict[str, float] = {}

    # ------------------------------------------------------------------ public helpers
    @property
    def killed(self) -> bool:
        return self._kill_day is not None and self._kill_day == self._day

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        """The latest step as plain JSON-serialisable dicts (what the web server serves)."""
        return self._snapshot

    # ------------------------------------------------------------------ account / book
    def _virtual_account(self) -> AccountState:
        eq = float(self.cfg.broker.starting_equity)
        used = sum(max(p.entry_price, 0.0) * MULTIPLIER * p.contracts if not p.idea.is_credit
                   else p.idea.max_loss * p.contracts for p in self._shadow_book)
        return AccountState(equity=eq, cash=eq, buying_power=max(eq - used, 0.0), day_pnl=0.0,
                            open_risk=sum(p.capital_at_risk for p in self._shadow_book))

    def _account(self) -> AccountState:
        if self.broker is None:
            return self._virtual_account()
        return self.broker.account()

    def _open_positions(self) -> list[Position]:
        if self.broker is None:
            return list(self._shadow_book)
        return [p for p in self.broker.positions() if p.is_open]

    def _trades_today(self, now: datetime) -> int:
        try:
            n = self.journal.trades_today(now)
        except Exception:  # pragma: no cover - defensive
            n = 0
        return max(n, self._sent_today)

    def _roll_day(self, now: datetime) -> None:
        if self._day != now.date():
            self._day = now.date()
            self._sent_today = 0
            self._shadow_book = []
            self._pending = []
            self._news_cache.clear()

    def _risk_cfg_for(self, now: datetime) -> RiskConfig:
        """On early-close days pull the flatten time and entry cut-off in front of the 13:00 close. A learned
        "no new trades before" can only move the entry-window START later (never loosens anything)."""
        r = self.cfg.risk
        d = now.date()
        if d in EARLY_CLOSES:
            close = session_close_dt(d)
            flat = (close - timedelta(minutes=5)).time()
            end = (close - timedelta(minutes=30)).time()
            r = dataclasses.replace(
                r, flatten_time=min(parse_hhmm(r.flatten_time), flat).strftime("%H:%M"),
                entry_window_end=min(parse_hhmm(r.entry_window_end), end).strftime("%H:%M"))
        hub = self.learning
        if hub is not None:
            start = hub.entry_start(r.entry_window_start)
            if parse_hhmm(start) > parse_hhmm(r.entry_window_start):
                r = dataclasses.replace(r, entry_window_start=start)
        return r

    # ------------------------------------------------------------------ data pieces
    def _chain_dte(self) -> int:
        return int(max(self.cfg.scan.vol_dte[1], self.cfg.scan.directional_dte[1]))

    def _regime(self, res: StepResult, now: datetime | None = None) -> DirectionalSignal:
        key = None if now is None else (now.date(), now.hour, now.minute // 30)
        if self.cache_enabled and key is not None and self._regime_cache and self._regime_cache[0] == key:
            return self._regime_cache[1]
        sig = self._regime_uncached(res)
        if key is not None and sig.detail.get("reason") != "data_unavailable":
            self._regime_cache = (key, sig)
        return sig

    def _regime_uncached(self, res: StepResult) -> DirectionalSignal:
        try:
            spy = self.data.daily_bars("SPY", 2.0)
            vix = self.data.daily_bars("^VIX", 1.5)
            vix3m = None
            try:
                vix3m = self.data.daily_bars("^VIX3M", 1.5)
            except Exception as exc:
                log.debug("^VIX3M unavailable: %s", exc)
            return regime_signal(spy, vix, vix3m)
        except Exception as exc:
            log.warning("regime data unavailable: %s", exc)
            res.errors.append({"symbol": None, "stage": "regime", "message": f"{type(exc).__name__}: {exc}"[:300]})
            return DirectionalSignal("MARKET", "regime", 0.0, 0.0, {"reason": "data_unavailable", "risk_multiplier": 1.0})

    def _news_signal(self, sym: str, now: datetime) -> DirectionalSignal | None:
        if self.news is None:
            return None
        hit = self._news_cache.get(sym)
        if hit is not None and (now - hit[0]).total_seconds() < 300:
            return hit[1]
        sig = None
        try:
            from kwanttrades.data.sentiment import score_news

            items = self.news.headlines(sym, self.cfg.sentiment.lookback_hours)
            sig = score_news(sym, items, self.cfg.sentiment, now=now)
        except Exception as exc:
            log.warning("news scoring failed for %s: %s", sym, exc)
        self._news_cache[sym] = (now, sig)
        return sig

    def _earnings(self, sym: str, now: datetime) -> date | None:
        key = (sym, now.date())
        if key in self._earn_cache:
            return self._earn_cache[key]
        fn = find_attr(self.data, "earnings_date")
        val = None
        if fn is not None:
            try:
                val = fn(sym)
            except Exception as exc:
                log.debug("earnings lookup failed for %s: %s", sym, exc)
        self._earn_cache[key] = val
        return val

    def _daily(self, sym: str, data_now: datetime):
        """Daily bars, fetched once per symbol per session day."""
        key = (sym, data_now.date())
        if self.cache_enabled and key in self._daily_cache:
            return self._daily_cache[key]
        df = self.data.daily_bars(sym, 2.0)
        if self.cache_enabled:
            if len(self._daily_cache) > 400:
                self._daily_cache.clear()
            self._daily_cache[key] = df
        return df

    def _fetch_chain(self, sym: str, held_exps: set) -> tuple[OptionChain, OptionChain]:
        """(trimmed chain for the engine, chain incl. held expiries for marking). Only the expiries the
        engine can trade are requested: the nearest few inside directional_dte (progressively wider
        windows) and the one or two inside vol_dte closest to the target DTE."""
        sc = self.cfg.scan
        lo, hi = int(sc.directional_dte[0]), int(sc.directional_dte[1])
        vlo, vhi = int(sc.vol_dte[0]), int(sc.vol_dte[1])
        tgt = int(getattr(self.engine, "target_vol_dte", 30))
        last: list[Exception] = []

        def grab(a: int, b: int):
            try:
                c = self.data.chain(sym, a, b)
                return c if c.quotes else None
            except Exception as exc:
                last.append(exc)
                return None

        dch = None
        for b in dict.fromkeys([min(hi, lo + 3), min(hi, lo + 7), hi]):
            dch = grab(lo, b)
            if dch:
                break
        vch = None
        for a, b in dict.fromkeys([(max(vlo, tgt - 5), min(vhi, tgt + 5)), (vlo, vhi)]):
            vch = grab(a, b)
            if vch:
                break
        base = dch or vch
        if base is None:
            raise last[-1] if last else RuntimeError(f"no option chain for {sym}")
        today = base.asof.astimezone(NY).date()
        keep: list = []
        if dch:
            keep += sorted(dch.expiries())[: self.dir_expiries]
        if vch:
            keep += sorted(vch.expiries(), key=lambda e: (abs((e - today).days - tgt), e))[: self.vol_expiries]
        keep_set = set(keep)
        quotes, seen = [], set()
        for c in (dch, vch):
            if c is None:
                continue
            for e in c.expiries():
                if e in keep_set and e not in seen:
                    seen.add(e)
                    quotes += [q for q in c.quotes if q.expiry == e]
        chain = dataclasses.replace(base, quotes=quotes)
        extra = [e for e in held_exps if e not in keep_set]
        if not extra:
            return chain, chain
        mq = list(quotes)
        for e in sorted(extra):
            n = (e - today).days
            c = grab(n, n) if n >= 0 else None
            if c is not None:
                mq += [q for q in c.quotes if q.expiry == e]
        return chain, dataclasses.replace(base, quotes=mq)

    def _fetch_symbol(self, sym: str, now: datetime, data_now: datetime, held_exps: set) -> dict:
        f: dict = {"chain": None, "mark_chain": None, "bars": None, "daily": None, "news": None,
                   "chain_error": None, "data_error": None}
        try:
            f["chain"], f["mark_chain"] = self._fetch_chain(sym, held_exps)
        except Exception as exc:
            f["chain_error"] = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("chain failed for %s: %s", sym, exc)
        try:
            f["bars"] = self.data.intraday_bars(sym, "1m", 5)
            f["daily"] = self._daily(sym, data_now)
        except Exception as exc:
            f["data_error"] = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("bars failed for %s: %s", sym, exc)
        if f["data_error"] is None:
            f["news"] = self._news_signal(sym, now)
        return f

    def _fetch_all(self, syms: list[str], now: datetime, data_now: datetime, held_exps: dict) -> dict[str, dict]:
        """I/O (chains, bars, news) across symbols on a bounded thread pool; everything that touches the
        journal or broker stays on the calling thread."""
        if self.max_workers <= 1 or len(syms) <= 1:
            return {s: self._fetch_symbol(s, now, data_now, held_exps.get(s, set())) for s in syms}
        pool = ThreadPoolExecutor(max_workers=min(self.max_workers, len(syms)), thread_name_prefix="kwanttrades-fetch")
        out: dict[str, dict] = {}
        try:
            futs = {s: pool.submit(self._fetch_symbol, s, now, data_now, held_exps.get(s, set())) for s in syms}
            deadline = _time.monotonic() + self.fetch_timeout
            for s, fu in futs.items():
                try:
                    out[s] = fu.result(timeout=max(1.0, deadline - _time.monotonic()))
                except Exception as exc:  # timeout or unexpected worker failure: one symbol, never the whole step
                    msg = f"fetch {type(exc).__name__}: {exc}"[:300]
                    out[s] = {"chain": None, "mark_chain": None, "bars": None, "daily": None, "news": None,
                              "chain_error": msg, "data_error": msg}
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return out

    def _signals(self, sym: str, now: datetime, data_now: datetime, regime: DirectionalSignal, rec: SymbolRecord,
                 bars: Any, daily: Any, news: DirectionalSignal | None) -> DirectionalSignal:
        cfg = self.cfg
        comps: dict[str, DirectionalSignal] = {
            "orb": orb_signal(sym, bars, daily, cfg.signals, now=data_now),
            "vwap": vwap_signal(sym, bars, now=data_now),
            "gap": gap_signal(sym, bars, daily, data_now),
            "regime": regime,
        }
        if news is not None:
            comps["news"] = news
        rec.components = comps
        comp = combine(sym, list(comps.values()), cfg.signals.weights)
        rec.composite = comp
        try:
            d = data_now.date()
            sess = visible_bars(session_bars(bars, d), None)
            if len(sess):
                closes = [float(x) for x in sess["close"].to_numpy()]
                step = max(1, int(math.ceil(len(closes) / 80)))
                rec.sparkline = [round(c, 4) for c in closes[::step]]
                prev = clean_daily(daily, before=d)
                if prev is not None and len(prev):
                    pc = float(prev["close"].iloc[-1])
                    if pc > 0:
                        rec.change_pct = closes[-1] / pc - 1.0
        except Exception as exc:  # pragma: no cover - cosmetic only
            log.debug("sparkline failed for %s: %s", sym, exc)
        try:
            self.journal.record_signal(comp, now)
        except Exception as exc:  # pragma: no cover
            log.debug("signal journaling failed: %s", exc)
        return comp

    @staticmethod
    def _flags(analysis: Any) -> dict[str, str]:
        out: dict[str, str] = {}
        for m in getattr(analysis, "mispricings", None) or []:
            try:
                if m.model == "svi_residual" or m.quote.occ_symbol not in out:
                    out[m.quote.occ_symbol] = "cheap" if m.edge > 0 else "rich"
            except Exception:  # pragma: no cover
                continue
        return out

    @staticmethod
    def _misp_dicts(analysis: Any, limit: int = 40) -> list[dict]:
        out = []
        for m in (getattr(analysis, "mispricings", None) or [])[:limit]:
            out.append(to_jsonable({
                "occ": m.quote.occ_symbol, "model": m.model, "edge": m.edge, "edge_after_costs": m.edge_after_costs,
                "zscore": m.zscore, "market_iv": m.market_iv, "fair_iv": m.fair_iv, "notes": m.notes,
            }))
        return out

    # ------------------------------------------------------------------ closing
    def _close_limit(self, pos: Position, chain: OptionChain) -> float | None:
        v = structure_quotes_value(pos, chain)
        if v is None:
            return None
        mid, hs = v
        return round(mid - hs - 0.01, 2)  # marketable: through the far side of the structure

    def _do_close(self, pos: Position, chain: OptionChain | None, reason: str, now: datetime, res: StepResult) -> bool:
        info = {"position_id": pos.id, "symbol": pos.idea.symbol, "strategy": pos.idea.strategy, "reason": reason,
                "contracts": pos.contracts, "status": "not_sent", "fill_price": None, "pnl": None, "limit": None}
        res.exits.append(info)
        if chain is None:
            info["status"] = "no_chain"
            return False
        limit = self._close_limit(pos, chain)
        if limit is None:
            info["status"] = "no_quote"
            return False
        info["limit"] = limit
        try:
            order = self.broker.close(pos.id, limit, reason)
        except Exception as exc:
            log.error("close failed for %s: %s", pos.id, exc)
            self.order_failures += 1
            info["status"] = "error"
            res.errors.append({"symbol": pos.idea.symbol, "stage": "close", "message": f"{type(exc).__name__}: {exc}"[:300]})
            return False
        info["status"] = order.status.value
        info["fill_price"] = order.fill_price
        self.order_failures = self.order_failures + 1 if order.status == OrderStatus.REJECTED else 0
        if order.status == OrderStatus.FILLED:
            info["pnl"] = pos.realized_pnl
            log.info("closed %s %s (%s): pnl %.2f", pos.idea.symbol, pos.idea.strategy, reason, pos.realized_pnl)
            return True
        if order.status == OrderStatus.NEW:
            self._pending.append(order)
        return order.status == OrderStatus.FILLED

    def _chain_for(self, sym: str) -> OptionChain | None:
        ch = self.chains.get(sym)
        if ch is not None:
            return ch
        try:
            ch = self.data.chain(sym, 0, 60)
            self.chains[sym] = ch
            return ch
        except Exception as exc:
            log.warning("chain unavailable for %s: %s", sym, exc)
            return None

    def flatten_all(self, now: datetime | None = None, reason: str = "manual_flatten") -> list[dict]:
        """Close every open position (broker modes). Used by the web UI and the backtester."""
        now = (now or self._clock()).astimezone(NY)
        res = StepResult(now=now, mode=self.mode)
        if self.broker is None:
            return []
        with self._lock:
            ps = [p for p in self.broker.positions() if p.is_open]
            chains = {}
            for sym in {p.idea.symbol for p in ps}:
                ch = self._chain_for(sym)
                if ch is not None:
                    chains[sym] = ch
            self.broker.mark_to_market(chains)
            for p in ps:
                self._do_close(p, chains.get(p.idea.symbol), reason, now, res)
            return res.exits

    def close_position(self, position_id: str, now: datetime | None = None, reason: str = "manual_close") -> dict:
        now = (now or self._clock()).astimezone(NY)
        res = StepResult(now=now, mode=self.mode)
        if self.broker is None:
            return {"ok": False, "message": f"mode {self.mode} has no broker"}
        with self._lock:
            pos = next((p for p in self.broker.positions() if p.id == position_id and p.is_open), None)
            if pos is None:
                return {"ok": False, "message": "position not found or already closed"}
            ch = self._chain_for(pos.idea.symbol)
            if ch is not None:
                self.broker.mark_to_market({pos.idea.symbol: ch})
            ok = self._do_close(pos, ch, reason, now, res)
            return {"ok": ok, "message": res.exits[-1]["status"], "exit": res.exits[-1]}

    # ------------------------------------------------------------------ the step
    def step(self, now: datetime | None = None, symbols: list[str] | None = None) -> StepResult:
        """One full cycle: settle/mark -> exits -> signals -> analysis -> sizing -> risk gate -> orders."""
        with self._lock:
            now = now if now is not None else self._clock()
            now = now.astimezone(NY) if now.tzinfo else now.replace(tzinfo=NY)
            res = StepResult(now=now, mode=self.mode)
            try:
                self._step(now, res, list(symbols) if symbols else list(self.universe))
            except Exception as exc:
                log.exception("step failed")
                res.errors.append({"symbol": None, "stage": "step", "message": f"{type(exc).__name__}: {exc}"[:300]})
            self.last = res
            try:
                self._snapshot = self._build_snapshot(res)
            except Exception as exc:  # pragma: no cover - never let UI plumbing break trading
                log.error("snapshot failed: %s", exc)
            return res

    def _step(self, now: datetime, res: StepResult, universe: list[str]) -> None:
        self._roll_day(now)
        state = market_state(now)
        is_open = state in ("opening_range", "open")
        data_now = effective_time(now)
        res.market_state, res.data_now = state, data_now
        risk_cfg = self._risk_cfg_for(now)
        risk = RiskManager(risk_cfg)
        if not is_open:
            res.notes.append(
                f"Market is {state}: option quotes are delayed/stale (as of the last session close, "
                f"{data_now:%Y-%m-%d %H:%M} NY); no opening orders are sent.")
        d = now.date()
        if d in HOLIDAYS:
            res.notes.append(f"US market holiday: {HOLIDAYS[d]}.")
        if d in EARLY_CLOSES:
            res.notes.append(f"Early close (13:00 NY): {EARLY_CLOSES[d]}.")

        regime = self._regime(res, now)
        res.regime = regime
        rm = float(regime.detail.get("risk_multiplier", 1.0) or 1.0)

        # ---- positions, then data for the universe + held symbols (parallel I/O)
        positions = self._open_positions()
        held_syms = {p.idea.symbol for p in positions}
        held_exps: dict[str, set] = {}
        for p in positions:
            held_exps.setdefault(p.idea.symbol, set()).update(l.quote.expiry for l in p.idea.legs)
        hub = self.learning
        if hub is not None and self.broker is not None:  # post-exit tracking: only symbols that are scanned anyway
            for sym, exps in hub.tracked_expiries(self, now).items():
                if sym in universe:
                    held_exps.setdefault(sym, set()).update(exps)
        wanted = list(dict.fromkeys(universe + sorted(held_syms - set(universe))))
        reuse: dict[str, dict] = {}
        if self.cache_enabled and not is_open:  # market closed: quotes cannot change, reuse the last analysis
            for sym in wanted:
                e = self._analysis_cache.get(sym)
                if (e and sym not in held_syms and e["key"] == (data_now, state)
                        and (now - e["now"]).total_seconds() < 1800):
                    reuse[sym] = e
        fetched = self._fetch_all([s for s in wanted if s not in reuse], now, data_now, held_exps)
        self.chains = {}
        analysis_chains: dict[str, OptionChain] = {}
        for sym, f in fetched.items():
            if f["chain"] is not None:
                self.chains[sym] = f["mark_chain"]
                analysis_chains[sym] = f["chain"]
            else:
                rec = res.symbols.setdefault(sym, SymbolRecord(sym))
                rec.error = f"chain: {f['chain_error']}"
                res.errors.append({"symbol": sym, "stage": "chain", "message": rec.error})

        if self.broker is not None:
            try:
                self.broker.mark_to_market(self.chains)
            except Exception as exc:
                log.error("mark_to_market failed: %s", exc)
                res.errors.append({"symbol": None, "stage": "mark_to_market", "message": f"{type(exc).__name__}: {exc}"[:300]})
            self._settle(now, res)
            if hub is not None:
                hub.after_marks(self, now)
            positions = self._open_positions()
            held_syms = {p.idea.symbol for p in positions}
            account = self._account()
            self._update_kill_switch(now, account, res)
        else:
            account = self._virtual_account()
        res.kill_switch = self.killed
        res.kill_reason = self._kill_reason if self.killed else None

        # ---- signals (universe + held symbols)
        composites: dict[str, DirectionalSignal] = {}
        bar_store: dict[str, dict] = {}
        for sym in wanted:
            if sym in reuse:
                res.symbols[sym] = reuse[sym]["rec"]
                composites[sym] = reuse[sym]["comp"]
                continue
            rec = res.symbols.setdefault(sym, SymbolRecord(sym))
            f = fetched[sym]
            ch = self.chains.get(sym)
            if ch is not None:
                rec.spot = float(ch.spot)
            if f["data_error"] is not None:
                rec.error = f"signals: {f['data_error']}"
                res.errors.append({"symbol": sym, "stage": "signals", "message": rec.error})
                continue
            try:
                composites[sym] = self._signals(sym, now, data_now, regime, rec, f["bars"], f["daily"], f["news"])
                bar_store[sym] = {"bars": f["bars"], "daily": f["daily"]}
            except Exception as exc:
                log.warning("signals failed for %s: %s", sym, exc)
                rec.error = f"signals: {type(exc).__name__}: {exc}"[:300]
                res.errors.append({"symbol": sym, "stage": "signals", "message": rec.error})

        # ---- exits
        closed_now: set[str] = set()
        if self.broker is not None and is_open and positions:
            for pos in positions:
                reason = self._exit_reason(pos, now, composites.get(pos.idea.symbol), risk_cfg)
                if reason is None:
                    continue
                if self._do_close(pos, self.chains.get(pos.idea.symbol), reason, now, res):
                    closed_now.add(pos.idea.symbol)
            positions = self._open_positions()
            held_syms = {p.idea.symbol for p in positions}
            account = self._account()

        # ---- analysis and idea generation
        trades_today = self._trades_today(now)
        window_ok = self._in_entry_window(now, risk_cfg)
        entries_possible = (is_open and window_ok and not self.killed
                            and trades_today < risk_cfg.max_trades_per_day
                            and len(positions) < risk_cfg.max_open_positions)
        cands: list[tuple[TradeIdea, SymbolRecord, Any]] = []
        learn_blocks: dict[int, str] = {}

        def learned(idea: TradeIdea) -> TradeIdea:
            if hub is not None:
                idea = copy.copy(idea)  # never mutate a cached idea
                why = hub.adjust_idea(idea)
                if why:
                    learn_blocks[id(idea)] = why
            return idea

        grade_fn = getattr(self.engine, "grade", None)
        for sym in universe:
            rec = res.symbols.setdefault(sym, SymbolRecord(sym))
            comp = composites.get(sym)
            if comp is None:
                continue
            if not self.analyze_when_blocked and (not entries_possible or sym in held_syms or sym in closed_now):
                continue
            if sym in reuse:
                e = reuse[sym]
                self.analyses[sym] = e["analysis"]
                for idea in e["ideas"]:
                    cands.append((learned(copy.copy(idea)), rec, e["analysis"]))
                continue
            ch = analysis_chains.get(sym)
            if ch is None:
                continue
            grade = None
            if self.skip_idle and grade_fn is not None and sym not in held_syms:
                try:
                    grade = grade_fn(comp)
                except Exception:
                    grade = None
                orb = rec.components.get("orb")
                orb_state = orb.detail.get("state") if orb is not None else None
                prev = self._idle.get(sym)
                if (grade in ("neutral", "unclear") and prev and prev[0] == now.date() and prev[1] == grade
                        and prev[2] == orb_state):
                    rec.vol_view, rec.mispricings, rec.notes = prev[3], prev[4], prev[5]
                    continue
            try:
                store = bar_store.get(sym, {})
                analysis = self.engine.analyze(
                    sym, ch, store.get("daily"), comp, intraday_bars=store.get("bars"),
                    earnings_date=self._earnings(sym, now), risk_multiplier=rm)
                ideas = list(self.engine.generate(analysis) or [])
            except Exception as exc:
                log.warning("analysis failed for %s: %s", sym, exc)
                rec.error = f"analysis: {type(exc).__name__}: {exc}"[:300]
                res.errors.append({"symbol": sym, "stage": "analysis", "message": rec.error})
                continue
            self.analyses[sym] = analysis
            rec.vol_view = getattr(analysis, "vol_view", None)
            rec.mispricings = self._misp_dicts(analysis)
            rec.notes = list(getattr(analysis, "notes", []) or [])
            rec.n_ideas = len(ideas)
            if self.skip_idle:
                if not ideas and grade in ("neutral", "unclear"):
                    orb = rec.components.get("orb")
                    self._idle[sym] = (now.date(), grade, orb.detail.get("state") if orb is not None else None,
                                       rec.vol_view, rec.mispricings, rec.notes)
                else:
                    self._idle.pop(sym, None)
            if self.cache_enabled and not is_open:
                self._analysis_cache[sym] = {"key": (data_now, state), "now": now, "rec": rec,
                                             "comp": comp, "analysis": analysis, "ideas": ideas}
            for idea in ideas:
                cands.append((learned(idea), rec, analysis))

        # ---- sizing, risk gate, orders (best score first across the whole universe)
        cands.sort(key=lambda c: c[0].score, reverse=True)
        sent_syms: set[str] = set()
        for idea, rec, analysis in cands:
            sym = idea.symbol
            contracts = 0
            try:
                contracts = size_position(idea, account, risk_cfg, risk_multiplier=rm)
            except Exception as exc:
                log.warning("sizing failed for %s: %s", sym, exc)
            idea.contracts = contracts
            extra: list[str] = []
            if self.killed:
                extra.append("BLOCK: kill switch active (daily loss limit hit); no new entries until the next session")
            if not is_open:
                extra.append(f"BLOCK: market is {state}; no opening orders outside the session")
            if sym in held_syms or sym in sent_syms or sym in closed_now:
                extra.append(f"BLOCK: {sym} already has an open position or order (one trade per symbol at a time)")
            if any(o.status == OrderStatus.NEW and o.idea.symbol == sym and o.action == "open" for o in self._pending):
                extra.append(f"BLOCK: an order for {sym} is still pending")
            if id(idea) in learn_blocks:
                extra.append(learn_blocks[id(idea)])
            try:
                day5 = self.journal.day_trades_last_5_sessions(now)
            except Exception:
                day5 = 0
            dec = risk.check(idea, contracts, account, positions, now, trades_today, day5)
            decision = RiskDecision(passed=not extra and dec.passed, reasons=extra + list(dec.reasons))
            irec = IdeaRecord(idea, contracts, decision, spot=rec.spot, flags=self._flags(analysis))
            if decision.passed and self.mode == "shadow":
                decision = RiskDecision(True, ["shadow: order logged but NOT sent"])
                irec.decision = decision
            try:
                self.journal.record_decision(idea, decision, contracts, now)
            except Exception as exc:  # pragma: no cover
                log.error("decision journaling failed: %s", exc)
            res.ideas.append(irec)
            if not decision.passed:
                irec.action = "blocked"
                continue
            if self.mode == "scan":
                continue  # scan ranks and reports only; there is no broker to send to
            if self.mode == "shadow":
                self._shadow_order(idea, contracts, now, irec, res)
            else:
                self._send_order(idea, contracts, irec, res, now)
                if irec.action == "sent" and hub is not None and res.orders:
                    hub.on_entry(self, idea, contracts, res.orders[-1], rec, analysis, rm, now)
            if irec.action in ("sent", "shadow"):
                sent_syms.add(sym)
                trades_today = self._trades_today(now)
                account = self._account()
                positions = self._open_positions()

        # ---- wrap up
        if self.broker is not None:
            try:
                account = self._account()
                self.journal.record_equity(account, now)
            except Exception as exc:
                log.error("equity snapshot failed: %s", exc)
        res.account = {"equity": account.equity, "cash": account.cash, "buying_power": account.buying_power,
                       "day_pnl": account.day_pnl, "open_risk": account.open_risk}
        res.trades_today = self._trades_today(now)
        if hub is not None:
            hub.after_step(self, now)

    # ------------------------------------------------------------------ step parts
    @staticmethod
    def _in_entry_window(now: datetime, risk_cfg: RiskConfig) -> bool:
        t = now.time().replace(second=0, microsecond=0)
        return parse_hhmm(risk_cfg.entry_window_start) <= t <= parse_hhmm(risk_cfg.entry_window_end)

    def _exit_reason(self, pos: Position, now: datetime, direction: DirectionalSignal | None,
                     risk_cfg: RiskConfig) -> str | None:
        if is_intraday(pos.idea) and pos.opened.astimezone(NY).date() < now.date():
            return "time_flatten"  # an intraday idea must never be carried overnight
        return evaluate_exit(pos, self.chains.get(pos.idea.symbol), direction, now, risk_cfg,
                             kill_switch=self.killed and self.mode in AUTO_FLATTEN_MODES)

    def _settle(self, now: datetime, res: StepResult) -> None:
        """After the close (or on non-trading days) settle expired positions at intrinsic value."""
        fn = getattr(self.broker, "settle_expired", None)
        if fn is None:
            return
        d = now.date()
        after_close = (not is_trading_day(d)) or now >= session_close_dt(d)
        settle_date = d if after_close else d - timedelta(days=1)
        try:
            open_ps = [p for p in self.broker.positions() if p.is_open and p.idea.expiry <= settle_date]
            if not open_ps:
                return
            spots = {}
            for p in open_ps:
                sym = p.idea.symbol
                try:
                    spots[sym] = float(self.data.spot(sym))
                except Exception as exc:
                    log.warning("no spot to settle %s: %s", sym, exc)
            self.last_settle_spots = dict(spots)
            for p in fn(spots, settle_date):
                res.settled.append({"position_id": p.id, "symbol": p.idea.symbol, "strategy": p.idea.strategy,
                                    "pnl": p.realized_pnl, "exit_value": p.exit_price})
                log.info("settled %s %s at expiry: pnl %.2f", p.idea.symbol, p.idea.strategy, p.realized_pnl)
        except Exception as exc:
            log.error("settlement failed: %s", exc)
            res.errors.append({"symbol": None, "stage": "settle", "message": f"{type(exc).__name__}: {exc}"[:300]})

    def _update_kill_switch(self, now: datetime, account: AccountState, res: StepResult) -> None:
        start = account.equity - account.day_pnl
        limit = self.cfg.risk.daily_loss_limit_pct
        if start <= 0 or self.killed:
            return
        pct = account.day_pnl / start
        if pct <= -limit + 1e-9:
            self._kill_day = now.date()
            self._kill_reason = f"daily loss {pct:.1%} reached the -{limit:.1%} limit"
            try:
                self.journal.set_value("kill_switch_day", self._kill_day.isoformat())
            except Exception:  # pragma: no cover
                pass
            log.warning("KILL SWITCH: %s. New entries blocked until the next session.", self._kill_reason)
            if self.mode == "alpaca_live":
                log.warning("alpaca_live: positions are NOT auto-closed by the kill switch; close them yourself.")
        elif self._kill_day == now.date():
            pass

    def _shadow_order(self, idea: TradeIdea, contracts: int, now: datetime, irec: IdeaRecord, res: StepResult) -> None:
        order = Order(id="shadow-" + uuid.uuid4().hex[:12], idea=idea, contracts=contracts, limit_price=idea.entry_limit,
                      action="open", status=OrderStatus.NEW, reason="shadow: intended order, NOT sent", submitted=now)
        try:
            self.journal.save_order(order)
        except Exception as exc:  # pragma: no cover
            log.error("shadow order journaling failed: %s", exc)
        self._shadow_book.append(Position(id="shadow-pos-" + order.id[-8:], idea=idea, contracts=contracts,
                                          entry_price=idea.entry_limit, opened=now, mark=idea.net_price))
        self._sent_today += 1
        irec.action, irec.order_id, irec.order_status = "shadow", order.id, "shadow"
        res.orders.append({"order_id": order.id, "symbol": idea.symbol, "strategy": idea.strategy, "contracts": contracts,
                           "limit": idea.entry_limit, "status": "shadow", "shadow": True, "fill_price": None,
                           "reason": order.reason})
        log.info("SHADOW %s %s x%d limit %.2f (not sent)", idea.symbol, idea.strategy, contracts, idea.entry_limit)

    def _send_order(self, idea: TradeIdea, contracts: int, irec: IdeaRecord, res: StepResult, now: datetime) -> None:
        try:
            order = self.broker.submit(idea, contracts, idea.entry_limit)
        except Exception as exc:
            log.error("order submit failed for %s: %s", idea.symbol, exc)
            self.order_failures += 1
            irec.action = "send_failed"
            res.errors.append({"symbol": idea.symbol, "stage": "submit", "message": f"{type(exc).__name__}: {exc}"[:300]})
            return
        irec.order_id, irec.order_status = order.id, order.status.value
        res.orders.append({"order_id": order.id, "symbol": idea.symbol, "strategy": idea.strategy, "contracts": contracts,
                           "limit": idea.entry_limit, "status": order.status.value, "shadow": False,
                           "fill_price": order.fill_price, "fees": order.fees, "reason": order.reason,
                           "position_id": order.position_id})
        if order.status == OrderStatus.REJECTED:
            self.order_failures += 1
            irec.action = "send_failed"
            log.info("order rejected for %s: %s", idea.symbol, order.reason)
            return
        self.order_failures = 0
        irec.action = "sent"
        self._sent_today += 1
        if order.status == OrderStatus.NEW:
            self._pending.append(order)
        log.info("ORDER %s %s x%d limit %.2f -> %s", idea.symbol, idea.strategy, contracts, idea.entry_limit, order.status.value)

    # ------------------------------------------------------------------ snapshot
    def _build_snapshot(self, res: StepResult) -> dict:
        now = res.now
        d = res.to_dict()
        positions: list[dict] = []
        closed: list[dict] = []
        if self.broker is not None:
            try:
                for p in self.broker.positions(include_closed=True):
                    (positions if p.is_open else closed).append(position_dict(p))
            except Exception as exc:  # pragma: no cover
                log.debug("positions for snapshot failed: %s", exc)
        else:
            positions = [position_dict(p) for p in self._shadow_book]
        d["positions"] = {"open": positions, "closed": closed[-50:]}
        d["market"] = to_jsonable({
            "state": res.market_state, "next_open": next_open(now), "next_close": next_close(now),
            "opening_range_ends": opening_range_end(now) if res.market_state in ("pre", "opening_range") else None})
        d["data_source"] = self.data_name
        d["universe"] = list(self.universe)
        d["max_trades_per_day"] = self.cfg.risk.max_trades_per_day
        d["data_delay_note"] = res.notes[0] if res.notes else "Free data feeds are delayed by about 15 minutes."
        return d

    # ------------------------------------------------------------------ run loop
    def run(self, max_steps: int | None = None, sleep: Callable[[float], Any] | None = None) -> int:
        """Loop every cfg.poll_seconds during the NY session; one housekeeping step after the close
        (settlement, equity); stop on the kill switch once positions are closed. Returns steps run."""
        self._stop.clear()
        sleeper = sleep or (lambda s: self._stop.wait(s))
        steps = 0
        poll = max(1, int(self.cfg.poll_seconds))
        while not self._stop.is_set():
            if max_steps is not None and steps >= max_steps:
                break
            now = self._clock().astimezone(NY)
            state = market_state(now)
            if state in ("opening_range", "open"):
                self.step(now)
                steps += 1
                if self.killed and self.mode in BROKER_MODES and not self._open_positions():
                    log.warning("kill switch active and flat: stopping the session loop")
                    break
                sleeper(poll)
                continue
            key = (now.date(), state)
            if key != self._housekeeping_key:
                self._housekeeping_key = key
                self.step(now)
                steps += 1
            wait = (next_open(now) - now).total_seconds()
            sleeper(max(1.0, min(wait, 60.0)))
        return steps


# --------------------------------------------------------------------------- factories
PROVIDERS = ("yahoo", "openbb", "crosscheck", "synthetic")


# The OpenBB fetch runs one `asyncio.run` per call, which is not safe from several threads
# (aiohttp's cache lock gets bound to a different event loop and callers hang). For parallel scans we
# route every CBOE fetch through ONE persistent background event loop instead (workaround; the
# module-level `_fetch_raw` of the OpenBB provider is swapped at runtime).
_SHARED_LOOP: dict[str, Any] = {}
_SHARED_LOCK = threading.Lock()


def install_shared_openbb_loop() -> None:
    import asyncio

    from kwanttrades.data import openbb_provider as ob

    with _SHARED_LOCK:
        if _SHARED_LOOP.get("installed"):
            return
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, name="openbb-loop", daemon=True).start()
        original = ob._fetch_raw
        _SHARED_LOOP["loop"], _SHARED_LOOP["installed"] = loop, True

        def fetch_raw(symbol: str) -> dict:
            try:
                from openbb_cboe.models.options_chains import CboeOptionsChainsFetcher
            except ImportError:
                return original(symbol)

            async def go() -> dict:
                query = CboeOptionsChainsFetcher.transform_query({"symbol": symbol})
                return await CboeOptionsChainsFetcher.aextract_data(query, {})

            def run() -> dict:
                return asyncio.run_coroutine_threadsafe(go(), loop).result(timeout=90)

            try:
                return run()
            except Exception as exc:
                if not ob._is_cert_error(exc):
                    raise
                import aiohttp.connector as conn
                import certifi
                import ssl

                with _SHARED_LOCK:  # trust the bundled CA file (verification stays ON) from now on
                    conn._SSL_CONTEXT_VERIFIED = ssl.create_default_context(cafile=certifi.where())
                return run()

        ob._fetch_raw = fetch_raw


def openbb_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("openbb_cboe") is not None and importlib.util.find_spec("openbb_core") is not None
    except Exception:  # pragma: no cover
        return False


def default_provider_name() -> str:
    return "crosscheck" if openbb_available() else "yahoo"


def make_provider(name: str | None = None, *, symbols: list[str] | None = None,
                  clock: Callable[[], datetime] | None = None, seed: int = 7, cache_dir: str = "data/cache") -> Any:
    """Market-data provider by name: yahoo | openbb | crosscheck | synthetic (default: crosscheck when
    openbb_cboe imports, else yahoo)."""
    name = (name or default_provider_name()).lower()
    if name not in PROVIDERS:
        raise ValueError(f"unknown provider {name!r}; choose one of {', '.join(PROVIDERS)}")
    if name == "synthetic":
        from kwanttrades.data.synthetic import SyntheticProvider

        now = (clock or now_ny)()
        end = effective_time(now).date()
        from kwanttrades.data.synthetic import trading_days_before

        start = trading_days_before(end + timedelta(days=1), 10)[0]
        return SyntheticProvider.simulated(list(symbols or ["SPY"]), n_days=10, start_date=start, seed=seed, clock=clock)
    from kwanttrades.data.yahoo import YahooProvider

    yahoo = YahooProvider(cache_dir=cache_dir)
    yahoo.name = "yahoo"
    if name == "yahoo":
        return yahoo
    try:
        from kwanttrades.data.openbb_provider import OpenBBProvider
    except ImportError as exc:  # pragma: no cover
        raise ValueError("OpenBB is not installed: python -m pip install openbb-core openbb-cboe openbb-derivatives") from exc
    if not openbb_available():
        raise ValueError("OpenBB (openbb_cboe) is not installed: python -m pip install openbb-core openbb-cboe openbb-derivatives")
    install_shared_openbb_loop()
    obb = OpenBBProvider(fallback=yahoo)
    if name == "openbb":
        obb.name = "openbb"
        return obb
    from kwanttrades.data.crosscheck import CrossCheckedProvider

    cc = CrossCheckedProvider(obb, yahoo)
    cc.name = "crosscheck"
    return cc


def make_broker(cfg: Config, journal: Journal, clock: Callable[[], datetime] | None = None) -> Any:
    """Broker for cfg.broker.mode. Live trading is gated only by AlpacaBroker's own environment check."""
    mode = cfg.broker.mode
    if mode == "paper":
        from kwanttrades.execution.paper import PaperBroker

        return PaperBroker(cfg.broker, journal, clock=clock)
    if mode in ("alpaca_paper", "alpaca_live"):
        from kwanttrades.execution.alpaca import AlpacaBroker

        return AlpacaBroker(cfg.broker, journal=journal)
    raise ValueError(f"unknown broker mode {mode!r}; use paper, alpaca_paper or alpaca_live in config.toml")

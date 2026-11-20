"""Graduation: when the built-in paper simulator has earned a move to the owner's Alpaca PAPER account.

Stages (``data/stage.json``): "paper" -> "alpaca_paper". There is no live stage: live trading stays the owner's
manual decision behind the KWANT_ALLOW_LIVE environment gate (README "Going live"); nothing here can enable it.

Criteria (defaults in ``[graduation]``), all on closed paper-simulator trades:
  closed trades >= 40; trading days covered >= 20; profit factor >= 1.3; worst drawdown of closed-trade P&L <= 10% of
  starting equity; >= 60% of weeks profitable with at least 4 weeks; 90% CI lower bound of mean (P&L / max loss) > 0;
  and >= 25 trades AFTER the most recent learning change (or safety fallback), on which the quality criteria
  (profit factor, drawdown, weekly consistency, CI) must hold as well.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from kwanttrades.app.session import is_trading_day
from kwanttrades.config import GraduationConfig
from kwanttrades.learning import stats
from kwanttrades.types import NY

log = logging.getLogger("kwanttrades.graduation")

STAGES = ("paper", "alpaca_paper")
LIVE_NOTE = ("Live trading (real money) is the owner's decision and is never switched on by Kwant Trades. "
             "This checklist only shows whether the Alpaca paper results would meet the same bar.")
LIVE_HOW = ("To go live you would, yourself: read README \"Going live\", put live keys and the line "
            "KWANT_ALLOW_LIVE=YES_I_ACCEPT_THE_RISK in .env, set mode = \"alpaca_live\" in config.toml, shrink the risk "
            "limits and start it from the command line. The dashboard can never start live trading.")


def _ts(x: Any) -> datetime | None:
    if isinstance(x, datetime):
        return x.astimezone(NY)
    if isinstance(x, str) and x:
        try:
            return datetime.fromisoformat(x).astimezone(NY)
        except ValueError:
            return None
    return None


def trading_days_spanned(trades: list[dict]) -> int:
    if not trades:
        return 0
    first = min(_ts(t["opened"]) or _ts(t["closed"]) for t in trades).date()
    last = max(_ts(t["closed"]) for t in trades).date()
    n, d = 0, first
    while d <= last:
        if is_trading_day(d):
            n += 1
        d += timedelta(days=1)
    return n


def weekly(trades: list[dict]) -> tuple[int, int]:
    """(weeks with at least one closed trade, of which profitable)."""
    by: dict[tuple[int, int], float] = {}
    for t in trades:
        iso = _ts(t["closed"]).isocalendar()
        by[(iso[0], iso[1])] = by.get((iso[0], iso[1]), 0.0) + float(t.get("realized_pnl") or 0.0)
    return len(by), sum(1 for v in by.values() if v > 0)


def _crit(cid: str, label: str, plain: str, value: Any, target: Any, dv: str, dt: str, ok: bool, progress: float) -> dict:
    p = 0.0 if progress is None or not math.isfinite(progress) else max(0.0, min(1.0, float(progress)))
    return {"id": cid, "label": label, "plain": plain, "value": value, "target": target, "display_value": dv,
            "display_target": dt, "pass": bool(ok), "progress": round(1.0 if ok else min(p, 0.99), 4)}


def quality(trades: list[dict], g: GraduationConfig, start_equity: float) -> list[dict]:
    """Profit factor, drawdown, weekly consistency and CI on a set of trades."""
    pnls = [float(t.get("realized_pnl") or 0.0) for t in trades]
    rs = [float(t["r"]) for t in trades if t.get("r") is not None]
    out = []
    pf = stats.profit_factor(pnls)
    if pf is None:
        out.append(_crit("profit_factor", "Profit factor", "Money won ÷ money lost. Above 1 means winners outweigh losers.",
                         None, g.min_profit_factor, "—", f"{g.min_profit_factor:.2f}", False, 0.0))
    else:
        dv = "no losses yet" if math.isinf(pf) else f"{pf:.2f}"
        out.append(_crit("profit_factor", "Profit factor", "Money won ÷ money lost. Above 1 means winners outweigh losers.",
                         None if math.isinf(pf) else round(pf, 4), g.min_profit_factor, dv, f"{g.min_profit_factor:.2f}",
                         pf >= g.min_profit_factor, pf / g.min_profit_factor if g.min_profit_factor > 0 else 1.0))
    dd = stats.max_drawdown(pnls, start_equity)
    out.append(_crit("max_drawdown", "Worst drawdown", "The deepest dip from a high point, as a share of the starting money.",
                     round(dd, 4), g.max_drawdown_pct, f"{dd:.1%}", f"at most {g.max_drawdown_pct:.0%}",
                     dd <= g.max_drawdown_pct + 1e-12, 1.0 if dd <= 0 else g.max_drawdown_pct / dd))
    weeks, good = weekly(trades)
    frac = good / weeks if weeks else 0.0
    wp = min(1.0, frac / g.min_profitable_weeks_frac if g.min_profitable_weeks_frac > 0 else 1.0) * min(1.0, weeks / max(g.min_weeks, 1))
    out.append(_crit("profitable_weeks", "Profitable weeks", f"Most weeks made money (needs at least {g.min_weeks} weeks).",
                     round(frac, 4), g.min_profitable_weeks_frac, f"{good} of {weeks} weeks ({frac:.0%})",
                     f"{g.min_profitable_weeks_frac:.0%} of {g.min_weeks}+ weeks",
                     weeks >= g.min_weeks and frac >= g.min_profitable_weeks_frac, wp))
    m, lo, hi, n = stats.mean_ci(rs, 0.90)
    if n >= 2 and math.isfinite(lo):
        hw = (hi - lo) / 2.0
        prog = 1.0 if lo > 0 else ((m + hw) / (2 * hw) if hw > 0 else (1.0 if m > 0 else 0.0))
        dv = f"{lo:+.1%} (average {m:+.1%})"
    else:
        prog, dv = 0.0, "—"
    ok = (n >= 2 and lo > 0) if g.require_positive_ci else True
    out.append(_crit("edge_ci", "Edge is real", "Profit per trade as a share of the amount risked: even the cautious end of "
                     "the 90% range must be above zero, so it is unlikely to be luck.",
                     None if not math.isfinite(lo) else round(lo, 4), 0.0, dv, "above 0%", ok, prog))
    return out


def evaluate(trades: list[dict], g: GraduationConfig, start_equity: float, oos_since: datetime | None) -> dict:
    """The full checklist for a set of closed trades (oldest first)."""
    trades = sorted([t for t in trades if t.get("closed") is not None], key=lambda t: _ts(t["closed"]))
    n = len(trades)
    days = trading_days_spanned(trades)
    crits = [
        _crit("closed_trades", "Closed trades", "Enough finished trades to judge the results.", n, g.min_closed_trades,
              str(n), str(g.min_closed_trades), n >= g.min_closed_trades, n / max(g.min_closed_trades, 1)),
        _crit("trading_days", "Trading days covered", "Spread over enough days, not one lucky week.", days,
              g.min_trading_days, str(days), str(g.min_trading_days), days >= g.min_trading_days,
              days / max(g.min_trading_days, 1)),
    ]
    crits += quality(trades, g, start_equity)
    oos = [t for t in trades if oos_since is None or (_ts(t.get("opened")) or _ts(t["closed"])) > oos_since]
    oq = quality(oos, g, start_equity)
    oq_ok = all(c["pass"] for c in oq)
    need = g.out_of_sample_trades
    count_ok = len(oos) >= need
    if count_ok and not oq_ok:
        oprog = sum(c["progress"] for c in oq) / len(oq)
    else:
        oprog = len(oos) / max(need, 1)
    crits.append(_crit("out_of_sample", "Fresh trades since the last change",
                       "Trades opened after the most recent learning change must pass the same tests, so the result "
                       "is not just the learning fitting old trades.", len(oos), need,
                       str(len(oos)) + (", not all passing" if count_ok and not oq_ok else ""),
                       str(need), count_ok and oq_ok, oprog))
    progress = sum(c["progress"] for c in crits) / len(crits)
    return {"criteria": crits, "progress": round(progress, 4), "pass": all(c["pass"] for c in crits),
            "out_of_sample": {"trades": len(oos), "required": need, "since": None if oos_since is None else oos_since.isoformat(),
                              "criteria": oq, "pass": count_ok and oq_ok}}


class StageStore:
    """data/stage.json: {"stage", "ready", "alert", "events", "updated"} (path None = memory only)."""

    def __init__(self, path: str | os.PathLike | None):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self.data = {"stage": "paper", "ready": False, "alert": None, "events": [], "updated": None}
        self.load()

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("cannot read %s (%s); stage starts at paper", self.path, exc)
            return
        if not isinstance(raw, dict):
            return
        st = raw.get("stage")
        self.data["stage"] = st if st in STAGES else "paper"  # anything else (e.g. a hand-typed "live") = paper
        self.data["ready"] = bool(raw.get("ready", False))
        self.data["alert"] = raw.get("alert") if isinstance(raw.get("alert"), str) else None
        ev = raw.get("events")
        self.data["events"] = [e for e in ev if isinstance(e, dict)][-50:] if isinstance(ev, list) else []
        self.data["updated"] = raw.get("updated")

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=1, allow_nan=False, default=str), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def stage(self) -> str:
        st = self.data.get("stage")
        return st if st in STAGES else "paper"

    def set(self, stage: str, now: datetime, kind: str, message: str, evidence: dict | None = None,
            alert: str | None = None) -> dict:
        if stage not in STAGES:
            raise ValueError(f"stage must be one of {STAGES}")
        with self._lock:
            ev = {"ts": now.astimezone(NY).isoformat(), "kind": kind, "from": self.stage, "to": stage, "message": message,
                  "evidence": evidence or {}}
            self.data["stage"] = stage
            self.data["ready"] = False
            self.data["alert"] = alert
            self.data["events"] = (self.data.get("events") or [])[-49:] + [ev]
            self.data["updated"] = ev["ts"]
            self.save()
            return ev

    def mark_ready(self, ready: bool) -> None:
        with self._lock:
            if bool(self.data.get("ready")) != bool(ready):
                self.data["ready"] = bool(ready)
                self.save()

    def last_event(self, kind: str | None = None) -> dict | None:
        evs = [e for e in (self.data.get("events") or []) if kind is None or e.get("kind") == kind]
        return evs[-1] if evs else None

    def last_fallback_ts(self) -> datetime | None:
        e = self.last_event("fallback")
        return _ts(e.get("ts")) if e else None

    def clear_alert(self) -> None:
        with self._lock:
            if self.data.get("alert"):
                self.data["alert"] = None
                self.save()


def stage_path_for(journal: Any) -> Path | None:
    db = str(getattr(journal, "db_path", ":memory:") or ":memory:")
    if db == ":memory:":
        return None
    return Path(db).expanduser().parent / "stage.json"

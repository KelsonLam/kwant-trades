"""Supervised learning: slow, bounded, evidence-gated changes to a handful of strategy knobs.

Learned values live in ``data/learned.json`` (next to the journal) and are applied on top of config.toml when an
engine or session is built. config.toml is never rewritten. Risk caps (max loss per trade, daily loss limit,
portfolio risk, trades per day, defined-risk rule, live gate) are not knobs: learning cannot touch them. The only
risk-config field it influences is the entry-window START, and only ever later (tighter).

Rules (each a small pure function; ``r`` = realised P&L / max loss of the trade):

* Minimum samples ........ no change of any kind before MIN_TOTAL (20) closed trades.
* Strategy pause ......... a strategy with >= 10 trades (since its weight last changed) whose 80% t-interval
                           of mean r lies entirely below 0 is paused (weight 0; the one change exempt from the
                           step limit, because cutting a proven loser is risk-reducing). A paused strategy makes
                           no trades and therefore no new evidence: it stays paused until the owner reverts it.
* Strategy raise ......... a strategy with a reduced weight (0 < w < 1) whose 80% interval lies entirely above 0
                           over >= 10 trades gets +0.25.
* Calibration ............ n >= 20 trades with a predicted POP: if fewer won than predicted with a one-sided
                           binomial p < 0.10 (model overconfident), VOL_SHRINK moves toward the market (-0.05),
                           or, if already at its floor, DRIFT_K is lowered (-0.05).
* Direction .............. n >= 20 directional trades: if the 90% Clopper-Pearson interval of the hit rate lies
                           entirely below 50%, DRIFT_K -0.05; when DRIFT_K is at its floor, min_direction +0.05.
* Exits .................. replay closed trades along their recorded mark paths (including post-exit tracking)
                           under a small stop/take grid; adopt a pair only if it beats the current pair by >= 3%
                           of max loss per trade with the 80% interval of the paired gain above 0 on the first
                           2/3 of trades AND is not worse on the last 1/3 (walk-forward); n >= 25 replayable trades.
* Time of day ............ if entries in the first 15 minutes of the allowed window (n >= 15) lose on average,
                           trail later entries (n >= 15) by >= 10% of max loss, and a one-sided Welch test gives
                           p < 0.10, entries before window-start + 15 min stop (09:45 -> 10:00 -> 10:15 ... 11:00).
* One knob per review, each move <= 1/4 of the knob's range, 5 trading days of cooldown between changes; every
  change stores its evidence (sample sizes, statistic, before/after).
* Auto-revert ............ once 15 trades have closed after a change, if their mean r is >= 0.10 below the 15
                           before it with a one-sided Welch p < 0.10, the change is undone (allowed during the
                           cooldown: it restores a known earlier state).
* Freeze / revert / reset  owner controls; freezing stops automatic changes (including auto-revert).
* Mode guard ............. learning never runs for alpaca_live.

Rule evidence is taken from trades closed after the most recent change of the knob(s) the rule would move, so a
change is never justified twice by the same trades.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import logging
import math
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from kwanttrades.app.session import is_trading_day
from kwanttrades.config import Config
from kwanttrades.execution.exits import parse_hhmm
from kwanttrades.learning import stats
from kwanttrades.types import MULTIPLIER, NY, TradeIdea, now_ny

log = logging.getLogger("kwanttrades.learning")

MIN_TOTAL = 20
MIN_STRATEGY = 10
MIN_CALIBRATION = 20
MIN_DIRECTION = 20
MIN_EXITS = 25
MIN_TOD_BUCKET = 15
AUTO_REVERT_N = 15
COOLDOWN_TRADING_DAYS = 5
EXIT_MIN_GAIN = 0.03
TOD_MIN_GAP = 0.10
REVERT_MIN_GAP = 0.10
LEARNING_MODES = ("paper", "alpaca_paper")
ENTRY_FLOOR_MIN = 9 * 60 + 45
TOD_STEP_MIN = 15

try:  # pristine engine defaults (the engine may already carry learned values)
    from kwanttrades.strategy.engine import DRIFT_K as _DRIFT_K, VOL_SHRINK as _VOL_SHRINK
except Exception:  # pragma: no cover
    _DRIFT_K, _VOL_SHRINK = 0.35, 0.5
ENGINE_DEFAULTS = {"min_confidence": 0.30, "drift_k": _DRIFT_K, "vol_shrink": _VOL_SHRINK}
_IDEA_FIELDS = {f.name: f.default for f in dataclasses.fields(TradeIdea)}
EXIT_DEFAULTS = {"stop_loss_pct": float(_IDEA_FIELDS["stop_loss_pct"]), "take_profit_pct": float(_IDEA_FIELDS["take_profit_pct"])}

STRATEGY_NAMES = {
    "long_call": "Long call", "long_put": "Long put", "bull_call_spread": "Bull call spread",
    "bear_put_spread": "Bear put spread", "bull_put_spread": "Bull put spread", "bear_call_spread": "Bear call spread",
    "iron_condor": "Iron condor", "long_straddle": "Long straddle", "long_strangle": "Long strangle",
}


@dataclass(frozen=True)
class Knob:
    key: str
    label: str
    lo: float
    hi: float
    fmt: str  # "num" | "pct" | "time" | "weight"
    plain: str

    @property
    def max_step(self) -> float:
        return (self.hi - self.lo) / 4.0


def _hm(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60:02d}:{m % 60:02d}"


def _min(hhmm: str) -> int:
    t = parse_hhmm(hhmm)
    return t.hour * 60 + t.minute


def knob_table(min_conf_default: float = ENGINE_DEFAULTS["min_confidence"]) -> dict[str, Knob]:
    return {
        "min_direction": Knob("min_direction", "Minimum direction strength", 0.20, 0.45, "num",
                              "How strong the combined signal must be before it bets on a direction."),
        "min_confidence": Knob("min_confidence", "Minimum signal confidence", float(min_conf_default), 0.60, "pct",
                               "How consistent the signals must be before it bets on a direction."),
        "min_vol_edge": Knob("min_vol_edge", "Minimum volatility edge", 0.05, 0.25, "pct",
                             "How cheap or expensive options must look before it trades volatility."),
        "drift_k": Knob("drift_k", "Trust in the direction signal", 0.15, 0.50, "num",
                        "How far the model shifts its forecast in the signal's direction."),
        "vol_shrink": Knob("vol_shrink", "Trust in its own volatility forecast", 0.30, 0.70, "pct",
                           "Weight on its own volatility forecast versus the options market's."),
        "stop_loss_pct": Knob("stop_loss_pct", "Stop-loss", 0.25, 0.60, "pct",
                              "Close a trade once it has lost this share of its maximum loss."),
        "take_profit_pct": Knob("take_profit_pct", "Take-profit", 0.35, 0.75, "pct",
                                "Close a trade once it has made this share of its maximum profit."),
        "no_entry_before": Knob("no_entry_before", "No new trades before", float(ENTRY_FLOOR_MIN), 11 * 60.0, "time",
                                "The earliest time of day a new trade may be opened (New York time)."),
    }


def weight_knob(strategy: str) -> Knob:
    name = STRATEGY_NAMES.get(strategy, strategy.replace("_", " ").capitalize())
    return Knob(f"weight:{strategy}", f"{name} weight", 0.0, 1.0, "weight",
                f"How much the system favours {name.lower()} trades (0 = paused).")


class ScanOverlay:
    """Reads learned scan thresholds first and everything else from the live config section (so Settings edits of
    other fields keep flowing through)."""

    def __init__(self, base: Any, overrides: dict[str, float]):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_ov", dict(overrides))

    def __getattr__(self, name: str) -> Any:
        ov = object.__getattribute__(self, "_ov")
        if name in ov:
            return ov[name]
        return getattr(object.__getattribute__(self, "_base"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_base"), name, value)


# --------------------------------------------------------------------------- persistence
class LearnedStore:
    """data/learned.json: {"version", "rev", "frozen", "values", "history"} (path None = memory only)."""

    def __init__(self, path: str | os.PathLike | None):
        self.path = Path(path) if path else None
        self.data = {"version": 1, "rev": 0, "frozen": False, "values": {}, "history": []}
        self.load()

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("cannot read %s (%s); learning starts from defaults", self.path, exc)
            return
        if not isinstance(raw, dict):
            return
        self.data["rev"] = int(raw.get("rev") or 0)
        self.data["frozen"] = bool(raw.get("frozen", False))
        vals = raw.get("values") if isinstance(raw.get("values"), dict) else {}
        self.data["values"] = {str(k): v for k, v in vals.items()}
        hist = raw.get("history") if isinstance(raw.get("history"), list) else []
        self.data["history"] = [h for h in hist if isinstance(h, dict) and h.get("id")]

    def save(self) -> None:
        self.data["rev"] = int(self.data.get("rev") or 0) + 1
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=1, allow_nan=False, default=str), encoding="utf-8")
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- small helpers
def _ts(x: Any) -> datetime | None:
    if isinstance(x, datetime):
        return x.astimezone(NY)
    if isinstance(x, str) and x:
        try:
            return datetime.fromisoformat(x).astimezone(NY)
        except ValueError:
            return None
    return None


def _after(trades: list[dict], since: datetime | None) -> list[dict]:
    if since is None:
        return list(trades)
    return [t for t in trades if t.get("closed") is not None and t["closed"] > since]


def _r(t: dict) -> float | None:
    return stats.finite_or_none(t.get("r"))


def trading_days_after(d0: date, d1: date) -> int:
    """Trading days d with d0 < d <= d1."""
    n, d = 0, d0 + timedelta(days=1)
    while d <= d1:
        if is_trading_day(d):
            n += 1
        d += timedelta(days=1)
    return n


def proposal(knob: str, before: float, after: float, rule: str, reason: str, evidence: dict) -> dict:
    return {"knob": knob, "before": before, "after": after, "rule": rule, "reason": reason, "evidence": evidence}


# --------------------------------------------------------------------------- rules (pure)
def rule_strategy_pause(trades: list[dict], weights: dict[str, float], since: dict[str, datetime | None]) -> dict | None:
    for strat in sorted({t.get("strategy") for t in trades if t.get("strategy")}):
        w = weights.get(strat, 1.0)
        if w <= 0:
            continue
        key = f"weight:{strat}"
        mine = [t for t in _after(trades, since.get(key)) if t.get("strategy") == strat]
        rs = [x for x in (_r(t) for t in mine) if x is not None]
        if len(rs) < MIN_STRATEGY:
            continue
        m, lo, hi, n = stats.mean_ci(rs, 0.80)
        if hi < 0:
            name = STRATEGY_NAMES.get(strat, strat)
            return proposal(key, w, 0.0, "strategy_pause",
                            f"Paused {name.lower()} trades: its last {n} trades lost {abs(m):.0%} of the amount risked on "
                            f"average, and even the hopeful end of the 80% range ({hi:+.0%}) is a loss.",
                            {"n": n, "mean_r": round(m, 4), "ci80": [round(lo, 4), round(hi, 4)], "strategy": strat})
    return None


def rule_strategy_raise(trades: list[dict], weights: dict[str, float], since: dict[str, datetime | None]) -> dict | None:
    for strat, w in sorted(weights.items()):
        if not 0 < w < 1:
            continue
        key = f"weight:{strat}"
        rs = [x for x in (_r(t) for t in _after(trades, since.get(key)) if t.get("strategy") == strat) if x is not None]
        if len(rs) < MIN_STRATEGY:
            continue
        m, lo, hi, n = stats.mean_ci(rs, 0.80)
        if lo > 0:
            name = STRATEGY_NAMES.get(strat, strat)
            return proposal(key, w, min(1.0, w + 0.25), "strategy_raise",
                            f"Gave {name.lower()} trades more weight: its last {n} trades made {m:.0%} of the amount risked "
                            f"on average, and even the cautious end of the 80% range ({lo:+.0%}) is a gain.",
                            {"n": n, "mean_r": round(m, 4), "ci80": [round(lo, 4), round(hi, 4)], "strategy": strat})
    return None


def pop_bins(trades: list[dict]) -> list[dict]:
    edges = [(0.0, 0.40), (0.40, 0.55), (0.55, 0.70), (0.70, 1.01)]
    out = []
    for a, b in edges:
        sel = [t for t in trades if a <= t["features"]["pop"] < b]
        if sel:
            out.append({"range": [a, min(b, 1.0)], "n": len(sel),
                        "predicted": round(sum(t["features"]["pop"] for t in sel) / len(sel), 4),
                        "realized": round(sum(1 for t in sel if t.get("win")) / len(sel), 4)})
    return out


def rule_calibration(trades: list[dict], values: dict, knobs: dict[str, Knob], since: dict[str, datetime | None]) -> dict | None:
    s = max([x for x in (since.get("vol_shrink"), since.get("drift_k")) if x is not None], default=None)
    sel = [t for t in _after(trades, s) if stats.finite_or_none((t.get("features") or {}).get("pop")) is not None]
    n = len(sel)
    if n < MIN_CALIBRATION:
        return None
    k = sum(1 for t in sel if t.get("win"))
    p_bar = sum(t["features"]["pop"] for t in sel) / n
    p = stats.binom_p_less(k, n, p_bar)
    if not (k / n < p_bar and p < 0.10):
        return None
    ev = {"n": n, "wins": k, "predicted_win_rate": round(p_bar, 4), "realized_win_rate": round(k / n, 4),
          "p_value": round(p, 4), "test": "one-sided exact binomial", "bins": pop_bins(sel)}
    why = (f"it expected about {p_bar:.0%} of trades to win, but only {k} of {n} ({k / n:.0%}) did "
           f"(a gap this size by luck alone: p = {p:.2f}).")
    vs, dk = values["vol_shrink"], values["drift_k"]
    if vs - 0.05 >= knobs["vol_shrink"].lo - 1e-9:
        return proposal("vol_shrink", vs, round(vs - 0.05, 4), "calibration",
                        "Trusts its own volatility forecast less and the options market more: " + why, ev)
    if dk - 0.05 >= knobs["drift_k"].lo - 1e-9:
        return proposal("drift_k", dk, round(dk - 0.05, 4), "calibration",
                        "Trusts its direction signal less: " + why, ev)
    return None


def rule_direction(trades: list[dict], values: dict, knobs: dict[str, Knob], since: dict[str, datetime | None]) -> dict | None:
    s = max([x for x in (since.get("drift_k"), since.get("min_direction")) if x is not None], default=None)
    sel = [t for t in _after(trades, s) if (t.get("features") or {}).get("direction_call")
           and (t.get("outcome") or {}).get("direction_right") is not None]
    n = len(sel)
    if n < MIN_DIRECTION:
        return None
    k = sum(1 for t in sel if t["outcome"]["direction_right"])
    lo, hi = stats.proportion_ci(k, n, 0.90)
    if hi >= 0.5:
        return None
    ev = {"n": n, "right": k, "hit_rate": round(k / n, 4), "ci90": [round(lo, 4), round(hi, 4)],
          "test": "90% Clopper-Pearson interval"}
    why = (f"only {k} of {n} direction calls were right ({k / n:.0%}); even the hopeful end of the 90% range "
           f"({hi:.0%}) is worse than a coin flip.")
    dk, md = values["drift_k"], values["min_direction"]
    if dk - 0.05 >= knobs["drift_k"].lo - 1e-9:
        return proposal("drift_k", dk, round(dk - 0.05, 4), "direction", "Trusts its direction signal less: " + why, ev)
    if md + 0.05 <= knobs["min_direction"].hi + 1e-9:
        return proposal("min_direction", md, round(md + 0.05, 4), "direction",
                        "Needs a stronger direction signal before betting: " + why, ev)
    return None


def replay_trade(t: dict, path: list[dict], stop: float, take: float, flatten_time: str = "15:55") -> float | None:
    """r (P&L / max loss, per contract-set, at mid marks) of trade `t` had it used stop/take, replayed along its
    mark path. Exits that the replay cannot re-derive (signal turned, kill switch, by hand, end of day) happen at
    the same moment they really did; stop/take exits continue along the post-exit tracking. None if the path does
    not cover what the replay needs."""
    f, o = t.get("features") or {}, t.get("outcome") or {}
    entry = stats.finite_or_none(o.get("entry_price"))
    ml = stats.finite_or_none(f.get("max_loss"))
    opened, closed = _ts(t.get("opened")), _ts(t.get("closed"))
    if entry is None or not ml or ml <= 0 or opened is None or closed is None:
        return None
    pts = sorted((p for p in path if p.get("mark") is not None and _ts(p["ts"]) and _ts(p["ts"]) > opened),
                 key=lambda p: _ts(p["ts"]))
    if not pts:
        return None
    reason = t.get("exit_reason") or ""
    fixed = closed if reason not in ("stop_loss", "take_profit") else None
    if fixed is None and not any(p.get("phase") == "post" for p in pts):
        return None  # a stop/take exit with no post-exit tracking cannot be replayed honestly
    mp = stats.finite_or_none(f.get("max_profit")) if f.get("max_profit") is not None else None
    if mp is None or not math.isfinite(mp):
        target = take * max(entry, 0.0) * MULTIPLIER
    else:
        target = take * mp
    end = None
    if f.get("intraday"):
        end = datetime.combine(opened.date(), parse_hhmm(flatten_time), tzinfo=NY)
    last = None
    for p in pts:
        ts = _ts(p["ts"])
        if end is not None and ts > end:
            break
        if fixed is not None and ts > fixed:
            break
        last = p
        pnl = (p["mark"] - entry) * MULTIPLIER
        if -pnl >= stop * ml - 1e-9 or (target > 0 and pnl >= target - 1e-9):
            return pnl / ml
    if last is None:
        return None
    return (last["mark"] - entry) * MULTIPLIER / ml


def exit_grid(stop: float, take: float, knobs: dict[str, Knob]) -> list[tuple[float, float]]:
    ks, kt = knobs["stop_loss_pct"], knobs["take_profit_pct"]
    out = set()
    for ds in (-0.05, 0.0, 0.05):
        for dt in (-0.10, -0.05, 0.0, 0.05, 0.10):
            s, t = round(stop + ds, 4), round(take + dt, 4)
            if ks.lo - 1e-9 <= s <= ks.hi + 1e-9 and kt.lo - 1e-9 <= t <= kt.hi + 1e-9 and (s, t) != (round(stop, 4), round(take, 4)):
                out.add((s, t))
    return sorted(out)


def rule_exits(trades: list[dict], paths: dict[str, list[dict]], values: dict, knobs: dict[str, Knob],
               flatten_time: str = "15:55") -> dict | None:
    cur_s, cur_t = values["stop_loss_pct"], values["take_profit_pct"]
    rows = []
    for t in sorted(trades, key=lambda x: x["closed"]):
        base = replay_trade(t, paths.get(t["position_id"], []), cur_s, cur_t, flatten_time)
        if base is not None:
            rows.append((t, base))
    n = len(rows)
    if n < MIN_EXITS:
        return None
    split = (2 * n) // 3
    best = None
    for s, tk in exit_grid(cur_s, cur_t, knobs):
        diffs = []
        for t, base in rows:
            c = replay_trade(t, paths.get(t["position_id"], []), s, tk, flatten_time)
            diffs.append(None if c is None else c - base)
        if any(d is None for d in diffs):
            continue
        train, test = diffs[:split], diffs[split:]
        m, lo, _, _ = stats.mean_ci(train, 0.80)
        m_test = sum(test) / len(test) if test else 0.0
        if m >= EXIT_MIN_GAIN and lo > 0 and m_test >= 0:
            if best is None or m > best[2]:
                best = (s, tk, m, lo, m_test)
    if best is None:
        return None
    s, tk, m, lo, m_test = best
    ev = {"n": n, "train_n": split, "test_n": n - split, "train_gain": round(m, 4), "train_gain_ci80_low": round(lo, 4),
          "test_gain": round(m_test, 4), "before": {"stop_loss_pct": cur_s, "take_profit_pct": cur_t},
          "after": {"stop_loss_pct": s, "take_profit_pct": tk}, "test": "walk-forward replay on recorded mark paths"}
    return proposal("exits", {"stop_loss_pct": cur_s, "take_profit_pct": cur_t}, {"stop_loss_pct": s, "take_profit_pct": tk},
                    "exits",
                    f"Moved the stop-loss to {s:.0%} and the take-profit to {tk:.0%}: replaying {n} closed trades along their "
                    f"recorded prices, this did {m:+.1%} of the amount risked per trade better on the older two-thirds and "
                    f"{m_test:+.1%} on the most recent third.", ev)


def rule_time_of_day(trades: list[dict], values: dict, knobs: dict[str, Knob]) -> dict | None:
    cur = int(values["no_entry_before"])
    if cur + TOD_STEP_MIN > knobs["no_entry_before"].hi + 1e-9:
        return None
    off = cur - ENTRY_FLOOR_MIN
    early, late = [], []
    for t in trades:
        b = (t.get("features") or {}).get("entry_bucket_min")
        r = _r(t)
        if b is None or r is None or b < off:
            continue
        (early if b < off + TOD_STEP_MIN else late).append(r)
    if len(early) < MIN_TOD_BUCKET or len(late) < MIN_TOD_BUCKET:
        return None
    me, ml = sum(early) / len(early), sum(late) / len(late)
    p = stats.welch_p_less(early, late)
    if not (me < 0 and ml - me >= TOD_MIN_GAP and p < 0.10):
        return None
    new = cur + TOD_STEP_MIN
    ev = {"n_early": len(early), "n_later": len(late), "mean_r_early": round(me, 4), "mean_r_later": round(ml, 4),
          "p_value": round(p, 4), "window": [_hm(cur), _hm(new)], "test": "one-sided Welch t-test"}
    return proposal("no_entry_before", float(cur), float(new), "time_of_day",
                    f"No new trades before {_hm(new)}: the {len(early)} trades opened between {_hm(cur)} and {_hm(new)} lost "
                    f"{abs(me):.0%} of the amount risked on average, versus {ml:+.0%} for the {len(late)} later ones "
                    f"(p = {p:.2f}).", ev)


def rule_auto_revert(trades: list[dict], change: dict) -> tuple[bool, dict] | None:
    """(revert?, evidence) once AUTO_REVERT_N trades have closed after `change`; None before that."""
    ts = _ts(change.get("ts"))
    if ts is None:
        return None
    after = [t for t in trades if t.get("closed") and t["closed"] > ts]
    before = [t for t in trades if t.get("closed") and t["closed"] <= ts]
    if len(after) < AUTO_REVERT_N or len(before) < AUTO_REVERT_N:
        return None
    a = [x for x in (_r(t) for t in after[:AUTO_REVERT_N]) if x is not None]
    b = [x for x in (_r(t) for t in before[-AUTO_REVERT_N:]) if x is not None]
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    p = stats.welch_p_less(a, b)
    worse = (mb - ma) >= REVERT_MIN_GAP and p < 0.10
    return worse, {"n_after": len(a), "n_before": len(b), "mean_r_after": round(ma, 4), "mean_r_before": round(mb, 4),
                   "p_value": round(p, 4), "test": "one-sided Welch t-test"}


# --------------------------------------------------------------------------- the learner
class LearningError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Learner:
    def __init__(self, cfg: Config, path: str | os.PathLike | None = None, journal: Any = None,
                 clock: Callable[[], datetime] | None = None):
        self.cfg = cfg
        self.journal = journal
        self.clock = clock or now_ny
        self.store = LearnedStore(path)
        self.knobs = knob_table()
        self._lock = threading.RLock()
        self.last_review: dict = {"ts": None, "note": "No review yet."}
        self.listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------------ values
    def defaults(self) -> dict[str, float]:
        c = self.cfg
        return {"min_direction": float(c.scan.min_direction), "min_vol_edge": float(c.scan.min_vol_edge),
                **{k: float(v) for k, v in ENGINE_DEFAULTS.items()}, **EXIT_DEFAULTS,
                "no_entry_before": float(_min(c.risk.entry_window_start))}

    def knob(self, key: str) -> Knob:
        return weight_knob(key.split(":", 1)[1]) if key.startswith("weight:") else self.knobs[key]

    def _stored(self) -> dict[str, float]:
        """Validated overrides from the store, clamped into bounds; garbage is ignored."""
        out = {}
        for k, v in (self.store.data.get("values") or {}).items():
            try:
                kn = self.knob(k)
            except KeyError:
                continue
            if k == "no_entry_before" and isinstance(v, str):
                try:
                    v = _min(v)
                except (ValueError, AttributeError):
                    continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(f):
                continue
            out[k] = min(max(f, kn.lo), kn.hi)
        return out

    def overrides(self) -> dict[str, float]:
        with self._lock:
            return self._stored()

    def values(self) -> dict[str, float]:
        with self._lock:
            v = self.defaults()
            v.update({k: x for k, x in self._stored().items() if not k.startswith("weight:")})
            return v

    def weights(self) -> dict[str, float]:
        with self._lock:
            return {k.split(":", 1)[1]: x for k, x in self._stored().items() if k.startswith("weight:")}

    def strategy_weight(self, strategy: str) -> float:
        return self.weights().get(strategy, 1.0)

    @property
    def frozen(self) -> bool:
        return bool(self.store.data.get("frozen"))

    # ------------------------------------------------------------------ application
    def apply_engine(self, engine: Any) -> None:
        """Set the learned engine knobs (or their defaults) on a StrategyEngine-like object."""
        if engine is None:
            return
        v, ov = self.values(), self.overrides()
        scan_ov = {k: v[k] for k in ("min_direction", "min_vol_edge") if k in ov}
        if hasattr(engine, "cfg") and isinstance(getattr(engine, "cfg", None), Config):
            base = engine.__dict__.get("_kt_base_cfg") or engine.cfg
            engine.__dict__["_kt_base_cfg"] = base
            engine.cfg = dataclasses.replace(base, scan=ScanOverlay(base.scan, scan_ov)) if scan_ov else base
        for attr in ("min_confidence", "drift_k", "vol_shrink"):
            if hasattr(engine, attr):
                setattr(engine, attr, v[attr])

    def adjust_idea(self, idea: TradeIdea) -> str | None:
        """Apply learned exits and strategy weight to an idea. Returns a BLOCK reason, or None."""
        ov = self.overrides()
        if "stop_loss_pct" in ov:
            idea.stop_loss_pct = ov["stop_loss_pct"]
        if "take_profit_pct" in ov:
            idea.take_profit_pct = ov["take_profit_pct"]
        w = ov.get(f"weight:{idea.strategy}", 1.0)
        if w >= 1.0:
            return None
        name = STRATEGY_NAMES.get(idea.strategy, idea.strategy).lower()
        if w <= 0:
            return f"BLOCK: {name} trades are paused by learning (they kept losing; revert it on the Learning page)"
        idea.score = round(idea.score * w, 4)
        if idea.score < self.cfg.scan.min_score:
            return f"BLOCK: score {idea.score:.3f} is below the minimum after the learned {name} weight ({w:.2f})"
        return None

    def entry_start(self, base_start: str) -> str:
        """The later of the configured entry-window start and the learned "no new trades before"."""
        ov = self.overrides()
        if "no_entry_before" not in ov:
            return base_start
        return _hm(max(_min(base_start), ov["no_entry_before"]))

    # ------------------------------------------------------------------ history helpers
    def history(self) -> list[dict]:
        return list(self.store.data.get("history") or [])

    def last_change_ts(self) -> datetime | None:
        ts = [_ts(h.get("ts")) for h in self.history()]
        ts = [t for t in ts if t is not None]
        return max(ts) if ts else None

    def _since(self) -> dict[str, datetime | None]:
        """Most recent change time per knob (an exits change counts for both exit knobs)."""
        out: dict[str, datetime | None] = {}
        for h in self.history():
            ts = _ts(h.get("ts"))
            if ts is None:
                continue
            keys = []
            if h.get("kind") == "reset":
                keys = list(self.knobs) + [k for k in (h.get("before") or {}) if str(k).startswith("weight:")]
            elif h.get("knob") == "exits":
                keys = ["stop_loss_pct", "take_profit_pct"]
            elif h.get("knob"):
                keys = [h["knob"]]
            for k in keys:
                if out.get(k) is None or ts > out[k]:
                    out[k] = ts
        return out

    def _set(self, key: str, value: float | None) -> None:
        vals = self.store.data.setdefault("values", {})
        default = self.defaults().get(key, 1.0 if key.startswith("weight:") else None)
        if value is None or (default is not None and abs(float(value) - float(default)) < 1e-9):
            vals.pop(key, None)
        else:
            vals[key] = _hm(value) if key == "no_entry_before" else round(float(value), 4)

    def _notify(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception as exc:  # pragma: no cover
                log.warning("learning listener failed: %s", exc)

    def _event(self, kind: str, message: str, payload: dict, now: datetime) -> None:
        if self.journal is not None:
            try:
                self.journal.record_event(kind, message, payload, now)
            except Exception as exc:  # pragma: no cover
                log.warning("learning event journaling failed: %s", exc)

    # ------------------------------------------------------------------ applying a proposal
    def _bounded(self, key: str, before: float, after: float, exempt: bool = False) -> float | None:
        kn = self.knob(key)
        if not exempt:
            step = after - before
            if abs(step) > kn.max_step + 1e-9:
                after = before + math.copysign(kn.max_step, step)
        after = min(max(after, kn.lo), kn.hi)
        if abs(after - before) < 1e-9:
            return None
        return round(after, 4)

    def apply(self, prop: dict, now: datetime, n_total: int) -> dict | None:
        """Bound, record and persist one proposal. Returns the history entry (None if bounds leave nothing to do)."""
        key = prop["knob"]
        with self._lock:
            if key == "exits":
                b, a = prop["before"], prop["after"]
                s = self._bounded("stop_loss_pct", b["stop_loss_pct"], a["stop_loss_pct"])
                t = self._bounded("take_profit_pct", b["take_profit_pct"], a["take_profit_pct"])
                after = {"stop_loss_pct": s if s is not None else b["stop_loss_pct"],
                         "take_profit_pct": t if t is not None else b["take_profit_pct"]}
                if s is None and t is None:
                    return None
                self._set("stop_loss_pct", after["stop_loss_pct"])
                self._set("take_profit_pct", after["take_profit_pct"])
                before = b
            else:
                before = float(prop["before"])
                want = float(prop["after"])
                if (want - before) == 0:
                    return None
                after = self._bounded(key, before, want, exempt=prop["rule"] == "strategy_pause")
                if after is None or (after - before) * (want - before) <= 0:
                    return None
                self._set(key, after)
            entry = {"id": "chg-" + uuid.uuid4().hex[:10], "kind": "change", "ts": now.astimezone(NY).isoformat(),
                     "knob": key, "label": "Exits (stop-loss and take-profit)" if key == "exits" else self.knob(key).label,
                     "before": before, "after": after, "rule": prop["rule"], "reason": prop["reason"],
                     "evidence": prop.get("evidence") or {}, "trades_at_change": int(n_total), "status": "active",
                     "evaluated": False, "by": "learning"}
            self.store.data.setdefault("history", []).append(entry)
            self.store.save()
        self._event("learning_change", entry["reason"], entry, now)
        log.info("learning change: %s %s -> %s (%s)", key, before, after, prop["rule"])
        self._notify()
        return entry

    # ------------------------------------------------------------------ review
    def review(self, trades: list[dict], now: datetime | None = None, mode: str = "paper",
               paths: dict[str, list[dict]] | None = None) -> dict | None:
        """Look at every closed trade; change at most one knob. Returns the history entry of a change, or None."""
        now = (now or self.clock()).astimezone(NY)
        trades = sorted([t for t in trades if t.get("closed") is not None and t.get("mode") in LEARNING_MODES],
                        key=lambda t: t["closed"])
        n = len(trades)
        note = self._review(trades, now, mode, paths or {})
        self.last_review = {"ts": now.isoformat(), "trades": n, "note": note if isinstance(note, str) else note["reason"]}
        return note if isinstance(note, dict) else None

    def _review(self, trades: list[dict], now: datetime, mode: str, paths: dict) -> dict | str:
        if mode not in LEARNING_MODES:
            return f"Learning does not run in {mode} mode."
        if self.frozen:
            return "Learning is frozen: nothing is changed automatically."
        n = len(trades)
        if n < MIN_TOTAL:
            return f"Waiting for evidence: {n} of {MIN_TOTAL} closed trades needed before anything may change."
        # auto-revert first (safety action, allowed during the cooldown)
        for ch in reversed(self.history()):
            if ch.get("kind") != "change" or ch.get("status") != "active" or ch.get("evaluated"):
                continue
            res = rule_auto_revert(trades, ch)
            if res is None:
                continue
            worse, ev = res
            with self._lock:
                ch["evaluated"] = True
                ch["evaluation"] = {**ev, "result": "reverted" if worse else "kept"}
                self.store.save()
            if worse:
                why = (f"Undid “{ch.get('label')}” automatically: the {ev['n_after']} trades after it averaged "
                       f"{ev['mean_r_after']:+.0%} of the amount risked, versus {ev['mean_r_before']:+.0%} for the "
                       f"{ev['n_before']} before it.")
                out = self._revert(ch["id"], now, why, by="auto", evidence=ev)
                return out
        last = self.last_change_ts()
        if last is not None:
            days = trading_days_after(last.date(), now.date())
            if days < COOLDOWN_TRADING_DAYS:
                return (f"Cooling down: the last change was {days} trading day{'s' if days != 1 else ''} ago; "
                        f"{COOLDOWN_TRADING_DAYS} are needed between changes.")
        v, w, since = self.values(), self.weights(), self._since()
        rules: list[Callable[[], dict | None]] = [
            lambda: rule_strategy_pause(trades, w, since),
            lambda: rule_direction(trades, v, self.knobs, since),
            lambda: rule_calibration(trades, v, self.knobs, since),
            lambda: rule_exits(trades, paths, v, self.knobs, self.cfg.risk.flatten_time),
            lambda: rule_time_of_day(trades, v, self.knobs),
            lambda: rule_strategy_raise(trades, w, since),
        ]
        for rule in rules:
            try:
                prop = rule()
            except Exception as exc:  # a broken rule must never stop trading
                log.warning("learning rule failed: %s", exc)
                prop = None
            if prop is None:
                continue
            entry = self.apply(prop, now, n)
            if entry is not None:
                return entry
        return f"Reviewed {n} closed trades: no rule found clear enough evidence to change anything."

    # ------------------------------------------------------------------ owner controls
    def set_frozen(self, frozen: bool, now: datetime | None = None) -> None:
        now = (now or self.clock()).astimezone(NY)
        with self._lock:
            self.store.data["frozen"] = bool(frozen)
            self.store.save()
        self._event("learning_freeze", "Learning frozen by the owner." if frozen else "Learning unfrozen by the owner.",
                    {"frozen": bool(frozen)}, now)

    def revert(self, change_id: str, now: datetime | None = None) -> dict:
        now = (now or self.clock()).astimezone(NY)
        return self._revert(change_id, now, "Reverted by the owner.", by="owner")

    def _revert(self, change_id: str, now: datetime, reason: str, by: str, evidence: dict | None = None) -> dict:
        with self._lock:
            hist = self.store.data.setdefault("history", [])
            ch = next((h for h in hist if h.get("id") == change_id and h.get("kind") == "change"), None)
            if ch is None:
                raise LearningError(404, "No such change.")
            if ch.get("status") != "active":
                raise LearningError(409, "That change was already undone.")
            newer = [h for h in hist if h.get("kind") == "change" and h.get("status") == "active" and h is not ch
                     and h.get("knob") == ch.get("knob") and (_ts(h.get("ts")) or now) > (_ts(ch.get("ts")) or now)]
            if newer:
                raise LearningError(409, "A newer change to the same setting is active; revert that one first.")
            if ch["knob"] == "exits":
                self._set("stop_loss_pct", ch["before"]["stop_loss_pct"])
                self._set("take_profit_pct", ch["before"]["take_profit_pct"])
            else:
                self._set(ch["knob"], ch["before"])
            ch.update(status="reverted", reverted_at=now.isoformat(), revert_reason=reason, reverted_by=by)
            entry = {"id": "rev-" + uuid.uuid4().hex[:10], "kind": "revert", "ts": now.isoformat(), "knob": ch["knob"],
                     "label": ch.get("label"), "before": ch["after"], "after": ch["before"], "rule": "auto_revert" if by == "auto" else "owner_revert",
                     "reason": reason, "evidence": evidence or {}, "reverts": change_id, "status": "done", "by": by}
            hist.append(entry)
            self.store.save()
        self._event("learning_revert", reason, entry, now)
        self._notify()
        return entry

    def reset(self, now: datetime | None = None) -> dict:
        now = (now or self.clock()).astimezone(NY)
        with self._lock:
            before = copy.deepcopy(self.store.data.get("values") or {})
            for h in self.store.data.setdefault("history", []):
                if h.get("kind") == "change" and h.get("status") == "active":
                    h.update(status="reverted", reverted_at=now.isoformat(), revert_reason="Reset to defaults by the owner.",
                             reverted_by="owner")
            self.store.data["values"] = {}
            entry = {"id": "rst-" + uuid.uuid4().hex[:10], "kind": "reset", "ts": now.isoformat(), "knob": None,
                     "label": "All learned settings", "before": before, "after": {}, "rule": "owner_reset",
                     "reason": "Everything learned was reset to the defaults by the owner.", "evidence": {}, "status": "done",
                     "by": "owner"}
            self.store.data["history"].append(entry)
            self.store.save()
        self._event("learning_reset", entry["reason"], entry, now)
        self._notify()
        return entry

    # ------------------------------------------------------------------ display
    def fmt(self, key: str, v: Any) -> str:
        if isinstance(v, dict):
            return f"stop {v.get('stop_loss_pct', 0):.0%} / take {v.get('take_profit_pct', 0):.0%}"
        if v is None:
            return "–"
        if key == "exits":
            return str(v)
        kn = self.knob(key) if key else None
        f = float(v)
        if kn is None:
            return f"{f:g}"
        if kn.fmt == "time":
            return _hm(f)
        if kn.fmt == "pct":
            return f"{f:.0%}"
        if kn.fmt == "weight":
            return "paused" if f <= 0 else f"{f:.2f}×"
        return f"{f:.2f}"

    def settings(self) -> list[dict]:
        d, v, ov = self.defaults(), self.values(), self.overrides()
        rows = []
        for key, kn in self.knobs.items():
            rows.append({"key": key, "label": kn.label, "plain": kn.plain, "value": v[key], "default": d[key],
                         "display_value": self.fmt(key, v[key]), "display_default": self.fmt(key, d[key]),
                         "bounds": [kn.lo, kn.hi], "changed": key in ov})
        for strat, w in sorted(self.weights().items()):
            kn = weight_knob(strat)
            rows.append({"key": kn.key, "label": kn.label, "plain": kn.plain, "value": w, "default": 1.0,
                         "display_value": self.fmt(kn.key, w), "display_default": "1.00×", "bounds": [0.0, 1.0],
                         "changed": True})
        return rows

    def status(self, n_trades: int, now: datetime | None = None) -> dict:
        now = (now or self.clock()).astimezone(NY)
        last = self.last_change_ts()
        cooldown_until = None
        if last is not None:
            d, k = last.date(), 0
            while k < COOLDOWN_TRADING_DAYS:
                d += timedelta(days=1)
                if is_trading_day(d):
                    k += 1
            cooldown_until = d.isoformat() if d > now.date() else None
        return {"closed_trades": int(n_trades), "min_trades": MIN_TOTAL, "frozen": self.frozen,
                "cooldown_until": cooldown_until, "last_change": None if last is None else last.isoformat(),
                "last_review": self.last_review, "rev": int(self.store.data.get("rev") or 0)}


def learned_path_for(journal: Any) -> Path | None:
    """data/learned.json next to the journal; None (memory only) for an in-memory journal."""
    db = str(getattr(journal, "db_path", ":memory:") or ":memory:")
    if db == ":memory:":
        return None
    return Path(db).expanduser().parent / "learned.json"

"""Trade post-mortems: what we knew at entry, what happened, and one plain-language reason for every loss.

Pure functions (no I/O) so each cause can be tested in isolation.

Causes (``CAUSES``), checked in this order for a LOSS; the first that fits is the primary cause:

1. ``gap``               the underlying jumped overnight against the trade by >= GAP_SIGMA daily sigmas.
2. ``stopped_recovered`` stopped out, and the post-exit tracking shows the structure later got back to at least
                         break-even before its expiry (intraday ideas: before that day's flatten time). Only
                         knowable after the exit, so the classification is updated when the tracking sees it.
3. ``costs``             spread + commissions were the difference: the trade was flat or profitable before costs.
4. ``direction_wrong``   a directional bet and the stock moved the other way.
5. ``too_slow``          a long-premium directional bet, the stock moved the right way but not far/fast enough
                         to beat time decay.
6. ``vol_over``          a long-volatility bet (straddle/strangle, or bought options because they looked cheap)
                         and the stock moved less than the model expected.
7. ``vol_under``         (addition) a short-volatility bet (iron condor) and the stock moved MORE than expected.
8. ``unknown``           (addition) not enough was recorded (e.g. trades from before this feature) to tell.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Any

from kwanttrades.execution.exits import idea_direction, is_intraday, parse_hhmm
from kwanttrades.types import MULTIPLIER, NY, Position, Side, TradeIdea

CAUSES = {
    "direction_wrong": "Direction was wrong",
    "too_slow": "Right direction, but the move came too slowly (time decay)",
    "vol_over": "The stock moved less than the model expected (volatility overestimated)",
    "vol_under": "The stock moved more than the model expected (volatility underestimated)",
    "stopped_recovered": "Stopped out, then it recovered",
    "costs": "Costs ate the edge",
    "gap": "Big overnight/news gap",
    "unknown": "Not enough data to tell",
    "win": "Win",
}
GAP_SIGMA = 2.0  # an overnight jump this many daily sigmas against the trade is a "gap" loss
VOL_RATIO_LOW = 0.8  # realised move below this share of the model's expected move = "moved less than expected"
VOL_RATIO_HIGH = 1.25
SESSION_MINUTES = 390.0
OVERNIGHT_SESSIONS = 0.25  # variance credited to each overnight gap, in sessions
LONG_VOL = {"long_straddle", "long_strangle"}
SHORT_VOL = {"iron_condor"}
EXIT_WORDS = {
    "take_profit": "Took profit", "stop_loss": "Hit the stop-loss", "time_flatten": "Closed at the end of the day",
    "signal_invalidated": "Closed when the signal turned", "dte_exit": "Closed a day before expiry",
    "expired": "Settled at expiry", "kill_switch": "Closed by the kill switch", "manual_close": "Closed by hand",
    "manual_flatten": "Closed by hand", "flatten_all": "Closed by hand",
}


# --------------------------------------------------------------------------- time helpers
def bucket_minutes(t: datetime) -> int:
    """Minutes after 09:45 New York (negative before)."""
    t = t.astimezone(NY)
    return (t.hour * 60 + t.minute) - (9 * 60 + 45)


def sessions_between(t0: datetime, t1: datetime) -> float:
    """Variance clock between two times, in trading sessions: market minutes / 390 plus OVERNIGHT_SESSIONS for
    every overnight/weekend gap crossed. Floored at 5 minutes so a quick trade still has a defined move size."""
    from kwanttrades.app.session import is_trading_day, session_close_time

    t0, t1 = t0.astimezone(NY), t1.astimezone(NY)
    if t1 <= t0:
        return 5.0 / SESSION_MINUTES
    total = 0.0
    d = t0.date()
    first = True
    while d <= t1.date():
        if is_trading_day(d):
            o = datetime.combine(d, time(9, 30), tzinfo=NY)
            c = datetime.combine(d, session_close_time(d), tzinfo=NY)
            a, b = max(o, t0), min(c, t1)
            if b > a:
                total += (b - a).total_seconds() / 60.0 / SESSION_MINUTES
            if not first and t0 < o <= t1:
                total += OVERNIGHT_SESSIONS
            first = False
        d += timedelta(days=1)
    return max(total, 5.0 / SESSION_MINUTES)


def _flatten_cutoff(idea_intraday: bool, opened: datetime, flatten_time: str = "15:55") -> datetime | None:
    if not idea_intraday:
        return None
    return datetime.combine(opened.astimezone(NY).date(), parse_hhmm(flatten_time), tzinfo=NY)


# --------------------------------------------------------------------------- features at entry
def round_trip_cost(idea: TradeIdea, slip: float = 0.5, commission: float = 0.65) -> float:
    """Estimated $ per contract-set to get in and out: crossing toward the limit on entry, slip x half-spread on
    exit, commissions both ways (same cost model as the strategy engine)."""
    hs = sum(0.5 * l.quote.spread * l.ratio for l in idea.legs)
    n_legs = sum(l.ratio for l in idea.legs)
    entry_cross = max(float(idea.entry_limit) - float(idea.net_price), 0.0)
    return (entry_cross + slip * hs) * MULTIPLIER + 2.0 * commission * n_legs


def entry_features(idea: TradeIdea, *, now: datetime, contracts: int, composite: Any = None, orb_state: str | None = None,
                   spot: float | None = None, analysis: Any = None, risk_multiplier: float = 1.0,
                   slip: float = 0.5, commission: float = 0.65) -> dict:
    """Everything the post-mortem needs from the moment of entry (JSON-friendly)."""
    exp = idea.expiry
    ev = None
    vols = getattr(analysis, "expiry_vols", None) or {}
    if exp in vols:
        ev = vols[exp]
    vv = getattr(analysis, "vol_view", None)
    entry_iv = _f(ev.get("atm")) if ev else _f(getattr(vv, "atm_iv", None))
    vol_edge = _f(ev.get("edge")) if ev else _f(getattr(vv, "vol_edge", None))
    model_sd = _f(ev.get("sigma_daily")) if ev else None
    if model_sd is None and entry_iv is not None:
        fc = _f(getattr(vv, "forecast_rv", None))
        model_sd = (fc if fc else entry_iv) / math.sqrt(252.0)
    cost = round_trip_cost(idea, slip, commission)
    return {
        "strategy": idea.strategy, "symbol": idea.symbol, "direction_call": idea_direction(idea),
        "composite_direction": _f(getattr(composite, "direction", None)),
        "composite_confidence": _f(getattr(composite, "confidence", None)),
        "vol_edge": vol_edge, "risk_multiplier": _f(risk_multiplier), "orb_state": orb_state,
        "entry_time": now.astimezone(NY).isoformat(), "entry_bucket_min": bucket_minutes(now),
        "dte_entry": (exp - now.astimezone(NY).date()).days, "model_ev": _f(idea.expected_value), "pop": _f(idea.pop),
        "score": _f(idea.score), "entry_iv": entry_iv, "model_sigma_daily": model_sd, "entry_spot": _f(spot),
        "cost_per_set": round(cost, 2), "cost_frac": round(cost / idea.max_loss, 4) if idea.max_loss > 0 else None,
        "max_loss": _f(idea.max_loss), "max_profit": _f(idea.max_profit), "contracts": int(contracts), "is_credit": bool(idea.is_credit),
        "intraday": bool(is_intraday(idea)), "stop_loss_pct": _f(idea.stop_loss_pct),
        "take_profit_pct": _f(idea.take_profit_pct), "recorded": True,
    }


def fallback_features(pos: Position, slip: float = 0.5, commission: float = 0.65) -> dict:
    """What can still be derived for a trade opened before features were recorded (no signal or spot data)."""
    idea = pos.idea
    cost = round_trip_cost(idea, slip, commission)
    return {
        "strategy": idea.strategy, "symbol": idea.symbol, "direction_call": idea_direction(idea),
        "composite_direction": None, "composite_confidence": None, "vol_edge": None, "risk_multiplier": None,
        "orb_state": None, "entry_time": pos.opened.astimezone(NY).isoformat(),
        "entry_bucket_min": bucket_minutes(pos.opened), "dte_entry": (idea.expiry - pos.opened.astimezone(NY).date()).days,
        "model_ev": _f(idea.expected_value), "pop": _f(idea.pop), "score": _f(idea.score), "entry_iv": None,
        "model_sigma_daily": None, "entry_spot": None, "cost_per_set": round(cost, 2),
        "cost_frac": round(cost / idea.max_loss, 4) if idea.max_loss > 0 else None, "max_loss": _f(idea.max_loss),
        "max_profit": _f(idea.max_profit), "contracts": int(pos.contracts), "is_credit": bool(idea.is_credit), "intraday": bool(is_intraday(idea)),
        "stop_loss_pct": _f(idea.stop_loss_pct), "take_profit_pct": _f(idea.take_profit_pct), "recorded": False,
    }


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# --------------------------------------------------------------------------- outcome
def outcome(pos: Position, feats: dict, marks: list[dict], exit_spot: float | None,
            flatten_time: str = "15:55") -> dict:
    """Realised result and the evidence numbers the classifier uses."""
    idea = pos.idea
    ml_total = idea.max_loss * pos.contracts
    pnl = float(pos.realized_pnl)
    r = pnl / ml_total if ml_total > 0 else 0.0
    opened, closed = pos.opened.astimezone(NY), (pos.closed or pos.opened).astimezone(NY)
    hold_min = (closed - opened).total_seconds() / 60.0
    s0 = feats.get("entry_spot")
    if s0 is None:  # first recorded underlying while open
        s0 = next((m["underlying"] for m in marks if m.get("phase") == "open" and m.get("underlying")), None)
    if exit_spot is None:
        before = [m for m in marks if m.get("phase") == "open" and m.get("underlying") and m["ts"] <= closed]
        exit_spot = before[-1]["underlying"] if before else None
    sessions = sessions_between(opened, closed)
    move_pct = move_sigma = rv_iv = rv_model = None
    iv = feats.get("entry_iv")
    if s0 and exit_spot and s0 > 0 and exit_spot > 0:
        lr = math.log(exit_spot / s0)
        move_pct = exit_spot / s0 - 1.0
        if iv:
            imp = iv / math.sqrt(252.0) * math.sqrt(sessions)
            move_sigma = lr / imp if imp > 0 else None
            rv_iv = abs(lr) / imp if imp > 0 else None
        msd = feats.get("model_sigma_daily")
        if msd:
            exp_move = msd * math.sqrt(sessions)
            rv_model = abs(lr) / exp_move if exp_move > 0 else None
    dcall = int(feats.get("direction_call") or 0)
    dir_right = None
    if dcall and move_pct is not None and move_pct != 0:
        dir_right = (move_pct > 0) == (dcall > 0)
    cost_total = (feats.get("cost_per_set") or 0.0) * pos.contracts
    gap = overnight_gap(marks, opened, closed, dcall, idea.strategy, feats.get("model_sigma_daily") or (iv / math.sqrt(252.0) if iv else None))
    post = post_exit(pos, marks, flatten_time)
    return {
        "realized_pnl": round(pnl, 2), "max_loss_total": round(ml_total, 2), "r": round(r, 4),
        "exit_reason": pos.exit_reason, "hold_minutes": round(hold_min, 1), "sessions_held": round(sessions, 3),
        "entry_spot": _f(s0), "exit_spot": _f(exit_spot), "move_pct": _f(move_pct), "move_sigma": _f(move_sigma),
        "direction_right": dir_right, "realized_vs_implied": _f(rv_iv), "realized_vs_model": _f(rv_model),
        "cost_total": round(cost_total, 2), "gross_pnl": round(pnl + cost_total, 2), "gap_sigma": _f(gap),
        "best_post_pnl": post["best_pnl"], "recovered": post["recovered"], "post_marks": post["n"],
        "entry_price": float(pos.entry_price), "exit_price": _f(pos.exit_price),
    }


def overnight_gap(marks: list[dict], opened: datetime, closed: datetime, dcall: int, strategy: str,
                  sigma_daily: float | None) -> float | None:
    """Largest overnight underlying jump AGAINST the position while it was open, in daily sigmas (positive =
    against). Direction-neutral long-vol trades are never hurt by a jump; short-vol trades by either side."""
    if not sigma_daily or sigma_daily <= 0:
        return None
    pts = [m for m in marks if m.get("phase") == "open" and m.get("underlying") and opened <= m["ts"] <= closed]
    worst = None
    for a, b in zip(pts, pts[1:]):
        if b["ts"].astimezone(NY).date() == a["ts"].astimezone(NY).date():
            continue
        lr = math.log(b["underlying"] / a["underlying"]) / sigma_daily
        if strategy in LONG_VOL:
            against = 0.0
        elif strategy in SHORT_VOL or not dcall:
            against = abs(lr)
        else:
            against = -lr * dcall
        worst = against if worst is None else max(worst, against)
    return worst


def post_exit(pos: Position, marks: list[dict], flatten_time: str = "15:55") -> dict:
    """Best P&L the structure reached AFTER the exit (from post-exit tracking), within what the rules would have
    allowed (intraday ideas: until that day's flatten time)."""
    idea = pos.idea
    cutoff = _flatten_cutoff(is_intraday(idea), pos.opened, flatten_time)
    pts = [m for m in marks if m.get("phase") == "post" and m.get("mark") is not None
           and (cutoff is None or m["ts"] <= cutoff)]
    if not pts:
        return {"best_pnl": None, "recovered": False, "n": 0}
    best = max((m["mark"] - pos.entry_price) * MULTIPLIER * pos.contracts for m in pts)
    return {"best_pnl": round(best, 2), "recovered": best >= 0.0, "n": len(pts)}


# --------------------------------------------------------------------------- classification
def _pct(x: float | None) -> str:
    if x is None:
        return "–"
    return f"{abs(x) * 100:.2f}%" if abs(x) < 0.001 else f"{abs(x) * 100:.1f}%"


def _money(x: float) -> str:
    return f"${abs(x):,.2f}"


def classify(strategy: str, symbol: str, feats: dict, out: dict) -> tuple[str, str]:
    """(cause key, plain sentence). Wins get key "win" and a short summary."""
    pnl = out.get("realized_pnl") or 0.0
    dcall = int(feats.get("direction_call") or 0)
    move = out.get("move_pct")
    up = move is not None and move > 0
    verb_bet = "rise" if dcall > 0 else "fall"
    exit_txt = EXIT_WORDS.get(out.get("exit_reason") or "", "Closed")
    if pnl >= 0:
        if dcall and move is not None:
            moved = f"it {'rose' if up else 'fell'} {_pct(move)}"
            return "win", f"Bet {symbol} would {verb_bet}; {moved}. {exit_txt} for +{_money(pnl)}."
        if strategy in LONG_VOL and move is not None:
            return "win", f"Bet {symbol} would make a big move; it moved {_pct(move)}. {exit_txt} for +{_money(pnl)}."
        if strategy in SHORT_VOL and move is not None:
            return "win", f"Bet {symbol} would stay in a range; it moved only {_pct(move)}. {exit_txt} for +{_money(pnl)}."
        return "win", f"{exit_txt} for +{_money(pnl)}."

    gap = out.get("gap_sigma")
    if gap is not None and gap >= GAP_SIGMA:
        return "gap", (f"{symbol} jumped overnight against the trade (about {gap:.1f} times a normal day's move); "
                       f"the loss was {_money(pnl)}.")
    if out.get("exit_reason") == "stop_loss" and out.get("recovered"):
        return "stopped_recovered", (f"Hit the stop-loss for a {_money(pnl)} loss, but the trade later came back to "
                                     f"{'+' if (out.get('best_post_pnl') or 0) >= 0 else '-'}{_money(out.get('best_post_pnl') or 0)} "
                                     "before it expired.")
    gross = out.get("gross_pnl")
    if gross is not None and gross >= 0 and (out.get("cost_total") or 0) > 0:
        return "costs", (f"{symbol} barely moved the way that mattered; spreads and commissions "
                         f"(about {_money(out.get('cost_total') or 0)}) turned a flat trade into a {_money(pnl)} loss.")
    if dcall and out.get("direction_right") is False:
        return "direction_wrong", f"Bet {symbol} would {verb_bet}; it {'rose' if up else 'fell'} {_pct(move)} instead."
    if dcall and out.get("direction_right") is True:
        if not feats.get("is_credit"):
            return "too_slow", (f"Bet {symbol} would {verb_bet}; it did, but only {_pct(move)}, too little and too slowly "
                                "to beat the option's time decay.")
        return "vol_under", (f"Bet {symbol} would not {'fall' if dcall > 0 else 'rise'}; it moved the right way "
                             f"({_pct(move)}), but the options got more expensive, so the sold spread still lost.")
    ratio = out.get("realized_vs_model")
    long_vol = strategy in LONG_VOL or (not feats.get("is_credit") and (feats.get("vol_edge") or 0) > 0 and not dcall)
    if long_vol and ratio is not None and ratio < VOL_RATIO_LOW:
        return "vol_over", (f"Paid for a big move in {symbol}; it moved only {_pct(move)}, about {ratio:.0%} of what the "
                            "model expected.")
    if strategy in SHORT_VOL and ratio is not None and ratio > VOL_RATIO_HIGH:
        return "vol_under", (f"Bet {symbol} would stay in a range; it moved {_pct(move)}, about {ratio:.1f} times what the "
                             "model expected.")
    if long_vol and move is not None:
        return "vol_over", f"Paid for a big move in {symbol}; it moved only {_pct(move)}, not enough to pay for the options."
    if strategy in SHORT_VOL and move is not None:
        return "vol_under", f"Bet {symbol} would stay in a range; it moved {_pct(move)}, enough to hurt the trade."
    return "unknown", (f"Lost {_money(pnl)} ({exit_txt.lower()}). Not enough was recorded about this trade to say why; "
                       "trades opened from now on record what is needed.")


def build(pos: Position, mode: str, feats: dict, marks: list[dict], exit_spot: float | None, now: datetime,
          flatten_time: str = "15:55") -> dict:
    """The full post-mortem row for the journal."""
    out = outcome(pos, feats, marks, exit_spot, flatten_time)
    cause, text = classify(pos.idea.strategy, pos.idea.symbol, feats, out)
    return {
        "position_id": pos.id, "mode": mode, "symbol": pos.idea.symbol, "strategy": pos.idea.strategy,
        "opened": pos.opened, "closed": pos.closed, "realized_pnl": out["realized_pnl"],
        "max_loss_total": out["max_loss_total"], "r": out["r"], "win": out["realized_pnl"] > 0,
        "exit_reason": pos.exit_reason, "cause": cause, "cause_text": text, "features": feats, "outcome": out,
        "tracking_done": tracking_finished(pos, now), "updated": now,
    }


def tracking_finished(pos: Position, now: datetime) -> bool:
    """Post-exit tracking runs until the structure's original expiry (16:00 on the earliest leg's expiry)."""
    end = datetime.combine(pos.idea.expiry, time(16, 0), tzinfo=NY)
    return now.astimezone(NY) >= end


def intrinsic_value(idea: TradeIdea, spot: float, on: date) -> float:
    """Structure value per share at expiry with the underlying at `spot` (legs expiring later count at 0 extra)."""
    v = 0.0
    for l in idea.legs:
        q = l.quote
        intr = max(spot - q.strike, 0.0) if q.right.value == "C" else max(q.strike - spot, 0.0)
        v += (1.0 if l.side == Side.BUY else -1.0) * intr * l.ratio
    return v

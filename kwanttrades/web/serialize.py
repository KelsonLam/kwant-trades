"""JSON-safe conversion and the pure payload builders (payoff, breakevens, surface, position extras)."""
from __future__ import annotations

import math
from datetime import date, datetime
from enum import Enum
from typing import Any

import numpy as np

from kwanttrades.quant.distribution import payoff_at_expiry
from kwanttrades.types import MULTIPLIER, NY, Position, TradeIdea


def jsonsafe(o: Any) -> Any:
    """Plain JSON types only: NaN/+-inf -> None, datetimes -> ISO-8601 in America/New_York, numpy -> python."""
    if o is None or isinstance(o, (str, bool)):
        return o
    if isinstance(o, Enum):
        return jsonsafe(o.value)
    if isinstance(o, np.generic):
        return jsonsafe(o.item())
    if isinstance(o, int):
        return int(o)
    if isinstance(o, float):
        return float(o) if math.isfinite(o) else None
    if isinstance(o, datetime):
        if o.tzinfo is None:
            o = o.replace(tzinfo=NY)
        return o.astimezone(NY).isoformat()
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, dict):
        return {str(k): jsonsafe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set, frozenset)):
        return [jsonsafe(v) for v in o]
    if isinstance(o, np.ndarray):
        return [jsonsafe(v) for v in o.tolist()]
    return str(o)


def num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# --------------------------------------------------------------------------- payoff / breakevens
def _per_set_pnl(idea: TradeIdea, prices) -> np.ndarray:
    return (payoff_at_expiry(idea.legs, prices) - idea.entry_limit) * MULTIPLIER


def idea_payoff(idea: TradeIdea, spot: float | None, sigma_annual: float | None = None, n: int = 57) -> dict:
    """Expiry P&L per contract-set (net of the entry price) on ~60 prices.

    The window is spot +- max(3 daily sigmas, 4%) widened to include the outer strikes plus a margin, with every
    strike (the kinks of the payoff) added to the grid so the corners are exact."""
    strikes = [float(l.quote.strike) for l in idea.legs]
    ref = float(spot) if spot and spot > 0 else float(np.mean(strikes))
    sig = float(sigma_annual) if sigma_annual and math.isfinite(sigma_annual) and sigma_annual > 0 else 0.25
    half = max(3.0 * sig / math.sqrt(252.0), 0.04) * ref
    kmin, kmax = min(strikes), max(strikes)
    margin = 0.15 * max(kmax - kmin, 0.02 * ref)
    lo = max(min(ref - half, kmin) - margin, 0.01 * ref)
    hi = max(ref + half, kmax) + margin
    grid = np.unique(np.round(np.concatenate([np.linspace(lo, hi, n), strikes, [ref]]), 4))
    pnl = _per_set_pnl(idea, grid)
    return {"prices": [float(p) for p in grid], "pnl": [round(float(v), 2) for v in pnl]}


def idea_breakevens(idea: TradeIdea) -> list[float]:
    """Exact zero crossings of the piecewise-linear expiry P&L (it is linear between strikes)."""
    strikes = sorted({float(l.quote.strike) for l in idea.legs})
    knots = np.array([0.0] + strikes + [strikes[-1] * 3.0])
    y = _per_set_pnl(idea, knots)
    out: list[float] = []
    for i in range(len(knots) - 1):
        a, b = float(y[i]), float(y[i + 1])
        if a == 0.0:
            out.append(float(knots[i]))
        if (a < 0.0 < b) or (b < 0.0 < a):
            t = -a / (b - a)
            out.append(float(knots[i] + t * (knots[i + 1] - knots[i])))
    if float(y[-1]) == 0.0:
        out.append(float(knots[-1]))
    return sorted({round(v, 2) for v in out if v > 0})


# --------------------------------------------------------------------------- positions
def position_extras(p: Position) -> dict:
    """Per-share structure values at which the take-profit / stop-loss triggers (same rule as evaluate_exit)."""
    idea = p.idea
    if math.isinf(idea.max_profit):
        target = idea.take_profit_pct * max(p.entry_price, 0.0) * MULTIPLIER
    else:
        target = idea.take_profit_pct * idea.max_profit
    stop = idea.stop_loss_pct * idea.max_loss
    return {
        "target_value": round(p.entry_price + target / MULTIPLIER, 4) if target > 0 else None,
        "stop_value": round(p.entry_price - stop / MULTIPLIER, 4) if stop > 0 else None,
    }


# --------------------------------------------------------------------------- volatility surface
def surface_payload(analysis: Any, flags: dict[str, str]) -> dict:
    chain, surf = analysis.chain, analysis.surface
    asof = chain.asof
    misp = list(getattr(analysis, "mispricings", None) or [])
    zmap: dict[str, float] = {}
    for m in misp:
        occ = m.quote.occ_symbol
        if m.model == "svi_residual" or occ not in zmap:
            zmap[occ] = m.zscore
    slices = []
    for expiry in sorted(surf.slices):
        sl = surf.slices[expiry]
        tab = sl.table
        points = []
        if tab is not None and len(tab):
            for _, r in tab.iterrows():
                miv = num(r.get("market_iv"))
                if miv is None:
                    continue
                strike = float(r["strike"])
                fit = num(r.get("fit_iv"))
                if fit is None:
                    fit = num(sl.iv(strike))
                occ = r["quote"].occ_symbol
                points.append({
                    "strike": strike, "right": str(r["right"]), "market_iv": miv, "bid_iv": num(r.get("bid_iv")),
                    "ask_iv": num(r.get("ask_iv")), "fit_iv": fit, "flag": flags.get(occ), "zscore": num(zmap.get(occ)),
                })
        curve = {"strikes": [], "iv": []}
        if points:
            ks = [p["strike"] for p in points]
            grid = np.linspace(min(ks), max(ks), 48)
            ivs = np.asarray(sl.iv(grid), dtype=float)
            for k, v in zip(grid, ivs):
                if math.isfinite(v):
                    curve["strikes"].append(round(float(k), 4))
                    curve["iv"].append(float(v))
        slices.append({
            "expiry": expiry.isoformat(), "dte": (expiry - asof.date()).days, "forward": num(sl.forward),
            "method": sl.method, "rmse": num(sl.rmse), "points": points, "fit_curve": curve,
        })
    return {
        "symbol": analysis.symbol, "spot": num(chain.spot), "asof": asof, "slices": slices,
        "mispricings": [jsonsafe({
            "occ": m.quote.occ_symbol, "model": m.model, "edge": m.edge, "edge_after_costs": m.edge_after_costs,
            "zscore": m.zscore, "market_iv": m.market_iv, "fair_iv": m.fair_iv, "notes": m.notes,
        }) for m in misp[:80]],
    }

"""Performance statistics for a list of closed trades and an equity curve (pure functions)."""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable, Mapping


def max_drawdown(equity: Iterable[float]) -> tuple[float, float]:
    """(largest peak-to-trough fall in dollars, same as a fraction of the peak). Both >= 0."""
    peak = -math.inf
    best = best_pct = 0.0
    for v in equity:
        peak = max(peak, v)
        dd = peak - v
        if dd > best:
            best = dd
        if peak > 0:
            best_pct = max(best_pct, dd / peak)
    return best, best_pct


def sharpe_daily(day_end_equity: list[float], starting_equity: float) -> float:
    """Annualised Sharpe (sqrt(252), zero risk-free) of daily returns of the day-end equity series,
    each return measured against the previous day's closing equity (the first against the start)."""
    rets = []
    prev = starting_equity
    for e in day_end_equity:
        if prev > 0:
            rets.append((e - prev) / prev)
        prev = e
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    return 0.0 if sd < 1e-15 else m / sd * math.sqrt(252.0)


def compute_metrics(trades: list[Mapping[str, Any]], day_end_equity: list[float], starting_equity: float,
                    equity_curve: list[float] | None = None) -> dict[str, Any]:
    """Trade statistics: win rate, average win/loss, profit factor (inf if no losses), expectancy per
    trade, total P&L, Sharpe of daily P&L, max drawdown ($ and fraction) and breakdowns."""
    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    gp, gl = sum(wins), -sum(losses)
    curve = equity_curve if equity_curve else list(day_end_equity)
    dd, dd_pct = max_drawdown([starting_equity] + list(curve))
    if gl > 0:
        pf = gp / gl
    else:
        pf = math.inf if gp > 0 else 0.0
    by_strat: dict[str, list[float]] = defaultdict(list)
    by_exit: dict[str, list[float]] = defaultdict(list)
    for t, p in zip(trades, pnls):
        by_strat[str(t.get("strategy", "?"))].append(p)
        by_exit[str(t.get("exit_reason", "?") or "?")].append(p)
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / n if n else 0.0,
        "avg_win": gp / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": pf, "expectancy": sum(pnls) / n if n else 0.0,
        "total_pnl": sum(pnls), "sharpe": sharpe_daily(list(day_end_equity), starting_equity),
        "max_drawdown": dd, "max_drawdown_pct": dd_pct,
        "by_strategy": [{"strategy": k, "trades": len(v), "pnl": sum(v), "win_rate": sum(1 for x in v if x > 0) / len(v)}
                        for k, v in sorted(by_strat.items())],
        "by_exit": [{"reason": k, "trades": len(v), "pnl": sum(v)} for k, v in sorted(by_exit.items())],
    }

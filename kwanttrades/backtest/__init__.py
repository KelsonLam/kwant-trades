"""Backtesting: metrics and the session-driven Backtester."""
from .engine import Backtester, BacktestResult, step_times
from .metrics import compute_metrics, max_drawdown, sharpe_daily

__all__ = ["Backtester", "BacktestResult", "step_times", "compute_metrics", "max_drawdown", "sharpe_daily"]

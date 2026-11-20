"""Configuration: dataclass defaults, optionally overridden by config.toml and .env.

Secrets (ALPACA_API_KEY, ALPACA_SECRET_KEY, KWANT_WEB_PASSWORD) are read ONLY from the
environment / .env file. They are never written to disk or logged by this package.
"""
from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RiskConfig:
    max_loss_per_trade_pct: float = 0.01  # of equity, per trade (max_loss * contracts)
    daily_loss_limit_pct: float = 0.03  # realised + unrealised; breach => no new trades, flatten
    max_trades_per_day: int = 6
    max_open_positions: int = 5
    max_positions_per_symbol: int = 1
    max_portfolio_risk_pct: float = 0.15  # sum of max_loss over open positions / equity
    min_reward_risk: float = 1.0  # max_profit / max_loss for debit structures
    min_credit_to_width: float = 0.20  # credit spreads: credit / width (the EV filter is the real gate)
    entry_window_start: str = "09:45"  # after the 15-minute opening range
    entry_window_end: str = "15:30"
    flatten_time: str = "15:55"  # close anything expiring today / intraday-only ideas
    kelly_fraction: float = 0.25
    max_contracts_per_trade: int = 10
    defined_risk_only: bool = True  # never allow naked short options
    pdt_guard: bool = True  # block day trades beyond 3 in 5 days when equity < $25k


@dataclass
class ScanConfig:
    universe: list[str] = field(default_factory=lambda: [
        "SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD",
    ])
    directional_dte: tuple[int, int] = (1, 14)
    vol_dte: tuple[int, int] = (14, 45)
    min_open_interest: int = 100
    min_volume: int = 1
    max_spread_pct: float = 0.12
    min_bid: float = 0.05
    min_svi_zscore: float = 2.0  # |residual| / half-spread (in vol units) to flag a mispricing
    min_vol_edge: float = 0.10  # |forecast_rv - atm_iv| / atm_iv to hold a vol view
    min_direction: float = 0.25  # |composite direction| to hold a directional view
    min_expected_value: float = 10.0  # $ per contract-set after costs (~4x round-trip commission)
    min_score: float = 0.05
    max_ideas_per_symbol: int = 2


@dataclass
class SignalConfig:
    opening_range_minutes: int = 15
    breakout_bar_minutes: int = 5
    min_relative_volume: float = 1.2
    weights: dict[str, float] = field(default_factory=lambda: {
        "orb": 0.40, "vwap": 0.15, "gap": 0.05, "news": 0.25, "regime": 0.15,
    })


@dataclass
class SentimentConfig:
    enabled: bool = True
    max_headlines_per_symbol: int = 12
    lookback_hours: int = 24
    half_life_hours: float = 6.0


@dataclass
class BrokerConfig:
    mode: str = "paper"  # "paper" (built-in simulator) | "alpaca_paper" | "alpaca_live"
    starting_equity: float = 10_000.0
    commission_per_contract: float = 0.65
    slippage_frac_of_half_spread: float = 0.5  # fills at mid +/- this * half-spread, against us
    db_path: str = "data/kwanttrades.sqlite"


@dataclass
class GraduationConfig:
    """When the built-in paper simulator has earned a move to the Alpaca PAPER account. Live trading is never
    switched on by this (or by anything else in the program); see README "Going live"."""
    min_closed_trades: int = 40
    min_trading_days: int = 20
    min_profit_factor: float = 1.3
    max_drawdown_pct: float = 0.10  # of starting equity, on closed-trade P&L
    min_profitable_weeks_frac: float = 0.6
    min_weeks: int = 4
    require_positive_ci: bool = True  # 90% CI lower bound of mean (P&L / max loss) per trade must be > 0
    out_of_sample_trades: int = 25  # qualifying trades needed AFTER the most recent learning change
    auto_switch: bool = True  # switch the running paper session to alpaca_paper when everything passes


@dataclass
class Config:
    risk: RiskConfig = field(default_factory=RiskConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    sentiment: SentimentConfig = field(default_factory=SentimentConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    graduation: GraduationConfig = field(default_factory=GraduationConfig)
    poll_seconds: int = 60


def _merge(obj, data: dict):
    for f in dataclasses.fields(obj):
        if f.name not in data:
            continue
        cur, new = getattr(obj, f.name), data[f.name]
        if dataclasses.is_dataclass(cur) and isinstance(new, dict):
            _merge(cur, new)
        elif isinstance(cur, tuple):
            setattr(obj, f.name, tuple(new))
        elif isinstance(cur, dict) and isinstance(new, dict):
            cur.update(new)
        else:
            setattr(obj, f.name, new)


def load_env(path: str | Path = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: str | Path = "config.toml") -> Config:
    cfg = Config()
    p = Path(path)
    if p.exists():
        with p.open("rb") as fh:
            _merge(cfg, tomllib.load(fh))
    load_env()
    return cfg

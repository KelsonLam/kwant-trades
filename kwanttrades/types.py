"""Shared data contracts for Kwant Trades. Every module imports from here.

Conventions (binding for all modules):
- Option prices are quoted PER SHARE. Contract multiplier is 100.
- Dollar amounts on TradeIdea (max_loss, max_profit, expected_value) are per ONE
  contract-set (i.e. already multiplied by 100) and are positive numbers.
- TradeIdea.net_price > 0 means a debit paid per share, < 0 a credit received.
- Volatilities, rates and yields are annualised decimals (0.20 == 20%).
- Time to expiry T is in years, ACT/365, measured to 16:00 America/New_York on expiry.
- All datetimes are timezone-aware, America/New_York.
- Bars are pandas DataFrames indexed by a tz-aware DatetimeIndex (America/New_York)
  with float columns: open, high, low, close, volume.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")
MULTIPLIER = 100
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def now_ny() -> datetime:
    return datetime.now(NY)


def year_fraction(asof: datetime, expiry: date) -> float:
    """ACT/365 time from `asof` to 16:00 NY on `expiry`, floored at 1 minute."""
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=NY)
    end = datetime.combine(expiry, MARKET_CLOSE, tzinfo=NY)
    seconds = (end - asof.astimezone(NY)).total_seconds()
    return max(seconds, 60.0) / (365.0 * 24 * 3600)


class Right(str, Enum):
    CALL = "C"
    PUT = "P"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class OptionQuote:
    underlying: str
    expiry: date
    strike: float
    right: Right
    bid: float
    ask: float
    last: float = 0.0
    volume: int = 0
    open_interest: int = 0
    vendor_iv: float | None = None

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return 0.5 * (self.bid + self.ask)
        return self.ask if self.ask > 0 else self.last

    @property
    def spread(self) -> float:
        return max(self.ask - self.bid, 0.0)

    @property
    def spread_pct(self) -> float:
        m = self.mid
        return self.spread / m if m > 0 else float("inf")

    @property
    def has_two_sided_market(self) -> bool:
        return self.bid > 0 and self.ask > self.bid

    @property
    def occ_symbol(self) -> str:
        return f"{self.underlying}{self.expiry:%y%m%d}{self.right.value}{int(round(self.strike * 1000)):08d}"


@dataclass
class OptionChain:
    underlying: str
    spot: float
    asof: datetime
    quotes: list[OptionQuote]
    risk_free: float = 0.04
    div_yield: float = 0.0

    def expiries(self) -> list[date]:
        return sorted({q.expiry for q in self.quotes})

    def for_expiry(self, expiry: date) -> list[OptionQuote]:
        return sorted((q for q in self.quotes if q.expiry == expiry), key=lambda q: (q.strike, q.right.value))

    def T(self, expiry: date) -> float:
        return year_fraction(self.asof, expiry)

    def find(self, expiry: date, strike: float, right: Right) -> OptionQuote | None:
        for q in self.quotes:
            if q.expiry == expiry and q.right == right and abs(q.strike - strike) < 1e-6:
                return q
        return None


@dataclass(frozen=True)
class NewsItem:
    symbol: str  # "" for market-wide
    headline: str
    published: datetime
    source: str = ""
    url: str = ""
    summary: str = ""


@dataclass
class Mispricing:
    """One option that a quant model says is cheap (edge > 0) or rich (edge < 0)."""
    quote: OptionQuote
    model: str  # "svi_residual" | "vrp" | "parity" | "butterfly" | "calendar"
    fair_value: float  # model price per share
    market_mid: float
    edge: float  # fair_value - market_mid, per share
    edge_after_costs: float  # edge net of half-spread crossing; same sign rule, 0 if not tradable
    zscore: float  # edge in units of model uncertainty / half-spread
    market_iv: float | None = None
    fair_iv: float | None = None
    notes: str = ""


@dataclass
class DirectionalSignal:
    symbol: str
    source: str  # "orb" | "vwap" | "gap" | "news" | "regime" | "composite"
    direction: float  # -1 (bearish) .. +1 (bullish)
    confidence: float  # 0 .. 1
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class VolView:
    """Model view of volatility for one symbol/horizon."""
    symbol: str
    horizon_days: int
    atm_iv: float
    forecast_rv: float
    vol_edge: float  # (forecast_rv - atm_iv) / atm_iv ; > 0 options cheap, < 0 options rich
    components: dict[str, float] = field(default_factory=dict)  # per-estimator forecasts


@dataclass(frozen=True)
class Leg:
    quote: OptionQuote
    side: Side
    ratio: int = 1


@dataclass
class TradeIdea:
    symbol: str
    strategy: str  # long_call | long_put | bull_call_spread | bear_put_spread | bull_put_spread | bear_call_spread | iron_condor | long_straddle | long_strangle
    legs: list[Leg]
    net_price: float  # per share; >0 debit, <0 credit (computed at mid)
    entry_limit: float  # per share limit we would submit (mid adjusted toward the market)
    max_loss: float  # $ per contract-set, positive
    max_profit: float  # $ per contract-set, positive (float('inf') if unbounded)
    pop: float  # probability of profit at expiry under our physical distribution
    expected_value: float  # $ per contract-set under our physical distribution, net of costs
    score: float  # ranking score, higher is better
    rationale: list[str] = field(default_factory=list)
    greeks: dict[str, float] = field(default_factory=dict)  # net delta/gamma/vega/theta per contract-set
    contracts: int = 0  # filled in by sizing
    stop_loss_pct: float = 0.35  # exit if position value loses this fraction of max_loss
    take_profit_pct: float = 0.50  # exit when this fraction of max_profit (or of debit, if unbounded) is captured
    created: datetime = field(default_factory=now_ny)

    @property
    def expiry(self) -> date:
        return min(l.quote.expiry for l in self.legs)

    @property
    def is_credit(self) -> bool:
        return self.net_price < 0


class OrderStatus(str, Enum):
    NEW = "new"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELED = "canceled"


@dataclass
class Order:
    id: str
    idea: TradeIdea
    contracts: int
    limit_price: float  # per share, same sign convention as TradeIdea.net_price
    action: str = "open"  # "open" | "close"
    status: OrderStatus = OrderStatus.NEW
    fill_price: float | None = None
    fees: float = 0.0
    submitted: datetime = field(default_factory=now_ny)
    filled: datetime | None = None
    reason: str = ""  # rejection reason or exit reason
    position_id: str | None = None


@dataclass
class Position:
    id: str
    idea: TradeIdea
    contracts: int
    entry_price: float  # per share, >0 debit paid, <0 credit received
    opened: datetime
    mark: float | None = None  # current per-share value of the structure, same sign convention as entry
    closed: datetime | None = None
    exit_price: float | None = None
    realized_pnl: float = 0.0
    fees: float = 0.0
    exit_reason: str = ""

    @property
    def is_open(self) -> bool:
        return self.closed is None

    @property
    def unrealized_pnl(self) -> float:
        """(mark - entry) * 100 * contracts. Works for debits and credits alike because a credit
        structure has negative entry and a (less) negative mark when it decays in our favour."""
        if self.mark is None or not self.is_open:
            return 0.0
        return (self.mark - self.entry_price) * MULTIPLIER * self.contracts

    @property
    def capital_at_risk(self) -> float:
        return self.idea.max_loss * self.contracts


@dataclass
class AccountState:
    equity: float
    cash: float
    buying_power: float
    day_pnl: float = 0.0
    open_risk: float = 0.0  # sum of max_loss over open positions


# ---------------------------------------------------------------- provider interfaces


@runtime_checkable
class MarketDataProvider(Protocol):
    def spot(self, symbol: str) -> float: ...

    def chain(self, symbol: str, min_dte: int = 0, max_dte: int = 45) -> OptionChain: ...

    def intraday_bars(self, symbol: str, interval: str = "1m", days: int = 1) -> pd.DataFrame: ...

    def daily_bars(self, symbol: str, years: float = 2.0) -> pd.DataFrame: ...

    def risk_free_rate(self) -> float: ...


@runtime_checkable
class NewsProvider(Protocol):
    def headlines(self, symbol: str, lookback_hours: int = 24) -> list[NewsItem]: ...


@runtime_checkable
class Broker(Protocol):
    def account(self) -> AccountState: ...

    def submit(self, idea: TradeIdea, contracts: int, limit_price: float) -> Order: ...

    def close(self, position_id: str, limit_price: float, reason: str = "") -> Order: ...

    def positions(self, include_closed: bool = False) -> list[Position]: ...

    def mark_to_market(self, chains: dict[str, OptionChain]) -> None: ...

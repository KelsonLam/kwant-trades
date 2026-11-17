"""Alpaca broker (alpaca-py, lazily imported).

Safety: `alpaca_live` refuses to construct unless KWANT_ALLOW_LIVE == "YES_I_ACCEPT_THE_RISK".
API keys come only from the environment (ALPACA_API_KEY / ALPACA_SECRET_KEY) and are never logged.
The order mapping has NOT been verified against a live paper account; see `alpaca_limit_price`.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from typing import Any

from kwanttrades.config import BrokerConfig
from kwanttrades.execution.exits import structure_mark
from kwanttrades.execution.journal import Journal
from kwanttrades.types import (
    MULTIPLIER,
    NY,
    AccountState,
    OptionChain,
    Order,
    OrderStatus,
    Position,
    Side,
    TradeIdea,
    now_ny,
)

log = logging.getLogger("kwanttrades.alpaca")

LIVE_ENV = "KWANT_ALLOW_LIVE"
LIVE_ACK = "YES_I_ACCEPT_THE_RISK"


class LiveTradingNotAllowed(RuntimeError):
    pass


class AlpacaConfigError(RuntimeError):
    pass


def alpaca_limit_price(limit: float, opening: bool, multileg: bool) -> float:
    """ISOLATED price-convention function: map our limit to the number sent to Alpaca.

    Our convention (TradeIdea.net_price): positive = debit paid, negative = credit received; for a
    close order `limit` is the minimum structure value we accept receiving, so the net debit we
    pay is -limit. Alpaca convention (per its options docs, UNVERIFIED on a paper account): for
    multi-leg orders limit_price is the net price, positive = net debit, negative = net credit.
    For single-leg orders the side already encodes direction, so the price is a positive number.
    If paper testing shows Alpaca wants the absolute value for credits, change ONLY this function.
    """
    net_debit = limit if opening else -limit
    price = net_debit if multileg else abs(net_debit)
    return round(price, 2)


def _intent(side: Side, opening: bool) -> str:
    if opening:
        return "buy_to_open" if side == Side.BUY else "sell_to_open"
    return "sell_to_close" if side == Side.BUY else "buy_to_close"


def build_order_spec(idea: TradeIdea, contracts: int, limit_price: float, opening: bool = True) -> dict[str, Any]:
    """SDK-independent description of the Alpaca request (also what tests assert against)."""
    multileg = len(idea.legs) > 1
    price = alpaca_limit_price(limit_price, opening, multileg)

    def side_of(leg) -> str:
        s = leg.side if opening else (Side.SELL if leg.side == Side.BUY else Side.BUY)
        return s.value

    if not multileg:
        leg = idea.legs[0]
        return {
            "order_class": "simple", "symbol": leg.quote.occ_symbol, "qty": contracts * leg.ratio,
            "side": side_of(leg), "time_in_force": "day", "limit_price": price,
            "position_intent": _intent(leg.side, opening),
        }
    return {
        "order_class": "mleg", "qty": contracts, "time_in_force": "day", "limit_price": price,
        "legs": [
            {"symbol": l.quote.occ_symbol, "ratio_qty": l.ratio, "side": side_of(l),
             "position_intent": _intent(l.side, opening)}
            for l in idea.legs
        ],
    }


def to_sdk_request(spec: dict[str, Any]):
    """Convert a spec into alpaca-py request objects (lazy import)."""
    from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

    def side(s: str):
        return OrderSide.BUY if s == "buy" else OrderSide.SELL

    if spec["order_class"] == "simple":
        return LimitOrderRequest(
            symbol=spec["symbol"], qty=spec["qty"], side=side(spec["side"]),
            time_in_force=TimeInForce.DAY, limit_price=spec["limit_price"],
            position_intent=PositionIntent(spec["position_intent"]),
        )
    legs = [
        OptionLegRequest(
            symbol=l["symbol"], ratio_qty=l["ratio_qty"], side=side(l["side"]),
            position_intent=PositionIntent(l["position_intent"]),
        )
        for l in spec["legs"]
    ]
    return LimitOrderRequest(
        qty=spec["qty"], order_class=OrderClass.MLEG, time_in_force=TimeInForce.DAY,
        limit_price=spec["limit_price"], legs=legs,
    )


class AlpacaBroker:
    def __init__(self, cfg: BrokerConfig, client: Any = None, journal: Journal | None = None):
        self.cfg = cfg
        if cfg.mode == "alpaca_live":
            if os.environ.get(LIVE_ENV) != LIVE_ACK:
                raise LiveTradingNotAllowed(
                    f"Live trading refused: set environment variable {LIVE_ENV}={LIVE_ACK} yourself to enable it."
                )
            log.warning("!!! ALPACA LIVE TRADING MODE ENABLED: orders will use REAL MONEY !!!")
            paper = False
        elif cfg.mode == "alpaca_paper":
            paper = True
        else:
            raise ValueError(f"AlpacaBroker needs mode 'alpaca_paper' or 'alpaca_live', got {cfg.mode!r}")
        self.paper = paper
        self.journal = journal
        if client is not None:
            self.client = client
        else:
            key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
            if not key or not secret:
                raise AlpacaConfigError(
                    "Alpaca API keys are missing. Put ALPACA_API_KEY and ALPACA_SECRET_KEY in your .env file "
                    "yourself (the app never writes or logs them)."
                )
            try:
                from alpaca.trading.client import TradingClient
            except ImportError as exc:  # pragma: no cover
                raise AlpacaConfigError("alpaca-py is not installed: python -m pip install alpaca-py") from exc
            self.client = TradingClient(key, secret, paper=paper)
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._quotes_chains: dict[str, OptionChain] = {}

    # ------------------------------------------------------------------ internals
    def _save(self, order: Order) -> None:
        if self.journal is not None:
            self.journal.save_order(order)

    def _send(self, idea: TradeIdea, contracts: int, limit_price: float, opening: bool, action: str,
              position_id: str | None, reason: str = "") -> Order:
        order = Order(id="local-" + uuid.uuid4().hex[:12], idea=idea, contracts=contracts, limit_price=limit_price,
                      action=action, submitted=now_ny(), position_id=position_id, reason=reason)
        if contracts < 1:
            order.status, order.reason = OrderStatus.REJECTED, "contracts must be >= 1"
            self._save(order)
            return order
        spec = build_order_spec(idea, contracts, limit_price, opening)
        try:
            resp = self.client.submit_order(to_sdk_request(spec))
        except Exception as exc:
            order.status, order.reason = OrderStatus.REJECTED, f"broker error: {type(exc).__name__}: {exc}"
            self._save(order)
            return order
        order.id = str(getattr(resp, "id", order.id))
        self._orders[order.id] = order
        self._save(order)
        return order

    def _sync(self) -> None:
        """Poll pending orders; turn fills into positions / closed positions."""
        for oid, order in list(self._orders.items()):
            if order.status != OrderStatus.NEW:
                continue
            try:
                resp = self.client.get_order_by_id(oid)
            except Exception as exc:  # pragma: no cover - network
                log.warning("order status poll failed for %s: %s", oid, type(exc).__name__)
                continue
            status = str(getattr(getattr(resp, "status", ""), "value", getattr(resp, "status", ""))).lower()
            if status in ("canceled", "expired", "rejected"):
                order.status = OrderStatus.CANCELED if status != "rejected" else OrderStatus.REJECTED
                self._save(order)
                continue
            if status != "filled":
                continue
            avg = float(getattr(resp, "filled_avg_price", None) or 0.0)
            order.status, order.filled = OrderStatus.FILLED, now_ny()
            order.fees = 0.0
            if order.action == "open":
                price = avg if not order.idea.is_credit else -avg  # Alpaca reports absolute average price
                order.fill_price = price
                pos = Position(id="pos-" + oid, idea=order.idea, contracts=order.contracts, entry_price=price,
                               opened=order.filled, mark=price)
                order.position_id = pos.id
                self._positions[pos.id] = pos
                if self.journal is not None:
                    self.journal.save_position(pos)
            else:
                pos = self._positions.get(order.position_id or "")
                if pos is not None and pos.is_open:
                    value = avg if not pos.idea.is_credit else -avg
                    order.fill_price = value
                    pos.exit_price, pos.closed, pos.exit_reason = value, order.filled, order.reason
                    pos.realized_pnl = (value - pos.entry_price) * MULTIPLIER * pos.contracts
                    if self.journal is not None:
                        self.journal.save_position(pos)
            self._save(order)

    # ------------------------------------------------------------------ Broker API
    def account(self) -> AccountState:
        a = self.client.get_account()
        equity, last = float(a.equity), float(getattr(a, "last_equity", a.equity))
        open_risk = sum(p.capital_at_risk for p in self._positions.values() if p.is_open)
        return AccountState(
            equity=equity, cash=float(a.cash),
            buying_power=float(getattr(a, "options_buying_power", None) or a.buying_power),
            day_pnl=equity - last, open_risk=open_risk,
        )

    def submit(self, idea: TradeIdea, contracts: int, limit_price: float) -> Order:
        return self._send(idea, contracts, limit_price, True, "open", None)

    def close(self, position_id: str, limit_price: float, reason: str = "") -> Order:
        pos = self._positions.get(position_id)
        if pos is None:
            raise KeyError(f"unknown position {position_id}")
        return self._send(pos.idea, pos.contracts, limit_price, False, "close", position_id, reason)

    def positions(self, include_closed: bool = False) -> list[Position]:
        self._sync()
        ps = list(self._positions.values())
        if not include_closed:
            ps = [p for p in ps if p.is_open]
        return sorted(ps, key=lambda p: p.opened)

    def mark_to_market(self, chains: dict[str, OptionChain]) -> None:
        for p in self._positions.values():
            chain = chains.get(p.idea.symbol)
            if chain is not None and p.is_open:
                m = structure_mark(p, chain)
                if m is not None:
                    p.mark = m

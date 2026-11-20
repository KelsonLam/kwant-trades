"""SQLite journal: orders, positions, equity snapshots, decisions, signals.

Everything is stored as plain columns plus JSON for structured parts (legs/ideas). Dates are ISO
strings, enums are stored by value and non-finite floats (``inf`` max_profit, ``-inf`` market
limits) are tagged so they survive a JSON/SQLite round trip. Objects are fully reconstructed.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from kwanttrades.types import (
    NY,
    AccountState,
    DirectionalSignal,
    Leg,
    OptionQuote,
    Order,
    OrderStatus,
    Position,
    Right,
    Side,
    TradeIdea,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, day TEXT, action TEXT, status TEXT, contracts INTEGER,
    limit_price TEXT, fill_price REAL, fees REAL, reason TEXT, position_id TEXT,
    submitted TEXT, filled TEXT, idea TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY, idea TEXT, contracts INTEGER, entry_price REAL, opened TEXT,
    mark REAL, closed TEXT, exit_price REAL, realized_pnl REAL, fees REAL, exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT, day TEXT, equity REAL, cash REAL, buying_power REAL, day_pnl REAL, open_risk REAL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, strategy TEXT,
    decision TEXT, reasons TEXT, contracts INTEGER, idea TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, source TEXT,
    direction REAL, confidence REAL, detail TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ix_orders_day ON orders(day);
CREATE INDEX IF NOT EXISTS ix_equity_day ON equity(day);
"""

# Added for the supervised learning loop (schema version 2). Created on demand, so an existing journal simply
# gains these tables; `_migrate` adds any column a partially created older table is missing.
_LEARNING_SCHEMA = """
CREATE TABLE IF NOT EXISTS entry_features (
    order_id TEXT PRIMARY KEY, position_id TEXT, mode TEXT, ts TEXT, features TEXT
);
CREATE TABLE IF NOT EXISTS mark_paths (
    position_id TEXT, ts TEXT, mark REAL, underlying REAL, phase TEXT
);
CREATE TABLE IF NOT EXISTS post_mortems (
    position_id TEXT PRIMARY KEY, mode TEXT, symbol TEXT, strategy TEXT, opened TEXT, closed TEXT,
    realized_pnl REAL, max_loss_total REAL, r REAL, win INTEGER, exit_reason TEXT, cause TEXT, cause_text TEXT,
    features TEXT, outcome TEXT, tracking_done INTEGER DEFAULT 0, updated TEXT
);
CREATE TABLE IF NOT EXISTS learning_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, message TEXT, payload TEXT
);
"""
_LEARNING_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_marks_pos ON mark_paths(position_id, ts);
CREATE INDEX IF NOT EXISTS ix_feat_pos ON entry_features(position_id);
CREATE INDEX IF NOT EXISTS ix_pm_closed ON post_mortems(closed);
"""
SCHEMA_VERSION = 2
_LEARNING_COLUMNS = {
    "entry_features": {"order_id": "TEXT", "position_id": "TEXT", "mode": "TEXT", "ts": "TEXT", "features": "TEXT"},
    "mark_paths": {"position_id": "TEXT", "ts": "TEXT", "mark": "REAL", "underlying": "REAL", "phase": "TEXT"},
    "post_mortems": {"position_id": "TEXT", "mode": "TEXT", "symbol": "TEXT", "strategy": "TEXT", "opened": "TEXT",
                     "closed": "TEXT", "realized_pnl": "REAL", "max_loss_total": "REAL", "r": "REAL", "win": "INTEGER",
                     "exit_reason": "TEXT", "cause": "TEXT", "cause_text": "TEXT", "features": "TEXT", "outcome": "TEXT",
                     "tracking_done": "INTEGER DEFAULT 0", "updated": "TEXT"},
    "learning_events": {"ts": "TEXT", "kind": "TEXT", "message": "TEXT", "payload": "TEXT"},
}


# ---------------------------------------------------------------- (de)serialisation helpers


def _enc(obj: Any) -> Any:
    """Make an object JSON-safe: tag non-finite floats, ISO dates, enums by value."""
    if isinstance(obj, float):
        if math.isnan(obj):
            return {"__f": "nan"}
        if math.isinf(obj):
            return {"__f": "inf" if obj > 0 else "-inf"}
        return obj
    if isinstance(obj, datetime):
        return {"__dt": obj.isoformat()}
    if isinstance(obj, date):
        return {"__d": obj.isoformat()}
    if isinstance(obj, (Right, Side, OrderStatus)):
        return obj.value
    if isinstance(obj, dict):
        return {str(k): _enc(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_enc(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):  # numpy scalar
        try:
            return _enc(obj.item())
        except Exception:  # pragma: no cover
            pass
    return obj


def _dec(obj: Any) -> Any:
    if isinstance(obj, dict):
        if set(obj) == {"__f"}:
            return float(obj["__f"])
        if set(obj) == {"__dt"}:
            return _parse_dt(obj["__dt"])
        if set(obj) == {"__d"}:
            return date.fromisoformat(obj["__d"])
        return {k: _dec(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_dec(v) for v in obj]
    return obj


def _parse_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s).astimezone(NY)


def _dt(d: datetime | None) -> str | None:
    return None if d is None else d.astimezone(NY).isoformat()


def _fenc(x: float) -> str:
    return repr(float(x))


def _quote_to_dict(q: OptionQuote) -> dict:
    return _enc({
        "underlying": q.underlying, "expiry": q.expiry, "strike": q.strike, "right": q.right,
        "bid": q.bid, "ask": q.ask, "last": q.last, "volume": q.volume,
        "open_interest": q.open_interest, "vendor_iv": q.vendor_iv,
    })


def _quote_from_dict(d: dict) -> OptionQuote:
    d = _dec(d)
    return OptionQuote(
        underlying=d["underlying"], expiry=d["expiry"], strike=float(d["strike"]),
        right=Right(d["right"]), bid=float(d["bid"]), ask=float(d["ask"]), last=float(d["last"]),
        volume=int(d["volume"]), open_interest=int(d["open_interest"]), vendor_iv=d["vendor_iv"],
    )


def idea_to_json(idea: TradeIdea) -> str:
    d = {
        "symbol": idea.symbol, "strategy": idea.strategy,
        "legs": [{"quote": _quote_to_dict(l.quote), "side": l.side.value, "ratio": l.ratio} for l in idea.legs],
        "net_price": idea.net_price, "entry_limit": idea.entry_limit, "max_loss": idea.max_loss,
        "max_profit": idea.max_profit, "pop": idea.pop, "expected_value": idea.expected_value,
        "score": idea.score, "rationale": list(idea.rationale), "greeks": dict(idea.greeks),
        "contracts": idea.contracts, "stop_loss_pct": idea.stop_loss_pct,
        "take_profit_pct": idea.take_profit_pct, "created": idea.created,
    }
    return json.dumps(_enc(d), allow_nan=False)


def idea_from_json(s: str) -> TradeIdea:
    d = _dec(json.loads(s))
    legs = [Leg(_quote_from_dict(l["quote"]), Side(l["side"]), int(l["ratio"])) for l in d["legs"]]
    return TradeIdea(
        symbol=d["symbol"], strategy=d["strategy"], legs=legs, net_price=d["net_price"],
        entry_limit=d["entry_limit"], max_loss=d["max_loss"], max_profit=d["max_profit"],
        pop=d["pop"], expected_value=d["expected_value"], score=d["score"],
        rationale=list(d["rationale"]), greeks=dict(d["greeks"]), contracts=int(d["contracts"]),
        stop_loss_pct=d["stop_loss_pct"], take_profit_pct=d["take_profit_pct"],
        created=d["created"].astimezone(NY),
    )


class Journal:
    """SQLite store. ``db_path`` may be ``":memory:"``; parent directories are created."""

    def __init__(self, db_path: str | Path = "data/kwanttrades.sqlite"):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Bring an older journal up to SCHEMA_VERSION without touching existing rows."""
        ver = int(self._conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        self._conn.executescript(_LEARNING_SCHEMA)
        for table, cols in _LEARNING_COLUMNS.items():
            have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, decl in cols.items():
                if col not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self._conn.executescript(_LEARNING_INDEXES)  # after the columns exist
        if ver < SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------ key/value
    def set_value(self, key: str, value: float | str) -> None:
        self._exec("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, repr(value) if isinstance(value, float) else str(value)))

    def get_value(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def get_float(self, key: str) -> float | None:
        v = self.get_value(key)
        return None if v is None else float(v)

    # ------------------------------------------------------------------ orders
    def save_order(self, o: Order) -> None:
        when = o.filled or o.submitted
        self._exec(
            "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o.id, when.astimezone(NY).date().isoformat(), o.action, o.status.value, o.contracts,
             _fenc(o.limit_price), o.fill_price, o.fees, o.reason, o.position_id,
             _dt(o.submitted), _dt(o.filled), idea_to_json(o.idea)),
        )

    @staticmethod
    def _order_from_row(r: sqlite3.Row) -> Order:
        return Order(
            id=r["id"], idea=idea_from_json(r["idea"]), contracts=r["contracts"],
            limit_price=float(r["limit_price"]), action=r["action"], status=OrderStatus(r["status"]),
            fill_price=r["fill_price"], fees=r["fees"], submitted=_parse_dt(r["submitted"]),
            filled=_parse_dt(r["filled"]), reason=r["reason"] or "", position_id=r["position_id"],
        )

    def orders(self) -> list[Order]:
        return [self._order_from_row(r) for r in self._query("SELECT * FROM orders ORDER BY submitted, rowid")]

    def get_order(self, order_id: str) -> Order | None:
        rows = self._query("SELECT * FROM orders WHERE id=?", (order_id,))
        return self._order_from_row(rows[0]) if rows else None

    # ------------------------------------------------------------------ positions
    def save_position(self, p: Position) -> None:
        self._exec(
            "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (p.id, idea_to_json(p.idea), p.contracts, p.entry_price, _dt(p.opened), p.mark,
             _dt(p.closed), p.exit_price, p.realized_pnl, p.fees, p.exit_reason),
        )

    @staticmethod
    def _position_from_row(r: sqlite3.Row) -> Position:
        return Position(
            id=r["id"], idea=idea_from_json(r["idea"]), contracts=r["contracts"],
            entry_price=r["entry_price"], opened=_parse_dt(r["opened"]), mark=r["mark"],
            closed=_parse_dt(r["closed"]), exit_price=r["exit_price"],
            realized_pnl=r["realized_pnl"] or 0.0, fees=r["fees"] or 0.0,
            exit_reason=r["exit_reason"] or "",
        )

    def positions(self, include_closed: bool = True) -> list[Position]:
        sql = "SELECT * FROM positions" + ("" if include_closed else " WHERE closed IS NULL") + " ORDER BY opened, rowid"
        return [self._position_from_row(r) for r in self._query(sql)]

    def open_positions(self) -> list[Position]:
        return self.positions(include_closed=False)

    def closed_positions(self) -> list[Position]:
        rows = self._query("SELECT * FROM positions WHERE closed IS NOT NULL ORDER BY closed, rowid")
        return [self._position_from_row(r) for r in rows]

    # ------------------------------------------------------------------ equity
    def record_equity(self, account: AccountState, ts: datetime) -> None:
        ts = ts.astimezone(NY)
        self._exec(
            "INSERT INTO equity VALUES (?,?,?,?,?,?,?)",
            (ts.isoformat(), ts.date().isoformat(), account.equity, account.cash,
             account.buying_power, account.day_pnl, account.open_risk),
        )

    def day_start_equity(self, day: date) -> float | None:
        rows = self._query("SELECT equity FROM equity WHERE day=? ORDER BY ts, rowid LIMIT 1", (day.isoformat(),))
        return rows[0]["equity"] if rows else None

    def equity_curve(self) -> pd.DataFrame:
        rows = self._query("SELECT * FROM equity ORDER BY ts, rowid")
        cols = ["equity", "cash", "buying_power", "day_pnl", "open_risk"]
        if not rows:
            return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz=NY))
        idx = pd.DatetimeIndex([datetime.fromisoformat(r["ts"]) for r in rows]).tz_convert(NY)
        return pd.DataFrame({c: [r[c] for r in rows] for c in cols}, index=idx)

    # ------------------------------------------------------------------ decisions / signals
    def record_decision(self, idea: TradeIdea, decision: Any, contracts: int, ts: datetime | None = None) -> None:
        """`decision` is a string ("PASS", "BLOCK: ...") or an object with .passed/.reasons."""
        if hasattr(decision, "passed") and hasattr(decision, "reasons"):
            reasons = list(decision.reasons)
            text = "PASS" if decision.passed else "BLOCK"
        else:
            text = str(decision)
            reasons = []
        ts = (ts or datetime.now(NY)).astimezone(NY)
        self._exec(
            "INSERT INTO decisions(ts, symbol, strategy, decision, reasons, contracts, idea) VALUES (?,?,?,?,?,?,?)",
            (ts.isoformat(), idea.symbol, idea.strategy, text, json.dumps(reasons), int(contracts), idea_to_json(idea)),
        )

    def decisions(self, limit: int = 100) -> list[dict]:
        rows = self._query("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (int(limit),))
        return [
            {"id": r["id"], "ts": _parse_dt(r["ts"]), "symbol": r["symbol"], "strategy": r["strategy"],
             "decision": r["decision"], "reasons": json.loads(r["reasons"]), "contracts": r["contracts"],
             "idea": idea_from_json(r["idea"])}
            for r in rows
        ]

    def record_signal(self, signal: DirectionalSignal, ts: datetime | None = None) -> None:
        ts = (ts or datetime.now(NY)).astimezone(NY)
        self._exec(
            "INSERT INTO signals(ts, symbol, source, direction, confidence, detail) VALUES (?,?,?,?,?,?)",
            (ts.isoformat(), signal.symbol, signal.source, float(signal.direction),
             float(signal.confidence), json.dumps(_enc(signal.detail), default=str, allow_nan=False)),
        )

    def signals(self, limit: int = 100) -> list[DirectionalSignal]:
        rows = self._query("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (int(limit),))
        return [DirectionalSignal(r["symbol"], r["source"], r["direction"], r["confidence"], _dec(json.loads(r["detail"]))) for r in rows]

    # ------------------------------------------------------------------ risk queries
    def trades_today(self, now: datetime) -> int:
        """Number of filled opening orders on the NY trading day of `now`."""
        day = now.astimezone(NY).date().isoformat()
        rows = self._query("SELECT COUNT(*) AS n FROM orders WHERE day=? AND action='open' AND status='filled'", (day,))
        return int(rows[0]["n"])

    def day_trades_last_5_sessions(self, now: datetime) -> int:
        """Positions opened and closed on the same NY day within the last 5 weekday sessions."""
        d = now.astimezone(NY).date()
        sessions: list[date] = []
        while len(sessions) < 5:
            if d.weekday() < 5:
                sessions.append(d)
            d -= timedelta(days=1)
        earliest = min(sessions)
        n = 0
        for p in self.closed_positions():
            od, cd = p.opened.astimezone(NY).date(), p.closed.astimezone(NY).date()
            if od == cd and cd >= earliest and cd <= now.astimezone(NY).date():
                n += 1
        return n

    # ------------------------------------------------------------------ learning: entry features
    def save_entry_features(self, order_id: str, position_id: str | None, mode: str, ts: datetime, features: dict) -> None:
        self._exec("INSERT OR REPLACE INTO entry_features VALUES (?,?,?,?,?)",
                   (order_id, position_id, mode, _dt(ts), json.dumps(_enc(features), allow_nan=False, default=str)))

    def entry_features(self, position_id: str) -> dict | None:
        """Features recorded when the position was opened, found by position id or via its opening order."""
        rows = self._query("SELECT features FROM entry_features WHERE position_id=? LIMIT 1", (position_id,))
        if not rows:
            rows = self._query(
                "SELECT f.features FROM entry_features f JOIN orders o ON o.id = f.order_id "
                "WHERE o.position_id=? AND o.action='open' LIMIT 1", (position_id,))
        return _dec(json.loads(rows[0]["features"])) if rows else None

    # ------------------------------------------------------------------ learning: mark paths
    def record_mark(self, position_id: str, ts: datetime, mark: float, underlying: float | None, phase: str = "open") -> None:
        self._exec("INSERT INTO mark_paths VALUES (?,?,?,?,?)",
                   (position_id, _dt(ts), float(mark), None if underlying is None else float(underlying), phase))

    def marks(self, position_id: str) -> list[dict]:
        rows = self._query("SELECT ts, mark, underlying, phase FROM mark_paths WHERE position_id=? ORDER BY ts, rowid",
                           (position_id,))
        return [{"ts": _parse_dt(r["ts"]), "mark": r["mark"], "underlying": r["underlying"], "phase": r["phase"]} for r in rows]

    def last_mark_ts(self, position_id: str) -> datetime | None:
        rows = self._query("SELECT MAX(ts) AS t FROM mark_paths WHERE position_id=?", (position_id,))
        return _parse_dt(rows[0]["t"]) if rows and rows[0]["t"] else None

    # ------------------------------------------------------------------ learning: post-mortems
    def save_post_mortem(self, pm: dict) -> None:
        enc = lambda x: json.dumps(_enc(x or {}), allow_nan=False, default=str)  # noqa: E731
        self._exec(
            "INSERT OR REPLACE INTO post_mortems VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pm["position_id"], pm.get("mode"), pm.get("symbol"), pm.get("strategy"), _dt(pm.get("opened")),
             _dt(pm.get("closed")), pm.get("realized_pnl"), pm.get("max_loss_total"), pm.get("r"),
             1 if pm.get("win") else 0, pm.get("exit_reason"), pm.get("cause"), pm.get("cause_text"),
             enc(pm.get("features")), enc(pm.get("outcome")), 1 if pm.get("tracking_done") else 0,
             _dt(pm.get("updated") or datetime.now(NY))),
        )

    @staticmethod
    def _pm_from_row(r: sqlite3.Row) -> dict:
        return {
            "position_id": r["position_id"], "mode": r["mode"], "symbol": r["symbol"], "strategy": r["strategy"],
            "opened": _parse_dt(r["opened"]), "closed": _parse_dt(r["closed"]), "realized_pnl": r["realized_pnl"],
            "max_loss_total": r["max_loss_total"], "r": r["r"], "win": bool(r["win"]), "exit_reason": r["exit_reason"],
            "cause": r["cause"], "cause_text": r["cause_text"], "features": _dec(json.loads(r["features"] or "{}")),
            "outcome": _dec(json.loads(r["outcome"] or "{}")), "tracking_done": bool(r["tracking_done"]),
            "updated": _parse_dt(r["updated"]),
        }

    def post_mortems(self, mode: str | None = None) -> list[dict]:
        """Oldest first (by close time)."""
        sql = "SELECT * FROM post_mortems" + (" WHERE mode=?" if mode else "") + " ORDER BY closed, rowid"
        return [self._pm_from_row(r) for r in self._query(sql, (mode,) if mode else ())]

    def post_mortem(self, position_id: str) -> dict | None:
        rows = self._query("SELECT * FROM post_mortems WHERE position_id=?", (position_id,))
        return self._pm_from_row(rows[0]) if rows else None

    def post_mortem_ids(self) -> set[str]:
        return {r["position_id"] for r in self._query("SELECT position_id FROM post_mortems")}

    # ------------------------------------------------------------------ learning: events (changes, stage switches)
    def record_event(self, kind: str, message: str, payload: dict | None = None, ts: datetime | None = None) -> None:
        ts = (ts or datetime.now(NY)).astimezone(NY)
        self._exec("INSERT INTO learning_events(ts, kind, message, payload) VALUES (?,?,?,?)",
                   (ts.isoformat(), kind, message, json.dumps(_enc(payload or {}), allow_nan=False, default=str)))

    def events(self, kind: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM learning_events" + (" WHERE kind=?" if kind else "") + " ORDER BY id DESC LIMIT ?"
        rows = self._query(sql, ((kind, int(limit)) if kind else (int(limit),)))
        return [{"id": r["id"], "ts": _parse_dt(r["ts"]), "kind": r["kind"], "message": r["message"],
                 "payload": _dec(json.loads(r["payload"] or "{}"))} for r in rows]

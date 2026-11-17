"""Validation of POST /api/config bodies and a small TOML writer (the stdlib can only read TOML)."""
from __future__ import annotations

import json
import math
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Callable

SYMBOL_RE = re.compile(r"^[A-Z\^][A-Z0-9.\-\^]{0,9}$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ConfigError(ValueError):
    """A rejected setting; the message names the field (e.g. "risk.max_trades_per_day: ...")."""


# ------------------------------------------------------------------ per-field validators: (value) -> normalised value
def _float(lo: float, hi: float, lo_open: bool = False) -> Callable[[Any], float]:
    def check(v: Any) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"must be a number between {lo:g} and {hi:g}")
        v = float(v)
        if v < lo or v > hi or (lo_open and v == lo):
            raise ValueError(f"must be a number between {lo:g} and {hi:g}" + (" (exclusive of the lower bound)" if lo_open else ""))
        return v
    return check


def _int(lo: int, hi: int) -> Callable[[Any], int]:
    def check(v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            if isinstance(v, float) and math.isfinite(v) and v == int(v):
                v = int(v)
            else:
                raise ValueError(f"must be a whole number between {lo} and {hi}")
        if v < lo or v > hi:
            raise ValueError(f"must be a whole number between {lo} and {hi}")
        return int(v)
    return check


def _bool(v: Any) -> bool:
    if not isinstance(v, bool):
        raise ValueError("must be true or false")
    return v


def _locked_true(v: Any) -> bool:
    if v is not True:
        raise ValueError("is always on and cannot be switched off from the dashboard")
    return True


def _hhmm(v: Any) -> str:
    if not isinstance(v, str) or not HHMM_RE.match(v.strip()):
        raise ValueError("must be a time written HH:MM (24-hour, New York)")
    return v.strip()


def _pair(lo: int, hi: int) -> Callable[[Any], tuple[int, int]]:
    one = _int(lo, hi)

    def check(v: Any) -> tuple[int, int]:
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise ValueError(f"must be a pair [min, max] of whole numbers between {lo} and {hi}")
        a, b = one(v[0]), one(v[1])
        if a > b:
            raise ValueError("the first number must not be larger than the second")
        return (a, b)
    return check


def _symbols(v: Any) -> list[str]:
    if not isinstance(v, list) or not v or len(v) > 60:
        raise ValueError("must be a list of 1 to 60 ticker symbols")
    out: list[str] = []
    for s in v:
        if not isinstance(s, str):
            raise ValueError("must contain only ticker symbols (text)")
        s = s.strip().upper()
        if not SYMBOL_RE.match(s):
            raise ValueError(f"{s[:12]!r} is not a valid ticker symbol")
        if s not in out:
            out.append(s)
    return out


SCHEMA: dict[str, dict[str, Callable[[Any], Any]]] = {
    "risk": {
        "max_loss_per_trade_pct": _float(0.0, 0.10, lo_open=True),
        "daily_loss_limit_pct": _float(0.0, 0.25, lo_open=True),
        "max_trades_per_day": _int(0, 100),
        "max_open_positions": _int(1, 50),
        "max_positions_per_symbol": _int(1, 10),
        "max_portfolio_risk_pct": _float(0.0, 1.0, lo_open=True),
        "min_reward_risk": _float(0.0, 20.0),
        "min_credit_to_width": _float(0.0, 0.95),
        "entry_window_start": _hhmm,
        "entry_window_end": _hhmm,
        "flatten_time": _hhmm,
        "kelly_fraction": _float(0.0, 1.0),
        "max_contracts_per_trade": _int(1, 1000),
        "defined_risk_only": _locked_true,
        "pdt_guard": _bool,
    },
    "scan": {
        "universe": _symbols,
        "directional_dte": _pair(0, 60),
        "vol_dte": _pair(1, 180),
        "min_open_interest": _int(0, 10_000_000),
        "min_volume": _int(0, 100_000_000),
        "max_spread_pct": _float(0.0, 1.0, lo_open=True),
        "min_bid": _float(0.0, 100.0),
        "min_svi_zscore": _float(0.0, 50.0),
        "min_vol_edge": _float(0.0, 5.0),
        "min_direction": _float(0.0, 1.0),
        "min_expected_value": _float(0.0, 100_000.0),
        "min_score": _float(0.0, 100.0),
        "max_ideas_per_symbol": _int(1, 20),
    },
    "signals": {
        "opening_range_minutes": _int(5, 60),
        "breakout_bar_minutes": _int(1, 30),
        "min_relative_volume": _float(0.0, 20.0),
    },
}


def validate_update(body: Any) -> dict[str, dict[str, Any]]:
    """Return {section: {key: normalised value}}; raise ConfigError naming the first bad field."""
    if not isinstance(body, dict):
        raise ConfigError("the request body must be a JSON object like {\"risk\": {...}}")
    out: dict[str, dict[str, Any]] = {}
    for sec, vals in body.items():
        if sec not in SCHEMA:
            hint = " (broker settings are read-only here; edit config.toml)" if sec == "broker" else ""
            raise ConfigError(f"{sec}: unknown section; editable sections are {', '.join(SCHEMA)}{hint}")
        if not isinstance(vals, dict):
            raise ConfigError(f"{sec}: must be an object of settings")
        for key, v in vals.items():
            fn = SCHEMA[sec].get(key)
            if fn is None:
                raise ConfigError(f"{sec}.{key}: unknown setting")
            try:
                out.setdefault(sec, {})[key] = fn(v)
            except ValueError as exc:
                raise ConfigError(f"{sec}.{key}: {exc}") from None
    return out


def check_consistency(cfg: Any, updates: dict[str, dict[str, Any]]) -> None:
    """Cross-field rules evaluated on the merged result (cfg is the live Config; it is not modified)."""
    risk = updates.get("risk", {})
    start = risk.get("entry_window_start", cfg.risk.entry_window_start)
    end = risk.get("entry_window_end", cfg.risk.entry_window_end)
    if start >= end:
        field = "risk.entry_window_end" if "entry_window_end" in risk else "risk.entry_window_start"
        raise ConfigError(f"{field}: the entry window must start before it ends ({start} to {end})")


# ------------------------------------------------------------------ TOML writing
_BARE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _key(k: str) -> str:
    return k if _BARE.match(k) else json.dumps(k)


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError("non-finite number cannot be written to TOML")
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise ValueError(f"cannot write {type(v).__name__} to TOML")


def dumps_toml(data: dict[str, Any], _prefix: str = "") -> str:
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    for k, v in scalars.items():
        lines.append(f"{_key(k)} = {_value(v)}")
    for k, v in tables.items():
        name = f"{_prefix}.{_key(k)}" if _prefix else _key(k)
        if lines:
            lines.append("")
        lines.append(f"[{name}]")
        body = dumps_toml(v, name)
        if body:
            lines.append(body)
    return "\n".join(lines)


def persist_updates(path: str | Path, updates: dict[str, dict[str, Any]]) -> None:
    """Merge `updates` into the TOML file at `path` (created if missing) and write it atomically.

    Untouched sections keep their values; comments in an existing file are not preserved."""
    p = Path(path)
    existing: dict[str, Any] = {}
    if p.exists():
        with p.open("rb") as fh:
            existing = tomllib.load(fh)  # a malformed file raises here and is never overwritten
    for sec, vals in updates.items():
        table = existing.setdefault(sec, {})
        if not isinstance(table, dict):
            raise ValueError(f"{p}: [{sec}] is not a table")
        for k, v in vals.items():
            table[k] = list(v) if isinstance(v, tuple) else v
    text = dumps_toml(existing) + "\n"
    tomllib.loads(text)  # never write something we cannot read back
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, p)

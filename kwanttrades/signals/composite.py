"""Composite directional signal.

Active sources = signals whose source has a positive weight and non-zero confidence (neutral/missing
sources drop out).  Weights renormalise over the active sources (w~_i).  Then
    direction  = sum_i w~_i * conf_i * dir_i
    agreement  = fraction of w~ whose direction sign equals the composite's sign
    confidence = sum_i w~_i * conf_i * (0.5 + 0.5 * agreement)
"""
from __future__ import annotations

import math

import numpy as np

from kwanttrades.types import DirectionalSignal

from ._common import clamp, make_signal, neutral

SOURCE = "composite"


def _finite(x: object) -> float:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def combine(symbol: str, signals: list[DirectionalSignal], weights: dict[str, float]) -> DirectionalSignal:
    try:
        return _combine(symbol, signals or [], weights or {})
    except Exception as exc:  # noqa: BLE001
        return neutral(symbol, SOURCE, f"error: {type(exc).__name__}: {exc}")


def _combine(symbol: str, signals: list[DirectionalSignal], weights: dict[str, float]) -> DirectionalSignal:
    # one signal per source: keep the most confident duplicate
    by_src: dict[str, DirectionalSignal] = {}
    for s in signals:
        if s is None or s.source not in weights:
            continue
        cur = by_src.get(s.source)
        if cur is None or _finite(s.confidence) > _finite(cur.confidence):
            by_src[s.source] = s

    extra: dict[str, object] = {}
    reg = by_src.get("regime")
    if reg is not None and "risk_multiplier" in reg.detail:
        extra["risk_multiplier"] = reg.detail["risk_multiplier"]
    orb = by_src.get("orb")
    if orb is not None and "state" in orb.detail:
        extra["orb_state"] = orb.detail["state"]

    active: dict[str, DirectionalSignal] = {}
    missing: list[str] = []
    for src, w in weights.items():
        s = by_src.get(src)
        if _finite(w) <= 0 or s is None or _finite(s.confidence) <= 0:
            missing.append(src)
        else:
            active[src] = s
    ignored = sorted({s.source for s in signals if s is not None and s.source not in weights})
    if not active:
        return neutral(symbol, SOURCE, "no_active_sources", missing=missing, ignored=ignored, components={}, **extra)

    total_w = sum(_finite(weights[src]) for src in active)
    wn = {src: _finite(weights[src]) / total_w for src in active}
    cd = {src: clamp(_finite(s.direction), -1, 1) for src, s in active.items()}
    cc = {src: clamp(_finite(s.confidence), 0, 1) for src, s in active.items()}
    direction = sum(wn[k] * cc[k] * cd[k] for k in active)
    strength = sum(wn[k] * cc[k] for k in active)
    dsign = float(np.sign(direction))
    agreement = sum(wn[k] for k in active if dsign != 0 and np.sign(cd[k]) == dsign) if dsign else 0.0
    confidence = strength * (0.5 + 0.5 * agreement)
    components = {
        k: {"direction": cd[k], "confidence": cc[k], "weight": wn[k], "contribution": wn[k] * cc[k] * cd[k]}
        for k in active
    }
    detail = {"components": components, "agreement": float(agreement), "weights_used": wn,
              "missing": missing, "ignored": ignored, "strength": strength, **extra}
    return make_signal(symbol, SOURCE, direction, confidence, detail)

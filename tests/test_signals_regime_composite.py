from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from kwanttrades.config import SignalConfig
from kwanttrades.signals.composite import combine
from kwanttrades.signals.regime import regime_signal
from kwanttrades.types import DirectionalSignal

W = SignalConfig().weights


def daily_close(n: int, drift: float, start: float = 100.0, noise: float = 0.002, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    c = start * np.exp(np.cumsum(drift + rng.normal(0, noise, n)))
    idx = pd.bdate_range(end="2025-03-11", periods=n)
    return pd.DataFrame({"open": c, "high": c * 1.005, "low": c * 0.995, "close": c, "volume": 1e6}, index=idx)


def vix_series(last: float, n: int = 260, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    base = 17 + 3 * np.sin(np.linspace(0, 12, n)) + rng.normal(0, 0.3, n)
    base[-1] = last
    idx = pd.bdate_range(end="2025-03-11", periods=n)
    return pd.DataFrame({"close": base}, index=idx)


def vix3m_for(vix: pd.DataFrame, ratio: float) -> pd.DataFrame:
    out = vix.copy()
    out["close"] = vix["close"] / ratio
    return out


# ------------------------------------------------------------------ regime
def test_regime_uptrend_calm_has_full_risk_multiplier_and_bullish():
    spy = daily_close(120, 0.001)
    vix = vix_series(16.0)
    s = regime_signal(spy, vix, vix3m_for(vix, 0.85))
    assert s.source == "regime" and s.direction > 0 and s.confidence > 0
    assert s.detail["risk_multiplier"] == pytest.approx(1.0)
    assert s.detail["term_structure"] == "contango" and s.detail["trend"] > 0
    assert s.detail["spy_close"] > s.detail["ema20"] > s.detail["ema50"]
    json.dumps(s.detail)


def test_regime_downtrend_bearish():
    s = regime_signal(daily_close(120, -0.002), vix_series(16.0))
    assert s.direction < 0 and s.detail["below_both_emas"] is True
    assert s.detail["risk_multiplier"] < 1.0  # trend haircut


def test_regime_backwardation_lowers_multiplier():
    spy, vix = daily_close(120, 0.001), vix_series(19.0)
    contango = regime_signal(spy, vix, vix3m_for(vix, 0.85))
    back = regime_signal(spy, vix, vix3m_for(vix, 0.93))  # ratio 0.93 -> still contango, mild haircut
    deep = regime_signal(spy, vix, vix3m_for(vix, 1.08))  # ratio 1.08 -> backwardation
    assert deep.detail["term_structure"] == "backwardation" and deep.detail["vix_ratio"] > 1.0
    assert deep.detail["risk_multiplier"] <= 0.5
    assert deep.detail["risk_multiplier"] < back.detail["risk_multiplier"] < contango.detail["risk_multiplier"] + 1e-12
    assert deep.detail["risk_off"] is True
    extreme = regime_signal(spy, vix, vix3m_for(vix, 1.3))
    assert 0.25 <= extreme.detail["risk_multiplier"] <= deep.detail["risk_multiplier"]
    # without VIX3M the backwardation penalty is absent
    assert regime_signal(spy, vix).detail["risk_multiplier"] > deep.detail["risk_multiplier"]


def test_regime_high_vix_caps_multiplier():
    spy = daily_close(120, 0.001)
    m30 = regime_signal(spy, vix_series(30.0)).detail["risk_multiplier"]
    m35 = regime_signal(spy, vix_series(35.0)).detail["risk_multiplier"]
    m50 = regime_signal(spy, vix_series(50.0)).detail["risk_multiplier"]
    assert m30 <= 0.5 and m35 < m30 and m50 == pytest.approx(0.25)
    m25 = regime_signal(spy, vix_series(25.0)).detail["risk_multiplier"]
    assert 0.5 < m25 < 1.0


def test_regime_multiplier_always_bounded():
    rng = np.random.default_rng(42)
    for i in range(60):
        spy = daily_close(int(rng.integers(50, 300)), float(rng.normal(0, 0.003)), seed=i)
        vix = vix_series(float(rng.uniform(9, 80)), seed=i)
        v3 = vix3m_for(vix, float(rng.uniform(0.7, 1.4))) if i % 2 else None
        s = regime_signal(spy, vix, v3)
        assert 0.25 <= s.detail["risk_multiplier"] <= 1.0
        assert -1 <= s.direction <= 1 and 0 <= s.confidence <= 1
        json.dumps(s.detail)


def test_regime_neutral_when_data_missing():
    spy, vix = daily_close(120, 0.001), vix_series(16.0)
    for a, b in [(None, vix), (spy, None), (spy.iloc[:30], vix), (spy, vix.iloc[:5]), (pd.DataFrame(), vix)]:
        s = regime_signal(a, b)
        assert s.direction == 0.0 and s.confidence == 0.0
        assert s.detail["reason"] and s.detail["risk_multiplier"] == 1.0
    # garbage vix3m is ignored rather than raising
    s = regime_signal(spy, vix, pd.DataFrame({"close": []}))
    assert s.detail["term_structure"] == "unknown"


# ------------------------------------------------------------------ composite
def sig(source, d, c, **detail):
    return DirectionalSignal("SPY", source, d, c, dict(detail))


def test_composite_weighted_blend_arithmetic():
    s = combine("SPY", [sig("orb", 0.8, 0.8), sig("vwap", 0.5, 0.5)], W)
    wo, wv = 0.40 / 0.55, 0.15 / 0.55  # news/gap/regime missing -> renormalised
    assert s.source == "composite" and s.symbol == "SPY"
    assert s.direction == pytest.approx(wo * 0.8 * 0.8 + wv * 0.5 * 0.5)
    assert s.detail["agreement"] == pytest.approx(1.0)
    assert s.confidence == pytest.approx(wo * 0.8 + wv * 0.5)
    comp = s.detail["components"]
    assert set(comp) == {"orb", "vwap"} and comp["orb"]["weight"] == pytest.approx(wo)
    assert sum(v for v in s.detail["weights_used"].values()) == pytest.approx(1.0)
    assert set(s.detail["missing"]) == {"gap", "news", "regime"}
    json.dumps(s.detail)


def test_composite_renormalisation_single_source_equals_its_own_view():
    s = combine("SPY", [sig("news", -0.6, 0.5)], W)
    assert s.direction == pytest.approx(-0.3) and s.confidence == pytest.approx(0.5)
    assert s.detail["weights_used"] == {"news": pytest.approx(1.0)}


def test_composite_agreement_fraction_of_weight():
    sigs = [sig("orb", 0.9, 0.9), sig("vwap", -0.8, 0.8), sig("news", 0.4, 0.6), sig("gap", -0.5, 0.5)]
    s = combine("SPY", sigs, W)
    total = 0.40 + 0.15 + 0.25 + 0.05
    assert s.direction > 0
    assert s.detail["agreement"] == pytest.approx((0.40 + 0.25) / total)
    full = combine("SPY", [sig("orb", 0.9, 0.9), sig("news", 0.4, 0.6)], W)
    assert full.detail["agreement"] == pytest.approx(1.0)
    # disagreement lowers confidence relative to the same strength with full agreement
    assert s.confidence < s.detail["strength"]
    assert full.confidence == pytest.approx(full.detail["strength"])


def test_composite_opposing_equal_signals_cancel():
    s = combine("SPY", [sig("orb", 0.8, 0.5), sig("news", -0.8, 0.5)], {"orb": 1.0, "news": 1.0})
    assert s.direction == pytest.approx(0.0, abs=1e-12) and s.detail["agreement"] == 0.0


def test_composite_zero_confidence_sources_drop_out():
    a = combine("SPY", [sig("orb", 0.8, 0.8), sig("vwap", 0.0, 0.0), sig("news", 1.0, 0.0)], W)
    b = combine("SPY", [sig("orb", 0.8, 0.8)], W)
    assert a.direction == pytest.approx(b.direction) and a.confidence == pytest.approx(b.confidence)
    assert set(a.detail["components"]) == {"orb"}


def test_composite_empty_unknown_and_nonpositive_weights_neutral():
    for sigs, w in [([], W), ([sig("orb", 1, 1)], {}), ([sig("foo", 1, 1)], W), ([sig("orb", 1, 1)], {"orb": 0.0}),
                    ([sig("orb", 1, 1)], {"orb": -1.0})]:
        s = combine("SPY", sigs, w)
        assert s.direction == 0.0 and s.confidence == 0.0 and s.detail["reason"]
    assert combine("SPY", [sig("foo", 1, 1)], W).detail["ignored"] == ["foo"]
    assert combine("SPY", None, None).confidence == 0.0  # type: ignore[arg-type]


def test_composite_clamps_and_survives_nan():
    s = combine("SPY", [sig("orb", 5.0, 3.0), sig("vwap", float("nan"), float("nan"))], W)
    assert -1 <= s.direction <= 1 and 0 <= s.confidence <= 1
    assert s.direction == pytest.approx(1.0 * 1.0 * (0.40 / 0.40))
    assert set(s.detail["components"]) == {"orb"}


def test_composite_passes_through_regime_multiplier_and_orb_state():
    sigs = [sig("orb", 0.7, 0.7, state="triggered"), sig("regime", 0.0, 0.0, risk_multiplier=0.4)]
    s = combine("SPY", sigs, W)
    assert s.detail["risk_multiplier"] == 0.4 and s.detail["orb_state"] == "triggered"
    f = combine("SPY", [DirectionalSignal("SPY", "orb", 0, 0, {"state": "failed"})], W)
    assert f.detail["orb_state"] == "failed" and f.direction == 0.0


def test_composite_duplicate_source_keeps_most_confident():
    s = combine("SPY", [sig("orb", 0.2, 0.2), sig("orb", -0.9, 0.9)], W)
    assert s.direction == pytest.approx(-0.81)


def test_composite_end_to_end_with_real_signals():
    from kwanttrades.signals import orb_signal, vwap_signal, gap_signal
    from tests.test_signals_synth import build

    bars, daily = build("up")
    cfg = SignalConfig()
    parts = [orb_signal("SPY", bars, daily, cfg), vwap_signal("SPY", bars), gap_signal("SPY", bars, daily),
             regime_signal(daily_close(120, 0.001), vix_series(15.0))]
    s = combine("SPY", parts, cfg.weights)
    assert s.direction > 0 and s.detail["agreement"] > 0.5
    assert "orb" in s.detail["components"] and s.detail["orb_state"] == "triggered"
    json.dumps(s.detail)

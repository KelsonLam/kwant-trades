"""Session clock and total-variance vol edges per expiry."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pytest

from kwanttrades.config import Config
from kwanttrades.quant import bs
from kwanttrades.quant.surface import build_surface
from kwanttrades.strategy import StrategyEngine, SymbolAnalysis
from kwanttrades.strategy import clock
from kwanttrades.strategy import structures as st
from kwanttrades.types import NY, DirectionalSignal, OptionChain, OptionQuote, Right, VolView
from tests.test_quant_synth import half_spread, make_chain
from tests.test_quant_volforecast import gbm_bars
from tests.test_strategy_fixtures import load_bars

MON = date(2026, 11, 16)


def ny(*a):
    return datetime(*a, tzinfo=NY)


# ----------------------------------------------------------------------------- clock


def test_weekend_and_monday_premarket_same_sessions():
    on = 0.3
    s = [clock.trading_sessions(t, MON, on) for t in (ny(2026, 11, 13, 16, 30), ny(2026, 11, 14, 12, 0), ny(2026, 11, 15, 23, 54), ny(2026, 11, 16, 9, 0))]
    assert s == pytest.approx([1.0] * 4)
    # same w_f for any ExpiryContext: w_f depends only on sessions
    ch = make_chain(days=(7,), spot=400.0, strikes=np.arange(360.0, 440.01, 1.0))
    ctx = st.expiry_context(ch, build_surface(ch, Config().scan), ch.expiries()[0], Config().scan)
    eng = StrategyEngine(Config())
    wf = [eng.variance_view(ctx, x, 0.18)["w_f"] for x in s]
    assert max(wf) - min(wf) < 1e-15 and wf[0] == pytest.approx(0.18**2 / 252)


def test_partial_session_and_holidays():
    on = 0.25
    assert clock.trading_sessions(ny(2026, 11, 16, 12, 45), MON, on) == pytest.approx(0.75 * 0.5)
    assert clock.trading_sessions(ny(2026, 11, 16, 16, 0), MON, on) == 0.0
    assert clock.trading_sessions(ny(2026, 11, 16, 9, 30), MON, on) == pytest.approx(0.75)
    # Thanksgiving (Thu 26 Nov) is not a session
    assert clock.trading_sessions(ny(2026, 11, 25, 17, 0), date(2026, 11, 27), on) == pytest.approx(1.0)
    assert not clock.is_session(date(2026, 11, 26)) and clock.is_session(date(2026, 11, 27))
    assert not clock.is_session(date(2026, 11, 14))
    # a full week from Monday pre-market to Friday close = 5 sessions
    assert clock.trading_sessions(ny(2026, 11, 16, 8, 0), date(2026, 11, 20), on) == pytest.approx(5.0)


def test_total_variance_monotone_in_trading_time():
    on = 0.25
    asof = ny(2026, 11, 16, 11, 0)
    exps = [asof.date() + timedelta(days=d) for d in range(0, 60)]
    s = [clock.trading_sessions(asof, e, on) for e in exps]
    assert all(b >= a for a, b in zip(s, s[1:]))
    times = [ny(2026, 11, 13, 9, 0) + timedelta(minutes=37 * i) for i in range(300)]
    s2 = [clock.trading_sessions(t, date(2026, 11, 30), on) for t in times]
    assert all(b <= a + 1e-12 for a, b in zip(s2, s2[1:]))
    w = [0.2**2 * x / 252 for x in s]
    assert all(b >= a for a, b in zip(w, w[1:]))


def test_overnight_share_bounds():
    assert clock.overnight_share(None) == 0.25
    assert clock.overnight_share(gbm_bars(n_days=10)) == 0.25
    assert clock.overnight_share(gbm_bars(n_days=300, overnight_frac=0.0)) == pytest.approx(0.10)
    assert clock.overnight_share(gbm_bars(n_days=300, overnight_frac=0.9, seed=1)) == pytest.approx(0.50)
    mid = clock.overnight_share(gbm_bars(n_days=400, overnight_frac=0.3, seed=2))
    assert 0.2 < mid < 0.4
    real = clock.overnight_share(load_bars("spy_daily_real.csv"))
    assert 0.10 <= real <= 0.50


# ----------------------------------------------------------------------------- calendar artefact


SUNDAY = ny(2026, 11, 15, 20, 0)
SIGMA_SESSION = 0.16


def flat_chain_constant_session_variance(on: float) -> OptionChain:
    """Each expiry's flat IV gives exactly SIGMA_SESSION^2/252 of variance per SESSION, so the
    1-DTE IV looks 'high' on the calendar clock purely as annualisation."""
    spot, r, q = 400.0, 0.04, 0.01
    strikes = np.arange(360.0, 440.01, 1.0)
    quotes = []
    for e in (date(2026, 11, 16), date(2026, 11, 18), date(2026, 11, 20), date(2026, 12, 16)):
        T = OptionChain("X", spot, SUNDAY, [], r, q).T(e)
        sessions = clock.trading_sessions(SUNDAY, e, on)
        iv = SIGMA_SESSION * np.sqrt(sessions / 252.0 / T)
        for cp, right in ((1, Right.CALL), (-1, Right.PUT)):
            px = np.asarray(bs.price(spot, strikes, T, r, q, iv, cp))
            hs = half_spread(px)
            for K, p, h in zip(strikes, px, hs):
                quotes.append(OptionQuote("X", e, float(K), right, float(max(p - h, 0.0)), float(p + h), volume=50, open_interest=500))
    return OptionChain("X", spot, SUNDAY, quotes, r, q)


def test_calendar_artefact_gives_no_vol_edge():
    on = clock.DEFAULT_OVERNIGHT_SHARE
    ch = flat_chain_constant_session_variance(on)
    surf = build_surface(ch, Config().scan)
    first = surf.slices[date(2026, 11, 16)]
    assert first.atm_iv > 1.25 * SIGMA_SESSION  # looks rich on the calendar clock
    vv = VolView("X", 31, surf.atm_iv_at(31), SIGMA_SESSION, 0.0, {"session_forecast": SIGMA_SESSION})
    eng = StrategyEngine(Config())
    sig = DirectionalSignal("X", "composite", 0.6, 0.7)
    a = SymbolAnalysis("X", ch, surf, vv, [], sig, overnight_share=on)
    for e in surf.slices:
        ctx = st.expiry_context(ch, surf, e, Config().scan)
        x = eng.expiry_view(a, ctx)
        assert abs(x["edge"]) < 0.03, (e, x["edge"])
        assert x["vol_scale"] == pytest.approx(1.0, abs=0.03)
    # bullish: the "no vol view" row (both vertical types), never the rich row's credit-only
    names = {c.strategy for c, _ in eng.candidates(a)}
    assert "bull_call_spread" in names and "bull_put_spread" in names and "long_call" not in names
    # neutral: no vol trade at all
    a.direction = DirectionalSignal("X", "composite", 0.0, 0.5)
    assert eng.candidates(a) == []


def test_analyze_fills_expiry_views():
    ch = make_chain(days=(7, 30), spot=400.0, strikes=np.arange(340.0, 460.01, 1.0))
    eng = StrategyEngine(Config())
    a = eng.analyze("X", ch, gbm_bars(n_days=300, sigma=0.2, s0=400.0, overnight_frac=0.2), DirectionalSignal("X", "composite", 0.6, 0.7))
    assert set(a.expiry_vols) == set(ch.expiries())
    assert 0.10 <= a.overnight_share <= 0.50
    for e, x in a.expiry_vols.items():
        assert x["sessions"] == pytest.approx(clock.trading_sessions(ch.asof, e, a.overnight_share))
        assert x["edge"] == pytest.approx(np.sqrt(x["w_f"] / x["w_m"]) - 1)
        assert any(str(e) in n and "total-variance vol edge" in n for n in a.notes)
    ve = max(a.expiry_vols)
    assert a.vol_view.vol_edge == pytest.approx(a.expiry_vols[ve]["edge"])
    assert (a.vol_view.forecast_rv - a.vol_view.atm_iv) / a.vol_view.atm_iv == pytest.approx(a.vol_view.vol_edge, abs=1e-9)
    for i in eng.generate(a):
        assert any(r.startswith("Vol at this expiry") for r in i.rationale)

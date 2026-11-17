from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pytest

from kwanttrades.app.session import ManualClock
from kwanttrades.data.recorder import (
    ReplayError,
    ReplayProvider,
    Recorder,
    chain_from_dict,
    chain_to_dict,
    load_snapshot,
    save_snapshot,
)
from kwanttrades.data.synthetic import SyntheticProvider
from kwanttrades.quant import bs
from kwanttrades.types import NY, MarketDataProvider

START = date(2026, 11, 2)


def at(d, hh, mm):
    return datetime(2026, 11, d, hh, mm, tzinfo=NY)


def make(n_days=10, when=at(12, 11, 0), **kw):
    clock = ManualClock(when)
    return SyntheticProvider.simulated(["SPY", "AAPL"], n_days=n_days, start_date=START, clock=clock, **kw), clock


def test_provider_satisfies_protocol_and_is_deterministic():
    a, _ = make()
    b, _ = make()
    assert isinstance(a, MarketDataProvider)
    assert a.intraday_bars("SPY", "1m", 2).equals(b.intraday_bars("SPY", "1m", 2))
    qa, qb = a.chain("SPY").quotes, b.chain("SPY").quotes
    assert qa == qb


def test_chain_prices_match_bs_at_surface_and_spread_brackets_mid():
    p, _ = make()
    ch = p.chain("SPY", 1, 30)
    assert len(ch.quotes) > 200
    level = p.iv_level("SPY", ch.asof.date())
    prev = float(p._daily["SPY"].loc[[d < ch.asof.date() for d in p._daily["SPY"].index], "close"].iloc[-1])
    checked = 0
    for q in ch.quotes:
        T = ch.T(q.expiry)
        iv = float(p.surface_iv("SPY", ch.spot, q.strike, T, level, prev))
        fair = bs.price(ch.spot, q.strike, T, ch.risk_free, ch.div_yield, iv, bs.cp_of(q.right))
        assert q.bid <= fair + 1e-9 and fair <= q.ask + 1e-9
        assert q.bid <= q.mid <= q.ask and q.bid > 0
        assert abs(q.mid - fair) <= max(0.5 * q.spread, 0.011)
        checked += 1
    assert checked == len(ch.quotes)
    # skew: OTM puts are priced richer in vol than equidistant OTM calls
    T = 30 / 365
    assert p.surface_iv("SPY", 100.0, 90.0, T, 0.2) > p.surface_iv("SPY", 100.0, 110.0, T, 0.2)


def test_spread_widens_with_moneyness():
    p, _ = make()
    ch = p.chain("SPY", 7, 30)
    exp = ch.expiries()[0]
    calls = [q for q in ch.for_expiry(exp) if q.right.value == "C"]
    atm = min(calls, key=lambda q: abs(q.strike - ch.spot))
    far = max((q for q in calls if q.mid >= 0.05), key=lambda q: q.strike)
    assert far.spread_pct > atm.spread_pct


def test_clock_gating_bars_spot_and_daily():
    p, clock = make(when=at(12, 10, 0))
    bars = p.intraday_bars("SPY", "1m", 1)
    assert bars.index[-1] == at(12, 9, 59)  # only completed minutes
    assert p.spot("SPY") == pytest.approx(float(bars["close"].iloc[-1]))
    d = p.daily_bars("SPY")
    assert d.index[-1].date() == date(2026, 11, 11)  # today's daily bar not visible yet
    clock.set(at(12, 16, 0))
    assert p.daily_bars("SPY").index[-1].date() == date(2026, 11, 12)
    assert p.intraday_bars("SPY", "1m", 1).index[-1] == at(12, 15, 59)
    five = p.intraday_bars("SPY", "5m", 1)
    assert five.index[-1] == at(12, 15, 55)
    clock.set(at(12, 10, 2))
    five = p.intraday_bars("SPY", "5m", 1)
    assert five.index[-1] == at(12, 9, 55)  # bar 10:00-10:05 is still forming


def test_chain_has_no_expired_expiries_and_zero_volume_before_open():
    p, clock = make(when=at(12, 9, 0))
    ch = p.chain("SPY")
    assert all(q.volume == 0 for q in ch.quotes)
    clock.set(at(12, 16, 0))
    assert all(q.expiry > date(2026, 11, 12) for q in p.chain("SPY").quotes)


def test_simulated_days_do_not_depend_on_later_days():
    a, _ = make(n_days=4, when=at(5, 12, 0))
    b, _ = make(n_days=10, when=at(5, 12, 0))
    assert a.intraday_bars("SPY", "1m", 5).equals(b.intraday_bars("SPY", "1m", 5))
    assert a.daily_bars("SPY").equals(b.daily_bars("SPY"))
    qa, qb = a.chain("SPY").quotes, b.chain("SPY").quotes
    assert qa == qb


def test_vix_proxy_for_regime_is_served():
    p, _ = make()
    assert p.daily_bars("^VIX")["close"].iloc[-1] > 5
    assert p.daily_bars("^VIX3M")["close"].iloc[-1] > p.daily_bars("^VIX")["close"].iloc[-1]


def test_simulated_bars_look_sane():
    p, clock = make(when=at(13, 16, 0))
    day = p.intraday_bars("SPY", "1m", 1)
    assert len(day) == 390
    assert (day["high"] >= day[["open", "close"]].max(axis=1) - 1e-9).all()
    assert (day["low"] <= day[["open", "close"]].min(axis=1) + 1e-9).all()
    v = day["volume"].to_numpy()
    assert v[:15].mean() > v[150:240].mean() < v[-15:].mean()  # U-shaped volume


# ------------------------------------------------------------------ recorder
def test_snapshot_round_trip(tmp_path):
    p, _ = make()
    ch = p.chain("SPY", 0, 20)
    path = save_snapshot(ch, tmp_path)
    assert path.name.endswith(".json.gz") and path.parent.name == "SPY"
    back = load_snapshot(path)
    assert back.quotes == ch.quotes and back.spot == ch.spot and back.asof == ch.asof
    assert chain_from_dict(chain_to_dict(ch)).quotes == ch.quotes


def test_recorder_and_replay_as_of_lookup(tmp_path):
    p, clock = make(when=at(12, 10, 0))
    rec = Recorder(p, tmp_path, max_dte=20)
    for hh, mm in [(10, 0), (11, 0), (12, 0)]:
        clock.set(at(12, hh, mm))
        files, errors = rec.record_once(["SPY", "NOPE"])
        assert len(files) == 1 and "NOPE" in errors
    rp_clock = ManualClock(at(12, 9, 0))
    rp = ReplayProvider(tmp_path, clock=rp_clock)
    assert rp.symbols() == ["SPY"] and len(rp.snapshot_times("SPY")) == 3
    with pytest.raises(ReplayError):
        rp.chain("SPY")  # nothing recorded yet at 09:00
    rp_clock.set(at(12, 10, 0))
    assert rp.chain("SPY").asof == at(12, 10, 0)
    rp_clock.set(at(12, 11, 30))
    assert rp.chain("SPY").asof == at(12, 11, 0)  # latest at or before the clock
    rp_clock.set(at(12, 15, 0))
    ch = rp.chain("SPY", 0, 3)
    assert ch.asof == at(12, 12, 0) and all((q.expiry - date(2026, 11, 12)).days <= 3 for q in ch.quotes)
    assert rp.spot("SPY") == pytest.approx(ch.spot)

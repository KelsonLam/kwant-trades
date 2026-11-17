"""Offline tests for OpenBBProvider; the CBOE payload is fabricated in the exact live shape."""
from __future__ import annotations

import asyncio
import builtins
import sys
from datetime import date, datetime

import pytest

from kwanttrades.data import openbb_provider as ob
from kwanttrades.data.openbb_provider import OpenBBProvider
from kwanttrades.types import NY, MarketDataProvider, Right

TODAY = date(2026, 11, 15)  # a Sunday; the feed holds Friday's close


def _row(option, bid, ask, iv=0.16, oi=100.0, vol=10.0, delta=0.5, **kw):
    r = {
        "option": option, "bid": bid, "bid_size": 5.0, "ask": ask, "ask_size": 5.0, "iv": iv,
        "open_interest": oi, "volume": vol, "delta": delta, "gamma": 0.02, "vega": 0.4,
        "theta": -0.5, "rho": 0.1, "theo": 1.0, "change": 0.0, "open": 0.0, "high": 0.0,
        "low": 0.0, "tick": "up", "last_trade_price": 1.0, "last_trade_time": "2026-11-13T16:14:48",
        "percent_change": 0.0, "prev_day_close": 1.0,
    }
    r.update(kw)
    return r


def raw_payload(**data_over):
    data = {
        "symbol": "SPY", "security_type": "stock", "current_price": 754.37,
        "bid": 749.45, "ask": 749.63, "iv30": 14.874, "last_trade_time": "2026-11-13T16:00:00",
        "options": [
            _row("SPY261116C00750000", 5.0, 5.2),                       # DTE 1
            _row("SPY261120P00750000", 4.0, 4.2, delta=-0.4),           # DTE 5
            _row("SPY261120C00760000", float("nan"), 0.30, oi=float("nan"), vol=float("nan")),  # ask only
            _row("SPY261120C00800000", 0.0, 0.0),                       # no market -> dropped
            _row("SPY261218C00750000", 15.0, 15.4),                     # DTE 33
            _row("SPY261113C00750000", 1.0, 1.1),                       # expired (DTE -2)
            _row("B1SPY261120C00710000", 1.0, 1.2),                     # adjusted root -> ignored
            _row("SPY261120C00770000", 0.2, 0.3, iv=0.0),               # no IV/greeks
        ],
    }
    data.update(data_over)
    return {"timestamp": "2026-11-16 02:37:41", "data": data, "symbol": "SPY"}


class Fallback:
    def __init__(self):
        self.calls = []

    def spot(self, symbol):
        self.calls.append(("spot", symbol))
        return 111.0

    def chain(self, symbol, min_dte=0, max_dte=45):
        raise AssertionError

    def intraday_bars(self, symbol, interval="1m", days=1):
        self.calls.append(("intraday", symbol, interval, days))
        return "intra"

    def daily_bars(self, symbol, years=2.0):
        self.calls.append(("daily", symbol, years))
        return "daily"

    def risk_free_rate(self):
        self.calls.append(("rf",))
        return 0.0412


@pytest.fixture
def fetched(monkeypatch):
    calls = []
    payload = {"p": raw_payload()}

    def fake(symbol):
        calls.append(symbol)
        return payload["p"]

    monkeypatch.setattr(ob, "_fetch_raw", fake)
    monkeypatch.setattr(ob, "_today", lambda: TODAY)
    fake.calls = calls
    fake.payload = payload
    return fake


def test_protocol_conformance():
    assert isinstance(OpenBBProvider(), MarketDataProvider)


def test_chain_mapping_and_occ(fetched):
    ch = OpenBBProvider(fallback=Fallback()).chain("spy", 0, 45)
    assert ch.underlying == "SPY"
    assert ch.spot == 754.37
    assert ch.asof == datetime(2026, 11, 13, 16, 0, tzinfo=NY)
    assert ch.risk_free == 0.0412
    occ = [q.occ_symbol for q in ch.quotes]
    assert occ == ["SPY261116C00750000", "SPY261120P00750000", "SPY261120C00760000",
                   "SPY261120C00770000", "SPY261218C00750000"]
    put = ch.quotes[1]
    assert put.right == Right.PUT and put.strike == 750.0 and put.expiry == date(2026, 11, 20)
    assert (put.bid, put.ask, put.volume, put.open_interest, put.vendor_iv) == (4.0, 4.2, 10, 100, 0.16)
    assert put.last == 1.0
    assert isinstance(put.volume, int) and isinstance(put.open_interest, int)


def test_dte_filter(fetched):
    p = OpenBBProvider()
    ch = p.chain("SPY", min_dte=2, max_dte=10)
    assert {q.expiry for q in ch.quotes} == {date(2026, 11, 20)}
    ch2 = p.chain("SPY", min_dte=20, max_dte=45)
    assert [q.expiry for q in ch2.quotes] == [date(2026, 12, 18)]
    with pytest.raises(RuntimeError, match="no quoted"):
        p.chain("SPY", min_dte=100, max_dte=200)


def test_nan_handling_and_one_sided_kept(fetched):
    ch = OpenBBProvider().chain("SPY", 0, 45)
    q = next(x for x in ch.quotes if x.strike == 760.0)
    assert (q.bid, q.ask, q.volume, q.open_interest) == (0.0, 0.30, 0, 0)
    assert all(x.strike != 800.0 for x in ch.quotes)  # nothing quoted -> dropped
    assert next(x for x in ch.quotes if x.strike == 770.0).vendor_iv is None


def test_spot_fallback_to_stock_mid_then_fallback_provider(fetched):
    fetched.payload["p"] = raw_payload(current_price=0.0)
    assert OpenBBProvider().chain("SPY").spot == pytest.approx(749.54)
    fetched.payload["p"] = raw_payload(current_price=None, bid=None, ask=None)
    with pytest.raises(RuntimeError, match="no fallback"):
        OpenBBProvider().chain("SPY")
    assert OpenBBProvider(fallback=Fallback()).chain("SPY").spot == 111.0


def test_asof_fallbacks(fetched, monkeypatch):
    fetched.payload["p"] = raw_payload(last_trade_time=None)
    assert OpenBBProvider().chain("SPY").asof == datetime(2026, 11, 16, 2, 37, 41, tzinfo=NY)
    fixed = datetime(2026, 11, 15, 12, 0, tzinfo=NY)
    p = raw_payload(last_trade_time=None)
    p["timestamp"] = None
    fetched.payload["p"] = p
    monkeypatch.setattr(ob, "now_ny", lambda: fixed)
    assert OpenBBProvider().chain("SPY").asof == fixed


def test_vendor_greeks(fetched):
    g = OpenBBProvider().vendor_greeks("SPY")
    assert set(g["SPY261120P00750000"]) == {"delta", "gamma", "theta", "vega", "iv"}
    assert g["SPY261120P00750000"]["delta"] == -0.4
    assert g["SPY261116C00750000"]["iv"] == 0.16
    assert "SPY261120C00770000" not in g  # iv == 0 -> no greeks
    assert not any(k.startswith("B1") for k in g)


def test_vendor_greeks_empty_when_feed_has_none(fetched):
    p = raw_payload()
    for r in p["data"]["options"]:
        r["iv"] = 0.0
    fetched.payload["p"] = p
    assert OpenBBProvider().vendor_greeks("SPY") == {}


def test_empty_feed_errors(fetched):
    fetched.payload["p"] = {"data": {"options": []}}
    with pytest.raises(RuntimeError, match="empty option chain"):
        OpenBBProvider().chain("SPY")
    fetched.payload["p"] = {}
    with pytest.raises(RuntimeError, match="no data"):
        OpenBBProvider().chain("SPY")


def test_delegation(fetched):
    fb = Fallback()
    p = OpenBBProvider(fallback=fb)
    assert p.spot("SPY") == 111.0
    assert p.intraday_bars("SPY", "5m", 2) == "intra"
    assert p.daily_bars("SPY", 3.0) == "daily"
    assert p.risk_free_rate() == 0.0412
    assert ("intraday", "SPY", "5m", 2) in fb.calls and ("daily", "SPY", 3.0) in fb.calls


def test_no_fallback_raises_clear_errors():
    p = OpenBBProvider()
    for fn in (lambda: p.spot("SPY"), lambda: p.intraday_bars("SPY"),
               lambda: p.daily_bars("SPY"), p.risk_free_rate):
        with pytest.raises(RuntimeError, match="fallback"):
            fn()


def test_cache_ttl(fetched):
    now = [1000.0]
    p = OpenBBProvider(cache_ttl=60.0)
    p._clock = lambda: now[0]
    a = p.chain("SPY", 0, 45)
    assert p.chain("SPY", 0, 45) is a and fetched.calls == ["SPY"]
    p.chain("SPY", 2, 10)  # different window: new chain but shares the raw fetch
    assert fetched.calls == ["SPY"]
    p.vendor_greeks("SPY")
    assert fetched.calls == ["SPY"]
    now[0] += 61
    b = p.chain("SPY", 0, 45)
    assert b is not a and fetched.calls == ["SPY", "SPY"]


def test_missing_install_error(monkeypatch):
    for name in [m for m in sys.modules if m.startswith("openbb_cboe")]:
        monkeypatch.delitem(sys.modules, name)
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("openbb_cboe"):
            raise ImportError("No module named 'openbb_cboe'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="pip install openbb-core openbb-cboe openbb-derivatives"):
        OpenBBProvider().chain("SPY")


def test_run_coro_without_and_with_running_loop():
    async def co():
        return 42

    assert ob._run_coro(co) == 42

    async def outer():
        return ob._run_coro(co)  # loop already running in this thread

    assert asyncio.run(outer()) == 42

    async def boom():
        raise ValueError("x")

    async def outer2():
        return ob._run_coro(boom)

    with pytest.raises(ValueError):
        asyncio.run(outer2())


def test_cert_error_detection():
    assert ob._is_cert_error(Exception("[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired"))
    assert not ob._is_cert_error(ValueError("other"))

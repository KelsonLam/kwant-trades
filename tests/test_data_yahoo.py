from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from kwanttrades.data import yahoo
from kwanttrades.data.yahoo import YahooProvider, normalise_bars, parse_option_frame, retry_call
from kwanttrades.types import NY, MarketDataProvider, OptionChain, Right

NOW = datetime(2026, 11, 13, 10, 0, tzinfo=NY)  # a Friday


def frame(rows):
    cols = ["contractSymbol", "lastTradeDate", "strike", "lastPrice", "bid", "ask", "volume", "openInterest", "impliedVolatility"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)


CALLS = frame([
    ("SPY261120C00700000", None, 700.0, 5.0, 4.9, 5.1, 120.0, 10, 0.18),
    ("SPY261120C00705000", None, 705.0, 2.0, 0.0, 2.2, np.nan, np.nan, 0.00001),   # one-sided, NaNs
    ("SPY261120C00710000", None, 710.0, 0.0, 0.0, 0.0, 0.0, 5, 0.5),                # dead row -> dropped
    ("SPY261120C00715000", None, 715.0, 1.0, np.nan, np.nan, 3.0, 2, 0.2),          # NaN both -> dropped
    ("SPY261120C00720000", None, 720.0, 0.5, 0.6, 0.4, 1.0, 1, 0.2),                # crossed -> dropped
    ("SPY261120C00000000", None, 0.0, 0.5, 0.4, 0.6, 1.0, 1, 0.2),                  # bad strike -> dropped
])


def test_parse_option_frame_cleaning():
    qs = parse_option_frame(CALLS, "SPY", date(2026, 11, 20), Right.CALL)
    assert [q.strike for q in qs] == [700.0, 705.0]
    a, b = qs
    assert (a.bid, a.ask, a.volume, a.open_interest, a.vendor_iv) == (4.9, 5.1, 120, 10, 0.18)
    assert b.bid == 0.0 and b.ask == 2.2 and b.volume == 0 and b.open_interest == 0
    assert b.vendor_iv is None  # placeholder IV
    assert b.mid == 2.2  # one-sided: ask
    assert a.occ_symbol == "SPY261120C00700000"
    assert parse_option_frame(None, "SPY", date(2026, 11, 20), Right.CALL) == []
    assert parse_option_frame(CALLS.iloc[0:0], "SPY", date(2026, 11, 20), Right.PUT) == []


def raw_intraday():
    idx = pd.date_range("2026-11-13 08:00", "2026-11-13 16:30", freq="30min", tz=NY)
    n = len(idx)
    return pd.DataFrame({"Open": 1.0 + np.arange(n), "High": 2.0 + np.arange(n), "Low": 0.5 + np.arange(n),
                         "Close": 1.5 + np.arange(n), "Volume": 100.0, "Dividends": 0.0, "Stock Splits": 0.0}, index=idx)


def test_normalise_intraday_session_filter_and_case():
    out = normalise_bars(raw_intraday(), intraday=True)
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert str(out.index.tz) == "America/New_York"
    assert out.index[0].strftime("%H:%M") == "09:30"
    assert out.index[-1].strftime("%H:%M") == "15:30"  # 16:00 bar excluded
    assert out.index.is_monotonic_increasing


def test_normalise_converts_utc_and_naive_index():
    df = raw_intraday()
    utc = df.copy()
    utc.index = utc.index.tz_convert("UTC")
    b = normalise_bars(df, True)
    pd.testing.assert_frame_equal(normalise_bars(utc, True), b, check_freq=False)
    naive = df.copy()
    naive.index = naive.index.tz_convert("UTC").tz_localize(None)  # naive means UTC
    pd.testing.assert_frame_equal(normalise_bars(naive, True), b, check_freq=False)


def test_normalise_drops_nan_dupes_and_keeps_daily():
    df = raw_intraday().iloc[12:16].copy()
    df.iloc[1, df.columns.get_loc("Close")] = np.nan
    df.iloc[2, df.columns.get_loc("Volume")] = np.nan
    df = pd.concat([df, df.iloc[[0]]])
    out = normalise_bars(df, True)
    assert len(out) == 3 and out["volume"].notna().all() and out.index.is_unique
    daily = pd.DataFrame({"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5], "Volume": [10.0]},
                         index=pd.DatetimeIndex(["2026-11-12"], tz="America/New_York"))
    assert len(normalise_bars(daily, intraday=False)) == 1  # midnight bar kept for daily
    assert normalise_bars(None, True).empty
    with pytest.raises(yahoo.DataUnavailable):
        normalise_bars(pd.DataFrame({"Close": [1.0]}, index=pd.DatetimeIndex(["2026-11-12"])), False)


# ------------------------------------------------------------------ retry / backoff


class RateLimit(Exception):
    pass


def test_retry_backoff_then_success(monkeypatch):
    sleeps = []
    monkeypatch.setattr(yahoo.time, "sleep", sleeps.append)
    n = {"c": 0}

    def fn():
        n["c"] += 1
        if n["c"] < 3:
            raise RateLimit("Too Many Requests. Rate limited. Try after a while.")
        return "ok"

    assert retry_call(fn) == "ok"
    assert sleeps == [1.0, 2.0]


def test_retry_gives_up_and_ignores_other_errors(monkeypatch):
    sleeps = []
    monkeypatch.setattr(yahoo.time, "sleep", sleeps.append)

    def always():
        raise RateLimit("429 too many requests")

    with pytest.raises(RateLimit):
        retry_call(always, attempts=3)
    assert len(sleeps) == 2
    sleeps.clear()

    def other():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        retry_call(other)
    assert sleeps == []


# ------------------------------------------------------------------ provider with a fake ticker


class FakeTicker:
    def __init__(self, sym):
        self.sym = sym
        self.calls = {"option_chain": [], "history": 0, "options": 0}
        self.fail_first = 0
        self.options_list = ("2026-11-13", "2026-11-16", "2026-11-20", "2026-12-18", "2027-03-19")
        self.fast_info = {"last_price": 750.0}
        self.info = {"dividendYield": 1.0, "trailingAnnualDividendYield": 0.0075}
        self.calendar = {"Earnings Date": [date(2026, 11, 2), date(2027, 1, 28)]}

    @property
    def options(self):
        self.calls["options"] += 1
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RateLimit("Too Many Requests")
        return self.options_list

    def option_chain(self, d):
        self.calls["option_chain"].append(d)

        class OC:
            calls = CALLS
            puts = CALLS

        return OC()

    def history(self, **kw):
        self.calls["history"] += 1
        self.last_history_kwargs = kw
        if self.sym == "^IRX":
            return pd.DataFrame({"Close": [4.10, 4.12]}, index=pd.date_range("2026-11-12", periods=2, tz="America/Chicago"))
        if kw.get("interval") == "1d":
            return pd.DataFrame({"Open": [1.0, 2.0], "High": [2.0, 3.0], "Low": [0.5, 1.0], "Close": [1.5, 2.5], "Volume": [1.0, 2.0]},
                                index=pd.date_range("2026-11-12", periods=2, tz=NY))
        return raw_intraday()


@pytest.fixture
def provider(tmp_path):
    tickers = {}

    def factory(sym):
        tickers[sym] = FakeTicker(sym)
        return tickers[sym]

    p = YahooProvider(tmp_path, clock=lambda: NOW, ticker_factory=factory)
    p.tickers = tickers
    return p


def test_chain_dte_window_and_fields(provider, monkeypatch):
    monkeypatch.setattr(yahoo.time, "sleep", lambda s: None)
    ch = provider.chain("spy", min_dte=1, max_dte=10)
    assert isinstance(ch, OptionChain)
    assert ch.underlying == "SPY" and ch.spot == 750.0
    assert ch.expiries() == [date(2026, 11, 16), date(2026, 11, 20)]  # 11-13 is DTE 0, 12-18 is DTE 35
    assert provider.tickers["SPY"].calls["option_chain"] == ["2026-11-16", "2026-11-20"]  # only window fetched
    assert ch.asof == NOW and ch.asof.tzinfo is not None
    assert ch.risk_free == pytest.approx(0.0412)
    assert ch.div_yield == pytest.approx(0.0075)
    assert {q.right for q in ch.quotes} == {Right.CALL, Right.PUT}
    assert len(ch.quotes) == 2 * 2 * 2
    assert isinstance(provider, MarketDataProvider)


def test_chain_cached_and_retried(provider, monkeypatch):
    sleeps = []
    monkeypatch.setattr(yahoo.time, "sleep", sleeps.append)
    provider._ticker("SPY").fail_first = 2
    c1 = provider.chain("SPY", 0, 10)
    assert sleeps == [1.0, 2.0]
    n = len(provider.tickers["SPY"].calls["option_chain"])
    c2 = provider.chain("SPY", 0, 10)  # within 60 s TTL -> disk cache, no new calls
    assert len(provider.tickers["SPY"].calls["option_chain"]) == n
    assert len(c2.quotes) == len(c1.quotes)


def test_chain_empty_window_raises(provider):
    with pytest.raises(yahoo.DataUnavailable):
        provider.chain("SPY", 200, 300)


def test_bars_and_rates(provider):
    b = provider.intraday_bars("SPY", "1m", days=30)
    assert provider.tickers["SPY"].last_history_kwargs["period"] == "7d"  # 1m clamped to Yahoo limit
    assert provider.tickers["SPY"].last_history_kwargs["prepost"] is False
    assert b.index[0].strftime("%H:%M") == "09:30" and str(b.index.tz) == "America/New_York"
    d = provider.daily_bars("SPY", years=1.0)
    assert list(d.columns) == ["open", "high", "low", "close", "volume"] and len(d) == 2
    assert provider.risk_free_rate() == pytest.approx(0.0412)


def test_fallbacks(tmp_path):
    class Broken(FakeTicker):
        def __init__(self, sym):
            super().__init__(sym)

        @property
        def info(self):
            raise RuntimeError("nope")

        @info.setter
        def info(self, v):
            pass

        def history(self, **kw):
            raise RuntimeError("down")

    p = YahooProvider(tmp_path, clock=lambda: NOW, ticker_factory=Broken)
    assert p.risk_free_rate() == 0.04
    assert p.dividend_yield("SPY") == 0.0


def test_dividend_yield_field_handling():
    f = YahooProvider._yield_from_info
    assert f({"trailingAnnualDividendYield": 0.0075}) == 0.0075
    assert f({"dividendYield": 1.5}) == pytest.approx(0.015)  # percent field
    assert f({"dividendYield": float("nan")}) == 0.0
    assert f({}) == 0.0


def test_earnings_date(provider):
    assert provider.earnings_date("AAPL") == date(2027, 1, 28)  # past date skipped
    provider._ticker("ETF").calendar = {}
    provider._ticker("ETF").get_earnings_dates = lambda limit: pd.DataFrame(index=[])
    assert provider.earnings_date("ETF") is None


def test_spot_falls_back_to_daily_close(provider):
    provider._ticker("QQQ").fast_info = {"last_price": float("nan")}
    assert provider.spot("QQQ") == 2.5

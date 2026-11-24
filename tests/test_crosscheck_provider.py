"""Offline tests for CrossCheckedProvider with hand-built chains."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from kwanttrades.data.crosscheck import CrossCheckedProvider, crude_delta, tolerance
from kwanttrades.types import NY, MarketDataProvider, OptionChain, OptionQuote, Right

EXP = date(2026, 11, 20)
ASOF_A = datetime(2026, 11, 13, 16, 0, tzinfo=NY)
ASOF_B = datetime(2026, 11, 13, 15, 0, tzinfo=NY)


def Q(strike, bid, ask, right=Right.CALL, vol=0, oi=0, iv=None, expiry=EXP):
    return OptionQuote("SPY", expiry, strike, right, bid, ask, 0.0, vol, oi, iv)


def C(quotes, spot=100.0, asof=ASOF_A, rf=0.04, dy=0.0):
    return OptionChain("SPY", spot, asof, list(quotes), rf, dy)


class Prov:
    def __init__(self, chain=None, exc=None):
        self._chain, self._exc, self.calls = chain, exc, []

    def spot(self, s):
        self.calls.append("spot")
        return 1.0

    def chain(self, s, min_dte=0, max_dte=45):
        self.calls.append(("chain", s, min_dte, max_dte))
        if self._exc:
            raise self._exc
        return self._chain

    def intraday_bars(self, s, interval="1m", days=1):
        self.calls.append("intra")
        return "i"

    def daily_bars(self, s, years=2.0):
        self.calls.append("daily")
        return "d"

    def risk_free_rate(self):
        self.calls.append("rf")
        return 0.05


def run(pq, sq, **kw):
    p, s = Prov(pq), Prov(sq)
    x = CrossCheckedProvider(p, s, **kw)
    return x, x.chain("SPY", 0, 10)


def test_protocol():
    assert isinstance(CrossCheckedProvider(Prov(), Prov()), MarketDataProvider)


def test_crude_delta_buckets():
    assert crude_delta(Q(80, 1, 2), 100) == 1.0                 # ITM call
    assert crude_delta(Q(120, 1, 2, Right.PUT), 100) == 1.0     # ITM put
    assert crude_delta(Q(101, 1, 2), 100) == 0.5                # within 2%
    assert crude_delta(Q(99, 1, 2, Right.PUT), 100) == 0.5
    assert crude_delta(Q(120, 1, 2), 100) == 0.25               # OTM call
    assert crude_delta(Q(80, 1, 2, Right.PUT), 100) == 0.25


def test_tolerance_arithmetic():
    a, b = Q(120, 1.9, 2.1), Q(120, 2.4, 2.6)  # mids 2.0 / 2.5, OTM call -> delta 0.25
    # max(0.05, 0.1*2.25)=0.225 ; + 0.25*|100-102| = 0.5
    assert tolerance(a, b, 100.0, 102.0, 0.10, 0.05) == pytest.approx(0.725)
    # abs_tol floor when relative part is tiny
    c, d = Q(120, 0.09, 0.11), Q(120, 0.09, 0.11)
    assert tolerance(c, d, 100.0, 100.0, 0.10, 0.05) == pytest.approx(0.05)


def test_confirm_by_mid_tolerance():
    pq = C([Q(100, 4.90, 5.00)])   # mid 4.95
    sq = C([Q(100, 5.10, 5.20)])   # mid 5.15 -> diff 0.20 <= 0.1*5.05
    _, out = run(pq, sq)
    assert len(out.quotes) == 1


def test_confirm_by_interval_overlap_even_when_mid_gap_large():
    pq = C([Q(100, 1.0, 9.0)])   # mid 5
    sq = C([Q(100, 8.0, 10.0)])  # mid 9; diff 4 > tol, but intervals overlap in [8, 9]
    x, out = run(pq, sq)
    assert len(out.quotes) == 1 and x.last_report["SPY"]["n_confirmed"] == 1


def test_drop_on_disagreement():
    pq = C([Q(100, 4.9, 5.0), Q(105, 2.0, 2.1)])
    sq = C([Q(100, 4.9, 5.0), Q(105, 3.0, 3.1)])  # 105 call: mids 2.05 vs 3.05
    x, out = run(pq, sq)
    assert [q.strike for q in out.quotes] == [100]
    rep = x.last_report["SPY"]
    assert rep["n_dropped"] == 1
    assert rep["dropped"] == [{"occ": "SPY261120C00105000", "mid_primary": pytest.approx(2.05),
                               "mid_secondary": pytest.approx(3.05)}]


def test_spot_difference_term_rescues_borderline_quote():
    # deep ITM call strike 80: mids differ by 1.5 because spots differ by 2 (delta 1.0)
    pq = C([Q(80, 21.9, 22.1)], spot=100.0)             # mid 22.0
    sq = C([Q(80, 20.4, 20.6)], spot=98.0)              # mid 20.5; diff 1.5
    _, out = run(pq, sq, rel_tol=0.01)                  # 0.2125 + 1.0*2 = 2.2125 >= 1.5
    assert len(out.quotes) == 1
    pq2, sq2 = C([Q(80, 21.9, 22.1)], spot=100.0), C([Q(80, 20.4, 20.6)], spot=100.0)
    x, out2 = run(pq2, sq2, rel_tol=0.01)               # no spot gap: tol 0.2125 < 1.5 -> drop
    assert out2.quotes == [] and x.last_report["SPY"]["n_dropped"] == 1


def test_tighter_spread_and_max_volume_oi():
    pq = C([Q(100, 4.8, 5.2, vol=10, oi=500, iv=0.2)])   # spread 0.4
    sq = C([Q(100, 4.9, 5.1, vol=70, oi=300, iv=0.25)])  # spread 0.2
    _, out = run(pq, sq)
    q = out.quotes[0]
    assert (q.bid, q.ask) == (4.9, 5.1)
    assert q.volume == 70 and q.open_interest == 500
    assert q.vendor_iv == 0.25
    _, out = run(C([Q(100, 4.9, 5.1, vol=1)]), C([Q(100, 4.8, 5.2, vol=2)]))
    assert (out.quotes[0].bid, out.quotes[0].ask, out.quotes[0].volume) == (4.9, 5.1, 2)
    _, out = run(C([Q(100, 4.9, 5.1, iv=0.2)]), C([Q(100, 4.9, 5.1, iv=0.3)]))
    assert out.quotes[0].vendor_iv == 0.2  # tie -> primary


def test_one_sided_matched_contract_is_dropped_not_confirmed():
    pq = C([Q(100, 0.0, 5.0)])
    sq = C([Q(100, 4.9, 5.1)])
    x, out = run(pq, sq)
    assert out.quotes == []
    rep = x.last_report["SPY"]
    assert (rep["n_matched"], rep["n_confirmed"], rep["n_dropped"]) == (1, 0, 1)


def test_unmatched_default_dropped_and_keep_flag():
    pq = C([Q(100, 4.9, 5.0), Q(110, 1.0, 1.1), Q(100, 4.0, 4.1, Right.PUT)])
    sq = C([Q(100, 4.9, 5.0), Q(120, 0.5, 0.6), Q(100, 4.0, 4.1, Right.PUT, expiry=date(2026, 11, 27))])
    x, out = run(pq, sq)
    assert [(q.strike, q.right) for q in out.quotes] == [(100.0, Right.CALL)]
    assert x.last_report["SPY"]["n_unmatched"] == 4
    x2, out2 = run(pq, sq, keep_unmatched=True)
    assert len(out2.quotes) == 5
    assert out2.quotes == sorted(out2.quotes, key=lambda q: (q.expiry, q.strike, q.right.value))


def test_strike_float_noise_still_matches():
    _, out = run(C([Q(100.0004, 4.9, 5.0)]), C([Q(100.0, 4.9, 5.0)]))
    assert len(out.quotes) == 1


def test_output_uses_primary_metadata():
    pq = C([Q(100, 4.9, 5.0)], spot=100.0, asof=ASOF_A, rf=0.041, dy=0.013)
    sq = C([Q(100, 4.9, 5.0)], spot=100.5, asof=ASOF_B, rf=0.09, dy=0.0)
    _, out = run(pq, sq)
    assert (out.spot, out.asof, out.risk_free, out.div_yield) == (100.0, ASOF_A, 0.041, 0.013)
    assert out.underlying == "SPY"


@pytest.mark.parametrize("secondary", [Prov(exc=RuntimeError("boom")), Prov(chain=C([]))])
def test_secondary_failure_or_empty_returns_primary_unchanged(secondary):
    pchain = C([Q(100, 4.9, 5.0), Q(110, 1.0, 1.1)])
    x = CrossCheckedProvider(Prov(pchain), secondary)
    out = x.chain("SPY")
    assert out is pchain
    rep = x.last_report["SPY"]
    assert rep["secondary_ok"] is False and rep["n_primary"] == 2 and rep["n_secondary"] == 0
    assert rep["error"]


@pytest.mark.parametrize("primary", [Prov(exc=RuntimeError("403 Forbidden")), Prov(C([])), Prov(None)])
def test_primary_failure_falls_back_to_secondary_alone(primary):
    sq = C([Q(100, 4.9, 5.0), Q(101, 4.0, 4.1)], spot=100.2)
    x = CrossCheckedProvider(primary, Prov(sq))
    out = x.chain("SPY", 0, 10)
    assert out is sq
    rep = x.last_report["SPY"]
    assert rep["source"] == "secondary_only" and rep["n_secondary"] == 2 and rep["n_primary"] == 0
    assert rep["error"].startswith("primary fetch failed")


def test_both_feeds_failing_raises_the_secondary_error():
    with pytest.raises(ValueError, match="yahoo down"):
        CrossCheckedProvider(Prov(exc=RuntimeError("403")), Prov(exc=ValueError("yahoo down"))).chain("SPY")


def test_report_contents_and_median():
    pq = C([Q(100, 4.9, 5.0), Q(101, 4.0, 4.1), Q(102, 3.0, 3.1), Q(103, 1.0, 1.1)], spot=100.0)
    sq = C([Q(100, 4.9, 5.0), Q(101, 4.0, 4.1), Q(102, 3.0, 3.1), Q(104, 1.0, 1.1)], spot=100.3)
    x, out = run(pq, sq)
    rep = x.last_report["SPY"]
    assert rep["n_primary"] == 4 and rep["n_secondary"] == 4
    assert rep["n_matched"] == 3 and rep["n_confirmed"] == 3 and rep["n_dropped"] == 0
    assert rep["n_unmatched"] == 2
    assert rep["median_abs_mid_diff"] == pytest.approx(0.0)
    assert rep["spot_primary"] == 100.0 and rep["spot_secondary"] == 100.3
    assert rep["secondary_ok"] is True and rep["dropped"] == []


def test_dropped_list_capped_at_20():
    n = 30
    pq = C([Q(50 + i, 1.0, 1.1) for i in range(n)])
    sq = C([Q(50 + i, 5.0, 5.1) for i in range(n)])
    x, out = run(pq, sq)
    rep = x.last_report["SPY"]
    assert rep["n_dropped"] == n and len(rep["dropped"]) == 20
    assert out.quotes == []


def test_args_forwarded_and_delegation_to_primary():
    p, s = Prov(C([Q(100, 4.9, 5.0)])), Prov(C([Q(100, 4.9, 5.0)]))
    x = CrossCheckedProvider(p, s)
    x.chain("SPY", 2, 9)
    assert ("chain", "SPY", 2, 9) in p.calls and ("chain", "SPY", 2, 9) in s.calls
    assert x.spot("SPY") == 1.0 and x.intraday_bars("SPY") == "i"
    assert x.daily_bars("SPY") == "d" and x.risk_free_rate() == 0.05
    assert s.calls == [("chain", "SPY", 2, 9)]

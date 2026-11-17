"""Regression tests on real (weekend snapshot) SPY / AAPL chains: the scanner must be quiet
on efficient markets and must never report stale/sub-intrinsic quotes as edge."""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pytest

from kwanttrades.config import ScanConfig
from kwanttrades.quant import bs
from kwanttrades.quant.mispricing import scan_chain
from kwanttrades.quant.surface import build_surface
from kwanttrades.types import OptionChain, OptionQuote, Right

FIX = Path(__file__).parent / "fixtures"


def load_chain(name: str) -> OptionChain:
    raw = json.loads((FIX / name).read_text(encoding="utf-8"))
    cols = raw["columns"]
    quotes = []
    for row in raw["quotes"]:
        d = dict(zip(cols, row))
        viv = d.get("vendor_iv")
        quotes.append(
            OptionQuote(
                underlying=raw["underlying"],
                expiry=date.fromisoformat(str(d["expiry"])[:10]),
                strike=float(d["strike"]),
                right=Right(d["right"]),
                bid=float(d["bid"] or 0.0),
                ask=float(d["ask"] or 0.0),
                last=float(d.get("last") or 0.0),
                volume=int(d.get("volume") or 0),
                open_interest=int(d.get("open_interest") or 0),
                vendor_iv=None if viv is None else float(viv),
            )
        )
    return OptionChain(
        raw["underlying"], float(raw["spot"]), datetime.fromisoformat(raw["asof"]), quotes, float(raw["risk_free"]), float(raw["div_yield"])
    )


CHAINS = ["spy_chain_real.json", "aapl_chain_real.json"]


@pytest.fixture(scope="module", params=CHAINS)
def real(request):
    if not (FIX / request.param).exists():
        pytest.skip("fixture missing")
    ch = load_chain(request.param)
    s = build_surface(ch)
    return ch, s, scan_chain(ch, s, ScanConfig())


def test_real_chain_scanner_is_quiet(real):
    ch, s, flags = real
    assert len(s.slices) >= 5
    counts = Counter(f.model for f in flags)
    assert len(flags) <= 25, counts


def test_no_flags_on_sub_intrinsic_quotes(real):
    ch, s, flags = real
    for f in flags:
        q = f.quote
        F = s.forward(q.expiry)
        D = np.exp(-ch.risk_free * ch.T(q.expiry))
        intrinsic = max(bs.cp_of(q.right) * D * (F - q.strike), 0.0)
        assert q.mid >= intrinsic - q.spread, (f.model, q.occ_symbol, q.bid, q.ask, intrinsic)


def test_svi_flags_are_outside_bid_ask(real):
    _, _, flags = real
    for f in flags:
        if f.model == "svi_residual":
            assert f.fair_value > f.quote.ask or f.fair_value < f.quote.bid
            assert (f.edge > 0) == (f.fair_value > f.quote.ask)
            assert np.sign(f.zscore) == np.sign(f.edge)


def test_real_chain_injected_anomaly_is_flagged():
    """Sensitivity on real data: push one liquid OTM SPY put well outside its neighbours."""
    import dataclasses

    if not (FIX / "spy_chain_real.json").exists():
        pytest.skip("fixture missing")
    ch = load_chain("spy_chain_real.json")
    s = build_surface(ch)
    e = sorted(s.slices, key=lambda d: s.slices[d].T)[4]
    sl = s.slices[e]
    cfg = ScanConfig()
    cand = [
        q
        for q in ch.for_expiry(e)
        if q.right == Right.PUT and q.strike < sl.forward * 0.98 and q.has_two_sided_market and q.bid >= 2.0 and q.spread_pct < 0.03 and q.open_interest >= 100
    ]
    target = max(cand, key=lambda q: q.strike)
    vega = bs.greeks(sl.forward, target.strike, sl.T, sl.rate, sl.rate, sl.iv(target.strike), -1)["vega"]
    dx = -max(0.03 * vega, 4 * target.spread)  # 3 vol points cheaper (at least 4 spreads)
    quotes = [dataclasses.replace(q, bid=q.bid + dx, ask=q.ask + dx) if q is target else q for q in ch.quotes]
    pert = OptionChain(ch.underlying, ch.spot, ch.asof, quotes, ch.risk_free, ch.div_yield)
    flags = [f for f in scan_chain(pert, build_surface(pert), cfg) if f.model == "svi_residual"]
    hit = [f for f in flags if f.quote.occ_symbol == target.occ_symbol]
    assert hit and hit[0].edge > 0 and hit[0].fair_value > hit[0].quote.ask

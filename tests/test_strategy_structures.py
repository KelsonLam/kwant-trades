"""Structure builders on clean synthetic chains: legs, sides, ordering, defined risk, prices."""
from __future__ import annotations

import math

import numpy as np
import pytest

from kwanttrades.config import ScanConfig
from kwanttrades.execution.risk import uncovered_short_legs
from kwanttrades.quant.surface import build_surface
from kwanttrades.strategy import structures as st
from kwanttrades.types import Mispricing, OptionChain, Right, Side, TradeIdea
from tests.test_quant_synth import expiry_for, make_chain

STRIKES = np.arange(340.0, 460.01, 1.0)


@pytest.fixture(scope="module")
def chain() -> OptionChain:
    return make_chain(days=(7, 30), spot=400.0, strikes=STRIKES)


@pytest.fixture(scope="module")
def surface(chain):
    return build_surface(chain, ScanConfig())


@pytest.fixture(scope="module")
def ctx(chain, surface):
    c = st.expiry_context(chain, surface, expiry_for(30), ScanConfig())
    assert c is not None and c.usable
    return c


def _idea(c: st.Candidate) -> TradeIdea:
    return TradeIdea("X", c.strategy, c.legs, c.net_mid, c.net_mid, c.max_loss, c.max_profit, 0.5, 0.0, 0.0)


def _common_checks(c: st.Candidate, ctx: st.ExpiryContext):
    assert uncovered_short_legs(_idea(c)) == []
    cfg = ScanConfig()
    mid = sum((1 if l.side == Side.BUY else -1) * l.quote.mid for l in c.legs)
    assert c.net_mid == pytest.approx(mid)
    assert c.half_spread == pytest.approx(sum(0.5 * l.quote.spread for l in c.legs))
    for l in c.legs:
        assert st.liquid(l.quote, cfg)
        assert ctx.k_lo <= l.quote.strike <= ctx.k_hi
        assert l.quote.expiry == ctx.expiry and l.ratio == 1


def _delta(ctx, q):
    rows = ctx.rows(q.right)
    return next(r.delta for r in rows if r.quote is q)


def test_context_rows_are_eligible_and_sorted(ctx):
    for rows, sign in ((ctx.calls, 1), (ctx.puts, -1)):
        ks = [r.strike for r in rows]
        assert ks == sorted(ks)
        assert all(sign * r.delta > 0 for r in rows)
    # calls: delta decreasing in strike
    d = [r.delta for r in ctx.calls]
    assert all(a >= b for a, b in zip(d, d[1:]))


def test_long_call_and_put(ctx):
    for right, name in ((Right.CALL, "long_call"), (Right.PUT, "long_put")):
        (c,) = st.long_option(ctx, right)
        assert c.strategy == name and len(c.legs) == 1
        leg = c.legs[0]
        assert leg.side == Side.BUY and leg.quote.right == right
        assert 0.40 <= abs(_delta(ctx, leg.quote)) <= 0.60
        assert c.net_mid > 0
        assert c.max_loss == pytest.approx(leg.quote.mid * 100)
        assert math.isinf(c.max_profit) if right == Right.CALL else c.max_profit == pytest.approx((leg.quote.strike - leg.quote.mid) * 100)
        assert c.width == 0
        _common_checks(c, ctx)


def test_bull_call_debit_spreads(ctx):
    cs = st.debit_verticals(ctx, Right.CALL)
    assert len(cs) >= 2  # several widths
    longs = {c.legs[0].quote.strike for c in cs}
    assert len(longs) == 1
    shorts = [c.legs[1].quote.strike for c in cs]
    assert len(set(shorts)) == len(shorts)
    for c in cs:
        lo, sh = c.legs
        assert c.strategy == "bull_call_spread"
        assert lo.side == Side.BUY and sh.side == Side.SELL
        assert lo.quote.right == sh.quote.right == Right.CALL
        assert lo.quote.strike < sh.quote.strike
        assert 0.45 <= _delta(ctx, lo.quote) <= 0.65
        W = sh.quote.strike - lo.quote.strike
        net = lo.quote.mid - sh.quote.mid
        assert c.net_mid == pytest.approx(net) and net > 0
        assert c.width == pytest.approx(W)
        assert c.max_loss == pytest.approx(net * 100)
        assert c.max_profit == pytest.approx((W - net) * 100)
        _common_checks(c, ctx)


def test_bear_put_debit_spreads(ctx):
    cs = st.debit_verticals(ctx, Right.PUT)
    assert cs
    for c in cs:
        lo, sh = c.legs
        assert c.strategy == "bear_put_spread"
        assert lo.side == Side.BUY and sh.side == Side.SELL and lo.quote.right == Right.PUT
        assert lo.quote.strike > sh.quote.strike
        W = lo.quote.strike - sh.quote.strike
        net = lo.quote.mid - sh.quote.mid
        assert net > 0
        assert c.max_loss == pytest.approx(net * 100)
        assert c.max_profit == pytest.approx((W - net) * 100)
        _common_checks(c, ctx)


def test_credit_verticals(ctx):
    for right, name in ((Right.PUT, "bull_put_spread"), (Right.CALL, "bear_call_spread")):
        cs = st.credit_verticals(ctx, right)
        assert len(cs) >= 3
        for c in cs:
            sh, lo = c.legs
            assert c.strategy == name and c.is_credit_type
            assert sh.side == Side.SELL and lo.side == Side.BUY
            assert sh.quote.right == lo.quote.right == right
            assert 0.20 <= abs(_delta(ctx, sh.quote)) <= 0.45
            if right == Right.PUT:
                assert lo.quote.strike < sh.quote.strike
            else:
                assert lo.quote.strike > sh.quote.strike
            W = abs(sh.quote.strike - lo.quote.strike)
            credit = sh.quote.mid - lo.quote.mid
            assert c.net_mid == pytest.approx(-credit) and credit > 0
            assert c.width == pytest.approx(W)
            assert c.max_loss == pytest.approx((W - credit) * 100)
            assert c.max_profit == pytest.approx(credit * 100)
            _common_checks(c, ctx)


def test_iron_condor(ctx):
    cs = st.iron_condor(ctx)
    assert cs
    for c in cs:
        lp, sp, sc, lc = c.legs
        assert [l.side for l in c.legs] == [Side.BUY, Side.SELL, Side.SELL, Side.BUY]
        assert [l.quote.right for l in c.legs] == [Right.PUT, Right.PUT, Right.CALL, Right.CALL]
        assert lp.quote.strike < sp.quote.strike < sc.quote.strike < lc.quote.strike
        for q in (sp.quote, sc.quote):
            assert 0.11 <= abs(_delta(ctx, q)) <= 0.25
        wp, wc = sp.quote.strike - lp.quote.strike, lc.quote.strike - sc.quote.strike
        credit = sp.quote.mid + sc.quote.mid - lp.quote.mid - lc.quote.mid
        assert c.net_mid == pytest.approx(-credit) and credit > 0
        assert c.width == pytest.approx(max(wp, wc))
        assert c.max_loss == pytest.approx((max(wp, wc) - credit) * 100)
        assert c.max_profit == pytest.approx(credit * 100)
        _common_checks(c, ctx)


def test_straddle_and_strangle(ctx):
    (s,) = st.long_straddle(ctx)
    assert s.strategy == "long_straddle"
    c, p = s.legs
    assert c.side == p.side == Side.BUY and c.quote.strike == p.quote.strike
    assert {c.quote.right, p.quote.right} == {Right.CALL, Right.PUT}
    assert abs(c.quote.strike - ctx.forward) <= 1.0
    assert s.max_loss == pytest.approx((c.quote.mid + p.quote.mid) * 100) and math.isinf(s.max_profit)
    _common_checks(s, ctx)
    (g,) = st.long_strangle(ctx)
    p, c = g.legs
    assert g.strategy == "long_strangle" and p.quote.right == Right.PUT and c.quote.right == Right.CALL
    assert p.quote.strike < ctx.forward <= c.quote.strike
    assert 0.20 <= abs(_delta(ctx, p.quote)) <= 0.40 and 0.20 <= _delta(ctx, c.quote) <= 0.40
    assert g.max_loss == pytest.approx((p.quote.mid + c.quote.mid) * 100)
    _common_checks(g, ctx)


def test_irregular_strikes_and_missing_strikes():
    ks = [300, 320, 340, 350, 360, 370, 375, 380, 385, 390, 392.5, 395, 397.5, 400, 402.5, 407.5, 410, 412.5, 415, 420, 425, 430, 440, 450, 475, 500]
    ch = make_chain(days=(9, 30), spot=400.0, strikes=np.array(ks, dtype=float))
    surf = build_surface(ch, ScanConfig())
    for d in (9, 30):
        ctx = st.expiry_context(ch, surf, expiry_for(d), ScanConfig())
        builders = [
            st.long_option(ctx, Right.CALL), st.long_option(ctx, Right.PUT),
            st.debit_verticals(ctx, Right.CALL), st.debit_verticals(ctx, Right.PUT),
            st.credit_verticals(ctx, Right.PUT), st.credit_verticals(ctx, Right.CALL),
            st.iron_condor(ctx), st.long_straddle(ctx), st.long_strangle(ctx),
        ]
        assert sum(len(b) for b in builders) >= 6
        for b in builders:
            for c in b:
                assert all(l.quote.strike in ks for l in c.legs)
                assert 405.0 not in [l.quote.strike for l in c.legs]
                _common_checks(c, ctx)


def test_no_extrapolation_outside_fitted_range():
    """Surface fitted on 380-420 only: wider chain strikes are never eligible."""
    wide = make_chain(days=(30,), spot=400.0, strikes=STRIKES)
    narrow = OptionChain(wide.underlying, wide.spot, wide.asof, [q for q in wide.quotes if 380 <= q.strike <= 420], wide.risk_free, wide.div_yield)
    surf = build_surface(narrow, ScanConfig())
    ctx = st.expiry_context(wide, surf, expiry_for(30), ScanConfig())
    assert ctx.k_lo >= 380 and ctx.k_hi <= 420
    assert all(380 <= r.strike <= 420 for r in ctx.calls + ctx.puts)
    for c in st.iron_condor(ctx) + st.credit_verticals(ctx, Right.PUT):
        assert all(380 <= l.quote.strike <= 420 for l in c.legs)


def test_liquidity_filter_and_volume_waiver(chain, surface):
    base = st.expiry_context(chain, surface, expiry_for(30), ScanConfig())
    tight = ScanConfig(max_spread_pct=0.001)
    assert st.expiry_context(chain, surface, expiry_for(30), tight).calls == []
    assert st.expiry_context(chain, surface, expiry_for(30), ScanConfig(min_open_interest=10_000)).calls == []
    # volume filter applies when some volume exists, waived when all volume is zero
    vol_cfg = ScanConfig(min_volume=100)  # synthetic quotes have volume 50
    assert st.expiry_context(chain, surface, expiry_for(30), vol_cfg).calls == []
    from dataclasses import replace
    zero = OptionChain(chain.underlying, chain.spot, chain.asof, [replace(q, volume=0) for q in chain.quotes], chain.risk_free, chain.div_yield)
    assert st.volume_all_zero(zero)
    waived = st.expiry_context(zero, surface, expiry_for(30), vol_cfg)
    assert len(waived.calls) == len(base.calls) > 0


def _flag(q, edge, z):
    return Mispricing(q, "svi_residual", q.mid + edge, q.mid, edge, edge, z)


def test_prefers_buying_flagged_cheap_strike(chain, surface):
    ctx0 = st.expiry_context(chain, surface, expiry_for(30), ScanConfig())
    (base,) = st.long_option(ctx0, Right.CALL)
    k0 = base.legs[0].quote.strike
    nb = chain.find(expiry_for(30), k0 + 1, Right.CALL)
    ctx = st.expiry_context(chain, surface, expiry_for(30), ScanConfig(), [_flag(nb, +0.20, 3.0)])
    (c,) = st.long_option(ctx, Right.CALL)
    assert c.legs[0].quote.strike == k0 + 1
    assert c.flags and "cheap" in c.flags[0]
    # a RICH flag on the neighbour does not attract the buyer
    ctx_r = st.expiry_context(chain, surface, expiry_for(30), ScanConfig(), [_flag(nb, -0.20, -3.0)])
    (c2,) = st.long_option(ctx_r, Right.CALL)
    assert c2.legs[0].quote.strike == k0


def test_prefers_selling_flagged_rich_strike(chain, surface):
    ctx0 = st.expiry_context(chain, surface, expiry_for(30), ScanConfig())
    base = st.credit_verticals(ctx0, Right.PUT, short_targets=(0.30,))
    ks = base[0].legs[0].quote.strike
    nb = chain.find(expiry_for(30), ks - 1, Right.PUT)
    ctx = st.expiry_context(chain, surface, expiry_for(30), ScanConfig(), [_flag(nb, -0.15, -3.0)])
    cs = st.credit_verticals(ctx, Right.PUT, short_targets=(0.30,))
    assert cs[0].legs[0].quote.strike == ks - 1 and cs[0].legs[0].side == Side.SELL
    assert any("rich" in f for f in cs[0].flags)

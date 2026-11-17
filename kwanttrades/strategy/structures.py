"""Pure builders of candidate option structures from a chain + fitted vol surface.

Nothing here prices a structure under OUR view or decides whether to trade it; that is the
engine's job. A builder only picks legs and reports the structure's mid net price, half
spread and payoff extremes.

Judgement calls (documented assumptions)
----------------------------------------
* **Eligible strikes.** A quote can be a leg only if it passes the ScanConfig liquidity filter
  (two-sided market, bid >= min_bid, spread/mid <= max_spread_pct, open interest >=
  min_open_interest, volume >= min_volume; the volume test is waived when the whole chain
  reports zero volume, i.e. the market is closed) AND its strike lies inside the range of
  strikes the slice's smile was actually fitted on (``table.in_fit``). Outside that range the
  surface is extrapolated and neither its deltas nor its "residuals" mean anything, so such
  strikes are never used (no-extrapolation rule).
* **Delta.** Strike selection uses ``bs.greeks`` at the surface IV of that strike with
  S = spot and the implied carry ``q = r - ln(F/S)/T`` taken from the surface forward F, so
  the delta is consistent with the forward the smile was fitted on (dividends/borrow are in F).
* **Robust strike choice on real chains.** Strikes are selected by delta (never by "n dollars
  away"), working only on the sorted list of ELIGIBLE strikes, so irregular spacing, missing
  strikes, $0.50/$1/$2.50/$5 increments and weekly vs monthly expiries are all handled the same
  way. A target band that contains no eligible strike is widened once by ``BAND_TOLERANCE``;
  if still empty the builder returns nothing. Wings "1-3 strikes beyond" count ELIGIBLE strikes,
  so a missing/illiquid strike is skipped rather than producing an illiquid leg.
* **Cheap / rich preference.** Each eligible quote gets a preference score ``pref``: the SVI
  residual in half-spread units, ``(fit_iv - market_iv) / ((ask_iv - bid_iv)/2)`` clipped to
  +-3 (positive = trades below the smile = cheap), plus +-``FLAG_BONUS`` when the quote is
  flagged by ``mispricing.scan_chain`` (sign of the flag's edge). Around the delta-nearest
  strike, an ADJACENT eligible strike still inside the band replaces it when it is better by
  more than ``PREF_MARGIN`` (buyers want high pref, sellers low pref).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from kwanttrades.config import ScanConfig
from kwanttrades.quant import bs
from kwanttrades.quant.distribution import TerminalDist, evaluate_structure
from kwanttrades.quant.surface import SmileSlice, VolSurface
from kwanttrades.types import MULTIPLIER, Leg, Mispricing, OptionChain, OptionQuote, Right, Side

BAND_TOLERANCE = 0.05  # widen an empty delta band once by this much on each side
FLAG_BONUS = 3.0  # preference added for a scan_chain flag (sign = edge sign)
PREF_MARGIN = 0.5  # adjacent strike must beat the delta-nearest by this much to replace it
RESID_CLIP = 3.0

BULLISH = {"long_call", "bull_call_spread", "bull_put_spread"}
BEARISH = {"long_put", "bear_put_spread", "bear_call_spread"}
CREDIT = {"bull_put_spread", "bear_call_spread", "iron_condor"}
VOL = {"iron_condor", "long_straddle", "long_strangle"}


# ----------------------------------------------------------------------------- data classes


@dataclass
class Row:
    """One eligible quote with its model attributes."""

    quote: OptionQuote
    delta: float  # signed (calls > 0, puts < 0)
    iv: float
    pref: float  # > 0 cheap, < 0 rich
    flag: Mispricing | None = None

    @property
    def strike(self) -> float:
        return self.quote.strike

    @property
    def adelta(self) -> float:
        return abs(self.delta)


@dataclass
class ExpiryContext:
    """Everything a builder needs for one expiry."""

    expiry: date
    spot: float
    forward: float
    T: float
    r: float
    q: float  # implied carry yield so that F = S e^{(r-q)T}
    slice: SmileSlice
    k_lo: float  # fitted strike range (no-extrapolation bounds)
    k_hi: float
    calls: list[Row] = field(default_factory=list)  # sorted by strike
    puts: list[Row] = field(default_factory=list)

    def rows(self, right: Right) -> list[Row]:
        return self.calls if right == Right.CALL else self.puts

    @property
    def usable(self) -> bool:
        return len(self.calls) >= 3 and len(self.puts) >= 3


@dataclass
class Candidate:
    """A candidate structure (before our-view pricing).

    ``net_mid`` per share (> 0 debit, < 0 credit); ``half_spread`` per share = sum of leg half
    spreads x ratio; ``max_loss`` / ``max_profit`` in $ per contract-set AT MID, no costs
    (max_profit may be inf); ``width`` = largest strike distance between a short leg and the
    long leg covering it (0 for structures without short legs)."""

    strategy: str
    legs: list[Leg]
    expiry: date
    net_mid: float
    half_spread: float
    max_loss: float
    max_profit: float
    width: float
    flags: list[str] = field(default_factory=list)

    @property
    def direction(self) -> int:
        return 1 if self.strategy in BULLISH else (-1 if self.strategy in BEARISH else 0)

    @property
    def is_credit_type(self) -> bool:
        return self.strategy in CREDIT

    @property
    def key(self) -> tuple:
        return (self.strategy, self.expiry, tuple(sorted((l.quote.occ_symbol, l.side.value) for l in self.legs)))


# ----------------------------------------------------------------------------- helpers


def liquid(q: OptionQuote, cfg: ScanConfig, waive_volume: bool = False) -> bool:
    """ScanConfig liquidity filter; the volume test is skipped when ``waive_volume``."""
    return (
        q.has_two_sided_market
        and q.bid >= cfg.min_bid
        and q.spread_pct <= cfg.max_spread_pct
        and q.open_interest >= cfg.min_open_interest
        and (waive_volume or q.volume >= cfg.min_volume)
    )


def volume_all_zero(chain: OptionChain) -> bool:
    return bool(chain.quotes) and all((q.volume or 0) <= 0 for q in chain.quotes)


def implied_carry_q(spot: float, forward: float, T: float, r: float) -> float:
    if spot <= 0 or forward <= 0 or T <= 0:
        return 0.0
    return r - math.log(forward / spot) / T


def _residual_z(sl: SmileSlice) -> dict[str, float]:
    """occ -> clipped SVI residual (fit - mid) in half-spread vol units (> 0 cheap)."""
    tab = sl.table
    out: dict[str, float] = {}
    if tab is None or tab.empty:
        return out
    fit = tab["fit_iv"].to_numpy(dtype=float)
    miv = tab["market_iv"].to_numpy(dtype=float)
    hs = 0.5 * (tab["ask_iv"].to_numpy(dtype=float) - tab["bid_iv"].to_numpy(dtype=float))
    with np.errstate(invalid="ignore", divide="ignore"):
        z = np.where(np.isfinite(fit) & np.isfinite(miv) & np.isfinite(hs) & (hs > 0), (fit - miv) / hs, 0.0)
    z = np.clip(np.nan_to_num(z), -RESID_CLIP, RESID_CLIP)
    for q, zi in zip(tab["quote"], z):
        out[q.occ_symbol] = float(zi)
    return out


def fitted_range(sl: SmileSlice) -> tuple[float, float]:
    """(min, max) strike actually used in the smile fit; falls back to all quoted strikes."""
    tab = sl.table
    if tab is None or tab.empty:
        return (math.nan, math.nan)
    if "in_fit" in tab and bool(tab["in_fit"].any()):
        ks = tab.loc[tab["in_fit"].astype(bool), "strike"].to_numpy(dtype=float)
    else:
        ks = tab["strike"].to_numpy(dtype=float)
    return float(ks.min()), float(ks.max())


def expiry_context(
    chain: OptionChain,
    surface: VolSurface,
    expiry: date,
    cfg: ScanConfig,
    mispricings: list[Mispricing] | None = None,
    waive_volume: bool | None = None,
) -> ExpiryContext | None:
    """Build the eligible-strike table for one expiry, or None without a fitted slice."""
    sl = surface.slices.get(expiry)
    if sl is None:
        return None
    if waive_volume is None:
        waive_volume = volume_all_zero(chain)
    S, F, T, r = float(chain.spot), float(sl.forward), float(sl.T), float(sl.rate)
    q = implied_carry_q(S, F, T, r)
    lo, hi = fitted_range(sl)
    ctx = ExpiryContext(expiry, S, F, T, r, q, sl, lo, hi)
    if not (np.isfinite(lo) and np.isfinite(hi)):
        return ctx
    flags: dict[str, Mispricing] = {}
    for m in mispricings or []:
        occ = m.quote.occ_symbol
        if m.quote.expiry == expiry and (occ not in flags or abs(m.zscore) > abs(flags[occ].zscore)):
            flags[occ] = m
    resid = _residual_z(sl)
    quotes = [x for x in chain.for_expiry(expiry) if lo - 1e-9 <= x.strike <= hi + 1e-9 and liquid(x, cfg, waive_volume)]
    if not quotes:
        return ctx
    K = np.array([x.strike for x in quotes], dtype=float)
    cp = np.array([bs.cp_of(x.right) for x in quotes], dtype=float)
    iv = np.asarray(sl.iv(K), dtype=float).reshape(-1)
    delta = np.asarray(bs.greeks(S, K, T, r, q, iv, cp)["delta"], dtype=float).reshape(-1)
    for x, d, v in zip(quotes, delta, iv):
        if not (np.isfinite(d) and np.isfinite(v) and v > 0):
            continue
        fl = flags.get(x.occ_symbol)
        pref = resid.get(x.occ_symbol, 0.0)
        if fl is not None and fl.edge != 0:
            pref += math.copysign(FLAG_BONUS, fl.edge)
        row = Row(x, float(d), float(v), float(pref), fl)
        ctx.rows(x.right).append(row)
    ctx.calls.sort(key=lambda w: w.strike)
    ctx.puts.sort(key=lambda w: w.strike)
    return ctx


def pick(rows: list[Row], target: float, band: tuple[float, float], side: Side, exclude: set[float] | None = None) -> Row | None:
    """Pick a strike near |delta| = ``target`` inside ``band`` (widened once if empty).

    The delta-nearest eligible strike is the default; an adjacent eligible strike (by strike
    order, still inside the band) replaces it when its preference is better by more than
    PREF_MARGIN: higher pref (cheaper) when buying, lower (richer) when selling."""
    exclude = exclude or set()
    for widen in (0.0, BAND_TOLERANCE):
        lo, hi = band[0] - widen, band[1] + widen
        inb = [w for w in rows if lo - 1e-12 <= w.adelta <= hi + 1e-12 and w.strike not in exclude]
        if inb:
            break
    else:
        return None
    inb.sort(key=lambda w: w.strike)
    i0 = min(range(len(inb)), key=lambda i: (abs(inb[i].adelta - target), inb[i].strike))
    nearest = best = inb[i0]
    sgn = 1.0 if side == Side.BUY else -1.0
    for j in (i0 - 1, i0 + 1):
        if 0 <= j < len(inb):
            cand = inb[j]
            if sgn * (cand.pref - nearest.pref) > PREF_MARGIN and sgn * cand.pref > sgn * best.pref:
                best = cand
    return best


def _beyond(rows: list[Row], ref: Row, right: Right, n: int) -> Row | None:
    """The n-th eligible strike further out-of-the-money than ``ref`` (n >= 1)."""
    if right == Right.CALL:
        outs = [w for w in rows if w.strike > ref.strike + 1e-9]
    else:
        outs = sorted((w for w in rows if w.strike < ref.strike - 1e-9), key=lambda w: -w.strike)
    return outs[n - 1] if len(outs) >= n else None


def _flag_note(row: Row, side: Side) -> str | None:
    fl = row.flag
    if fl is None:
        return None
    kind = "cheap" if fl.edge > 0 else "rich"
    verb = "buying" if side == Side.BUY else "selling"
    good = (kind == "cheap") == (side == Side.BUY)
    return (
        f"{verb} {row.quote.right.value}{row.strike:g} {row.quote.expiry} flagged {kind} by {fl.model} "
        f"(z {fl.zscore:+.1f}, edge ${fl.edge:+.2f}/sh)" + ("" if good else " - against the flag")
    )


def make_candidate(strategy: str, legs: list[tuple[Row, Side]]) -> Candidate:
    """Assemble a Candidate and compute its mid metrics exactly from the payoff kinks."""
    lg = [Leg(r.quote, s, 1) for r, s in legs]
    net = sum((1.0 if s == Side.BUY else -1.0) * r.quote.mid for r, s in legs)
    hs = sum(0.5 * r.quote.spread for r, _ in legs)
    spot = legs[0][0].quote.strike
    ev = evaluate_structure(lg, net, TerminalDist(np.array([spot]), np.array([1.0])))
    width = 0.0
    for r, s in legs:
        if s == Side.SELL:
            covers = [o.strike for o, so in legs if so == Side.BUY and o.quote.right == r.quote.right]
            if covers:
                width = max(width, min(abs(r.strike - c) for c in covers))
    notes = [n for n in (_flag_note(r, s) for r, s in legs) if n]
    return Candidate(strategy, lg, legs[0][0].quote.expiry, float(net), float(hs), float(ev["max_loss"]), float(ev["max_profit"]), float(width), notes)


# ----------------------------------------------------------------------------- builders


def long_option(ctx: ExpiryContext, right: Right, target: float = 0.50, band: tuple[float, float] = (0.40, 0.60)) -> list[Candidate]:
    """Long call / long put at |delta| ~ 0.50 (band 0.40-0.60)."""
    row = pick(ctx.rows(right), target, band, Side.BUY)
    if row is None:
        return []
    return [make_candidate("long_call" if right == Right.CALL else "long_put", [(row, Side.BUY)])]


def debit_verticals(
    ctx: ExpiryContext,
    right: Right,
    long_target: float = 0.55,
    long_band: tuple[float, float] = (0.50, 0.60),
    short_targets: tuple[float, ...] = (0.40, 0.30, 0.20),
    require_rich_short: bool = False,
) -> list[Candidate]:
    """Bull call (calls) / bear put (puts) debit spreads.

    Long leg ~0.55 delta (0.50-0.60). Several widths: the short leg is chosen by delta targets
    (0.40 / 0.30 / 0.20), which produces narrow-to-wide spreads independent of the strike
    increment; reward:risk >= 1 typically needs the short leg at or below ~0.30 delta.
    ``require_rich_short`` keeps only spreads whose short strike is flagged rich (the
    "debit vertical financed by a rich strike" of the rich-vol matrix row)."""
    rows = ctx.rows(right)
    long_row = pick(rows, long_target, long_band, Side.BUY)
    if long_row is None:
        return []
    out, seen = [], set()
    for t in short_targets:
        outs = [w for w in rows if (w.strike > long_row.strike if right == Right.CALL else w.strike < long_row.strike)]
        short_row = pick(outs, t, (max(t - 0.05, 0.05), t + 0.05), Side.SELL)
        if short_row is None or short_row.strike in seen:
            continue
        if require_rich_short and not (short_row.flag is not None and short_row.flag.edge < 0):
            continue
        seen.add(short_row.strike)
        name = "bull_call_spread" if right == Right.CALL else "bear_put_spread"
        out.append(make_candidate(name, [(long_row, Side.BUY), (short_row, Side.SELL)]))
    return out


def credit_verticals(
    ctx: ExpiryContext,
    right: Right,
    short_targets: tuple[float, ...] = (0.30, 0.35),
    wings: tuple[int, ...] = (1, 2, 3),
) -> list[Candidate]:
    """Bull put (puts) / bear call (calls) credit spreads: short ~0.30 delta, long wing 1-3
    eligible strikes further out-of-the-money.

    Two short targets are tried, 0.30 (band 0.25-0.35) and 0.35 (band 0.30-0.40): with a 0.30
    delta short a one-strike-wide spread collects roughly 0.25-0.30 of its width at mid, which
    sits right on RiskConfig.min_credit_to_width once slippage is paid, so the slightly closer
    short gives the filter a realistic alternative."""
    rows = ctx.rows(right)
    out, seen = [], set()
    for t in short_targets:
        short_row = pick(rows, t, (t - 0.05, t + 0.05), Side.SELL)
        if short_row is None or short_row.strike in seen:
            continue
        seen.add(short_row.strike)
        for n in wings:
            wing = _beyond(rows, short_row, right, n)
            if wing is None:
                break
            name = "bear_call_spread" if right == Right.CALL else "bull_put_spread"
            out.append(make_candidate(name, [(short_row, Side.SELL), (wing, Side.BUY)]))
    return out


def iron_condor(
    ctx: ExpiryContext,
    short_target: float = 0.18,
    short_band: tuple[float, float] = (0.16, 0.20),
    wings: tuple[int, ...] = (1, 2, 3),
) -> list[Candidate]:
    """Short put + short call at ~0.16-0.20 delta, each covered by a long wing. The put wing is
    1-3 eligible strikes beyond the short put; the call wing is the eligible strike beyond the
    short call whose width is closest to the put wing's (balanced risk)."""
    sp = pick(ctx.puts, short_target, short_band, Side.SELL)
    sc = pick(ctx.calls, short_target, short_band, Side.SELL)
    if sp is None or sc is None or sp.strike >= sc.strike:
        return []
    out, seen = [], set()
    call_outs = [w for w in ctx.calls if w.strike > sc.strike + 1e-9]
    for n in wings:
        lp = _beyond(ctx.puts, sp, Right.PUT, n)
        if lp is None or not call_outs:
            break
        w = sp.strike - lp.strike
        lc = min(call_outs, key=lambda c: (abs((c.strike - sc.strike) - w), c.strike))
        key = (lp.strike, lc.strike)
        if key in seen:
            continue
        seen.add(key)
        out.append(make_candidate("iron_condor", [(lp, Side.BUY), (sp, Side.SELL), (sc, Side.SELL), (lc, Side.BUY)]))
    return out


def long_straddle(ctx: ExpiryContext) -> list[Candidate]:
    """Buy call + put at the same strike nearest the forward (both legs eligible). Between
    the two strikes bracketing the forward, the one with the better combined preference
    (cheaper legs) wins when it is better by more than PREF_MARGIN."""
    calls = {w.strike: w for w in ctx.calls}
    puts = {w.strike: w for w in ctx.puts}
    common = sorted(set(calls) & set(puts), key=lambda k: (abs(k - ctx.forward), k))[:2]
    if not common:
        return []
    best = common[0]
    if len(common) > 1:
        p0 = calls[common[0]].pref + puts[common[0]].pref
        p1 = calls[common[1]].pref + puts[common[1]].pref
        if p1 - p0 > PREF_MARGIN:
            best = common[1]
    return [make_candidate("long_straddle", [(calls[best], Side.BUY), (puts[best], Side.BUY)])]


def long_strangle(ctx: ExpiryContext, target: float = 0.30, band: tuple[float, float] = (0.25, 0.35)) -> list[Candidate]:
    """Buy an OTM put and an OTM call, each ~0.30 delta."""
    c = pick([w for w in ctx.calls if w.strike >= ctx.forward], target, band, Side.BUY)
    p = pick([w for w in ctx.puts if w.strike < ctx.forward], target, band, Side.BUY)
    if c is None or p is None or p.strike >= c.strike:
        return []
    return [make_candidate("long_strangle", [(p, Side.BUY), (c, Side.BUY)])]


__all__ = [
    "BULLISH", "BEARISH", "CREDIT", "VOL", "Candidate", "ExpiryContext", "Row",
    "credit_verticals", "debit_verticals", "expiry_context", "fitted_range", "implied_carry_q",
    "iron_condor", "liquid", "long_option", "long_straddle", "long_strangle", "make_candidate",
    "pick", "volume_all_zero",
]

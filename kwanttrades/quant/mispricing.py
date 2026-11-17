"""Volatility view and per-option mispricing scans.

Sign convention (types.Mispricing): ``edge = fair_value - market_mid``; > 0 means the option
is cheap (buy), < 0 rich (sell). ``edge_after_costs`` keeps the sign but is net of crossing
the spread and is 0 when the model value lies inside the bid/ask.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from kwanttrades.config import ScanConfig
from kwanttrades.quant import american, bs
from kwanttrades.quant.surface import SmileSlice, VolSurface
from kwanttrades.quant.volforecast import forecast_vol
from kwanttrades.types import Mispricing, OptionChain, OptionQuote, Right, VolView

_EPS = 1e-9
log = logging.getLogger(__name__)


def vol_view(chain: OptionChain, surface: VolSurface, daily_bars: pd.DataFrame, horizon_days: int, intraday_bars=None) -> VolView:
    """Compare the forecast realised vol with the surface's ATM implied vol at the horizon.

    ``horizon_days`` is in CALENDAR days (like option DTE): the implied side is
    ``surface.atm_iv_at(horizon_days)`` and the forecast runs over
    max(1, round(horizon_days * 252/365)) trading days. vol_edge =
    (forecast_rv - atm_iv) / atm_iv, set to 0.0 if either side is unavailable.
    ``components`` holds every forecast estimator plus ``atm_iv``."""
    H = max(int(horizon_days), 1)
    H_trading = max(1, int(round(H * 252.0 / 365.0)))
    try:
        atm = float(surface.atm_iv_at(H))
    except Exception:  # noqa: BLE001
        atm = float("nan")
    fc = forecast_vol(daily_bars, H_trading, intraday_bars)
    rv = float(fc.get("ensemble", float("nan")))
    edge = (rv - atm) / atm if (np.isfinite(rv) and np.isfinite(atm) and atm > 0) else 0.0
    comps = {k: float(v) for k, v in fc.items()}
    comps["atm_iv"] = atm
    return VolView(symbol=chain.underlying, horizon_days=H, atm_iv=atm, forecast_rv=rv, vol_edge=float(edge), components=comps)



# ----------------------------------------------------------------------------- data hygiene


@dataclass
class _Ctx:
    """Per-expiry context shared by the scans: forward, T, rate, ATM vol and band."""

    F: float
    T: float
    r: float
    atm: float

    @property
    def D(self) -> float:
        return math.exp(-self.r * self.T)

    @property
    def band(self) -> float:
        """Active-market moneyness band: |ln(K/F)| <= max(3 atm_iv sqrt(T), 0.05)."""
        a = self.atm if np.isfinite(self.atm) and self.atm > 0 else 0.3
        return max(3.0 * a * math.sqrt(self.T), 0.05)


def _liquid(q: OptionQuote, cfg: ScanConfig) -> bool:
    """Full ScanConfig liquidity filter: two-sided, bid >= min_bid, spread/mid <=
    max_spread_pct, open interest >= min_open_interest, volume >= min_volume."""
    return (
        q.has_two_sided_market
        and q.bid >= cfg.min_bid
        and q.spread_pct <= cfg.max_spread_pct
        and q.open_interest >= cfg.min_open_interest
        and q.volume >= cfg.min_volume
    )


def _is_otm(q: OptionQuote, F: float) -> bool:
    return q.strike >= F if q.right == Right.CALL else q.strike < F


def _is_stale(q: OptionQuote, ctx: _Ctx) -> bool:
    """Mid below discounted forward intrinsic e^{-rT} max(cp (F-K), 0) by more than the spread: a stale / bad quote, never an opportunity."""
    intrinsic = max(bs.cp_of(q.right) * ctx.D * (ctx.F - q.strike), 0.0)
    return q.mid < intrinsic - q.spread


def _in_band(q: OptionQuote, ctx: _Ctx) -> bool:
    return abs(math.log(q.strike / ctx.F)) <= ctx.band


class _Stats:
    def __init__(self):
        self.stale: set[str] = set()
        self.illiquid: set[str] = set()
        self.out_of_band: set[str] = set()
        self.skipped_slices: dict = {}

    def as_dict(self) -> dict:
        return {
            "rejected_stale": len(self.stale),
            "rejected_illiquid": len(self.illiquid),
            "rejected_out_of_band": len(self.out_of_band),
            "stale_symbols": sorted(self.stale)[:50],
            "svi_skipped_slices": dict(self.skipped_slices),
        }


def _eligible(q: OptionQuote, ctx: _Ctx, cfg: ScanConfig, st: _Stats) -> bool:
    """Liquid, not stale, inside the active moneyness band (rejections are counted)."""
    if not q.has_two_sided_market:
        st.illiquid.add(q.occ_symbol)
        return False
    if _is_stale(q, ctx):
        st.stale.add(q.occ_symbol)
        return False
    if not _liquid(q, cfg):
        st.illiquid.add(q.occ_symbol)
        return False
    if not _in_band(q, ctx):
        st.out_of_band.add(q.occ_symbol)
        return False
    return True


# ----------------------------------------------------------------------------- svi residual

_NEIGH = 3  # neighbours per side for the local residual median
_MIN_SIDE = 2  # minimum neighbours on each side (no flags at the edges of the strike range)
_MIN_POINTS = 7  # >= 6 neighbours + the point itself
_SCALE_FLOOR = 0.0015  # floor of the robust residual scale, vol units (0.15 vol points)
_SHORT_T = 1.0 / 252.0  # below ~1 trading day ...
_SHORT_T_MIN_POINTS = 40  # ... require this many usable points


def _svi_residuals(quotes: list[OptionQuote], sl: SmileSlice, ctx: _Ctx, cfg: ScanConfig, st: _Stats) -> list[Mispricing]:
    """Neighbour-relative smile anomalies.

    Usable points: OTM (puts K < F, calls K >= F), two-sided, not stale, finite mid IV.
    Sorted by strike, with r_i = market_iv_i - fit_iv_i:
      * local_i = median of r_j over up to 3 neighbours per side (excluding i); the local
        fair vol is fit_iv_i + local_i, i.e. the SVI fit corrected by the smooth local misfit;
      * anomaly a_i = market_iv_i - local fair vol;
      * noise_i = sqrt(half_spread_iv_i^2 + s^2), s = max(1.4826 MAD(a), 0.0015) over the slice;
      * zscore = -a_i / noise_i  (> 0 cheap, < 0 rich).
    A quote is flagged only if it passes the full liquidity filter and the moneyness band,
    |z| >= cfg.min_svi_zscore, it has >= 2 neighbours on each side (never at/outside the
    observed strike range), and the local fair price lies outside its bid/ask (cheap: fair >
    ask, rich: fair < bid). Slices need >= 7 usable points; slices shorter than one trading
    day need >= 40. Only SVI-fitted slices are scanned."""
    if sl.method != "svi":
        st.skipped_slices[sl.expiry] = f"method {sl.method}"
        return []
    F, T, r = ctx.F, ctx.T, ctx.r
    pts = [q for q in quotes if q.has_two_sided_market and _is_otm(q, F) and not _is_stale(q, ctx)]
    pts.sort(key=lambda q: q.strike)
    if not pts:
        return []
    K = np.array([q.strike for q in pts])
    cp = np.array([bs.cp_of(q.right) for q in pts], dtype=float)
    bid = np.array([q.bid for q in pts])
    ask = np.array([q.ask for q in pts])
    mid = 0.5 * (bid + ask)
    miv = np.asarray(bs.implied_vol(mid, F, K, T, r, r, cp), dtype=float).reshape(-1)
    keep = np.isfinite(miv)
    pts = [p for p, k in zip(pts, keep) if k]
    K, cp, bid, ask, mid, miv = K[keep], cp[keep], bid[keep], ask[keep], mid[keep], miv[keep]
    n = len(pts)
    need = _SHORT_T_MIN_POINTS if T < _SHORT_T else _MIN_POINTS
    if n < need:
        st.skipped_slices[sl.expiry] = f"{n} usable points < {need}"
        return []
    fit = np.asarray(sl.iv(K), dtype=float).reshape(-1)
    biv = np.asarray(bs.implied_vol(bid, F, K, T, r, r, cp), dtype=float).reshape(-1)
    aiv = np.asarray(bs.implied_vol(ask, F, K, T, r, r, cp), dtype=float).reshape(-1)
    vega = np.asarray(bs.greeks(F, K, T, r, r, miv, cp)["vega"], dtype=float).reshape(-1)
    hs_vol = np.where(np.isfinite(biv) & np.isfinite(aiv), 0.5 * (aiv - biv), 0.5 * (ask - bid) / np.maximum(vega, 1e-12))
    res = miv - fit
    local = np.full(n, np.nan)
    n_left = np.zeros(n, dtype=int)
    n_right = np.zeros(n, dtype=int)
    for i in range(n):
        lo, hi = max(0, i - _NEIGH), min(n, i + _NEIGH + 1)
        nb = np.concatenate([res[lo:i], res[i + 1 : hi]])
        n_left[i], n_right[i] = i - lo, hi - i - 1
        if nb.size:
            local[i] = float(np.median(nb))
    a = res - local
    fin = np.isfinite(a)
    if fin.sum() < need:
        return []
    mad = float(np.median(np.abs(a[fin] - np.median(a[fin]))))
    s = max(1.4826 * mad, _SCALE_FLOOR)
    noise = np.sqrt(np.maximum(hs_vol, 0.0) ** 2 + s * s)
    out = []
    for i, q in enumerate(pts):
        if not (fin[i] and n_left[i] >= _MIN_SIDE and n_right[i] >= _MIN_SIDE):
            continue
        z = -a[i] / noise[i]
        if abs(z) < cfg.min_svi_zscore:
            continue
        if not _eligible(q, ctx, cfg, st):
            continue
        fair_iv = fit[i] + local[i]
        if not (np.isfinite(fair_iv) and fair_iv > 0):
            continue
        fair = float(bs.price(F, K[i], T, r, r, fair_iv, cp[i]))
        if z > 0 and fair > ask[i]:
            eac = fair - ask[i]
        elif z < 0 and fair < bid[i]:
            eac = fair - bid[i]
        else:
            continue
        out.append(
            Mispricing(
                quote=q,
                model="svi_residual",
                fair_value=fair,
                market_mid=float(mid[i]),
                edge=float(fair - mid[i]),
                edge_after_costs=float(eac),
                zscore=float(z),
                market_iv=float(miv[i]),
                fair_iv=float(fair_iv),
                notes=(
                    f"bid_iv={biv[i]:.4f} ask_iv={aiv[i]:.4f} svi_iv={fit[i]:.4f} local_adj={local[i]:+.4f} "
                    f"noise={noise[i]:.4f} slice_scale={s:.4f}"
                ),
            )
        )
    return out


# ----------------------------------------------------------------------------- static arbitrage


_PAR_NEIGH = 4  # neighbouring strikes per side for the local forward line


def _theil_sen(x: np.ndarray, y: np.ndarray, x0: float) -> tuple[float, float]:
    """Theil-Sen line (median pairwise slope, median intercept): (value at x0, slope)."""
    dx = x[:, None] - x[None, :]
    dy = y[:, None] - y[None, :]
    m = dx > 0
    slope = float(np.median(dy[m] / dx[m])) if m.any() else 0.0
    return float(np.median(y - slope * x) + slope * x0), slope


def _parity(quotes: list[OptionQuote], sl: SmileSlice, ctx: _Ctx, div_yield: float, cfg: ScanConfig, st: _Stats, steps: int = 100) -> list[Mispricing]:
    """Put-call parity C - P = e^{-rT}(F_ref - K) violated beyond spreads + early exercise.

    Both legs must pass the liquidity filter, be non-stale and inside the moneyness band.
    F_ref is a Theil-Sen line through the implied forwards K' + e^{rT}(C' - P') of up to 4
    eligible neighbouring strikes per side (excluding the strike tested; slice forward if < 4
    neighbours), evaluated at K: an isolated bad pair is detected, smooth carry/early-exercise
    drift across strikes is not. The reference carries an uncertainty u = max(1.4826 MAD of
    the neighbours around the line, half their median combined half-spread), subtracted from
    the violation. Expiries shorter than one trading day are skipped.
    Executable synthetic forward: bid = C_bid - P_ask, ask = C_ask - P_bid, so the measured
    violation e^{-rT}(F-K) - syn_ask (too cheap) or syn_bid - e^{-rT}(F-K) (too rich) is
    already net of both half-spreads (relative to the mids). The tolerance added on top is the
    early-exercise premium of the put (cheap case) or of the call (rich case) from
    ``american.early_exercise_premium`` at the surface vol and the chain dividend yield. The
    leg reported is the one further (in price) from the surface model; its fair value is the
    parity value implied by the other leg's mid (pairs implying a non-positive fair are dropped)."""
    F, T, r = ctx.F, ctx.T, ctx.r
    if T < _SHORT_T:
        return []  # sub-one-day expiries: closing/0DTE parity noise dominates
    D = ctx.D
    # early-exercise tolerance is priced on the spot consistent with the implied forward
    # (robust to a stale underlying print), using the chain's dividend yield
    q_div = float(div_yield) if np.isfinite(div_yield) else 0.0
    spot = F * math.exp(-(r - q_div) * T)
    calls = {q.strike: q for q in quotes if q.right == Right.CALL and q.has_two_sided_market}
    puts = {q.strike: q for q in quotes if q.right == Right.PUT and q.has_two_sided_market}
    pairs = []
    for K in sorted(set(calls) & set(puts)):
        c, p = calls[K], puts[K]
        if _eligible(c, ctx, cfg, st) & _eligible(p, ctx, cfg, st):
            pairs.append((K, c, p))
    # per-strike implied forward from the mids; the reference forward for a strike is a
    # robust (Theil-Sen) line through its neighbours' values (excluding itself), evaluated at
    # K. This absorbs smooth strike-dependent carry effects (early exercise, discrete
    # dividends, stale spot) that a single forward cannot, even with one-sided neighbours.
    Ks = np.array([K for K, _, _ in pairs], dtype=float)
    Fk = np.array([K + (c.mid - p.mid) / D for K, c, p in pairs])
    hs_pair = np.array([0.5 * (c.spread + p.spread) for _, c, p in pairs])
    out = []
    for i, (K, c, p) in enumerate(pairs):
        lo, hi = max(0, i - _PAR_NEIGH), min(len(pairs), i + _PAR_NEIGH + 1)
        idx = [j for j in range(lo, hi) if j != i]
        if len(idx) >= 4:
            F_ref, slope = _theil_sen(Ks[idx], Fk[idx], K)
            resid = Fk[idx] - (F_ref + slope * (Ks[idx] - K))
            # uncertainty of the reference (price units): neighbour scatter around the line,
            # at least half the typical neighbour half-spread (their mids are that noisy)
            unc = max(1.4826 * float(np.median(np.abs(resid - np.median(resid)))) * D, 0.5 * float(np.median(hs_pair[idx])))
        else:
            F_ref, unc = F, 0.0
        pv = D * (F_ref - K)
        cheap = pv - (c.ask - p.bid) - unc
        rich = (c.bid - p.ask) - pv - unc
        if max(cheap, rich) <= _EPS:
            continue
        sig = float(sl.iv(K))
        if cheap > 0:
            tol = float(american.early_exercise_premium(spot, K, T, r, q_div, sig, -1, steps))
            viol = cheap - tol
        else:
            tol = float(american.early_exercise_premium(spot, K, T, r, q_div, sig, 1, steps))
            viol = rich - tol
        if viol <= _EPS:
            continue
        hc, hp = 0.5 * c.spread, 0.5 * p.spread
        # report the leg further (in price) from the surface model; its fair value is the
        # parity value implied by the other leg's mid
        dev_c = c.mid - float(sl.price(K, 1))
        dev_p = p.mid - float(sl.price(K, -1))
        if abs(dev_c) >= abs(dev_p):
            culprit, cpk, fair = c, 1, p.mid + pv
        else:
            culprit, cpk, fair = p, -1, c.mid - pv
        if not fair > 0:
            continue
        edge = fair - culprit.mid
        sign = 1.0 if edge > 0 else -1.0
        gap = abs(c.mid - p.mid - pv) - tol - unc
        z = sign * gap / max(hc + hp, 1e-6)
        miv = bs.implied_vol(culprit.mid, F, K, T, r, r, cpk)
        fiv = bs.implied_vol(fair, F, K, T, r, r, cpk) if fair > 0 else float("nan")
        out.append(
            Mispricing(
                quote=culprit,
                model="parity",
                fair_value=float(fair),
                market_mid=float(culprit.mid),
                edge=float(edge),
                edge_after_costs=float(sign * viol),
                zscore=float(z),
                market_iv=float(miv) if np.isfinite(miv) else None,
                fair_iv=float(fiv) if np.isfinite(fiv) else None,
                notes=f"C-P={c.mid - p.mid:.4f} parity={pv:.4f} eep_tol={tol:.4f}",
            )
        )
    return out


def _butterflies(quotes: list[OptionQuote], ctx: _Ctx, cfg: ScanConfig, st: _Stats) -> list[Mispricing]:
    """Convexity in strike with executable prices among eligible quotes (liquid, non-stale,
    in band): for neighbouring strikes K1 < K2 < K3, w1 = (K3-K2)/(K3-K1), w3 = 1 - w1, the fly
    w1 X(K1) - X(K2) + w3 X(K3) must cost >= 0. If w1 ask1 + w3 ask3 - bid2 < 0 (i.e. the
    convexity violation at mids exceeds the combined weighted half-spreads) the body is
    flagged rich."""
    out = []
    for right in (Right.CALL, Right.PUT):
        qs = sorted((q for q in quotes if q.right == right and q.has_two_sided_market and _eligible(q, ctx, cfg, st)), key=lambda q: q.strike)
        for q1, q2, q3 in zip(qs[:-2], qs[1:-1], qs[2:]):
            K1, K2, K3 = q1.strike, q2.strike, q3.strike
            if not (K1 < K2 < K3):
                continue
            w1 = (K3 - K2) / (K3 - K1)
            w3 = 1.0 - w1
            cost = w1 * q1.ask + w3 * q3.ask - q2.bid
            if cost >= -_EPS:
                continue
            fair = w1 * q1.mid + w3 * q3.mid
            hs = w1 * 0.5 * q1.spread + w3 * 0.5 * q3.spread + 0.5 * q2.spread
            out.append(
                Mispricing(
                    quote=q2,
                    model="butterfly",
                    fair_value=float(fair),
                    market_mid=float(q2.mid),
                    edge=float(fair - q2.mid),
                    edge_after_costs=float(cost),
                    zscore=float((fair - q2.mid) / max(hs, 1e-6)),
                    notes=f"wings {K1:g}/{K3:g} executable fly cost {cost:.4f}",
                )
            )
    return out


def _calendars(chain: OptionChain, ctxs: dict, cfg: ScanConfig, st: _Stats) -> list[Mispricing]:
    """American options are non-decreasing in maturity at fixed strike: if the farther
    expiry's ask is below the nearer expiry's bid (violation at mids beyond both half-spreads),
    the far option is flagged cheap. Both quotes must be eligible (liquid, non-stale, in band)
    and OTM relative to their own forward (where the check is also valid for European quotes)."""
    out = []
    by_key: dict[tuple, list[OptionQuote]] = {}
    for q in chain.quotes:
        if q.has_two_sided_market and q.expiry in ctxs:
            by_key.setdefault((q.right, round(q.strike, 6)), []).append(q)
    for qs in by_key.values():
        qs = sorted(qs, key=lambda q: q.expiry)
        for near, far in zip(qs[:-1], qs[1:]):
            if near.expiry == far.expiry:
                continue
            cn, cf = ctxs[near.expiry], ctxs[far.expiry]
            gap = near.bid - far.ask
            if gap <= _EPS:
                continue
            if not (_is_otm(near, cn.F) and _is_otm(far, cf.F)):
                continue
            if not (_eligible(near, cn, cfg, st) & _eligible(far, cf, cfg, st)):
                continue
            fair = near.mid
            out.append(
                Mispricing(
                    quote=far,
                    model="calendar",
                    fair_value=float(fair),
                    market_mid=float(far.mid),
                    edge=float(fair - far.mid),
                    edge_after_costs=float(gap),
                    zscore=float((fair - far.mid) / max(0.5 * (near.spread + far.spread), 1e-6)),
                    notes=f"near {near.expiry} bid {near.bid:.2f} > far ask {far.ask:.2f}",
                )
            )
    return out


# ----------------------------------------------------------------------------- entry point

last_scan_diagnostics: dict = {}


def scan_chain(chain: OptionChain, surface: VolSurface, cfg: ScanConfig) -> list[Mispricing]:
    """Run all mispricing scans and return flags sorted by |zscore| descending.

    * ``svi_residual``: neighbour-relative smile anomalies (see ``_svi_residuals``).
    * ``parity``: put-call parity beyond spreads + early-exercise tolerance.
    * ``butterfly`` / ``calendar``: static arbitrage in executable quoted prices.
    Every quote involved must pass the full ScanConfig liquidity filter, lie inside the
    active band |ln(K/F)| <= max(3 atm_iv sqrt(T), 5%), and not be stale (mid below intrinsic by
    more than the spread). Rejections are counted in ``surface.diagnostics["scan"]`` (also
    exposed as module-level ``last_scan_diagnostics`` and logged at INFO). Expiries without a
    fitted slice are not scanned. Never raises on malformed data."""
    cfg = cfg or ScanConfig()
    st = _Stats()
    flags: list[Mispricing] = []
    ctxs: dict = {}
    for expiry in chain.expiries():
        sl = surface.slices.get(expiry)
        if sl is None:
            continue
        try:
            ctx = _Ctx(F=sl.forward, T=sl.T, r=sl.rate, atm=sl.atm_iv)
        except Exception:  # noqa: BLE001
            continue
        ctxs[expiry] = ctx
        quotes = chain.for_expiry(expiry)
        for fn in (
            lambda: _svi_residuals(quotes, sl, ctx, cfg, st),
            lambda: _parity(quotes, sl, ctx, chain.div_yield, cfg, st),
            lambda: _butterflies(quotes, ctx, cfg, st),
        ):
            try:
                flags.extend(fn())
            except Exception as exc:  # noqa: BLE001
                log.warning("scan step failed for %s %s: %r", chain.underlying, expiry, exc)
    try:
        flags.extend(_calendars(chain, ctxs, cfg, st))
    except Exception as exc:  # noqa: BLE001
        log.warning("calendar scan failed for %s: %r", chain.underlying, exc)
    flags.sort(key=lambda m: abs(m.zscore), reverse=True)
    diag = st.as_dict()
    diag["n_flags"] = len(flags)
    global last_scan_diagnostics
    last_scan_diagnostics = diag
    try:
        surface.diagnostics["scan"] = diag
    except Exception:  # noqa: BLE001
        pass
    if diag["rejected_stale"]:
        log.info("%s: %d stale quotes (mid below intrinsic by > spread) ignored", chain.underlying, diag["rejected_stale"])
    return flags

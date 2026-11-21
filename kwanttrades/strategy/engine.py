"""Strategy brain: composite direction + vol view + surface mispricings -> ranked TradeIdeas.

Pipeline
--------
``analyze`` fits the vol surface, picks the vol-structure expiry (inside ``vol_dte``, closest to
30 DTE), computes the VolView at that horizon and runs the mispricing scan.
``generate`` applies the structure matrix, prices every candidate under OUR physical
distribution net of costs, filters, scores, de-duplicates and returns the best ideas.

Documented assumptions / judgement calls
----------------------------------------
* **Direction grading.** ``strong`` = |direction| >= ``cfg.scan.min_direction`` AND
  confidence >= ``self.min_confidence`` (0.30). ``neutral`` = |direction| < min_direction.
  Anything else (a large direction with low confidence) is ``unclear`` and produces no idea:
  it is neither a reliable directional view nor a reliable "nothing will happen" view.
* **Structure matrix.** strong & V >= min_vol_edge (cheap): long call/put + debit verticals.
  strong & V <= -min_vol_edge (rich): credit verticals in the direction + debit verticals
  financed by a short strike that scan_chain flagged rich. strong & |V| < min_vol_edge (no
  vol view; the matrix only names the sign): both vertical types, which are close to
  vega-neutral, and the EV decides. neutral & V <= -min_vol_edge: iron condor. neutral &
  V >= ``strong_vol_mult`` x min_vol_edge ("strongly" cheap, default 1.5x): long straddle and
  strangle. Everything else: no ideas.
* **Session clock (per expiry).** Vols are compared as TOTAL variance to expiry: our forecast
  (``forecast_vol`` with horizon = trading sessions to that expiry) on the session clock,
  w_f = sigma_f^2 * sessions / 252 (``clock.trading_sessions``: overnight + linear intraday
  share per session, holidays excluded), versus the market's w_m = atm_iv^2 * T_calendar.
  w_phys = VOL_SHRINK * w_f + (1 - VOL_SHRINK) * w_m; vol_scale = sqrt(w_phys / w_m); the
  expiry's vol edge sqrt(w_f / w_m) - 1 drives the cheap/rich routing of structures on that
  expiry (so a short-dated IV that is "high" only because of calendar annualisation is not
  mistaken for rich). The daily sigma of the direction tilt is sqrt(w_phys / sessions).
* **Physical distribution.** The distribution is ``physical.physical_dist``: the
  market-implied (Breeden-Litzenberger) density of that expiry's smile with every IV scaled by
  sigma_phys / atm_iv, so we bet on vol LEVEL and DIRECTION but never against the market's
  skew SHAPE (a symmetric lognormal always "finds" edge in selling crash-skewed puts);
  lognormal only as a recorded fallback. It is centred on the market forward (so a zero
  view reproduces the market) and shifted in log space by the tilt, where the TOTAL log-space mean shift
  over the life of the trade is ``tilt = direction * confidence * DRIFT_K * sigma_daily``,
  sigma_daily = sigma_phys / sqrt(252). Interpretation: the composite is an intraday signal,
  so it is credited with roughly one day's edge, not compounded every day to expiry
  (compounding would claim 50%+ annualised drifts from a 15-minute opening range).
* **Costs.** cost per share = (entry_limit - mid) [entry crossing incl. cent rounding] +
  slip * half_spread [exit crossing] + 2 * commission * legs / 100. entry_limit = mid + slip *
  half_spread rounded UP to the cent, which is exactly what PaperBroker needs to fill.
  max_loss / max_profit / reward:risk / credit:width are computed at entry_limit (what we
  would really pay), so they are slightly more conservative than at mid.
* **Humility.** EV is clipped at ``max_edge_frac`` (30%) of max_loss - model EVs far above that
  are almost always model error - and the clip is stated in the rationale. POP must lie in
  ``pop_bounds`` = [0.05, 0.95].
* **Diversity.** The best candidate of each strategy is taken first (no five widths of one
  vertical); remaining slots are filled with candidates sharing no leg with a chosen idea.
* **Stale quotes.** If ``chain.asof`` is outside 09:30-16:00 NY on a weekday, or every volume
  is zero, ideas are still produced (for review) with the rationale line ``STALE_LINE``.
* **Earnings.** No short-premium (credit) structure whose expiry is on/after a known earnings
  date that has not passed yet; long-premium structures spanning earnings get a warning line.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time

import numpy as np
import pandas as pd

from kwanttrades.config import Config
from kwanttrades.quant import bs
from kwanttrades.quant.distribution import TerminalDist, evaluate_structure
from kwanttrades.quant.mispricing import scan_chain, vol_view
from kwanttrades.quant.surface import VolSurface, build_surface
from kwanttrades.quant.volforecast import forecast_vol
from kwanttrades.strategy import clock
from kwanttrades.strategy import structures as st
from kwanttrades.strategy.physical import SOURCE_TEXT, physical_dist
from kwanttrades.strategy.structures import Candidate, ExpiryContext
from kwanttrades.types import (
    MULTIPLIER,
    NY,
    DirectionalSignal,
    Mispricing,
    OptionChain,
    Right,
    Side,
    TradeIdea,
    VolView,
)

VOL_SHRINK = 0.5  # weight on our forecast realised vol in sigma_phys (rest = market ATM IV)
DRIFT_K = 0.35  # mean log shift = direction * confidence * DRIFT_K * sigma_daily
STALE_LINE = "quotes are from the last close, for review only"

STRATEGY_LABELS = {
    "long_call": "Long call",
    "long_put": "Long put",
    "bull_call_spread": "Bull call debit spread",
    "bear_put_spread": "Bear put debit spread",
    "bull_put_spread": "Bull put credit spread",
    "bear_call_spread": "Bear call credit spread",
    "iron_condor": "Iron condor",
    "long_straddle": "Long straddle",
    "long_strangle": "Long strangle",
}


@dataclass
class SymbolAnalysis:
    symbol: str
    chain: OptionChain
    surface: VolSurface
    vol_view: VolView  # horizon = DTE of the expiry used for vol structures
    mispricings: list[Mispricing]
    direction: DirectionalSignal  # composite
    risk_multiplier: float = 1.0
    earnings_date: date | None = None
    notes: list[str] = field(default_factory=list)
    # additive, optional (filled by analyze): share of a session's variance realised overnight,
    # and per-expiry session-clock forecasts {expiry: {"sessions", "forecast", ...}}
    overnight_share: float = clock.DEFAULT_OVERNIGHT_SHARE
    expiry_vols: dict = field(default_factory=dict)


def _ceil_cent(x: float) -> float:
    return math.ceil(round(x * 100.0, 6) - 1e-9) / 100.0


def _fmt_money(x: float) -> str:
    return "unlimited" if math.isinf(x) else f"${x:,.2f}"


class StrategyEngine:
    """See module docstring. Tunables are instance attributes so a backtest can calibrate them."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.vol_shrink = VOL_SHRINK
        self.drift_k = DRIFT_K
        self.min_confidence = 0.30  # minimum composite confidence for a "strong" direction
        self.strong_vol_mult = 1.5  # neutral long-vol needs V >= this x min_vol_edge
        self.max_edge_frac = 0.30  # EV clipped at this fraction of max_loss
        self.pop_bounds = (0.05, 0.95)
        self.target_vol_dte = 30
        self.kurtosis_mix = 0.1
        self.vol_conf_scale = 0.30  # vol-structure confidence = min(1, |V| / this)
        self.last_rejections: list[tuple[str, str]] = []  # (candidate description, reason)

    # ------------------------------------------------------------------ small helpers
    @staticmethod
    def dte(chain: OptionChain, expiry: date) -> int:
        return (expiry - chain.asof.astimezone(NY).date()).days

    @staticmethod
    def is_stale(chain: OptionChain) -> bool:
        """Market closed at chain.asof (weekend / outside 09:30-16:00 NY) or no volume at all."""
        t = chain.asof.astimezone(NY)
        closed = t.weekday() >= 5 or not (time(9, 30) <= t.time() <= time(16, 0))
        return closed or st.volume_all_zero(chain)

    def grade(self, signal: DirectionalSignal) -> str:
        d, c = float(signal.direction), float(signal.confidence)
        if not np.isfinite(d) or abs(d) < self.cfg.scan.min_direction:
            return "neutral"
        if np.isfinite(c) and c >= self.min_confidence:
            return "bullish" if d > 0 else "bearish"
        return "unclear"

    def _contexts(self, chain, surface, mispricings, waive) -> dict[date, ExpiryContext]:
        out = {}
        for e in sorted(surface.slices):
            ctx = st.expiry_context(chain, surface, e, self.cfg.scan, mispricings, waive)
            if ctx is not None and ctx.usable:
                out[e] = ctx
        return out

    def directional_expiry(self, chain: OptionChain, ctxs: dict[date, ExpiryContext]) -> date | None:
        lo, hi = self.cfg.scan.directional_dte
        ok = [e for e in ctxs if lo <= self.dte(chain, e) <= hi]
        return min(ok) if ok else None

    def vol_expiry(self, chain: OptionChain, ctxs: dict[date, ExpiryContext]) -> date | None:
        lo, hi = self.cfg.scan.vol_dte
        ok = [e for e in ctxs if lo <= self.dte(chain, e) <= hi]
        return min(ok, key=lambda e: (abs(self.dte(chain, e) - self.target_vol_dte), e)) if ok else None

    # ------------------------------------------------------------------ analyze
    def analyze(
        self,
        symbol: str,
        chain: OptionChain,
        daily_bars: pd.DataFrame,
        direction: DirectionalSignal,
        intraday_bars: pd.DataFrame | None = None,
        earnings_date: date | None = None,
        risk_multiplier: float = 1.0,
    ) -> SymbolAnalysis:
        notes: list[str] = []
        surface = build_surface(chain, self.cfg.scan)
        if surface.diagnostics.get("skipped"):
            notes.append(f"surface skipped expiries: {sorted(str(e) for e in surface.diagnostics['skipped'])}")
        waive = st.volume_all_zero(chain)
        ctxs = self._contexts(chain, surface, None, waive)
        ve = self.vol_expiry(chain, ctxs)
        de = self.directional_expiry(chain, ctxs)
        horizon = self.dte(chain, ve) if ve else self.target_vol_dte
        notes.append(f"vol expiry {ve} ({horizon} DTE)" if ve else f"no liquid expiry inside vol_dte {self.cfg.scan.vol_dte}; vol view at {horizon}d")
        notes.append(f"directional expiry {de} ({self.dte(chain, de)} DTE)" if de else f"no liquid expiry inside directional_dte {self.cfg.scan.directional_dte}")
        on = clock.overnight_share(daily_bars)
        notes.append(f"overnight share of session variance {on:.0%}")
        expiry_vols: dict = {}
        fc_cache: dict[int, dict] = {}
        for e, ctx in ctxs.items():
            sessions = clock.trading_sessions(chain.asof, e, on)
            H = max(1, int(round(sessions)))
            if H not in fc_cache:
                try:
                    fc_cache[H] = {k: float(v) for k, v in forecast_vol(daily_bars, H, intraday_bars).items()}
                except Exception as exc:  # noqa: BLE001 - a failed forecast must not kill the symbol
                    fc_cache[H] = {}
                    notes.append(f"vol forecast failed for {H} sessions ({exc!r})")
            fc = fc_cache[H]
            ev_ = self.variance_view(ctx, sessions, fc.get("ensemble", float("nan")))
            ev_["components"] = fc
            expiry_vols[e] = ev_
            notes.append(
                f"{e} ({self.dte(chain, e)} DTE, {sessions:.2f} sessions): forecast {ev_['forecast']:.1%} vs ATM IV "
                f"{ev_['atm']:.1%}; total-variance vol edge {ev_['edge']:+.1%}"
            )
        if ve is not None and np.isfinite(expiry_vols[ve]["forecast"]):
            x = expiry_vols[ve]
            T = ctxs[ve].T
            comps = dict(x["components"])
            comps.update({"atm_iv": x["atm"], "session_forecast": x["forecast"], "sessions": x["sessions"], "overnight_share": on})
            vv = VolView(symbol, horizon, x["atm"], math.sqrt(x["w_f"] / T), x["edge"], comps)
        else:
            try:
                vv = vol_view(chain, surface, daily_bars, horizon, intraday_bars)
            except Exception as exc:  # noqa: BLE001
                atm = surface.atm_iv_at(horizon) if surface.slices else float("nan")
                vv = VolView(symbol, horizon, float(atm), float("nan"), 0.0, {})
                notes.append(f"vol forecast failed ({exc!r}); vol edge set to 0")
        try:
            mis = scan_chain(chain, surface, self.cfg.scan)
        except Exception as exc:  # noqa: BLE001
            mis = []
            notes.append(f"mispricing scan failed ({exc!r})")
        if self.is_stale(chain):
            notes.append(STALE_LINE)
        return SymbolAnalysis(symbol, chain, surface, vv, list(mis), direction, float(risk_multiplier), earnings_date, notes, on, expiry_vols)

    # ------------------------------------------------------------------ session-clock variance
    def variance_view(self, ctx: ExpiryContext, sessions: float, sigma_f: float) -> dict:
        """Compare TOTAL variance to expiry: forecast on the session clock vs market on the
        calendar clock. w_f = sigma_f^2 * sessions/252, w_m = atm_iv^2 * T_calendar,
        w_phys = shrink*w_f + (1-shrink)*w_m; vol_scale = sqrt(w_phys/w_m), edge =
        sqrt(w_f/w_m) - 1, sigma_daily = sqrt(w_phys / sessions) (per-session sd). An
        unavailable forecast gives w_f = w_m (no edge)."""
        atm = float(ctx.slice.atm_iv)
        w_m = atm * atm * ctx.T
        s = max(float(sessions), 1e-6)
        if np.isfinite(sigma_f) and sigma_f > 0:
            w_f = sigma_f * sigma_f * s / 252.0
        else:
            w_f = w_m
        w_phys = self.vol_shrink * w_f + (1.0 - self.vol_shrink) * w_m
        ok = w_m > 0
        return {
            "sessions": float(sessions), "forecast": float(sigma_f), "atm": atm,
            "w_f": w_f, "w_m": w_m, "w_phys": w_phys,
            "vol_scale": math.sqrt(w_phys / w_m) if ok else 1.0,
            "edge": math.sqrt(w_f / w_m) - 1.0 if ok else 0.0,
            "sigma_daily": math.sqrt(w_phys / max(s, 0.05)),
        }

    def expiry_view(self, analysis: SymbolAnalysis, ctx: ExpiryContext) -> dict:
        """Per-expiry variance view: from analyze's expiry-specific forecast when present,
        otherwise (hand-built analyses) from vol_view's session forecast at this expiry's
        session count."""
        pre = analysis.expiry_vols.get(ctx.expiry) if analysis.expiry_vols else None
        if pre is not None:
            return pre
        sessions = clock.trading_sessions(analysis.chain.asof, ctx.expiry, analysis.overnight_share)
        comps = analysis.vol_view.components or {}
        sigma_f = float(comps.get("session_forecast", analysis.vol_view.forecast_rv))
        return self.variance_view(ctx, sessions, sigma_f)

    # ------------------------------------------------------------------ generate
    def candidates(self, analysis: SymbolAnalysis) -> list[tuple[Candidate, str]]:
        """Candidates from the structure matrix, each tagged "directional" or "vol"."""
        sc = self.cfg.scan
        chain = analysis.chain
        waive = st.volume_all_zero(chain)
        ctxs = self._contexts(chain, analysis.surface, analysis.mispricings, waive)
        g = self.grade(analysis.direction)
        out: list[tuple[Candidate, str]] = []
        if g in ("bullish", "bearish"):
            de = self.directional_expiry(chain, ctxs)
            if de is not None:
                ctx = ctxs[de]
                V = self.expiry_view(analysis, ctx)["edge"]  # this expiry's total-variance edge
                rl = Right.CALL if g == "bullish" else Right.PUT  # long-premium right
                rc = Right.PUT if g == "bullish" else Right.CALL  # credit-spread right
                cs: list[Candidate] = []
                if V >= sc.min_vol_edge:
                    cs += st.long_option(ctx, rl) + st.debit_verticals(ctx, rl)
                elif V <= -sc.min_vol_edge:
                    cs += st.credit_verticals(ctx, rc) + st.debit_verticals(ctx, rl, require_rich_short=True)
                else:
                    cs += st.debit_verticals(ctx, rl) + st.credit_verticals(ctx, rc)
                out += [(c, "directional") for c in cs]
        elif g == "neutral":
            ve = self.vol_expiry(chain, ctxs)
            if ve is not None:
                ctx = ctxs[ve]
                V = self.expiry_view(analysis, ctx)["edge"]
                if V <= -sc.min_vol_edge:
                    out += [(c, "vol") for c in st.iron_condor(ctx)]
                elif V >= self.strong_vol_mult * sc.min_vol_edge:
                    out += [(c, "vol") for c in st.long_straddle(ctx) + st.long_strangle(ctx)]
        return out

    def _dist(self, analysis: SymbolAnalysis, ctx: ExpiryContext) -> tuple[TerminalDist, dict]:
        x = self.expiry_view(analysis, ctx)
        sig = analysis.direction
        d = float(sig.direction) if np.isfinite(sig.direction) else 0.0
        c = float(np.clip(sig.confidence, 0.0, 1.0)) if np.isfinite(sig.confidence) else 0.0
        tilt = d * c * self.drift_k * x["sigma_daily"]  # one total shift, session-based daily sd
        dist = physical_dist(ctx.slice, ctx.forward, ctx.T, x["vol_scale"], tilt, kurtosis_mix=self.kurtosis_mix)
        source = getattr(dist, "source", "lognormal")
        return dist, {
            **x, "tilt": tilt, "tilt_sd": d * c * self.drift_k,
            "source": source, "source_reason": getattr(dist, "reason", ""),
        }

    def _greeks(self, cand: Candidate, ctx: ExpiryContext) -> dict[str, float]:
        tot = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
        for leg in cand.legs:
            q = leg.quote
            iv = float(ctx.slice.iv(q.strike))
            g = bs.greeks(ctx.spot, q.strike, ctx.T, ctx.r, ctx.q, iv, bs.cp_of(q.right))
            s = (1.0 if leg.side == Side.BUY else -1.0) * leg.ratio * MULTIPLIER
            for k in tot:
                tot[k] += s * float(g[k])
        return {k: round(v, 4) for k, v in tot.items()}

    @staticmethod
    def _describe(c: Candidate, dte: int) -> str:
        parts = [f"{'buy' if l.side == Side.BUY else 'sell'} {l.quote.right.value}{l.quote.strike:g}" for l in c.legs]
        return f"{' / '.join(parts)} exp {c.expiry} ({dte} DTE)"

    def evaluate(self, analysis: SymbolAnalysis, cand: Candidate, kind: str, ctx: ExpiryContext, dist_info) -> tuple[TradeIdea | None, str]:
        """Price one candidate under our view; return (idea, "") or (None, rejection reason)."""
        cfg, sc, risk = self.cfg, self.cfg.scan, self.cfg.risk
        chain = analysis.chain
        dist, info = dist_info
        slip = cfg.broker.slippage_frac_of_half_spread
        n_legs = sum(l.ratio for l in cand.legs)
        mid = cand.net_mid
        limit = _ceil_cent(mid + slip * cand.half_spread)
        credit_type = cand.is_credit_type
        if credit_type and not limit < 0:
            return None, "credit structure does not collect a credit at the limit"
        if not credit_type and not limit > 0:
            return None, "debit structure has no debit"
        cost_ps = (limit - mid) + slip * cand.half_spread + 2.0 * cfg.broker.commission_per_contract * n_legs / MULTIPLIER
        full = evaluate_structure(cand.legs, mid, dist, cost_ps)
        at_limit = evaluate_structure(cand.legs, limit, TerminalDist(np.array([ctx.spot]), np.array([1.0])))
        max_loss, max_profit = float(at_limit["max_loss"]), float(at_limit["max_profit"])
        if not (np.isfinite(max_loss) and max_loss > 0):
            return None, "undefined or zero max loss"
        if credit_type:
            credit = -limit
            if cand.width <= 0 or credit / cand.width < risk.min_credit_to_width - 1e-9:
                return None, f"credit/width {credit / max(cand.width, 1e-9):.2f} < {risk.min_credit_to_width}"
            rm_ratio = (-mid) / (max_loss / MULTIPLIER - mid)
            if rm_ratio < risk.min_credit_to_width - 1e-9:
                return None, f"credit/width (risk-gate formula) {rm_ratio:.2f} < {risk.min_credit_to_width}"
        else:
            rr = math.inf if math.isinf(max_profit) else max_profit / max_loss
            if rr < risk.min_reward_risk - 1e-9:
                return None, f"reward:risk {rr:.2f} < {risk.min_reward_risk}"
        ed = analysis.earnings_date
        asof_d = chain.asof.astimezone(NY).date()
        spans_earnings = ed is not None and asof_d <= ed <= cand.expiry
        if spans_earnings and credit_type:
            return None, f"short premium through earnings on {ed}"
        pop = float(full["pop"])
        if not (self.pop_bounds[0] <= pop <= self.pop_bounds[1]):
            return None, f"POP {pop:.2f} outside {self.pop_bounds}"
        ev_model = float(full["ev"])
        cap = self.max_edge_frac * max_loss
        clipped = ev_model > cap
        ev = min(ev_model, cap)
        if ev < sc.min_expected_value:
            return None, f"EV ${ev:.2f} < ${sc.min_expected_value:.2f}"
        V = float(info["edge"])  # this expiry's total-variance vol edge
        if kind == "directional":
            conf = float(np.clip(analysis.direction.confidence, 0.0, 1.0))
        else:
            conf = min(1.0, abs(V) / self.vol_conf_scale)
        score = ev / max_loss * conf * float(analysis.risk_multiplier)
        if score < sc.min_score:
            return None, f"score {score:.3f} < {sc.min_score}"

        # ---------------------------------------------------------------- rationale
        sig = analysis.direction
        dte = self.dte(chain, cand.expiry)
        g = self.grade(sig)
        agree = sig.detail.get("agreement") if isinstance(sig.detail, dict) else None
        lines = [
            f"Signal: composite {g} (direction {sig.direction:+.2f}, confidence {sig.confidence:.2f}"
            + (f", agreement {float(agree):.2f}" if isinstance(agree, (int, float)) else "")
            + f"); regime risk multiplier {analysis.risk_multiplier:.2f}",
        ]
        vv = analysis.vol_view
        view = "options cheap" if V >= sc.min_vol_edge else ("options rich" if V <= -sc.min_vol_edge else "no clear vol edge")
        lines.append(
            f"Vol view (headline, {vv.horizon_days}d): ATM IV {vv.atm_iv:.1%}, total-variance edge {vv.vol_edge:+.0%}"
        )
        fc_txt = f"{info['forecast']:.1%}" if np.isfinite(info["forecast"]) else "n/a"
        lines.append(
            f"Vol at this expiry: {info['sessions']:.2f} trading sessions left; forecast {fc_txt} (per session clock) vs "
            f"market ATM IV {info['atm']:.1%} (calendar clock) -> total-variance edge {V:+.0%}, {view}"
        )
        lines.append(
            f"Model: market variance x{info['vol_scale'] ** 2:.2f} ({self.vol_shrink:.0%} forecast / {1 - self.vol_shrink:.0%} market "
            f"total variance), mean shift {info['tilt_sd']:+.2f} session sigma from the signal"
        )
        lines.append(
            "Distribution: " + SOURCE_TEXT.get(info["source"], info["source"])
            + (f" ({info['source_reason']})" if info.get("source_reason") else "")
        )
        side_txt = "debit" if limit > 0 else "credit"
        lines.append(
            f"{STRATEGY_LABELS.get(cand.strategy, cand.strategy)}: {self._describe(cand, dte)}; "
            f"{side_txt} {abs(mid):.2f} at mid, limit {abs(limit):.2f}"
        )
        lines.append("Mispriced legs used: " + ("; ".join(cand.flags) if cand.flags else "none flagged (legs chosen by delta)"))
        be = ", ".join(f"{b:,.2f}" for b in full["breakevens"]) or "none"
        lines.append(
            f"EV ${ev:,.2f} per contract-set after costs (${cost_ps * MULTIPLIER:,.2f} spread + commission), "
            f"POP {pop:.0%}, breakevens {be}; max loss {_fmt_money(max_loss)}, max profit {_fmt_money(max_profit)}"
        )
        if clipped:
            lines.append(
                f"Model EV ${ev_model:,.2f} clipped to ${ev:,.2f} ({self.max_edge_frac:.0%} of max loss): "
                "edges that large are usually model error, not opportunity"
            )
        if kind == "directional":
            lines.append(
                f"intraday: directional trade on today's signal; flatten by {self.cfg.risk.flatten_time} NY if not closed earlier"
            )
        if spans_earnings:
            lines.append(f"Warning: expiry spans earnings on {ed} (IV crush / gap risk)")
        if self.is_stale(chain):
            lines.append(STALE_LINE)
        idea = TradeIdea(
            symbol=analysis.symbol,
            strategy=cand.strategy,
            legs=list(cand.legs),
            net_price=round(mid, 4),
            entry_limit=limit,
            max_loss=round(max_loss, 2),
            max_profit=max_profit if math.isinf(max_profit) else round(max_profit, 2),
            pop=round(pop, 4),
            expected_value=round(ev, 2),
            score=round(score, 4),
            rationale=lines,
            greeks=self._greeks(cand, ctx),
        )
        return idea, ""

    def generate(self, analysis: SymbolAnalysis) -> list[TradeIdea]:
        """Ranked (best-first) defined-risk ideas for one symbol; at most max_ideas_per_symbol."""
        self.last_rejections = []
        chain = analysis.chain
        waive = st.volume_all_zero(chain)
        cands = self.candidates(analysis)
        if not cands:
            return []
        ctx_cache: dict[date, ExpiryContext] = {}
        dist_cache: dict[date, tuple] = {}
        scored: list[TradeIdea] = []
        seen = set()
        for cand, kind in cands:
            if cand.key in seen:
                continue
            seen.add(cand.key)
            if cand.expiry not in ctx_cache:
                ctx_cache[cand.expiry] = st.expiry_context(chain, analysis.surface, cand.expiry, self.cfg.scan, analysis.mispricings, waive)
                dist_cache[cand.expiry] = self._dist(analysis, ctx_cache[cand.expiry])
            ctx = ctx_cache[cand.expiry]
            idea, why = self.evaluate(analysis, cand, kind, ctx, dist_cache[cand.expiry])
            if idea is None:
                self.last_rejections.append((f"{cand.strategy} {self._describe(cand, self.dte(chain, cand.expiry))}", why))
            else:
                scored.append(idea)
        return self.select(scored, self.cfg.scan.max_ideas_per_symbol)

    @staticmethod
    def select(ideas: list[TradeIdea], n: int) -> list[TradeIdea]:
        """Diverse top-n: best idea per strategy first, then others sharing no leg with a pick."""
        ranked = sorted(ideas, key=lambda i: (-i.score, -i.expected_value, i.strategy))
        picked: list[TradeIdea] = []
        strategies: set[str] = set()
        for i in ranked:
            if len(picked) >= n:
                break
            if i.strategy not in strategies:
                picked.append(i)
                strategies.add(i.strategy)
        used = {(l.quote.occ_symbol, l.side) for p in picked for l in p.legs}
        for i in ranked:
            if len(picked) >= n:
                break
            if i in picked:
                continue
            legs = {(l.quote.occ_symbol, l.side) for l in i.legs}
            if legs & used:
                continue
            picked.append(i)
            used |= legs
        return sorted(picked, key=lambda i: (-i.score, -i.expected_value, i.strategy))


__all__ = ["DRIFT_K", "STALE_LINE", "STRATEGY_LABELS", "VOL_SHRINK", "StrategyEngine", "SymbolAnalysis"]

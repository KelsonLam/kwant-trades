"""LearningHub: the glue between a TradingSession step and the learning loop.

Attached to a session as ``session.learning``. Every call is wrapped so a failure here can never break trading.
Recording (features, marks, post-mortems) and learning happen only for the paper and alpaca_paper modes; scan
sessions still get learned idea adjustments (so the ideas list shows what the trading loop would do).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta
from typing import Any, Callable

from kwanttrades.app.session import effective_time
from kwanttrades.execution.exits import structure_mark
from kwanttrades.learning import postmortem as pmmod
from kwanttrades.learning.learner import LEARNING_MODES, Learner
from kwanttrades.types import NY, Position

log = logging.getLogger("kwanttrades.learning")

TRACK_LOOKBACK_DAYS = 60  # closed positions older than this are never tracked (their expiry has passed anyway)


class LearningHub:
    def __init__(self, cfg: Any, journal: Any, learner: Learner):
        self.cfg = cfg
        self.journal = journal
        self.learner = learner
        self._lock = threading.RLock()
        self._last_mark: dict[str, datetime] = {}
        self._tracking_done: set[str] = set()
        self.callbacks: list[Callable[[Any, datetime, list[dict]], None]] = []

    # ------------------------------------------------------------------ idea adjustments (all modes)
    def adjust_idea(self, idea: Any) -> str | None:
        try:
            return self.learner.adjust_idea(idea)
        except Exception as exc:
            log.warning("learned idea adjustment failed: %s", exc)
            return None

    def entry_start(self, base_start: str) -> str:
        try:
            return self.learner.entry_start(base_start)
        except Exception as exc:  # pragma: no cover
            log.warning("learned entry start failed: %s", exc)
            return base_start

    def signal_weights(self, base: dict[str, float]) -> dict[str, float]:
        try:
            return self.learner.signal_weights(base)
        except Exception as exc:  # pragma: no cover
            log.warning("learned signal weights failed: %s", exc)
            return base

    @staticmethod
    def records(session: Any) -> bool:
        return getattr(session, "mode", None) in LEARNING_MODES and getattr(session, "broker", None) is not None

    # ------------------------------------------------------------------ post-exit tracking set
    def tracked(self, session: Any, now: datetime) -> list[Position]:
        """Closed positions whose original expiry has not passed: their structure keeps being marked."""
        if not self.records(session):
            return []
        try:
            allp = session.broker.positions(include_closed=True)
        except Exception:
            return []
        out = []
        oldest = now - timedelta(days=TRACK_LOOKBACK_DAYS)
        for p in allp:
            if p.is_open or p.id in self._tracking_done or p.closed is None or p.closed < oldest:
                continue
            if now.astimezone(NY).date() > p.idea.expiry:
                continue
            out.append(p)
        return out

    def tracked_expiries(self, session: Any, now: datetime) -> dict[str, set]:
        out: dict[str, set] = {}
        try:
            for p in self.tracked(session, now):
                out.setdefault(p.idea.symbol, set()).update(l.quote.expiry for l in p.idea.legs)
        except Exception as exc:  # pragma: no cover
            log.debug("tracked expiries failed: %s", exc)
        return out

    # ------------------------------------------------------------------ mark paths
    def after_marks(self, session: Any, now: datetime) -> None:
        if not self.records(session):
            return
        try:
            with self._lock:
                self._record_marks(session, now)
        except Exception as exc:
            log.warning("mark path recording failed: %s", exc)

    def _mark(self, pid: str, now: datetime, mark: float, spot: float | None, phase: str) -> None:
        last = self._last_mark.get(pid)
        if last is not None and last >= now:
            return
        self.journal.record_mark(pid, now, mark, spot, phase)
        self._last_mark[pid] = now

    def _record_marks(self, session: Any, now: datetime) -> None:
        chains = getattr(session, "chains", {}) or {}
        for p in session.broker.positions():
            ch = chains.get(p.idea.symbol)
            if ch is None:
                continue
            m = structure_mark(p, ch)
            if m is not None:
                self._mark(p.id, now, m, float(ch.spot), "open")
        for p in self.tracked(session, now):
            ch = chains.get(p.idea.symbol)
            if ch is None:
                continue
            exp_close = datetime.combine(p.idea.expiry, time(16, 0), tzinfo=NY)
            if now >= exp_close:
                # final point: settlement at intrinsic value, only if the quotes are from the expiry session itself
                if effective_time(now).date() == p.idea.expiry:
                    v = pmmod.intrinsic_value(p.idea, float(ch.spot), p.idea.expiry)
                    self._mark(p.id, exp_close, v, float(ch.spot), "post")
                self._tracking_done.add(p.id)
                continue
            m = structure_mark(p, ch)
            if m is not None:
                self._mark(p.id, now, m, float(ch.spot), "post")

    # ------------------------------------------------------------------ entry features
    def on_entry(self, session: Any, idea: Any, contracts: int, order: dict, rec: Any, analysis: Any, rm: float,
                 now: datetime) -> None:
        if not self.records(session):
            return
        try:
            comp = getattr(rec, "composite", None)
            orb = (getattr(rec, "components", None) or {}).get("orb")
            b = self.cfg.broker
            feats = pmmod.entry_features(idea, now=now, contracts=contracts, composite=comp,
                                         news=(getattr(rec, "components", None) or {}).get("news"),
                                         orb_state=(orb.detail.get("state") if orb is not None else None),
                                         spot=getattr(rec, "spot", None), analysis=analysis, risk_multiplier=rm,
                                         slip=b.slippage_frac_of_half_spread, commission=b.commission_per_contract)
            self.journal.save_entry_features(order.get("order_id") or f"noid-{now.timestamp()}", order.get("position_id"),
                                             session.mode, now, feats)
        except Exception as exc:
            log.warning("entry feature recording failed: %s", exc)

    # ------------------------------------------------------------------ post-mortems + review
    def after_step(self, session: Any, now: datetime) -> list[dict]:
        if not self.records(session):
            return []
        new: list[dict] = []
        try:
            with self._lock:
                new = self._post_mortems(session, now)
                changed = self._update_tracking(session, now)
                if new or changed:
                    self.review(now, session.mode)
        except Exception as exc:
            log.warning("post-mortem/learning step failed: %s", exc)
        for cb in list(self.callbacks):
            try:
                cb(session, now, new)
            except Exception as exc:
                log.warning("learning callback failed: %s", exc)
        return new

    def _features_for(self, pos: Position) -> dict:
        f = self.journal.entry_features(pos.id)
        if f:
            return f
        b = self.cfg.broker
        return pmmod.fallback_features(pos, b.slippage_frac_of_half_spread, b.commission_per_contract)

    def _exit_spot(self, session: Any, pos: Position, now: datetime) -> float | None:
        if pos.exit_reason == "expired":
            sp = (getattr(session, "last_settle_spots", None) or {}).get(pos.idea.symbol)
            if sp:
                return float(sp)
        ch = (getattr(session, "chains", {}) or {}).get(pos.idea.symbol)
        if ch is not None and pos.closed is not None and (now - pos.closed) <= timedelta(minutes=10):
            return float(ch.spot)
        return None

    def _post_mortems(self, session: Any, now: datetime) -> list[dict]:
        have = self.journal.post_mortem_ids()
        out = []
        for p in session.broker.positions(include_closed=True):
            if p.is_open or p.id in have or p.closed is None:
                continue
            feats = self._features_for(p)
            marks = self.journal.marks(p.id)
            row = pmmod.build(p, session.mode, feats, marks, self._exit_spot(session, p, now), now,
                              self.cfg.risk.flatten_time)
            self.journal.save_post_mortem(row)
            out.append(row)
            log.info("post-mortem %s %s: %s", p.idea.symbol, p.idea.strategy, row["cause_text"])
        return out

    def _update_tracking(self, session: Any, now: datetime) -> bool:
        """Re-check unfinished post-mortems against the post-exit tracking (e.g. "stopped out, then it recovered")."""
        changed = False
        byid = {p.id: p for p in session.broker.positions(include_closed=True) if not p.is_open}
        for pm in self.journal.post_mortems():
            if pm.get("tracking_done") or pm["position_id"] not in byid:
                continue
            p = byid[pm["position_id"]]
            done = pmmod.tracking_finished(p, now) or p.id in self._tracking_done
            marks = self.journal.marks(p.id)
            row = pmmod.build(p, pm.get("mode") or session.mode, pm.get("features") or self._features_for(p), marks,
                              (pm.get("outcome") or {}).get("exit_spot"), now, self.cfg.risk.flatten_time)
            row["tracking_done"] = done
            if row["cause"] != pm.get("cause") or done or row["outcome"].get("post_marks") != (pm.get("outcome") or {}).get("post_marks"):
                self.journal.save_post_mortem(row)
                if row["cause"] != pm.get("cause"):
                    changed = True
                    log.info("post-mortem %s reclassified: %s", p.id, row["cause_text"])
            if done:
                self._tracking_done.add(p.id)
        return changed

    def trades(self) -> list[dict]:
        return [t for t in self.journal.post_mortems() if t.get("mode") in LEARNING_MODES]

    def review(self, now: datetime, mode: str) -> dict | None:
        trades = self.trades()
        paths = {t["position_id"]: self.journal.marks(t["position_id"]) for t in trades}
        return self.learner.review(trades, now, mode, paths)

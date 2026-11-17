import { h, panel, term, stagger, decisionChip, signed, label } from "../dom.js";
import { store } from "../store.js";
import { money, signedMoney, pct, timeNY, relTime, isNum, num } from "../format.js";
import { lineChart, gauge } from "../charts.js";
import { MODES, MARKET_STATES, strategyInfo } from "../glossary.js";
import { ideaCard, scanControl, emptyIdeas, serverNow, progressPair, delayNote, strategyTag } from "../shared.js";

function summarySentence(st) {
  if (!st) return ["Connecting…"];
  const market = MARKET_STATES[st.market?.state] || "Market state unknown";
  const mode = st.running ? (MODES[st.mode]?.desc || st.mode) : "Not running — nothing will be traded";
  const parts = [`${market}.`, `${mode}.`];
  if (st.kill_switch) parts.push("Kill switch is ON: no new trades today.");
  else if (isNum(st.trades_today)) parts.push(`${st.trades_today} of ${st.max_trades_per_day ?? "—"} trades used today.`);
  return parts;
}

export function render(root) {
  const hero = h("section", { class: "hero cu", "aria-label": "Summary" });
  const drawHero = () => {
    const st = store.data("status");
    hero.replaceChildren(
      h("p", { class: "eyebrow" }, "Right now"),
      h("p", { class: "hero-line" }, ...summarySentence(st).map((p, i) => h("span", { class: i === 0 ? "hl" : "" }, p + " "))),
    );
  };
  const uh = store.subscribe("status", drawHero); drawHero();
  hero._cleanup = uh;

  const top = panel({
    title: "Top opportunities", cls: "span-2 ideas-top", store, key: "ideas",
    actions: h("a", { href: "#/opportunities", class: "linkbtn" }, "See all →"),
    render: (d, { first }) => {
      const ideas = (d?.ideas || []).slice().sort((a, b) => (b.score ?? -1e9) - (a.score ?? -1e9)).slice(0, 3);
      if (!ideas.length) return null;
      const grid = h("div", { class: "card-grid three" }, ideas.map((i, k) => ideaCard(i, { compact: true, rank: k + 1, animate: first })));
      if (first) stagger([...grid.children], 60);
      return h("div", null, grid, h("p", { class: "muted small" }, d.asof ? `Ideas from the scan at ${timeNY(d.asof)} NY. ` : "", delayNote()));
    },
    empty: emptyIdeas,
  });
  top.querySelector(".panel-head").after(scanControl({ compact: false }));

  const positions = panel({
    title: "What you hold", store, key: "positions",
    actions: h("a", { href: "#/positions", class: "linkbtn" }, "Manage →"),
    render: (d) => {
      const open = d?.open || [];
      if (!open.length) return null;
      const total = open.reduce((a, p) => a + (isNum(p.unrealized_pnl) ? p.unrealized_pnl : 0), 0);
      return h("div", null,
        h("p", { class: "big-figure" }, signed(signedMoney(total), total), h("span", { class: "muted small" }, ` open profit/loss on ${open.length} position${open.length > 1 ? "s" : ""}`)),
        h("ul", { class: "pos-mini" }, open.map((p) => h("li", null,
          h("div", { class: "pm-top" }, h("strong", { class: "sym" }, p.symbol ?? "—"), " ", h("span", { class: "muted small" }, strategyInfo(p.strategy, p.strategy_label).label),
            h("span", { class: "pm-pnl" }, signed(signedMoney(p.unrealized_pnl), p.unrealized_pnl))),
          progressPair(p)))));
    },
    empty: { title: "No open positions", hint: "When the system takes a trade in a paper mode it will appear here." },
  });

  const equity = panel({
    title: "Account value", cls: "span-2", store, key: "equity",
    render: (d, { first }) => {
      const pts = (d?.points || []).map((p) => ({ t: p.t, v: p.equity }));
      if (pts.length < 2) return null;
      const last = pts[pts.length - 1].v, start = d.starting_equity;
      return h("div", null,
        h("p", { class: "big-figure" }, money(last), " ", isNum(start) ? signed(`${signedMoney(last - start, { cents: false })} since start`, last - start, "small") : null),
        lineChart(pts, { height: 210, baseline: isNum(start) ? start : null, baselineLabel: "Starting equity", label: "Account value over time", animate: first }));
    },
    empty: { title: "No equity history yet", hint: "The curve starts once a session has run." },
  });

  const regime = panel({
    title: "Market mood", store, key: "signals",
    render: (d) => {
      const r = d?.regime;
      if (!r) return null;
      const dirWord = r.direction > 0.15 ? "Leaning bullish" : r.direction < -0.15 ? "Leaning bearish" : "Neutral";
      const det = r.detail && typeof r.detail === "object" ? r.detail : {};
      return h("div", { class: "regime" },
        gauge(r.direction, { size: 200 }),
        h("p", { class: "regime-word" }, dirWord),
        h("dl", { class: "kv" },
          h("dt", null, term("confidence", "Confidence")), h("dd", { class: "num" }, pct(r.confidence, 0)),
          h("dt", null, term("risk_multiplier", "Position size")), h("dd", { class: "num" }, isNum(r.risk_multiplier) ? `${num(r.risk_multiplier * 100, 0)}% of normal` : "—"),
          ...Object.entries(det).slice(0, 4).flatMap(([k, v]) => [h("dt", null, k.replace(/_/g, " ")), h("dd", null, typeof v === "number" ? (k.includes("percentile") ? pct(v, 0) : num(v, 1)) : String(v ?? "—"))])),
        h("p", { class: "muted small" }, term("regime", "What is this?")));
    },
    empty: { title: "No market regime yet", hint: "Appears after the first scan." },
  });

  const decisions = panel({
    title: "Latest decisions", store, key: "decisions",
    actions: h("a", { href: "#/activity", class: "linkbtn" }, "Full log →"),
    render: (d) => {
      const list = (d?.decisions || []).slice(0, 6);
      if (!list.length) return null;
      return h("ul", { class: "dec-mini" }, list.map((x) => h("li", null,
        decisionChip(x.passed),
        h("span", { class: "dm-main" }, h("strong", { class: "sym" }, x.symbol ?? "—"), " ", h("span", { class: "small" }, strategyInfo(x.strategy).label),
          h("span", { class: "muted small dm-reason" }, (x.reasons || [])[0] ? String(x.reasons[0]) : "")),
        h("time", { class: "muted small num" }, timeNY(x.t)))));
    },
    empty: { title: "No decisions yet", hint: "Every idea the system considers is logged here with PASS or BLOCK and the reasons." },
  });

  const grid = h("div", { class: "grid" }, top, positions, equity, regime, decisions);
  stagger([hero, ...grid.children], 0, 50);
  root.append(hero, grid);
}

// Shared app context and UI pieces used by several screens.
import { h, clear, term, label, toast, announce, decisionChip, flagChip, signed, stagger, emptyState } from "./dom.js";
import { api, isDemo } from "./api.js";
import { store } from "./store.js";
import { money, signedMoney, price, pct, isNum, ymd, timeNY, relTime, num, parseTime, dateTimeNY } from "./format.js";
import { strategyInfo, MODES } from "./glossary.js";
import { payoffChart } from "./charts.js";

// ---------------------------------------------------------------- server clock
let clockOffset = 0;
export function syncClock(nowIso) {
  const t = parseTime(nowIso);
  if (t != null) clockOffset = t - Date.now();
}
export const serverNow = () => Date.now() + clockOffset;

export const status = () => store.data("status");
export const isPracticeMode = () => { const st = status(); return !!(st && st.running && MODES[st.mode]?.practice); };

// ---------------------------------------------------------------- scanning
export async function startScan(symbols = null) {
  try {
    await api.scan(symbols);
    toast("Scan started — this looks only; it never places orders.");
    announce("Scan started");
    store.refresh("scan");
  } catch (e) {
    toast(`Couldn't start the scan: ${e.message}`, "error");
  }
}
export const scanRunning = () => store.data("scan")?.state === "running";

/** "Scan now" button + symbol-by-symbol sweep. */
export function scanControl({ compact = false } = {}) {
  const btn = h("button", { type: "button", class: "btn primary scan-btn", onclick: () => startScan() },
    h("span", { class: "scan-ico", "aria-hidden": "true" }), h("span", { class: "scan-txt" }, "Scan now"));
  const sweep = h("ol", { class: "sweep", "aria-label": "Scan progress by symbol" });
  const meta = h("p", { class: "scan-meta muted small", "aria-live": "polite" });
  const el = h("div", { class: `scan-ctl cu ${compact ? "compact" : ""}` }, h("div", { class: "scan-row" }, btn, meta), sweep);
  const draw = () => {
    const sc = store.data("scan");
    const st = status();
    const running = sc?.state === "running";
    btn.disabled = running;
    btn.classList.toggle("busy", running);
    btn.lastChild.textContent = running ? "Scanning…" : "Scan now";
    const prog = Array.isArray(sc?.progress) ? sc.progress : [];
    const done = prog.filter((p) => p.state === "done" || p.state === "error").length;
    if (running) meta.textContent = `${done} of ${prog.length} symbols checked`;
    else if (sc?.state === "error") meta.textContent = "The last scan failed. Try again.";
    else meta.textContent = st?.last_scan ? `Last scan ${relTime(st.last_scan, serverNow())} (${timeNY(st.last_scan)} NY)` : "No scan yet today";
    if (!prog.length || (!running && compact)) { clear(sweep); sweep.hidden = true; return; }
    sweep.hidden = false;
    // reuse nodes so state changes animate rather than re-enter
    if (sweep.children.length !== prog.length) {
      clear(sweep);
      prog.forEach((p) => sweep.appendChild(h("li", { class: "sw" }, h("span", { class: "sw-sym" }), h("span", { class: "sw-ico", "aria-hidden": "true" }))));
    }
    prog.forEach((p, i) => {
      const li = sweep.children[i];
      li.className = `sw ${p.state || "queued"}`;
      li.firstChild.textContent = p.symbol ?? "—";
      li.lastChild.textContent = p.state === "done" ? "✓" : p.state === "error" ? "!" : p.state === "running" ? "•" : "";
      li.title = p.message || p.state || "";
      li.setAttribute("aria-label", `${p.symbol}: ${p.state}${p.message ? ", " + p.message : ""}`);
    });
  };
  const u1 = store.subscribe("scan", draw), u2 = store.subscribe("status", draw);
  el._cleanup = () => { u1(); u2(); };
  draw();
  return el;
}

// ---------------------------------------------------------------- notes
export function delayNote() {
  const st = status();
  const txt = st?.data_delay_note || "Quotes may be delayed.";
  return h("p", { class: "delay-note" }, h("span", { class: "dn-ico", "aria-hidden": "true" }, "⏱"), " ", term("data_delay", "Data:"), " ", txt);
}

export function strategyTag(key, apiLabel, { withWhat = false } = {}) {
  const info = strategyInfo(key, apiLabel);
  return h("span", { class: "strat" },
    h("span", { class: "strat-label" }, info.label),
    withWhat && info.what ? h("span", { class: "strat-what" }, info.what) : null);
}

// ---------------------------------------------------------------- idea card
const sideWord = (s) => (s === "buy" ? "Buy" : s === "sell" ? "Sell" : "—");

export function legsTable(legs) {
  const rows = (legs || []).map((l) => h("tr", null,
    h("td", null, h("span", { class: `side ${l.side}` }, sideWord(l.side)), l.ratio > 1 ? ` ×${l.ratio}` : ""),
    h("td", { class: "num" }, isNum(l.strike) ? num(l.strike, l.strike % 1 ? 1 : 0) : "—", " ", l.right === "C" ? "call" : l.right === "P" ? "put" : ""),
    h("td", null, ymd(l.expiry)),
    h("td", { class: "num" }, price(l.bid), " / ", price(l.ask)),
    h("td", null, flagChip(l.flag) || h("span", { class: "muted" }, "—"))));
  return h("div", { class: "tbl-wrap" }, h("table", { class: "tbl compact legs" },
    h("caption", { class: "sr-only" }, "Option legs"),
    h("thead", null, h("tr", null, h("th", { scope: "col" }, "Action"), h("th", { scope: "col" }, "Option"), h("th", { scope: "col" }, "Expires"), h("th", { scope: "col", class: "num" }, "Bid / ask"), h("th", { scope: "col" }, "Price check"))),
    h("tbody", null, rows)));
}

export function ideaCard(idea, { compact = false, rank = null, animate = true } = {}) {
  const info = strategyInfo(idea.strategy, idea.strategy_label);
  const d = idea.decision;
  const credit = isNum(idea.net_price) && idea.net_price < 0;
  const stats = h("dl", { class: "stats" },
    stat(label("Chance of profit", "pop", "POP"), pct(idea.pop, 0), null, "pop"),
    stat(label("Expected value", "ev", "EV"), signedMoney(idea.expected_value), idea.expected_value, "ev"),
    stat(term("max_loss", "Max loss"), money(idea.max_loss), null),
    stat(term("max_profit", "Max profit"), idea.max_profit == null ? "Unlimited" : money(idea.max_profit), null));
  const head = h("header", { class: "idea-head" },
    rank != null ? h("span", { class: "rank", "aria-label": `Rank ${rank}` }, String(rank).padStart(2, "0")) : null,
    h("div", { class: "idea-title" },
      h("h3", null, h("span", { class: "sym" }, idea.symbol ?? "—"), " ", h("span", { class: "strat-label" }, info.label)),
      info.what ? h("p", { class: "strat-what" }, info.what) : null),
    decisionChip(d ? d.passed : null));
  const priceLine = h("p", { class: "price-line small" },
    credit ? "Collect " : "Pay ", h("strong", { class: "num" }, money(Math.abs(idea.net_price))), " per share at mid · ",
    term("entry_limit", "limit"), " ", h("span", { class: "num" }, price(idea.entry_limit)), " · ",
    term("contracts", "contracts"), " ", h("strong", { class: "num" }, isNum(idea.contracts) ? String(idea.contracts) : "—"),
    isNum(idea.contracts) && idea.contracts > 0 && isNum(idea.max_loss) ? h("span", { class: "muted" }, ` (worst case ${money(idea.max_loss * idea.contracts, { cents: false })})`) : null,
    idea.intraday ? h("span", { class: "pill" }, "day trade") : null);
  const card = h("article", { class: `card idea ${d ? (d.passed ? "is-pass" : "is-block") : ""}` }, head, stats);
  card.appendChild(payoffChart(idea, { height: compact ? 120 : 170, animate }));
  card.appendChild(h("p", { class: "be-line small muted" }, term("breakeven", "Breakeven"), ": ",
    (idea.breakevens || []).filter(isNum).map((b) => price(b)).join(" and ") || "—",
    " · stock now ", isNum(idea.spot) ? price(idea.spot) : "—"));
  card.appendChild(priceLine);
  if (!compact) {
    card.appendChild(h("h4", null, "Why the system likes it"));
    card.appendChild(h("ul", { class: "rationale" }, (idea.rationale || []).map((r) => h("li", null, String(r)))));
    card.appendChild(h("h4", null, "The legs"));
    card.appendChild(legsTable(idea.legs));
    const g = idea.greeks || {};
    card.appendChild(h("p", { class: "greeks small muted" },
      term("delta", "Delta"), " ", num(g.delta, 1), " · ", term("theta", "Theta"), " ", signedMoney(g.theta), "/day · ",
      term("vega", "Vega"), " ", signedMoney(g.vega), " · ", term("score", "Score"), " ", num(idea.score, 3)));
  }
  card.appendChild(decisionBlock(d, compact));
  return card;
}

function stat(lbl, value, signedV, key) {
  return h("div", { class: `stat ${key || ""}` }, h("dt", null, lbl),
    h("dd", { class: `num ${isNum(signedV) ? (signedV > 0 ? "gain" : signedV < 0 ? "loss" : "") : ""}` }, value));
}

export function decisionBlock(d, compact) {
  if (!d) return h("div", { class: "decision none small muted" }, "Not checked by the risk gate yet.");
  const reasons = Array.isArray(d.reasons) ? d.reasons : [];
  return h("div", { class: `decision ${d.passed ? "pass" : "block"}` },
    h("p", { class: "decision-title" }, d.passed ? "Risk gate: allowed" : "Risk gate: blocked"),
    h("ul", null, (compact ? reasons.slice(0, 1) : reasons).map((r) => h("li", null, String(r)))));
}

// ---------------------------------------------------------------- position progress
export function progressPair(p) {
  const t = isNum(p.progress_to_target) ? Math.max(0, Math.min(1, p.progress_to_target)) : 0;
  const s = isNum(p.progress_to_stop) ? Math.max(0, Math.min(1, p.progress_to_stop)) : 0;
  const bar = (v, cls) => { const f = h("span", { class: "pp-fill" }); f.style.transform = `scaleX(${v})`; return h("span", { class: `pp ${cls}` }, f); };
  return h("div", { class: "pp-pair", role: "img", "aria-label": `${Math.round(t * 100)}% of the way to the profit target, ${Math.round(s * 100)}% of the way to the stop-loss` },
    h("span", { class: "pp-lbl small" }, "Stop"), bar(s, "stop"), h("span", { class: "pp-mid", "aria-hidden": "true" }), bar(t, "target"), h("span", { class: "pp-lbl small" }, "Target"));
}

export function modeBadge(st) {
  const mode = st?.running ? st.mode : "idle";
  const m = MODES[mode] || { label: String(mode || "—").toUpperCase(), desc: "", practice: false };
  return h("span", { class: `mode-badge mode-${mode} ${m.practice ? "practice" : ""}`, "data-tip": m.desc },
    h("span", { class: "mb-dot", "aria-hidden": "true" }), m.label,
    m.practice ? h("span", { class: "mb-sub" }, "practice money") : null);
}

export function emptyIdeas() {
  return { title: "No trade ideas yet", hint: "Run a scan to see opportunities. A scan only looks — it never places orders.", action: h("button", { type: "button", class: "btn primary small", onclick: () => startScan() }, "Scan now") };
}

export { stagger, emptyState, signed, dateTimeNY, isDemo };

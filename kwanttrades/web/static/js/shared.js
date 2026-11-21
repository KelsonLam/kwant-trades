// Shared app context and UI pieces used by several screens.
import { h, put, clear, term, label, toast, announce, flagChip, signed, stagger, emptyState, decisionChip } from "./dom.js";
import { api, isDemo } from "./api.js";
import { store } from "./store.js";
import { money, signedMoney, price, pct, isNum, ymd, timeNY, relTime, num, parseTime, dateTimeNY } from "./format.js";
import { strategyInfo, MODES } from "./glossary.js";
import { payoffChart } from "./charts.js";
import { icon, group, row, openSheet, spinner } from "./ui.js";

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
    toast("Scan started. A scan only looks and never places orders.");
    announce("Scan started");
    store.refresh("scan");
  } catch (e) {
    toast(`Couldn't start the scan: ${e.message}`, "error");
  }
}
export const scanRunning = () => store.data("scan")?.state === "running";

function scanCounts() {
  const prog = Array.isArray(store.data("scan")?.progress) ? store.data("scan").progress : [];
  return { done: prog.filter((p) => p.state === "done" || p.state === "error").length, total: prog.length };
}

/** Nav-bar "Scan" button with a progress state. */
export function scanButton() {
  const ico = h("span", { class: "nb-ico" }, icon("refresh", { size: 18 }));
  const txt = h("span", { class: "nb-btn-txt" }, "Scan");
  const btn = h("button", { type: "button", class: "nb-btn scan-btn cu", onclick: () => startScan() }, ico, txt);
  const draw = () => {
    const run = scanRunning();
    btn.disabled = run;
    btn.classList.toggle("busy", run);
    const { done, total } = scanCounts();
    txt.textContent = run ? (total ? `${done}/${total}` : "Scanning") : "Scan";
    btn.setAttribute("aria-label", run ? `Scanning, ${done} of ${total} symbols checked` : "Scan for new trade ideas");
  };
  const u1 = store.subscribe("scan", draw);
  btn._cleanup = u1;
  draw();
  return btn;
}

/** Progress line under the Ideas title: bar while scanning, "last scan" otherwise. */
export function scanStatusLine() {
  const text = h("span", { class: "scan-line-txt", "aria-live": "polite" });
  const fill = h("span", { class: "bar-fill" });
  const bar = h("span", { class: "bar", "aria-hidden": "true" }, fill);
  const el = h("div", { class: "scan-line cu" }, text, bar);
  const draw = () => {
    const sc = store.data("scan"), st = status();
    const run = sc?.state === "running";
    const { done, total } = scanCounts();
    bar.hidden = !run;
    el.classList.toggle("running", run);
    if (run) {
      text.textContent = `Checking ${done} of ${total || "…"} symbols…`;
      fill.style.transform = `scaleX(${total ? done / total : 0.05})`;
    } else if (sc?.state === "error") text.textContent = "The last scan failed. Try again.";
    else text.textContent = st?.last_scan ? `Last scan ${relTime(st.last_scan, serverNow())} · ${timeNY(st.last_scan)} NY` : "No scan yet today";
  };
  const u1 = store.subscribe("scan", draw), u2 = store.subscribe("status", draw);
  el._cleanup = () => { u1(); u2(); };
  draw();
  return el;
}

// ---------------------------------------------------------------- notes
/** Data-delay note, plus a slim "market closed" banner on screens that show quotes. Reacts to status changes. */
export function quoteNote({ banner = true } = {}) {
  const el = h("div", { class: "quote-note cu" });
  const draw = () => {
    const st = status();
    const txt = st?.data_delay_note || "Quotes may be delayed.";
    const closed = st?.market?.state === "closed";
    el.replaceChildren(...[
      banner && closed ? h("p", { class: "inline-banner", role: "note" }, icon("clock", { size: 16 }), h("span", null, "Market closed. Prices shown are from the last session.")) : null,
      h("p", { class: "delay-note" }, term("data_delay", "Data:"), " ", txt)].filter(Boolean));
  };
  el._cleanup = store.subscribe("status", draw);
  draw();
  return el;
}
export const delayNote = quoteNote;

// ---------------------------------------------------------------- plain-language idea summaries
const BULL = new Set(["long_call", "bull_call_spread", "bull_put_spread"]);
const BEAR = new Set(["long_put", "bear_put_spread", "bear_call_spread"]);

export function ideaHeadline(i) {
  const sym = i.symbol ?? "this stock";
  if (BULL.has(i.strategy)) return `Bet ${sym} rises`;
  if (BEAR.has(i.strategy)) return `Bet ${sym} falls`;
  if (i.strategy === "iron_condor") return `Bet ${sym} stays in a range`;
  if (i.strategy === "long_straddle" || i.strategy === "long_strangle") return `Bet ${sym} makes a big move`;
  return `Trade ${sym}`;
}

export function ideaSummary(i) {
  const risk = isNum(i.max_loss) ? money(i.max_loss, { cents: false }) : "–";
  const gain = i.max_profit == null ? "for unlimited upside" : `to make up to ${money(i.max_profit, { cents: false })}`;
  return `${ideaHeadline(i)} · risk ${risk} ${gain}`;
}

export function gateChip(d) {
  if (!d) return h("span", { class: "chip neutral" }, "Not checked");
  return decisionChip(d.passed);
}

/** One idea as a list row. onOpen(idea) opens its sheet. */
export function ideaRow(idea, { onOpen = null, rank = null } = {}) {
  const pop = isNum(idea.pop) ? pct(idea.pop, 0) : "–";
  return row({
    title: h("span", { class: "idea-title-line" }, rank != null ? h("span", { class: "rank-n" }, String(rank)) : null, h("span", { class: "sym" }, idea.symbol ?? "–"), gateChip(idea.decision)),
    sub: ideaSummary(idea),
    trailing: h("span", { class: "pop" }, h("strong", { class: "num" }, pop), h("span", { class: "pop-lbl" }, "chance")),
    chevron: true,
    onclick: () => (onOpen || openIdeaSheet)(idea),
    label: `${ideaSummary(idea)}. Chance of profit ${pop}. ${idea.decision ? (idea.decision.passed ? "Allowed" : "Blocked") : "Not checked"}. Open details.`,
    haspopup: "dialog",
    cls: "idea-row",
  });
}

function tile(lbl, value, v) {
  return h("div", { class: "tile" }, h("dt", null, lbl), h("dd", { class: `num ${isNum(v) ? (v > 0 ? "gain" : v < 0 ? "loss" : "") : ""}` }, value));
}

const sideWord = (s) => (s === "buy" ? "Buy" : s === "sell" ? "Sell" : "–");

/** Option legs as list rows (phone friendly). */
export function legsGroup(legs, { header = "The legs" } = {}) {
  const rows = (legs || []).map((l) => row({
    title: `${sideWord(l.side)}${l.ratio > 1 ? ` ×${l.ratio}` : ""} · ${isNum(l.strike) ? num(l.strike, l.strike % 1 ? 1 : 0) : "–"} ${l.right === "C" ? "call" : l.right === "P" ? "put" : ""}`,
    sub: `Expires ${ymd(l.expiry)} · bid ${price(l.bid)} / ask ${price(l.ask)}`,
    trailing: flagChip(l.flag),
  }));
  return group(rows, { header });
}

export function decisionBlock(d) {
  if (!d) return h("div", { class: "decision none" }, h("p", { class: "decision-title" }, "Not checked by the risk gate yet."));
  const reasons = Array.isArray(d.reasons) ? d.reasons : [];
  return h("div", { class: `decision ${d.passed ? "pass" : "block"}` },
    h("p", { class: "decision-title" }, h("span", { "aria-hidden": "true" }, d.passed ? "✓ " : "✕ "), d.passed ? "Risk check: allowed" : "Risk check: blocked"),
    reasons.length ? h("ul", null, reasons.map((r) => h("li", null, String(r)))) : null);
}

/** Bottom sheet with everything about one idea. Technical numbers sit behind a collapsed "Details". */
export function openIdeaSheet(idea) {
  const info = strategyInfo(idea.strategy, idea.strategy_label);
  return openSheet({
    title: idea.symbol ?? "Idea",
    build: (body) => {
      const credit = isNum(idea.net_price) && idea.net_price < 0;
      const g = idea.greeks || {};
      put(body,
        h("div", { class: "sheet-hero" },
          h("p", { class: "sheet-big" }, ideaHeadline(idea)),
          h("p", { class: "muted" }, info.label), info.what ? h("p", { class: "muted small" }, info.what) : null,
          h("div", { class: "sheet-chip" }, gateChip(idea.decision))),
        h("dl", { class: "tiles" },
          tile(label("Chance of profit", "pop", "POP"), pct(idea.pop, 0)),
          tile(label("Expected value", "ev", "EV"), signedMoney(idea.expected_value), idea.expected_value),
          tile(term("max_loss", "Most you can lose"), money(idea.max_loss, { cents: false })),
          tile(term("max_profit", "Most you can make"), idea.max_profit == null ? "Unlimited" : money(idea.max_profit, { cents: false }))),
        h("div", { class: "sheet-card" }, payoffChart(idea, { height: 170, animate: true }),
          h("p", { class: "small muted" }, "Profit or loss at expiry for each stock price. ", term("breakeven", "Breakeven"), ": ",
            (idea.breakevens || []).filter(isNum).map((b) => price(b)).join(" and ") || "–", " · stock now ", isNum(idea.spot) ? price(idea.spot) : "–")),
        h("p", { class: "price-line" }, credit ? "Collect " : "Pay ", h("strong", { class: "num" }, money(Math.abs(idea.net_price))), " per share at mid · ",
          term("entry_limit", "limit"), " ", h("span", { class: "num" }, price(idea.entry_limit)), " · ",
          term("contracts", "contracts"), " ", h("strong", { class: "num" }, isNum(idea.contracts) ? String(idea.contracts) : "–"),
          isNum(idea.contracts) && idea.contracts > 0 && isNum(idea.max_loss) ? h("span", { class: "muted" }, ` (worst case ${money(idea.max_loss * idea.contracts, { cents: false })})`) : null,
          idea.intraday ? h("span", { class: "pill" }, "day trade") : null),
        (idea.rationale || []).length ? group([...(idea.rationale || []).map((r) => row({ title: h("span", { class: "wrap" }, String(r)) }))], { header: "Why the system likes it" }) : null,
        decisionBlock(idea.decision),
        legsGroup(idea.legs),
        h("details", { class: "disclosure" }, h("summary", null, "Details"),
          h("dl", { class: "kv" },
            h("dt", null, term("delta", "Delta")), h("dd", { class: "num" }, num(g.delta, 1)),
            h("dt", null, term("theta", "Theta")), h("dd", { class: "num" }, `${signedMoney(g.theta)}/day`),
            h("dt", null, term("vega", "Vega")), h("dd", { class: "num" }, signedMoney(g.vega)),
            h("dt", null, term("score", "Score")), h("dd", { class: "num" }, num(idea.score, 3)),
            h("dt", null, "Net price per share"), h("dd", { class: "num" }, price(idea.net_price)))),
        quoteNote({ banner: false }),
      );
      return () => body.querySelectorAll(".cu").forEach((n) => n._cleanup && n._cleanup());
    },
  });
}

// ---------------------------------------------------------------- position progress
export function progressPair(p) {
  const t = isNum(p.progress_to_target) ? Math.max(0, Math.min(1, p.progress_to_target)) : 0;
  const s = isNum(p.progress_to_stop) ? Math.max(0, Math.min(1, p.progress_to_stop)) : 0;
  const bar = (v, cls) => { const f = h("span", { class: "pp-fill" }); f.style.transform = `scaleX(${v})`; return h("span", { class: `pp ${cls}` }, f); };
  return h("div", { class: "pp-pair", role: "img", "aria-label": `${Math.round(t * 100)}% of the way to the profit target, ${Math.round(s * 100)}% of the way to the stop-loss` },
    h("span", { class: "pp-lbl" }, "Stop"), bar(s, "stop"), h("span", { class: "pp-mid", "aria-hidden": "true" }), bar(t, "target"), h("span", { class: "pp-lbl" }, "Target"));
}

export function modeBadge(st) {
  const mode = st?.running ? st.mode : "idle";
  const m = MODES[mode] || { label: String(mode || "–").toUpperCase(), desc: "", practice: false };
  return h("span", { class: `mode-badge mode-${mode} ${m.practice ? "practice" : ""}`, "data-tip": m.desc, title: m.desc },
    h("span", { class: "mb-dot", "aria-hidden": "true" }), m.label);
}

export function emptyIdeas() {
  return { title: "No trade ideas yet", hint: "Run a scan to see opportunities. A scan only looks and never places orders.", action: h("button", { type: "button", class: "btn primary small", onclick: () => startScan() }, "Scan now") };
}

export { stagger, emptyState, signed, dateTimeNY, isDemo, spinner };

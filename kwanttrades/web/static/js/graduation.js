// Road to Alpaca: the compact Home card and the full checklist sheet (graduation from the built-in paper simulator to
// the Alpaca PAPER account). All API strings go in as text nodes. Live trading is never offered here.
import { h } from "./dom.js";
import { store } from "./store.js";
import { dateNY, timeNY, isNum } from "./format.js";
import { ring } from "./charts.js";
import { group, row, icon, openSheet, bar } from "./ui.js";

const pctTxt = (f) => `${Math.round((isNum(f) ? f : 0) * 100)}%`;

function progressRing(frac, label) {
  return h("span", { class: "ring-wrap" }, ring(isNum(frac) ? frac : 0, { size: 44, stroke: 4, label }),
    h("span", { class: "ring-num num", "aria-hidden": "true" }, pctTxt(frac)));
}

function critIcon(pass) {
  return h("span", { class: `crit-ico ${pass ? "pass" : ""}` }, icon(pass ? "check" : "clock", { size: 16 }),
    h("span", { class: "sr-only" }, pass ? "Passed" : "Not yet"));
}

function critRow(c) {
  return h("li", { class: "row" }, h("div", { class: "row-main static stacked" },
    h("div", { class: "risk-line" },
      h("span", { class: "head-with" }, critIcon(c.pass), h("span", { class: "cell-title" }, String(c.label ?? ""))),
      h("span", { class: "num muted small" }, `${c.display_value ?? "–"} · need ${c.display_target ?? "–"}`)),
    bar(c.progress, { level: "ok", label: `${c.label}: ${pctTxt(c.progress)} of the way` }),
    c.plain ? h("p", { class: "cell-sub" }, String(c.plain)) : null));
}

function furthest(criteria, n = 3) {
  return (criteria || []).filter((c) => !c.pass).sort((a, b) => (a.progress ?? 0) - (b.progress ?? 0)).slice(0, n);
}

/** Compact Home card: ring + the criteria furthest from passing. Tap opens the full checklist. */
export function roadCard() {
  const host = h("div", { class: "group-wrap cu" });
  const draw = () => {
    const g = store.data("graduation");
    if (!g) { host.replaceChildren(); return; }
    const onAlpaca = g.stage === "alpaca_paper";
    let title, sub, frac, label;
    if (onAlpaca) {
      const lr = g.live_readiness || {};
      frac = lr.progress;
      title = "Trading on Alpaca paper";
      sub = h("span", { class: "sub-stack" }, h("span", null, "Practice money in your Alpaca account. Live trading stays your decision."),
        h("span", null, `Live-readiness checks: ${pctTxt(frac)}`));
      label = `Live trading readiness ${pctTxt(frac)}. Read only.`;
    } else {
      frac = g.progress;
      title = "Road to Alpaca";
      const lines = furthest(g.criteria).map((c) => h("span", { class: "road-line" },
        h("span", { class: "risk-line" }, h("span", null, String(c.label ?? "")), h("span", { class: "num" }, `${c.display_value ?? "–"} / ${c.display_target ?? "–"}`)),
        bar(c.progress, { level: "ok", label: `${c.label}: ${pctTxt(c.progress)} of the way` })));
      if (g.ready) {
        sub = h("span", null, g.needs?.length ? "Every check passes. One step is left for you." : "Every check passes. Moving to Alpaca paper…");
      } else {
        sub = h("span", { class: "road-lines" }, ...lines);
      }
      label = `Road to Alpaca: ${pctTxt(frac)} of the way. Open the checklist.`;
    }
    const list = h("ul", { class: "group", "aria-label": "Road to Alpaca" },
      row({ leading: progressRing(frac, `${pctTxt(frac)} of the way`), title, sub, chevron: true, onclick: () => openGraduationSheet(), haspopup: "dialog", label }));
    const alert = g.alert ? h("p", { class: "inline-banner", role: "status" }, icon("warn", { size: 16 }), h("span", null, String(g.alert))) : null;
    host.replaceChildren(list, ...(alert ? [h("div", { class: "group-foot" }, alert)] : []));
  };
  host._cleanup = store.subscribe("graduation", draw);
  draw();
  return host;
}

function sheetContent(g) {
  if (!g) return [h("p", { class: "muted" }, "Waiting for data…")];
  const onAlpaca = g.stage === "alpaca_paper";
  const out = [
    h("p", { class: "muted sheet-lead" }, onAlpaca
      ? "Trading now goes to your Alpaca paper (practice) account. No real money is used."
      : "The system practises with the built-in simulator. When every check below passes, including enough fresh trades since its last learning change, the running session moves to your Alpaca paper (practice) account by itself. It never switches to real money."),
  ];
  if (g.alert) out.push(h("p", { class: "inline-banner", role: "status" }, icon("warn", { size: 16 }), h("span", null, String(g.alert))));
  if (g.needs?.length) out.push(group(g.needs.map((n) => row({ leading: icon("flag", { size: 20 }), title: h("span", { class: "wrap" }, String(n)) })), { header: "What's left for you" }));
  out.push(group((g.criteria || []).map(critRow), { header: onAlpaca ? "Built-in simulator record" : "Checklist", footer: `${pctTxt(g.progress)} of the way there.` }));
  const oos = g.out_of_sample || {};
  if ((oos.criteria || []).length) {
    out.push(group(oos.criteria.map(critRow), {
      header: `Fresh trades since the last change (${oos.trades ?? 0} of ${oos.required ?? "–"})`,
      footer: oos.since ? `Counting trades opened after ${dateNY(oos.since)} ${timeNY(oos.since)} NY, when the system last changed a setting.` : "No learning change yet, so every trade counts.",
    }));
  }
  const ev = g.last_event;
  if (ev) out.push(h("p", { class: "small muted" }, `Last stage change, ${dateNY(ev.ts)} ${timeNY(ev.ts)} NY: `, String(ev.message ?? "")));
  const lr = g.live_readiness;
  if (onAlpaca && lr) {
    out.push(group((lr.criteria || []).map(critRow), { header: "Live trading readiness (read only)", footer: String(lr.note ?? "") }));
    out.push(h("p", { class: "small muted" }, String(lr.how ?? "")));
  }
  return out;
}

export function openGraduationSheet() {
  return openSheet({
    title: "Road to Alpaca",
    build: (body) => {
      const host = h("div");
      body.append(host);
      const draw = () => { host.replaceChildren(...sheetContent(store.data("graduation"))); };
      draw();
      store.refresh("graduation");
      return store.subscribe("graduation", draw);
    },
  });
}

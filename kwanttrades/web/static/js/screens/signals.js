import { h, panel, term, stagger, signed } from "../dom.js";
import { store } from "../store.js";
import { price, pct, signedPct, isNum, timeNY, num } from "../format.js";
import { sparkline, orbChart } from "../charts.js";
import { COMPONENTS, ORB_STATES } from "../glossary.js";
import { delayNote } from "../shared.js";

export function directionBar(dir, conf, { size = "" } = {}) {
  const d = isNum(dir) ? Math.max(-1, Math.min(1, dir)) : 0;
  const fill = h("span", { class: `dir-fill ${d >= 0 ? "pos" : "neg"}` });
  fill.style.transform = `scaleX(${Math.abs(d)})`;
  fill.style.opacity = String(0.45 + 0.55 * (isNum(conf) ? conf : 0.5));
  const word = d > 0.25 ? "Bullish" : d > 0.05 ? "Slightly bullish" : d < -0.25 ? "Bearish" : d < -0.05 ? "Slightly bearish" : "Neutral";
  return h("div", { class: `dir ${size}`, role: "img", "aria-label": `${word}: direction ${d.toFixed(2)}, confidence ${pct(conf, 0)}` },
    h("div", { class: "dir-track" }, h("span", { class: "dir-zero" }), fill),
    h("div", { class: "dir-scale small muted", "aria-hidden": "true" }, h("span", null, "← bearish"), h("span", null, "0"), h("span", null, "bullish →")),
    h("p", { class: "dir-word" }, h("strong", null, word), " ", h("span", { class: "muted small" }, `${signedNumber(d)} · ${pct(conf, 0)} `, term("confidence", "confidence"))));
}
const signedNumber = (d) => (d > 0 ? "+" : d < 0 ? "−" : "") + Math.abs(d).toFixed(2);

function compChip(c, comp) {
  if (!comp) return h("span", { class: "comp none", "data-term": c.term, tabindex: "0" }, c.name, h("span", { class: "comp-v" }, "—"));
  const d = comp.direction;
  const cls = !isNum(d) || Math.abs(d) < 0.05 ? "flat" : d > 0 ? "gain" : "loss";
  return h("span", { class: `comp ${cls}`, "data-term": c.term, tabindex: "0" },
    c.name, h("span", { class: "comp-v" }, h("span", { "aria-hidden": "true" }, cls === "gain" ? "▲" : cls === "loss" ? "▼" : "◆"), " ", isNum(d) ? signedNumber(d) : "—"),
    h("span", { class: "comp-c", style: { "--c": isNum(comp.confidence) ? comp.confidence : 0 } }));
}

function volView(v) {
  if (!v) return h("p", { class: "muted small" }, "No volatility view yet.");
  const max = Math.max(v.atm_iv || 0, v.forecast_rv || 0, 0.01) * 1.15;
  const bar = (val, cls) => { const f = h("span", { class: `vb-fill ${cls}` }); f.style.transform = `scaleX(${isNum(val) ? val / max : 0})`; return h("span", { class: "vb" }, f); };
  const e = v.vol_edge;
  const verdict = !isNum(e) ? "—" : e > 0.1 ? "Options look cheap" : e < -0.1 ? "Options look expensive" : "Options look fairly priced";
  return h("div", { class: "volview" },
    h("div", { class: "vb-row" }, h("span", { class: "small" }, term("iv", "Implied")), bar(v.atm_iv, "iv"), h("span", { class: "num small" }, pct(v.atm_iv))),
    h("div", { class: "vb-row" }, h("span", { class: "small" }, term("rv", "Forecast")), bar(v.forecast_rv, "rv"), h("span", { class: "num small" }, pct(v.forecast_rv))),
    h("p", { class: "small" }, h("strong", null, verdict), " · ", term("vol_edge", "vol edge"), " ", h("span", { class: "num" }, signedPct(e, 0))));
}

function symbolCard(s, first) {
  const orb = s.orb;
  const orbState = orb?.state ? ORB_STATES[orb.state] || orb.state : "No opening range yet";
  return h("article", { class: "card sig" },
    h("header", { class: "sig-head" },
      h("div", null, h("h3", null, h("span", { class: "sym" }, s.symbol ?? "—")),
        h("p", { class: "num" }, price(s.spot), " ", signed(signedPct(s.change_pct, 2), s.change_pct, "small"))),
      sparkline(s.sparkline, { w: 128, hgt: 36, label: `${s.symbol} today` })),
    directionBar(s.composite?.direction, s.composite?.confidence),
    h("p", { class: "small muted" }, term("agreement", "Agreement"), " ", pct(s.composite?.agreement, 0)),
    h("div", { class: "comps" }, COMPONENTS.map((c) => compChip(c, s.components ? s.components[c.key] : null))),
    h("div", { class: "sig-split" },
      h("div", null,
        h("h4", null, term("opening_range", "Opening range"), " ", h("span", { class: `orb-state st-${orb?.state || "none"}` }, orbState)),
        orbChart(orb, s.sparkline, { height: 118, symbol: s.symbol }),
        orb && isNum(orb.rel_volume) ? h("p", { class: "small muted" }, term("rel_volume", "Relative volume"), ` ${num(orb.rel_volume, 2)}×`) : null),
      h("div", null, h("h4", null, "Volatility view"), volView(s.vol))));
}

export function render(root) {
  const head = h("section", { class: "panel span-3 toolbar" },
    h("h1", { class: "screen-title" }, "Signals"),
    h("p", { class: "muted" }, "What each day-trading signal says about direction, per symbol. The composite blends them; confidence says how much to trust it."),
    delayNote());
  const list = panel({
    title: "Symbols", cls: "span-3 bare", store, key: "signals",
    render: (d, { first }) => {
      const syms = d?.symbols || [];
      if (!syms.length) return null;
      const grid = h("div", { class: "card-grid two" }, syms.map((s) => symbolCard(s, first)));
      if (first) stagger([...grid.children], 0, 40);
      return h("div", null, h("p", { class: "muted small list-meta" }, d.asof ? `As of ${timeNY(d.asof)} NY` : ""), grid);
    },
    empty: { title: "No signals yet", hint: "Signals appear after the first scan or session step." },
  });
  const grid = h("div", { class: "grid" }, head, list);
  stagger([...grid.children], 0, 60);
  root.append(grid);
}

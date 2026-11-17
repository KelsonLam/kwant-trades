import { h, panel, stagger, emptyState } from "../dom.js";
import { store } from "../store.js";
import { timeNY } from "../format.js";
import { strategyInfo } from "../glossary.js";
import { ideaCard, scanControl, emptyIdeas, delayNote } from "../shared.js";

const filt = { symbol: "", strategy: "", passed: false, sort: "score" };

const SORTS = {
  score: { label: "Best ranked", fn: (a, b) => (b.score ?? -1e9) - (a.score ?? -1e9) },
  ev: { label: "Expected value", fn: (a, b) => (b.expected_value ?? -1e9) - (a.expected_value ?? -1e9) },
  pop: { label: "Chance of profit", fn: (a, b) => (b.pop ?? -1) - (a.pop ?? -1) },
  risk: { label: "Smallest max loss", fn: (a, b) => (a.max_loss ?? 1e12) - (b.max_loss ?? 1e12) },
};

export function render(root, params) {
  if (params[0]) filt.symbol = String(params[0]).toUpperCase();
  const symSel = h("select", { id: "f-sym" });
  const stratSel = h("select", { id: "f-strat" });
  const sortSel = h("select", { id: "f-sort" }, Object.entries(SORTS).map(([k, v]) => h("option", { value: k }, v.label)));
  const passed = h("input", { type: "checkbox", id: "f-pass" });
  sortSel.value = filt.sort; passed.checked = filt.passed;
  const bar = h("div", { class: "filters", role: "group", "aria-label": "Filter ideas" },
    h("label", { for: "f-sym" }, "Symbol", symSel),
    h("label", { for: "f-strat" }, "Strategy", stratSel),
    h("label", { for: "f-sort" }, "Sort by", sortSel),
    h("label", { class: "check", for: "f-pass" }, passed, h("span", null, "Only ideas the risk gate allowed")));

  let lastData = null;
  const list = panel({
    title: "Trade ideas", cls: "span-3 bare", store, key: "ideas",
    render: (d, { first }) => {
      lastData = d;
      const ideas = d?.ideas || [];
      fillOptions(symSel, [...new Set(ideas.map((i) => i.symbol).filter(Boolean))].sort(), filt.symbol, "All symbols", (x) => x);
      fillOptions(stratSel, [...new Set(ideas.map((i) => i.strategy).filter(Boolean))], filt.strategy, "All strategies", (x) => strategyInfo(x).label);
      if (!ideas.length) return null;
      const shown = ideas.filter((i) => (!filt.symbol || i.symbol === filt.symbol) && (!filt.strategy || i.strategy === filt.strategy) && (!filt.passed || i.decision?.passed))
        .sort(SORTS[filt.sort].fn);
      const nPass = ideas.filter((i) => i.decision?.passed).length;
      const head = h("p", { class: "muted small list-meta" }, `${shown.length} of ${ideas.length} ideas shown · ${nPass} allowed by the risk gate`, d.asof ? ` · scan at ${timeNY(d.asof)} NY` : "");
      if (!shown.length) return h("div", null, head, emptyState({ title: "No ideas match these filters", hint: "Clear a filter to see more." }));
      const grid = h("div", { class: "card-grid two" }, shown.map((i, k) => ideaCard(i, { rank: k + 1, animate: first })));
      if (first) stagger([...grid.children], 0, 70);
      return h("div", null, head, grid);
    },
    empty: emptyIdeas,
  });
  const redraw = () => list._redraw();
  symSel.addEventListener("change", () => { filt.symbol = symSel.value; redraw(); });
  stratSel.addEventListener("change", () => { filt.strategy = stratSel.value; redraw(); });
  sortSel.addEventListener("change", () => { filt.sort = sortSel.value; redraw(); });
  passed.addEventListener("change", () => { filt.passed = passed.checked; redraw(); });

  const top = h("section", { class: "panel span-3 toolbar" },
    h("div", { class: "toolbar-row" },
      h("div", null, h("h1", { class: "screen-title" }, "Opportunities"), h("p", { class: "muted" }, "Ranked trade ideas from the latest scan. Every idea has a known worst case.")),
      scanControl()),
    bar, delayNote());
  const grid = h("div", { class: "grid" }, top, list);
  stagger([...grid.children], 0, 60);
  root.append(grid);
}

function fillOptions(sel, values, current, allLabel, labelOf) {
  const want = ["", ...values];
  const have = [...sel.options].map((o) => o.value);
  if (want.join("|") === have.join("|")) { sel.value = current; return; }
  sel.replaceChildren(h("option", { value: "" }, allLabel), ...values.map((v) => h("option", { value: v }, labelOf(v))));
  if (current && !values.includes(current)) sel.appendChild(h("option", { value: current }, labelOf(current)));
  sel.value = current;
}

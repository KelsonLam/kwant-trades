// Ideas: the scan results as a clean list. Tap a row for the full details in a bottom sheet.
import { h, panel, stagger, emptyState, firstTime } from "../dom.js";
import { store } from "../store.js";
import { segmented, group } from "../ui.js";
import { ideaRow, scanButton, scanStatusLine, quoteNote, emptyIdeas } from "../shared.js";

export const meta = { title: "Ideas" };

const filt = { symbol: "", passed: false, sort: "score" };

const SORTS = {
  score: { label: "Best ranked", fn: (a, b) => (b.score ?? -1e9) - (a.score ?? -1e9) },
  ev: { label: "Expected value", fn: (a, b) => (b.expected_value ?? -1e9) - (a.expected_value ?? -1e9) },
  pop: { label: "Chance of profit", fn: (a, b) => (b.pop ?? -1) - (a.pop ?? -1) },
  risk: { label: "Smallest max loss", fn: (a, b) => (a.max_loss ?? 1e12) - (b.max_loss ?? 1e12) },
};

export function render(root, params, ctx) {
  filt.symbol = params[0] ? String(params[0]).toUpperCase() : "";
  ctx.trail.append(scanButton());

  let list;
  const redraw = () => list && list._redraw();
  const seg = segmented({ options: [["all", "All"], ["allowed", "Allowed"]], value: filt.passed ? "allowed" : "all", label: "Show ideas",
    onChange: (v) => { filt.passed = v === "allowed"; redraw(); } });
  const sortSel = h("select", { id: "f-sort", "aria-label": "Sort ideas by" }, Object.entries(SORTS).map(([k, v]) => h("option", { value: k }, v.label)));
  sortSel.value = filt.sort;
  sortSel.addEventListener("change", () => { filt.sort = sortSel.value; redraw(); });

  const first = firstTime("ideas");
  let staged = false;
  list = panel({
    title: "Trade ideas", id: "ideas-list", headless: true, store, key: "ideas",
    render: (d) => {
      const ideas = d?.ideas || [];
      if (!ideas.length) return null;
      const shown = ideas.filter((i) => (!filt.symbol || i.symbol === filt.symbol) && (!filt.passed || i.decision?.passed)).sort(SORTS[filt.sort].fn);
      const nPass = ideas.filter((i) => i.decision?.passed).length;
      const meta2 = h("div", { class: "list-meta" },
        h("span", null, `${shown.length} of ${ideas.length} ideas · ${nPass} allowed`),
        filt.symbol ? h("button", { type: "button", class: "chip-btn", onclick: () => { filt.symbol = ""; history.replaceState(null, "", "#/ideas"); redraw(); } }, `${filt.symbol} `, h("span", { "aria-hidden": "true" }, "✕"), h("span", { class: "sr-only" }, "Clear symbol filter")) : null);
      if (!shown.length) return h("div", null, meta2, emptyState({ title: filt.passed ? "Nothing was allowed by the risk gate" : "No ideas match", hint: filt.passed ? "Switch to All to see why ideas were blocked." : "Clear the symbol filter to see more." }));
      const g = group(shown.map((i, k) => ideaRow(i)), { label: "Trade ideas" });
      if (first && !staged) stagger([...g.querySelectorAll(".row")], 0, 40);
      staged = true;
      return h("div", null, meta2, g);
    },
    empty: emptyIdeas,
  });

  root.append(
    h("div", { class: "controls" }, seg, h("label", { class: "sort-pill", for: "f-sort" }, h("span", null, "Sort"), sortSel)),
    scanStatusLine(), quoteNote(), list,
    h("p", { class: "footer-note" }, "Ideas are model estimates. Every idea has a known worst case."));
}

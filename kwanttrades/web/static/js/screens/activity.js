import { h, panel, term, stagger, decisionChip, extLink, emptyState } from "../dom.js";
import { store } from "../store.js";
import { signedMoney, isNum, timeNY, dateNY, relTime, num, pct } from "../format.js";
import { strategyInfo } from "../glossary.js";
import { serverNow } from "../shared.js";

const f = { outcome: "all", symbol: "", q: "" };

function sentimentBar(v) {
  const d = isNum(v) ? Math.max(-1, Math.min(1, v)) : 0;
  const fill = h("span", { class: `sb-fill ${d >= 0 ? "pos" : "neg"}` });
  fill.style.transform = `scaleX(${Math.abs(d)})`;
  const word = !isNum(v) ? "Not scored" : d > 0.15 ? "Positive" : d < -0.15 ? "Negative" : "Neutral";
  return h("span", { class: "sb", role: "img", "aria-label": `Sentiment ${word}${isNum(v) ? " " + d.toFixed(2) : ""}` },
    h("span", { class: "sb-track" }, h("span", { class: "sb-zero" }), fill),
    h("span", { class: `sb-word small ${d > 0.15 ? "gain" : d < -0.15 ? "loss" : "muted"}` }, h("span", { "aria-hidden": "true" }, d > 0.15 ? "▲ " : d < -0.15 ? "▼ " : ""), word));
}

export function render(root) {
  const outcome = h("div", { class: "seg", role: "radiogroup", "aria-label": "Show decisions" });
  const opts = [["all", "All"], ["pass", "Allowed"], ["block", "Blocked"]];
  const symIn = h("input", { type: "search", id: "a-sym", placeholder: "e.g. NVDA", autocomplete: "off", maxlength: "8", value: f.symbol });
  const qIn = h("input", { type: "search", id: "a-q", placeholder: "Search reasons", autocomplete: "off", maxlength: "60", value: f.q });
  let dec;
  const redraw = () => dec && dec._redraw();
  opts.forEach(([v, l]) => {
    const b = h("button", { type: "button", role: "radio", "aria-checked": String(f.outcome === v), class: "seg-btn" }, l);
    b.addEventListener("click", () => { f.outcome = v; [...outcome.children].forEach((c) => c.setAttribute("aria-checked", String(c === b))); redraw(); });
    outcome.appendChild(b);
  });
  outcome.addEventListener("keydown", (e) => {
    if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
    const bs = [...outcome.children]; const i = bs.findIndex((b) => b.getAttribute("aria-checked") === "true");
    const n = bs[(i + (e.key === "ArrowRight" ? 1 : bs.length - 1)) % bs.length]; n.click(); n.focus();
  });
  symIn.addEventListener("input", () => { f.symbol = symIn.value.trim().toUpperCase(); redraw(); });
  qIn.addEventListener("input", () => { f.q = qIn.value.trim().toLowerCase(); redraw(); });

  dec = panel({
    title: "Decision log", cls: "span-2", store, key: "decisions",
    note: "Every idea the system considered, and whether the risk gate allowed it (PASS) or stopped it (BLOCK) — with the reasons.",
    render: (d) => {
      const all = d?.decisions || [];
      if (!all.length) return null;
      const list = all.filter((x) => (f.outcome === "all" || (f.outcome === "pass" ? x.passed : !x.passed))
        && (!f.symbol || String(x.symbol || "").includes(f.symbol))
        && (!f.q || (x.reasons || []).some((r) => String(r).toLowerCase().includes(f.q))));
      if (!list.length) return emptyState({ title: "No decisions match", hint: "Change the filters above." });
      return h("div", null,
        h("p", { class: "muted small list-meta" }, `${list.length} of ${all.length} decisions`),
        h("ol", { class: "dec-list" }, list.slice(0, 150).map((x) => h("li", { class: x.passed ? "pass" : "block" },
          h("div", { class: "dl-top" }, decisionChip(x.passed), h("strong", { class: "sym" }, x.symbol ?? "—"), h("span", { class: "small" }, strategyInfo(x.strategy).label),
            h("time", { class: "small muted num" }, dateNY(x.t), " ", timeNY(x.t))),
          h("ul", { class: "dl-reasons small" }, (x.reasons || []).map((r) => h("li", null, String(r)))),
          h("p", { class: "small muted num" }, `${isNum(x.contracts) ? x.contracts : "—"} contracts · EV ${signedMoney(x.expected_value)} · score ${num(x.score, 3)}`)))));
    },
    empty: { title: "No decisions yet", hint: "Run a scan or start a session; each idea considered is logged here." },
  });
  dec.querySelector(".panel-note").after(h("div", { class: "filters" }, outcome, h("label", { for: "a-sym" }, "Symbol", symIn), h("label", { for: "a-q" }, "Reason contains", qIn)));

  const newsP = panel({
    title: "News", store, key: "news",
    note: "Headlines from third-party feeds, scored for tone. They are inputs to the model, not advice.",
    render: (d) => {
      const items = (d?.items || []).slice().sort((a, b) => (Date.parse(b.published) || 0) - (Date.parse(a.published) || 0));
      if (!items.length) return null;
      const st = store.data("status");
      return h("div", null,
        st?.sentiment_engine ? h("p", { class: "small muted" }, "Scored by: ", "built-in finance word list") : null,
        h("ul", { class: "news" }, items.slice(0, 60).map((n) => h("li", null,
          h("p", { class: "news-h" }, extLink(n.url, String(n.headline ?? "Untitled"))),
          h("p", { class: "small muted" }, h("strong", { class: "sym" }, n.symbol ?? ""), " · ", String(n.source ?? "unknown source"), " · ", relTime(n.published, serverNow())),
          sentimentBar(n.sentiment),
          isNum(n.relevance) || isNum(n.materiality) ? h("p", { class: "small muted" }, `relevance ${pct(n.relevance, 0)} · materiality ${pct(n.materiality, 0)}`) : null))));
    },
    empty: { title: "No headlines yet", hint: "News is fetched during scans." },
  });

  const head = h("section", { class: "panel span-3 toolbar" }, h("h1", { class: "screen-title" }, "Activity"), h("p", { class: "muted" }, "What the system decided and the news it read."));
  const grid = h("div", { class: "grid" }, head, dec, newsP);
  stagger([...grid.children], 0, 60);
  root.append(grid);
}

// Activity timeline (decision log + news merged) used by Portfolio > Activity, and the full News page under More.
import { h, put, panel, stagger, decisionChip, extLink, emptyState, skeleton, term, firstTime } from "../dom.js";
import { store } from "../store.js";
import { signedMoney, isNum, timeNY, dateNY, relTime, num, pct, parseTime } from "../format.js";
import { strategyInfo } from "../glossary.js";
import { group, row, segmented, openSheet } from "../ui.js";
import { serverNow } from "../shared.js";

export const meta = { title: "News" };

const sentWord = (v) => (!isNum(v) ? "Not scored" : v > 0.15 ? "Positive" : v < -0.15 ? "Negative" : "Neutral");
const sentTone = (v) => (!isNum(v) ? "muted" : v > 0.15 ? "gain" : v < -0.15 ? "loss" : "muted");
const sentArrow = (v) => (!isNum(v) ? "" : v > 0.15 ? "▲ " : v < -0.15 ? "▼ " : "");

function sentimentBar(v) {
  const d = isNum(v) ? Math.max(-1, Math.min(1, v)) : 0;
  const fill = h("span", { class: `sb-fill ${d >= 0 ? "pos" : "neg"}` });
  fill.style.transform = `scaleX(${Math.abs(d)})`;
  return h("span", { class: "sb", role: "img", "aria-label": `Sentiment ${sentWord(v)}${isNum(v) ? " " + d.toFixed(2) : ""}` },
    h("span", { class: "sb-track" }, h("span", { class: "sb-zero" }), fill),
    h("span", { class: `sb-word small ${sentTone(v)}` }, h("span", { "aria-hidden": "true" }, sentArrow(v)), sentWord(v)));
}

function openDecisionSheet(x) {
  return openSheet({
    title: x.symbol ?? "Decision",
    build: (body) => {
      const reasons = Array.isArray(x.reasons) ? x.reasons : [];
      put(body,
        h("div", { class: "sheet-hero" },
          h("p", { class: "sheet-big" }, `${x.passed ? "Allowed" : "Blocked"} · ${strategyInfo(x.strategy).label}`),
          h("p", { class: "muted small" }, `${dateNY(x.t)} ${timeNY(x.t)} NY`),
          h("div", { class: "sheet-chip" }, decisionChip(x.passed))),
        reasons.length ? group(reasons.map((r) => row({ title: h("span", { class: "wrap" }, String(r)) })), { header: "Reasons" }) : null,
        h("dl", { class: "tiles" },
          h("div", { class: "tile" }, h("dt", null, "Contracts"), h("dd", { class: "num" }, isNum(x.contracts) ? String(x.contracts) : "–")),
          h("div", { class: "tile" }, h("dt", null, term("ev", "Expected value")), h("dd", { class: "num" }, signedMoney(x.expected_value)))),
        h("details", { class: "disclosure" }, h("summary", null, "Details"),
          h("dl", { class: "kv" }, h("dt", null, term("score", "Score")), h("dd", { class: "num" }, num(x.score, 3)))));
    },
  });
}

function decisionRow(x) {
  const reason = (x.reasons || [])[0] ? String(x.reasons[0]) : "";
  return row({
    title: h("span", { class: "idea-title-line" }, h("span", { class: "sym" }, x.symbol ?? "–"), decisionChip(x.passed)),
    sub: `${strategyInfo(x.strategy).label}${reason ? " · " + reason : ""}`,
    trailing: h("span", { class: "when" }, h("span", { class: "num" }, timeNY(x.t)), h("span", { class: "pop-lbl" }, dateNY(x.t))),
    chevron: true, onclick: () => openDecisionSheet(x), haspopup: "dialog",
    label: `${x.passed ? "Allowed" : "Blocked"} ${x.symbol ?? ""}, ${reason}. Open details.`,
    cls: "idea-row",
  });
}

function newsRow(n) {
  return row({
    title: h("span", { class: "wrap news-h" }, extLink(n.url, String(n.headline ?? "Untitled"))),
    sub: h("span", null, h("strong", { class: "sym" }, n.symbol ?? ""), ` · ${String(n.source ?? "unknown source")} · ${relTime(n.published, serverNow())}`),
    trailing: h("span", { class: `val ${sentTone(n.sentiment)}` }, h("span", { "aria-hidden": "true" }, sentArrow(n.sentiment)), sentWord(n.sentiment)),
    cls: "news-row",
  });
}

/** Decisions and news merged into one timeline, newest first. */
export function timelineView() {
  const f = { type: "all", q: "" };
  const host = h("div", { class: "timeline-host" });
  const q = h("input", { type: "search", id: "tl-q", placeholder: "Search symbol or text", autocomplete: "off", maxlength: "60", "aria-label": "Search the timeline" });
  const seg = segmented({ options: [["all", "All"], ["decision", "Decisions"], ["news", "News"]], value: "all", label: "Show", onChange: (v) => { f.type = v; draw(true); } });
  q.addEventListener("input", () => { f.q = q.value.trim().toLowerCase(); draw(true); });
  let lastSig = null, staged = false;
  const first = firstTime("timeline");
  const draw = (force) => {
    const sd = store.get("decisions"), sn = store.get("news");
    const loading = (s) => !s || (s.data == null && !s.error);
    if (loading(sd) && loading(sn)) { if (!host.querySelector(".skeleton")) host.replaceChildren(skeleton()); return; }
    const noData = (s) => !s || s.data == null;
    if (noData(sd) && noData(sn)) {
      const unreachable = (sd?.errorKind || sn?.errorKind) === "network";
      host.replaceChildren(h("div", { class: "state err", role: "status" },
        h("p", { class: "state-title" }, unreachable ? "Can't reach your PC right now." : "Something went wrong loading the activity."),
        h("p", { class: "muted small" }, unreachable ? "Check it's on and Tailscale is connected." : String(sd?.error || sn?.error || "")),
        h("button", { type: "button", class: "btn small", onclick: () => store.refreshMany(["decisions", "news"]) }, "Retry")));
      lastSig = null;
      return;
    }
    const stale = !!(sd?.error || sn?.error);
    let sig = null;
    try { sig = JSON.stringify([sd?.data, sn?.data, f, stale]); } catch { sig = null; }
    if (!force && sig && sig === lastSig) return;
    lastSig = sig;
    const items = [];
    if (f.type !== "news") for (const x of sd?.data?.decisions || []) items.push({ k: "d", t: parseTime(x.t) || 0, x });
    if (f.type !== "decision") for (const n of sn?.data?.items || []) items.push({ k: "n", t: Date.parse(n.published) || 0, x: n });
    const match = (it) => {
      if (!f.q) return true;
      const o = it.x;
      const text = it.k === "d" ? (o.reasons || []).join(" ") : `${o.headline ?? ""} ${o.source ?? ""}`;
      return String(o.symbol || "").toLowerCase().includes(f.q) || text.toLowerCase().includes(f.q);
    };
    const shown = items.filter(match).sort((a, b) => b.t - a.t).slice(0, 120);
    const out = [];
    if (!shown.length) {
      out.push(items.length ? emptyState({ title: "Nothing matches", hint: "Change the search or the filter above." })
        : emptyState({ title: "Nothing yet", hint: "Run a scan or start practice; every idea the system considers is logged here, along with the news it reads." }));
    } else {
      const g = group(shown.map((it) => (it.k === "d" ? decisionRow(it.x) : newsRow(it.x))), { label: "Activity timeline" });
      if (first && !staged) stagger([...g.querySelectorAll(".row")].slice(0, 12), 0, 30);
      staged = true;
      out.push(h("div", { class: "list-meta" }, h("span", null, `${shown.length} item${shown.length === 1 ? "" : "s"}, newest first`)), g);
    }
    if (stale) out.push(h("p", { class: "updating", role: "status" }, h("span", { class: "spin", "aria-hidden": "true" }), "Updating…"));
    host.replaceChildren(...out);
  };
  const u1 = store.subscribe("decisions", () => draw(false)), u2 = store.subscribe("news", () => draw(false));
  const el = h("div", { class: "cu" },
    h("div", { class: "controls" }, seg),
    h("div", { class: "search-row" }, q),
    host,
    h("p", { class: "group-foot" }, "Decisions come from the risk gate. Headlines come from third-party feeds, scored for tone. They are inputs to the model, not advice."));
  el._cleanup = () => { u1(); u2(); };
  draw(true);
  return el;
}

/** Full News page (More > News). */
export function render(root) {
  const news = panel({
    title: "News", id: "news-page", headless: true, store, key: "news",
    render: (d) => {
      const items = (d?.items || []).slice().sort((a, b) => (Date.parse(b.published) || 0) - (Date.parse(a.published) || 0));
      if (!items.length) return null;
      const st = store.data("status");
      const g = group(items.slice(0, 60).map((n) => {
        const r = row({
          title: h("span", { class: "wrap news-h" }, extLink(n.url, String(n.headline ?? "Untitled"))),
          sub: h("span", null, h("strong", { class: "sym" }, n.symbol ?? ""), ` · ${String(n.source ?? "unknown source")} · ${relTime(n.published, serverNow())}`),
          cls: "news-row",
        });
        const main = r.querySelector(".cell-main");
        main.append(sentimentBar(n.sentiment));
        if (isNum(n.relevance) || isNum(n.materiality)) main.append(h("span", { class: "cell-sub" }, `relevance ${pct(n.relevance, 0)} · materiality ${pct(n.materiality, 0)}`));
        return r;
      }), { label: "News headlines" });
      return h("div", null, st?.sentiment_engine ? h("p", { class: "list-meta" }, "Scored by the built-in finance word list") : null, g);
    },
    empty: { title: "No headlines yet", hint: "News is fetched during scans." },
  });
  root.append(news, h("p", { class: "group-foot" }, "Headlines from third-party feeds, scored for tone. They are inputs to the model, not advice."));
}

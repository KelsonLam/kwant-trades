// Learning: why each trade lost (plain words first), what the system changed and why, with owner controls
// (freeze, undo a change, reset). Every API string is inserted as text.
import { h, put, panel, signed, confirmDialog, toast, announce, stagger, firstTime } from "../dom.js";
import { store } from "../store.js";
import { api } from "../api.js";
import { money, signedMoney, pct, num, isNum, dateNY, timeNY, duration } from "../format.js";
import { strategyInfo, exitLabel } from "../glossary.js";
import { group, row, segmented, openSheet } from "../ui.js";

export const meta = { title: "Learning" };

let filter = "losses";

const when = (iso) => (iso ? `${dateNY(iso)} ${timeNY(iso)}` : "–");
const times = (v, d = 1) => (isNum(v) ? `${num(v, d)}×` : "–");
const yesNo = (v) => (v === true ? "Yes" : v === false ? "No" : "–");

function afterAction(r, msg) {
  if (r && typeof r === "object" && "frozen" in r) store.set("learning", r);
  store.refreshMany(["learning", "graduation"]);
  toast(msg); announce(msg);
}

// ---------------------------------------------------------------- post-mortem sheet
function kv(pairs) {
  return h("dl", { class: "kv" }, ...pairs.filter(([, v]) => v != null && v !== "–").flatMap(([k, v]) => [h("dt", null, k), h("dd", { class: "num" }, String(v))]));
}

function tile(lbl, value, v) {
  return h("div", { class: "tile" }, h("dt", null, lbl), h("dd", { class: `num ${isNum(v) ? (v > 0 ? "gain" : v < 0 ? "loss" : "") : ""}` }, value));
}

function openPostMortem(p) {
  const e = p.evidence || {};
  const dir = e.direction_call === 1 ? "Up" : e.direction_call === -1 ? "Down" : e.direction_call === 0 ? "No direction (a volatility trade)" : "–";
  const held = isNum(e.hold_minutes) ? duration(e.hold_minutes * 60000) : "–";
  return openSheet({
    title: p.symbol ?? "Trade",
    build: (body) => {
      put(body,
        h("div", { class: "sheet-hero" },
          h("p", { class: "sheet-big" }, String(p.cause_label ?? "")),
          h("p", null, String(p.cause_text ?? "")),
          h("p", { class: "muted small" }, `${strategyInfo(p.strategy, p.strategy_label).label} · ${p.mode === "alpaca_paper" ? "Alpaca paper" : "built-in paper"} · closed ${when(p.closed)} NY`)),
        h("dl", { class: "tiles" },
          tile("Result", signedMoney(p.pnl), p.pnl),
          tile("Share of the most it could lose", isNum(p.r) ? pct(p.r, 0) : "–", p.r),
          tile("Held for", held),
          tile("Stock moved", isNum(e.move_pct) ? pct(e.move_pct, 2) : "–", e.move_pct)),
        group([h("li", { class: "row" }, h("div", { class: "row-main static stacked" }, kv([
          ["Bet on the stock going", dir],
          ["Was the direction right?", yesNo(e.direction_right)],
          ["Did the news lean the way the stock moved?", yesNo(e.news_right)],
          ["Move compared with a normal day", isNum(e.move_sigma) ? times(Math.abs(e.move_sigma)) : "–"],
          ["Move compared with what options priced in", times(e.realized_vs_implied)],
          ["Move compared with what the model expected", times(e.realized_vs_model)],
          ["Overnight jump against it", isNum(e.gap_sigma) && e.gap_sigma > 0 ? times(e.gap_sigma) + " a normal day" : "–"],
          ["Best result after it was closed", isNum(e.best_post_pnl) ? signedMoney(e.best_post_pnl) : "–"],
          ["Cost to get in and out", isNum(e.cost_total) ? `${money(e.cost_total)}${isNum(e.cost_frac) ? ` (${pct(e.cost_frac, 0)} of the max loss)` : ""}` : "–"],
          ["How it was closed", exitLabel(p.exit_reason)],
        ])))], { header: "What happened" }),
        h("details", { class: "disclosure" }, h("summary", null, "What the model thought at entry"),
          kv([
            ["Signal direction (−1 to +1)", isNum(e.composite_direction) ? num(e.composite_direction, 2) : "–"],
            ["Signal confidence", isNum(e.composite_confidence) ? pct(e.composite_confidence, 0) : "–"],
            ["News sentiment (−1 to +1)", isNum(e.news_direction) ? num(e.news_direction, 2) : "–"],
            ["News confidence", isNum(e.news_confidence) ? pct(e.news_confidence, 0) : "–"],
            ["Chance of profit (model)", isNum(e.pop) ? pct(e.pop, 0) : "–"],
            ["Expected value (model)", isNum(e.model_ev) ? signedMoney(e.model_ev) : "–"],
            ["Volatility edge", isNum(e.vol_edge) ? pct(e.vol_edge, 0) : "–"],
            ["Option price level (implied volatility)", isNum(e.entry_iv) ? pct(e.entry_iv, 1) : "–"],
            ["Market-stress size multiplier", isNum(e.risk_multiplier) ? num(e.risk_multiplier, 2) : "–"],
            ["Opening-range state", e.orb_state ? String(e.orb_state).replace(/_/g, " ") : "–"],
            ["Opened", isNum(e.entry_bucket_min) ? `${e.entry_bucket_min} min after 09:45` : "–"],
            ["Days to expiry at entry", isNum(e.dte_entry) ? String(e.dte_entry) : "–"],
          ])),
        e.recorded === false ? h("p", { class: "small muted" }, "This trade was opened before Kwant Trades recorded entry details, so some numbers are missing.") : null,
        p.tracking_done ? null : h("p", { class: "small muted" }, "Still following this option until it expires, to see whether a different exit would have done better."));
    },
  });
}

// ---------------------------------------------------------------- sections
function controls() {
  return panel({
    title: "Learning", id: "lr-controls", headless: true, store, key: "learning",
    render: (d) => {
      const id = "lr-freeze";
      const input = h("input", { type: "checkbox", id, checked: !!d.frozen, "aria-describedby": "lr-freeze-help" });
      input.addEventListener("change", async () => {
        input.disabled = true;
        try { afterAction(await api.learningFreeze(input.checked), input.checked ? "Learning frozen: nothing will change by itself." : "Learning is on again."); }
        catch (e) { toast(`Couldn't change it: ${e.message}`, "error"); input.checked = !input.checked; }
        input.disabled = false;
      });
      const s = d.summary || {};
      let line;
      if (d.frozen) line = "Frozen: nothing changes by itself. Post-mortems are still written, and you can still undo changes.";
      else if (isNum(s.closed_trades) && isNum(s.min_trades) && s.closed_trades < s.min_trades) line = `Learned from ${s.closed_trades} closed trade${s.closed_trades === 1 ? "" : "s"}. It needs ${s.min_trades} before it may change anything.`;
      else if (s.cooldown_until) line = `Waiting until ${dateNY(s.cooldown_until + "T12:00:00-05:00")} before the next change (5 trading days between changes).`;
      else line = String(s.last_review?.note || `Learned from ${s.closed_trades ?? 0} closed trades.`);
      return group([h("li", { class: "row" }, h("div", { class: "row-main static stacked" },
        h("label", { class: "check switch", for: id }, input, h("span", { class: "sw-track", "aria-hidden": "true" }), h("span", { class: "cell-title" }, "Freeze learning")),
        h("p", { class: "cell-sub", id: "lr-freeze-help" }, line)))],
      { header: "Control", footer: d.active ? "Learning is following the running practice session." : "Learning runs while a paper or Alpaca paper session is running. Live trading never learns." });
    },
  });
}

function timeline() {
  let pnl = null;
  const seg = segmented({ options: [["losses", "Losses"], ["all", "All trades"]], value: filter, label: "Which trades",
    onChange: (v) => { filter = v; if (pnl) pnl._redraw(); } });
  pnl = panel({
    title: "What it learned", id: "lr-timeline", store, key: "learning",
    note: "Every closed trade gets one plain reason. Tap a trade for the numbers behind it.",
    render: (d) => {
      const all = d.postmortems || [];
      const list = filter === "losses" ? all.filter((p) => !p.win) : all;
      if (!list.length) return null;
      return group(list.slice(0, 100).map((p) => row({
        title: h("span", { class: "wrap" }, h("span", { class: "sym" }, p.symbol ?? "–"), " · ", String(p.cause_label ?? "")),
        sub: h("span", { class: "sub-stack" }, h("span", { class: "cause-line" }, String(p.cause_text ?? "")),
          h("span", null, `${strategyInfo(p.strategy, p.strategy_label).label} · ${when(p.closed)}`)),
        trailing: signed(signedMoney(p.pnl), p.pnl), chevron: true, haspopup: "dialog",
        onclick: () => openPostMortem(p),
        label: `${p.symbol}: ${p.cause_text}. Result ${signedMoney(p.pnl)}. Open the evidence.`,
      })), { label: "Closed trades and their causes" });
    },
    empty: () => ({ title: filter === "losses" ? "No losing trades yet" : "No closed trades yet", hint: "When a practice trade closes, the reason it won or lost appears here." }),
  });
  return h("div", null, h("div", { class: "controls" }, seg), pnl);
}

function settingsChanged() {
  return panel({
    title: "Settings it changed", id: "lr-settings", store, key: "learning",
    render: (d) => {
      const rows = d.changed || [];
      if (!rows.length) return null;
      return group(rows.map((x) => row({
        title: String(x.label ?? x.key),
        sub: String(x.plain ?? ""),
        trailing: h("span", { class: "when" }, h("span", { class: "num" }, String(x.display_value ?? "–")), h("span", { class: "muted small" }, `was ${x.display_default ?? "–"}`)),
      })), { footer: "Learned values sit on top of config.toml; the file itself is never rewritten. Risk limits are never changed." });
    },
    empty: { title: "Nothing changed yet", hint: "Everything runs on your config.toml settings. A change appears here only when the evidence is strong." },
  });
}

const EV_LABELS = {
  n: "Trades looked at", wins: "Winners", right: "Direction calls right", hit_rate: "Hit rate", predicted_win_rate: "Win rate the model expected",
  realized_win_rate: "Win rate it got", p_value: "Chance a gap this big is luck (p)", mean_r: "Average result (share of amount risked)",
  ci80: "80% range of the average", ci90: "90% range", train_n: "Older trades (tested on)", test_n: "Recent trades (checked on)",
  train_gain: "Gain on older trades", train_gain_ci80_low: "Cautious end of that gain", test_gain: "Gain on recent trades",
  n_early: "Early trades", n_later: "Later trades", mean_r_early: "Early trades' average", mean_r_later: "Later trades' average",
  n_after: "Trades after the change", n_before: "Trades before it", mean_r_after: "Average after", mean_r_before: "Average before",
  test: "Test used", strategy: "Strategy", window: "Time window",
};
const PCT_KEYS = /rate|mean|gain|ci/;

function evValue(k, v) {
  if (Array.isArray(v)) return v.every(isNum) ? (PCT_KEYS.test(k) ? `${pct(v[0], 0)} to ${pct(v[1], 0)}` : v.join(" – ")) : v.join(" – ");
  if (isNum(v)) return k === "p_value" ? num(v, 3) : PCT_KEYS.test(k) ? pct(v, 1) : String(v);
  if (typeof v === "string") return v.replace(/_/g, " ");
  return null;
}

function revertButton(x) {
  const btn = h("button", { type: "button", class: "btn small row-btn" }, "Undo…");
  btn.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "Undo this change?",
      body: [`${x.label}: back from ${x.display_after} to ${x.display_before}.`,
        "This counts as a change too: the next automatic change waits 5 trading days, and the Road to Alpaca needs fresh trades again."],
      confirmText: "Undo change",
    });
    if (!ok) return;
    btn.disabled = true;
    try { afterAction(await api.learningRevert(x.id), "Change undone."); }
    catch (e) { toast(`Couldn't undo it: ${e.message}`, "error"); btn.disabled = false; }
  });
  return btn;
}

function history() {
  return panel({
    title: "Change history", id: "lr-history", store, key: "learning",
    render: (d) => {
      const list = d.history || [];
      if (!list.length) return null;
      return group(list.map((x) => {
        const title = x.kind === "revert" ? `Undo: ${x.label ?? ""}` : x.kind === "reset" ? "Reset to defaults" : String(x.label ?? "");
        const status = x.kind !== "change" ? (x.by === "auto" ? "automatic" : "by you") : x.status === "active" ? "active" : "undone";
        const evid = Object.entries(x.evidence || {}).map(([k, v]) => [EV_LABELS[k] || k.replace(/_/g, " "), evValue(k, v)]).filter(([, v]) => v != null);
        return h("li", { class: "row" }, h("div", { class: "row-main static stacked" },
          h("div", { class: "risk-line" }, h("span", { class: "cell-title" }, title), x.can_revert ? revertButton(x) : h("span", { class: "chip neutral" }, status)),
          h("p", { class: "cell-sub cause-line" }, String(x.reason ?? "")),
          h("p", { class: "cell-sub" }, `${when(x.ts)} NY · ${x.display_before ?? "–"} → ${x.display_after ?? "–"}`, x.revert_reason ? ` · ${x.revert_reason}` : ""),
          evid.length ? h("details", { class: "disclosure" }, h("summary", null, "Evidence"), kv(evid)) : null));
      }), { footer: "At most one setting changes per review, by a small step, with 5 trading days between changes. A change is undone automatically if the 15 trades after it do clearly worse than the 15 before it." });
    },
    empty: { title: "No changes yet", hint: "Nothing may change before 20 closed trades, and then only on clear evidence." },
  });
}

function resetBlock() {
  const btn = h("button", { type: "button", class: "btn danger-ghost block" }, "Reset everything learned…");
  btn.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "Reset everything learned?",
      body: ["Every learned setting goes back to your config.toml defaults and paused strategies are allowed again.",
        "Post-mortems and the change history are kept. The Road to Alpaca will need fresh trades again."],
      confirmText: "Reset", danger: true,
    });
    if (!ok) return;
    btn.disabled = true;
    try { afterAction(await api.learningReset(), "Everything learned was reset to the defaults."); }
    catch (e) { toast(`Couldn't reset: ${e.message}`, "error"); }
    btn.disabled = false;
  });
  return h("div", { class: "group-wrap" }, btn);
}

export function render(root) {
  const sections = [
    h("p", { class: "lead muted" }, "After every closed trade, Kwant Trades writes down in plain words why it won or lost. It changes a setting only when the evidence is strong, one small step at a time, and it never loosens a risk limit."),
    controls(), timeline(), settingsChanged(), history(), resetBlock(),
    h("p", { class: "footer-note" }, "Learning adapts on practice trades only. Paper results do not predict live results."),
  ];
  if (firstTime("learning")) stagger(sections, 0, 50);
  root.append(...sections);
}

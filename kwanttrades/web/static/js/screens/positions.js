// Portfolio: Open / Closed / Activity. Open positions are rows; tap one for details and the Close action.
import { h, put, panel, term, stagger, signed, confirmDialog, toast, announce, tweenNumber, firstTime } from "../dom.js";
import { store } from "../store.js";
import { api } from "../api.js";
import { money, signedMoney, price, isNum, dateNY, timeNY, daysUntil, ymd, tone, arrow } from "../format.js";
import { strategyInfo, exitLabel, MODES } from "../glossary.js";
import { group, row, segmented, openSheet } from "../ui.js";
import { progressPair, serverNow, isPracticeMode, quoteNote, legsGroup } from "../shared.js";
import { timelineView } from "./activity.js";

export const meta = { title: "Portfolio" };

const refreshAfterAction = () => store.refreshMany(["positions", "status", "equity", "decisions"]);

function closeDisabledReason() {
  const st = store.data("status");
  if (!st) return "Status unknown.";
  if (!st.running) return "No session is running. Positions can be closed from this page only while a paper (practice money) session runs.";
  if (!MODES[st.mode]?.practice) return `In ${MODES[st.mode]?.label || st.mode} mode no orders are sent, so there is nothing to close from here.`;
  return null;
}

/** Returns true when the position was closed. */
async function closeOne(p, btn) {
  const ok = await confirmDialog({
    title: `Close ${p.symbol ?? "position"}?`,
    body: [
      `This sends a closing order for ${p.contracts ?? "–"} × ${strategyInfo(p.strategy, p.strategy_label).label}, using practice money.`,
      h("p", null, "Current open profit/loss: ", signed(signedMoney(p.unrealized_pnl), p.unrealized_pnl), ". The final price may differ slightly (the system pays part of the bid–ask spread)."),
    ],
    confirmText: "Close position",
    danger: true,
  });
  if (!ok) return false;
  btn.disabled = true; btn.textContent = "Closing…";
  let done = false;
  try {
    const r = await api.closePosition(p.id);
    toast(r?.message || (r?.ok ? "Closed." : "Not closed."), r?.ok ? "info" : "error");
    announce(r?.message || "");
    done = !!r?.ok;
  } catch (e) {
    toast(`Couldn't close: ${e.message}`, "error");
  }
  if (!done) { btn.disabled = false; btn.textContent = "Close position…"; }
  refreshAfterAction();
  return done;
}

async function flattenAll(n) {
  const ok = await confirmDialog({
    title: "Flatten everything?",
    body: [`This closes all ${n} open position${n === 1 ? "" : "s"} at once, using practice money.`, "Use it when you want to be out of the market. It cannot be undone, but nothing new is opened."],
    confirmText: `Close all ${n}`,
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await api.flatten();
    toast(r?.message || (r?.ok ? "Flattened." : "Nothing closed."), r?.ok ? "info" : "error");
  } catch (e) { toast(`Couldn't flatten: ${e.message}`, "error"); }
  refreshAfterAction();
}

// P&L elements persist across re-renders so values tween and flash on change
const pnlEls = new Map();
function pnlNode(p) {
  let el = pnlEls.get(p.id);
  if (!el) { el = h("span", { class: "num" }); pnlEls.set(p.id, el); }
  tweenNumber(el, p.unrealized_pnl, (v) => signedMoney(v));
  return h("span", { class: `val ${tone(p.unrealized_pnl)}` }, h("span", { class: "arw", "aria-hidden": "true" }, arrow(p.unrealized_pnl)), " ", el);
}

const findOpen = (id) => (store.data("positions")?.open || []).find((x) => x.id === id);

function openPositionSheet(id) {
  const start = findOpen(id);
  return openSheet({
    title: start?.symbol ?? "Position",
    build: (body, ctl) => {
      const live = h("div", { class: "live-host" });
      const actions = h("div", { class: "sheet-actions" });
      const legsHost = h("div");
      body.append(live, legsHost, actions);
      let built = false;
      const draw = () => {
        const p = findOpen(id);
        if (!p) { toast("That position is no longer open."); ctl.close(); return; }
        const dte = daysUntil(p.expiry, serverNow());
        const credit = isNum(p.entry_price) && p.entry_price < 0;
        const info = strategyInfo(p.strategy, p.strategy_label);
        live.replaceChildren(
          h("div", { class: "sheet-hero" },
            h("p", { class: "muted" }, info.label),
            h("p", { class: `sheet-big val ${tone(p.unrealized_pnl)}` }, h("span", { class: "arw", "aria-hidden": "true" }, arrow(p.unrealized_pnl)), " ", signedMoney(p.unrealized_pnl)),
            h("p", { class: "muted small" }, `Open profit/loss · ${p.contracts ?? "–"} contract-set${p.contracts === 1 ? "" : "s"} · opened ${dateNY(p.opened)} ${timeNY(p.opened)}`)),
          progressPair(p),
          h("dl", { class: "tiles" },
            h("div", { class: "tile" }, h("dt", null, credit ? "Collected" : "Paid"), h("dd", { class: "num" }, price(Math.abs(p.entry_price ?? NaN)), h("span", { class: "muted small" }, " /sh"))),
            h("div", { class: "tile" }, h("dt", null, "Worth now"), h("dd", { class: "num" }, p.mark == null ? "–" : price(Math.abs(p.mark)), h("span", { class: "muted small" }, " /sh"))),
            h("div", { class: "tile" }, h("dt", null, term("max_loss", "Most you can lose")), h("dd", { class: "num" }, isNum(p.max_loss) && isNum(p.contracts) ? money(p.max_loss * p.contracts, { cents: false }) : "–")),
            h("div", { class: "tile" }, h("dt", null, "Expires"), h("dd", { class: "num" }, ymd(p.expiry), h("span", { class: "muted small" }, isNum(dte) ? ` · ${dte <= 0 ? "today" : dte + " d"}` : "")))));
        if (!built) {
          built = true;
          legsHost.append(legsGroup(p.legs));
          const why = closeDisabledReason();
          const btn = h("button", { type: "button", class: "btn danger block", disabled: !!why, "aria-describedby": why ? "close-why" : null }, "Close position…");
          btn.addEventListener("click", async () => { const cur = findOpen(id) || p; if (await closeOne(cur, btn)) ctl.close(); });
          put(actions, btn, why ? h("p", { class: "small muted", id: "close-why" }, why) : null, quoteNote({ banner: false }));
        }
      };
      draw();
      const unsub = store.subscribe("positions", draw);
      return () => { unsub(); actions.querySelectorAll(".cu").forEach((n) => n._cleanup && n._cleanup()); };
    },
  });
}

function openView() {
  const summary = h("div", { class: "list-meta" });
  const why = h("p", { class: "group-foot" });
  const syncFlat = () => {
    const open = store.data("positions")?.open || [];
    const r = closeDisabledReason();
    why.textContent = open.length ? (r || "“Close all” in the top bar closes every open position.") : "";
  };
  const list = panel({
    title: "Open positions", id: "pf-open", headless: true, store, key: "positions",
    render: (d) => {
      const open = d?.open || [];
      if (!open.length) return null;
      const total = open.reduce((a, p) => a + (isNum(p.unrealized_pnl) ? p.unrealized_pnl : 0), 0);
      summary.replaceChildren(h("span", null, `${open.length} open · `), signed(signedMoney(total), total));
      const g = group(open.map((p) => {
        const r = row({
          title: h("span", { class: "sym" }, p.symbol ?? "–"),
          sub: `${strategyInfo(p.strategy, p.strategy_label).label}`,
          trailing: pnlNode(p), chevron: true, onclick: () => openPositionSheet(p.id), haspopup: "dialog",
          label: `${p.symbol}, ${strategyInfo(p.strategy, p.strategy_label).label}, open profit or loss ${signedMoney(p.unrealized_pnl)}. Open details.`,
          cls: "pos-row",
        });
        r.querySelector(".cell-main").append(progressPair(p));
        return r;
      }), { label: "Open positions" });
      return h("div", null, summary, g);
    },
    empty: () => ({ title: "No open positions", hint: isPracticeMode() ? "The system will open trades here when an idea passes the risk gate during the entry window." : "Start practice from Home to let the system trade paper money." }),
  });
  const wrap = h("div", { class: "cu" }, list, why, quoteNote());
  const u = store.subscribe("positions", syncFlat), u2 = store.subscribe("status", syncFlat);
  wrap._cleanup = () => { u(); u2(); };
  syncFlat();
  return wrap;
}

function closedView() {
  return panel({
    title: "Closed trades", id: "pf-closed", headless: true, store, key: "positions",
    render: (d) => {
      const list = d?.closed || [];
      if (!list.length) return null;
      const total = list.reduce((a, p) => a + (isNum(p.realized_pnl) ? p.realized_pnl : 0), 0);
      const wins = list.filter((p) => p.realized_pnl > 0).length;
      return h("div", null,
        h("div", { class: "list-meta" }, h("span", null, `${list.length} closed · ${wins} winner${wins === 1 ? "" : "s"} · total `), signed(signedMoney(total), total)),
        group(list.map((p) => row({
          title: h("span", { class: "sym" }, p.symbol ?? "–"),
          sub: h("span", { class: "sub-stack" },
            h("span", null, `${strategyInfo(p.strategy, p.strategy_label).label} · ${exitLabel(p.exit_reason)} · ${dateNY(p.closed)} ${timeNY(p.closed)}`),
            p.postmortem?.cause_text ? h("span", { class: "cause-line" }, String(p.postmortem.cause_text)) : null),
          trailing: signed(signedMoney(p.realized_pnl), p.realized_pnl),
          label: `${p.symbol}, ${signedMoney(p.realized_pnl)}. ${p.postmortem?.cause_text ?? exitLabel(p.exit_reason)}`,
        })), { label: "Closed trades" }),
        h("p", { class: "group-foot" }, "The line under each trade says, in plain words, why it won or lost. More on the Learning page."));
    },
    empty: { title: "No closed trades yet", hint: "Finished trades and why they closed (target, stop, end of day…) will be listed here." },
  });
}

export function render(root, params, ctx) {
  const VIEWS = ["open", "closed", "activity"];
  let cur = VIEWS.includes(params[0]) ? params[0] : "open";

  // Flatten all lives in the nav bar (destructive: always confirmed)
  const flatBtn = h("button", { type: "button", class: "nb-btn danger cu" }, "Close all");
  const syncFlat = () => {
    const open = store.data("positions")?.open || [];
    flatBtn.disabled = !!closeDisabledReason() || !open.length;
    flatBtn.hidden = cur !== "open";
  };
  flatBtn.addEventListener("click", () => flattenAll((store.data("positions")?.open || []).length));
  const u1 = store.subscribe("positions", syncFlat), u2 = store.subscribe("status", syncFlat);
  flatBtn._cleanup = () => { u1(); u2(); };
  ctx.trail.append(flatBtn);

  const host = h("div", { class: "seg-host" });
  const cleanHost = () => host.querySelectorAll(".cu").forEach((n) => { try { n._cleanup && n._cleanup(); } catch { /* ignore */ } });
  const show = (v, animate) => {
    cur = v;
    cleanHost();
    host.replaceChildren(v === "open" ? openView() : v === "closed" ? closedView() : timelineView());
    if (animate) { host.classList.remove("seg-in"); void host.offsetWidth; host.classList.add("seg-in"); }
    syncFlat();
  };
  const seg = segmented({ options: [["open", "Open"], ["closed", "Closed"], ["activity", "Activity"]], value: cur, label: "Portfolio view",
    onChange: (v) => { show(v, true); history.replaceState(null, "", v === "open" ? "#/portfolio" : `#/portfolio/${v}`); } });
  const holder = h("div", { class: "cu" }, host);
  holder._cleanup = cleanHost;
  show(cur, false);
  root.append(h("div", { class: "controls" }, seg), holder);
  if (firstTime("portfolio")) stagger([seg, holder], 0, 60);
}

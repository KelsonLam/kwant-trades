import { h, panel, term, stagger, signed, confirmDialog, toast, announce, tweenNumber } from "../dom.js";
import { store } from "../store.js";
import { api } from "../api.js";
import { money, signedMoney, price, isNum, dateNY, timeNY, daysUntil, ymd } from "../format.js";
import { strategyInfo, exitLabel, MODES } from "../glossary.js";
import { progressPair, serverNow, isPracticeMode, delayNote, legsTable } from "../shared.js";

const refreshAfterAction = () => store.refreshMany(["positions", "status", "equity", "decisions"]);

function closeDisabledReason() {
  const st = store.data("status");
  if (!st) return "Status unknown.";
  if (!st.running) return "No session is running. Positions can be closed from this page only while a paper (practice money) session runs.";
  if (!MODES[st.mode]?.practice) return `In ${MODES[st.mode]?.label || st.mode} mode no orders are sent, so there is nothing to close from here.`;
  return null;
}

async function closeOne(p, btn) {
  const ok = await confirmDialog({
    title: `Close ${p.symbol ?? "position"}?`,
    body: [
      `This sends a closing order for ${p.contracts ?? "—"} × ${strategyInfo(p.strategy, p.strategy_label).label}, using practice money.`,
      h("p", null, "Current open profit/loss: ", signed(signedMoney(p.unrealized_pnl), p.unrealized_pnl), ". The final price may differ slightly (the system pays part of the bid–ask spread)."),
    ],
    confirmText: "Close position",
    danger: true,
  });
  if (!ok) return;
  btn.disabled = true; btn.textContent = "Closing…";
  try {
    const r = await api.closePosition(p.id);
    toast(r?.message || (r?.ok ? "Closed." : "Not closed."), r?.ok ? "info" : "error");
    announce(r?.message || "");
  } catch (e) {
    toast(`Couldn't close: ${e.message}`, "error");
  }
  refreshAfterAction();
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

function openCard(p) {
  const why = closeDisabledReason();
  const btn = h("button", { type: "button", class: "btn danger-ghost small", disabled: !!why, "aria-describedby": why ? `why-${p.id}` : null }, "Close…");
  btn.addEventListener("click", () => closeOne(p, btn));
  const dte = daysUntil(p.expiry, serverNow());
  const credit = isNum(p.entry_price) && p.entry_price < 0;
  let pnlEl = pnlEls.get(p.id);
  if (!pnlEl) { pnlEl = h("span", { class: "num" }); pnlEls.set(p.id, pnlEl); }
  tweenNumber(pnlEl, p.unrealized_pnl, (v) => signedMoney(v));
  const legs = h("details", { class: "legs-d" }, h("summary", null, "Legs"), legsTable(p.legs));
  return h("article", { class: "card pos" },
    h("header", { class: "pos-head" },
      h("div", null, h("h3", null, h("span", { class: "sym" }, p.symbol ?? "—"), " ", h("span", { class: "strat-label" }, strategyInfo(p.strategy, p.strategy_label).label)),
        h("p", { class: "small muted" }, `${p.contracts ?? "—"} contract-set${p.contracts === 1 ? "" : "s"} · opened ${dateNY(p.opened)} ${timeNY(p.opened)}`)),
      h("div", { class: "pos-pnl" }, h("span", { class: `val big ${isNum(p.unrealized_pnl) ? (p.unrealized_pnl > 0 ? "gain" : p.unrealized_pnl < 0 ? "loss" : "flat") : ""}` },
        h("span", { class: "arw", "aria-hidden": "true" }, p.unrealized_pnl > 0 ? "▲" : p.unrealized_pnl < 0 ? "▼" : "◆"), " ", pnlEl), h("span", { class: "small muted" }, "open profit/loss"))),
    h("dl", { class: "stats four" },
      h("div", { class: "stat" }, h("dt", null, credit ? "Collected" : "Paid"), h("dd", { class: "num" }, price(Math.abs(p.entry_price ?? NaN)), h("span", { class: "muted small" }, " /sh"))),
      h("div", { class: "stat" }, h("dt", null, "Worth now"), h("dd", { class: "num live" }, p.mark == null ? "—" : price(Math.abs(p.mark)), h("span", { class: "muted small" }, " /sh"))),
      h("div", { class: "stat" }, h("dt", null, term("max_loss", "Max loss")), h("dd", { class: "num" }, isNum(p.max_loss) && isNum(p.contracts) ? money(p.max_loss * p.contracts, { cents: false }) : "—")),
      h("div", { class: "stat" }, h("dt", null, "Expires"), h("dd", { class: "num" }, ymd(p.expiry), h("span", { class: "muted small" }, isNum(dte) ? ` · ${dte <= 0 ? "today" : dte + " d"}` : "")))),
    progressPair(p),
    h("div", { class: "pos-foot" }, legs, h("div", { class: "pos-act" }, btn, why ? h("p", { class: "small muted", id: `why-${p.id}` }, why) : null)));
}

export function render(root) {
  const flatBtn = h("button", { type: "button", class: "btn danger small" }, "Flatten all…");
  const flatWhy = h("p", { class: "small muted" });
  const head = h("section", { class: "panel span-3 toolbar cu" },
    h("div", { class: "toolbar-row" },
      h("div", null, h("h1", { class: "screen-title" }, "Positions"), h("p", { class: "muted" }, "What you hold, how it is doing, and how close each trade is to its profit target or stop-loss.")),
      h("div", { class: "flat-wrap" }, flatBtn, flatWhy)),
    delayNote());
  const syncFlat = () => {
    const open = store.data("positions")?.open || [];
    const why = closeDisabledReason();
    flatBtn.disabled = !!why || !open.length;
    flatWhy.textContent = why || (open.length ? "Closes every open position." : "Nothing open.");
  };
  flatBtn.addEventListener("click", () => flattenAll((store.data("positions")?.open || []).length));
  const u1 = store.subscribe("positions", syncFlat), u2 = store.subscribe("status", syncFlat);
  head._cleanup = () => { u1(); u2(); };
  syncFlat();

  const open = panel({
    title: "Open positions", cls: "span-3 bare", store, key: "positions",
    render: (d, { first }) => {
      const list = d?.open || [];
      if (!list.length) return null;
      const grid = h("div", { class: "card-grid two" }, list.map(openCard));
      if (first) stagger([...grid.children], 0, 60);
      return grid;
    },
    empty: () => ({ title: "No open positions", hint: isPracticeMode() ? "The system will open trades here when an idea passes the risk gate during the entry window." : "Start a Paper session in Settings to let the system trade practice money." }),
  });

  const closed = panel({
    title: "Closed trades", cls: "span-3", store, key: "positions",
    render: (d) => {
      const list = d?.closed || [];
      if (!list.length) return null;
      const total = list.reduce((a, p) => a + (isNum(p.realized_pnl) ? p.realized_pnl : 0), 0);
      const wins = list.filter((p) => p.realized_pnl > 0).length;
      return h("div", null,
        h("p", { class: "small" }, `${list.length} closed · ${wins} winners · total `, signed(signedMoney(total), total)),
        h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
          h("caption", { class: "sr-only" }, "Closed trades"),
          h("thead", null, h("tr", null, ...["Closed", "Symbol", "Trade", "Contracts", "Why it closed"].map((x) => h("th", { scope: "col" }, x)), h("th", { scope: "col", class: "num" }, "Result"))),
          h("tbody", null, list.map((p) => h("tr", null,
            h("td", { class: "num" }, dateNY(p.closed), " ", timeNY(p.closed)),
            h("td", null, h("strong", { class: "sym" }, p.symbol ?? "—")),
            h("td", { class: "small" }, strategyInfo(p.strategy, p.strategy_label).label),
            h("td", { class: "num" }, p.contracts ?? "—"),
            h("td", null, h("span", { class: `reason r-${p.exit_reason || "none"}` }, exitLabel(p.exit_reason))),
            h("td", { class: "num" }, signed(signedMoney(p.realized_pnl), p.realized_pnl))))))));
    },
    empty: { title: "No closed trades yet", hint: "Finished trades and why they closed (target, stop, end of day…) will be listed here." },
  });

  const grid = h("div", { class: "grid" }, head, open, closed);
  stagger([...grid.children], 0, 60);
  root.append(grid);
}

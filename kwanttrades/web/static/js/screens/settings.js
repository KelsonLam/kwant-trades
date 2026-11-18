import { h, panel, term, stagger, toast, announce, confirmDialog, firstTime } from "../dom.js";
import { store } from "../store.js";
import { api } from "../api.js";
import { isNum } from "../format.js";
import { MODES } from "../glossary.js";
import { modeBadge } from "../shared.js";
import { group, row } from "../ui.js";

export const meta = { title: "Settings" };

// Field metadata: kind = pct (stored as decimal, shown as %), int, num, money, time, bool, pair, list
const FIELDS = {
  risk: [
    ["max_loss_per_trade_pct", "Max loss per trade", "pct", { min: 0.1, max: 5, step: 0.1, help: "Worst case of any single trade, as a share of your account." }],
    ["daily_loss_limit_pct", "Daily loss limit", "pct", { min: 0.5, max: 10, step: 0.5, help: "If today's losses reach this, the kill switch stops all trading for the day.", term: "daily_loss" }],
    ["max_portfolio_risk_pct", "Max total open risk", "pct", { min: 1, max: 50, step: 1, help: "Sum of the worst cases of everything held at once.", term: "open_risk" }],
    ["max_trades_per_day", "Max trades per day", "int", { min: 0, max: 50, unit: "trades" }],
    ["max_open_positions", "Max open positions", "int", { min: 1, max: 20, unit: "positions" }],
    ["max_positions_per_symbol", "Max positions per symbol", "int", { min: 1, max: 5, unit: "positions" }],
    ["max_contracts_per_trade", "Max contracts per trade", "int", { min: 1, max: 100, unit: "contracts" }],
    ["min_reward_risk", "Min reward-to-risk (debit trades)", "num", { min: 0.1, max: 5, step: 0.1, unit: "×", help: "Max profit must be at least this multiple of max loss." }],
    ["min_credit_to_width", "Min credit-to-width (credit trades)", "num", { min: 0.05, max: 0.9, step: 0.01, unit: "×", help: "Cash collected as a share of the spread width.", term: "credit_spread" }],
    ["kelly_fraction", "Kelly fraction", "num", { min: 0, max: 1, step: 0.05, help: "Share of the mathematically 'optimal' bet size to use. Lower is safer." }],
    ["entry_window_start", "New trades from", "time", { help: "New York time. 09:45 leaves the first 15 minutes to measure the opening range.", term: "opening_range" }],
    ["entry_window_end", "New trades until", "time", { help: "New York time." }],
    ["flatten_time", "Close day trades at", "time", { help: "New York time. Day trades and anything expiring today are closed by then." }],
    ["pdt_guard", "Pattern-day-trader guard", "bool", { help: "Blocks a 4th day trade in 5 days when the account is under $25,000." }],
    ["defined_risk_only", "Defined risk only", "locked", { help: "Always on: every sold option must be covered by a bought one, so losses are capped." }],
  ],
  scan: [
    ["min_expected_value", "Min expected value", "money", { min: 0, max: 500, step: 1, unit: "$ per contract-set", term: "ev" }],
    ["min_score", "Min score", "num", { min: 0, max: 5, step: 0.01, term: "score" }],
    ["min_direction", "Min direction strength", "num", { min: 0, max: 1, step: 0.05, help: "How strong the composite signal must be (0–1) to take a directional trade.", term: "composite" }],
    ["min_vol_edge", "Min vol edge", "pct", { min: 0, max: 100, step: 1, term: "vol_edge" }],
    ["min_svi_zscore", "Min mispricing z-score", "num", { min: 0.5, max: 10, step: 0.1, term: "zscore" }],
    ["max_ideas_per_symbol", "Max ideas per symbol", "int", { min: 1, max: 10, unit: "ideas" }],
    ["max_spread_pct", "Max bid–ask spread", "pct", { min: 1, max: 50, step: 1, help: "Skip options whose spread is wider than this share of their price." }],
    ["min_bid", "Min option bid", "money", { min: 0, max: 5, step: 0.01, unit: "$ per share" }],
    ["min_open_interest", "Min open interest", "int", { min: 0, max: 100000, unit: "contracts" }],
    ["min_volume", "Min volume today", "int", { min: 0, max: 100000, unit: "contracts" }],
    ["directional_dte", "Day-trade expiries", "pair", { min: 0, max: 60, unit: "days to expiry" }],
    ["vol_dte", "Volatility-trade expiries", "pair", { min: 1, max: 120, unit: "days to expiry" }],
    ["universe", "Symbols to scan", "list", { help: "Comma-separated tickers, e.g. SPY, QQQ, AAPL." }],
  ],
};

const SYM_RE = /^[A-Z][A-Z0-9.\-^]{0,9}$/;

function inputFor(sec, [key, lbl, kind, o], value) {
  const id = `cfg-${sec}-${key}`;
  const help = o.help ? h("p", { class: "help", id: id + "-h" }, o.help) : null;
  const err = h("p", { class: "field-err", id: id + "-e" });
  const lab = h("label", { for: id }, lbl, o.term ? [" ", term(o.term, "?")] : null);
  let input, read, unit = o.unit || "";
  const describe = [help ? id + "-h" : null, id + "-e"].filter(Boolean).join(" ");
  if (kind === "bool") {
    input = h("input", { type: "checkbox", id, checked: !!value, "aria-describedby": describe });
    read = () => ({ ok: true, v: input.checked });
    return { el: h("div", { class: "field bool" }, h("label", { class: "check switch", for: id }, input, h("span", { class: "sw-track", "aria-hidden": "true" }), h("span", null, lbl)), help, err), read, err, input };
  }
  if (kind === "locked") {
    return { el: h("div", { class: "field" }, h("p", { class: "lbl-static" }, lbl), h("p", { class: "locked" }, h("span", { "aria-hidden": "true" }, "\u{1F512} "), value ? "On" : "Off (set in config.toml)"), help), read: null };
  }
  if (kind === "pair") {
    const a = h("input", { type: "number", id, min: o.min, max: o.max, step: 1, inputmode: "numeric", value: Array.isArray(value) ? value[0] : "", "aria-describedby": describe, "aria-label": `${lbl} from` });
    const b = h("input", { type: "number", min: o.min, max: o.max, step: 1, inputmode: "numeric", value: Array.isArray(value) ? value[1] : "", "aria-label": `${lbl} to` });
    read = () => {
      const x = Number(a.value), y = Number(b.value);
      if (!Number.isInteger(x) || !Number.isInteger(y)) return { ok: false, msg: "Use whole numbers." };
      if (x < o.min || y > o.max || x > y) return { ok: false, msg: `From ${o.min} to ${o.max}, and 'from' must not exceed 'to'.` };
      return { ok: true, v: [x, y] };
    };
    return { el: h("div", { class: "field" }, lab, h("div", { class: "pair" }, a, h("span", { class: "muted" }, "to"), b, h("span", { class: "unit" }, unit)), help, err), read, err, input: a, inputs: [a, b] };
  }
  if (kind === "list") {
    input = h("input", { type: "text", id, value: Array.isArray(value) ? value.join(", ") : "", autocomplete: "off", spellcheck: "false", "aria-describedby": describe });
    read = () => {
      const syms = input.value.toUpperCase().split(/[\s,]+/).filter(Boolean);
      if (!syms.length) return { ok: false, msg: "Enter at least one symbol." };
      const bad = syms.find((x) => !SYM_RE.test(x));
      if (bad) return { ok: false, msg: `"${bad}" doesn't look like a ticker.` };
      if (syms.length > 40) return { ok: false, msg: "At most 40 symbols." };
      return { ok: true, v: [...new Set(syms)] };
    };
    return { el: h("div", { class: "field wide" }, lab, input, help, err), read, err, input };
  }
  if (kind === "time") {
    input = h("input", { type: "time", id, value: typeof value === "string" ? value : "", step: 60, "aria-describedby": describe });
    read = () => (/^\d{2}:\d{2}$/.test(input.value) ? { ok: true, v: input.value } : { ok: false, msg: "Use HH:MM (24-hour, New York)." });
    return { el: h("div", { class: "field" }, lab, h("div", { class: "with-unit" }, input, h("span", { class: "unit" }, "NY")), help, err), read, err, input };
  }
  const shown = kind === "pct" ? (isNum(value) ? +(value * 100).toFixed(4) : "") : (isNum(value) ? value : "");
  if (kind === "pct") unit = "%";
  input = h("input", { type: "number", id, min: o.min, max: o.max, step: o.step ?? 1, value: shown, inputmode: kind === "int" ? "numeric" : "decimal", "aria-describedby": describe });
  read = () => {
    const raw = input.value.trim();
    const n = Number(raw);
    if (raw === "" || !Number.isFinite(n)) return { ok: false, msg: "Enter a number." };
    if (kind === "int" && !Number.isInteger(n)) return { ok: false, msg: "Use a whole number." };
    if (isNum(o.min) && n < o.min) return { ok: false, msg: `Minimum is ${o.min}${unit === "%" ? "%" : ""}.` };
    if (isNum(o.max) && n > o.max) return { ok: false, msg: `Maximum is ${o.max}${unit === "%" ? "%" : ""}.` };
    return { ok: true, v: kind === "pct" ? +(n / 100).toFixed(6) : n };
  };
  return { el: h("div", { class: "field" }, lab, h("div", { class: "with-unit" }, input, unit ? h("span", { class: "unit" }, unit) : null), help, err), read, err, input };
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function configForm(cfg) {
  const controls = [];
  const sections = Object.entries(FIELDS).map(([sec, fields]) => {
    const vals = cfg?.[sec] || {};
    const items = fields.filter((f) => f[0] in vals).map((f) => { const c = inputFor(sec, f, vals[f[0]]); c.sec = sec; c.key = f[0]; c.orig = vals[f[0]]; controls.push(c); return c.el; });
    return h("div", { class: "group-wrap cfg-sec" }, h("h3", { class: "group-head" }, sec === "risk" ? "Risk limits" : "Scan thresholds"),
      h("div", { class: "group fields" }, items),
      h("p", { class: "group-foot" }, sec === "risk" ? "Hard limits the risk gate enforces before any order." : "What counts as a worthwhile idea."));
  });
  const status = h("p", { class: "small muted", "aria-live": "polite" });
  const save = h("button", { type: "submit", class: "btn primary" }, "Save changes");
  const reset = h("button", { type: "button", class: "btn ghost" }, "Undo edits");
  const bar = h("div", { class: "form-actions sticky", hidden: true }, save, reset, status);
  let saving = false;
  const form = h("form", { class: "cfg-form", novalidate: true }, ...sections, bar);
  const collect = () => {
    const changed = {}; let bad = 0;
    for (const c of controls) {
      if (!c.read) continue;
      const r = c.read();
      c.err.textContent = r.ok ? "" : r.msg;
      (c.inputs || [c.input]).forEach((i) => i.setAttribute("aria-invalid", String(!r.ok)));
      if (!r.ok) { bad++; continue; }
      if (!same(r.v, c.orig)) (changed[c.sec] ||= {})[c.key] = r.v;
    }
    const n = Object.values(changed).reduce((a, o) => a + Object.keys(o).length, 0);
    status.textContent = bad ? `${bad} field${bad > 1 ? "s need" : " needs"} fixing.` : n ? `${n} unsaved change${n > 1 ? "s" : ""}.` : "No changes.";
    save.disabled = !!bad || !n;
    bar.hidden = !bad && !n && !saving;
    return { changed, bad, n };
  };
  form.addEventListener("input", collect);
  form.addEventListener("change", collect);
  reset.addEventListener("click", () => { store.refresh("config").then(() => form.dispatchEvent(new CustomEvent("reload"))); });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const { changed, bad, n } = collect();
    if (bad || !n) return;
    saving = true; save.disabled = true; save.textContent = "Saving…";
    try {
      const saved = await api.saveConfig(changed);
      if (saved && typeof saved === "object") store.set("config", saved); else store.refresh("config");
      toast(`Saved ${n} setting${n > 1 ? "s" : ""}.`);
      announce("Settings saved");
    } catch (ex) {
      toast(`Couldn't save: ${ex.message}`, "error");
      save.disabled = false;
    }
    save.textContent = "Save changes"; saving = false; collect();
  });
  collect();
  return form;
}

function sessionPanel() {
  const el = h("section", { class: "cu session", "aria-label": "Session controls" });
  const draw = () => {
    const st = store.data("status");
    const busy = el.dataset.busy === "1";
    const start = (mode) => async () => {
      if (st?.running && st.mode !== mode) {
        const ok = await confirmDialog({ title: `Switch to ${MODES[mode].label}?`, body: `The current ${MODES[st.mode]?.label || st.mode} session will stop first. Open positions stay open.`, confirmText: "Switch" });
        if (!ok) return;
      }
      el.dataset.busy = "1"; draw();
      try { const s = await api.start(mode); if (s && s.mode) store.set("status", s); toast(`${MODES[mode].label} session started.`); announce(`${MODES[mode].label} session started`); }
      catch (e) { toast(`Couldn't start: ${e.message}`, "error"); }
      el.dataset.busy = "0"; store.refresh("status");
    };
    const stop = async () => {
      el.dataset.busy = "1"; draw();
      try { const s = await api.stop(); if (s && s.mode) store.set("status", s); toast("Session stopped. Nothing new will be traded."); announce("Session stopped"); }
      catch (e) { toast(`Couldn't stop: ${e.message}`, "error"); }
      el.dataset.busy = "0"; store.refresh("status");
    };
    const keys = !!st?.alpaca_keys_present;
    const opt = (mode, text, sub, disabled, hint) => {
      const isRun = !!(st?.running && st.mode === mode);
      return row({
        title: text, sub: h("span", null, sub, hint ? h("span", { class: "hint" }, " " + hint) : null),
        trailing: isRun ? h("span", { class: "pill on" }, "Running")
          : h("button", { type: "button", class: `btn small ${mode === "paper" ? "primary" : ""}`, disabled: busy || disabled, onclick: start(mode), "aria-label": `${text}` }, "Start"),
      });
    };
    el.replaceChildren(
      group([
        opt("shadow", "Shadow", "Runs everything and records what it would trade. Sends no orders.", false),
        opt("paper", "Paper", "Trades practice money in the built-in simulator.", false),
        opt("alpaca_paper", "Alpaca Paper", "Sends orders to your Alpaca practice account.", !keys, keys ? "" : "Unavailable: no Alpaca keys found in the .env file on this computer."),
        row({ title: "Stop session", sub: "Stops new trades. Open positions stay open (use Portfolio → Close all to close them).",
          trailing: h("button", { type: "button", class: "btn small danger-ghost", disabled: busy || !st?.running, onclick: stop }, "Stop") }),
      ], { header: h("span", { class: "head-with" }, "Session ", st ? modeBadge(st) : null),
        footer: h("span", { class: "live-note", role: "note" }, h("strong", null, "Live trading (real money) cannot be started from this page."),
          " It can only be enabled by the owner from the command line, with an explicit safety setting. API keys live in the .env file on this computer; they are never shown or entered here.") }));
  };
  const u = store.subscribe("status", draw);
  el._cleanup = u;
  draw();
  return el;
}

export function render(root) {
  const cfg = panel({
    title: "Limits and thresholds", id: "settings-cfg", headless: true, store, key: "config",
    render: (d) => {
      const host = h("div");
      const mount = () => host.replaceChildren(configForm(store.data("config")));
      mount();
      host.addEventListener("reload", mount);
      return host;
    },
    empty: { title: "No configuration loaded" },
  });
  const kids = [h("p", { class: "lead" }, "Choose how the system runs and the limits it must respect. Only changed fields are saved."), sessionPanel(), cfg];
  if (firstTime("settings")) stagger(kids, 0, 60);
  root.append(...kids);
}

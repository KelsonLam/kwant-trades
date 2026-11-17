// App shell: theme, demo banner, always-visible header strip, hash router, polling.
import { h, s, clear, term, initTooltips, tweenNumber, meter, announce, toast, prefersReducedMotion } from "./dom.js";
import { init, api, isDemo, getDemoReason } from "./api.js";
import { store, startPolling } from "./store.js";
import { money, signedMoney, clock, timeNY, dateNY, parseTime, relTime, isNum, tone, arrow, pct } from "./format.js";
import { MARKET_STATES } from "./glossary.js";
import { ring } from "./charts.js";
import { syncClock, serverNow, modeBadge } from "./shared.js";
import * as overview from "./screens/overview.js";
import * as opportunities from "./screens/opportunities.js";
import * as signals from "./screens/signals.js";
import * as volatility from "./screens/volatility.js";
import * as positions from "./screens/positions.js";
import * as activity from "./screens/activity.js";
import * as backtest from "./screens/backtest.js";
import * as settings from "./screens/settings.js";

const ROUTES = [
  { id: "overview", label: "Overview", mod: overview },
  { id: "opportunities", label: "Opportunities", mod: opportunities },
  { id: "signals", label: "Signals", mod: signals },
  { id: "volatility", label: "Volatility", mod: volatility },
  { id: "positions", label: "Positions", mod: positions },
  { id: "activity", label: "Activity", mod: activity },
  { id: "backtest", label: "Backtest", mod: backtest },
  { id: "settings", label: "Settings", mod: settings },
];

// ---------------------------------------------------------------- theme
const THEME_KEY = "kwanttrades.theme";
function getStoredTheme() { try { return localStorage.getItem(THEME_KEY); } catch { return null; } }
function setStoredTheme(t) { try { localStorage.setItem(THEME_KEY, t); } catch { /* storage unavailable */ } }
function currentTheme() {
  const t = document.documentElement.dataset.theme;
  if (t === "light" || t === "dark") return t;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}
function applyTheme(t) { document.documentElement.dataset.theme = t; }
applyTheme(getStoredTheme() || currentTheme());

function themeToggle() {
  const sun = s("svg", { width: 18, height: 18, viewBox: "0 0 24 24", class: "ico ico-sun", "aria-hidden": "true" },
    s("circle", { cx: 12, cy: 12, r: 4.5 }), ...[0, 45, 90, 135, 180, 225, 270, 315].map((a) => s("line", { x1: 12, y1: 2.5, x2: 12, y2: 5, transform: `rotate(${a} 12 12)` })));
  const moon = s("svg", { width: 18, height: 18, viewBox: "0 0 24 24", class: "ico ico-moon", "aria-hidden": "true" }, s("path", { d: "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z" }));
  const btn = h("button", { type: "button", class: "icon-btn theme-btn", "aria-label": "Switch to light theme" }, sun, moon);
  const sync = () => {
    const t = currentTheme();
    btn.setAttribute("aria-label", t === "dark" ? "Switch to light theme" : "Switch to dark theme");
    btn.setAttribute("aria-pressed", String(t === "light"));
  };
  btn.addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    applyTheme(next); setStoredTheme(next); sync();
    announce(`${next === "dark" ? "Dark" : "Light"} theme`);
  });
  sync();
  return btn;
}

// ---------------------------------------------------------------- header strip
const hdr = {};
function buildHeader() {
  hdr.mode = h("span", { class: "hdr-mode" });
  hdr.ringHost = h("span", { class: "mk-visual" });
  hdr.mkState = h("span", { class: "mk-state" }, "—");
  hdr.mkCount = h("span", { class: "mk-count num" }, "");
  hdr.mkBar = h("span", { class: "bar-fill" });
  hdr.sessBar = h("span", { class: "bar sess-bar", hidden: true, "aria-hidden": "true" }, hdr.mkBar);
  hdr.equity = h("span", { class: "eq-val num" }, "—");
  hdr.dayArrow = h("span", { class: "arw", "aria-hidden": "true" });
  hdr.dayVal = h("span", { class: "num" }, "—");
  hdr.day = h("span", { class: "day-pnl val flat" }, hdr.dayArrow, " ", hdr.dayVal);
  hdr.meters = h("div", { class: "meters" });
  hdr.kill = h("span", { class: "kill" });
  hdr.conn = h("span", { class: "conn", hidden: true, role: "status" }, "Reconnecting…");
  hdr.note = h("p", { class: "hdr-note small" });
  hdr.pulse = h("span", { class: "live-dot", "aria-hidden": "true" });

  const brand = h("a", { href: "#/overview", class: "brand", "aria-label": "Kwant Trades overview" },
    s("svg", { width: 22, height: 22, viewBox: "0 0 32 32", class: "brand-mark", "aria-hidden": "true" },
      s("path", { d: "M3 24 L12 15 L18 20 L29 7", class: "bm-line" }), s("circle", { cx: 29, cy: 7, r: 3, class: "bm-dot" })),
    h("span", { class: "brand-word" }, "KWANT", h("span", { class: "brand-edge" }, "TRADES")));

  return h("header", { class: "hdr", role: "banner" },
    h("div", { class: "hdr-row" },
      h("div", { class: "hdr-id" }, brand, hdr.mode),
      h("div", { class: "hdr-market", "aria-live": "polite" }, hdr.ringHost, h("span", { class: "mk-text" }, hdr.mkState, hdr.mkCount, hdr.sessBar)),
      h("div", { class: "hdr-eq" }, h("span", { class: "eq-lbl small" }, "Account value"), hdr.equity, h("span", { class: "eq-day small" }, hdr.day, h("span", { class: "muted" }, " today"))),
      hdr.meters,
      h("div", { class: "hdr-tools" }, hdr.conn, hdr.kill, themeToggle())),
    h("div", { class: "hdr-sub" }, hdr.pulse, hdr.note));
}

let lastMarketState = null, lastMode = null, ringEl = null, wasOffline = false;
function drawHeader() {
  const st = store.data("status");
  const err = store.get("status")?.error;
  // Connection indicator: shown while polling fails (phone asleep, PC off, network change); recovers by itself.
  hdr.conn.hidden = !err;
  if (wasOffline && !err) store.refreshMany(["config", "ideas", "signals", "positions", "equity", "decisions", "news", "scan"]);
  wasOffline = !!err;
  if (st) {
    syncClock(st.now);
    hdr.mode.replaceChildren(modeBadge(st));
    const acc = st.account || {};
    tweenNumber(hdr.equity, acc.equity, (v) => money(v));
    tweenNumber(hdr.dayVal, acc.day_pnl, (v) => signedMoney(v), { flash: false });
    hdr.day.className = `day-pnl val ${tone(acc.day_pnl)}`;
    const prevDay = hdr.day._prev;
    if (isNum(prevDay) && isNum(acc.day_pnl) && Math.abs(prevDay - acc.day_pnl) >= 0.005) {
      hdr.equity.parentElement.classList.remove("flash-gain", "flash-loss"); void hdr.equity.offsetWidth;
      hdr.equity.parentElement.classList.add(acc.day_pnl > prevDay ? "flash-gain" : "flash-loss");
    }
    hdr.day._prev = acc.day_pnl;
    hdr.dayArrow.textContent = arrow(acc.day_pnl);
    drawMeters(st, acc);
    hdr.kill.replaceChildren(st.kill_switch
      ? h("span", { class: "kill-on", role: "status" }, h("span", { "aria-hidden": "true" }, "⛔ "), term("kill_switch", "KILL SWITCH ON"))
      : h("span", { class: "kill-off" }, h("span", { "aria-hidden": "true" }, "● "), term("kill_switch", "Kill switch off")));
    const prov = st.provider ? `Source: ${st.provider}` : "";
    hdr.note.replaceChildren(...[
      h("span", null, String(st.data_delay_note || "Quotes may be delayed.")),
      prov ? h("span", { class: "muted" }, ` · ${prov}`) : null,
      h("span", { class: "muted" }, ` · updated ${timeNY(st.now, { seconds: true })} NY`),
      err ? h("span", { class: "warn-txt" }, " · connection problem — retrying") : null,
    ].filter(Boolean));
    hdr.pulse.classList.toggle("stale", !!err);
    hdr.pulse.classList.remove("beat"); void hdr.pulse.offsetWidth; hdr.pulse.classList.add("beat");
    if (lastMode && lastMode !== (st.running ? st.mode : "idle")) announce(`Mode changed to ${st.running ? st.mode : "idle"}`);
    lastMode = st.running ? st.mode : "idle";
    document.body.classList.toggle("is-practice", !!(st.running && ["paper", "alpaca_paper"].includes(st.mode)));
  } else if (err) {
    hdr.note.replaceChildren(h("span", { class: "warn-txt" }, `Can't reach the server: ${err}. Retrying…`));
    hdr.pulse.classList.add("stale");
  }
  tickMarket();
}

function drawMeters(st, acc) {
  const cfg = store.data("config");
  const eq = acc.equity;
  // The server states the dollar caps itself (st.limits); fall back to deriving them from the percentages.
  const lim = st.limits || {};
  const riskCap = isNum(lim.max_portfolio_risk) ? lim.max_portfolio_risk
    : isNum(cfg?.risk?.max_portfolio_risk_pct) && isNum(eq) ? cfg.risk.max_portfolio_risk_pct * eq : null;
  const lossCap = isNum(lim.daily_loss_limit) ? lim.daily_loss_limit
    : isNum(cfg?.risk?.daily_loss_limit_pct) && isNum(eq) ? cfg.risk.daily_loss_limit_pct * (eq - Math.min(0, acc.day_pnl || 0)) : null;
  const loss = isNum(acc.day_pnl) ? Math.max(0, -acc.day_pnl) : null;
  const mk = (lbl, key, value, cap, fmt) => h("div", { class: "hm" }, h("span", { class: "hm-lbl small" }, term(key, lbl)), meter(value, cap, { label: lbl, fmt }));
  hdr.meters.replaceChildren(
    mk("Open risk", "open_risk", acc.open_risk, riskCap, (v, c) => `${money(v, { cents: false })} / ${isNum(c) ? money(c, { cents: false }) : "—"}`),
    mk("Trades", "contracts", st.trades_today, st.max_trades_per_day, (v, c) => `${isNum(v) ? v : "—"} / ${isNum(c) ? c : "—"}`),
    mk("Day loss", "daily_loss", loss, lossCap, (v, c) => `${money(v, { cents: false })} / ${isNum(c) ? money(c, { cents: false }) : "—"}`));
}

/** Countdown ring during the opening range, then a session progress bar. Runs every second. */
function tickMarket() {
  const st = store.data("status");
  if (!st || !st.market) return;
  const m = st.market;
  const now = serverNow();
  const state = m.state;
  const close = parseTime(m.next_close), open = parseTime(m.next_open), orEnd = parseTime(m.opening_range_ends);
  hdr.mkState.textContent = MARKET_STATES[state] || "Market state unknown";
  let count = "", showBar = false;
  if (state === "opening_range" && orEnd) {
    const start = orEnd - 15 * 60000;
    const frac = (now - start) / (orEnd - start);
    if (!ringEl) { ringEl = ring(frac, { size: 40, label: "Opening range progress" }); hdr.ringHost.replaceChildren(ringEl, h("span", { class: "ring-txt num", "aria-hidden": "true" })); }
    ringEl._set(frac);
    hdr.ringHost.lastChild.textContent = clock(orEnd - now).replace(/^00:/, "");
    count = ` · range set in ${clock(orEnd - now)}`;
  } else {
    if (ringEl) { ringEl = null; hdr.ringHost.replaceChildren(); }
    if (state === "open" && close) {
      const sessStart = close - 6.5 * 3600000;
      hdr.mkBar.style.transform = `scaleX(${Math.max(0, Math.min(1, (now - sessStart) / (close - sessStart)))})`;
      showBar = true;
      count = ` · closes in ${clock(close - now)}`;
    } else if (state === "pre" && open) count = ` · opens in ${clock(open - now)}`;
    else if (state === "closed" && open) count = ` · opens ${dateNY(open, { weekday: true })} ${timeNY(open)} NY`;
    hdr.ringHost.replaceChildren(h("span", { class: `mk-dot ${state}`, "aria-hidden": "true" }));
  }
  hdr.mkCount.textContent = count;
  hdr.sessBar.hidden = !showBar;
  if (lastMarketState && lastMarketState !== state) {
    announce(MARKET_STATES[state] || state);
    if (state === "open") toast("Opening range is set — the entry window is open.");
    store.refreshMany(["status", "signals", "ideas"]);
  }
  lastMarketState = state;
}

// ---------------------------------------------------------------- router
let currentView = null, currentRoute = null;
function parseHash() {
  const raw = (location.hash || "").replace(/^#\/?/, "");
  const [path] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map((p) => { try { return decodeURIComponent(p); } catch { return p; } });
  const route = ROUTES.find((r) => r.id === parts[0]) || ROUTES[0];
  return { route, params: parts.slice(1) };
}

function unmount(view) {
  view.querySelectorAll(".cu").forEach((el) => { try { el._cleanup && el._cleanup(); } catch { /* ignore */ } });
}

function navigate() {
  const { route, params } = parseHash();
  const main = document.getElementById("main");
  document.querySelectorAll(".tabs a").forEach((a) => {
    const on = a.dataset.route === route.id;
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    if (on) a.scrollIntoView({ block: "nearest", inline: "nearest", behavior: prefersReducedMotion() ? "auto" : "smooth" });
  });
  moveTabInk();
  document.title = `${route.label} · Kwant Trades${isDemo() ? " (demo)" : ""}`;
  const old = currentView;
  const view = h("div", { class: "view", "data-route": route.id });
  const mountNew = () => {
    if (old) { unmount(old); old.remove(); }
    main.appendChild(view);
    try { route.mod.render(view, params); }
    catch (e) { console.error(e); view.append(h("p", { class: "state err" }, "This screen couldn't be displayed.")); }
    const show = () => view.classList.add("in");
    requestAnimationFrame(show);
    setTimeout(show, 80); // rAF doesn't fire in background tabs
  };
  if (old && !prefersReducedMotion() && currentRoute !== route.id) {
    old.classList.add("out");
    setTimeout(mountNew, 140);
  } else mountNew();
  currentView = view; currentRoute = route.id;
  if (old) main.focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: "auto" });
}

function moveTabInk() {
  const nav = document.querySelector(".tabs");
  const ink = nav && nav.querySelector(".tab-ink");
  const a = nav && nav.querySelector("a[aria-current=page]");
  if (!ink || !a) return;
  ink.style.transform = `translateX(${a.offsetLeft}px) scaleX(${a.offsetWidth / 100})`;
}

function buildTabs() {
  return h("nav", { class: "tabs", "aria-label": "Screens" },
    h("ul", null, ROUTES.map((r) => h("li", null, h("a", { href: `#/${r.id}`, "data-route": r.id }, r.label)))),
    h("span", { class: "tab-ink", "aria-hidden": "true" }));
}

// ---------------------------------------------------------------- demo banner
function demoBanner() {
  return h("div", { class: "demo-banner", role: "note", "aria-label": "Demo data notice" },
    h("strong", null, "DEMO DATA — not real quotes"),
    h("span", null, ` ${getDemoReason()}. Every price, trade and result on this page is simulated in your browser.`),
    location.search.includes("demo=1") ? h("a", { href: location.pathname, class: "demo-exit" }, "Try the live server") : null);
}

// ---------------------------------------------------------------- polling definitions
function defineResources() {
  const scanRunning = () => store.data("scan")?.state === "running";
  store.define("status", api.status, { interval: 3000, always: true });
  store.define("config", api.config, { interval: 60000, always: true });
  store.define("scan", api.scanStatus, { interval: () => (scanRunning() ? 1000 : 10000), always: true });
  store.define("ideas", api.ideas, { interval: 12000 });
  store.define("signals", api.signals, { interval: 12000 });
  store.define("positions", api.positions, { interval: 10000 });
  store.define("equity", api.equity, { interval: 15000 });
  store.define("decisions", () => api.decisions(100), { interval: 12000 });
  store.define("news", () => api.news("ALL"), { interval: 15000 });
  store.define("backtest", api.backtestResult, { interval: () => (store.data("backtest")?.state === "running" ? 1000 : 15000) });

  let prevScan = null;
  store.subscribe("scan", (st) => {
    const now = st?.data?.state;
    if (prevScan === "running" && now && now !== "running") {
      store.refreshMany(["ideas", "decisions", "status", "positions", "signals", "equity"]).then(() => {
        const n = store.data("ideas")?.ideas?.length ?? 0;
        const msg = now === "done" ? `Scan finished: ${n} idea${n === 1 ? "" : "s"}.` : "Scan finished with an error.";
        announce(msg); toast(msg, now === "done" ? "info" : "error");
      });
    }
    prevScan = now;
  });
  // positions opened by the running session show up in status.trades_today
  let prevTrades = null;
  store.subscribe("status", (st) => {
    const t = st?.data?.trades_today;
    if (isNum(prevTrades) && isNum(t) && t !== prevTrades) store.refreshMany(["positions", "equity", "decisions", "ideas"]);
    prevTrades = t;
  });
  let prevBt = null;
  store.subscribe("backtest", (st) => {
    const s2 = st?.data?.state;
    if (prevBt === "running" && s2 === "done") { announce("Backtest finished"); toast("Backtest finished. Read the caveats before the numbers."); }
    prevBt = s2;
  });
}

// ---------------------------------------------------------------- boot
async function boot() {
  initTooltips();
  const app = document.getElementById("app");
  const header = buildHeader();
  const tabs = buildTabs();
  let first = null;
  try { first = await init(); } catch (e) { first = null; }
  if (isDemo()) { document.body.classList.add("is-demo"); header.prepend(demoBanner()); }
  app.prepend(header, tabs);
  defineResources();
  if (first) store.set("status", first);
  store.subscribe("status", drawHeader);
  store.subscribe("config", drawHeader);
  drawHeader();
  startPolling();
  let tick = setInterval(() => { if (!document.hidden) tickMarket(); }, 1000);
  document.addEventListener("visibilitychange", () => { document.body.classList.toggle("paused", document.hidden); });
  window.addEventListener("hashchange", navigate);
  window.addEventListener("online", () => store.refreshMany(["status", "scan", "ideas", "signals", "positions"]));
  window.addEventListener("resize", () => requestAnimationFrame(moveTabInk));
  document.fonts?.ready?.then(moveTabInk);
  navigate();
  void tick;
}

window.addEventListener("unhandledrejection", (e) => { console.warn("Unhandled:", e.reason); e.preventDefault(); });
boot();

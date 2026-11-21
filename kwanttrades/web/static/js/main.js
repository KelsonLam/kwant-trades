// App shell: bottom tab bar, hash router with push/pop + tab transitions, alert banners, polling.
import { h, initTooltips, announce, toast, prefersReducedMotion } from "./dom.js";
import { init, api, isDemo, getDemoReason } from "./api.js";
import { store, startPolling } from "./store.js";
import { isNum, parseTime } from "./format.js";
import { MARKET_STATES } from "./glossary.js";
import { syncClock } from "./shared.js";
import { createPage, icon, closeSheet, SHEET_EASE } from "./ui.js";
import { apply as applyTheme, watchSystem } from "./appearance.js";
import * as overview from "./screens/overview.js";
import * as opportunities from "./screens/opportunities.js";
import * as positions from "./screens/positions.js";
import * as more from "./screens/more.js";
import * as signals from "./screens/signals.js";
import * as volatility from "./screens/volatility.js";
import * as activity from "./screens/activity.js";
import * as backtest from "./screens/backtest.js";
import * as settings from "./screens/settings.js";
import * as learning from "./screens/learning.js";

applyTheme();
watchSystem();

// ---------------------------------------------------------------- routes
const TABS = [
  { id: "home", label: "Home", icon: "home", mod: overview },
  { id: "ideas", label: "Ideas", icon: "ideas", mod: opportunities },
  { id: "portfolio", label: "Portfolio", icon: "portfolio", mod: positions },
  { id: "more", label: "More", icon: "more", mod: more },
];
const MORE_PAGES = { signals, volatility, news: activity, backtest, learning, settings };
const MORE_TITLES = Object.fromEntries([...more.PAGES.map((p) => [p.id, p.title]), ["settings", "Settings"]]);

// Old bookmarks (#/overview, #/opportunities, ...) map onto the new structure.
const LEGACY = {
  overview: ["home"], opportunities: ["ideas"], positions: ["portfolio"], activity: ["portfolio", "activity"],
  signals: ["more", "signals"], volatility: ["more", "volatility"], news: ["more", "news"],
  backtest: ["more", "backtest"], settings: ["more", "settings"], learning: ["more", "learning"],
};

function parseHash() {
  const raw = (location.hash || "").replace(/^#\/?/, "");
  let parts = raw.split("?")[0].split("/").filter(Boolean).map((p) => { try { return decodeURIComponent(p); } catch { return p; } });
  let rewrite = false;
  if (parts[0] && LEGACY[parts[0]]) { parts = [...LEGACY[parts[0]], ...parts.slice(1)]; rewrite = true; }
  let tab = TABS.find((t) => t.id === parts[0]);
  if (!tab) { tab = TABS[0]; parts = ["home"]; rewrite = rewrite || !!raw; }
  let route;
  if (tab.id === "more" && parts[1] && MORE_PAGES[parts[1]]) {
    route = { tab, depth: 1, key: `more/${parts[1]}`, mod: MORE_PAGES[parts[1]], params: parts.slice(2), title: MORE_TITLES[parts[1]] || parts[1] };
  } else {
    if (tab.id === "more" && parts[1]) { parts = ["more"]; rewrite = true; }
    route = { tab, depth: 0, key: tab.id, mod: tab.mod, params: parts.slice(1), title: tab.label };
  }
  route.hash = "#/" + parts.map(encodeURIComponent).join("/");
  route.rewrite = rewrite;
  return route;
}

// ---------------------------------------------------------------- shell elements
const stage = document.getElementById("stage");
const banners = document.getElementById("banners");
const root = document.documentElement;
let current = null; // { route, el, page }
let stackFromApp = false;
const scrollPos = new Map();
let skipNextAnim = false;

function buildTabbar() {
  const items = TABS.map((t) => {
    const a = h("a", { href: `#/${t.id}`, class: "tab", "data-tab": t.id }, icon(t.icon, { size: 26, cls: "tab-ico" }), h("span", { class: "tab-lbl" }, t.label));
    a.addEventListener("click", (e) => {
      if (current && current.route.tab.id === t.id && current.route.depth === 0) {
        e.preventDefault();
        current.page.scroller.scrollTo({ top: 0, behavior: prefersReducedMotion() ? "auto" : "smooth" });
      }
    });
    return h("li", null, a);
  });
  return h("nav", { class: "tabbar", "aria-label": "Main" },
    h("p", { class: "rail-brand", "aria-hidden": "true" }, "KWANT", h("span", { class: "brand-edge" }, "TRADES")),
    h("ul", null, items));
}
const tabbar = buildTabbar();
document.getElementById("app").append(tabbar);

function markTab(route) {
  tabbar.querySelectorAll(".tab").forEach((a) => {
    if (a.dataset.tab === route.tab.id) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
}

// ---------------------------------------------------------------- pages
function unmount(el) {
  el.querySelectorAll(".cu").forEach((n) => { try { n._cleanup && n._cleanup(); } catch { /* ignore */ } });
}

function buildPage(route) {
  const meta = route.mod.meta || {};
  const back = route.depth ? { label: "More", href: "#/more", onclick: goBack } : null;
  const page = createPage({ title: route.title, back, eyebrow: meta.eyebrow ? meta.eyebrow() : null, key: route.key });
  try { route.mod.render(page.content, route.params, { trail: page.trail, page, route }); }
  catch (e) { console.error(e); page.content.append(h("p", { class: "state err" }, "This screen couldn't be displayed.")); }
  return page;
}

function goBack() {
  if (stackFromApp) history.back();
  else location.hash = "#/more";
}

const EASE = SHEET_EASE;
const DUR = 450;

function animateFallback(newEl, oldEl, dir) {
  const reduce = prefersReducedMotion();
  const done = () => { if (oldEl) { unmount(oldEl); oldEl.remove(); } };
  const opts = (d) => ({ duration: reduce ? 160 : d, easing: reduce ? "ease-out" : EASE, fill: "both" });
  if (!oldEl) return;
  if (reduce || dir === "tab") {
    const a = newEl.animate([{ opacity: 0, transform: reduce ? "none" : "scale(.985)" }, { opacity: 1, transform: "none" }], opts(300));
    oldEl.animate([{ opacity: 1 }, { opacity: 0 }], opts(180));
    a.onfinish = () => { done(); newEl.getAnimations().forEach((x) => x.cancel()); };
    return;
  }
  const dim = (el, from, to) => { const d = el.querySelector(".page-dim"); if (d) d.animate([{ opacity: from }, { opacity: to }], opts(DUR)); };
  if (dir === "push") {
    const a = newEl.animate([{ transform: "translateX(100%)" }, { transform: "none" }], opts(DUR));
    oldEl.animate([{ transform: "none" }, { transform: "translateX(-30%)" }], opts(DUR));
    dim(oldEl, 0, 1);
    a.onfinish = () => { done(); newEl.getAnimations().forEach((x) => x.cancel()); };
  } else {
    newEl.animate([{ transform: "translateX(-30%)" }, { transform: "none" }], opts(DUR));
    dim(newEl, 1, 0);
    const a = oldEl.animate([{ transform: "none" }, { transform: "translateX(100%)" }], opts(DUR));
    a.onfinish = () => { done(); newEl.getAnimations().forEach((x) => x.cancel()); };
  }
}

let activeVT = null;
let queue = Promise.resolve();

function navigate() { queue = queue.then(run).catch((e) => console.error(e)); }

async function run() {
  const next = parseHash();
  if (next.rewrite) history.replaceState(null, "", next.hash);
  closeSheet(true);
  const same = current && current.route.key === next.key;
  if (same && current.route.params.join("/") === next.params.join("/")) return;
  if (activeVT) { try { activeVT.skipTransition(); await activeVT.updateCallbackDone; } catch { /* ignore */ } }
  // drop leftovers from an interrupted fallback animation
  [...stage.children].forEach((c) => { if (!current || c !== current.el) { unmount(c); c.remove(); } });

  const old = current;
  let dir = "none";
  if (old && !same) dir = next.tab.id !== old.route.tab.id ? "tab" : next.depth > old.route.depth ? "push" : next.depth < old.route.depth ? "pop" : "tab";
  if (skipNextAnim) { dir = "none"; skipNextAnim = false; }
  if (dir === "push") stackFromApp = true; else if (dir !== "none") stackFromApp = false;

  const swap = (useVT) => {
    if (old) scrollPos.set(old.route.key, old.page.scroller.scrollTop);
    const page = buildPage(next);
    const dimmer = h("div", { class: "page-dim", "aria-hidden": "true" });
    page.el.append(dimmer);
    if (dir === "pop") stage.insertBefore(page.el, old.el); else stage.append(page.el);
    if (old) old.el.setAttribute("inert", "");
    current = { route: next, el: page.el, page };
    markTab(next);
    root.classList.toggle("has-detail", next.depth > 0);
    document.title = `${next.title} · Kwant Trades${isDemo() ? " (demo)" : ""}`;
    const y = scrollPos.get(next.key);
    if (y && dir !== "push") page.scroller.scrollTop = y;
    page.update();
    if (old) {
      if (useVT) { unmount(old.el); old.el.remove(); }
      else if (dir === "none") { unmount(old.el); old.el.remove(); }
      else animateFallback(page.el, old.el, dir);
      const h1 = page.el.querySelector("h1");
      if (h1) h1.focus({ preventScroll: true });
    }
  };

  if (old && dir !== "none" && typeof document.startViewTransition === "function" && !document.hidden) {
    root.classList.add(`vt-${dir}`);
    try {
      activeVT = document.startViewTransition(() => swap(true));
      const vt = activeVT;
      vt.finished.catch(() => {}).finally(() => { root.classList.remove("vt-push", "vt-pop", "vt-tab"); if (activeVT === vt) activeVT = null; });
      // If the browser never gets to render (throttled pane), don't let the router queue wait forever.
      const guard = setTimeout(() => { try { vt.skipTransition(); } catch { /* ignore */ } }, 1500);
      await vt.updateCallbackDone.catch(() => {});
      clearTimeout(guard);
      return;
    } catch (e) {
      root.classList.remove("vt-push", "vt-pop", "vt-tab");
      activeVT = null;
    }
  }
  swap(false);
}

// ---------------------------------------------------------------- swipe back (Home Screen app only)
function initSwipeBack() {
  const standalone = (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || navigator.standalone === true;
  if (!standalone) return; // in Safari the browser has its own back gesture: don't fight it
  root.classList.add("standalone");
  const edge = h("div", { class: "edge-swipe", "aria-hidden": "true" });
  document.body.append(edge);
  let st = null, raf = 0;
  const paint = () => {
    raf = 0;
    if (!st || !st.under) return;
    const w = stage.clientWidth || 1;
    st.top.style.transform = `translateX(${st.dx}px)`;
    const p = st.dx / w;
    st.under.el.style.transform = `translateX(${(-30 * (1 - p)).toFixed(2)}%)`;
    const d = st.under.el.querySelector(".page-dim"); if (d) d.style.opacity = String(1 - p);
  };
  const cleanupUnder = () => { if (st && st.under) { unmount(st.under.el); st.under.el.remove(); } };
  edge.addEventListener("pointerdown", (e) => {
    if (!current || current.route.depth < 1) return;
    st = { id: e.pointerId, x0: e.clientX, y0: e.clientY, dx: 0, top: current.el, under: null, samples: [[performance.now(), e.clientX]], on: false };
    edge.setPointerCapture(e.pointerId);
  });
  edge.addEventListener("pointermove", (e) => {
    if (!st || e.pointerId !== st.id) return;
    const dx = e.clientX - st.x0;
    if (!st.on) {
      if (Math.abs(dx) < 6) return;
      if (Math.abs(e.clientY - st.y0) > Math.abs(dx)) { st = null; return; }
      st.on = true;
      const parent = { tab: current.route.tab, depth: 0, key: current.route.tab.id, mod: current.route.tab.mod, params: [], title: current.route.tab.label };
      const page = buildPage(parent);
      page.el.append(h("div", { class: "page-dim", "aria-hidden": "true" }));
      page.el.setAttribute("inert", "");
      const y = scrollPos.get(parent.key); if (y) page.scroller.scrollTop = y;
      stage.insertBefore(page.el, st.top);
      st.under = { el: page.el };
      st.top.classList.add("swiping");
    }
    st.dx = Math.max(0, dx);
    const now = performance.now();
    st.samples.push([now, e.clientX]);
    while (st.samples.length > 2 && now - st.samples[0][0] > 100) st.samples.shift();
    if (!raf) raf = requestAnimationFrame(paint);
  });
  const end = (e) => {
    if (!st || e.pointerId !== st.id) return;
    const s0 = st; st = null;
    if (!s0.on) return;
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    const w = stage.clientWidth || 1;
    const a = s0.samples[0], b = s0.samples[s0.samples.length - 1];
    const v = b[0] > a[0] ? (b[1] - a[1]) / (b[0] - a[0]) : 0;
    const commit = e.type !== "pointercancel" && (s0.dx > w * 0.35 || v > 0.5);
    const o = { duration: 240, easing: EASE, fill: "forwards" };
    const under = s0.under.el;
    if (commit) {
      s0.top.animate([{ transform: `translateX(${s0.dx}px)` }, { transform: "translateX(100%)" }], o);
      under.animate([{ transform: `translateX(${-30 * (1 - s0.dx / w)}%)` }, { transform: "none" }], o);
      setTimeout(() => { skipNextAnim = true; s0.top.classList.remove("swiping"); goBack(); }, 250);
    } else {
      s0.top.animate([{ transform: `translateX(${s0.dx}px)` }, { transform: "none" }], o).onfinish = () => { s0.top.getAnimations().forEach((x) => x.cancel()); s0.top.style.transform = ""; };
      under.animate([{ transform: `translateX(${-30 * (1 - s0.dx / w)}%)` }, { transform: "translateX(-30%)" }], o).onfinish = () => { unmount(under); under.remove(); };
      s0.top.classList.remove("swiping");
    }
  };
  edge.addEventListener("pointerup", end);
  edge.addEventListener("pointercancel", end);
  void cleanupUnder;
}

// ---------------------------------------------------------------- banners (only when they matter)
const bn = { demo: null, kill: null, conn: null };
function buildBanners() {
  if (isDemo()) {
    bn.demo = h("div", { class: "banner demo", role: "note", "aria-label": "Demo data notice" },
      h("strong", null, "Demo mode"),
      h("span", { class: "demo-why" }, getDemoReason() ? `${getDemoReason()} Showing sample data, not live market quotes.` : "Sample data for illustration, not live market quotes."),
      location.search.includes("demo=1") ? h("a", { href: location.pathname, class: "demo-exit" }, "Exit demo") : null);
  }
  bn.kill = h("div", { class: "banner kill", role: "status" }, icon("shield", { size: 18 }), h("span", null, "Kill switch on. Trading is stopped for today."));
  bn.conn = h("div", { class: "banner conn", role: "status" }, h("span", { class: "spin", "aria-hidden": "true" }), h("span", null, "Reconnecting…"));
}
let bannerSig = "";
let wasOffline = false;
function drawBanners() {
  const st = store.data("status");
  const offline = store.failingFor() > 5000;
  const show = [bn.demo, st?.kill_switch ? bn.kill : null, offline ? bn.conn : null].filter(Boolean);
  const sig = show.map((b) => b.className).join("|");
  if (sig !== bannerSig) {
    bannerSig = sig;
    banners.replaceChildren(...show);
    root.classList.toggle("has-banner", show.length > 0);
  }
  if (wasOffline && !offline && store.failingFor() === 0) {
    store.refreshMany(["config", "ideas", "signals", "positions", "equity", "decisions", "news", "scan"]);
  }
  wasOffline = offline;
}

// ---------------------------------------------------------------- market state changes (announce + refresh)
let lastMarketState = null, lastMode = null;
function onStatus() {
  const st = store.data("status");
  if (st) {
    syncClock(st.now);
    const mode = st.running ? st.mode : "idle";
    if (lastMode && lastMode !== mode) announce(`Mode changed to ${mode}`);
    lastMode = mode;
    const state = st.market?.state;
    if (lastMarketState && state && lastMarketState !== state) {
      announce(MARKET_STATES[state] || state);
      if (state === "open") toast("Opening range is set. The entry window is open.");
      store.refreshMany(["status", "signals", "ideas"]);
    }
    if (state) lastMarketState = state;
  }
  drawBanners();
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
  store.define("learning", api.learning, { interval: 20000 });
  store.define("graduation", api.graduation, { interval: 20000 });

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
  // a stage change (graduation to Alpaca paper, or a safety fallback) is announced once
  let prevStage = null;
  store.subscribe("status", (st) => {
    const s2 = st?.data?.stage;
    if (prevStage && s2 && s2 !== prevStage) {
      const msg = s2 === "alpaca_paper" ? "Graduated: trading moved to your Alpaca paper (practice) account." : (st.data.stage_alert || "Moved back to the built-in paper simulator.");
      announce(msg); toast(msg, s2 === "alpaca_paper" ? "info" : "error");
      store.refreshMany(["graduation", "learning"]);
    }
    if (s2) prevStage = s2;
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
  let first = null;
  try { first = await init(); } catch (e) { first = null; }
  if (isDemo()) document.body.classList.add("is-demo");
  buildBanners();
  defineResources();
  if (first) store.set("status", first);
  store.subscribe("status", onStatus);
  onStatus();
  startPolling();
  setInterval(() => { if (!document.hidden) drawBanners(); }, 1000);
  document.addEventListener("visibilitychange", () => { document.body.classList.toggle("paused", document.hidden); });
  document.addEventListener("touchstart", () => {}, { passive: true }); // lets :active press feedback work on iOS
  window.addEventListener("hashchange", navigate);
  window.addEventListener("online", () => store.refreshMany(["status", "scan", "ideas", "signals", "positions"]));
  initSwipeBack();
  navigate();
}

window.addEventListener("unhandledrejection", (e) => { console.warn("Unhandled:", e.reason); e.preventDefault(); });
boot();

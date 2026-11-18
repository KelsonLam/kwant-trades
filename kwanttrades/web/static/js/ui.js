// iOS-style building blocks: icons, grouped lists, segmented control, bottom sheet, nav-bar pages with collapsing titles.
import { h, s, prefersReducedMotion } from "./dom.js";

export const SHEET_EASE = "cubic-bezier(0.32, 0.72, 0, 1)";
export const clamp01 = (v) => Math.max(0, Math.min(1, v));

// ---------------------------------------------------------------- icons
const ICONS = {
  home: "M3 11.5 12 4l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  ideas: "M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z",
  portfolio: "M4 8h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1zM9 8V6a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M3 13h18",
  chevron: "M9 6l6 6-6 6",
  back: "M15 5l-7 7 7 7",
  signals: "M3 12h4l3-8 4 16 3-8h4",
  volatility: "M2 12c2-6 4-6 6 0s4 6 6 0 4-6 6 0",
  news: "M5 4h11a2 2 0 0 1 2 2v13H7a2 2 0 0 1-2-2zM18 9h2a1 1 0 0 1 1 1v7a2 2 0 0 1-2 2M8 8h7M8 12h7M8 16h4",
  backtest: "M3 12a9 9 0 1 0 3-6.7M3 4v5h5M12 8v4l3 2",
  settings: "M4 7h9M17 7h3M4 17h3M11 17h9",
  appearance: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 3v18",
  signout: "M10 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h5M15 8l4 4-4 4M19 12H9",
  shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z",
  refresh: "M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5",
  check: "M5 12.5l4.5 4.5L19 7.5",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v5l3 2",
  paper: "M7 3h8l4 4v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM15 3v4h4M9 12h6M9 16h6",
  eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  pause: "M9 5v14M15 5v14",
  warn: "M12 4l9 16H3zM12 10v4M12 17v.5",
};

export function icon(name, { size = 22, cls = "" } = {}) {
  const svg = s("svg", { width: size, height: size, viewBox: "0 0 24 24", class: `ico ${cls}`, "aria-hidden": "true", focusable: "false" });
  if (name === "more") {
    [5, 12, 19].forEach((cx) => svg.appendChild(s("circle", { cx, cy: 12, r: 1.7, class: "ico-fill" })));
    return svg;
  }
  svg.appendChild(s("path", { d: ICONS[name] || "" }));
  if (name === "settings") { svg.appendChild(s("circle", { cx: 15, cy: 7, r: 2 })); svg.appendChild(s("circle", { cx: 9, cy: 17, r: 2 })); }
  if (name === "appearance") svg.appendChild(s("path", { d: "M12 3a9 9 0 0 1 0 18z", class: "ico-fill" }));
  return svg;
}

// ---------------------------------------------------------------- grouped lists
/** iOS inset grouped list. items: <li> nodes built with row(). */
export function group(items, { header = null, footer = null, cls = "", label = null } = {}) {
  const list = h("ul", { class: `group ${cls}`, "aria-label": label || (typeof header === "string" ? header : null) }, items.filter(Boolean));
  return h("div", { class: "group-wrap" },
    header ? h("h3", { class: "group-head" }, header) : null,
    list,
    footer ? h("p", { class: "group-foot" }, footer) : null);
}

/**
 * One list cell. Pass href or onclick to make it tappable (>= 44px, press feedback, optional chevron).
 * leading/trailing/title/sub accept strings or nodes.
 */
export function row({ leading = null, title, sub = null, trailing = null, chevron = false, href = null, onclick = null, cls = "", label = null, expanded = null, haspopup = null }) {
  const main = h("span", { class: "cell-main" }, h("span", { class: "cell-title" }, title), sub ? h("span", { class: "cell-sub" }, sub) : null);
  const inner = [leading ? h("span", { class: "cell-lead" }, leading) : null, main,
    trailing ? h("span", { class: "cell-trail" }, trailing) : null,
    chevron ? h("span", { class: "cell-chev" }, icon("chevron", { size: 16 })) : null];
  let el;
  if (href) el = h("a", { class: "row-main", href, "aria-label": label }, ...inner);
  else if (onclick) {
    el = h("button", { type: "button", class: "row-main", "aria-label": label, "aria-expanded": expanded, "aria-haspopup": haspopup }, ...inner);
    el.addEventListener("click", onclick);
  } else el = h("div", { class: "row-main static", "aria-label": label }, ...inner);
  return h("li", { class: `row ${cls}` }, el);
}

export function sectionHead(title, action = null) {
  return h("div", { class: "section-head" }, h("h2", null, title), action);
}

// ---------------------------------------------------------------- segmented control
/** Sliding-thumb segmented control (radiogroup with roving tabindex + arrow keys). */
export function segmented({ options, value, onChange, label }) {
  const el = h("div", { class: "seg", role: "radiogroup", "aria-label": label });
  el.style.setProperty("--n", String(options.length));
  const thumb = h("span", { class: "seg-thumb", "aria-hidden": "true" });
  el.append(thumb);
  const btns = options.map(([v, text]) => {
    const b = h("button", { type: "button", role: "radio", class: "seg-btn", "data-v": v }, text);
    b.addEventListener("click", () => { if (cur !== v) { set(v); onChange && onChange(v); } });
    el.append(b);
    return b;
  });
  let cur = null;
  function set(v) {
    cur = v;
    const i = Math.max(0, options.findIndex((o) => o[0] === v));
    el.style.setProperty("--i", String(i));
    btns.forEach((b) => { const on = b.dataset.v === v; b.setAttribute("aria-checked", String(on)); b.tabIndex = on ? 0 : -1; });
  }
  el.addEventListener("keydown", (e) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) return;
    e.preventDefault();
    const i = options.findIndex((o) => o[0] === cur);
    const dir = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : -1;
    const n = options[(i + dir + options.length) % options.length][0];
    set(n); onChange && onChange(n); btns.find((b) => b.dataset.v === n).focus();
  });
  set(value);
  el.set = set;
  return el;
}

// ---------------------------------------------------------------- small helpers
export function spinner() { return h("span", { class: "spin", "aria-hidden": "true" }); }

export function bar(ratio, { level = "ok", label = "" } = {}) {
  const fill = h("span", { class: "bar-fill" });
  fill.style.transform = `scaleX(${clamp01(ratio)})`;
  return h("span", { class: `bar ${level}`, role: "img", "aria-label": label }, fill);
}

// ---------------------------------------------------------------- bottom sheet
let active = null;

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"])';

export function closeSheet(immediate = false) { if (active) active.close(immediate); }
export const sheetOpen = () => !!active;

/**
 * Open a bottom sheet. build(body, ctl) fills the scrollable body and may return a cleanup function.
 * Slides up with the iOS sheet curve; drag the grabber/header down to dismiss; Escape and backdrop tap close it;
 * focus is trapped while open and returned to the opener afterwards.
 */
export function openSheet({ title, build, onClose = null }) {
  if (active) active.close(true);
  const opener = document.activeElement;
  const app = document.getElementById("app");
  const titleId = "sheet-title";
  const backdrop = h("div", { class: "sheet-backdrop" });
  const done = h("button", { type: "button", class: "sheet-done" }, "Done");
  const head = h("header", { class: "sheet-head" }, h("span", { class: "sheet-spacer", "aria-hidden": "true" }), h("h2", { id: titleId, class: "sheet-title" }, title), done);
  const grab = h("div", { class: "sheet-grab" }, h("span", { class: "grabber", "aria-hidden": "true" }), head);
  const body = h("div", { class: "sheet-body" });
  const sheet = h("section", { class: "sheet", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId, tabindex: "-1" }, grab, body);
  const root = h("div", { class: "sheet-root" }, backdrop, sheet);
  let cleanup = null, closed = false, dragging = false;

  const ctl = { body, sheet, close: (imm) => close(imm), setTitle: (t) => { head.querySelector(".sheet-title").textContent = t; } };

  function close(immediate = false) {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey, true);
    if (cleanup) { try { cleanup(); } catch { /* ignore */ } }
    const finish = () => {
      root.remove();
      if (active === ctl) active = null;
      if (app) { app.inert = false; app.removeAttribute("aria-hidden"); }
      if (opener && opener.isConnected && typeof opener.focus === "function") { try { opener.focus({ preventScroll: true }); } catch { /* ignore */ } }
      if (onClose) onClose();
    };
    if (immediate) { finish(); return; }
    sheet.style.transition = "";
    sheet.style.transform = "";
    backdrop.style.opacity = "";
    root.classList.remove("open");
    root.classList.add("closing");
    let ended = false;
    const end = () => { if (ended) return; ended = true; finish(); };
    sheet.addEventListener("transitionend", (e) => { if (e.target === sheet && e.propertyName === "transform") end(); });
    setTimeout(end, prefersReducedMotion() ? 260 : 520);
  }

  function onKey(e) {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (e.key !== "Tab") return;
    const f = [...sheet.querySelectorAll(FOCUSABLE)].filter((n) => !n.closest("[hidden]") && n.offsetParent !== null);
    if (!f.length) { e.preventDefault(); sheet.focus(); return; }
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === sheet)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    else if (!sheet.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
  }

  backdrop.addEventListener("click", () => close());
  done.addEventListener("click", () => close());

  // ---- drag to dismiss (pointer events, rAF-driven, velocity-aware)
  let startY = 0, startX = 0, dy = 0, raf = 0, samples = [], pid = null;
  const paint = () => {
    raf = 0;
    sheet.style.transform = `translateY(${dy}px)`;
    backdrop.style.opacity = String(1 - clamp01(dy / Math.max(1, sheet.offsetHeight)) * 0.9);
  };
  grab.addEventListener("pointerdown", (e) => {
    if (e.button > 0 || e.target.closest("button")) return;
    startY = e.clientY; startX = e.clientX; dy = 0; samples = [[performance.now(), e.clientY]]; pid = e.pointerId; dragging = false;
    grab.setPointerCapture(pid);
  });
  grab.addEventListener("pointermove", (e) => {
    if (pid !== e.pointerId) return;
    const ddy = e.clientY - startY;
    if (!dragging) {
      if (Math.abs(ddy) < 4) return;
      dragging = true; sheet.classList.add("dragging");
    }
    dy = Math.max(0, ddy);
    const now = performance.now();
    samples.push([now, e.clientY]);
    while (samples.length > 2 && now - samples[0][0] > 100) samples.shift();
    if (!raf) raf = requestAnimationFrame(paint);
  });
  const release = (e) => {
    if (pid !== e.pointerId) return;
    pid = null;
    if (!dragging) return;
    dragging = false;
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    sheet.classList.remove("dragging");
    const a = samples[0], b = samples[samples.length - 1];
    const v = b && a && b[0] > a[0] ? (b[1] - a[1]) / (b[0] - a[0]) : 0; // px per ms
    if (e.type !== "pointercancel" && (dy > sheet.offsetHeight * 0.3 || v > 0.55)) {
      close(); // slides on from where the finger left it
    } else {
      sheet.style.transition = `transform .35s ${SHEET_EASE}`;
      sheet.style.transform = "";
      backdrop.style.opacity = "";
      setTimeout(() => { sheet.style.transition = ""; }, 380);
    }
  };
  grab.addEventListener("pointerup", release);
  grab.addEventListener("pointercancel", release);

  document.body.append(root);
  if (app) { app.inert = true; app.setAttribute("aria-hidden", "true"); }
  document.addEventListener("keydown", onKey, true);
  try { cleanup = build(body, ctl) || null; } catch (err) { console.warn("sheet build failed", err); body.append(h("p", { class: "state err" }, "This couldn't be displayed.")); }
  active = ctl;
  const show = () => { root.classList.add("open"); sheet.focus({ preventScroll: true }); };
  requestAnimationFrame(() => requestAnimationFrame(show));
  setTimeout(() => { if (!root.classList.contains("open") && !closed) show(); }, 120); // rAF doesn't fire in background tabs
  return ctl;
}

// ---------------------------------------------------------------- nav-bar pages (large title that collapses on scroll)
/**
 * Build a page: translucent nav bar (back button, small centred title, trailing slot) + scroller with the large title.
 * Returns { el, content, trail, scroller }. Scroll-driven effects touch only transform/opacity, in rAF.
 */
export function createPage({ title, large = true, back = null, eyebrow = null, key = "" }) {
  const trail = h("div", { class: "nb-trail" });
  const lead = h("div", { class: "nb-lead" });
  if (back) {
    const b = h("a", { class: "nb-back", href: back.href, "aria-label": `Back to ${back.label}` }, icon("back", { size: 22 }), h("span", null, back.label));
    if (back.onclick) b.addEventListener("click", (e) => { e.preventDefault(); back.onclick(); });
    lead.append(b);
  }
  const small = h("div", { class: "nb-title", "aria-hidden": "true" }, title);
  const bg = h("div", { class: "nb-bg" });
  const nav = h("div", { class: "navbar" }, bg, h("div", { class: "nb-row" }, lead, small, trail));
  const largeEl = h("h1", { class: "large-title", tabindex: "-1" }, title);
  const content = h("div", { class: "content" });
  const column = h("div", { class: "column" }, eyebrow, large ? largeEl : h("h1", { class: "sr-only", tabindex: "-1" }, title), content);
  const scroller = h("div", { class: "scroller" }, column);
  const el = h("section", { class: `page ${large ? "has-large" : "no-large"}`, "data-key": key, "aria-label": title }, scroller, nav);

  let raf = 0;
  const update = () => {
    raf = 0;
    const y = Math.max(0, scroller.scrollTop);
    const p = large ? clamp01((y - 22) / 18) : 1;
    small.style.opacity = String(p);
    small.style.transform = `translateY(${((1 - p) * 8).toFixed(1)}px)`;
    bg.style.opacity = String(clamp01(y / (large ? 12 : 6)));
    if (large) largeEl.style.opacity = String(1 - clamp01((y - 8) / 34) * 0.9);
  };
  scroller.addEventListener("scroll", () => { if (!raf) raf = requestAnimationFrame(update); }, { passive: true });
  update();
  return { el, content, trail, scroller, nav, update };
}

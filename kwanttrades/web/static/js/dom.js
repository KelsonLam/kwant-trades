// Safe DOM building, tooltips, dialogs, toasts, panels and small animation helpers.
// API strings are ALWAYS inserted as text nodes; innerHTML is never used with data.
import { GLOSSARY } from "./glossary.js";
import { tone, arrow, isNum } from "./format.js";

const SVG_NS = "http://www.w3.org/2000/svg";

function apply(el, attrs) {
  if (!attrs) return;
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.setAttribute("class", Array.isArray(v) ? v.filter(Boolean).join(" ") : v);
    else if (k === "text") el.textContent = String(v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "href") {
      // Only https URLs (or same-page hashes) ever become links.
      const s = String(v);
      if (s.startsWith("https://") || s.startsWith("#")) el.setAttribute("href", s);
    } else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

/** h("div", {class: "x"}, "text", child) */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) { children.unshift(attrs); attrs = null; }
  apply(el, attrs);
  append(el, children);
  return el;
}

export function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "text") el.textContent = String(v);
    else if (k === "class") el.setAttribute("class", Array.isArray(v) ? v.filter(Boolean).join(" ") : v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

export const prefersReducedMotion = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Append children, skipping null/false (Node.append would print "null"). */
export function put(el, ...children) { append(el, children); return el; }

/** Safe external link: only https:// URLs become anchors. */
export function extLink(url, text) {
  if (typeof url === "string" && url.startsWith("https://")) {
    return h("a", { href: url, target: "_blank", rel: "noopener noreferrer", class: "ext" }, text);
  }
  return h("span", null, text);
}

/** Signed value with arrow + tone class (colour is never the only cue). */
export function signed(text, v, extra = "") {
  return h("span", { class: `val ${tone(v)} ${extra}` }, h("span", { class: "arw", "aria-hidden": "true" }, arrow(v)), " ", text);
}

// ---------------------------------------------------------------- jargon tooltips
let tipEl = null, tipFor = null, tipPinned = false;
function ensureTip() {
  if (tipEl) return tipEl;
  tipEl = h("div", { id: "tip", role: "tooltip", class: "tip", hidden: true });
  document.body.appendChild(tipEl);
  return tipEl;
}

function showTip(target) {
  const key = target.dataset.term;
  const g = GLOSSARY[key];
  const text = g ? g.def : target.dataset.tip;
  if (!text) return;
  const tip = ensureTip();
  clear(tip);
  if (g) tip.appendChild(h("strong", null, g.name));
  tip.appendChild(h("span", null, text));
  tip.hidden = false;
  tipFor = target;
  target.setAttribute("aria-describedby", "tip");
  const r = target.getBoundingClientRect();
  const tw = Math.min(300, window.innerWidth - 24);
  tip.style.maxWidth = tw + "px";
  const tr = tip.getBoundingClientRect();
  let left = Math.min(Math.max(12, r.left + r.width / 2 - tr.width / 2), window.innerWidth - tr.width - 12);
  let top = r.bottom + 8;
  if (top + tr.height > window.innerHeight - 8) top = r.top - tr.height - 8;
  tip.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
  tip.classList.remove("in"); void tip.offsetWidth; tip.classList.add("in");
}
function hideTip() {
  if (!tipEl) return;
  tipEl.hidden = true;
  tipPinned = false;
  if (tipFor) tipFor.removeAttribute("aria-describedby");
  tipFor = null;
}

export function initTooltips() {
  const sel = "[data-term],[data-tip]";
  document.addEventListener("pointerover", (e) => {
    const t = e.target.closest?.(sel);
    if (t && e.pointerType === "mouse" && !tipPinned) showTip(t);
  });
  document.addEventListener("pointerout", (e) => {
    const t = e.target.closest?.(sel);
    if (t && e.pointerType === "mouse" && !tipPinned && !t.contains(e.relatedTarget)) hideTip();
  });
  document.addEventListener("focusin", (e) => { const t = e.target.closest?.(sel); if (t) showTip(t); });
  document.addEventListener("focusout", (e) => { if (e.target.closest?.(sel) && !tipPinned) hideTip(); });
  document.addEventListener("click", (e) => {
    const t = e.target.closest?.("button.term");
    if (t) { e.preventDefault(); if (tipFor === t && tipPinned) hideTip(); else { showTip(t); tipPinned = true; } return; }
    if (tipPinned) hideTip();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideTip(); });
  window.addEventListener("scroll", () => { if (tipFor) hideTip(); }, { passive: true, capture: true });
}

/** Inline jargon term: plain words shown, explanation on hover/focus/tap. */
export function term(key, label) {
  const g = GLOSSARY[key];
  return h("button", { type: "button", class: "term", "data-term": key }, label ?? (g ? g.name : key));
}

/** Plain label + technical term: "Chance of profit (POP)" */
export function label(plain, key, short) {
  return h("span", { class: "lbl" }, plain, " ", term(key, `(${short})`));
}

// ---------------------------------------------------------------- toasts / live region
let toastHost = null;
export function toast(message, kind = "info") {
  if (!toastHost) toastHost = document.getElementById("toasts");
  if (!toastHost) return;
  const t = h("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" }, String(message));
  toastHost.appendChild(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 400); }, 4200);
}

export function announce(msg) {
  const el = document.getElementById("live");
  if (el) { el.textContent = ""; setTimeout(() => { el.textContent = msg; }, 30); }
}

// ---------------------------------------------------------------- confirm dialog (iOS alert style)
export function confirmDialog({ title, body, confirmText = "Confirm", danger = false, cancelText = "Cancel" }) {
  return new Promise((resolve) => {
    const dlg = h("dialog", { class: "dlg", "aria-labelledby": "dlg-title" });
    let finished = false;
    const done = (v) => {
      if (finished) return; finished = true;
      dlg.classList.add("closing");
      setTimeout(() => { if (dlg.open) dlg.close(); dlg.remove(); resolve(v); }, prefersReducedMotion() ? 0 : 180);
    };
    const okBtn = h("button", { type: "button", class: `dlg-btn ${danger ? "danger" : "primary"}`, onclick: () => done(true) }, confirmText);
    dlg.append(
      h("div", { class: "dlg-body" },
        h("h2", { id: "dlg-title" }, title),
        ...(Array.isArray(body) ? body : [body]).map((b) => (b instanceof Node ? b : h("p", null, b)))),
      h("div", { class: "dlg-actions" }, h("button", { type: "button", class: "dlg-btn", onclick: () => done(false), autofocus: true }, cancelText), okBtn),
    );
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(false); });
    dlg.addEventListener("click", (e) => { if (e.target === dlg) done(false); });
    document.body.appendChild(dlg);
    dlg.showModal();
  });
}

// ---------------------------------------------------------------- panels with load/error/empty states
/** Panels that have already been drawn once this session: they never replay their entrance animation. */
const SEEN = new Set();
/** True only the first time a key is seen: gate entrance animations so they never replay on revisits. */
export function firstTime(key) { if (SEEN.has(key)) return false; SEEN.add(key); return true; }

/**
 * A section bound to a store key. render(data, {first}) returns a Node (or null for empty).
 * Handles: skeleton while loading, a plain-words error with Retry only when there has never been data,
 * a quiet "Updating…" hint while a refresh is failing (the last good data stays on screen), and an honest empty state.
 */
export function panel({ title, id, cls = "", store, key, render, empty, retry, actions, note, headless = false }) {
  const body = h("div", { class: "panel-body" });
  const head = headless ? null : h("header", { class: "panel-head" }, h("h2", null, title), actions || null);
  const el = h("section", { class: `panel cu ${cls}`, "aria-label": title }, head, note ? h("p", { class: "panel-note" }, note) : null, body);
  const seenKey = id || `${key}:${title}`;
  let lastSig = null;
  const draw = (force) => {
    const st = store.get(key);
    if (!st || (st.data == null && !st.error)) {
      if (!body.querySelector(".skeleton")) { clear(body); body.append(skeleton()); }
      return;
    }
    // Skip identical polls so open <details>, focus and form edits survive.
    let sig = null;
    try { sig = JSON.stringify(st.data) + "|" + (st.error || ""); } catch { sig = null; }
    if (force !== true && sig != null && sig === lastSig) return;
    lastSig = sig;
    if (st.error && st.data == null) {
      clear(body);
      const unreachable = st.errorKind === "network";
      body.append(h("div", { class: "state err", role: "status" },
        h("p", { class: "state-title" }, unreachable ? "Can't reach your PC right now." : `Something went wrong loading ${title.toLowerCase()}.`),
        h("p", { class: "muted small" }, unreachable ? "Check it's on and Tailscale is connected." : String(st.error)),
        h("button", { type: "button", class: "btn small", onclick: () => (retry ? retry() : store.refresh(key)) }, "Retry")));
      return;
    }
    const first = !SEEN.has(seenKey);
    let node = null;
    try { node = render(st.data, { first }); }
    catch (err) { console.warn("render failed", title, err); node = h("p", { class: "state err" }, "Something in this section couldn't be displayed."); }
    clear(body);
    if (node == null) body.append(emptyState(typeof empty === "function" ? empty(st.data) : empty));
    else body.append(node);
    // A failing refresh is not an error while we still have data: show a quiet hint and keep going.
    if (st.error) body.append(h("p", { class: "updating", role: "status" }, h("span", { class: "spin", "aria-hidden": "true" }), "Updating…"));
    if (node != null) SEEN.add(seenKey);
  };
  const unsub = store.subscribe(key, () => draw(false));
  draw(true);
  el._cleanup = unsub;
  el._redraw = () => draw(true);
  return el;
}

export function emptyState(msg) {
  const m = msg || { title: "Nothing here yet" };
  return h("div", { class: "state empty" },
    h("p", { class: "state-title" }, m.title || String(m)),
    m.hint ? h("p", { class: "muted small" }, m.hint) : null,
    m.action || null);
}

export function skeleton(lines = 3) {
  const w = h("div", { class: "skeleton", "aria-busy": "true", "aria-label": "Loading" });
  for (let i = 0; i < lines; i++) w.append(h("span", { class: `sk-line w${i}` }));
  return w;
}

// ---------------------------------------------------------------- animation helpers
const tweens = new WeakMap();
/** Tween a number displayed in `el` from its previous value to `to`. */
export function tweenNumber(el, to, fmt, { flash = true, dur = 700 } = {}) {
  const prev = tweens.get(el);
  const from = prev ? prev.value : null;
  if (prev && prev.raf) cancelAnimationFrame(prev.raf);
  const rec = { value: to, raf: 0 };
  tweens.set(el, rec);
  if (!isNum(to)) { el.textContent = fmt(to); return; }
  if (!isNum(from) || from === to || prefersReducedMotion() || document.hidden) { el.textContent = fmt(to); }
  else {
    const t0 = performance.now();
    const step = (t) => {
      const k = Math.min(1, (t - t0) / dur);
      const e = 1 - Math.pow(1 - k, 3);
      el.textContent = fmt(from + (to - from) * e);
      if (k < 1) rec.raf = requestAnimationFrame(step);
    };
    rec.raf = requestAnimationFrame(step);
  }
  if (flash && isNum(from) && Math.abs(to - from) >= 0.005) {
    el.classList.remove("flash-gain", "flash-loss"); void el.offsetWidth;
    el.classList.add(to > from ? "flash-gain" : "flash-loss");
  }
}

/** Mark children for staggered entrance (CSS handles the motion). */
export function stagger(children, base = 0, step = 45) {
  children.forEach((c, i) => { c.classList.add("enter"); c.style.setProperty("--d", `${base + i * step}ms`); });
  return children;
}

/** Chip like Allowed/Blocked with icon + text. */
export function decisionChip(passed) {
  if (passed == null) return h("span", { class: "chip neutral" }, "Not checked");
  return passed
    ? h("span", { class: "chip pass" }, h("span", { "aria-hidden": "true" }, "✓ "), "Allowed")
    : h("span", { class: "chip block" }, h("span", { "aria-hidden": "true" }, "✕ "), "Blocked");
}

export function flagChip(flag) {
  if (flag === "cheap") return h("span", { class: "flag cheap", "data-term": "cheap", tabindex: "0" }, "▼ cheap");
  if (flag === "rich") return h("span", { class: "flag rich", "data-term": "rich", tabindex: "0" }, "▲ rich");
  return null;
}

/** Horizontal meter: value vs cap. */
export function meter(value, cap, { label: lbl, fmt, invert = false } = {}) {
  const ratio = isNum(value) && isNum(cap) && cap > 0 ? Math.max(0, Math.min(1, value / cap)) : 0;
  const level = ratio >= 1 ? "max" : ratio >= 0.75 ? "hot" : "ok";
  const fill = h("span", { class: "meter-fill" });
  fill.style.transform = `scaleX(${ratio})`;
  return h("div", { class: `meter ${level}`, role: "meter", "aria-valuemin": "0", "aria-valuemax": String(isNum(cap) ? cap : 0), "aria-valuenow": String(isNum(value) ? value : 0), "aria-label": lbl },
    h("span", { class: "meter-track" }, fill),
    h("span", { class: "meter-text" }, fmt ? fmt(value, cap) : ""));
}

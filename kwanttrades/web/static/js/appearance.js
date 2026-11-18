// Appearance: "system" (default, follows the device), "light" or "dark". Persisted in localStorage (may be unavailable).
const KEY = "kwanttrades.theme";
const mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;

export function getPref() {
  let v = null;
  try { v = localStorage.getItem(KEY); } catch { v = null; }
  return v === "light" || v === "dark" ? v : "system";
}

export function resolve(pref = getPref()) {
  if (pref === "light" || pref === "dark") return pref;
  return mq && mq.matches ? "light" : "dark";
}

export function apply(pref = getPref()) {
  const t = resolve(pref);
  const root = document.documentElement;
  root.dataset.theme = t;
  root.dataset.themePref = pref;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", t === "light" ? "#f2f2f7" : "#000000");
  return t;
}

export function setPref(pref) {
  try { if (pref === "system") localStorage.removeItem(KEY); else localStorage.setItem(KEY, pref); } catch { /* storage unavailable */ }
  return apply(pref);
}

/** Re-apply when the device switches between light and dark while the preference is "system". */
export function watchSystem() {
  if (!mq) return;
  const on = () => { if (getPref() === "system") apply("system"); };
  if (mq.addEventListener) mq.addEventListener("change", on); else if (mq.addListener) mq.addListener(on);
}

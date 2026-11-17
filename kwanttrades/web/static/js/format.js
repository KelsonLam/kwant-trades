// Shared formatters. Every function is null-safe and returns "—" for unknown values.
export const DASH = "—";
const NY = "America/New_York";

export const isNum = (v) => typeof v === "number" && Number.isFinite(v);

const fmtCache = new Map();
function nf(opts) {
  const k = JSON.stringify(opts);
  if (!fmtCache.has(k)) fmtCache.set(k, new Intl.NumberFormat("en-US", opts));
  return fmtCache.get(k);
}

/** $1,234.56 (cents optional). */
export function money(v, { cents = true, compact = false } = {}) {
  if (!isNum(v)) return DASH;
  if (compact && Math.abs(v) >= 10000) return "$" + compactNum(v);
  const d = cents ? 2 : 0;
  return nf({ style: "currency", currency: "USD", minimumFractionDigits: d, maximumFractionDigits: d }).format(v);
}

/** +$12.30 / −$4.00 (true minus sign). */
export function signedMoney(v, opts = {}) {
  if (!isNum(v)) return DASH;
  const s = money(Math.abs(v), opts);
  if (Math.abs(v) < 0.005) return s;
  return (v > 0 ? "+" : "−") + s;
}

/** Per-share option price: 1.23 */
export function price(v, digits = 2) {
  if (!isNum(v)) return DASH;
  return nf({ minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v);
}

/** Decimal -> percent: 0.123 -> 12.3% */
export function pct(v, digits = 1) {
  if (!isNum(v)) return DASH;
  return nf({ minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v * 100) + "%";
}

export function signedPct(v, digits = 1) {
  if (!isNum(v)) return DASH;
  const s = pct(Math.abs(v), digits);
  if (Math.abs(v) < 0.5 * Math.pow(10, -digits - 2)) return s;
  return (v > 0 ? "+" : "−") + s;
}

/** Plain number with grouping. */
export function num(v, digits = 0) {
  if (!isNum(v)) return DASH;
  return nf({ minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v);
}

export function signedNum(v, digits = 2) {
  if (!isNum(v)) return DASH;
  const s = num(Math.abs(v), digits);
  return (v > 0 ? "+" : v < 0 ? "−" : "") + s;
}

export function compactNum(v) {
  if (!isNum(v)) return DASH;
  return nf({ notation: "compact", maximumFractionDigits: 1 }).format(v);
}

export function parseTime(iso) {
  if (!iso || typeof iso !== "string") return null;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : null;
}

/** 09:43 (NY) */
export function timeNY(iso, { seconds = false } = {}) {
  const t = typeof iso === "number" ? iso : parseTime(iso);
  if (t == null) return DASH;
  return new Intl.DateTimeFormat("en-US", {
    timeZone: NY, hour: "2-digit", minute: "2-digit", second: seconds ? "2-digit" : undefined, hour12: false,
  }).format(t);
}

/** Nov 13 */
export function dateNY(iso, { weekday = false, year = false } = {}) {
  const t = typeof iso === "number" ? iso : parseTime(iso);
  if (t == null) return DASH;
  return new Intl.DateTimeFormat("en-US", {
    timeZone: NY, month: "short", day: "numeric", weekday: weekday ? "short" : undefined, year: year ? "numeric" : undefined,
  }).format(t);
}

export function dateTimeNY(iso) {
  const t = parseTime(iso);
  if (t == null) return DASH;
  return dateNY(t) + " " + timeNY(t);
}

/** "YYYY-MM-DD" (no time zone conversion) -> "Nov 21" */
export function ymd(s) {
  if (!s || typeof s !== "string" || !/^\d{4}-\d{2}-\d{2}/.test(s)) return DASH;
  const [y, m, d] = s.slice(0, 10).split("-").map(Number);
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" }).format(Date.UTC(y, m - 1, d));
}

/** Days from `nowMs` to a YYYY-MM-DD expiry (calendar days, NY date). */
export function daysUntil(s, nowMs) {
  if (!s || typeof s !== "string") return null;
  const [y, m, d] = s.slice(0, 10).split("-").map(Number);
  if (!y) return null;
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: NY, year: "numeric", month: "2-digit", day: "2-digit" }).format(nowMs);
  const [ty, tm, td] = today.split("-").map(Number);
  return Math.round((Date.UTC(y, m - 1, d) - Date.UTC(ty, tm - 1, td)) / 86400000);
}

/** "3 min ago", "in 2 h" */
export function relTime(iso, nowMs = Date.now()) {
  const t = typeof iso === "number" ? iso : parseTime(iso);
  if (t == null) return DASH;
  const s = Math.round((t - nowMs) / 1000);
  const a = Math.abs(s);
  let v, u;
  if (a < 45) return s <= 0 ? "just now" : "in a moment";
  if (a < 3600) { v = Math.round(a / 60); u = "min"; }
  else if (a < 86400) { v = Math.round(a / 3600); u = "h"; }
  else { v = Math.round(a / 86400); u = v === 1 ? "day" : "days"; }
  return s < 0 ? `${v} ${u} ago` : `in ${v} ${u}`;
}

/** mm:ss or h:mm:ss from milliseconds */
export function clock(ms) {
  if (!isNum(ms)) return DASH;
  const t = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const p = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
}

/** "2 h 14 m" */
export function duration(ms) {
  if (!isNum(ms)) return DASH;
  const m = Math.max(0, Math.round(ms / 60000));
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${m % 60} m`;
  return `${Math.round(h / 24)} days`;
}

/** sign class for gain/loss styling */
export function tone(v) {
  if (!isNum(v) || Math.abs(v) < 0.005) return "flat";
  return v > 0 ? "gain" : "loss";
}

export function arrow(v) {
  if (!isNum(v) || Math.abs(v) < 0.005) return "◆"; // diamond for flat
  return v > 0 ? "▲" : "▼";
}

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export function titleCase(s) {
  if (typeof s !== "string" || !s) return DASH;
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

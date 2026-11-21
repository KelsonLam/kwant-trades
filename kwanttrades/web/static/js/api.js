// Small API client. LIVE mode talks to /api/* (relative URLs); DEMO mode uses the mock layer.
// Resilience: a GET that fails at the network level (dropped connection, timeout) is retried twice with a short
// backoff before an error is reported. HTTP errors (4xx/5xx) are never retried. POSTs are never retried because a
// timed-out POST may already have been applied by the server.
let demo = false;
let demoReason = "";
const STATIC_DEMO_HOST = /\.github\.io$/i;
let remote = false;
let mockModule = null;

export const isDemo = () => demo;
export const getDemoReason = () => demoReason;
export const isRemote = () => remote;

const RETRY_DELAYS = [300, 900];
const TIMEOUT_MS = 10000;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function toLogin(expired) {
  location.replace(expired ? "/login.html?expired=1" : "/login.html");
}

async function loadMock() {
  if (!mockModule) mockModule = await import("./mock.js");
  return mockModule;
}

function apiError(message, kind) {
  const e = new Error(message);
  e.kind = kind; // "network" = couldn't reach the server; "server" = it answered with an error
  return e;
}

async function attempt(method, path, body) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json", Accept: "application/json" } : { Accept: "application/json" },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch (e) {
    throw apiError(e && e.name === "AbortError" ? "The server took too long to answer." : "Can't reach the Kwant Trades server.", "network");
  } finally {
    clearTimeout(timer);
  }
  let json = null;
  try { json = await res.json(); } catch { json = null; }
  if (res.status === 401 && path !== "/api/login") {
    // Remote mode: the session ended (idle, expired, server restarted). Send the user to the sign-in screen.
    toLogin(true);
    return new Promise(() => {}); // the page is navigating away; never resolve
  }
  if (!res.ok) {
    const msg = json && typeof json.error === "string" ? json.error : `Server error (${res.status})`;
    throw apiError(msg, "server");
  }
  if (json == null) throw apiError("The server sent an unreadable reply.", "server");
  return json;
}

async function liveRequest(method, path, body) {
  const retries = method === "GET" ? RETRY_DELAYS.length : 0;
  for (let i = 0; ; i++) {
    try {
      return await attempt(method, path, body);
    } catch (e) {
      if (e.kind !== "network" || i >= retries) throw e;
      await sleep(RETRY_DELAYS[i]);
    }
  }
}

export async function request(method, path, body) {
  if (demo) {
    const m = await loadMock();
    return m.handle(method, path, body);
  }
  return liveRequest(method, path, body);
}

export const get = (path) => request("GET", path);
export const post = (path, body = {}) => request("POST", path, body);

/** Decide LIVE vs DEMO. Returns the first status payload. */
export async function init() {
  const params = new URLSearchParams(location.search);
  // The hosted demo site (GitHub Pages) has no server behind it, so it always shows sample data.
  if (params.get("demo") === "1" || STATIC_DEMO_HOST.test(location.hostname)) {
    demo = true;
    demoReason = "";
  } else if (location.protocol === "file:") {
    // Opened straight from disk: there is no server to probe (the probe would only log a failed request).
    demo = true;
    demoReason = "Local file";
  } else {
    try {
      // /api/session doubles as the "is there a server?" probe, so a static-only host costs one failed (retried) request.
      const sess = await liveRequest("GET", "/api/session");
      remote = !!sess.remote;
      if (remote && !sess.authenticated) { toLogin(false); return new Promise(() => {}); }
      return await liveRequest("GET", "/api/status");
    } catch (e) {
      demo = true;
      demoReason = "Server offline";
    }
  }
  return request("GET", "/api/status");
}

export async function signOut() {
  try { await liveRequest("POST", "/api/logout", {}); } catch { /* already signed out */ }
  toLogin(false);
}

export const api = {
  status: () => get("/api/status"),
  scan: (symbols = null) => post("/api/scan", { symbols }),
  scanStatus: () => get("/api/scan/status"),
  ideas: () => get("/api/ideas"),
  signals: () => get("/api/signals"),
  surface: (sym) => get(`/api/surface?symbol=${encodeURIComponent(sym)}`),
  positions: () => get("/api/positions"),
  closePosition: (id) => post(`/api/positions/${encodeURIComponent(id)}/close`, {}),
  equity: () => get("/api/equity"),
  decisions: (limit = 100) => get(`/api/decisions?limit=${limit}`),
  news: (sym = "ALL") => get(`/api/news?symbol=${encodeURIComponent(sym)}`),
  config: () => get("/api/config"),
  saveConfig: (partial) => post("/api/config", partial),
  start: (mode) => post("/api/session/start", { mode }),
  stop: () => post("/api/session/stop", {}),
  flatten: () => post("/api/session/flatten", {}),
  backtest: (body) => post("/api/backtest", body),
  backtestResult: () => get("/api/backtest/result"),
  learning: () => get("/api/learning"),
  graduation: () => get("/api/graduation"),
  learningFreeze: (frozen) => post("/api/learning/freeze", { frozen: !!frozen }),
  learningRevert: (changeId) => post("/api/learning/revert", { change_id: changeId }),
  learningReset: () => post("/api/learning/reset", {}),
};

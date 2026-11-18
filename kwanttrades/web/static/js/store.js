// Tiny store + poller. Each key has a fetcher and a polling interval.
// Polling pauses while the tab is hidden; fetch failures are stored, never thrown, and the last good data is kept.
const entries = new Map(); // key -> {fetch, interval, always, state, subs:Set, inflight, lastTry}

// Connection health, driven by the always-polled "status" resource.
let failingSince = 0;

function entry(key) {
  if (!entries.has(key)) entries.set(key, { fetch: null, interval: 0, always: false, state: null, subs: new Set(), inflight: null, lastTry: 0 });
  return entries.get(key);
}

function emit(key) {
  const e = entry(key);
  for (const fn of [...e.subs]) {
    try { fn(e.state); } catch (err) { console.warn("subscriber failed", key, err); }
  }
}

export const store = {
  /** Register a polled resource. interval may be a number (ms) or a function returning ms. */
  define(key, fetcher, { interval = 15000, always = false } = {}) {
    const e = entry(key);
    e.fetch = fetcher; e.interval = interval; e.always = always;
  },
  get(key) { return entry(key).state; },
  data(key) { const s = entry(key).state; return s ? s.data : null; },
  set(key, data) {
    const e = entry(key);
    e.state = { data, error: null, errorKind: null, at: Date.now() };
    if (key === "status") failingSince = 0;
    emit(key);
  },
  subscribe(key, fn) {
    const e = entry(key);
    e.subs.add(fn);
    if (!e.state && e.fetch && !e.inflight) store.refresh(key);
    return () => e.subs.delete(fn);
  },
  /** Fetch now. Resolves to the data, or null on failure (error stored in state, last good data kept). */
  refresh(key) {
    const e = entry(key);
    if (!e.fetch) return Promise.resolve(null);
    if (e.inflight) return e.inflight;
    e.lastTry = Date.now();
    e.inflight = (async () => {
      try {
        const data = await e.fetch();
        e.state = { data, error: null, errorKind: null, at: Date.now() };
        if (key === "status") failingSince = 0;
      } catch (err) {
        const msg = (err && err.message) || "Unknown error";
        e.state = { data: e.state ? e.state.data : null, error: msg, errorKind: (err && err.kind) || "server", at: Date.now() };
        if (key === "status" && !failingSince) failingSince = Date.now();
      } finally {
        e.inflight = null;
      }
      emit(key);
      return e.state.data;
    })();
    return e.inflight;
  },
  refreshMany(keys) { return Promise.all(keys.map((k) => store.refresh(k))); },
  /** Milliseconds the connection has been failing (0 when healthy). */
  failingFor() { return failingSince ? Date.now() - failingSince : 0; },
};

let timer = 0;
function tick() {
  timer = 0;
  if (!document.hidden) {
    const now = Date.now();
    for (const [key, e] of entries) {
      if (!e.fetch || e.inflight) continue;
      if (!e.always && e.subs.size === 0) continue;
      const iv = typeof e.interval === "function" ? e.interval() : e.interval;
      if (!iv) continue;
      if (now - e.lastTry >= iv) store.refresh(key);
    }
  }
  timer = setTimeout(tick, 500);
}

export function startPolling() {
  if (!timer) tick();
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      // Coming back: refresh what's visible right away.
      for (const [key, e] of entries) if (e.fetch && (e.always || e.subs.size)) store.refresh(key);
    }
  });
}

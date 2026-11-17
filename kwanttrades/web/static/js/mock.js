// DEMO data layer. Produces internally consistent, contract-shaped responses from a tiny
// simulated market: seeded 1-minute price paths, a parametric volatility smile, Black-Scholes
// option prices, and a paper account. NOTHING here is a real quote or a real result.

// ---------------------------------------------------------------- seeded randomness
function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function hash(str) { let h = 2166136261; for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
function gauss(rng) { let u = 0, v = 0; while (u === 0) u = rng(); while (v === 0) v = rng(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); }
const noiseAt = (key) => gauss(mulberry32(hash(key)));
const r2 = (x) => Math.round(x * 100) / 100;
const r4 = (x) => Math.round(x * 10000) / 10000;

// ---------------------------------------------------------------- New York time helpers
function nyOffsetMin(ms) {
  const part = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", timeZoneName: "shortOffset" })
    .formatToParts(ms).find((p) => p.type === "timeZoneName");
  const m = part && part.value.match(/GMT([+-])(\d{1,2})(?::(\d{2}))?/);
  if (!m) return -300;
  return (m[1] === "-" ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3] || 0));
}
function nyParts(ms) {
  const f = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, weekday: "short" });
  const o = {}; for (const p of f.formatToParts(ms)) o[p.type] = p.value;
  return { y: +o.year, m: +o.month, d: +o.day, hh: +o.hour % 24, mm: +o.minute, ss: +o.second, wd: o.weekday };
}
function nyEpoch(y, m, d, hh = 0, mm = 0, ss = 0) {
  const guess = Date.UTC(y, m - 1, d, hh, mm, ss);
  return guess - nyOffsetMin(guess) * 60000;
}
function iso(ms) {
  const p = nyParts(ms); const off = nyOffsetMin(ms);
  const z = (n, w = 2) => String(Math.abs(n)).padStart(w, "0");
  return `${p.y}-${z(p.m)}-${z(p.d)}T${z(p.hh)}:${z(p.mm)}:${z(p.ss)}${off < 0 ? "-" : "+"}${z(Math.trunc(Math.abs(off) / 60))}:${z(Math.abs(off) % 60)}`;
}
function ymd(y, m, d) { const t = new Date(Date.UTC(y, m - 1, d)); return t.toISOString().slice(0, 10); }
function addDays(ymdStr, n) { const t = new Date(ymdStr + "T00:00:00Z"); t.setUTCDate(t.getUTCDate() + n); return t.toISOString().slice(0, 10); }
function weekday(ymdStr) { return new Date(ymdStr + "T00:00:00Z").getUTCDay(); }
function prevWeekday(ymdStr) { let d = addDays(ymdStr, -1); while ([0, 6].includes(weekday(d))) d = addDays(d, -1); return d; }
function nextWeekday(ymdStr) { let d = addDays(ymdStr, 1); while ([0, 6].includes(weekday(d))) d = addDays(d, 1); return d; }
function ymdParts(s) { const [y, m, d] = s.split("-").map(Number); return [y, m, d]; }
const at = (day, hh, mm = 0, ss = 0) => nyEpoch(...ymdParts(day), hh, mm, ss);

// ---------------------------------------------------------------- the simulated session
const realStart = Date.now();
const todayNY = (() => { const p = nyParts(realStart); return ymd(p.y, p.m, p.d); })();
const SESSION = [0, 6].includes(weekday(todayNY)) ? prevWeekday(todayNY) : todayNY;
const PREV = prevWeekday(SESSION);
const OPEN = at(SESSION, 9, 30), OR_END = at(SESSION, 9, 45), CLOSE = at(SESSION, 16, 0);
// Demo clock starts just before the opening range ends; `?demo=1&demoTime=HH:MM` picks another start.
const demoStart = (() => {
  const m = (globalThis.location && new URLSearchParams(globalThis.location.search).get("demoTime") || "").match(/^(\d{1,2}):(\d{2})$/);
  return m ? at(SESSION, Math.min(23, +m[1]), Math.min(59, +m[2])) : at(SESSION, 9, 42, 30);
})();
const now = () => demoStart + (Date.now() - realStart);
const minuteIdx = (t) => (t - OPEN) / 60000;

function fridayOnOrAfter(day, minDays) {
  let d = addDays(day, minDays);
  while (weekday(d) !== 5) d = addDays(d, 1);
  return d;
}
const EXP_WEEK = fridayOnOrAfter(SESSION, 4);
const EXP_MONTH = fridayOnOrAfter(SESSION, 27);
const EXP_LATE = fridayOnOrAfter(SESSION, 41);
const dteOf = (exp, t = now()) => Math.max(0.5 / 24, (at(exp, 16) - t) / 86400000);

// symbol parameters: spot, ATM IV, our forecast vol, intraday trend after the opening range, news tone
const SYMS = {
  SPY: { spot: 598.4, iv: 0.142, rv: 0.118, trend: 0.0016, gap: 0.0021, news: 0.15, skew: 0.9 },
  QQQ: { spot: 512.7, iv: 0.185, rv: 0.152, trend: 0.0004, gap: 0.0012, news: 0.05, skew: 0.8 },
  IWM: { spot: 228.3, iv: 0.215, rv: 0.204, trend: -0.0002, gap: -0.0009, news: -0.05, skew: 0.7 },
  AAPL: { spot: 231.6, iv: 0.238, rv: 0.198, trend: 0.0011, gap: 0.0034, news: 0.35, skew: 0.6 },
  MSFT: { spot: 418.2, iv: 0.224, rv: 0.211, trend: 0.0003, gap: 0.0008, news: 0.1, skew: 0.6 },
  NVDA: { spot: 142.35, iv: 0.468, rv: 0.552, trend: 0.0042, gap: 0.0118, news: 0.55, skew: 0.4 },
  AMZN: { spot: 205.1, iv: 0.302, rv: 0.287, trend: -0.0008, gap: -0.0021, news: -0.1, skew: 0.5 },
  META: { spot: 576.9, iv: 0.336, rv: 0.281, trend: -0.0036, gap: -0.0094, news: -0.45, skew: 0.5 },
  GOOGL: { spot: 172.4, iv: 0.281, rv: 0.27, trend: 0.0002, gap: 0.0004, news: 0.0, skew: 0.5 },
  TSLA: { spot: 312.8, iv: 0.598, rv: 0.512, trend: -0.0051, gap: -0.0148, news: -0.4, skew: 0.3 },
  AMD: { spot: 138.2, iv: 0.472, rv: 0.611, trend: 0.0009, gap: 0.0026, news: 0.2, skew: 0.4 },
};
const UNIVERSE = Object.keys(SYMS);
const RATE = 0.043;

// one-minute paths for the session (prev close -> open gap -> intraday walk with trend)
const PATHS = {};
for (const sym of UNIVERSE) {
  const p = SYMS[sym];
  const rng = mulberry32(hash("path:" + sym + SESSION));
  const sig = p.rv / Math.sqrt(252 * 390) * 1.2;
  const out = new Float64Array(391);
  let x = p.spot * (1 + p.gap);
  for (let i = 0; i <= 390; i++) {
    const tr = i > 15 ? p.trend / 120 : 0;
    const shape = i < 15 ? 1.8 : 1;
    x *= Math.exp(tr + sig * shape * gauss(rng));
    out[i] = x;
  }
  PATHS[sym] = { prevClose: p.spot, closes: out };
}
function spotAt(sym, t = now()) {
  const P = PATHS[sym];
  const i = minuteIdx(t);
  if (i <= 0) return P.closes[0];
  if (i >= 390) return P.closes[390];
  const a = Math.floor(i), f = i - a;
  const wiggle = 1 + 0.00025 * Math.sin(t / 2300 + hash(sym) % 7);
  return (P.closes[a] * (1 - f) + P.closes[Math.min(390, a + 1)] * f) * wiggle;
}

// ---------------------------------------------------------------- Black-Scholes
function ncdf(x) {
  const k = 1 / (1 + 0.2316419 * Math.abs(x));
  const w = 1 - 0.3989422804 * Math.exp(-x * x / 2) * k * (0.31938153 + k * (-0.356563782 + k * (1.781477937 + k * (-1.821255978 + k * 1.330274429))));
  return x >= 0 ? w : 1 - w;
}
const npdf = (x) => Math.exp(-x * x / 2) / Math.sqrt(2 * Math.PI);
function bs(S, K, T, sig, cp) {
  if (T <= 0 || sig <= 0) return Math.max(0, cp * (S - K));
  const sq = sig * Math.sqrt(T);
  const d1 = (Math.log(S / K) + (RATE + sig * sig / 2) * T) / sq, d2 = d1 - sq;
  const disc = Math.exp(-RATE * T);
  const price = cp === 1 ? S * ncdf(d1) - K * disc * ncdf(d2) : K * disc * ncdf(-d2) - S * ncdf(-d1);
  const delta = cp === 1 ? ncdf(d1) : ncdf(d1) - 1;
  const gamma = npdf(d1) / (S * sq);
  const vega = S * npdf(d1) * Math.sqrt(T) / 100;
  const theta = (-(S * npdf(d1) * sig) / (2 * Math.sqrt(T)) - cp * RATE * K * disc * ncdf(cp * d2)) / 365;
  return { price, delta, gamma, vega, theta };
}

// ---------------------------------------------------------------- option chains with a smile
const stepOf = (S) => (S < 100 ? 1 : S < 260 ? 2.5 : 5);
function smileIV(sym, k, exp) {
  const p = SYMS[sym];
  const T = dteOf(exp) / 365;
  const term = 1 + 0.06 * Math.log(Math.max(T, 0.01) / 0.08);
  return Math.max(0.05, p.iv * term * (1 - p.skew * 0.9 * k + 2.4 * k * k));
}
function occ(sym, exp, right, K) {
  return `${sym}${exp.slice(2, 4)}${exp.slice(5, 7)}${exp.slice(8, 10)}${right}${String(Math.round(K * 1000)).padStart(8, "0")}`;
}
/** A single quote. Deterministic per (symbol, expiry, strike, right): flags don't flicker. */
function quote(sym, exp, right, K, t = now()) {
  const S = spotAt(sym, t);
  const T = dteOf(exp, t) / 365;
  const F = S * Math.exp(RATE * T);
  const k = Math.log(K / F);
  const fit = smileIV(sym, k, exp);
  const n = noiseAt(`${sym}|${exp}|${K}|${right}`);
  const cp = right === "C" ? 1 : -1;
  const g0 = bs(S, K, T, fit, cp);
  const half0 = Math.max(0.01, Math.min(0.5, Math.max(0.01, g0.price) * 0.025 + 0.012 + Math.abs(k) * 0.05));
  const halfIv0 = half0 / Math.max(1e-4, g0.vega * 100);
  // a few genuinely off-curve strikes (about 2.6 half-spreads away), the rest close to the fit
  const spike = Math.abs(n) > 1.5 ? Math.sign(n) * Math.min(0.03, 2.7 * halfIv0) : 0;
  const mkt = Math.max(0.04, fit + Math.max(-0.6, Math.min(0.6, n * 0.3)) * Math.min(0.01, halfIv0) + spike);
  const g = bs(S, K, T, mkt, cp);
  const mid = Math.max(0.01, g.price);
  const half = Math.max(0.01, Math.min(0.5, mid * 0.025 + 0.012 + Math.abs(k) * 0.05));
  const bid = Math.max(0, r2(mid - half)), ask = r2(mid + half);
  const vegaPts = Math.max(1e-4, g.vega * 100);
  const bidIv = Math.max(0.01, mkt - (mid - bid) / vegaPts), askIv = mkt + (ask - mid) / vegaPts;
  const halfIv = Math.max(1e-4, (askIv - bidIv) / 2);
  const z = (fit - mkt) / halfIv;
  let flag = null;
  if (bidIv > fit && Math.abs(z) >= 2) flag = "rich";
  else if (askIv < fit && Math.abs(z) >= 2) flag = "cheap";
  return { occ: occ(sym, exp, right, K), right, strike: K, expiry: exp, bid, ask, mid: r2((bid + ask) / 2), S, T, F, k, fit, mkt, bidIv, askIv, z, flag, greeks: g, halfIv };
}

// ---------------------------------------------------------------- signals
const WEIGHTS = { orb: 0.4, vwap: 0.15, gap: 0.05, news: 0.25, regime: 0.15 };
const REGIME = { direction: 0.32, confidence: 0.58, risk_multiplier: 0.85, detail: { spy_trend: "up (above 20 & 50-day averages)", vix: 15.8, vix_percentile_1y: 0.31, term_structure: "contango (calm)" } };

function orbOf(sym, t = now()) {
  const c = PATHS[sym].closes;
  const iNow = Math.floor(minuteIdx(t));
  if (iNow < 15) return { state: "waiting", or_high: null, or_low: null, or_mid: null, entry: null, stop: null, target: null, rel_volume: null, _dir: 0, _conf: 0, _forming: iNow >= 0 ? Array.from(c.slice(0, Math.max(1, iNow + 1))) : [] };
  let hi = -Infinity, lo = Infinity;
  for (let i = 0; i < 15; i++) { hi = Math.max(hi, c[i]); lo = Math.min(lo, c[i]); }
  hi *= 1.0006; lo *= 0.9994;
  const mid = (hi + lo) / 2;
  const relv = 0.9 + (hash("rv" + sym) % 100) / 100;
  let state = "waiting", entry = null, stop = null, target = null, dir = 0;
  for (let b = 20; b <= iNow; b += 5) {
    const close = c[b];
    if (state === "waiting") {
      if (close > hi) { state = "triggered"; dir = 1; entry = close; }
      else if (close < lo) { state = "triggered"; dir = -1; entry = close; }
      if (entry != null) { stop = mid; target = entry + (entry - stop); }
    } else if (state === "triggered") {
      if ((dir > 0 && close < mid) || (dir < 0 && close > mid)) { state = "failed"; dir = 0; break; }
      if ((dir > 0 && close >= target) || (dir < 0 && close <= target)) { state = "target_hit"; break; }
    }
  }
  const conf = state === "triggered" ? Math.min(0.9, 0.35 + (relv - 1) * 0.6) : state === "target_hit" ? 0.3 : 0;
  return { state, or_high: r2(hi), or_low: r2(lo), or_mid: r2(mid), entry: entry && r2(entry), stop: stop && r2(stop), target: target && r2(target), rel_volume: r2(relv), _dir: state === "failed" ? 0 : dir, _conf: conf };
}

function signalsFor(sym, t = now()) {
  const p = SYMS[sym];
  const S = spotAt(sym, t);
  const iNow = Math.max(0, Math.min(390, Math.floor(minuteIdx(t))));
  const c = PATHS[sym].closes;
  let vw = 0; for (let i = 0; i <= iNow; i++) vw += c[i]; vw /= iNow + 1;
  const orb = orbOf(sym, t);
  const comps = {
    orb: orb.state === "waiting" && iNow < 15 ? null : { direction: orb._dir, confidence: r2(orb._conf), detail: { state: orb.state, or_high: orb.or_high, or_low: orb.or_low } },
    vwap: { direction: r2(Math.max(-1, Math.min(1, (S - vw) / (S * 0.002)))), confidence: 0.35, detail: { vwap: r2(vw), upper: r2(vw * 1.003), lower: r2(vw * 0.997) } },
    gap: { direction: r2(Math.max(-1, Math.min(1, p.gap * 60))), confidence: Math.abs(p.gap) > 0.012 ? 0.2 : 0.45, detail: { gap_pct: r4(p.gap) } },
    news: { direction: p.news, confidence: Math.abs(p.news) > 0.3 ? 0.6 : 0.3, detail: { headlines: 3 + (hash(sym) % 6) } },
    regime: { direction: REGIME.direction, confidence: REGIME.confidence, detail: { risk_multiplier: REGIME.risk_multiplier } },
  };
  let num = 0, den = 0, wsum = 0, agree = 0;
  for (const [k, v] of Object.entries(comps)) {
    if (!v) continue;
    num += WEIGHTS[k] * v.confidence * v.direction; den += WEIGHTS[k] * v.confidence; wsum += WEIGHTS[k];
  }
  const dir = den ? num / den : 0;
  for (const [k, v] of Object.entries(comps)) if (v && Math.sign(v.direction) === Math.sign(dir) && v.direction !== 0) agree += WEIGHTS[k];
  const conf = wsum ? Math.min(1, den / wsum * 1.4) : 0;
  const atm = smileIV(sym, 0, EXP_MONTH);
  const comp = { ewma: p.rv * 1.04, har: p.rv * 0.97, garch: p.rv * 1.02, yang_zhang: p.rv * 0.95 };
  const spark = [];
  const n = iNow + 1, stepN = Math.max(1, Math.ceil(n / 80));
  for (let i = 0; i < n; i += stepN) spark.push(r2(c[i]));
  spark.push(r2(S));
  return {
    symbol: sym, spot: r2(S), change_pct: r4(S / PATHS[sym].prevClose - 1),
    composite: { direction: r2(dir), confidence: r2(conf), agreement: r2(wsum ? agree / wsum : 0) },
    components: comps,
    orb: iNow < 15 ? { state: "waiting", or_high: null, or_low: null, or_mid: null, entry: null, stop: null, target: null, rel_volume: null } : (({ _dir, _conf, _forming, ...o }) => o)(orb),
    vol: { atm_iv: r4(atm), forecast_rv: r4(p.rv), vol_edge: r4((p.rv - atm) / atm), components: Object.fromEntries(Object.entries(comp).map(([k, v]) => [k, r4(v)])) },
    sparkline: spark,
  };
}

// ---------------------------------------------------------------- the paper account
const START_EQUITY = 50000;
let prevCloseEquity = START_EQUITY; // set properly once the book is seeded
const CONFIG = {
  risk: { max_loss_per_trade_pct: 0.01, daily_loss_limit_pct: 0.03, max_trades_per_day: 6, max_open_positions: 5, max_positions_per_symbol: 1, max_portfolio_risk_pct: 0.15, min_reward_risk: 1.0, min_credit_to_width: 0.25, entry_window_start: "09:45", entry_window_end: "15:30", flatten_time: "15:55", kelly_fraction: 0.25, max_contracts_per_trade: 10, defined_risk_only: true, pdt_guard: true },
  scan: { universe: [...UNIVERSE], directional_dte: [1, 14], vol_dte: [14, 45], min_open_interest: 100, min_volume: 1, max_spread_pct: 0.12, min_bid: 0.05, min_svi_zscore: 2.0, min_vol_edge: 0.1, min_direction: 0.25, min_expected_value: 5.0, min_score: 0.05, max_ideas_per_symbol: 2 },
  signals: { opening_range_minutes: 15, breakout_bar_minutes: 5, min_relative_volume: 1.2, weights: { ...WEIGHTS } },
  broker: { mode: "paper", starting_equity: START_EQUITY },
};

const state = {
  mode: "paper", running: true, kill: false,
  ideas: [], ideasAsof: null, lastScan: null,
  decisions: [], open: [], closed: [], tradesToday: 0,
  scan: { job_id: null, state: "idle", progress: [], started: null, finished: null, t0: 0, symbols: [] },
  bt: { state: "idle", t0: 0, req: null, plan: null },
  lastStep: 0, idSeq: 100,
};

// ---------------------------------------------------------------- structures + evaluation
function strikeNear(sym, S, mult, exp) { const st = stepOf(S); return Math.round((S * mult) / st) * st; }
function legOf(sym, exp, right, K, side, t) {
  const q = quote(sym, exp, right, K, t);
  return { occ: q.occ, side, ratio: 1, right, strike: K, expiry: exp, bid: q.bid, ask: q.ask, mid: q.mid, flag: q.flag, _q: q };
}
const sgn = (side) => (side === "buy" ? 1 : -1);
function structValue(legs, S, t) {
  let v = 0;
  for (const l of legs) {
    const T = dteOf(l.expiry, t) / 365;
    const q = quote(l.occ.match(/^[A-Z]+/)[0], l.expiry, l.right, l.strike, t);
    v += sgn(l.side) * (T <= 0 ? Math.max(0, (l.right === "C" ? 1 : -1) * (S - l.strike)) : q.mid);
  }
  return v;
}
function payoffAt(legs, ST) { let v = 0; for (const l of legs) v += sgn(l.side) * l.ratio * Math.max(0, (l.right === "C" ? 1 : -1) * (ST - l.strike)); return v; }

const STRAT_LABEL = {
  long_call: "Bullish bet (buy a call)", long_put: "Bearish bet (buy a put)",
  bull_call_spread: "Bullish, capped (call debit spread)", bear_put_spread: "Bearish, capped (put debit spread)",
  bull_put_spread: "Get paid if it holds up (put credit spread)", bear_call_spread: "Get paid if it stays down (call credit spread)",
  iron_condor: "Get paid if it stays in a range (iron condor)", long_straddle: "Bet on a big move (straddle)", long_strangle: "Bet on a big move, cheaper (strangle)",
};

function buildLegs(sym, strat, S, t) {
  const st = stepOf(S);
  const atmK = strikeNear(sym, S, 1);
  const W = st * (S > 260 ? 1 : 2);
  const sdM = SYMS[sym].iv * Math.sqrt(dteOf(EXP_MONTH, t) / 365);
  const e = ["long_call", "long_put", "bull_call_spread", "bear_put_spread"].includes(strat) ? EXP_WEEK : EXP_MONTH;
  switch (strat) {
    case "long_call": return [legOf(sym, e, "C", atmK, "buy", t)];
    case "long_put": return [legOf(sym, e, "P", atmK, "buy", t)];
    case "bull_call_spread": return [legOf(sym, e, "C", atmK, "buy", t), legOf(sym, e, "C", atmK + 2 * W, "sell", t)];
    case "bear_put_spread": return [legOf(sym, e, "P", atmK, "buy", t), legOf(sym, e, "P", atmK - 2 * W, "sell", t)];
    case "bull_put_spread": { const k = strikeNear(sym, S, Math.exp(-0.5 * sdM)); return [legOf(sym, e, "P", k, "sell", t), legOf(sym, e, "P", k - W, "buy", t)]; }
    case "bear_call_spread": { const k = strikeNear(sym, S, Math.exp(0.5 * sdM)); return [legOf(sym, e, "C", k, "sell", t), legOf(sym, e, "C", k + W, "buy", t)]; }
    case "iron_condor": { const kp = strikeNear(sym, S, Math.exp(-0.9 * sdM)), kc = strikeNear(sym, S, Math.exp(0.9 * sdM)); return [legOf(sym, e, "P", kp - W, "buy", t), legOf(sym, e, "P", kp, "sell", t), legOf(sym, e, "C", kc, "sell", t), legOf(sym, e, "C", kc + W, "buy", t)]; }
    case "long_straddle": return [legOf(sym, e, "C", atmK, "buy", t), legOf(sym, e, "P", atmK, "buy", t)];
    case "long_strangle": return [legOf(sym, e, "C", atmK + W, "buy", t), legOf(sym, e, "P", atmK - W, "buy", t)];
  }
  return [];
}

function evaluate(sym, strat, sig, t) {
  const S = spotAt(sym, t);
  const legs = buildLegs(sym, strat, S, t);
  if (!legs.length) return null;
  const net = legs.reduce((a, l) => a + sgn(l.side) * l.mid, 0);
  const half = legs.reduce((a, l) => a + (l.ask - l.bid) / 2, 0);
  const slip = 0.5 * half;
  const entry = r2(net + slip);
  const T = dteOf(legs[0].expiry, t) / 365;
  const p = SYMS[sym];
  const sigPhys = 0.5 * p.rv + 0.5 * smileIV(sym, 0, legs[0].expiry);
  const d = sig.composite.direction * sig.composite.confidence;
  const drift = d * 0.35 * (sigPhys / Math.sqrt(252)) * 252 / Math.max(1, T * 252) ; // annualised tilt
  // physical terminal distribution on a grid
  const n = 600, lo = S * Math.exp(-5 * sigPhys * Math.sqrt(T)), hi = S * Math.exp(5 * sigPhys * Math.sqrt(T));
  const mu = Math.log(S) + (drift - sigPhys * sigPhys / 2) * T, sd = sigPhys * Math.sqrt(T);
  let ev = 0, pop = 0, wsum = 0;
  const costs = 2 * slip * 100 + 0.65 * legs.length * 2;
  for (let i = 0; i < n; i++) {
    const x = lo + (hi - lo) * (i + 0.5) / n;
    const z = (Math.log(x) - mu) / sd;
    const w = Math.exp(-z * z / 2) / x;
    const pnl = (payoffAt(legs, x) - net) * 100;
    ev += w * pnl; if (pnl - costs / 1 > 0) pop += w; wsum += w;
  }
  ev = ev / wsum - costs; pop = pop / wsum;
  // payoff diagram & extremes
  const pLo = S * Math.exp(-3.2 * sd), pHi = S * Math.exp(3.2 * sd);
  const prices = [], pnl = [];
  for (let i = 0; i < 61; i++) { const x = pLo + (pHi - pLo) * i / 60; prices.push(r2(x)); pnl.push(r2((payoffAt(legs, x) - entry) * 100)); }
  const probe = [0.01, ...legs.map((l) => l.strike), S * 4];
  const vals = probe.map((x) => (payoffAt(legs, x) - entry) * 100);
  const maxLoss = Math.max(0, -Math.min(...vals));
  const unbounded = legs.some((l) => l.right === "C" && l.side === "buy") && !legs.some((l) => l.right === "C" && l.side === "sell");
  const maxProfit = unbounded ? null : Math.max(...vals);
  const bes = [];
  const fine = 2000;
  let prev = null;
  for (let i = 0; i <= fine; i++) {
    const x = 0.3 * S + 1.4 * S * i / fine; const v = payoffAt(legs, x) - entry;
    if (prev && Math.sign(prev.v) !== Math.sign(v) && v !== 0) bes.push(r2(prev.x + (x - prev.x) * (-prev.v) / (v - prev.v)));
    prev = { x, v };
  }
  const greeks = { delta: 0, gamma: 0, vega: 0, theta: 0 };
  for (const l of legs) for (const k of Object.keys(greeks)) greeks[k] += sgn(l.side) * 100 * l._q.greeks[k];
  for (const k of Object.keys(greeks)) greeks[k] = r4(greeks[k]);
  const isVol = ["iron_condor", "long_straddle", "long_strangle"].includes(strat) || strat.includes("credit");
  const ve = sig.vol.vol_edge;
  const conf = isVol || strat === "bull_put_spread" || strat === "bear_call_spread" ? Math.min(1, Math.abs(ve) / 0.3) : sig.composite.confidence;
  const score = maxLoss > 0 ? ev / maxLoss * conf * REGIME.risk_multiplier : 0;
  const equity = account().equity;
  const budget = equity * CONFIG.risk.max_loss_per_trade_pct * REGIME.risk_multiplier;
  const contracts = maxLoss > 0 ? Math.max(0, Math.min(CONFIG.risk.max_contracts_per_trade, Math.floor(budget / maxLoss))) : 0;
  const cheap = legs.filter((l) => l.flag === "cheap" && l.side === "buy"), rich = legs.filter((l) => l.flag === "rich" && l.side === "sell");
  const dword = sig.composite.direction > 0.05 ? "bullish" : sig.composite.direction < -0.05 ? "bearish" : "neutral";
  const rationale = [
    `Signals lean ${dword}: composite ${sig.composite.direction >= 0 ? "+" : ""}${sig.composite.direction.toFixed(2)} with ${(sig.composite.confidence * 100).toFixed(0)}% confidence.`,
    ve >= 0 ? `Options look cheap: forecast volatility ${(p.rv * 100).toFixed(1)}% vs implied ${(sig.vol.atm_iv * 100).toFixed(1)}%.`
      : `Options look expensive: implied volatility ${(sig.vol.atm_iv * 100).toFixed(1)}% vs forecast ${(p.rv * 100).toFixed(1)}%, so selling premium is favoured.`,
    ...cheap.map((l) => `Buys the ${l.strike} ${l.right === "C" ? "call" : "put"}, which is priced below the fitted curve (cheap).`),
    ...rich.map((l) => `Sells the ${l.strike} ${l.right === "C" ? "call" : "put"}, which is priced above the fitted curve (rich).`),
    `Model expected value ${ev >= 0 ? "+" : "-"}$${Math.abs(ev).toFixed(2)} per contract-set after costs; chance of profit ${(pop * 100).toFixed(0)}%.`,
    `Breakeven${bes.length > 1 ? "s" : ""} at expiry: ${bes.map((b) => "$" + b.toFixed(2)).join(" and ") || "none in range"}.`,
  ];
  return {
    id: `${sym}-${strat}-${legs[0].expiry}`, symbol: sym, strategy: strat, strategy_label: STRAT_LABEL[strat],
    legs: legs.map(({ _q, ...l }) => l), net_price: r2(net), entry_limit: entry, max_loss: r2(maxLoss), max_profit: maxProfit == null ? null : r2(maxProfit),
    pop: r4(pop), expected_value: r2(ev), score: r4(score), contracts, rationale, greeks, breakevens: bes, payoff: { prices, pnl }, spot: r2(S),
    intraday: ["long_call", "long_put", "bull_call_spread", "bear_put_spread"].includes(strat), decision: null,
  };
}

function strategiesFor(sig) {
  const D = sig.composite.direction, V = sig.vol.vol_edge;
  const strong = Math.abs(D) >= CONFIG.scan.min_direction;
  if (strong && V > 0) return D > 0 ? ["bull_call_spread", "long_call"] : ["bear_put_spread", "long_put"];
  if (strong && V <= 0) return D > 0 ? ["bull_put_spread", "bull_call_spread"] : ["bear_call_spread", "bear_put_spread"];
  if (!strong && V < -CONFIG.scan.min_vol_edge / 2) return ["iron_condor"];
  if (!strong && V > CONFIG.scan.min_vol_edge) return ["long_straddle", "long_strangle"];
  return [];
}

function riskCheck(idea, t, pending) {
  const reasons = [];
  const acc = account();
  const hm = nyParts(t); const mins = hm.hh * 60 + hm.mm;
  const [ws, we] = [CONFIG.risk.entry_window_start, CONFIG.risk.entry_window_end].map((s) => { const [a, b] = s.split(":").map(Number); return a * 60 + b; });
  const budget = acc.equity * CONFIG.risk.max_loss_per_trade_pct;
  const held = [...state.open, ...pending].filter((p) => p.symbol === idea.symbol).length;
  const risk = acc.open_risk + pending.reduce((a, p) => a + p.max_loss * p.contracts, 0);
  const riskAfter = risk + idea.max_loss * Math.max(1, idea.contracts);
  if (state.kill) reasons.push("Kill switch is on — no new trades today.");
  if (mins < ws || mins >= we) reasons.push(`Outside the entry window (${CONFIG.risk.entry_window_start}–${CONFIG.risk.entry_window_end} New York). The first 15 minutes are for measuring the opening range.`);
  if (idea.contracts < 1) reasons.push(`Too big for the per-trade risk budget: one contract-set can lose $${idea.max_loss.toFixed(0)}, budget is $${(budget * REGIME.risk_multiplier).toFixed(0)}.`);
  if (held >= CONFIG.risk.max_positions_per_symbol) reasons.push(`Already holding ${idea.symbol} (limit ${CONFIG.risk.max_positions_per_symbol} position per symbol).`);
  if (riskAfter > acc.equity * CONFIG.risk.max_portfolio_risk_pct) reasons.push(`Total open risk would reach ${(riskAfter / acc.equity * 100).toFixed(1)}%, above the ${(CONFIG.risk.max_portfolio_risk_pct * 100).toFixed(0)}% cap.`);
  if (state.tradesToday + pending.length >= CONFIG.risk.max_trades_per_day) reasons.push(`Already made ${state.tradesToday} trades today (max ${CONFIG.risk.max_trades_per_day}).`);
  if (state.open.length + pending.length >= CONFIG.risk.max_open_positions) reasons.push(`Already ${state.open.length} open positions (max ${CONFIG.risk.max_open_positions}).`);
  if (idea.expected_value < CONFIG.scan.min_expected_value) reasons.push(`Expected value $${idea.expected_value.toFixed(2)} is below the $${CONFIG.scan.min_expected_value.toFixed(2)} minimum.`);
  if (reasons.length) return { passed: false, reasons };
  return { passed: true, reasons: [
    `Worst case $${(idea.max_loss * idea.contracts).toFixed(0)} is within the $${budget.toFixed(0)} per-trade cap.`,
    `Total open risk after this trade: ${(riskAfter / acc.equity * 100).toFixed(1)}% of equity (cap ${(CONFIG.risk.max_portfolio_risk_pct * 100).toFixed(0)}%).`,
    "Defined risk: every sold option is covered by a bought one.",
  ] };
}

// reward:risk for debits, credit:width for credits (RiskConfig minimums)
function passesShape(i) {
  if (i.net_price < 0) {
    const width = i.max_loss / 100 + Math.abs(i.entry_limit);
    return width > 0 && Math.abs(i.entry_limit) / width >= CONFIG.risk.min_credit_to_width;
  }
  return i.max_profit == null || i.max_profit / Math.max(1, i.max_loss) >= CONFIG.risk.min_reward_risk;
}

function runScan(symbols, t, { trade = false } = {}) {
  const ideas = [];
  const pending = [];
  for (const sym of symbols) {
    if (sym === "GOOGL") continue; // simulated data problem (reported in scan progress)
    const sig = signalsFor(sym, t);
    const cands = strategiesFor(sig).map((s) => evaluate(sym, s, sig, t)).filter(Boolean)
      .filter((i) => i.expected_value >= CONFIG.scan.min_expected_value && passesShape(i)).sort((a, b) => b.score - a.score).slice(0, CONFIG.scan.max_ideas_per_symbol);
    ideas.push(...cands);
  }
  ideas.sort((a, b) => b.score - a.score);
  for (const idea of ideas) {
    idea.decision = riskCheck(idea, t, pending);
    if (idea.decision.passed) pending.push(idea);
    state.decisions.unshift({ t: iso(t), symbol: idea.symbol, strategy: idea.strategy, passed: idea.decision.passed, reasons: idea.decision.reasons, contracts: idea.contracts, score: idea.score, expected_value: idea.expected_value });
  }
  state.decisions = state.decisions.slice(0, 300);
  state.ideas = ideas; state.ideasAsof = iso(t); state.lastScan = iso(t);
  if (trade && (state.mode === "paper" || state.mode === "alpaca_paper") && state.running) {
    for (const idea of pending.slice(0, 2)) openPosition(idea, t);
  }
}

function openPosition(idea, t, { entryOverride, openedAt, contracts } = {}) {
  const pos = {
    id: `P${++state.idSeq}`, symbol: idea.symbol, strategy: idea.strategy, strategy_label: idea.strategy_label,
    contracts: contracts ?? idea.contracts, entry_price: entryOverride ?? idea.entry_limit, mark: null, unrealized_pnl: 0, realized_pnl: 0,
    max_loss: idea.max_loss, max_profit: idea.max_profit, opened: iso(openedAt ?? t), closed: null, exit_reason: "",
    legs: idea.legs, expiry: idea.legs.map((l) => l.expiry).sort()[0], progress_to_target: 0, progress_to_stop: 0,
    _fees: 0.65 * idea.legs.length * (contracts ?? idea.contracts),
  };
  state.open.push(pos);
  if (!openedAt) state.tradesToday++;
  return pos;
}

function markPosition(p, t = now()) {
  const S = spotAt(p.symbol, t);
  p.mark = r2(structValue(p.legs, S, t));
  p.unrealized_pnl = r2((p.mark - p.entry_price) * 100 * p.contracts);
  const tgt = 0.5 * (p.max_profit ?? Math.abs(p.entry_price) * 100) * p.contracts;
  const stop = 0.35 * p.max_loss * p.contracts;
  p.progress_to_target = r4(Math.max(0, Math.min(1, p.unrealized_pnl / tgt)));
  p.progress_to_stop = r4(Math.max(0, Math.min(1, -p.unrealized_pnl / stop)));
  return p;
}

function closePosition(p, reason, t = now()) {
  markPosition(p, t);
  const half = p.legs.reduce((a, l) => a + (l.ask - l.bid) / 2, 0);
  const exit = r2(p.mark - 0.5 * half);
  p.realized_pnl = r2((exit - p.entry_price) * 100 * p.contracts - p._fees - 0.65 * p.legs.length * p.contracts);
  p.unrealized_pnl = 0; p.closed = iso(t); p.exit_reason = reason; p.mark = exit;
  state.open = state.open.filter((x) => x !== p);
  state.closed.unshift(p);
}

// history: closed trades from previous sessions (fees included), used for the equity curve
const HISTORY = [
  { d: 9, symbol: "SPY", strategy: "bull_call_spread", pnl: 118.7, reason: "take_profit", c: 2, ml: 212 },
  { d: 8, symbol: "TSLA", strategy: "bear_put_spread", pnl: -96.6, reason: "stop_loss", c: 1, ml: 276 },
  { d: 7, symbol: "QQQ", strategy: "iron_condor", pnl: 84.2, reason: "take_profit", c: 1, ml: 318 },
  { d: 6, symbol: "NVDA", strategy: "long_call", pnl: -41.3, reason: "time_flatten", c: 2, ml: 152 },
  { d: 5, symbol: "AAPL", strategy: "bull_put_spread", pnl: 61.4, reason: "take_profit", c: 2, ml: 188 },
  { d: 4, symbol: "META", strategy: "bear_call_spread", pnl: -132.9, reason: "signal_invalidated", c: 1, ml: 381 },
  { d: 3, symbol: "IWM", strategy: "long_straddle", pnl: 47.8, reason: "dte_exit", c: 1, ml: 402 },
  { d: 2, symbol: "AMZN", strategy: "bear_put_spread", pnl: 22.1, reason: "time_flatten", c: 2, ml: 190 },
];
const DAYS = []; { let d = SESSION; for (let i = 0; i < 12; i++) { d = prevWeekday(d); DAYS.unshift(d); } }
function historyDay(back) { return DAYS[DAYS.length - back]; }
for (const h of HISTORY) {
  const day = historyDay(h.d);
  state.closed.push({
    id: `H${h.d}`, symbol: h.symbol, strategy: h.strategy, strategy_label: STRAT_LABEL[h.strategy], contracts: h.c,
    entry_price: null, mark: null, unrealized_pnl: 0, realized_pnl: h.pnl, max_loss: h.ml, max_profit: null,
    opened: iso(at(day, 10, 5 + h.d * 3)), closed: iso(at(day, h.reason === "time_flatten" ? 15 : 13, h.reason === "time_flatten" ? 55 : 20 + h.d)),
    exit_reason: h.reason, legs: [], expiry: addDays(day, 3), progress_to_target: 0, progress_to_stop: 0, _fees: 0,
  });
}
state.closed.sort((a, b) => Date.parse(b.closed) - Date.parse(a.closed));

// open positions carried from earlier sessions (entered at that day's simulated prices)
function seedOpen(sym, strat, daysBack, entryAdj) {
  const t0 = at(historyDay(daysBack), 11, 0);
  const sig = signalsFor(sym, OPEN + 20 * 60000);
  const idea = evaluate(sym, strat, sig, OPEN + 60000);
  if (!idea) return;
  const entry = r2(idea.entry_limit * entryAdj);
  openPosition(idea, t0, { entryOverride: entry, openedAt: t0, contracts: Math.max(1, idea.contracts) });
}
seedOpen("QQQ", "iron_condor", 2, 1.12);
seedOpen("MSFT", "bull_put_spread", 1, 1.08);
seedOpen("AMD", "long_straddle", 3, 0.97);

const realizedBefore = state.closed.reduce((a, p) => a + p.realized_pnl, 0);
function account(t = now()) {
  for (const p of state.open) markPosition(p, t);
  const realized = state.closed.reduce((a, p) => a + p.realized_pnl, 0);
  const cashOut = state.open.reduce((a, p) => a + p.entry_price * 100 * p.contracts + p._fees, 0);
  const cash = START_EQUITY + realized - cashOut;
  const equity = cash + state.open.reduce((a, p) => a + p.mark * 100 * p.contracts, 0);
  const collateral = state.open.filter((p) => p.entry_price < 0).reduce((a, p) => a + (p.max_loss * p.contracts - p.entry_price * 100 * p.contracts), 0);
  const open_risk = state.open.reduce((a, p) => a + p.max_loss * p.contracts, 0);
  return { equity: r2(equity), cash: r2(cash), buying_power: r2(Math.max(0, cash - collateral)), day_pnl: r2(equity - prevCloseEquity), open_risk: r2(open_risk) };
}
{
  const tPrev = at(PREV, 16, 0);
  let unreal = 0;
  for (const p of state.open) unreal += (structValue(p.legs, PATHS[p.symbol].prevClose, tPrev) - p.entry_price) * 100 * p.contracts - p._fees;
  prevCloseEquity = r2(START_EQUITY + realizedBefore + unreal);
}

// first scan of the day happened before the entry window: ideas exist, all blocked by time
runScan(UNIVERSE, Math.min(now() - 60000, at(SESSION, 9, 40)), { trade: false });

// ---------------------------------------------------------------- live simulation stepping
function marketState(t) {
  if (t < OPEN) return "pre";
  if (t < OR_END) return "opening_range";
  if (t < CLOSE) return "open";
  return "closed";
}
function advance() {
  const t = now();
  // scan job progress
  const sj = state.scan;
  if (sj.state === "running") {
    const per = 650;
    const k = Math.floor((Date.now() - sj.t0) / per);
    sj.progress = sj.symbols.map((s, i) => {
      if (i < k) return s === "GOOGL" ? { symbol: s, state: "error", message: "Too few two-sided quotes in the option chain — skipped." } : { symbol: s, state: "done", message: "Analysed" };
      if (i === k) return { symbol: s, state: "running", message: "Pricing options and checking signals…" };
      return { symbol: s, state: "queued", message: "" };
    });
    if (k >= sj.symbols.length) {
      runScan(sj.symbols, t, { trade: false }); // a scan only looks; the running session trades on its own steps
      sj.state = "done"; sj.finished = iso(t);
      const counts = {}; for (const i of state.ideas) counts[i.symbol] = (counts[i.symbol] || 0) + 1;
      sj.progress = sj.progress.map((p) => p.state === "error" ? p : { ...p, message: counts[p.symbol] ? `${counts[p.symbol]} idea${counts[p.symbol] > 1 ? "s" : ""} found` : "Nothing worth trading" });
    }
  }
  // the running session steps every 2 simulated minutes once the entry window opens
  if (state.running && ["paper", "alpaca_paper", "shadow"].includes(state.mode) && t >= OR_END + 30000 && t - state.lastStep > 120000 && sj.state !== "running") {
    state.lastStep = t;
    runScan(UNIVERSE, t, { trade: state.mode !== "shadow" });
  }
}

function status() {
  const t = now();
  const ms = marketState(t);
  const nextOpen = ms === "pre" ? OPEN : at(nextWeekday(SESSION), 9, 30);
  return {
    mode: state.running ? state.mode : "idle", running: state.running, now: iso(t),
    market: { state: ms, next_open: iso(nextOpen), next_close: iso(CLOSE), opening_range_ends: ms === "pre" || ms === "opening_range" ? iso(OR_END) : null },
    account: account(t), kill_switch: state.kill, provider: "synthetic",
    data_delay_note: "DEMO — simulated prices generated in your browser, not real quotes.",
    universe: [...CONFIG.scan.universe], last_scan: state.lastScan,
    trades_today: state.tradesToday, max_trades_per_day: CONFIG.risk.max_trades_per_day,
    alpaca_keys_present: false, sentiment_engine: "lexicon",
  };
}

function surface(sym) {
  if (!SYMS[sym]) throw httpError(404, `Unknown symbol ${sym}`);
  const t = now(); const S = spotAt(sym, t);
  const slices = []; const mis = [];
  for (const exp of [EXP_WEEK, EXP_MONTH, EXP_LATE]) {
    const st = stepOf(S); const pts = [];
    const T = dteOf(exp, t) / 365; const F = S * Math.exp(RATE * T);
    const width = Math.max(0.06, 2.2 * SYMS[sym].iv * Math.sqrt(T));
    const kLo = Math.ceil(F * (1 - width) / st) * st, kHi = Math.floor(F * (1 + width) / st) * st;
    for (let K = kLo; K <= kHi + 1e-9; K += st) {
      const right = K < F ? "P" : "C";
      const q = quote(sym, exp, right, K, t);
      if (q.bid < 0.05) continue;
      pts.push({ strike: K, right, market_iv: r4(q.mkt), bid_iv: r4(q.bidIv), ask_iv: r4(q.askIv), fit_iv: r4(q.fit), flag: q.flag, zscore: Math.abs(q.z) >= 1 ? r2(q.z) : null });
      if (q.flag) {
        const fair = bs(S, K, T, q.fit, right === "C" ? 1 : -1).price;
        const edge = fair - q.mid;
        const eac = q.flag === "cheap" ? Math.max(0, fair - q.ask) : -Math.max(0, q.bid - fair);
        mis.push({ occ: q.occ, model: "svi_residual", edge: r4(edge), edge_after_costs: r4(eac), zscore: r2(q.z), market_iv: r4(q.mkt), fair_iv: r4(q.fit), notes: `${q.flag === "cheap" ? "Ask" : "Bid"} is ${q.flag === "cheap" ? "below" : "above"} the fitted curve (${exp}).` });
      }
    }
    const fs = [], fi = [];
    if (pts.length) for (let i = 0; i <= 40; i++) { const K = pts[0].strike + (pts[pts.length - 1].strike - pts[0].strike) * i / 40; fs.push(r2(K)); fi.push(r4(smileIV(sym, Math.log(K / F), exp))); }
    slices.push({ expiry: exp, dte: Math.round(dteOf(exp, t)), forward: r2(F), method: "svi", rmse: r4(0.0031 + (hash(sym + exp) % 20) / 10000), points: pts, fit_curve: { strikes: fs, iv: fi } });
  }
  mis.sort((a, b) => Math.abs(b.zscore) - Math.abs(a.zscore));
  return { symbol: sym, spot: r2(S), asof: iso(t), slices, mispricings: mis };
}

const NEWS = [
  ["NVDA", "Chip suppliers point to stronger data-center orders into year end", 0.62, 0.9, 0.7, 25],
  ["TSLA", "Automaker trims delivery outlook in a regional update", -0.58, 0.85, 0.75, 40],
  ["META", "Regulators open a new review of ad-targeting practices", -0.47, 0.8, 0.6, 55],
  ["AAPL", "Analysts lift price targets after services revenue beat", 0.44, 0.75, 0.55, 70],
  ["SPY", "Futures edge higher as Treasury yields ease", 0.18, 0.6, 0.35, 85],
  ["AMD", "New accelerator lineup gets a mixed first reception", 0.12, 0.7, 0.4, 110],
  ["AMZN", "Cloud unit faces tougher comparison next quarter, says broker note", -0.21, 0.65, 0.4, 150],
  ["QQQ", "Tech heavyweights steady ahead of inflation data", 0.05, 0.5, 0.3, 190],
  ["MSFT", "Software giant expands AI software pricing tiers", 0.22, 0.7, 0.45, 240],
  ["IWM", "Small caps lag as rate-cut bets cool", -0.16, 0.55, 0.35, 300],
  ["GOOGL", "Search unit unveils updated ad tools", 0.08, 0.6, 0.3, 420],
];
function news(sym) {
  const t = now();
  return { items: NEWS.filter((n) => sym === "ALL" || n[0] === sym).map(([s, h, se, re, ma, ago]) => ({
    symbol: s, headline: h, source: "Demo sample feed", url: "", published: iso(t - ago * 60000), sentiment: se, relevance: re, materiality: ma,
  })) };
}

function equityCurve() {
  const pts = [];
  let eq = START_EQUITY;
  const byDay = {}; for (const p of state.closed) if (p.id.startsWith("H")) { const d = p.closed.slice(0, 10); byDay[d] = (byDay[d] || 0) + p.realized_pnl; }
  for (const d of DAYS) { eq += byDay[d] || 0; pts.push({ t: iso(at(d, 16)), equity: r2(d === PREV ? prevCloseEquity : eq) }); }
  // today's intraday points every 5 minutes up to now, from re-marking the book
  const t = now();
  const acc = account(t);
  const start = Math.max(OPEN, at(SESSION, 9, 30));
  const steps = Math.max(0, Math.floor((Math.min(t, CLOSE) - start) / 300000));
  const realizedToday = state.closed.filter((p) => !p.id.startsWith("H")).reduce((a, p) => a + p.realized_pnl, 0);
  for (let i = 0; t >= OPEN && i <= steps; i++) {
    const ti = Math.min(CLOSE, start + i * 300000);
    let u = 0; for (const p of state.open) if (Date.parse(p.opened) <= ti) u += (structValue(p.legs, spotAt(p.symbol, ti), ti) - p.entry_price) * 100 * p.contracts - p._fees;
    pts.push({ t: iso(ti), equity: r2(START_EQUITY + realizedBefore + realizedToday + u) });
  }
  pts.push({ t: iso(t), equity: acc.equity });
  return { points: pts, starting_equity: START_EQUITY };
}

// ---------------------------------------------------------------- backtest simulation
function backtestResult() {
  const b = state.bt;
  if (b.state === "idle") return { state: "idle", progress: 0, summary: null, equity: [], by_strategy: [], by_exit: [], trades: [], caveats: [] };
  const dur = 9000;
  const prog = Math.min(1, (Date.now() - b.t0) / dur);
  const plan = b.plan;
  const n = Math.floor(plan.trades.length * prog);
  const trades = plan.trades.slice(0, n);
  const eq = []; let e = START_EQUITY;
  const shownDays = Math.max(1, Math.floor(plan.days.length * prog));
  for (let i = 0; i < shownDays; i++) { for (const tr of plan.trades) if (tr._day === i) e += tr.pnl; eq.push({ t: iso(at(plan.days[i], 16)), equity: r2(e) }); }
  const done = prog >= 1;
  const res = { state: done ? "done" : "running", progress: r4(prog), summary: null, equity: eq, by_strategy: [], by_exit: [], trades: trades.map(({ _day, ...x }) => x), caveats: plan.caveats };
  if (done) {
    const wins = plan.trades.filter((x) => x.pnl > 0), losses = plan.trades.filter((x) => x.pnl <= 0);
    const gw = wins.reduce((a, x) => a + x.pnl, 0), gl = -losses.reduce((a, x) => a + x.pnl, 0);
    let peak = START_EQUITY, mdd = 0; for (const p of eq) { peak = Math.max(peak, p.equity); mdd = Math.max(mdd, (peak - p.equity) / peak); }
    const daily = eq.map((p, i) => (i ? p.equity - eq[i - 1].equity : p.equity - START_EQUITY));
    const mean = daily.reduce((a, x) => a + x, 0) / daily.length;
    const sd = Math.sqrt(daily.reduce((a, x) => a + (x - mean) ** 2, 0) / Math.max(1, daily.length - 1));
    res.summary = { trades: plan.trades.length, win_rate: r4(wins.length / plan.trades.length), profit_factor: gl ? r2(gw / gl) : null, expectancy: r2((gw - gl) / plan.trades.length), sharpe: sd ? r2(mean / sd * Math.sqrt(252)) : null, max_drawdown: r4(mdd), total_pnl: r2(gw - gl) };
    const bs_ = {}, be = {};
    for (const x of plan.trades) {
      (bs_[x.strategy] ||= { strategy: x.strategy, trades: 0, pnl: 0, w: 0 }); bs_[x.strategy].trades++; bs_[x.strategy].pnl += x.pnl; if (x.pnl > 0) bs_[x.strategy].w++;
      (be[x.exit_reason] ||= { reason: x.exit_reason, trades: 0, pnl: 0 }); be[x.exit_reason].trades++; be[x.exit_reason].pnl += x.pnl;
    }
    res.by_strategy = Object.values(bs_).map((s) => ({ strategy: s.strategy, trades: s.trades, pnl: r2(s.pnl), win_rate: r4(s.w / s.trades) })).sort((a, b) => b.pnl - a.pnl);
    res.by_exit = Object.values(be).map((s) => ({ ...s, pnl: r2(s.pnl) })).sort((a, b) => b.trades - a.trades);
  }
  return res;
}
function planBacktest(req) {
  const rng = mulberry32(hash(JSON.stringify(req)));
  const days = []; let d = SESSION; for (let i = 0; i < req.days; i++) { d = prevWeekday(d); days.unshift(d); }
  const strats = ["bull_call_spread", "bear_put_spread", "iron_condor", "bull_put_spread", "bear_call_spread", "long_call", "long_straddle"];
  const exits = ["take_profit", "stop_loss", "time_flatten", "signal_invalidated", "dte_exit"];
  const trades = [];
  days.forEach((day, i) => {
    const k = Math.floor(rng() * 3);
    for (let j = 0; j < k; j++) {
      const sym = req.symbols[Math.floor(rng() * req.symbols.length)];
      const strat = strats[Math.floor(rng() * strats.length)];
      const ex = exits[Math.min(exits.length - 1, Math.floor(rng() * rng() * exits.length * 1.6))];
      const base = ex === "take_profit" ? 40 + rng() * 90 : ex === "stop_loss" ? -(50 + rng() * 110) : (rng() - 0.48) * 120;
      const o = at(day, 10, Math.floor(rng() * 50)), c = at(day, ex === "time_flatten" ? 15 : 12 + Math.floor(rng() * 3), ex === "time_flatten" ? 55 : Math.floor(rng() * 59));
      trades.push({ symbol: sym, strategy: strat, opened: iso(o), closed: iso(c), pnl: r2(base), exit_reason: ex, _day: i });
    }
  });
  return {
    days, trades,
    caveats: [
      "DEMO DATA: these trades were generated by the dashboard's sample-data layer, not by Kwant Trades's backtester. They mean nothing about real performance.",
      req.source === "real_bars" ? "Option prices were SYNTHETIC (Black-Scholes from a modelled volatility surface), not real historical quotes." : "Both the stock prices and the option prices were SIMULATED.",
      "Fills assume you trade at mid plus half the spread; real fills can be worse.",
      `Only ${trades.length} trades — far too few to judge whether the strategy works.`,
      "Simulated results do not predict paper or live results.",
    ],
  };
}

// ---------------------------------------------------------------- request router
function httpError(status, message) { const e = new Error(message); e.status = status; return e; }
const clone = (x) => JSON.parse(JSON.stringify(x, (k, v) => (k.startsWith("_") ? undefined : v)));
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

export async function handle(method, path, body) {
  await delay(80 + Math.random() * 180);
  advance();
  const url = new URL(path, "http://demo.local");
  const p = url.pathname;
  const t = now();
  if (method === "GET") {
    if (p === "/api/status") return clone(status());
    if (p === "/api/scan/status") { const { t0, symbols, ...s } = state.scan; return clone(s); }
    if (p === "/api/ideas") {
      const ideas = state.ideas.map((i) => ({ ...i }));
      return clone({ asof: state.ideasAsof, ideas });
    }
    if (p === "/api/signals") return clone({ asof: iso(t), regime: REGIME, symbols: CONFIG.scan.universe.filter((s) => SYMS[s]).map((s) => signalsFor(s, t)) });
    if (p === "/api/surface") return clone(surface((url.searchParams.get("symbol") || "SPY").toUpperCase()));
    if (p === "/api/positions") { account(t); return clone({ open: state.open, closed: state.closed }); }
    if (p === "/api/equity") return clone(equityCurve());
    if (p === "/api/decisions") return clone({ decisions: state.decisions.slice(0, Number(url.searchParams.get("limit")) || 100) });
    if (p === "/api/news") return clone(news((url.searchParams.get("symbol") || "ALL").toUpperCase()));
    if (p === "/api/config") return clone(CONFIG);
    if (p === "/api/backtest/result") return clone(backtestResult());
  }
  if (method === "POST") {
    if (p === "/api/scan") {
      if (state.scan.state === "running") throw httpError(409, "A scan is already running.");
      const syms = Array.isArray(body?.symbols) && body.symbols.length ? body.symbols.filter((s) => SYMS[s]) : [...CONFIG.scan.universe].filter((s) => SYMS[s]);
      const id = "demo-scan-" + Date.now().toString(36);
      state.scan = { job_id: id, state: "running", progress: syms.map((s) => ({ symbol: s, state: "queued", message: "" })), started: iso(t), finished: null, t0: Date.now(), symbols: syms };
      return { job_id: id };
    }
    const m = p.match(/^\/api\/positions\/([^/]+)\/close$/);
    if (m) {
      if (!state.running || !["paper", "alpaca_paper"].includes(state.mode)) return { ok: false, message: "Positions can only be closed from the page in a paper mode." };
      const pos = state.open.find((x) => x.id === decodeURIComponent(m[1]));
      if (!pos) return { ok: false, message: "That position is no longer open." };
      closePosition(pos, "manual", t);
      return { ok: true, message: `Closed ${pos.symbol} (practice money). Realised ${pos.realized_pnl >= 0 ? "+" : "-"}$${Math.abs(pos.realized_pnl).toFixed(2)}.` };
    }
    if (p === "/api/session/flatten") {
      if (!state.running || !["paper", "alpaca_paper"].includes(state.mode)) return { ok: false, message: "Flatten works only in a paper mode." };
      const n = state.open.length;
      for (const pos of [...state.open]) closePosition(pos, "flatten_all", t);
      return { ok: true, message: n ? `Closed ${n} position${n > 1 ? "s" : ""} (practice money).` : "Nothing was open." };
    }
    if (p === "/api/session/start") {
      const mode = body?.mode;
      if (mode === "alpaca_live") throw httpError(403, "Live trading cannot be started from the web page.");
      if (!["shadow", "paper", "alpaca_paper"].includes(mode)) throw httpError(400, "Unknown mode.");
      if (mode === "alpaca_paper") throw httpError(400, "Alpaca keys were not found in .env, so Alpaca paper can't start.");
      state.mode = mode; state.running = true; state.lastStep = 0;
      return clone(status());
    }
    if (p === "/api/session/stop") { state.running = false; return clone(status()); }
    if (p === "/api/config") {
      for (const sec of ["risk", "scan"]) {
        if (!body || typeof body[sec] !== "object") continue;
        for (const [k, v] of Object.entries(body[sec])) {
          if (!(k in CONFIG[sec])) throw httpError(400, `Unknown setting ${sec}.${k}`);
          const cur = CONFIG[sec][k];
          if (typeof cur === "number" && (typeof v !== "number" || !Number.isFinite(v))) throw httpError(400, `${k} must be a number`);
          CONFIG[sec][k] = v;
        }
      }
      return clone(CONFIG);
    }
    if (p === "/api/backtest") {
      if (state.bt.state === "running" && backtestResult().state === "running") throw httpError(409, "A backtest is already running.");
      const syms = Array.isArray(body?.symbols) && body.symbols.length ? body.symbols : ["SPY"];
      const days = Math.max(1, Math.min(60, Number(body?.days) || 5));
      const req = { symbols: syms, days, source: body?.source === "simulated" ? "simulated" : "real_bars" };
      state.bt = { state: "running", t0: Date.now(), req, plan: planBacktest(req) };
      return { job_id: "demo-bt-" + Date.now().toString(36) };
    }
  }
  throw httpError(404, `Not found: ${method} ${p}`);
}

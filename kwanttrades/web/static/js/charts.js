// Hand-written SVG charts. Each responsive chart redraws at its real pixel width
// (ResizeObserver), offers a hover/focus tooltip, and ships a table alternative.
import { h, s, clear, prefersReducedMotion } from "./dom.js";
import { isNum, money, signedMoney, price, pct, timeNY, dateNY, clamp, num } from "./format.js";

export const scale = (d0, d1, r0, r1) => (v) => (d1 === d0 ? (r0 + r1) / 2 : r0 + ((v - d0) / (d1 - d0)) * (r1 - r0));

export function niceTicks(lo, hi, n = 4) {
  if (!isNum(lo) || !isNum(hi)) return [];
  if (lo === hi) { lo -= 1; hi += 1; }
  const span = hi - lo, step0 = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const err = step0 / mag;
  const step = (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1) * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) out.push(Math.round(v / step) * step);
  return out;
}

const pathOf = (pts) => pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");

/** Wrap a draw(width) function into a responsive, re-drawing container. */
function responsive(cls, height, draw, { animate = true } = {}) {
  const wrap = h("div", { class: `chart ${cls}` });
  wrap.style.height = height + "px";
  let lastW = 0, first = animate && !prefersReducedMotion();
  const render = () => {
    const w = Math.floor(wrap.clientWidth);
    if (!w || w === lastW) return;
    lastW = w;
    clear(wrap);
    draw(wrap, w, height, first);
    first = false;
  };
  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(() => render());
    ro.observe(wrap);
  }
  setTimeout(render, 30); // fallback once attached (and for browsers without ResizeObserver)
  return wrap;
}

function tooltip(wrap) {
  const tip = h("div", { class: "chart-tip", hidden: true, "aria-hidden": "true" });
  wrap.appendChild(tip);
  return {
    show(x, y, lines) {
      clear(tip);
      lines.forEach((l, i) => tip.appendChild(h(i ? "span" : "strong", null, l)));
      tip.hidden = false;
      const W = wrap.clientWidth;
      const tw = tip.offsetWidth;
      const left = clamp(x + 12, 4, W - tw - 4);
      tip.style.transform = `translate(${left > x ? left : Math.max(4, x - tw - 12)}px, ${Math.max(0, y - 40)}px)`;
    },
    hide() { tip.hidden = true; },
  };
}

/** Table alternative under a chart (collapsed; charts are decorative to screen readers otherwise). */
export function tableAlt(caption, headers, rows) {
  const t = h("table", { class: "tbl compact" },
    h("caption", { class: "sr-only" }, caption),
    h("thead", null, h("tr", null, headers.map((x) => h("th", { scope: "col" }, x)))),
    h("tbody", null, rows.map((r) => h("tr", null, r.map((c) => h("td", null, c))))));
  return h("details", { class: "table-alt" }, h("summary", null, "Show as table"), h("div", { class: "tbl-wrap" }, t));
}

// ---------------------------------------------------------------- line / equity chart
/**
 * points: [{t: iso|ms, v: number}] ; baseline: number|null (dashed reference, e.g. starting equity)
 */
export function lineChart(points, { height = 200, baseline = null, baselineLabel = "Start", fmtY = (v) => money(v, { cents: false }), fmtX, label = "Chart", animate = true, intraday = false } = {}) {
  const pts = (points || []).map((p) => ({ t: typeof p.t === "number" ? p.t : Date.parse(p.t), v: p.v })).filter((p) => isNum(p.t) && isNum(p.v));
  const fx = fmtX || ((t) => (intraday ? timeNY(t) : dateNY(t)));
  const wrap = responsive("line", height, (el, W, H, first) => {
    if (pts.length < 2) { el.appendChild(h("p", { class: "muted small chart-empty" }, "Not enough points to draw yet.")); return; }
    const m = { l: 56, r: 12, t: 12, b: 24 };
    const vs = pts.map((p) => p.v); if (isNum(baseline)) vs.push(baseline);
    let lo = Math.min(...vs), hi = Math.max(...vs);
    const pad = (hi - lo) * 0.12 || Math.abs(hi) * 0.01 || 1; lo -= pad; hi += pad;
    const x = scale(pts[0].t, pts[pts.length - 1].t, m.l, W - m.r);
    const y = scale(lo, hi, H - m.b, m.t);
    const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": label });
    const ticks = niceTicks(lo, hi, H > 160 ? 4 : 3);
    for (const tv of ticks) {
      svg.appendChild(s("line", { x1: m.l, x2: W - m.r, y1: y(tv), y2: y(tv), class: "grid" }));
      svg.appendChild(s("text", { x: m.l - 8, y: y(tv) + 4, class: "axis", "text-anchor": "end", text: fmtY(tv) }));
    }
    const nx = Math.max(2, Math.min(5, Math.floor(W / 110)));
    for (let i = 0; i < nx; i++) {
      const p = pts[Math.round((i / (nx - 1)) * (pts.length - 1))];
      svg.appendChild(s("text", { x: x(p.t), y: H - 6, class: "axis", "text-anchor": i === 0 ? "start" : i === nx - 1 ? "end" : "middle", text: fx(p.t) }));
    }
    const last = pts[pts.length - 1];
    const up = isNum(baseline) ? last.v >= baseline : last.v >= pts[0].v;
    const line = pts.map((p) => [x(p.t), y(p.v)]);
    const gid = "g" + Math.random().toString(36).slice(2, 8);
    svg.appendChild(s("defs", null, s("linearGradient", { id: gid, x1: 0, x2: 0, y1: 0, y2: 1 },
      s("stop", { offset: "0%", class: up ? "stop-gain" : "stop-loss", "stop-opacity": 0.22 }),
      s("stop", { offset: "100%", class: up ? "stop-gain" : "stop-loss", "stop-opacity": 0 }))));
    const area = s("path", { d: pathOf(line) + `L${line[line.length - 1][0]},${H - m.b}L${line[0][0]},${H - m.b}Z`, fill: `url(#${gid})`, class: first ? "fade-in" : "" });
    svg.appendChild(area);
    if (isNum(baseline)) {
      svg.appendChild(s("line", { x1: m.l, x2: W - m.r, y1: y(baseline), y2: y(baseline), class: "ref" }));
      svg.appendChild(s("text", { x: W - m.r, y: y(baseline) - 5, class: "axis", "text-anchor": "end", text: baselineLabel }));
    }
    svg.appendChild(s("path", { d: pathOf(line), class: `ln ${up ? "ln-gain" : "ln-loss"} ${first ? "draw" : ""}`, pathLength: first ? 1 : null }));
    svg.appendChild(s("circle", { cx: line[line.length - 1][0], cy: line[line.length - 1][1], r: 4, class: `end-dot ${up ? "gain" : "loss"}` }));
    svg.appendChild(s("circle", { cx: line[line.length - 1][0], cy: line[line.length - 1][1], r: 4, class: `pulse-ring ${up ? "gain" : "loss"}` }));
    // hover crosshair
    const cross = s("line", { y1: m.t, y2: H - m.b, class: "cross", visibility: "hidden" });
    const dot = s("circle", { r: 4.5, class: "hover-dot", visibility: "hidden" });
    svg.append(cross, dot);
    el.appendChild(svg);
    const tt = tooltip(el);
    const hit = s("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" });
    svg.appendChild(hit);
    const move = (clientX) => {
      const r = svg.getBoundingClientRect();
      const px = clientX - r.left;
      let best = 0, bd = Infinity;
      line.forEach((p, i) => { const d = Math.abs(p[0] - px); if (d < bd) { bd = d; best = i; } });
      const [cx, cy] = line[best];
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", cx); dot.setAttribute("cy", cy); dot.setAttribute("visibility", "visible");
      const p = pts[best];
      tt.show(cx, cy, [fmtY(p.v), (intraday ? timeNY(p.t) : dateNY(p.t) + " " + timeNY(p.t)), isNum(baseline) ? `${signedMoney(p.v - baseline, { cents: false })} vs ${baselineLabel.toLowerCase()}` : ""].filter(Boolean));
    };
    hit.addEventListener("pointermove", (e) => move(e.clientX));
    hit.addEventListener("pointerleave", () => { tt.hide(); cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); });
  }, { animate });
  return h("figure", { class: "fig" }, wrap,
    tableAlt(label, ["Time", "Value"], pts.slice(-40).map((p) => [dateNY(p.t) + " " + timeNY(p.t), fmtY(p.v)])));
}

// ---------------------------------------------------------------- payoff diagram
export function payoffChart(idea, { height = 170, animate = true } = {}) {
  const P = idea?.payoff?.prices || [], L = idea?.payoff?.pnl || [];
  const pts = P.map((p, i) => [p, L[i]]).filter((p) => isNum(p[0]) && isNum(p[1]));
  const spot = idea?.spot;
  const bes = (idea?.breakevens || []).filter(isNum);
  const sym = idea?.symbol || "the stock";
  const wrap = responsive("payoff", height, (el, W, H, first) => {
    if (pts.length < 2) { el.appendChild(h("p", { class: "muted small chart-empty" }, "No payoff data.")); return; }
    const m = { l: 8, r: 8, t: 14, b: 22 };
    const ys = pts.map((p) => p[1]);
    let lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
    const pad = (hi - lo) * 0.1 || 1; lo -= pad; hi += pad;
    const x = scale(pts[0][0], pts[pts.length - 1][0], m.l, W - m.r);
    const y = scale(lo, hi, H - m.b, m.t);
    const y0 = y(0);
    const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Profit or loss at expiry for ${sym} by stock price` });
    const id = Math.random().toString(36).slice(2, 8);
    const line = pts.map((p) => [x(p[0]), y(p[1])]);
    const area = pathOf(line) + `L${line[line.length - 1][0]},${y0}L${line[0][0]},${y0}Z`;
    svg.appendChild(s("defs", null,
      s("clipPath", { id: "up" + id }, s("rect", { x: 0, y: 0, width: W, height: y0 })),
      s("clipPath", { id: "dn" + id }, s("rect", { x: 0, y: y0, width: W, height: H - y0 }))));
    const g = s("g", { class: first ? "fade-in" : "" },
      s("path", { d: area, class: "zone-gain", "clip-path": `url(#up${id})` }),
      s("path", { d: area, class: "zone-loss", "clip-path": `url(#dn${id})` }));
    svg.appendChild(g);
    svg.appendChild(s("line", { x1: m.l, x2: W - m.r, y1: y0, y2: y0, class: "zero" }));
    svg.appendChild(s("path", { d: pathOf(line), class: `ln ln-ink ${first ? "draw" : ""}`, pathLength: first ? 1 : null }));
    for (const b of bes) {
      if (b < pts[0][0] || b > pts[pts.length - 1][0]) continue;
      svg.appendChild(s("circle", { cx: x(b), cy: y0, r: 3.5, class: "be-dot" }));
      svg.appendChild(s("text", { x: clamp(x(b), 24, W - 24), y: H - 6, class: "axis", "text-anchor": "middle", text: price(b) }));
    }
    if (isNum(spot) && spot >= pts[0][0] && spot <= pts[pts.length - 1][0]) {
      svg.appendChild(s("line", { x1: x(spot), x2: x(spot), y1: m.t - 4, y2: H - m.b, class: "spot" }));
      const right = x(spot) > W * 0.7;
      svg.appendChild(s("text", { x: x(spot) + (right ? -5 : 5), y: m.t + 2, class: "axis strong", "text-anchor": right ? "end" : "start", text: `Now ${price(spot)}` }));
    }
    if (y0 - y(hi) > 16) svg.appendChild(s("text", { x: m.l + 2, y: y(hi) + 10, class: "axis zone-lbl", text: "profit" }));
    if (y(lo) - y0 > 16) svg.appendChild(s("text", { x: m.l + 2, y: y(lo) - 4, class: "axis zone-lbl", text: "loss" }));
    const dot = s("circle", { r: 4, class: "hover-dot", visibility: "hidden" });
    const cross = s("line", { y1: m.t, y2: H - m.b, class: "cross", visibility: "hidden" });
    svg.append(cross, dot);
    el.appendChild(svg);
    const tt = tooltip(el);
    const hit = s("rect", { x: 0, y: 0, width: W, height: H, fill: "transparent" });
    svg.appendChild(hit);
    hit.addEventListener("pointermove", (e) => {
      const r = svg.getBoundingClientRect(); const px = e.clientX - r.left;
      let bi = 0, bd = Infinity; line.forEach((p, i) => { const d = Math.abs(p[0] - px); if (d < bd) { bd = d; bi = i; } });
      const [cx, cy] = line[bi];
      dot.setAttribute("cx", cx); dot.setAttribute("cy", cy); dot.setAttribute("visibility", "visible");
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
      tt.show(cx, cy, [`${sym} at ${price(pts[bi][0])}`, `${signedMoney(pts[bi][1])} per contract-set`]);
    });
    hit.addEventListener("pointerleave", () => { tt.hide(); dot.setAttribute("visibility", "hidden"); cross.setAttribute("visibility", "hidden"); });
  }, { animate });
  const step = Math.max(1, Math.floor(pts.length / 12));
  return h("figure", { class: "fig" }, wrap,
    tableAlt(`Payoff at expiry for ${sym}`, [`${sym} price at expiry`, "Profit / loss per contract-set"],
      pts.filter((_, i) => i % step === 0).map((p) => [price(p[0]), signedMoney(p[1])])));
}

// ---------------------------------------------------------------- sparkline (fixed size)
export function sparkline(values, { w = 120, hgt = 32, label = "Price today" } = {}) {
  const v = (values || []).filter(isNum);
  const svg = s("svg", { width: w, height: hgt, viewBox: `0 0 ${w} ${hgt}`, class: "spark", role: "img", "aria-label": v.length ? `${label}: from ${price(v[0])} to ${price(v[v.length - 1])}` : `${label}: no data` });
  if (v.length < 2) return svg;
  const lo = Math.min(...v), hi = Math.max(...v);
  const x = scale(0, v.length - 1, 1, w - 4), y = scale(lo, hi, hgt - 3, 3);
  const up = v[v.length - 1] >= v[0];
  svg.appendChild(s("line", { x1: 0, x2: w, y1: y(v[0]), y2: y(v[0]), class: "ref thin" }));
  svg.appendChild(s("path", { d: pathOf(v.map((p, i) => [x(i), y(p)])), class: `ln ${up ? "ln-gain" : "ln-loss"}` }));
  svg.appendChild(s("circle", { cx: x(v.length - 1), cy: y(v[v.length - 1]), r: 2.5, class: `end-dot ${up ? "gain" : "loss"}` }));
  return svg;
}

// ---------------------------------------------------------------- opening-range mini chart
export function orbChart(orb, closes, { height = 120, symbol = "" } = {}) {
  const v = (closes || []).filter(isNum);
  const levels = orb ? [
    { k: "or_high", lbl: "Range high", cls: "lv-range" }, { k: "or_low", lbl: "Range low", cls: "lv-range" }, { k: "or_mid", lbl: "Mid", cls: "lv-mid" },
    { k: "entry", lbl: "Entry", cls: "lv-entry" }, { k: "stop", lbl: "Stop", cls: "lv-stop" }, { k: "target", lbl: "Target", cls: "lv-target" },
  ].filter((l) => isNum(orb[l.k])) : [];
  const wrap = responsive("orb", height, (el, W, H, first) => {
    if (v.length < 2) { el.appendChild(h("p", { class: "muted small chart-empty" }, "Price data appears once the session starts.")); return; }
    const m = { l: 4, r: 66, t: 8, b: 8 };
    const all = [...v, ...levels.map((l) => orb[l.k])];
    let lo = Math.min(...all), hi = Math.max(...all); const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
    const x = scale(0, Math.max(v.length - 1, 1), m.l, W - m.r), y = scale(lo, hi, H - m.b, m.t);
    const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${symbol} opening range chart` });
    if (isNum(orb?.or_high) && isNum(orb?.or_low)) svg.appendChild(s("rect", { x: m.l, y: y(orb.or_high), width: W - m.l - m.r, height: Math.max(1, y(orb.or_low) - y(orb.or_high)), class: "range-band" }));
    const used = [];
    for (const l of levels) {
      const yy = y(orb[l.k]);
      svg.appendChild(s("line", { x1: m.l, x2: W - m.r, y1: yy, y2: yy, class: `lv ${l.cls}` }));
      let ty = yy + 3; while (used.some((u) => Math.abs(u - ty) < 10)) ty += 10; used.push(ty);
      svg.appendChild(s("text", { x: W - m.r + 4, y: clamp(ty, 9, H - 2), class: `axis lv-lbl ${l.cls}`, text: `${l.lbl} ${price(orb[l.k])}` }));
    }
    svg.appendChild(s("path", { d: pathOf(v.map((p, i) => [x(i), y(p)])), class: `ln ln-ink ${first ? "draw" : ""}`, pathLength: first ? 1 : null }));
    svg.appendChild(s("circle", { cx: x(v.length - 1), cy: y(v[v.length - 1]), r: 3, class: "end-dot ink" }));
    el.appendChild(svg);
  });
  return wrap;
}

// ---------------------------------------------------------------- volatility smile
export function smileChart(slice, { height = 320, spot = null, onPoint } = {}) {
  const pts = (slice?.points || []).filter((p) => isNum(p.strike) && isNum(p.market_iv));
  const fs = slice?.fit_curve?.strikes || [], fi = slice?.fit_curve?.iv || [];
  const fit = fs.map((k, i) => [k, fi[i]]).filter((p) => isNum(p[0]) && isNum(p[1]));
  const wrap = responsive("smile", height, (el, W, H, first) => {
    if (pts.length < 2) { el.appendChild(h("p", { class: "muted small chart-empty" }, "Not enough quotes in this expiry to draw a smile.")); return; }
    const m = { l: 48, r: 14, t: 14, b: 30 };
    const ks = [...pts.map((p) => p.strike), ...fit.map((p) => p[0])];
    const ivs = [...pts.flatMap((p) => [p.market_iv, p.bid_iv, p.ask_iv]), ...fit.map((p) => p[1])].filter(isNum);
    let lo = Math.min(...ivs), hi = Math.max(...ivs); const pad = (hi - lo) * 0.1 || 0.01; lo -= pad; hi += pad;
    const x = scale(Math.min(...ks), Math.max(...ks), m.l, W - m.r), y = scale(lo, hi, H - m.b, m.t);
    const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Volatility smile for expiry ${slice.expiry}` });
    for (const tv of niceTicks(lo, hi, 4)) {
      svg.appendChild(s("line", { x1: m.l, x2: W - m.r, y1: y(tv), y2: y(tv), class: "grid" }));
      svg.appendChild(s("text", { x: m.l - 6, y: y(tv) + 4, class: "axis", "text-anchor": "end", text: pct(tv, 0) }));
    }
    for (const tv of niceTicks(Math.min(...ks), Math.max(...ks), Math.max(3, Math.floor(W / 90)))) {
      svg.appendChild(s("text", { x: x(tv), y: H - 10, class: "axis", "text-anchor": "middle", text: num(tv, tv % 1 ? 1 : 0) }));
    }
    svg.appendChild(s("text", { x: W - m.r, y: H - 0, class: "axis", "text-anchor": "end", text: "Strike price →" }));
    if (isNum(spot)) {
      const sx = x(spot);
      if (sx >= m.l && sx <= W - m.r) {
        svg.appendChild(s("line", { x1: sx, x2: sx, y1: m.t, y2: H - m.b, class: "spot" }));
        svg.appendChild(s("text", { x: sx + 4, y: m.t + 8, class: "axis strong", text: `Stock ${price(spot)}` }));
      }
    }
    if (fit.length > 1) svg.appendChild(s("path", { d: pathOf(fit.map((p) => [x(p[0]), y(p[1])])), class: `ln ln-fit ${first ? "draw" : ""}`, pathLength: first ? 1 : null }));
    const marks = [];
    pts.forEach((p, i) => {
      const cx = x(p.strike), cy = y(p.market_iv);
      const g = s("g", { class: `pt ${p.flag ? "flag-" + p.flag : ""} ${first ? "pop-in" : ""}` });
      g.style.setProperty("--d", `${Math.min(600, i * 18)}ms`); // CSSOM, not a style attribute (strict CSP)
      if (isNum(p.bid_iv) && isNum(p.ask_iv)) g.appendChild(s("line", { x1: cx, x2: cx, y1: y(p.bid_iv), y2: y(p.ask_iv), class: "whisker" }));
      if (p.flag) g.appendChild(s("circle", { cx, cy, r: 9, class: "flag-pulse" }));
      g.appendChild(p.right === "P"
        ? s("rect", { x: cx - 3.5, y: cy - 3.5, width: 7, height: 7, class: "dot put" })
        : s("circle", { cx, cy, r: 4, class: "dot call" }));
      svg.appendChild(g);
      marks.push([cx, cy, p]);
    });
    el.appendChild(svg);
    const tt = tooltip(el);
    const hit = s("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" });
    svg.appendChild(hit);
    const ring = s("circle", { r: 7, class: "hover-ring", visibility: "hidden" });
    svg.appendChild(ring);
    hit.addEventListener("pointermove", (e) => {
      const r = svg.getBoundingClientRect(); const px = e.clientX - r.left, py = e.clientY - r.top;
      let best = null, bd = Infinity;
      for (const mk of marks) { const d = Math.hypot(mk[0] - px, (mk[1] - py) * 0.5); if (d < bd) { bd = d; best = mk; } }
      if (!best || bd > 40) { tt.hide(); ring.setAttribute("visibility", "hidden"); return; }
      const p = best[2];
      ring.setAttribute("cx", best[0]); ring.setAttribute("cy", best[1]); ring.setAttribute("visibility", "visible");
      tt.show(best[0], best[1], [
        `${num(p.strike, p.strike % 1 ? 1 : 0)} ${p.right === "P" ? "put" : "call"}${p.flag ? " — " + p.flag : ""}`,
        `Market IV ${pct(p.market_iv)} (bid ${pct(p.bid_iv)} / ask ${pct(p.ask_iv)})`,
        `Fitted fair IV ${pct(p.fit_iv)}`,
        isNum(p.zscore) ? `Z-score ${p.zscore.toFixed(1)}` : "",
      ].filter(Boolean));
      if (onPoint) onPoint(p);
    });
    hit.addEventListener("pointerleave", () => { tt.hide(); ring.setAttribute("visibility", "hidden"); });
  });
  return h("figure", { class: "fig" }, wrap,
    tableAlt(`Smile points for ${slice?.expiry}`, ["Strike", "Type", "Market IV", "Bid IV", "Ask IV", "Fitted IV", "Flag"],
      pts.map((p) => [num(p.strike, p.strike % 1 ? 1 : 0), p.right === "P" ? "Put" : "Call", pct(p.market_iv), pct(p.bid_iv), pct(p.ask_iv), pct(p.fit_iv), p.flag || "—"])));
}

// ---------------------------------------------------------------- countdown ring
export function ring(fraction, { size = 44, stroke = 4, label = "" } = {}) {
  const r = (size - stroke) / 2, c = 2 * Math.PI * r;
  const fg = s("circle", { cx: size / 2, cy: size / 2, r, class: "ring-fg", "stroke-width": stroke, "stroke-dasharray": c.toFixed(2), "stroke-dashoffset": (c * (1 - clamp(fraction, 0, 1))).toFixed(2), transform: `rotate(-90 ${size / 2} ${size / 2})` });
  const svg = s("svg", { width: size, height: size, viewBox: `0 0 ${size} ${size}`, class: "ring", role: "img", "aria-label": label },
    s("circle", { cx: size / 2, cy: size / 2, r, class: "ring-bg", "stroke-width": stroke }), fg);
  svg._set = (f) => fg.setAttribute("stroke-dashoffset", (c * (1 - clamp(f, 0, 1))).toFixed(2));
  return svg;
}

// ---------------------------------------------------------------- regime gauge (semicircle)
export function gauge(direction, { size = 180, label = "Market regime" } = {}) {
  const W = size, H = size * 0.6 + 16, cx = W / 2, cy = size * 0.6 - 4, r = W / 2 - 12;
  const arc = (a0, a1) => {
    const p0 = [cx + r * Math.cos(a0), cy - r * Math.sin(a0)], p1 = [cx + r * Math.cos(a1), cy - r * Math.sin(a1)];
    return `M${p0[0]},${p0[1]}A${r},${r} 0 0 1 ${p1[0]},${p1[1]}`;
  };
  const d = isNum(direction) ? clamp(direction, -1, 1) : 0;
  const ang = Math.PI / 2 - d * (Math.PI / 2);
  const needle = s("line", { x1: cx, y1: cy, x2: cx, y2: cy - r + 10, class: "needle" });
  needle.style.transform = `rotate(${(Math.PI / 2 - ang) * 180 / Math.PI}deg)`;
  needle.style.transformOrigin = `${cx}px ${cy}px`;
  return s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, class: "gauge", role: "img", "aria-label": `${label}: ${d > 0.15 ? "bullish" : d < -0.15 ? "bearish" : "neutral"} (${d.toFixed(2)})` },
    s("path", { d: arc(Math.PI, Math.PI * 0.62), class: "g-arc g-loss" }),
    s("path", { d: arc(Math.PI * 0.6, Math.PI * 0.4), class: "g-arc g-flat" }),
    s("path", { d: arc(Math.PI * 0.38, 0), class: "g-arc g-gain" }),
    needle,
    s("circle", { cx, cy, r: 5, class: "needle-hub" }),
    s("text", { x: 10, y: H - 2, class: "axis", text: "Bearish" }),
    s("text", { x: W - 10, y: H - 2, class: "axis", "text-anchor": "end", text: "Bullish" }));
}

// ---------------------------------------------------------------- diverging bar list (HTML)
export function pnlBars(rows, { labelOf, valueOf, metaOf } = {}) {
  const vals = rows.map(valueOf).filter(isNum);
  const max = Math.max(1, ...vals.map(Math.abs));
  return h("ul", { class: "pnl-bars" }, rows.map((r, i) => {
    const v = valueOf(r);
    const w = isNum(v) ? Math.abs(v) / max : 0;
    const bar = h("span", { class: `pb-bar ${v >= 0 ? "gain" : "loss"}` });
    bar.style.setProperty("--w", w.toFixed(3));
    bar.style.setProperty("--d", `${i * 60}ms`);
    return h("li", null,
      h("span", { class: "pb-label" }, labelOf(r), metaOf ? h("span", { class: "muted small" }, " " + metaOf(r)) : null),
      h("span", { class: "pb-track" }, h("span", { class: "pb-axis" }), bar),
      h("span", { class: `pb-val val ${v > 0 ? "gain" : v < 0 ? "loss" : "flat"}` }, signedMoney(v, { cents: false })));
  }));
}

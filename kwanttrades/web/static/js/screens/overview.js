// Home: account value, one plain-words status card, risk used today, top ideas, positions summary, equity curve.
import { h, s, panel, term, stagger, signed, toast, announce, tweenNumber, meter, firstTime } from "../dom.js";
import { store } from "../store.js";
import { api } from "../api.js";
import { money, signedMoney, timeNY, dateNY, parseTime, clock, isNum, tone, arrow, pct } from "../format.js";
import { lineChart } from "../charts.js";
import { MODES, MARKET_STATES } from "../glossary.js";
import { group, row, icon, openSheet, bar } from "../ui.js";
import { ideaRow, quoteNote, emptyIdeas, serverNow, modeBadge } from "../shared.js";
import { roadCard } from "../graduation.js";

export const meta = {
  title: "Home",
  eyebrow: () => h("p", { class: "wordmark", "aria-label": "Kwant Trades" },
    s("svg", { width: 18, height: 18, viewBox: "0 0 32 32", class: "brand-mark", "aria-hidden": "true" },
      s("path", { d: "M3 24 L12 15 L18 20 L29 7", class: "bm-line" }), s("circle", { cx: 29, cy: 7, r: 3, class: "bm-dot" })),
    "KWANT", h("span", { class: "brand-edge" }, "TRADES")),
};

// ---------------------------------------------------------------- risk numbers
function riskNumbers(st) {
  const acc = st.account || {};
  const cfg = store.data("config");
  const eq = acc.equity;
  const lim = st.limits || {};
  const riskCap = isNum(lim.max_portfolio_risk) ? lim.max_portfolio_risk
    : isNum(cfg?.risk?.max_portfolio_risk_pct) && isNum(eq) ? cfg.risk.max_portfolio_risk_pct * eq : null;
  const lossCap = isNum(lim.daily_loss_limit) ? lim.daily_loss_limit
    : isNum(cfg?.risk?.daily_loss_limit_pct) && isNum(eq) ? cfg.risk.daily_loss_limit_pct * (eq - Math.min(0, acc.day_pnl || 0)) : null;
  const loss = isNum(acc.day_pnl) ? Math.max(0, -acc.day_pnl) : null;
  const dollars = (v, c) => `${money(v, { cents: false })} of ${isNum(c) ? money(c, { cents: false }) : "–"}`;
  const list = [
    { label: "Open risk", key: "open_risk", value: acc.open_risk, cap: riskCap, fmt: dollars, help: "The worst case of everything you hold right now, added up." },
    { label: "Trades today", key: "contracts", value: st.trades_today, cap: st.max_trades_per_day, fmt: (v, c) => `${isNum(v) ? v : "–"} of ${isNum(c) ? c : "–"}`, help: "New trades the system may open per day." },
    { label: "Losses today", key: "daily_loss", value: loss, cap: lossCap, fmt: dollars, help: "If today's losses reach the limit, the kill switch stops all trading for the day." },
  ];
  for (const r of list) r.ratio = isNum(r.value) && isNum(r.cap) && r.cap > 0 ? Math.max(0, Math.min(1, r.value / r.cap)) : null;
  return list;
}

function openRiskSheet() {
  return openSheet({
    title: "Risk used today",
    build: (body) => {
      const host = h("div");
      body.append(h("p", { class: "muted sheet-lead" }, "Three limits protect the account. Each bar shows how much of its limit is used."), host);
      const draw = () => {
        const st = store.data("status");
        if (!st) { host.replaceChildren(h("p", { class: "muted" }, "Waiting for data…")); return; }
        host.replaceChildren(group(riskNumbers(st).map((r) => h("li", { class: "row" }, h("div", { class: "row-main static stacked" },
          h("div", { class: "risk-line" }, h("span", { class: "cell-title" }, term(r.key, r.label)), h("span", { class: "num muted" }, r.fmt(r.value, r.cap))),
          meter(r.value, r.cap, { label: r.label, fmt: () => "" }),
          h("p", { class: "cell-sub" }, r.help))))));
      };
      draw();
      return store.subscribe("status", draw);
    },
  });
}

// ---------------------------------------------------------------- status card
function marketLine(st) {
  const m = st.market || {};
  const now = serverNow();
  const close = parseTime(m.next_close), open = parseTime(m.next_open), orEnd = parseTime(m.opening_range_ends);
  const name = MARKET_STATES[m.state] || "Market state unknown";
  if (m.state === "opening_range" && orEnd) return `${name} · range set in ${clock(orEnd - now)}`;
  if (m.state === "open" && close) return `${name} · closes in ${clock(close - now)}`;
  if (m.state === "pre" && open) return `${name} · opens in ${clock(open - now)}`;
  if (m.state === "closed" && open) return `${name} · opens ${dateNY(open, { weekday: true })} ${timeNY(open)} NY`;
  return name;
}

function statusCard() {
  const ico = h("span", { class: "sc-ico", "aria-hidden": "true" });
  const head = h("p", { class: "sc-head" });
  const sub = h("p", { class: "sc-sub" });
  const btn = h("button", { type: "button", class: "btn primary sc-btn" });
  const el = h("section", { class: "status-card cu", "aria-label": "Status", "aria-live": "polite" }, ico, h("div", { class: "sc-text" }, head, sub), btn);
  let busy = false;
  const act = async (fn, okMsg, failMsg) => {
    busy = true; draw();
    try { const r = await fn(); if (r && r.mode) store.set("status", r); toast(okMsg); announce(okMsg); }
    catch (e) { toast(`${failMsg}: ${e.message}`, "error"); }
    busy = false; store.refresh("status"); draw();
  };
  btn.addEventListener("click", () => {
    const st = store.data("status");
    if (st?.running) act(() => api.stop(), "Stopped. Nothing new will be traded.", "Couldn't stop");
    else act(() => api.start("paper"), "Practice started with paper money.", "Couldn't start");
  });
  function draw() {
    const st = store.data("status");
    if (!st) { head.textContent = "Connecting…"; sub.textContent = ""; btn.hidden = true; return; }
    const market = MARKET_STATES[st.market?.state] || "Market state unknown";
    const tradesTxt = isNum(st.trades_today) ? ` · ${st.trades_today} of ${st.max_trades_per_day ?? "–"} trades used` : "";
    let kind = "idle", icoName = "pause";
    btn.hidden = false; btn.disabled = busy;
    btn.classList.remove("danger-ghost", "primary");
    if (st.kill_switch) {
      kind = "kill"; icoName = "shield";
      head.textContent = "Kill switch on: trading stopped";
      sub.textContent = "No new trades will be placed today. Open positions are not added to.";
      if (st.running) { btn.textContent = busy ? "Stopping…" : "Stop"; btn.classList.add("danger-ghost"); } else btn.hidden = true;
    } else if (st.running) {
      const practice = !!MODES[st.mode]?.practice;
      kind = practice ? "practice" : "watch"; icoName = practice ? "paper" : "eye";
      head.textContent = practice ? "Practising with paper money" : "Watching only, no orders sent";
      sub.textContent = marketLine(st) + tradesTxt;
      btn.textContent = busy ? "Stopping…" : "Stop"; btn.classList.add("danger-ghost");
    } else {
      head.textContent = `${market} · Not trading`;
      sub.textContent = st.market?.state === "closed" ? marketLine(st) : "Nothing will be traded until you start practice.";
      btn.textContent = busy ? "Starting…" : "Start practice"; btn.classList.add("primary");
    }
    el.dataset.kind = kind;
    ico.replaceChildren(icon(icoName, { size: 22 }));
  }
  const u = store.subscribe("status", draw);
  const iv = setInterval(() => { if (!document.hidden && !busy) draw(); }, 1000);
  el._cleanup = () => { u(); clearInterval(iv); };
  draw();
  return el;
}

// ---------------------------------------------------------------- hero (account value)
function hero() {
  const val = h("span", { class: "hero-val num" }, "–");
  const dayArw = h("span", { class: "arw", "aria-hidden": "true" });
  const dayVal = h("span", { class: "num" }, "–");
  const day = h("p", { class: "hero-day val flat" }, dayArw, " ", dayVal, h("span", { class: "muted" }, " today"));
  const el = h("section", { class: "hero cu", "aria-label": "Account value" }, h("p", { class: "hero-lbl" }, "Account value"), val, day);
  const draw = () => {
    const st = store.data("status");
    const acc = st?.account || {};
    tweenNumber(val, acc.equity, (v) => money(v));
    tweenNumber(dayVal, acc.day_pnl, (v) => signedMoney(v), { flash: false });
    day.className = `hero-day val ${tone(acc.day_pnl)}`;
    dayArw.textContent = arrow(acc.day_pnl);
  };
  el._cleanup = store.subscribe("status", draw);
  draw();
  return el;
}

// ---------------------------------------------------------------- risk row
function riskRow() {
  const pctTxt = h("span", { class: "num" }, "–");
  const barHost = h("span", { class: "risk-bar" });
  const sub = h("span", { class: "risk-sub" });
  const cell = row({
    title: "Risk used today", sub: h("span", { class: "risk-stack" }, barHost, sub), trailing: pctTxt, chevron: true,
    onclick: () => openRiskSheet(), haspopup: "dialog", label: "Risk used today. Open the three details.",
  });
  const list = h("ul", { class: "group cu", "aria-label": "Risk used today" }, cell);
  const draw = () => {
    const st = store.data("status");
    if (!st) return;
    const r = riskNumbers(st);
    const known = r.filter((x) => x.ratio != null);
    const top = known.sort((a, b) => b.ratio - a.ratio)[0];
    const ratio = top ? top.ratio : 0;
    pctTxt.textContent = top ? `${Math.round(ratio * 100)}%` : "–";
    pctTxt.className = `num risk-pct ${ratio >= 1 ? "loss" : ratio >= 0.75 ? "warn" : ""}`;
    barHost.replaceChildren(bar(ratio, { level: ratio >= 1 ? "max" : ratio >= 0.75 ? "hot" : "ok", label: `${Math.round(ratio * 100)}% of the tightest limit used` }));
    sub.textContent = top ? `Tightest: ${top.label.toLowerCase()}, ${top.fmt(top.value, top.cap)}` : "No limits reported yet";
  };
  list._cleanup = store.subscribe("status", draw);
  list.dataset.cfg = "1";
  const u2 = store.subscribe("config", draw);
  const prev = list._cleanup;
  list._cleanup = () => { prev(); u2(); };
  draw();
  return h("div", { class: "group-wrap" }, list);
}

// ---------------------------------------------------------------- render
export function render(root, params, ctx) {
  // mode pill in the nav bar
  const pillHost = h("span", { class: "pill-host cu" });
  const drawPill = () => { const st = store.data("status"); pillHost.replaceChildren(st ? modeBadge(st) : ""); };
  pillHost._cleanup = store.subscribe("status", drawPill);
  drawPill();
  ctx.trail.append(pillHost);

  const first = firstTime("home");

  const top = panel({
    title: "Top ideas", id: "home-ideas", store, key: "ideas",
    actions: h("a", { href: "#/ideas", class: "link" }, "See all"),
    render: (d) => {
      const ideas = (d?.ideas || []).slice().sort((a, b) => (b.score ?? -1e9) - (a.score ?? -1e9)).slice(0, 3);
      if (!ideas.length) return null;
      const g = group(ideas.map((i) => ideaRow(i)), { label: "Top ideas" });
      return h("div", null, g, h("p", { class: "group-foot" }, d.asof ? `From the scan at ${timeNY(d.asof)} NY.` : ""));
    },
    empty: emptyIdeas,
  });

  const positions = panel({
    title: "Your positions", id: "home-positions", store, key: "positions",
    render: (d) => {
      const open = d?.open || [];
      if (!open.length) return null;
      const total = open.reduce((a, p) => a + (isNum(p.unrealized_pnl) ? p.unrealized_pnl : 0), 0);
      return group([row({
        title: `${open.length} open position${open.length > 1 ? "s" : ""}`, sub: "Open profit/loss",
        trailing: signed(signedMoney(total), total), chevron: true, href: "#/portfolio",
      })]);
    },
    empty: { title: "No open positions", hint: "When the system takes a trade in a paper mode it will appear here." },
  });

  const equity = panel({
    title: "Account value over time", id: "home-equity", store, key: "equity",
    render: (d, { first: f }) => {
      const pts = (d?.points || []).map((p) => ({ t: p.t, v: p.equity }));
      if (pts.length < 2) return null;
      const last = pts[pts.length - 1].v, start = d.starting_equity;
      return h("div", { class: "card chart-card" },
        isNum(start) ? h("p", { class: "chart-cap" }, signed(`${signedMoney(last - start, { cents: false })} since start`, last - start)) : null,
        lineChart(pts, { height: 180, baseline: isNum(start) ? start : null, baselineLabel: "Starting equity", label: "Account value over time", animate: f }));
    },
    empty: { title: "No equity history yet", hint: "The curve starts once a session has run." },
  });

  const sections = [hero(), statusCard(), riskRow(), roadCard(), top, quoteNote(), positions, equity,
    h("p", { class: "footer-note" }, "Model estimates, not guarantees. Paper results do not predict live results.")];
  if (first) stagger(sections, 0, 55);
  root.append(...sections);
}

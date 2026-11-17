import { h, panel, term, stagger, signed, toast, announce } from "../dom.js";
import { store } from "../store.js";
import { api, isDemo } from "../api.js";
import { money, signedMoney, pct, num, isNum, dateNY, timeNY } from "../format.js";
import { lineChart, pnlBars } from "../charts.js";
import { strategyInfo, exitLabel } from "../glossary.js";

const form = { symbols: null, days: 5, source: "real_bars" };

export function render(root) {
  const st = store.data("status");
  const universe = st?.universe?.length ? st.universe : ["SPY", "QQQ", "IWM"];
  if (!form.symbols) form.symbols = universe.slice(0, 3);

  const symBox = h("fieldset", { class: "sym-picks" }, h("legend", null, "Symbols"),
    ...universe.map((s) => {
      const cb = h("input", { type: "checkbox", value: s, checked: form.symbols.includes(s) });
      cb.addEventListener("change", () => { form.symbols = [...symBox.querySelectorAll("input:checked")].map((x) => x.value); validate(); });
      return h("label", { class: "pick" }, cb, h("span", null, s));
    }));
  const days = h("input", { type: "number", id: "bt-days", min: "1", max: "60", step: "1", value: String(form.days), inputmode: "numeric", "aria-describedby": "bt-days-help" });
  const srcReal = h("input", { type: "radio", name: "bt-src", value: "real_bars", checked: form.source === "real_bars" });
  const srcSim = h("input", { type: "radio", name: "bt-src", value: "simulated", checked: form.source === "simulated" });
  const err = h("p", { class: "field-err", role: "alert" });
  const run = h("button", { type: "submit", class: "btn primary" }, "Run backtest");
  const validate = () => {
    const d = Number(days.value);
    let msg = "";
    if (!form.symbols.length) msg = "Pick at least one symbol.";
    else if (!Number.isInteger(d) || d < 1 || d > 60) msg = "Days must be a whole number from 1 to 60.";
    err.textContent = msg;
    days.setAttribute("aria-invalid", String(!!msg && msg.startsWith("Days")));
    const running = store.data("backtest")?.state === "running";
    run.disabled = !!msg || running;
    run.textContent = running ? "Running…" : "Run backtest";
    return !msg;
  };
  days.addEventListener("input", () => { form.days = Number(days.value); validate(); });
  [srcReal, srcSim].forEach((r) => r.addEventListener("change", () => { form.source = r.value; }));

  const f = h("form", { class: "bt-form", novalidate: true },
    symBox,
    h("div", { class: "field" }, h("label", { for: "bt-days" }, "Trading days to replay"), days, h("p", { class: "help", id: "bt-days-help" }, "1–60. Free 1-minute data covers only the last ~7 trading days, so real-price runs are capped at 7.")),
    h("fieldset", { class: "field" }, h("legend", null, "Price source"),
      h("label", { class: "check" }, srcReal, h("span", null, "Real stock prices + synthetic option prices")),
      h("label", { class: "check" }, srcSim, h("span", null, "Fully simulated prices (for testing the plumbing)"))),
    err, h("div", { class: "form-actions" }, run));
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!validate()) return;
    run.disabled = true;
    try {
      await api.backtest({ symbols: form.symbols, days: form.days, source: form.source });
      toast("Backtest started. Option prices in backtests are modelled, not real quotes.");
      announce("Backtest started");
    } catch (ex) { toast(`Couldn't start the backtest: ${ex.message}`, "error"); }
    store.refresh("backtest");
  });

  const formPanel = h("section", { class: "panel cu", "aria-label": "Backtest settings" },
    h("header", { class: "panel-head" }, h("h2", null, "Run a backtest")),
    h("p", { class: "panel-note" }, "Replays past sessions through the same strategy and risk rules with practice money. Free historical option quotes don't exist, so option prices are modelled."), f);
  const u = store.subscribe("backtest", validate);
  formPanel._cleanup = u;
  validate();

  const results = panel({
    title: "Results", cls: "span-2", store, key: "backtest",
    render: (d, { first }) => {
      if (!d || d.state === "idle") return null;
      const out = h("div", { class: "bt-results" });
      // caveats first, always visible
      const cav = (d.caveats || []).filter(Boolean);
      if (cav.length) out.appendChild(h("section", { class: "caveats", "aria-label": "Caveats" },
        h("h3", null, h("span", { "aria-hidden": "true" }, "⚠ "), "Read this first"),
        h("ul", null, cav.map((c) => h("li", null, String(c))))));
      if (d.state === "running") {
        const fill = h("span", { class: "bar-fill" }); fill.style.transform = `scaleX(${isNum(d.progress) ? d.progress : 0})`;
        out.appendChild(h("div", { class: "bt-progress" }, h("p", null, h("span", { class: "pulse-dot", "aria-hidden": "true" }), ` Running… ${pct(d.progress, 0)}`),
          h("span", { class: "bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round((d.progress || 0) * 100)), "aria-label": "Backtest progress" }, fill)));
      }
      if (d.state === "error") out.appendChild(h("p", { class: "state err" }, "The backtest failed. Check the server log and try again."));
      const sm = d.summary;
      if (sm) {
        const tile = (lbl, v, cls = "") => h("div", { class: `tile ${cls}` }, h("dt", null, lbl), h("dd", { class: "num" }, v));
        out.appendChild(h("dl", { class: "tiles" },
          tile("Total result", signed(signedMoney(sm.total_pnl, { cents: false }), sm.total_pnl)),
          tile("Trades", num(sm.trades)),
          tile(term("win_rate", "Win rate"), pct(sm.win_rate, 0)),
          tile(term("profit_factor", "Profit factor"), num(sm.profit_factor, 2)),
          tile(term("expectancy", "Avg per trade"), signedMoney(sm.expectancy)),
          tile(term("sharpe", "Sharpe"), num(sm.sharpe, 2)),
          tile(term("max_drawdown", "Max drawdown"), pct(sm.max_drawdown, 1))));
      }
      const eq = (d.equity || []).map((p) => ({ t: p.t, v: p.equity }));
      if (eq.length >= 2) {
        out.appendChild(h("h3", null, "Simulated account value"));
        out.appendChild(lineChart(eq, { height: 200, baseline: eq[0].v, baselineLabel: "Start", label: "Backtest equity curve", animate: first }));
      }
      if ((d.by_strategy || []).length || (d.by_exit || []).length) {
        out.appendChild(h("div", { class: "two-col" },
          h("div", null, h("h3", null, "By strategy"), pnlBars(d.by_strategy || [], { labelOf: (r) => strategyInfo(r.strategy).label, valueOf: (r) => r.pnl, metaOf: (r) => `${r.trades} trades · ${pct(r.win_rate, 0)} won` })),
          h("div", null, h("h3", null, "By exit reason"), pnlBars(d.by_exit || [], { labelOf: (r) => exitLabel(r.reason), valueOf: (r) => r.pnl, metaOf: (r) => `${r.trades} trades` }))));
      }
      const trades = d.trades || [];
      if (trades.length) {
        out.appendChild(h("h3", null, `Trades (${trades.length})`));
        out.appendChild(h("div", { class: "tbl-wrap tall" }, h("table", { class: "tbl compact" },
          h("caption", { class: "sr-only" }, "Backtest trades"),
          h("thead", null, h("tr", null, ...["Opened", "Symbol", "Trade", "Exit"].map((x) => h("th", { scope: "col" }, x)), h("th", { scope: "col", class: "num" }, "Result"))),
          h("tbody", null, trades.slice().reverse().slice(0, 200).map((t) => h("tr", null,
            h("td", { class: "num" }, dateNY(t.opened), " ", timeNY(t.opened)), h("td", null, h("strong", { class: "sym" }, t.symbol ?? "—")),
            h("td", { class: "small" }, strategyInfo(t.strategy).label), h("td", { class: "small" }, exitLabel(t.exit_reason)),
            h("td", { class: "num" }, signed(signedMoney(t.pnl), t.pnl))))))));
      }
      return out;
    },
    empty: { title: "No backtest yet", hint: "Pick symbols and days, then Run backtest. Results show here with their caveats." },
  });

  const head = h("section", { class: "panel span-3 toolbar" }, h("h1", { class: "screen-title" }, "Backtest"),
    h("p", { class: "muted" }, "Test the rules on past days. ", h("strong", null, "Backtests are simulations: they do not predict paper or live results."), isDemo() ? " In demo mode the results below are generated sample data." : ""));
  const grid = h("div", { class: "grid" }, head, formPanel, results);
  stagger([...grid.children], 0, 60);
  root.append(grid);
}

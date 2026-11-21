import { h, panel, term, stagger, flagChip, firstTime } from "../dom.js";
import { store } from "../store.js";
import { api } from "../api.js";
import { price, pct, isNum, timeNY, ymd, num, signedNum } from "../format.js";
import { smileChart } from "../charts.js";
import { quoteNote } from "../shared.js";
import { group } from "../ui.js";

let current = { symbol: null, expiry: null };

function ensureKey(sym) {
  const key = `surface:${sym}`;
  store.define(key, () => api.surface(sym), { interval: 15000 });
  return key;
}

export const meta = { title: "Volatility" };

export function render(root, params) {
  const st = store.data("status");
  const universe = st?.universe?.length ? st.universe : ["SPY"];
  if (params[0]) current.symbol = String(params[0]).toUpperCase();
  if (!current.symbol || (!universe.includes(current.symbol) && !params[0])) current.symbol = universe[0];

  const symSel = h("select", { id: "v-sym" }, universe.map((s) => h("option", { value: s }, s)));
  if (!universe.includes(current.symbol)) symSel.appendChild(h("option", { value: current.symbol }, current.symbol));
  symSel.value = current.symbol;
  const expSel = h("select", { id: "v-exp", disabled: true }, h("option", { value: "" }, "Loading…"));
  const legend = h("ul", { class: "legend small" },
    h("li", null, h("span", { class: "lg-dot call", "aria-hidden": "true" }), "Call: market IV"),
    h("li", null, h("span", { class: "lg-sq put", "aria-hidden": "true" }), "Put: market IV"),
    h("li", null, h("span", { class: "lg-whisk", "aria-hidden": "true" }), "Bid–ask range"),
    h("li", null, h("span", { class: "lg-line", "aria-hidden": "true" }), term("svi", "Fitted curve")),
    h("li", null, h("span", { class: "lg-flag cheap", "aria-hidden": "true" }), term("cheap", "Cheap")),
    h("li", null, h("span", { class: "lg-flag rich", "aria-hidden": "true" }), term("rich", "Rich")));

  const head = h("div", { class: "vol-head" },
    h("p", { class: "lead" }, "Each dot is what the market charges for one option, expressed as ", term("iv", "implied volatility"), ". The line is a smooth fair-value curve. Dots clearly off the line, beyond their bid–ask range, are flagged as cheap or rich."),
    h("div", { class: "filters" }, h("label", { for: "v-sym" }, "Symbol", symSel), h("label", { for: "v-exp" }, "Expiry", expSel)),
    quoteNote());

  const host = h("div", { class: "vol-host" });
  let unmountPanels = [];
  const mount = () => {
    unmountPanels.forEach((p) => p._cleanup && p._cleanup());
    host.replaceChildren();
    const key = ensureKey(current.symbol);
    let lastSurf = null;
    const chart = panel({
      title: `${current.symbol} volatility smile`, id: "vol-smile", store, key,
      render: (d, { first }) => {
        lastSurf = d;
        const slices = d?.slices || [];
        const want = slices.map((s) => s.expiry).join("|");
        if ([...expSel.options].map((o) => o.value).join("|") !== want) {
          expSel.replaceChildren(...slices.map((s) => h("option", { value: s.expiry }, `${ymd(s.expiry)} (${isNum(s.dte) ? s.dte + " days" : "–"})`)));
        }
        expSel.disabled = !slices.length;
        if (!slices.find((s) => s.expiry === current.expiry)) current.expiry = slices.length > 1 ? slices[1].expiry : slices[0]?.expiry;
        expSel.value = current.expiry || "";
        const sl = slices.find((s) => s.expiry === current.expiry);
        if (!sl) return null;
        const flagged = (sl.points || []).filter((p) => p.flag);
        return h("div", { class: "card chart-card" },
          legend,
          smileChart(sl, { height: 300, spot: d.spot }),
          h("p", { class: "small muted" }, `Fit: ${sl.method || "–"} · fit error (RMSE) ${pct(sl.rmse, 2)} · forward ${price(sl.forward)} · ${flagged.length} flagged point${flagged.length === 1 ? "" : "s"} · as of ${timeNY(d.asof)} NY`));
      },
      empty: { title: "No surface for this symbol", hint: "Run a scan, or pick another symbol." },
    });
    chart._redrawSlice = () => { if (lastSurf) chart._redraw(); };
    expSel.onchange = () => { current.expiry = expSel.value; chart._redraw(); };

    const table = panel({
      title: "Mispricings found", id: "vol-mis", store, key,
      render: (d) => {
        const m = d?.mispricings || [];
        if (!m.length) return null;
        return h("div", { class: "card tbl-card" }, h("div", { class: "tbl-wrap" }, h("table", { class: "tbl compact" },
          h("caption", { class: "sr-only" }, "Mispriced options"),
          h("thead", null, h("tr", null, h("th", { scope: "col" }, "Option"), h("th", { scope: "col" }, term("model", "Model")), h("th", { scope: "col", class: "num" }, term("edge_after_costs", "Edge after costs")), h("th", { scope: "col", class: "num" }, term("zscore", "Z")))),
          h("tbody", null, m.slice(0, 30).map((x) => {
            const cheap = isNum(x.edge) && x.edge > 0;
            return h("tr", null,
              h("td", null, h("span", { class: "mono small" }, x.occ ?? "–"), h("div", { class: "small muted" }, x.notes ? String(x.notes) : "", " ", isNum(x.market_iv) ? `IV ${pct(x.market_iv)} vs fair ${pct(x.fair_iv)}` : "")),
              h("td", null, flagChip(cheap ? "cheap" : "rich"), h("div", { class: "small muted" }, String(x.model ?? "–"))),
              h("td", { class: "num" }, isNum(x.edge_after_costs) ? signedNum(x.edge_after_costs, 2) : "–", h("div", { class: "small muted" }, "per share")),
              h("td", { class: "num" }, isNum(x.zscore) ? num(x.zscore, 1) : "–"));
          })))));
      },
      empty: { title: "Nothing mispriced right now", hint: "No option is off the fitted curve by more than its bid–ask spread." },
    });
    host.append(chart, table);
    unmountPanels = [chart, table];
  };
  symSel.addEventListener("change", () => {
    current.symbol = symSel.value; current.expiry = null;
    history.replaceState(null, "", `#/more/volatility/${encodeURIComponent(current.symbol)}`);
    mount();
  });
  mount();
  host.classList.add("cu");
  host._cleanup = () => unmountPanels.forEach((p) => p._cleanup && p._cleanup());
  const kids = [head, host];
  if (firstTime("volatility")) stagger(kids, 0, 60);
  root.append(...kids);
}

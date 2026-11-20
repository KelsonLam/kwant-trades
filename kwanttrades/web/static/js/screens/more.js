// More: an iOS grouped inset list. Each row pushes a detail page (the page modules are reused and restyled).
import { h, announce, put } from "../dom.js";
import { isRemote, signOut } from "../api.js";
import { group, row, icon, segmented } from "../ui.js";
import { getPref, setPref } from "../appearance.js";

export const meta = { title: "More" };

/** Detail pages reachable from More, in display order. mod is attached by main.js. */
export const PAGES = [
  { id: "signals", title: "Market signals", icon: "signals", tint: "blue", sub: "What each signal says about direction" },
  { id: "volatility", title: "Volatility", icon: "volatility", tint: "purple", sub: "Smile chart and mispriced options" },
  { id: "news", title: "News", icon: "news", tint: "orange", sub: "Headlines the model has read" },
  { id: "backtest", title: "Backtest", icon: "backtest", tint: "teal", sub: "Replay the rules on past days" },
  { id: "learning", title: "Learning", icon: "learning", tint: "green", sub: "Why trades lost and what it changed" },
];

const tile = (name, tint) => h("span", { class: `ico-tile tint-${tint}` }, icon(name, { size: 18 }));

export function render(root) {
  const pages = group(PAGES.map((p) => row({ leading: tile(p.icon, p.tint), title: p.title, href: `#/more/${p.id}`, chevron: true, label: `${p.title}. ${p.sub}` })), { header: "Tools" });
  const settings = group([row({ leading: tile("settings", "gray"), title: "Settings", sub: "Session, limits and thresholds", href: "#/more/settings", chevron: true })], { header: "Control" });

  const seg = segmented({
    options: [["system", "System"], ["light", "Light"], ["dark", "Dark"]], value: getPref(), label: "Appearance",
    onChange: (v) => { const t = setPref(v); announce(v === "system" ? `Following the system (${t})` : `${t === "dark" ? "Dark" : "Light"} theme`); },
  });
  const appearance = group([h("li", { class: "row" }, h("div", { class: "row-main static stacked" },
    h("div", { class: "risk-line" }, h("span", { class: "cell-lead" }, tile("appearance", "indigo")), h("span", { class: "cell-title grow" }, "Appearance")), seg))],
  { header: "Display", footer: "System follows your device's light or dark setting." });

  const out = isRemote() ? group([row({ leading: tile("signout", "red"), title: "Sign out", sub: "Sign out on shared or lost devices", onclick: () => signOut(), cls: "danger-row" })], { header: "This device" }) : null;

  put(root, pages, settings, appearance, out,
    h("p", { class: "footer-note" }, "Model estimates, not guarantees. Paper results do not predict live results."));
}

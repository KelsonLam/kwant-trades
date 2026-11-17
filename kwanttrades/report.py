"""Self-contained HTML report (inline CSS + inline SVG, no external assets).

Every string that comes from data is HTML-escaped with `html.escape`.
"""
from __future__ import annotations

import html
import math
from datetime import datetime
from typing import Any, Iterable

from kwanttrades.types import NY, now_ny

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1a1d23;--mute:#5b6270;--line:#dde1e7;--pos:#0a7a43;--neg:#b42318;--acc:#2456c6}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--card:#1d2026;--ink:#e8eaee;--mute:#9aa2b1;--line:#2c313a;--pos:#4cc38a;--neg:#f0786d;--acc:#7aa2f7}}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,Segoe UI,Arial,sans-serif;padding:16px}
main{max-width:1100px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
.sub{color:var(--mute);margin-bottom:16px}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.tile .l{color:var(--mute);font-size:12px}.tile .v{font-size:20px;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mute);font-weight:600}td.n,th.n{text-align:right}.pos{color:var(--pos)}.neg{color:var(--neg)}
.caveat{background:var(--card);border-left:4px solid var(--neg);padding:8px 12px;margin:6px 0;border-radius:4px}
svg{width:100%;height:auto}.axis{fill:var(--mute);font-size:11px}
"""


def esc(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _money(x: Any) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(v):
        return "n/a"
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def _cls(x: Any) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    return "pos" if v > 0 else "neg" if v < 0 else ""


def equity_svg(points: list[tuple[str, float]], width: int = 900, height: int = 260) -> str:
    """Inline SVG line chart of (label, equity) points."""
    pts = [(str(t), float(v)) for t, v in points if v is not None and math.isfinite(float(v))]
    if len(pts) < 2:
        return '<p class="sub">Not enough equity points to draw a curve yet.</p>'
    ml, mr, mt, mb = 64, 12, 12, 26
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    w, h = width - ml - mr, height - mt - mb
    xy = [(ml + w * i / (len(pts) - 1), mt + h * (1 - (v - lo) / (hi - lo))) for i, (_, v) in enumerate(pts)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    area = f"{ml},{mt + h} {line} {xy[-1][0]:.1f},{mt + h}"
    grid = ""
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = mt + h * (1 - k / 4)
        grid += (f'<line x1="{ml}" x2="{width - mr}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--line)"/>'
                 f'<text class="axis" x="{ml - 6}" y="{y + 4:.1f}" text-anchor="end">{esc(f"{v:,.0f}")}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Equity curve">{grid}'
            f'<polygon points="{area}" fill="var(--acc)" opacity="0.10"/>'
            f'<polyline points="{line}" fill="none" stroke="var(--acc)" stroke-width="2"/>'
            f'<text class="axis" x="{ml}" y="{height - 6}">{esc(pts[0][0][:16])}</text>'
            f'<text class="axis" x="{width - mr}" y="{height - 6}" text-anchor="end">{esc(pts[-1][0][:16])}</text></svg>')


def _table(headers: list[str], rows: Iterable[list[Any]], num_cols: set[int] = frozenset(), pnl_cols: set[int] = frozenset(),
           empty: str = "Nothing to show.") -> str:
    rows = list(rows)
    if not rows:
        return f'<p class="sub">{esc(empty)}</p>'
    head = "".join(f'<th class="{"n" if i in num_cols else ""}">{esc(h)}</th>' for i, h in enumerate(headers))
    body = ""
    for r in rows:
        tds = ""
        for i, c in enumerate(r):
            cls = ("n " if i in num_cols else "") + (_cls(c) if i in pnl_cols else "")
            txt = _money(c) if i in pnl_cols else c
            tds += f'<td class="{cls.strip()}">{esc(txt)}</td>'
        body += f"<tr>{tds}</tr>"
    return f'<div class="card"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def render_report(data: dict) -> str:
    """data keys: title, subtitle, tiles [(label, value, pnl_class_value|None)], equity [(label, value)],
    open_positions [dict], closed_trades [dict], decisions [dict], caveats [str]."""
    tiles = "".join(
        f'<div class="tile"><div class="l">{esc(l)}</div><div class="v {esc(_cls(c) if c is not None else "")}">{esc(v)}</div></div>'
        for l, v, c in data.get("tiles", []))
    op = _table(["Symbol", "Strategy", "Qty", "Entry", "Mark", "Unrealised", "Max loss", "Opened"],
                [[p.get("symbol"), p.get("strategy"), p.get("contracts"), f"{float(p.get('entry_price') or 0):.2f}",
                  "" if p.get("mark") is None else f"{float(p['mark']):.2f}", p.get("unrealized_pnl"),
                  p.get("max_loss"), str(p.get("opened", ""))[:16]] for p in data.get("open_positions", [])],
                num_cols={2, 3, 4, 5, 6}, pnl_cols={5, 6}, empty="No open positions.")
    ct = _table(["Closed", "Symbol", "Strategy", "Qty", "P&L", "Exit reason"],
                [[str(t.get("closed", ""))[:16], t.get("symbol"), t.get("strategy"), t.get("contracts"), t.get("pnl"),
                  t.get("exit_reason")] for t in data.get("closed_trades", [])],
                num_cols={3, 4}, pnl_cols={4}, empty="No closed trades.")
    dl = _table(["Time", "Symbol", "Strategy", "Decision", "Qty", "Score", "EV", "Reasons"],
                [[str(d.get("t", ""))[:16], d.get("symbol"), d.get("strategy"), "PASS" if d.get("passed") else "BLOCK",
                  d.get("contracts"), f"{float(d.get('score') or 0):.3f}", d.get("expected_value"),
                  "; ".join(str(r) for r in d.get("reasons", []))[:200]] for d in data.get("decisions", [])],
                num_cols={4, 5, 6}, pnl_cols={6}, empty="No decisions journaled.")
    cav = "".join(f'<div class="caveat">{esc(c)}</div>' for c in data.get("caveats", []))
    gen = data.get("generated") or now_ny().astimezone(NY).strftime("%Y-%m-%d %H:%M %Z")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(data.get("title", "Kwant Trades report"))}</title><style>{CSS}</style></head><body><main>'
            f'<h1>{esc(data.get("title", "Kwant Trades report"))}</h1><div class="sub">{esc(data.get("subtitle", ""))} Generated {esc(gen)}. '
            f'Not financial advice.</div><div class="tiles">{tiles}</div>'
            f'<h2>Equity curve</h2><div class="card">{equity_svg(data.get("equity", []))}</div>'
            f'<h2>Open positions</h2>{op}<h2>Closed trades</h2>{ct}<h2>Decision log</h2>{dl}'
            f'{"<h2>Caveats</h2>" + cav if cav else ""}</main></body></html>')


def report_from_journal(journal: Any, starting_equity: float, now: datetime | None = None, decisions_limit: int = 200) -> str:
    """Report for a live/paper journal (the SQLite file the sessions write)."""
    from kwanttrades.execution.journal import Journal  # noqa: F401  (type context)

    now = (now or now_ny()).astimezone(NY)
    closed = journal.closed_positions()
    opens = journal.open_positions()
    curve = journal.equity_curve()
    eq = [(ts.isoformat(), float(v)) for ts, v in curve["equity"].items()]
    last_eq = eq[-1][1] if eq else starting_equity
    total = sum(p.realized_pnl for p in closed)
    wins = sum(1 for p in closed if p.realized_pnl > 0)
    unreal = sum(p.unrealized_pnl for p in opens)
    tiles = [("Equity", _money(last_eq), None), ("Total return", f"{(last_eq / starting_equity - 1):+.2%}", last_eq - starting_equity),
             ("Realised P&L", _money(total), total), ("Unrealised P&L", _money(unreal), unreal),
             ("Closed trades", str(len(closed)), None), ("Win rate", f"{wins / len(closed):.0%}" if closed else "n/a", None),
             ("Open positions", str(len(opens)), None)]
    dec = [{"t": d["ts"].isoformat(), "symbol": d["symbol"], "strategy": d["strategy"], "passed": d["decision"] == "PASS",
            "reasons": d["reasons"], "contracts": d["contracts"], "score": d["idea"].score,
            "expected_value": d["idea"].expected_value} for d in journal.decisions(decisions_limit)]
    return render_report({
        "title": "Kwant Trades paper/live journal report", "subtitle": "Built from the journal database.", "tiles": tiles,
        "equity": eq,
        "open_positions": [{"symbol": p.idea.symbol, "strategy": p.idea.strategy, "contracts": p.contracts,
                            "entry_price": p.entry_price, "mark": p.mark, "unrealized_pnl": p.unrealized_pnl,
                            "max_loss": p.idea.max_loss * p.contracts, "opened": p.opened.isoformat()} for p in opens],
        "closed_trades": [{"closed": p.closed.isoformat(), "symbol": p.idea.symbol, "strategy": p.idea.strategy,
                           "contracts": p.contracts, "pnl": p.realized_pnl, "exit_reason": p.exit_reason}
                          for p in closed[-200:]],
        "decisions": dec, "caveats": ["Paper results use modelled fills; they are not a promise of live results."],
        "generated": now.strftime("%Y-%m-%d %H:%M %Z"),
    })


def report_from_backtest(result: Any) -> str:
    m = result.metrics
    pf = "inf" if math.isinf(m["profit_factor"]) else f"{m['profit_factor']:.2f}"
    tiles = [("Total P&L", _money(m["total_pnl"]), m["total_pnl"]), ("Trades", str(m["trades"]), None),
             ("Win rate", f"{m['win_rate']:.0%}", None), ("Profit factor", pf, None),
             ("Expectancy", _money(m["expectancy"]), m["expectancy"]), ("Sharpe (daily)", f"{m['sharpe']:.2f}", None),
             ("Max drawdown", _money(m["max_drawdown"]), None)]
    return render_report({
        "title": f"Kwant Trades backtest ({result.source})", "subtitle": f"{', '.join(result.symbols)}, {len(result.sessions)} sessions.",
        "tiles": tiles, "equity": [(t.isoformat(), e) for t, e in result.equity], "open_positions": [],
        "closed_trades": result.trades, "decisions": result.decisions, "caveats": result.caveats,
    })

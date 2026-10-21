"""Builds reports/dashboard.html from the journal. Open it in any browser."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .config import ROOT
from .journal import Journal
from .stats import OUTCOME_LABELS, OUTCOMES, by_key, compliance, summarize, win_loss_profile

OUT = ROOT / "reports" / "dashboard.html"


def _section(j: Journal, mode: str, run_id: str | None) -> dict | None:
    if mode != "paper" and not run_id:
        return None
    trades = j.trades(mode, run_id)
    sessions = j.sessions(mode, run_id)
    if not trades and not sessions:
        return None
    info = j.run_info(run_id) if run_id else None
    s = summarize(trades)
    return {
        "mode": mode, "run_id": run_id,
        "settings": json.loads(info["settings"]) if info else None,
        "summary": s,
        "by_symbol": by_key(trades, "symbol") if trades else {},
        "by_direction": by_key(trades, "direction") if trades else {},
        "by_exit": by_key(trades, "exit_reason") if trades else {},
        "by_target": by_key(trades, "target_label") if trades else {},
        "compliance": compliance(sessions),
        "profile": win_loss_profile(trades) if trades else [],
        "trades": [{k: t[k] for k in ("symbol", "session_date", "direction", "entry_time", "entry",
                                      "stop", "target", "target_label", "exit", "exit_reason",
                                      "qty", "pnl", "r_multiple")} for t in trades],
        "open": j.open_trades(mode),
    }


def build(j: Journal, out: Path = OUT, modes=("paper", "backtest", "demo"),
          title: str = "ORB Retest Journal", note: str = "") -> Path:
    sections = []
    for mode in modes:
        sec = _section(j, mode, None if mode == "paper" else j.latest_run(mode))
        if sec:
            sections.append(sec)
    payload = json.dumps({"generated": datetime.now().strftime("%a %b %d, %Y %I:%M %p"),
                          "title": title, "note": note,
                          "outcomes": OUTCOMES, "outcome_labels": OUTCOME_LABELS,
                          "sections": sections}, default=str)
    out.parent.mkdir(parents=True, exist_ok=True)
    html = TEMPLATE.replace("/*__DATA__*/null", payload).replace("<title>ORB Retest Journal</title>",
                                                                   f"<title>{title}</title>")
    out.write_text(html, encoding="utf-8")
    return out


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ORB Retest Journal</title>
<style>
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);--win:#2a78d6;--loss:#e34948;--line:#2a78d6;
--good:#006300;--bad:#d03b3b;--chip:#f0efec}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);--win:#3987e5;--loss:#e66767;
--line:#3987e5;--good:#0ca30c;--bad:#e66767;--chip:#2c2c2a}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1120px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 2px}h2{font-size:15px;margin:28px 0 10px}
.sub{color:var(--ink2);margin:0 0 18px}
.tabs{display:flex;gap:6px;margin:0 0 18px;flex-wrap:wrap}
.tabs button{font:inherit;border:1px solid var(--ring);background:var(--surface);color:var(--ink2);padding:6px 14px;border-radius:999px;cursor:pointer}
.tabs button[aria-selected=true]{background:var(--ink);color:var(--page);border-color:var(--ink)}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:16px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}@media(max-width:760px){.kpis{grid-template-columns:1fr 1fr}}
.kpi{min-width:0}.kpi .v{overflow-wrap:anywhere}
.kpi .l{color:var(--ink2);font-size:12px}.kpi .v{font-size:24px;font-weight:600;margin-top:2px}
.kpi .h{color:var(--muted);font-size:11px;margin-top:2px}
.pos{color:var(--good)}.neg{color:var(--bad)}
.warn{border-left:3px solid var(--bad);padding:10px 14px;margin:0 0 14px;background:var(--surface);border-radius:8px;color:var(--ink2)}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:760px){.charts{grid-template-columns:1fr}}
.ct{font-weight:600;margin-bottom:2px}.cs{color:var(--muted);font-size:12px;margin-bottom:8px}
.chart{position:relative}.chart svg{display:block;width:100%;height:220px;overflow:visible}
.tip{position:absolute;pointer-events:none;background:var(--ink);color:var(--page);padding:6px 9px;border-radius:6px;font-size:12px;white-space:nowrap;opacity:0;transition:opacity .1s;z-index:2}
.legend{display:flex;gap:14px;color:var(--ink2);font-size:12px;margin-top:6px}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--grid);white-space:nowrap;font-variant-numeric:tabular-nums}
th{color:var(--ink2);font-weight:500;font-size:12px}th:first-child,td:first-child{text-align:left}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:760px){.grid2{grid-template-columns:1fr}}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;background:var(--chip);font-size:12px}
details{margin-top:8px}summary{cursor:pointer;color:var(--ink2)}pre{white-space:pre-wrap;color:var(--ink2);font-size:12px}
.empty{color:var(--muted);padding:28px;text-align:center}
</style></head><body><main>
<h1>ORB Retest Journal</h1>
<p class="sub" id="gen"></p>
<div class="tabs" role="tablist" id="tabs"></div>
<div id="view"></div>
</main>
<script>
const DATA = /*__DATA__*/null;
const LABEL = {paper:"Paper (live)", backtest:"Backtest", demo:"Demo (fake data)", oos:"Out-of-sample"};
const $ = (h) => { const d = document.createElement("div"); d.innerHTML = h; return d.firstElementChild; };
const money = (v) => (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const pct = (v) => (v*100).toFixed(1) + "%";
const r = (v) => (v >= 0 ? "+" : "") + v.toFixed(2) + "R";
const cls = (v) => v > 0 ? "pos" : v < 0 ? "neg" : "";
const pf = (v) => v === Infinity || v === "Infinity" || v > 1e9 ? "∞" : v.toFixed(2);
const NS = "http://www.w3.org/2000/svg";
function el(tag, attrs, parent){ const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); parent && parent.appendChild(e); return e; }

document.getElementById("gen").textContent = (DATA.note ? DATA.note + " · " : "") + "Generated " + DATA.generated;
document.querySelector("h1").textContent = DATA.title;
const tabs = document.getElementById("tabs"), view = document.getElementById("view");
if (!DATA.sections.length) view.innerHTML = '<div class="card empty">No trades yet. Run a backtest or the demo first.</div>';
DATA.sections.forEach((s, i) => {
  const b = $(`<button role="tab">${LABEL[s.mode]}</button>`);
  b.onclick = () => { [...tabs.children].forEach(x => x.setAttribute("aria-selected", x === b)); render(s); };
  tabs.appendChild(b); if (i === 0) b.click();
});

function kpi(l, v, h, c="") { return `<div class="card kpi"><div class="l">${l}</div><div class="v ${c}">${v}</div><div class="h">${h||""}</div></div>`; }

function render(s) {
  const m = s.summary; view.innerHTML = "";
  if (s.mode === "demo") view.appendChild($('<div class="warn">This is random fake data. It only proves the bot runs. These numbers say nothing about the strategy.</div>'));
  if (m.trades && m.trades < 30) view.appendChild($(`<div class="warn">Only ${m.trades} trades. Under 30 is too small to trust any of these numbers. Keep logging.</div>`));
  if (s.open && s.open.length) view.appendChild($(`<div class="warn">${s.open.length} trade(s) still open: ${s.open.map(o=>o.symbol).join(", ")}</div>`));
  if (!m.trades) { view.appendChild($('<div class="card empty">No closed trades yet.</div>')); compliance(s); return; }

  view.appendChild($(`<div class="kpis">
    ${kpi("Trades", m.trades, m.wins + " W / " + m.losses + " L")}
    ${kpi("Win rate", pct(m.win_rate), "loss rate " + pct(m.loss_rate))}
    ${kpi("Total P&L", money(m.total_pnl), "after slippage", cls(m.total_pnl))}
    ${kpi("Expectancy", r(m.expectancy_r), "avg result per trade", cls(m.expectancy_r))}
    ${kpi("Profit factor", pf(m.profit_factor), "gross win ÷ gross loss")}
    ${kpi("Avg win / loss", money(m.avg_win), money(m.avg_loss))}
    ${kpi("Max drawdown", money(-m.max_drawdown), "worst peak-to-valley", m.max_drawdown ? "neg" : "")}
    ${kpi("Worst streak", m.worst_losing_streak + " losses", "in a row")}
    ${kpi("Green days", m.green_days + " / " + m.days_traded, pct(m.green_days / m.days_traded) + " of days traded")}
    ${kpi("Best day", money(m.best_day), "", cls(m.best_day))}
    ${kpi("Worst day", money(m.worst_day), "", cls(m.worst_day))}
    ${kpi("Red days", m.red_days, "days that lost money")}
  </div>`));

  const charts = $('<div class="charts" style="margin-top:12px"></div>');
  const eq = $('<div class="card"><div class="ct">Equity curve</div><div class="cs">Cumulative P&L, trade by trade</div><div class="chart"></div></div>');
  const rb = $('<div class="card"><div class="ct">Result per trade</div><div class="cs">In R (1R = what you risked)</div><div class="chart"></div><div class="legend"><span><i style="background:var(--win)"></i>Win</span><span><i style="background:var(--loss)"></i>Loss</span></div></div>');
  charts.append(eq, rb); view.appendChild(charts);
  lineChart(eq.querySelector(".chart"), s.trades.map((t, i) => ({x: i, y: m.equity_curve[i], t})));
  barChart(rb.querySelector(".chart"), s.trades.map((t, i) => ({x: i, y: t.r_multiple, t})));

  view.appendChild($('<h2>By ticker</h2>'));
  view.appendChild($(`<div class="card tw">${groupTable(s.by_symbol, "Ticker")}</div>`));
  compliance(s);
  profile(s);
  view.appendChild($('<h2>Breakdowns</h2>'));
  const g = $('<div class="grid2"></div>');
  g.append($(`<div class="card tw">${groupTable(s.by_direction, "Direction")}</div>`),
           $(`<div class="card tw">${groupTable(s.by_exit, "Exit reason")}</div>`),
           $(`<div class="card tw" style="grid-column:1/-1">${groupTable(s.by_target, "Target used")}</div>`));
  view.appendChild(g);

  view.appendChild($('<h2>Trade log</h2>'));
  const rows = s.trades.slice().reverse().map(t => `<tr><td>${t.symbol}</td><td>${t.session_date}</td><td>${t.direction}</td>
    <td>${t.entry_time.slice(11,16)}</td><td>${t.entry.toFixed(2)}</td><td>${t.stop.toFixed(2)}</td><td>${t.target.toFixed(2)}</td>
    <td>${t.exit.toFixed(2)}</td><td><span class="chip">${t.exit_reason}</span></td><td>${t.qty}</td>
    <td class="${cls(t.pnl)}">${money(t.pnl)}</td><td class="${cls(t.r_multiple)}">${r(t.r_multiple)}</td></tr>`).join("");
  view.appendChild($(`<div class="card tw"><table><thead><tr><th>Ticker</th><th>Date</th><th>Side</th><th>Entry time</th><th>Entry</th><th>Stop</th>
    <th>Target</th><th>Exit</th><th>Why</th><th>Shares</th><th>P&L</th><th>R</th></tr></thead><tbody>${rows}</tbody></table></div>`));

  if (s.settings) view.appendChild($(`<details><summary>Settings used for this run</summary><pre>${JSON.stringify(s.settings, null, 2)}</pre></details>`));
}

function groupTable(groups, label) {
  const rows = Object.entries(groups).map(([k, m]) => `<tr><td>${k}</td><td>${m.trades}</td><td>${pct(m.win_rate)}</td>
    <td class="${cls(m.total_pnl)}">${money(m.total_pnl)}</td><td class="${cls(m.expectancy_r)}">${r(m.expectancy_r)}</td><td>${pf(m.profit_factor)}</td></tr>`).join("");
  return `<table><thead><tr><th>${label}</th><th>Trades</th><th>Win rate</th><th>P&L</th><th>Expectancy</th><th>Profit factor</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function compliance(s) {
  const c = s.compliance; if (!Object.keys(c).length) return;
  view.appendChild($('<h2>Setup compliance: how often each ticker follows the pattern</h2>'));
  const used = DATA.outcomes.filter(o => Object.values(c).some(v => v[o] > 0));
  const head = used.map(o => `<th>${DATA.outcome_labels[o]}</th>`).join("");
  const rows = Object.entries(c).map(([k, v]) => `<tr><td>${k}</td><td>${v.tradable_days}</td><td>${pct(v.breakout_rate)}</td>
    <td>${pct(v.retest_rate)}</td>${used.map(o => `<td>${v[o]}</td>`).join("")}</tr>`).join("");
  view.appendChild($(`<div class="card tw"><table><thead><tr><th>Ticker</th><th>Tradable days</th><th>Broke out</th><th>Breakouts → trade</th>
    ${head}</tr></thead><tbody>${rows}</tbody></table>
    <div class="cs" style="margin-top:8px">Each day ends in exactly one column. News days are skipped and left out of the rates.</div></div>`));
}

function profile(s) {
  if (!s.profile || !s.profile.length) return;
  view.appendChild($('<h2>Winners vs losers</h2>'));
  const rows = s.profile.map(p => `<tr><td>${p.feature}</td><td>${p.win.toFixed(2)}${p.pct ? "%" : ""}</td>
    <td>${p.loss.toFixed(2)}${p.pct ? "%" : ""}</td></tr>`).join("");
  view.appendChild($(`<div class="card tw"><table><thead><tr><th>Typical value (median)</th><th>Winners</th><th>Losers</th></tr></thead>
    <tbody>${rows}</tbody></table><div class="cs" style="margin-top:8px">If winners and losers look the same on a row, that thing doesn't predict the outcome.</div></div>`));
}

function frame(host, ys) {
  const W = host.clientWidth || 500, H = 220, P = {l: 52, r: 8, t: 8, b: 22};
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`}, host);
  let lo = Math.min(0, ...ys), hi = Math.max(0, ...ys); if (lo === hi) hi = lo + 1;
  const raw = (hi - lo) / 4, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(s => s * mag).find(s => s >= raw);   // "nice" tick step
  lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;     // ticks always include 0
  const y = v => P.t + (hi - v) / (hi - lo) * (H - P.t - P.b);
  for (let v = lo; v <= hi + step / 2; v += step) {
    const yy = y(v);
    el("line", {x1: P.l, x2: W - P.r, y1: yy, y2: yy, stroke: "var(--grid)", "stroke-width": 1}, svg);
    const t = el("text", {x: P.l - 6, y: yy + 4, "text-anchor": "end", fill: "var(--muted)", "font-size": 11}, svg); t.textContent = fmtAxis(v);
  }
  el("line", {x1: P.l, x2: W - P.r, y1: y(0), y2: y(0), stroke: "var(--axis)", "stroke-width": 1}, svg);
  const tip = document.createElement("div"); tip.className = "tip"; host.appendChild(tip);
  return {svg, W, H, P, y, tip};
}
var axisFmt = null;
function fmtAxis(v) { return axisFmt ? axisFmt(v) : v.toFixed(0); }
function tipText(t, extra) { return `<b>${t.symbol}</b> ${t.session_date} ${t.direction}<br>${t.exit_reason}: ${money(t.pnl)} (${r(t.r_multiple)})${extra||""}`; }
function showTip(f, host, px, py, html) {
  f.tip.innerHTML = html; f.tip.style.opacity = 1;
  const w = f.tip.offsetWidth; f.tip.style.left = Math.min(Math.max(px - w/2, 0), host.clientWidth - w) + "px"; f.tip.style.top = Math.max(py - 54, 0) + "px";
}

function lineChart(host, pts) {
  axisFmt = v => { const a = Math.abs(v); return (v < 0 ? "-" : "") + "$" + (a >= 1000 ? (a/1000).toFixed(1) + "k" : a.toFixed(0)); };
  const f = frame(host, pts.map(p => p.y)); axisFmt = null;
  const n = Math.max(pts.length - 1, 1), x = i => f.P.l + i / n * (f.W - f.P.l - f.P.r);
  el("path", {d: pts.map((p, i) => (i ? "L" : "M") + x(i) + "," + f.y(p.y)).join(""), fill: "none", stroke: "var(--line)", "stroke-width": 2, "stroke-linejoin": "round"}, f.svg);
  const cross = el("line", {y1: f.P.t, y2: f.H - f.P.b, stroke: "var(--axis)", "stroke-width": 1, opacity: 0}, f.svg);
  const dot = el("circle", {r: 4.5, fill: "var(--line)", stroke: "var(--surface)", "stroke-width": 2, opacity: 0}, f.svg);
  const hit = el("rect", {x: f.P.l, y: 0, width: f.W - f.P.l - f.P.r, height: f.H, fill: "transparent"}, f.svg);
  hit.addEventListener("mousemove", e => {
    const b = f.svg.getBoundingClientRect(), sx = (e.clientX - b.left) * f.W / b.width;
    const i = Math.round((sx - f.P.l) / (f.W - f.P.l - f.P.r) * n); const p = pts[Math.max(0, Math.min(pts.length - 1, i))];
    const xx = x(p.x), yy = f.y(p.y);
    cross.setAttribute("x1", xx); cross.setAttribute("x2", xx); cross.setAttribute("opacity", 1);
    dot.setAttribute("cx", xx); dot.setAttribute("cy", yy); dot.setAttribute("opacity", 1);
    showTip(f, host, xx * b.width / f.W, yy * b.height / f.H, tipText(p.t, `<br>Running total: ${money(p.y)}`));
  });
  hit.addEventListener("mouseleave", () => { cross.setAttribute("opacity", 0); dot.setAttribute("opacity", 0); f.tip.style.opacity = 0; });
}

function barChart(host, pts) {
  axisFmt = v => (v >= 0 ? "+" : "") + v.toFixed(1) + "R";
  const f = frame(host, pts.map(p => p.y)); axisFmt = null;
  const slot = (f.W - f.P.l - f.P.r) / pts.length, bw = Math.max(Math.min(slot - 2, 18), 1), base = f.y(0);
  pts.forEach((p, i) => {
    const cx = f.P.l + slot * (i + .5), top = Math.min(f.y(p.y), base), h = Math.max(Math.abs(f.y(p.y) - base), 1);
    const bar = el("rect", {x: cx - bw/2, y: top, width: bw, height: h, rx: Math.min(2, bw/2), fill: p.y >= 0 ? "var(--win)" : "var(--loss)"}, f.svg);
    const hit = el("rect", {x: cx - slot/2, y: f.P.t, width: slot, height: f.H - f.P.t - f.P.b, fill: "transparent"}, f.svg);
    const b = () => f.svg.getBoundingClientRect();
    hit.addEventListener("mouseenter", () => { bar.setAttribute("opacity", .75); const r_ = b(); showTip(f, host, cx * r_.width / f.W, top * r_.height / f.H, tipText(p.t)); });
    hit.addEventListener("mouseleave", () => { bar.setAttribute("opacity", 1); f.tip.style.opacity = 0; });
  });
}
</script></body></html>
"""

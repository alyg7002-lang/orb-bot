"""Backtest the 6 prior-day high/low strategies in orb/pdhl.py (rules + pick rule are in that file).

Period A: Oct 16 2023 - Oct 16 2025 (2 years). Period B: Oct 17 2025 - yesterday (out-of-sample year).
Same rules, unchanged, in both. Results in R (1R = the trade's initial risk), and $ at $100 risk.

    python scripts/pdhl_lab.py          (needs Alpaca keys the first time; caches 1-min bars in data/cache)
Writes data/pdhl_lab.txt (the table) and data/pdhl_lab.json (every trade).
"""
from __future__ import annotations

import json, sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orb import config, news, panel, pdhl
from orb.data import fetch_range
from orb.sessions import split_days

ET = ZoneInfo("America/New_York")
A_START, B_START = date(2023, 10, 16), date(2025, 10, 17)
FETCH_START = date(2023, 9, 1)            # extra month so ATR(14) exists on day one


def score(trades):
    n = len(trades)
    if not n:
        return {"trades": 0}
    rs = [t.r for t in sorted(trades, key=lambda t: str(t.entry_time))]
    wins, loss = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    eq = peak = dd = 0.0
    streak = worst = 0
    for r in rs:
        eq += r; peak = max(peak, eq); dd = max(dd, peak - eq)
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    return {"trades": n, "win_rate": len(wins) / n, "avg_r": sum(rs) / n, "total_r": sum(rs),
            "avg_win": sum(wins) / len(wins) if wins else 0.0, "avg_loss": sum(loss) / len(loss) if loss else 0.0,
            "pf": sum(wins) / -sum(loss) if loss and sum(loss) < 0 else 99.0, "max_dd_r": dd, "lose_streak": worst}


def main():
    cfg = config.load()
    end = datetime.now(ET).date()
    print(f"Loading 1-min bars {FETCH_START} to {end}...")
    bars = fetch_range(cfg, FETCH_START, end, 1)
    news_tbl = news.load()
    res = defaultdict(lambda: {"A": [], "B": []})
    for sym in cfg.symbols:
        days = {d: p["rth"] for d, p in split_days(bars[sym]).items() if p["rth"]}
        full = {d: r for d, r in days.items() if r[-1].ts.time() >= time(15, 45)}
        daily = panel.daily_bars(days)          # half days count as the prior session...
        for k, dly in enumerate(daily):
            day = dly.day
            if day not in full or day < A_START or day >= end:   # ...but only full days are traded
                continue
            per = "A" if day < B_START else "B"
            if news.event_for(sym, day, news_tbl):
                continue
            prev = daily[max(0, k - 30):k]
            for key in pdhl.STRATEGIES:
                t = pdhl.run_day(key, sym, day, full[day], prev)
                if t:
                    res[key][per].append(t)

    lines = [f"Prior-day high/low strategy lab, run {datetime.now(ET):%Y-%m-%d %H:%M} ET",
             f"Tickers: {', '.join(cfg.symbols)} | A = {A_START} to {B_START - timedelta(days=1)} | B = {B_START} to {end - timedelta(days=1)}",
             "Costs: $0.01/share each way. Stop checked before target. $ column = total at $100 risk per trade.", "",
             f"{'strategy':34s} {'per':3s} {'trades':>6s} {'win%':>6s} {'avgR':>7s} {'avgWin':>7s} {'avgLoss':>7s} {'PF':>5s} {'totalR':>7s} {'$@100':>8s} {'maxDD_R':>7s} {'streak':>6s}"]
    summary = {}
    for key, (name, _) in pdhl.STRATEGIES.items():
        summary[key] = {}
        for per in ("A", "B"):
            m = score(res[key][per])
            summary[key][per] = m
            if m["trades"]:
                lines.append(f"{name:34s} {per:3s} {m['trades']:6d} {m['win_rate']:6.1%} {m['avg_r']:+7.3f} {m['avg_win']:+7.2f} "
                             f"{m['avg_loss']:+7.2f} {m['pf']:5.2f} {m['total_r']:+7.1f} {m['total_r'] * 100:+8.0f} {m['max_dd_r']:7.1f} {m['lose_streak']:6d}")
            else:
                lines.append(f"{name:34s} {per:3s}      0")

    lines += ["", "Breakdown (info only, NOT used for the pick): avg R by side and by ticker, A | B"]
    for key, (name, _) in pdhl.STRATEGIES.items():
        parts = []
        for label, f in [("long", lambda t: t.side == "long"), ("short", lambda t: t.side == "short")] + \
                        [(s, (lambda s: lambda t: t.symbol == s)(s)) for s in cfg.symbols]:
            a = [t.r for t in res[key]["A"] if f(t)]
            b = [t.r for t in res[key]["B"] if f(t)]
            fa = f"{sum(a) / len(a):+.2f}({len(a)})" if a else "-"
            fb = f"{sum(b) / len(b):+.2f}({len(b)})" if b else "-"
            parts.append(f"{label} {fa}|{fb}")
        lines.append(f"  {name}: " + "  ".join(parts))

    qual = [k for k, m in summary.items() if all(m[p]["trades"] >= 50 and m[p]["avg_r"] > 0 for p in "AB")]
    qual.sort(key=lambda k: (-min(summary[k]["A"]["avg_r"], summary[k]["B"]["avg_r"]),
                             max(summary[k]["A"]["max_dd_r"], summary[k]["B"]["max_dd_r"])))
    lines += ["", "Pre-registered pick rule: avg R > 0 in BOTH periods, >= 50 trades each; rank by the worse period's avg R.",
              "Qualifiers: " + (", ".join(f"{pdhl.STRATEGIES[k][0]} (worse-period {min(summary[k]['A']['avg_r'], summary[k]['B']['avg_r']):+.3f}R)" for k in qual) or "none"),
              "PICK: " + (", ".join(pdhl.STRATEGIES[k][0] for k in qual[:2]) or "none qualified")]
    text = "\n".join(lines)
    print(text)
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "pdhl_lab.txt").write_text(text + "\n")
    (ROOT / "data" / "pdhl_lab.json").write_text(json.dumps(
        {"summary": summary, "qualifiers": qual, "trades": {k: {p: [t.as_dict() for t in v[p]] for p in "AB"} for k, v in res.items()}},
        default=str))


if __name__ == "__main__":
    main()

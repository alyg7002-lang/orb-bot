"""Run the baseline ORB+FVG backtest, let the voting panel (orb/panel.py) vote on every trade,
then measure which voters actually separate winners from losers, on two separate periods.

    python scripts/panel_study.py [out.json]      (needs the 1-min cache in data/cache)
"""
import json, sys
from bisect import bisect_left
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orb import challenger, config, panel, strategy_fvg
from orb.backtest import run_backtest
from orb.data import CACHE, load_cache
from orb.journal import Journal
from orb.sessions import split_days

reviews = {}
_orig_review = challenger.review
def _shadow_review(*a, **k):                      # record the challenger's opinion, never block
    rv = _orig_review(*a, **k)
    _shadow_review.last = rv
    return challenger.Review(True, rv.case_for, [])
challenger.review = _shadow_review
_orig_enter = strategy_fvg.ORBFVG._enter
def _enter(self, bar):
    sig = _orig_enter(self, bar)
    if sig:
        reviews[(self.symbol, str(self.session_date))] = getattr(_shadow_review, "last", None)
    return sig
strategy_fvg.ORBFVG._enter = _enter


def load_periods(cfg):
    A, B = {}, {}
    for s in cfg.symbols:
        A[s] = load_cache(s, 1, CACHE / f"{s}_1m_20231016_20251016.csv.gz")
        b = load_cache(s, 1)
        B[s] = [x for x in b if x.ts.date() >= date(2025, 9, 1)]
    return {"A: Oct 2023-Oct 2025": (A, date(2023, 10, 16)), "B: Oct 2025-Oct 2026": (B, date(2025, 10, 17))}


def votes_for_trades(bars_by_sym, trades):
    split = {s: split_days(b) for s, b in bars_by_sym.items()}
    daily = {s: panel.daily_bars({d: p["rth"] for d, p in sp.items()}) for s, sp in split.items()}
    dkeys = {s: [d.day for d in dl] for s, dl in daily.items()}
    out = []
    for t in trades:
        day = date.fromisoformat(t["session_date"])
        ets = datetime.fromisoformat(t["entry_time"])
        today = {s: [b for b in split[s].get(day, {"rth": []})["rth"] if b.ts < ets] for s in split}
        prior = {s: daily[s][max(0, bisect_left(dkeys[s], day) - 30):bisect_left(dkeys[s], day)] for s in daily}
        vs = panel.vote(t["symbol"], t["direction"], t["entry"], t["target"], ets, t["or_high"], t["or_low"],
                        today, prior, review=reviews.get((t["symbol"], t["session_date"])))
        out.append(vs)
    return out


def grp(ts):
    n = len(ts)
    if not n:
        return {"n": 0}
    return {"n": n, "win": sum(t["pnl"] > 0 for t in ts) / n, "avg_r": sum(t["r_multiple"] for t in ts) / n}


def main():
    cfg = config.load()
    cfg = replace(cfg, risk=replace(cfg.risk, sizing="fixed"),
                  strategy=replace(cfg.strategy, challenger="shadow"))
    report = {}
    for name, (data, first) in load_periods(cfg).items():
        j = Journal(":memory:")
        rid = run_backtest(data, cfg, j, mode="backtest", label="panel", first_day=first)
        trades = j.trades("backtest", rid)
        allv = votes_for_trades(data, trades)
        voters = [v.voter for v in allv[0]]
        res = {"all": grp(trades), "voters": {}, "net": {}}
        for i, vn in enumerate(voters):
            res["voters"][vn] = {k: grp([t for t, vs in zip(trades, allv) if vs[i].vote == val])
                                 for k, val in (("for", 1), ("against", -1), ("abstain", 0))}
        by_net = defaultdict(list)
        for t, vs in zip(trades, allv):
            net = panel.tally(vs)[2]
            by_net["<= -2" if net <= -2 else "-1..+1" if net <= 1 else "+2..+3" if net <= 3 else ">= +4"].append(t)
        res["net"] = {k: grp(v) for k, v in by_net.items()}
        losers = [vs for t, vs in zip(trades, allv) if t["r_multiple"] < 0]
        winners = [vs for t, vs in zip(trades, allv) if t["r_multiple"] > 0]
        res["against_rate"] = {vn: {"losers": sum(vs[i].vote < 0 for vs in losers) / max(len(losers), 1),
                                    "winners": sum(vs[i].vote < 0 for vs in winners) / max(len(winners), 1)}
                               for i, vn in enumerate(voters)}
        report[name] = res
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("reports/panel_study.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=1))

    names = list(report)
    print(f"{'voter':15s}" + "".join(f" | {n[:2]} FOR  n   avgR   AGAINST n   avgR   gap " for n in names))
    for vn in report[names[0]]["voters"]:
        row = f"{vn:15s}"
        for n in names:
            v = report[n]["voters"][vn]
            f, a = v["for"], v["against"]
            gap = (f.get("avg_r", 0) - a.get("avg_r", 0)) if f["n"] and a["n"] else float("nan")
            row += (f" |      {f['n']:4d} {f.get('avg_r', 0):+.3f}         {a['n']:4d} {a.get('avg_r', 0):+.3f} {gap:+.3f}")
        print(row)
    for n in names:
        print(f"\n{n}: all {report[n]['all']}")
        for k in ("<= -2", "-1..+1", "+2..+3", ">= +4"):
            g = report[n]["net"].get(k, {"n": 0})
            if g["n"]:
                print(f"   net vote {k:7s}: {g['n']:4d} trades  win {g['win']:.1%}  avg {g['avg_r']:+.3f}R")


if __name__ == "__main__":
    main()

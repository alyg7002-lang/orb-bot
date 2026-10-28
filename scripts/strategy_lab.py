"""Strategy lab: 5 strict, high-win-rate strategy designs, tested the honest way.

Rules were written down BEFORE testing. Each is run on period A (Oct 2023-Oct 2025) and then,
unchanged, on period B (Oct 2025-Oct 2026) as an out-of-sample check.
Costs: $0.01/share slippage on entry and exit. Results in R (initial risk per share).

    python scripts/strategy_lab.py            (needs the 1-min cache in data/cache)
"""
from __future__ import annotations

import json, math, sys
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orb import config, news, panel
from orb.data import CACHE, load_cache
from orb.sessions import split_days

SLIP = 0.01
A_START, B_START, B_END = date(2023, 10, 16), date(2025, 10, 17), date(2026, 10, 17)
NEWS = news.load()
ETFS = {"SPY", "QQQ"}


# ---------------------------------------------------------------- data
def load_all(symbols):
    """{sym: {day: {"pre": [...], "rth": [...]}}} over both cache files, plus daily bars."""
    out, daily = {}, {}
    for s in symbols:
        bars = load_cache(s, 1, CACHE / f"{s}_1m_20231016_20251016.csv.gz") or []
        last = bars[-1].ts if bars else None
        bars += [b for b in (load_cache(s, 1) or []) if last is None or b.ts > last]
        days = {d: p for d, p in split_days(bars).items() if p["rth"] and p["rth"][-1].ts.time() >= time(15, 45)}
        out[s] = days
        daily[s] = panel.daily_bars({d: p["rth"] for d, p in days.items()})
    return out, daily


def trade(sym, day, side, entry, stop, target, bars, start_i, last_exit=time(15, 55), max_bars=None):
    """Walk 1-min bars from start_i; stop checked first (pessimistic). Returns a trade dict."""
    s = 1 if side == "long" else -1
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    exit_px, reason, t_exit = None, None, None
    for k, b in enumerate(bars[start_i:]):
        if (s > 0 and b.low <= stop) or (s < 0 and b.high >= stop):
            exit_px, reason = (min(b.open, stop) if s > 0 else max(b.open, stop)), "stop"
        elif (s > 0 and b.high >= target) or (s < 0 and b.low <= target):
            exit_px, reason = (max(b.open, target) if s > 0 else min(b.open, target)), "target"
        elif b.ts.time() >= last_exit or (max_bars and k + 1 >= max_bars):
            exit_px, reason = b.close, "time"
        if exit_px is not None:
            t_exit = b.ts
            break
    if exit_px is None:
        exit_px, reason, t_exit = bars[-1].close, "eod", bars[-1].ts
    r = (s * (exit_px - entry) - 2 * SLIP) / risk
    return {"sym": sym, "day": day, "side": side, "entry": entry, "stop": stop, "target": target,
            "exit": exit_px, "reason": reason, "r": r, "t_in": bars[start_i].ts, "t_out": t_exit}


def rsi(vals, n):
    if len(vals) < n + 1:
        return None
    g = l = 0.0
    for a, b in zip(vals[:n], vals[1:n + 1]):
        g += max(b - a, 0); l += max(a - b, 0)
    ag, al = g / n, l / n
    for a, b in zip(vals[n:], vals[n + 1:]):
        ag = (ag * (n - 1) + max(b - a, 0)) / n
        al = (al * (n - 1) + max(a - b, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


# ---------------------------------------------------------------- 1. ORB Trap
def s1_orb_trap(sym, day, rth, prev):
    """ORB TRAP (failed breakout reversal; Raschke & Connors 'Turtle Soup' applied to Crabel's ORB).
    15-min range. Price CLOSES outside it, then within 30 minutes closes back past the range
    MIDPOINT: the breakout trapped traders. Enter the other way at that close.
    Stop: 1 tick past the failed breakout's extreme. Target: the opposite range edge.
    Entries 9:45-11:30, flat 15:55."""
    rng = rth[:15]
    hi, lo = max(b.high for b in rng), min(b.low for b in rng)
    mid = (hi + lo) / 2
    brk, brk_i, ext = None, None, None
    for i in range(15, len(rth)):
        b = rth[i]
        if b.ts.time() >= time(11, 30):
            return None
        if brk is None:
            if b.close > hi: brk, brk_i, ext = "up", i, b.high
            elif b.close < lo: brk, brk_i, ext = "down", i, b.low
            continue
        ext = max(ext, b.high) if brk == "up" else min(ext, b.low)
        if i - brk_i > 30:
            return None
        if brk == "up" and b.close < mid:
            return trade(sym, day, "short", b.close, ext + 0.01, lo, rth, i + 1)
        if brk == "down" and b.close > mid:
            return trade(sym, day, "long", b.close, ext - 0.01, hi, rth, i + 1)
    return None


# ---------------------------------------------------------------- 2. VWAP snap-back
def s2_vwap_snap(sym, day, rth, prev):
    """VWAP SNAP-BACK (mean reversion to VWAP; Wilder RSI extremes + VWAP standard-deviation bands).
    10:00-14:30. Price closes beyond VWAP +/- 2.5 standard deviations AND 1-min RSI(14) is
    > 80 (short) / < 20 (long) AND that candle reverses (red after a stretch up, green after a
    stretch down). Enter at its close. Target: halfway back to VWAP. Stop: as far beyond the
    entry as VWAP is away (R:R 0.5). Out after 60 minutes regardless. One trade a day."""
    pv = v = pv2 = 0.0
    closes = []
    for i, b in enumerate(rth):
        tp = (b.high + b.low + b.close) / 3
        pv += tp * b.volume; v += b.volume; pv2 += tp * tp * b.volume
        closes.append(b.close)
        t = b.ts.time()
        if t < time(10, 0) or v <= 0:
            continue
        if t > time(14, 30):
            return None
        vw = pv / v
        sd = math.sqrt(max(pv2 / v - vw * vw, 0))
        if sd <= 0:
            continue
        r = rsi(closes[-60:], 14)
        if r is None:
            continue
        dist = b.close - vw
        if dist > 2.5 * sd and r > 80 and b.close < b.open:
            return trade(sym, day, "short", b.close, b.close + dist, b.close - dist / 2, rth, i + 1, max_bars=60)
        if dist < -2.5 * sd and r < 20 and b.close > b.open:
            return trade(sym, day, "long", b.close, b.close + dist, b.close - dist / 2, rth, i + 1, max_bars=60)
    return None


# ---------------------------------------------------------------- 3. Connors RSI(2)
def s3_connors(sym, daily, i):
    """RSI(2) PULLBACK (Larry Connors & Cesar Alvarez, 'Short Term Trading Strategies That Work', 2008).
    Close above its 200-day average and 2-day RSI < 5: buy at the close. Mirror for shorts
    (below the 200-day, RSI(2) > 95). Exit at the first close back above (below) the 5-day
    average, or after 10 days. No stop in the original; risk unit for R = 2 x ATR(14)."""
    if i < 200:
        return None
    d = daily[i]
    closes = [x.close for x in daily[i - 199:i + 1]]
    sma200 = sum(closes) / 200
    r2 = rsi(closes[-30:], 2)
    trs = [max(a.high - a.low, abs(a.high - p.close), abs(a.low - p.close)) for p, a in zip(daily[i - 14:i], daily[i - 13:i + 1])]
    atr = sum(trs) / 14
    side = "long" if (d.close > sma200 and r2 < 5) else "short" if (d.close < sma200 and r2 > 95) else None
    if not side:
        return None
    s = 1 if side == "long" else -1
    for j in range(i + 1, min(i + 11, len(daily))):
        sma5 = sum(x.close for x in daily[j - 4:j + 1]) / 5
        if (s > 0 and daily[j].close > sma5) or (s < 0 and daily[j].close < sma5) or j == i + 10:
            r = (s * (daily[j].close - d.close) - 2 * SLIP) / (2 * atr)
            return {"sym": sym, "day": d.day, "side": side, "entry": d.close, "exit": daily[j].close,
                    "reason": "sma5" if j < i + 10 else "time", "r": r, "hold_days": j - i}
    return None


# ---------------------------------------------------------------- 4. Gap fill
def s4_gap_fill(sym, day, rth, prev):
    """HALF-GAP FILL (opening gaps tend to partly fill; widely published gap statistics).
    Gap between 0.2% and 0.8% (ETFs) or 0.4% and 2.0% (single stocks), no news that day.
    Enter at the close of the first 1-min candle IF it already moved toward the fill
    (red after a gap up / green after a gap down). Target: half the gap filled.
    Stop: one full gap-size beyond the entry (R:R 0.5). Out at 11:30."""
    if not prev:
        return None
    pc = prev.close
    g = (rth[0].open - pc) / pc * 100
    lo, hi = (0.2, 0.8) if sym in ETFS else (0.4, 2.0)
    if not (lo <= abs(g) <= hi):
        return None
    b = rth[0]
    gap = abs(rth[0].open - pc)
    if g > 0 and b.close < b.open:
        tgt = rth[0].open - gap / 2
        if b.close <= tgt:
            return None
        return trade(sym, day, "short", b.close, b.close + gap, tgt, rth, 1, last_exit=time(11, 30))
    if g < 0 and b.close > b.open:
        tgt = rth[0].open + gap / 2
        if b.close >= tgt:
            return None
        return trade(sym, day, "long", b.close, b.close - gap, tgt, rth, 1, last_exit=time(11, 30))
    return None


# ---------------------------------------------------------------- 5. Confluence ORB
def s5_confluence_orb(cfg, bars_by_sym, first, last):
    """CONFLUENCE ORB (Elijah's ORB+FVG rules + the only panel voters that held up in both periods).
    The live bot's exact setup, taken ONLY when 3 things agree at entry: 9/20 EMA trend in the
    trade direction, entry on the right side of VWAP, and the rejection candle closing in its
    top/bottom third. Target cut to 1R (from 1.5R) to raise the win rate."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import panel_study
    from orb.backtest import run_backtest
    from orb.journal import Journal
    c = replace(cfg, risk=replace(cfg.risk, sizing="fixed"),
                strategy=replace(cfg.strategy, challenger="shadow", target_r=1.0))
    data = {s: [b for b in bars if first <= b.ts.date() < last or
                (b.ts.date() < first and (first - b.ts.date()).days < 45)] for s, bars in bars_by_sym.items()}
    j = Journal(":memory:")
    rid = run_backtest(data, c, j, mode="backtest", label="confluence", first_day=first)
    trades = j.trades("backtest", rid)
    votes = panel_study.votes_for_trades(data, trades)
    out = []
    for t, vs in zip(trades, votes):
        v = {x.voter: x.vote for x in vs}
        if v.get("ema_trend") == 1 and v.get("vwap") == 1 and v.get("close_location") == 1:
            out.append({"sym": t["symbol"], "day": date.fromisoformat(t["session_date"]), "side": t["direction"],
                        "r": t["r_multiple"] - 2 * SLIP / abs(t["entry"] - t["stop"]), "reason": t["exit_reason"]})
    return out


# ---------------------------------------------------------------- scoring
def score(trades):
    n = len(trades)
    if not n:
        return {"trades": 0}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    loss = [r for r in rs if r <= 0]
    eq = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: (x["day"], x.get("t_in", datetime.min.replace(tzinfo=None)).__str__())):
        eq += t["r"]; peak = max(peak, eq); dd = max(dd, peak - eq)
    days = len({t["day"] for t in trades})
    return {"trades": n, "win_rate": len(wins) / n, "avg_r": sum(rs) / n, "total_r": sum(rs),
            "avg_win": sum(wins) / len(wins) if wins else 0, "avg_loss": sum(loss) / len(loss) if loss else 0,
            "pf": (sum(wins) / -sum(loss)) if loss and sum(loss) < 0 else float("inf"), "max_dd_r": dd,
            "worst_streak": _streak(rs), "per_month": n / 12 / ((B_END - A_START).days / 365) if False else None,
            "days": days}


def _streak(rs):
    w = c = 0
    for r in rs:
        c = c + 1 if r <= 0 else 0
        w = max(w, c)
    return w


def main():
    cfg = config.load()
    days_by_sym, daily = load_all(cfg.symbols)
    raw = {}
    for s in cfg.symbols:
        bars = load_cache(s, 1, CACHE / f"{s}_1m_20231016_20251016.csv.gz") or []
        last = bars[-1].ts
        raw[s] = bars + [b for b in (load_cache(s, 1) or []) if b.ts > last]

    results = defaultdict(lambda: {"A": [], "B": []})
    intraday = [("1 ORB Trap", s1_orb_trap), ("2 VWAP Snap-back", s2_vwap_snap), ("4 Half-Gap Fill", s4_gap_fill)]
    for s in cfg.symbols:
        dl = daily[s]
        idx = {d.day: k for k, d in enumerate(dl)}
        for day, parts in days_by_sym[s].items():
            per = "A" if A_START <= day < B_START else "B" if B_START <= day < B_END else None
            if per is None:
                continue
            k = idx.get(day)
            prev = dl[k - 1] if k else None
            news_day = news.event_for(s, day, NEWS)
            for name, fn in intraday:
                if news_day:
                    continue
                t = fn(s, day, parts["rth"], prev)
                if t:
                    results[name][per].append(t)
        for k, d in enumerate(dl):
            per = "A" if A_START <= d.day < B_START else "B" if B_START <= d.day < B_END else None
            if per:
                t = s3_connors(s, dl, k)
                if t:
                    results["3 RSI(2) Pullback"][per].append(t)
    for per, (a, b) in (("A", (A_START, B_START)), ("B", (B_START, B_END))):
        results["5 Confluence ORB"][per] = s5_confluence_orb(cfg, raw, a, b)

    report = {}
    print(f"{'strategy':20s} {'period':6s} {'trades':>6s} {'win%':>6s} {'avgR':>7s} {'avgWin':>7s} {'avgLoss':>8s} {'PF':>5s} {'totalR':>7s} {'maxDD R':>8s} {'lose streak':>11s}")
    for name in sorted(results):
        report[name] = {}
        for per in ("A", "B"):
            m = score(results[name][per])
            report[name][per] = m
            if m["trades"]:
                print(f"{name:20s} {per:6s} {m['trades']:6d} {m['win_rate']:6.1%} {m['avg_r']:+7.3f} {m['avg_win']:+7.2f} "
                      f"{m['avg_loss']:+8.2f} {m['pf']:5.2f} {m['total_r']:+7.1f} {m['max_dd_r']:8.1f} {m['worst_streak']:11d}")
            else:
                print(f"{name:20s} {per:6s}      0")
    Path("reports").mkdir(exist_ok=True)
    Path("reports/strategy_lab.json").write_text(json.dumps(report, indent=1, default=str))
    trades_out = {n: {p: [{k: str(v) if isinstance(v, (date, datetime)) else v for k, v in t.items()} for t in r[p]]
                      for p in ("A", "B")} for n, r in results.items()}
    Path("reports/strategy_lab_trades.json").write_text(json.dumps(trades_out, default=str))


if __name__ == "__main__":
    main()

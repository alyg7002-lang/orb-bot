"""Data for the trade review page in Jarvis: every paper trade + the 1-min candles of each trade's days.

    python scripts/trade_review.py           data/paper_trades.json only (no keys needed)
    python scripts/trade_review.py --fetch   also download missing candles into data/charts/

Runs at the end of every bot run on GitHub, so the page fills itself in. Candles are regular hours
only (9:30-16:00 ET), SIP feed, so they match the backtest data. Jarvis reads both files straight
from raw.githubusercontent.com: data/paper_trades.json and data/charts/<SYM>_<YYYY-MM-DD>.csv.
"""
import csv, json, re, sqlite3, sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
CHARTS = ROOT / "data" / "charts"
OUT = ROOT / "data" / "paper_trades.json"
BOTS = {"paper": "ORB", "paper_rsi2": "RSI(2)", "paper_nr7": "NR7", "paper_vol_breakout": "Vol Breakout"}
# Rules in plain words, shown next to each trade. Alpaca = real paper orders; journal = no orders sent.
ABOUT = {
    "paper": {"where": "Alpaca paper orders + journal",
              "rules": "Opening range 9:30-9:45, 1-min breakout, FVG retest, stop at the far side of the range, target 1.5R, flat at 15:55."},
    "paper_rsi2": {"where": "Journal only (no orders)",
                   "rules": "Above the 200-day average with RSI(2) under 5: buy near the close. Exit on a close above the 5-day average or after 10 days. The stop is a 2xATR sizing unit, not an order."},
    "paper_nr7": {"where": "Journal only (no orders, replayed the next morning)",
                  "rules": "Yesterday was the narrowest range of 7 days: stops 1 cent past yesterday's high and low. Stop at the middle of yesterday's range, flat at 15:55."},
    "paper_vol_breakout": {"where": "Journal only (no orders, replayed the next morning)",
                           "rules": "Stops at today's open plus or minus 50% of yesterday's range. Stop at today's open, flat at 15:55."},
}
# Shadow test of a 30-minute exit rule on ORB trades: no orders change, the page just shows what it would
# have done. Only trades from SHADOW_START on count toward a verdict. The rule was suggested after seeing
# the earlier trades, so those can't confirm it. Backtest Oct 2023-Oct 2026: profit fell 69%, see the page.
SHADOW_START = "2026-11-04"
SHADOW_MARK = 30         # minutes
SHADOW_SLIP = 0.01       # $/share each way, same as the backtests
FVG = re.compile(r"^(\d\d:\d\d) FVG (?:long|short) ([\d.]+)-([\d.]+)")


def chart_days(start: date, end: date) -> list[date]:
    """Weekdays from entry day to exit day (holidays just come back with no candles)."""
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _r(v, n=4):
    return None if v is None else round(v, n)


def fvg_zone(log: list[str], entry_time: str) -> list[float] | None:
    """The last fair value gap the engine marked before this entry."""
    zone = None
    for line in log:
        m = FVG.match(line)
        if m and m.group(1) <= entry_time[11:16]:
            zone = [float(m.group(2)), float(m.group(3))]
    return zone


def by_the_rules(t: dict) -> dict | None:
    """A trade the bot failed to close on time: what the rule (flat at 15:55 that day) would have paid."""
    if t["exit_reason"] != "stale_close":
        return None
    path = CHARTS / f"{t['symbol']}_{t['session_date']}.csv"
    if not path.exists():
        return None
    with open(path) as f:
        bar = next((r for r in csv.DictReader(f) if r["ts"][11:16] == "15:55"), None)
    if not bar:
        return None
    px, s = float(bar["open"]), 1 if t["direction"] == "long" else -1
    risk = abs(t["entry"] - t["stop"])
    return {"exit": px, "pnl": round((px - t["entry"]) * s * t["qty"], 2), "r": round((px - t["entry"]) * s / risk, 3) if risk else 0}


def replay(t: dict, bars: list[tuple], mark: int | None = None, green: bool = True) -> dict:
    """One trade on its entry day: same stop, target and flat as orb/strategy.py, stop checked first.
    bars: (time, open, high, low, close) from the entry minute on. With mark, the 30-minute rule applies:
    at the mark a trade at or above entry closes (if green=True), one below closes when it gets back to entry."""
    s = 1 if t["direction"] == "long" else -1
    e, stop, tgt = t["entry"], t["stop"], t["target"]
    start = datetime.fromisoformat(t["entry_time"])
    armed = False

    def done(px, why, when):
        risk = abs(e - stop)
        pnl = (px - e) * s * t["qty"] - 2 * SHADOW_SLIP * t["qty"]
        return {"exit": round(px, 4), "why": why, "at": when.isoformat(), "pnl": round(pnl, 2),
                "r": round(pnl / (risk * t["qty"]), 3) if risk else 0}

    for ts, o, hi, lo, c in bars:
        if (s > 0 and lo <= stop) or (s < 0 and hi >= stop):
            return done(min(o, stop) if s > 0 else max(o, stop), "stop", ts)
        if armed and ((s > 0 and hi >= e) or (s < 0 and lo <= e)):
            return done(max(o, e) if s > 0 else min(o, e), "even", ts)
        if tgt is not None and ((s > 0 and hi >= tgt) or (s < 0 and lo <= tgt)):
            return done(max(o, tgt) if s > 0 else min(o, tgt), "target", ts)
        end = ts + timedelta(minutes=1)
        if end.time() >= time(15, 55):
            return done(c, "time", end)
        if mark is not None and not armed and (end - start).total_seconds() >= mark * 60:
            if s * (c - e) >= 0:
                if green:
                    return done(c, "green", end)
                mark = None
            else:
                armed = True
    return done(bars[-1][4], "time", bars[-1][0])


def shadow(t: dict, today: date) -> dict | None:
    """What the current rules and the two 30-minute versions do with this trade, or None if we can't say yet."""
    if t["mode"] != "paper" or t["status"] != "closed" or t["stop"] is None:
        return None
    path = CHARTS / f"{t['symbol']}_{t['session_date']}.csv"
    if not path.exists():
        return None
    with open(path) as f:
        rows = list(csv.DictReader(f))
    start = datetime.fromisoformat(t["entry_time"])
    bars = [(datetime.fromisoformat(r["ts"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
            for r in rows if "09:30" <= r["ts"][11:16] < "16:00"]
    if not bars or (bars[-1][0].time() < time(15, 54) and t["session_date"] >= str(today)):
        return None                # today's candles aren't all in yet
    after = [b for b in bars if b[0] >= start]
    if not after:
        return None
    return {"current": replay(t, after), "full": replay(t, after, SHADOW_MARK), "reds": replay(t, after, SHADOW_MARK, green=False)}


def trades(db: Path) -> list[dict]:
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    today = datetime.now(ET).date()
    rows, count = [], {}
    votes: dict[int, list] = {}
    for v in c.execute("SELECT * FROM votes ORDER BY vote DESC, voter"):
        votes.setdefault(v["trade_id"], []).append({"voter": v["voter"], "vote": v["vote"], "why": v["why"]})
    logs = {(s["mode"], s["symbol"], s["session_date"]): (s["log"] or "").split("\n")
            for s in c.execute("SELECT mode, symbol, session_date, log FROM sessions WHERE mode LIKE 'paper%'")}
    for r in c.execute("SELECT * FROM trades WHERE mode LIKE 'paper%' ORDER BY entry_time"):
        mode = r["mode"]
        count[mode] = count.get(mode, 0) + 1
        start = date.fromisoformat(r["session_date"])
        end = datetime.fromisoformat(r["exit_time"]).astimezone(ET).date() if r["exit_time"] else today
        rows.append({
            "id": r["id"], "bot": BOTS.get(mode, mode.replace("paper_", "")), "mode": mode, "n": count[mode],
            "symbol": r["symbol"], "session_date": r["session_date"], "direction": r["direction"],
            "or_high": _r(r["or_high"]), "or_low": _r(r["or_low"]), "breakout_time": r["breakout_time"],
            "entry_time": r["entry_time"], "entry": _r(r["entry"]), "stop": _r(r["stop"]),
            "target": _r(r["target"]), "target_label": r["target_label"], "qty": r["qty"],
            "exit_time": r["exit_time"], "exit": _r(r["exit"]), "exit_reason": r["exit_reason"],
            "pnl": r["pnl"], "r_multiple": r["r_multiple"],
            "status": "closed" if r["exit"] is not None else "open",
            "days": [str(d) for d in chart_days(start, end)],
            "votes": votes.get(r["id"], []),
            "log": logs.get((mode, r["symbol"], r["session_date"]), []),
            "fvg": fvg_zone(logs.get((mode, r["symbol"], r["session_date"]), []), r["entry_time"]),
        })
    c.close()
    return rows


def outcomes(db: Path) -> dict:
    """How each bot's days ended, trades or not, for the 'days it passed on' counts."""
    c = sqlite3.connect(db)
    out: dict = {}
    for mode, oc, n in c.execute("SELECT mode, outcome, COUNT(*) FROM sessions WHERE mode LIKE 'paper%' GROUP BY 1, 2"):
        out.setdefault(BOTS.get(mode, mode), {})[oc] = n
    c.close()
    return out


def _complete(path: Path, day: date, today: date) -> bool:
    """A saved day is final once it has the 15:59 candle. Days a few days old also count as final when
    they're empty (holiday) or end at 12:59 (half day); anything else gets downloaded again."""
    if not path.exists():
        return False
    with open(path) as f:
        lines = f.read().strip().splitlines()
    if lines[-1].startswith(f"{day}T15:59"):
        return True
    return day < today - timedelta(days=3) and (len(lines) == 1 or lines[-1].startswith(f"{day}T12:59"))


def fetch_missing(rows: list[dict]) -> None:
    from orb import config
    from orb.data import fetch_bars
    cfg = config.load()
    cfg.require_keys()
    today = datetime.now(ET).date()
    latest = datetime.now(timezone.utc) - timedelta(minutes=16)     # free SIP data stops 15 min back
    need = sorted({(t["symbol"], d) for t in rows for d in t["days"]})
    CHARTS.mkdir(parents=True, exist_ok=True)
    for sym, d in need:
        day = date.fromisoformat(d)
        path = CHARTS / f"{sym}_{d}.csv"
        if _complete(path, day, today):
            continue
        start = datetime.combine(day, time(9, 30), ET)
        end = min(datetime.combine(day, time(16, 0), ET), latest)
        if end <= start:
            continue
        try:
            bars = fetch_bars(cfg, [sym], start, end, cfg.backtest_feed, 1)[sym]
        except Exception as e:    # one bad day shouldn't stop the rest
            print(f"  {sym} {d}: {e}")
            continue
        bars = [b for b in bars if time(9, 30) <= b.ts.time() < time(16, 0)]
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "open", "high", "low", "close", "volume"])
            for b in bars:
                w.writerow([b.ts.isoformat(), b.open, b.high, b.low, b.close, int(b.volume)])
        print(f"  {sym} {d}: {len(bars)} candles -> {path.relative_to(ROOT)}")


def main() -> None:
    rows = trades(ROOT / "data" / "journal.db")
    if "--fetch" in sys.argv:
        fetch_missing(rows)
    for t in rows:
        t["charts"] = [d for d in t["days"] if (CHARTS / f"{t['symbol']}_{d}.csv").exists()]
        t["rules"] = by_the_rules(t)
        t["shadow"] = shadow(t, datetime.now(ET).date())
    db = ROOT / "data" / "journal.db"
    OUT.write_text(json.dumps({"generated": datetime.now(ET).isoformat(timespec="seconds"), "trades": rows,
                               "bots": {BOTS[m]: ABOUT[m] for m in BOTS}, "outcomes": outcomes(db),
                               "shadow_start": SHADOW_START}, indent=1))
    print(f"{len(rows)} paper trades -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

"""Save one day of 1-min bars for charting a trade: python scripts/fetch_bars.py TSLA 2026-10-29
Writes data/charts/TSLA_2026-10-29.csv (SIP data up to 16 min ago)."""
import csv, sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orb import config
from orb.data import fetch_bars

ET = ZoneInfo("America/New_York")
sym, day = sys.argv[1].upper(), date.fromisoformat(sys.argv[2])
cfg = config.load()
cfg.require_keys()
start = datetime.combine(day, time(4, 0), ET)
end = min(datetime.combine(day, time(16, 0), ET), datetime.now(timezone.utc) - timedelta(minutes=16))
bars = fetch_bars(cfg, [sym], start, end, cfg.backtest_feed, 1)[sym]
out = Path(__file__).resolve().parents[1] / "data" / "charts" / f"{sym}_{day}.csv"
out.parent.mkdir(parents=True, exist_ok=True)
with open(out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["ts", "open", "high", "low", "close", "volume"])
    for b in bars:
        w.writerow([b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume])
print(f"{len(bars)} bars -> {out}")

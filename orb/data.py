"""Pulls candles from Alpaca (free account works) and caches history on disk.

The cache (data/cache/) means you only download a year of 1-minute candles once.
Later backtests with different settings reuse it and take seconds.
"""
from __future__ import annotations

import csv
import gzip
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ROOT, Config
from .strategy import Bar

ET = ZoneInfo("America/New_York")
CACHE = ROOT / "data" / "cache"


def _client(cfg: Config):
    from alpaca.data.historical import StockHistoricalDataClient
    cfg.require_keys()
    return StockHistoricalDataClient(cfg.api_key, cfg.secret_key)


def fetch_bars(cfg: Config, symbols: list[str], start: datetime, end: datetime,
               feed: str, minutes: int = 5) -> dict[str, list[Bar]]:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
        start=start.astimezone(timezone.utc),
        end=end.astimezone(timezone.utc),
        feed=DataFeed(feed),
        adjustment=Adjustment.SPLIT,   # so a stock split doesn't look like a 90% crash
    )
    raw = _client(cfg).get_stock_bars(req).data
    out: dict[str, list[Bar]] = {s: [] for s in symbols}
    for sym, rows in raw.items():
        out[sym] = [Bar(r.timestamp.astimezone(ET), float(r.open), float(r.high), float(r.low),
                        float(r.close), float(r.volume)) for r in rows
                    if 4 <= r.timestamp.astimezone(ET).hour < 16]     # premarket + regular hours only
    return out


# ---- cache -----------------------------------------------------------
def _cache_path(sym: str, minutes: int) -> Path:
    return CACHE / f"{sym}_{minutes}m.csv.gz"


def save_cache(sym: str, minutes: int, bars: list[Bar], path: Path | None = None) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    with gzip.open(path or _cache_path(sym, minutes), "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume])


def load_cache(sym: str, minutes: int, path: Path | None = None) -> list[Bar] | None:
    p = path or _cache_path(sym, minutes)
    if not p.exists():
        return None
    with gzip.open(p, "rt", newline="") as f:
        r = csv.reader(f)
        next(r)
        return [Bar(datetime.fromisoformat(ts), float(o), float(h), float(l), float(c), float(v))
                for ts, o, h, l, c, v in r]


def cache_age_hours(sym: str, minutes: int) -> float | None:
    p = _cache_path(sym, minutes)
    return (_time.time() - p.stat().st_mtime) / 3600 if p.exists() else None


def fetch_history(cfg: Config, days: int, minutes: int, refresh: bool = False,
                  cache_only: bool = False, say=print) -> dict[str, list[Bar]]:
    # free plans can't request SIP data from the last 15 minutes, so stop 20 min ago
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(days=days)
    out: dict[str, list[Bar]] = {}
    for sym in cfg.symbols:
        cached = None if refresh else load_cache(sym, minutes)
        age = cache_age_hours(sym, minutes)
        fresh = cached and (cache_only or (age is not None and age < 18))
        covers = cached and cached[0].ts <= start.astimezone(ET) + timedelta(days=4)
        if fresh and (covers or cache_only):
            say(f"  {sym}: {len(cached):,} bars (cached)")
            out[sym] = [b for b in cached if b.ts >= start.astimezone(ET)]
            continue
        if cache_only:
            raise SystemExit(f"No cached {minutes}-min data for {sym}. Run a backtest on the laptop first.")
        say(f"  {sym}: downloading {minutes}-min candles...")
        bars = fetch_bars(cfg, [sym], start, end, cfg.backtest_feed, minutes)[sym]
        save_cache(sym, minutes, bars)
        say(f"  {sym}: {len(bars):,} bars")
        out[sym] = bars
    return out


def fetch_range(cfg: Config, start_day, end_day, minutes: int, refresh: bool = False,
                cache_only: bool = False, say=print) -> dict[str, list[Bar]]:
    """Fixed historical window (for out-of-sample tests). Cached separately per window."""
    from datetime import date as _date
    start = datetime.combine(start_day - timedelta(days=7), datetime.min.time(), ET)   # room for prior-day levels
    end = datetime.combine(end_day, datetime.min.time(), ET)
    out: dict[str, list[Bar]] = {}
    for sym in cfg.symbols:
        tag = f"{sym}_{minutes}m_{start_day:%Y%m%d}_{end_day:%Y%m%d}"
        path = CACHE / f"{tag}.csv.gz"
        if path.exists() and not refresh:
            out[sym] = load_cache(sym, minutes, path)
            say(f"  {sym}: {len(out[sym]):,} bars (cached)")
            continue
        if cache_only:
            raise SystemExit(f"No cached data for {tag}. Run it on the laptop first.")
        say(f"  {sym}: downloading {minutes}-min candles {start_day} to {end_day} (takes a minute)...")
        bars = fetch_bars(cfg, [sym], start, end, cfg.backtest_feed, minutes)[sym]
        save_cache(sym, minutes, bars, path)
        say(f"  {sym}: {len(bars):,} bars")
        out[sym] = bars
    return out

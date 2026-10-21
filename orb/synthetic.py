"""Fake market data so you can test the whole pipeline with no account.

The numbers are a random walk. Results from this data mean NOTHING about
whether the strategy works. It's only for checking that the plumbing runs.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .strategy import Bar

ET = ZoneInfo("America/New_York")
START_PRICES = {"SPY": 580, "QQQ": 500, "NVDA": 140, "TSLA": 250, "AAPL": 230}


def generate(symbol: str, days: int, end: date | None = None, seed: int = 7,
             minutes: int = 5) -> list[Bar]:
    rng = random.Random(f"{symbol}-{seed}")
    price = START_PRICES.get(symbol, 100.0)
    end = end or date.today()
    day = end - timedelta(days=days)
    out: list[Bar] = []
    while day <= end:
        if day.weekday() < 5:
            price *= 1 + rng.gauss(0, 0.004)            # overnight gap
            # No trend on purpose. With a built-in trend, ORB (a momentum strategy) shows a fake
            # 70%+ win rate. Pure random walk = no edge, so the demo should show roughly zero or
            # slightly negative after slippage. If yours ever shows a big edge, suspect a bug.
            trend = 0.0
            t = datetime(day.year, day.month, day.day, 4, 0, tzinfo=ET)
            stop = datetime(day.year, day.month, day.day, 16, 0, tzinfo=ET)
            while t < stop:
                rth = t.hour > 9 or (t.hour == 9 and t.minute >= 30)
                vol = (0.0022 if (rth and t.hour < 10) else (0.0012 if rth else 0.0005)) * (minutes / 5) ** 0.5
                o = price
                path = [o * (1 + trend + rng.gauss(0, vol)) for _ in range(4)]
                c = path[-1]
                hi = max([o] + path) * (1 + abs(rng.gauss(0, vol / 3)))
                lo = min([o] + path) * (1 - abs(rng.gauss(0, vol / 3)))
                out.append(Bar(t, round(o, 2), round(hi, 2), round(lo, 2), round(c, 2),
                               rng.randint(1000, 50000)))
                price = c
                t += timedelta(minutes=minutes)
        day += timedelta(days=1)
    return out

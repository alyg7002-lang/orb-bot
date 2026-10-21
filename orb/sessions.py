"""Splits a stream of bars into trading days and works out each day's key levels."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from .strategy import Bar, Levels

PRE_START, RTH_START, RTH_END = time(4, 0), time(9, 30), time(16, 0)


def split_days(bars: list[Bar]) -> dict[date, dict[str, list[Bar]]]:
    """{date: {"pre": premarket bars, "rth": regular-hours bars}}, oldest first."""
    days: dict[date, dict[str, list[Bar]]] = defaultdict(lambda: {"pre": [], "rth": []})
    for b in sorted(bars, key=lambda x: x.ts):
        t = b.ts.time()
        if PRE_START <= t < RTH_START:
            days[b.ts.date()]["pre"].append(b)
        elif RTH_START <= t < RTH_END:
            days[b.ts.date()]["rth"].append(b)
    return dict(sorted(days.items()))


def levels_for(prev_rth: list[Bar] | None, today_pre: list[Bar] | None) -> Levels:
    lv = Levels()
    if prev_rth:
        lv.prior_high = max(b.high for b in prev_rth)
        lv.prior_low = min(b.low for b in prev_rth)
        lv.prior_close = prev_rth[-1].close
    if today_pre:
        lv.premarket_high = max(b.high for b in today_pre)
        lv.premarket_low = min(b.low for b in today_pre)
    return lv


OR_END = time(9, 45)


def opening_volume(rth: list[Bar], minutes: int = 15) -> float:
    """Total volume traded in the opening range (9:30 to 9:30 + minutes)."""
    end = (datetime.combine(date.today(), RTH_START) + timedelta(minutes=minutes)).time()
    return sum(b.volume for b in rth if b.ts.time() < end)

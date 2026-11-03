"""Prior-day high/low strategies: 6 published setups, one rules engine for backtest AND paper.

Every rule below was written down BEFORE any backtest (11/03/2026). They run on 1-min regular-hours
bars (9:30-16:00 ET). "PDH"/"PDL" = the prior regular session's high/low. ATR = 14-day ATR of
completed days. Nothing is held overnight: anything open is closed at 15:55.

Common rules (all six):
  * stop is checked before target inside a candle (pessimistic). When a stop-order entry fills
    inside a candle, that same candle is also checked against the stop (we can't see the order of
    ticks inside a candle, so we assume the worst). Exception: Oops and 80-20 put the stop at the
    day's extreme INCLUDING the fill candle, so their checks start on the next candle.
  * minimum stop distance 0.1 x ATR (stops tighter than that are pushed out to it), so a 2-cent
    stop can't turn into a fake +30R.
  * $0.01/share slippage on entry AND exit, charged inside R.
  * one trade per ticker per strategy per day. High-impact news days are skipped (orb/news_days.csv).

  1 OOPS!  (Larry Williams, 'Long-Term Secrets to Short-Term Trading', 1999)
      Opens BELOW yesterday's low -> buy stop at yesterday's low (fills only if price climbs back).
      Opens ABOVE yesterday's high -> sell stop at yesterday's high.
      Stop: the day's low (high) through the fill candle. Exit: 15:55. Entries until 15:00.
  2 80-20  (Linda Raschke & Larry Connors, 'Street Smarts', 1995)
      Yesterday opened in the top 20% of its range and closed in the bottom 20% (a bar of pure
      selling). Today price must trade at least 0.05 x ATR below yesterday's low, then a buy stop at
      yesterday's low. Mirror for shorts. Stop: the day's low (high) through the fill candle. Exit 15:55. Until 15:00.
  3 PDH/PDL BREAKOUT  (the classic prior-day level breakout, as taught on TradingView/LuxAlgo etc.)
      Day opens inside yesterday's range. First 5-min candle that CLOSES above PDH -> long at that close
      (below PDL -> short). Stop: that candle's low (high). Target 2R. Entries until 12:00.
  4 PDH/PDL FAILED BREAKOUT  (Raschke & Connors' 'Turtle Soup' false-breakout idea, 1-day levels)
      Day opens inside yesterday's range. Price trades above PDH, then within 60 minutes a 5-min
      candle closes back below PDH -> short at that close (mirror at PDL). Stop: the day's high (low)
      so far. Target 2R. Entries until 12:00.
  5 NR7 BREAKOUT  (Toby Crabel, 'Day Trading with Short Term Price Patterns', 1990)
      Yesterday's range was the narrowest of the last 7 days. Buy stop 1 cent above PDH, sell stop
      1 cent below PDL, first one hit wins. Stop: the middle of yesterday's range. Exit 15:55.
      Entries until 12:00. (Both levels hit in the same 1-min candle -> skip the day.)
  6 VOLATILITY BREAKOUT  (Larry Williams; won the 1987 Robbins World Cup with this family)
      Buy stop at today's open + 50% of yesterday's range, sell stop at open - 50%. First hit wins.
      Stop: today's open. Exit 15:55. Entries until 15:00.

Pre-registered pick rule: a strategy qualifies only if its average R is above zero in BOTH test
periods with at least 50 trades in each. Qualifiers are ranked by their WORSE period's average R
(the edge you can count on), ties broken by smaller max drawdown. The top two go to paper trading.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

from .panel import Daily
from .strategy import Bar

SLIP = 0.01
FLAT = time(15, 55)
MIN_RISK_ATR = 0.1


@dataclass
class Result:
    strategy: str
    symbol: str
    day: date
    side: str
    entry: float
    stop: float
    target: float | None
    entry_time: object
    exit: float
    exit_time: object
    exit_reason: str
    r: float

    def as_dict(self) -> dict:
        return {k: (str(v) if not isinstance(v, (int, float, str, type(None))) else v) for k, v in self.__dict__.items()}


def atr(prev: list[Daily], n: int = 14) -> float:
    pairs = list(zip(prev[-n - 1:-1], prev[-n:]))
    trs = [max(d.high - d.low, abs(d.high - p.close), abs(d.low - p.close)) for p, d in pairs]
    return sum(trs) / len(trs)


def _floor(side: str, entry: float, stop: float, a: float) -> float:
    if abs(entry - stop) >= MIN_RISK_ATR * a:
        return stop
    return entry - MIN_RISK_ATR * a if side == "long" else entry + MIN_RISK_ATR * a


def walk(strategy, sym, day, side, entry, stop, target, bars, i, same_bar_stop) -> Result:
    """Manage the trade from bar i. If same_bar_stop, bar i is the fill candle: check only the stop there."""
    s = 1 if side == "long" else -1
    risk = abs(entry - stop)
    t_in = bars[i].ts
    start = i if same_bar_stop else i + 1
    exit_px = reason = t_out = None
    for k in range(start, len(bars)):
        b = bars[k]
        if k > i and b.ts.time() >= FLAT:
            exit_px, reason = b.open, "time"
        elif (s > 0 and b.low <= stop) or (s < 0 and b.high >= stop):
            exit_px, reason = (min(b.open, stop) if s > 0 else max(b.open, stop)) if k > i else stop, "stop"
        elif k > i and target is not None and ((s > 0 and b.high >= target) or (s < 0 and b.low <= target)):
            exit_px, reason = (max(b.open, target) if s > 0 else min(b.open, target)), "target"
        if exit_px is not None:
            t_out = b.ts
            break
    if exit_px is None:
        exit_px, reason, t_out = bars[-1].close, "eod", bars[-1].ts
    r = (s * (exit_px - entry) - 2 * SLIP) / risk
    return Result(strategy, sym, day, side, round(entry, 4), round(stop, 4),
                  None if target is None else round(target, 4), t_in, round(exit_px, 4), t_out, reason, r)


def five_min(rth: list[Bar]) -> list[tuple[int, Bar]]:
    """5-min candles built from 1-min bars: [(index of the candle's last 1-min bar, candle)]."""
    out, cur, key = [], None, None
    for i, b in enumerate(rth):
        k = (b.ts.hour, b.ts.minute // 5)
        if k != key:
            if cur:
                out.append(cur)
            key = k
            cur = (i, Bar(b.ts, b.open, b.high, b.low, b.close, b.volume))
        else:
            c = cur[1]
            cur = (i, Bar(c.ts, c.open, max(c.high, b.high), min(c.low, b.low), b.close, c.volume + b.volume))
    if cur:
        out.append(cur)
    return out


def _stop_order(name, sym, day, rth, side, level, until, a, stop_fn, target_fn=None, start=0, same_bar_stop=True):
    """Fill a stop order at `level` from bar `start`; stop_fn(i, fill) gives the stop."""
    for i in range(start, len(rth)):
        b = rth[i]
        if b.ts.time() >= until:
            return None
        if (side == "long" and b.high >= level) or (side == "short" and b.low <= level):
            fill = max(b.open, level) if side == "long" else min(b.open, level)
            stop = _floor(side, fill, stop_fn(i, fill), a)
            tgt = target_fn(fill, stop) if target_fn else None
            return walk(name, sym, day, side, fill, stop, tgt, rth, i, same_bar_stop=same_bar_stop)
    return None


def _day_low(rth, i):
    """Low of the day through the fill candle (Williams/Raschke put the stop at the day's low)."""
    return min(b.low for b in rth[:i + 1])


def _day_high(rth, i):
    return max(b.high for b in rth[:i + 1])


# ------------------------------------------------------------------ the six strategies
def oops(sym, day, rth, prev):
    y, o, a = prev[-1], rth[0].open, atr(prev)
    if o < y.low:
        return _stop_order("oops", sym, day, rth, "long", y.low, time(15, 0), a, lambda i, f: _day_low(rth, i) - 0.01,
                           same_bar_stop=False)
    if o > y.high:
        return _stop_order("oops", sym, day, rth, "short", y.high, time(15, 0), a, lambda i, f: _day_high(rth, i) + 0.01,
                           same_bar_stop=False)
    return None


def eighty_twenty(sym, day, rth, prev):
    y, a = prev[-1], atr(prev)
    rng = y.high - y.low
    if rng <= 0:
        return None
    if y.open >= y.high - 0.2 * rng and y.close <= y.low + 0.2 * rng:
        side, level, arm = "long", y.low, y.low - 0.05 * a
    elif y.open <= y.low + 0.2 * rng and y.close >= y.high - 0.2 * rng:
        side, level, arm = "short", y.high, y.high + 0.05 * a
    else:
        return None
    for i, b in enumerate(rth):                   # arm: price must first push through by 0.05 ATR
        if b.ts.time() >= time(15, 0):
            return None
        if (side == "long" and b.low <= arm) or (side == "short" and b.high >= arm):
            fn = (lambda j, f: _day_low(rth, j) - 0.01) if side == "long" else (lambda j, f: _day_high(rth, j) + 0.01)
            return _stop_order("eighty_twenty", sym, day, rth, side, level, time(15, 0), a, fn, start=i + 1,
                               same_bar_stop=False)
    return None


def pdhl_breakout(sym, day, rth, prev):
    y, o, a = prev[-1], rth[0].open, atr(prev)
    if not (y.low < o < y.high):
        return None
    for j, c in five_min(rth):
        if c.ts.time() >= time(12, 0):
            return None
        if c.close > y.high or c.close < y.low:
            side = "long" if c.close > y.high else "short"
            stop = _floor(side, c.close, c.low - 0.01 if side == "long" else c.high + 0.01, a)
            risk = abs(c.close - stop)
            tgt = c.close + 2 * risk if side == "long" else c.close - 2 * risk
            return walk("pdhl_breakout", sym, day, side, c.close, stop, tgt, rth, j, same_bar_stop=False)
    return None


def pdhl_fade(sym, day, rth, prev):
    y, o, a = prev[-1], rth[0].open, atr(prev)
    if not (y.low < o < y.high):
        return None
    broke, t_break = None, None
    hod, lod = o, o
    for j, c in five_min(rth):
        if c.ts.time() >= time(12, 0):
            return None
        hod, lod = max(hod, c.high), min(lod, c.low)
        if broke is None:
            up, dn = c.high > y.high, c.low < y.low
            if up and dn:
                return None
            if up or dn:
                broke, t_break = ("up" if up else "down"), c.ts
        if broke is None:
            continue
        if (c.ts - t_break).total_seconds() > 3600:
            return None
        if broke == "up" and c.close < y.high:
            stop = _floor("short", c.close, hod + 0.01, a)
            return walk("pdhl_fade", sym, day, "short", c.close, stop, c.close - 2 * (stop - c.close), rth, j, False)
        if broke == "down" and c.close > y.low:
            stop = _floor("long", c.close, lod - 0.01, a)
            return walk("pdhl_fade", sym, day, "long", c.close, stop, c.close + 2 * (c.close - stop), rth, j, False)
    return None


def nr7(sym, day, rth, prev):
    if len(prev) < 7:
        return None
    y, a = prev[-1], atr(prev)
    if (y.high - y.low) > min(d.high - d.low for d in prev[-7:-1]):
        return None
    up, dn, mid = y.high + 0.01, y.low - 0.01, (y.high + y.low) / 2
    for i, b in enumerate(rth):
        if b.ts.time() >= time(12, 0):
            return None
        hit_up, hit_dn = b.high >= up, b.low <= dn
        if hit_up and hit_dn:
            return None
        if hit_up or hit_dn:
            side = "long" if hit_up else "short"
            return _stop_order("nr7", sym, day, rth, side, up if hit_up else dn, time(12, 0), a, lambda k, f: mid, start=i)
    return None


def vol_breakout(sym, day, rth, prev):
    y, o, a = prev[-1], rth[0].open, atr(prev)
    rng = y.high - y.low
    up, dn = o + 0.5 * rng, o - 0.5 * rng
    for i, b in enumerate(rth):
        if b.ts.time() >= time(15, 0):
            return None
        hit_up, hit_dn = b.high >= up, b.low <= dn
        if hit_up and hit_dn:
            return None
        if hit_up or hit_dn:
            side = "long" if hit_up else "short"
            return _stop_order("vol_breakout", sym, day, rth, side, up if hit_up else dn, time(15, 0), a, lambda k, f: o, start=i)
    return None


STRATEGIES = {
    "oops": ("1 Oops! (Williams)", oops),
    "eighty_twenty": ("2 80-20 (Raschke/Connors)", eighty_twenty),
    "pdhl_breakout": ("3 PDH/PDL Breakout", pdhl_breakout),
    "pdhl_fade": ("4 PDH/PDL Failed Breakout", pdhl_fade),
    "nr7": ("5 NR7 Breakout (Crabel)", nr7),
    "vol_breakout": ("6 Volatility Breakout (Williams)", vol_breakout),
}


def run_day(key: str, sym: str, day: date, rth: list[Bar], prev: list[Daily]) -> Result | None:
    """One strategy, one ticker, one finished session. Used by the backtest and the paper bot alike."""
    if len(prev) < 20 or not rth or rth[-1].ts.time() < time(15, 45):
        return None
    return STRATEGIES[key][1](sym, day, rth, prev)

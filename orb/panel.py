"""The voting panel: before (and after) every trade, a committee of well-known published
trading ideas each casts a vote on it. Votes never block a trade; they are recorded so that,
over many trades, we can see which ideas actually tell good ORB trades from bad ones.

Each voter returns +1 (agrees with the trade), -1 (argues against it) or 0 (no opinion /
not enough data). Thresholds are the textbook ones, set BEFORE looking at results.

Voter            Source idea                                   Votes FOR when...
---------------  --------------------------------------------  ------------------------------------------
vwap             VWAP as the institutional "fair price"         long above VWAP / short below
                 (Brian Shannon, institutional execution)
ema_trend        9/20 EMA trend (classic day-trading MAs)       long: EMA9 > EMA20 / short: below
daily_trend      Trend following, 20-day moving average         yesterday's close above the 20-day SMA (long)
                 (Donchian, Turtles, "trade with the trend")
gap              Gap-and-go (with the opening gap)              long after a gap up / short after a gap down
rsi              RSI overbought/oversold (Wilder, 1978)         AGAINST a long with 1-min RSI(14) > 70, or a
                                                                short with RSI < 30 (chasing); else no vote
volume           Volume confirms price (Wyckoff, O'Neil)        trigger candle volume >= its 20-candle average
rel_strength     Relative strength vs the market (O'Neil RS,   stock beating SPY since the open (long) /
                 sector rotation)                               lagging it (short). SPY itself: no vote
market           "Don't fight the tape": trade with the index   SPY up since the open (long) / down (short)
narrow_range     Toby Crabel's ORB research (1990): narrow      opening range < 30% of the 14-day ATR;
                 openings lead to expansion                     AGAINST when > 50%
room             Support/resistance: room to the target         no prior-day high/low/close between
                 (classic S/R, Al Brooks "room to run")         entry and target; AGAINST when one blocks it
time_of_day      Opening-drive / first-hour momentum            entry before 10:30; AGAINST after 11:00
                 (Crabel, Fisher "ACD")
extension        Don't chase (mean distance to the trigger)     entry within 0.5x range width of the edge;
                                                                AGAINST beyond 1x
fvg_depth*       Elijah's rule: price trades INTO the FVG       retest >= 25% into the gap
candle_body*     Strong rejection candle (price action)         body >= 50% of the candle
close_location*  Close at the extreme (price action)            close in the top/bottom third
candle_force*    Displacement (ICT / smart-money concepts)      candle >= average of the last 10
  * from the challenger review (orb/challenger.py)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from .strategy import Bar, Levels

RTH_OPEN = time(9, 30)


@dataclass
class Vote:
    voter: str
    vote: int          # +1 for, -1 against, 0 abstain
    why: str


@dataclass
class Daily:
    day: date
    open: float
    high: float
    low: float
    close: float


def daily_bars(rth_by_day: dict[date, list[Bar]]) -> list[Daily]:
    out = []
    for d, bars in sorted(rth_by_day.items()):
        if bars:
            out.append(Daily(d, bars[0].open, max(b.high for b in bars), min(b.low for b in bars), bars[-1].close))
    return out


def _ema(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[:n], closes[1:n + 1]):
        ch = b - a
        gains += max(ch, 0); losses += max(-ch, 0)
    ag, al = gains / n, losses / n
    for a, b in zip(closes[n:], closes[n + 1:]):
        ch = b - a
        ag = (ag * (n - 1) + max(ch, 0)) / n
        al = (al * (n - 1) + max(-ch, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def _atr(days: list[Daily], n: int = 14) -> float | None:
    if len(days) < n + 1:
        return None
    trs = [max(d.high - d.low, abs(d.high - p.close), abs(d.low - p.close)) for p, d in zip(days[-n - 1:-1], days[-n:])]
    return sum(trs) / n


def vote(symbol: str, direction: str, entry: float, target: float, entry_ts: datetime,
         or_high: float, or_low: float, today: dict[str, list[Bar]], daily: dict[str, list[Daily]],
         levels: Levels | None = None, review=None) -> list[Vote]:
    """today: each symbol's regular-hours 1-min candles that CLOSED before entry_ts.
    daily: each symbol's completed prior days (oldest first). review: challenger.Review or None."""
    s = 1 if direction == "long" else -1
    up, dn = ("above", "below") if s > 0 else ("below", "above")
    bars = [b for b in today.get(symbol, []) if b.ts.time() >= RTH_OPEN]
    days = daily.get(symbol) or []
    width = or_high - or_low
    v: list[Vote] = []

    # vwap
    vol = sum(b.volume for b in bars)
    if bars and vol > 0:
        vw = sum((b.high + b.low + b.close) / 3 * b.volume for b in bars) / vol
        v.append(Vote("vwap", 1 if s * (entry - vw) > 0 else -1, f"entry {up if s * (entry - vw) > 0 else dn} VWAP {vw:.2f}"))
    else:
        v.append(Vote("vwap", 0, "no volume data"))

    # ema trend
    closes = [b.close for b in bars]
    e9, e20 = _ema(closes, 9), _ema(closes, 20)
    if e9 is not None and e20 is not None:
        ok = s * (e9 - e20) > 0
        v.append(Vote("ema_trend", 1 if ok else -1, f"EMA9 {'above' if e9 > e20 else 'below'} EMA20"))
    else:
        v.append(Vote("ema_trend", 0, "not enough candles"))

    # daily trend
    if len(days) >= 20:
        sma = sum(d.close for d in days[-20:]) / 20
        ok = s * (days[-1].close - sma) > 0
        v.append(Vote("daily_trend", 1 if ok else -1,
                      f"yesterday's close {'above' if days[-1].close > sma else 'below'} the 20-day average"))
    else:
        v.append(Vote("daily_trend", 0, "not enough daily history"))

    # gap
    if days and bars:
        g = (bars[0].open - days[-1].close) / days[-1].close * 100
        if abs(g) < 0.1:
            v.append(Vote("gap", 0, f"no real gap ({g:+.2f}%)"))
        else:
            v.append(Vote("gap", 1 if s * g > 0 else -1, f"gap {g:+.2f}% {'with' if s * g > 0 else 'against'} the trade"))
    else:
        v.append(Vote("gap", 0, "no prior close"))

    # rsi
    r = _rsi(closes)
    if r is None:
        v.append(Vote("rsi", 0, "not enough candles"))
    elif (s > 0 and r > 70) or (s < 0 and r < 30):
        v.append(Vote("rsi", -1, f"RSI {r:.0f}: already stretched"))
    else:
        v.append(Vote("rsi", 0, f"RSI {r:.0f}: neutral"))

    # volume on the trigger candle
    if len(bars) >= 21:
        avg = sum(b.volume for b in bars[-21:-1]) / 20
        if avg > 0:
            m = bars[-1].volume / avg
            v.append(Vote("volume", 1 if m >= 1 else -1, f"trigger candle volume {m:.1f}x average"))
        else:
            v.append(Vote("volume", 0, "no volume"))
    else:
        v.append(Vote("volume", 0, "not enough candles"))

    # market + relative strength (vs SPY since the open)
    spy = [b for b in today.get("SPY", []) if b.ts.time() >= RTH_OPEN and b.ts < entry_ts]
    if spy:
        mk = (spy[-1].close - spy[0].open) / spy[0].open * 100
        v.append(Vote("market", 1 if s * mk > 0 else -1, f"SPY {mk:+.2f}% since the open"))
        if symbol != "SPY" and bars:
            me = (bars[-1].close - bars[0].open) / bars[0].open * 100
            rs = me - mk
            v.append(Vote("rel_strength", 1 if s * rs > 0 else -1, f"{symbol} {rs:+.2f}% vs SPY since the open"))
        else:
            v.append(Vote("rel_strength", 0, "SPY itself"))
    else:
        v.append(Vote("market", 0, "no SPY data"))
        v.append(Vote("rel_strength", 0, "no SPY data"))

    # narrow range (Crabel)
    atr = _atr(days)
    if atr:
        f = width / atr
        v.append(Vote("narrow_range", 1 if f < 0.30 else (-1 if f > 0.50 else 0),
                      f"opening range = {f:.0%} of the 14-day ATR"))
    else:
        v.append(Vote("narrow_range", 0, "not enough daily history"))

    # room to target
    if days:
        d = days[-1]
        lv = {"prior day high": d.high, "prior day low": d.low, "prior close": d.close}
        lo, hi = sorted((entry, target))
        block = [f"{k} {x:.2f}" for k, x in lv.items() if lo < x < hi]
        v.append(Vote("room", -1 if block else 1, ("blocked by " + ", ".join(block)) if block else "clear path to target"))
    else:
        v.append(Vote("room", 0, "no prior day"))

    # time of day
    t = entry_ts.time()
    v.append(Vote("time_of_day", 1 if t < time(10, 30) else (-1 if t >= time(11, 0) else 0), f"entry {t:%H:%M}"))

    # extension from the range edge
    edge = or_high if s > 0 else or_low
    ext = s * (entry - edge) / width if width > 0 else 0
    v.append(Vote("extension", 1 if ext <= 0.5 else (-1 if ext > 1.0 else 0), f"entry {ext:.1f}x range width past the edge"))

    # challenger checks
    if review is not None:
        obj = " ".join(review.objections)
        for name, key in (("fvg_depth", "shallow"), ("candle_body", "weak body"),
                          ("close_location", "close not"), ("candle_force", "no force")):
            hit = key in obj
            v.append(Vote(name, -1 if hit else 1, "challenger: " + ("objects" if hit else "ok")))
    return v


def tally(votes: list[Vote]) -> tuple[int, int, int]:
    f = sum(1 for x in votes if x.vote > 0)
    a = sum(1 for x in votes if x.vote < 0)
    return f, a, f - a


def describe(symbol: str, votes: list[Vote]) -> list[str]:
    f, a, net = tally(votes)
    lines = [f"Panel vote on {symbol}: {f} for, {a} against (net {net:+d})"]
    pro = [f"{x.voter} ({x.why})" for x in votes if x.vote > 0]
    con = [f"{x.voter} ({x.why})" for x in votes if x.vote < 0]
    if pro:
        lines.append("  FOR: " + "; ".join(pro))
    if con:
        lines.append("  AGAINST: " + "; ".join(con))
    return lines

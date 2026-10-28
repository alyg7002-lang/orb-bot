"""The challenger: a second bot that argues AGAINST every trade the ORB bot wants to take.

The ORB bot proposes a trade when it sees: breakout -> FVG -> retest -> rejection candle.
Before the order goes in, the challenger looks at the same candles and tries to knock the
trade down. A trade only goes through when both bots accept it ("double accepted").

Elijah's rule (after trade #9, SPY 10/27, which barely tagged the FVG and then drifted):
    Price has to actually trade INTO the fair value gap, then reject it STRONGLY, closing
    out of the gap with force. A weak rejection that doesn't jump in either direction is
    not a trade.

So the challenger's objections are:
  1. SHALLOW RETEST   price only tagged the edge of the FVG instead of trading into it
                      (depth into the gap < min_depth of the gap's height)
  2. WEAK BODY        the rejection candle is mostly wick: body < min_body of its range
  3. BAD CLOSE        it didn't close near its extreme in the trade direction
                      (long: top close_zone of the candle, short: bottom close_zone)
  4. NO FORCE         the rejection candle is smaller than min_range_mult x the average
                      candle of the last 10 minutes: no "jump", just drift

Any objection = veto. The ORB bot keeps watching; a later, better rejection can still pass.
Every review is written out as a short debate so you can read why a trade was or wasn't taken.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .strategy import Bar


@dataclass
class Review:
    approved: bool
    case_for: list[str] = field(default_factory=list)
    objections: list[str] = field(default_factory=list)

    def debate(self, symbol: str) -> list[str]:
        lines = [f"ORB bot: {symbol} " + "; ".join(self.case_for)]
        if self.approved:
            lines.append("Challenger: no objections -> DOUBLE ACCEPTED")
        else:
            lines.append("Challenger: VETO -> " + "; ".join(self.objections))
        return lines


def review(direction: str, fvg: tuple[float, float], retest_extreme: float | None, rej: Bar,
           prior: list[Bar], p) -> Review:
    """direction: "long"/"short"; fvg: (bottom, top); retest_extreme: lowest low (long) or
    highest high (short) printed while price was back in the gap; rej: the rejection candle;
    prior: the candles before it (most recent last); p: StrategyParams."""
    bottom, top = fvg
    height = max(top - bottom, 1e-9)
    long = direction == "long"
    r = Review(approved=True)

    # 1. depth into the gap
    if retest_extreme is None:
        depth = 0.0
    else:
        depth = (top - retest_extreme) / height if long else (retest_extreme - bottom) / height
    depth = max(0.0, depth)
    r.case_for.append(f"FVG {bottom:.2f}-{top:.2f} retested {min(depth, 9.99):.0%} deep")
    if p.ch_min_depth > 0 and depth < p.ch_min_depth:
        r.objections.append(f"shallow retest: only {depth:.0%} into the gap (needs {p.ch_min_depth:.0%})")

    # 2 + 3. candle shape
    rng = rej.high - rej.low
    body = abs(rej.close - rej.open)
    body_frac = body / rng if rng > 0 else 0.0
    loc = ((rej.close - rej.low) / rng if long else (rej.high - rej.close) / rng) if rng > 0 else 0.0
    r.case_for.append(f"rejection candle body {body_frac:.0%} of range, closed in the "
                      f"{'top' if long else 'bottom'} {1 - loc:.0%}")
    if p.ch_min_body > 0 and body_frac < p.ch_min_body:
        r.objections.append(f"weak body: {body_frac:.0%} of the candle (needs {p.ch_min_body:.0%})")
    if p.ch_close_zone > 0 and (1 - loc) > p.ch_close_zone:
        r.objections.append(f"close not at the {'high' if long else 'low'}: "
                            f"{'top' if long else 'bottom'} {1 - loc:.0%} of the candle (needs {p.ch_close_zone:.0%})")

    # 4. force vs recent candles
    window = [b.high - b.low for b in prior[-10:]]
    avg = sum(window) / len(window) if window else 0.0
    mult = rng / avg if avg > 0 else 0.0
    r.case_for.append(f"candle {mult:.1f}x the average 1-min range")
    if p.ch_min_range_mult > 0 and avg > 0 and mult < p.ch_min_range_mult:
        r.objections.append(f"no force: candle only {mult:.1f}x the recent average (needs {p.ch_min_range_mult:g}x)")

    r.approved = not r.objections
    return r

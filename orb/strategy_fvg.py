"""
ORB + FVG strategy on 1-minute candles. Elijah's rules, in order:

1. OPENING RANGE  - High and low of 9:30-9:45 ET (the first 15-minute candle).
2. BREAKOUT       - A 1-min candle CLOSES outside the range. That sets the direction.
                    If price later closes outside the OTHER side, direction flips.
3. FVG            - The breakout push leaves a fair value gap: a 3-candle pattern where
                    candle 1's high is below candle 3's low (bullish), or candle 1's low is
                    above candle 3's high (bearish). The gap between them is the FVG zone.
                    fvg_mode = "edge": the gap has to sit on the range high (long) / range low
                                       (short), i.e. formed right at the 15-min reference price.
                    fvg_mode = "any":  any FVG in the breakout direction after the breakout.
                    Gaps smaller than min_fvg_frac x range width are ignored (noise).
4. RETEST         - A later candle trades back into the FVG zone.
5. REJECTION      - A candle closes back out of the zone in the breakout direction, beyond the
                    range edge, with the right color (green for long, red for short).
                    Entry = that candle's close.
                    A close through the far side of the FVG kills that FVG; the bot waits for a new one.
6. STOP / TARGET / EXIT - Same as the original version. stop_mode "fvg" puts the stop one tick
                    past the far side of the FVG (or the rejection candle's wick, whichever is
                    further), which sits inside the range in "edge" mode.

Day outcomes (for the compliance table):
    no_breakout, no_fvg (broke out, no qualifying gap), no_retest (gap never retested + rejected),
    fvg_failed (price closed through the gap and no new one formed), traded, news_day.
"""
from __future__ import annotations

from collections import deque
from datetime import timedelta

from . import challenger
from .strategy import MARKET_OPEN, Bar, ORBRetest, Signal, Trade

TICK = 0.01


class ORBFVG(ORBRetest):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.recent: deque[Bar] = deque(maxlen=3)
        self.fvg: tuple[float, float] | None = None      # (bottom, top)
        self.fvg_failed_once = False
        self.retest_low: float | None = None              # extreme price seen during the retest
        self.pending: dict | None = None                  # rejection seen, waiting for follow-through
        self.day_open: float | None = None                # 9:30 open, for the gap filter
        self.or_volume = 0.0                              # volume traded in the opening range
        self.history: deque[Bar] = deque(maxlen=11)        # recent candles, for the challenger
        self.vetoes = 0
        self.last_review = None

    # ---- helpers -------------------------------------------------------
    def _set_direction(self, d: str, bar: Bar) -> None:
        if d != self.direction:
            if self.direction is not None:
                self._note(bar, f"Direction flipped to {d}")
            self.direction = d
            self.breakout_time = bar.ts
            self.fvg = None
            self.touched = False
            self.retest_low = None
            self.pending = None
            self.outcome = "fvg_failed" if self.fvg_failed_once else "no_fvg"
            self._note(bar, f"Breakout {d}, close {bar.close:.2f}")

    def _find_fvg(self) -> tuple[float, float] | None:
        if len(self.recent) < 3:
            return None
        b1, b2, b3 = self.recent
        width = self.or_high - self.or_low
        tol = width * self.p.retest_tolerance
        if self.direction == "long":
            bottom, top = b1.high, b3.low
            if top - bottom < max(width * self.p.min_fvg_frac, TICK):
                return None
            if b2.close <= b2.open:                      # the middle candle must push up
                return None
            if self.p.fvg_mode == "edge":
                if not (bottom <= self.or_high + tol and top >= self.or_high - tol):
                    return None
            elif top <= self.or_high:                    # "any": gap must be above the range high
                return None
        else:
            top, bottom = b1.low, b3.high
            if top - bottom < max(width * self.p.min_fvg_frac, TICK):
                return None
            if b2.close >= b2.open:
                return None
            if self.p.fvg_mode == "edge":
                if not (top >= self.or_low - tol and bottom <= self.or_low + tol):
                    return None
            elif bottom >= self.or_low:
                return None
        return bottom, top

    def stop_level(self) -> float:
        if self.p.stop_mode == "fvg" and self.fvg:
            bottom, top = self.fvg
            if self.direction == "long":
                return min(bottom, self.retest_low if self.retest_low is not None else bottom) - TICK
            return max(top, self.retest_low if self.retest_low is not None else top) + TICK
        return super().stop_level()

    # ---- main entry point ---------------------------------------------
    def on_bar(self, bar: Bar) -> list[Signal]:
        t = bar.ts.time()
        if t < MARKET_OPEN or self.phase == self.DONE:
            return []
        if t < self._range_end:                          # 1. build the 9:30-9:45 range
            if self.day_open is None:
                self.day_open = bar.open
            self.or_volume += bar.volume
            self.or_high = bar.high if self.or_high is None else max(self.or_high, bar.high)
            self.or_low = bar.low if self.or_low is None else min(self.or_low, bar.low)
            self.recent.append(bar)                      # a range candle can be candle 1 of an FVG
            return []
        if self.phase == self.IN_TRADE:
            return self._manage_trade(bar)

        if self.phase == self.RANGE:                     # first bar after 9:45
            if self.or_high is None:
                self.phase = self.DONE
                return []
            self.phase = self.WAIT_BREAKOUT
            self.outcome = "no_breakout"
            self.log.append(f"Range set: {self.or_low:.2f} - {self.or_high:.2f}")
            avg = self.levels.or_volume_avg
            if self.p.min_rvol > 0 and avg:
                rvol = self.or_volume / avg
                self.log.append(f"Opening volume {rvol:.2f}x normal")
                if rvol < self.p.min_rvol:
                    self.log.append(f"Not in play (needs {self.p.min_rvol:g}x): no trades today")
                    self.phase, self.outcome = self.DONE, "low_volume"
                    return []

        if t >= self.p.entry_cutoff:
            self._note(bar, f"Cutoff reached ({self.outcome})")
            self.phase = self.DONE
            return []

        self.recent.append(bar)
        prior = list(self.history)
        self.history.append(bar)

        # 2. Direction from closes outside the range (can flip)
        if bar.close > self.or_high:
            self._set_direction("long", bar)
        elif bar.close < self.or_low:
            self._set_direction("short", bar)
        if self.direction is None:
            return []
        self.phase = self.WAIT_RETEST

        # 4 + 5. Retest + rejection of an existing FVG (checked before looking for a new gap,
        # so the candle that completes an FVG can't also be its retest)
        if self.fvg:
            bottom, top = self.fvg
            long = self.direction == "long"
            if (long and bar.close < bottom) or (not long and bar.close > top):
                self._note(bar, f"FVG {bottom:.2f}-{top:.2f} failed (closed through it)")
                self.fvg, self.touched, self.retest_low, self.pending = None, False, None, None
                self.fvg_failed_once = True
                self.outcome = "fvg_failed"
            else:
                # 5b. Follow-through: after the rejection candle, wait confirm_bars more candles.
                # Each must hold outside the FVG; then a close beyond the rejection candle's
                # high (long) / low (short) triggers the entry. Back into the FVG = cancelled.
                if self.pending:
                    back_in = bar.close <= top if long else bar.close >= bottom
                    if back_in:
                        self._note(bar, "Follow-through failed: closed back into the FVG")
                        self.pending = None
                    else:
                        self.pending["seen"] += 1
                        beyond = (bar.close > self.pending["level"]) if long else (bar.close < self.pending["level"])
                        if self.pending["seen"] >= self.p.confirm_bars and beyond:
                            self.pending = None
                            return self._enter(bar)
                        return []
                if long and bar.low <= top:
                    self.touched = True
                    self.retest_low = bar.low if self.retest_low is None else min(self.retest_low, bar.low)
                if not long and bar.high >= bottom:
                    self.touched = True
                    self.retest_low = bar.high if self.retest_low is None else max(self.retest_low, bar.high)
                if long:
                    confirmed = (self.touched and bar.close > top and bar.close > self.or_high
                                 and bar.close > bar.open)
                else:
                    confirmed = (self.touched and bar.close < bottom and bar.close < self.or_low
                                 and bar.close < bar.open)
                if confirmed and self.p.challenger in ("shadow", "veto"):
                    rv = challenger.review(self.direction, self.fvg, self.retest_low, bar, prior, self.p)
                    self.last_review = rv
                    for line in rv.debate(self.symbol):
                        self._note(bar, line)
                    if not rv.approved and self.p.challenger == "shadow":
                        self._note(bar, "(shadow mode: challenger's veto recorded, trade still allowed)")
                    if not rv.approved and self.p.challenger == "veto":
                        self.vetoes += 1
                        self.outcome = "vetoed"
                        self.touched, self.retest_low = False, None   # needs a fresh retest of the gap
                        return []
                if confirmed:
                    if self.p.confirm_bars > 0:
                        self.pending = {"seen": 0, "level": bar.high if long else bar.low}
                        self._note(bar, f"Rejection candle, waiting {self.p.confirm_bars} candle(s) for follow-through")
                        return []
                    return self._enter(bar)
                return []

        # 3. Look for a new FVG
        gap = self._find_fvg()
        if gap:
            self.fvg = gap
            self.touched, self.retest_low = False, None
            self.outcome = "no_retest"
            self._note(bar, f"FVG {self.direction} {gap[0]:.2f}-{gap[1]:.2f}")
        return []

    def gap_pct(self) -> float | None:
        pc = self.levels.prior_close
        if not pc or self.day_open is None:
            return None
        return (self.day_open - pc) / pc * 100

    def _blocked(self, bar: Bar) -> str | None:
        """Setup filters checked at the moment of entry."""
        p = self.p
        if p.gap_filter:
            g = self.gap_pct()
            if g is not None:
                against = -g if self.direction == "long" else g
                if against > p.gap_min_pct:
                    return f"against the opening gap ({g:+.2f}%)"
        if p.skip_start and p.skip_end:
            t = (bar.ts + timedelta(minutes=p.bar_minutes)).time()
            if p.skip_start <= t < p.skip_end:
                return f"inside the no-entry window {p.skip_start:%H:%M}-{p.skip_end:%H:%M}"
        return None

    def _enter(self, bar: Bar) -> list[Signal]:
        why = self._blocked(bar)
        if why:
            self._note(bar, f"Setup skipped: {why}")
            self.phase = self.DONE
            self.outcome = "filtered"
            return []
        entry = bar.close
        stop = self.stop_level()
        risk = abs(entry - stop)
        if risk <= 0:
            return []
        target, label = self._pick_target(entry, risk)
        self.trade = Trade(
            symbol=self.symbol, session_date=self.session_date, direction=self.direction,
            or_high=self.or_high, or_low=self.or_low, breakout_time=self.breakout_time,
            entry_time=bar.ts + timedelta(minutes=self.p.bar_minutes),
            entry=entry, stop=stop, target=target, target_label=label)
        self.phase, self.outcome = self.IN_TRADE, "traded"
        self._note(bar, f"ENTRY {self.direction} @ {entry:.2f}, stop {stop:.2f}, "
                        f"target {target:.2f} ({label}), FVG {self.fvg[0]:.2f}-{self.fvg[1]:.2f}")
        return [Signal("entry", self.trade)]


def make_engine(symbol, session_date, levels, params):
    """Pick the engine named in config.toml -> [strategy] name."""
    if params.name == "orb_fvg":
        return ORBFVG(symbol, session_date, levels, params)
    return ORBRetest(symbol, session_date, levels, params)

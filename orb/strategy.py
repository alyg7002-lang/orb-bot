"""
ORB retest strategy engine.

This file is the brain of the bot and the only place the trading rules live.
The backtester and the live paper trader both feed it the same 5-minute bars,
one at a time, so a backtest result and a live result mean the same thing.

The rules, in order:

1. OPENING RANGE  - High and low of the first 15 minutes (9:30-9:45 ET).
2. BREAKOUT       - First 5-min candle that CLOSES above the range high (long)
                    or below the range low (short). A wick through doesn't count.
3. RETEST         - After the breakout, price comes back and touches the range
                    edge (within a small tolerance).
4. CONFIRM        - A 5-min candle closes back in the breakout direction
                    (long: closes above range high AND is a green candle).
                    Entry = that candle's close.
5. STOP           - Back inside the opening range (midpoint by default,
                    or the opposite edge if stop_mode = "opposite").
6. TARGET         - The nearest key level beyond entry that's at least
                    min_rr x risk away: prior day high/low, premarket high/low.
                    No level far enough? Fall back to fallback_rr x risk.
7. EXIT           - Stop, target, or flat at 15:55 ET. If stop and target are
                    both hit inside the same candle, we assume the STOP hit
                    first. That's pessimistic on purpose.

One trade per symbol per day. No new entries after entry_cutoff.

Every day also gets an "outcome" so you can see how often each ticker actually
follows the setup:
    no_data       - bars missing
    no_breakout   - never closed outside the range before the cutoff
    no_retest     - broke out but never came back to retest before the cutoff
    invalidated   - broke out, then closed back through the stop level before
                    a confirmed retest (the breakout failed)
    traded        - full setup, a trade was taken
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional

MARKET_OPEN = time(9, 30)


@dataclass(frozen=True)
class Bar:
    """One candle. ts is the bar's START time in New York time."""
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Levels:
    """Key levels used for targets. Any of them can be missing (None)."""
    prior_high: Optional[float] = None
    prior_low: Optional[float] = None
    premarket_high: Optional[float] = None
    premarket_low: Optional[float] = None
    prior_close: Optional[float] = None      # for the gap filter (not a target level)
    or_volume_avg: Optional[float] = None    # avg volume of the first 15 min over recent days (relative volume)

    def named(self) -> dict[str, float]:
        raw = {
            "prior day high": self.prior_high,
            "prior day low": self.prior_low,
            "premarket high": self.premarket_high,
            "premarket low": self.premarket_low,
        }
        return {k: v for k, v in raw.items() if v is not None}


@dataclass
class StrategyParams:
    name: str = "orb_retest"         # "orb_retest" (5-min, original) or "orb_fvg" (1-min FVG version)
    range_minutes: int = 15
    bar_minutes: int = 5
    retest_tolerance: float = 0.10   # fraction of range width counted as "touching" the edge
    stop_mode: str = "mid"           # "mid" = range midpoint, "opposite" = other side of range,
                                     # "fvg" = just past the far side of the FVG (FVG version only)
    fvg_mode: str = "edge"           # "edge" = FVG must sit on the range high/low, "any" = any FVG after breakout
    min_fvg_frac: float = 0.05       # ignore gaps smaller than 5% of the range width (noise)
    skip_news: bool = True           # don't trade FOMC / CPI / jobs / earnings-reaction days
    confirm_bars: int = 0            # FVG version: extra candles to wait after the rejection candle
    min_rvol: float = 0.0            # "stocks in play": skip the day unless opening-range volume is at least
                                     # this many times its recent average (0 = off)
    rvol_days: int = 14              # days in that average
    gap_filter: bool = False         # skip trades AGAINST the opening gap (gap > gap_min_pct the other way)
    gap_min_pct: float = 0.1         # gaps smaller than this % count as "no gap" (both directions allowed)
    skip_start: Optional[time] = None  # no entries from skip_start ...
    skip_end: Optional[time] = None    # ... until skip_end (ET)
    breakeven_r: float = 0.0         # once a trade is up this many R, move the stop to entry (0 = off)
    target_mode: str = "levels"      # "levels" = nearest key level (fallback_rr if none), "fixed" = target_r x risk
    target_r: float = 1.0            # used when target_mode = "fixed"
    min_rr: float = 1.0              # a level must be at least this many R away to be the target
    fallback_rr: float = 2.0         # target used when no key level qualifies
    entry_cutoff: time = time(11, 30)
    flat_time: time = time(15, 55)
    exit_on_fvg_fail: bool = False   # FVG version: exit early when a candle closes back through the far side of the FVG
    reverse_after_stop: bool = False # FVG version: after a stop-out, allow ONE new setup in the other direction
    # challenger bot (orb/challenger.py): argues against every FVG entry; 0 turns a check off
    challenger: str = "off"          # "off", "shadow" (argues + logs, never blocks) or "veto" (must approve)
    ch_min_depth: float = 0.25       # retest must trade this far into the FVG (fraction of its height)
    ch_min_body: float = 0.5         # rejection candle body >= this fraction of its high-low range
    ch_close_zone: float = 0.33      # ...and closes within this fraction of its high (long) / low (short)
    ch_min_range_mult: float = 1.0   # ...and is at least this many times the average candle of the last 10 min


@dataclass
class Trade:
    symbol: str
    session_date: date
    direction: str                   # "long" or "short"
    or_high: float
    or_low: float
    breakout_time: datetime
    entry_time: datetime
    entry: float
    stop: float
    target: float
    target_label: str
    initial_stop: Optional[float] = None    # stop at entry; `stop` may move to breakeven later
    exit_time: Optional[datetime] = None
    exit: Optional[float] = None
    exit_reason: Optional[str] = None   # "stop", "target", "time", "eod"

    def __post_init__(self):
        if self.initial_stop is None:
            self.initial_stop = self.stop

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.initial_stop)

    @property
    def is_open(self) -> bool:
        return self.exit is None

    def points(self) -> Optional[float]:
        """Move in your favor per share (negative = loss)."""
        if self.exit is None:
            return None
        sign = 1 if self.direction == "long" else -1
        return (self.exit - self.entry) * sign

    def r_multiple(self) -> Optional[float]:
        pts = self.points()
        if pts is None or self.risk_per_share == 0:
            return None
        return pts / self.risk_per_share


@dataclass
class Signal:
    kind: str     # "entry" or "exit"
    trade: Trade


class ORBRetest:
    """Runs the rules for ONE symbol on ONE day. Feed it bars with on_bar()."""

    RANGE, WAIT_BREAKOUT, WAIT_RETEST, IN_TRADE, DONE = (
        "range", "wait_breakout", "wait_retest", "in_trade", "done")

    def __init__(self, symbol: str, session_date: date, levels: Levels,
                 params: StrategyParams | None = None):
        self.symbol = symbol
        self.session_date = session_date
        self.levels = levels
        self.p = params or StrategyParams()

        self.phase = self.RANGE
        self.outcome = "no_data"
        self.or_high: Optional[float] = None
        self.or_low: Optional[float] = None
        self.direction: Optional[str] = None
        self.breakout_time: Optional[datetime] = None
        self.touched = False
        self.trade: Optional[Trade] = None
        self.log: list[str] = []          # plain-English play-by-play of the day

        self._range_end = (datetime.combine(session_date, MARKET_OPEN)
                           + timedelta(minutes=self.p.range_minutes)).time()

    # ---- helpers -------------------------------------------------------
    @property
    def or_mid(self) -> float:
        return (self.or_high + self.or_low) / 2

    @property
    def tolerance(self) -> float:
        return (self.or_high - self.or_low) * self.p.retest_tolerance

    def stop_level(self) -> float:
        if self.p.stop_mode == "opposite":
            return self.or_low if self.direction == "long" else self.or_high
        return self.or_mid

    def _note(self, bar: Bar, msg: str) -> None:
        self.log.append(f"{bar.ts:%H:%M} {msg}")

    def _bar_end(self, bar: Bar) -> time:
        return (bar.ts + timedelta(minutes=self.p.bar_minutes)).time()

    def _pick_target(self, entry: float, risk: float) -> tuple[float, str]:
        if self.p.target_mode == "fixed":
            dist = self.p.target_r * risk
            return (entry + dist if self.direction == "long" else entry - dist), f"{self.p.target_r:g}R fixed"
        need = self.p.min_rr * risk
        best: tuple[float, str] | None = None
        for name, lvl in self.levels.named().items():
            dist = (lvl - entry) if self.direction == "long" else (entry - lvl)
            if dist >= need and (best is None or dist < best[0]):
                best = (dist, name)
        if best:
            dist, name = best
            price = entry + dist if self.direction == "long" else entry - dist
            return price, name
        dist = self.p.fallback_rr * risk
        price = entry + dist if self.direction == "long" else entry - dist
        return price, f"{self.p.fallback_rr:g}R fallback"

    # ---- main entry point ---------------------------------------------
    def on_bar(self, bar: Bar) -> list[Signal]:
        t = bar.ts.time()
        if t < MARKET_OPEN or self.phase == self.DONE:
            return []

        # 1. Build the opening range
        if self.phase == self.RANGE:
            if t < self._range_end:
                self.or_high = bar.high if self.or_high is None else max(self.or_high, bar.high)
                self.or_low = bar.low if self.or_low is None else min(self.or_low, bar.low)
                return []
            if self.or_high is None:            # no opening bars came in
                self.phase = self.DONE
                return []
            self.phase = self.WAIT_BREAKOUT
            self.outcome = "no_breakout"
            self.log.append(f"Range set: {self.or_low:.2f} - {self.or_high:.2f}")
            # fall through: this bar can be the breakout bar

        # 2. Wait for a candle to CLOSE outside the range
        if self.phase == self.WAIT_BREAKOUT:
            if t >= self.p.entry_cutoff:
                self._note(bar, "No breakout before cutoff")
                self.phase = self.DONE
                return []
            if bar.close > self.or_high:
                self.direction = "long"
            elif bar.close < self.or_low:
                self.direction = "short"
            else:
                return []
            self.breakout_time = bar.ts
            self.phase = self.WAIT_RETEST
            self.outcome = "no_retest"
            self._note(bar, f"Breakout {self.direction}, close {bar.close:.2f}")
            return []   # the breakout candle itself can't be the retest

        # 3 + 4. Wait for the retest and a confirming close
        if self.phase == self.WAIT_RETEST:
            if t >= self.p.entry_cutoff:
                self._note(bar, "No confirmed retest before cutoff")
                self.phase = self.DONE
                return []
            stop = self.stop_level()
            if self.direction == "long":
                if bar.close < stop:
                    self._note(bar, f"Invalidated: closed {bar.close:.2f} below stop level {stop:.2f}")
                    self.outcome, self.phase = "invalidated", self.DONE
                    return []
                if bar.low <= self.or_high + self.tolerance:
                    self.touched = True
                confirmed = self.touched and bar.close > self.or_high and bar.close > bar.open
            else:
                if bar.close > stop:
                    self._note(bar, f"Invalidated: closed {bar.close:.2f} above stop level {stop:.2f}")
                    self.outcome, self.phase = "invalidated", self.DONE
                    return []
                if bar.high >= self.or_low - self.tolerance:
                    self.touched = True
                confirmed = self.touched and bar.close < self.or_low and bar.close < bar.open
            if not confirmed:
                return []

            entry = bar.close
            risk = abs(entry - stop)
            if risk <= 0:
                return []
            target, label = self._pick_target(entry, risk)
            self.trade = Trade(
                symbol=self.symbol, session_date=self.session_date, direction=self.direction,
                or_high=self.or_high, or_low=self.or_low, breakout_time=self.breakout_time,
                entry_time=bar.ts + timedelta(minutes=self.p.bar_minutes),  # filled at candle close
                entry=entry, stop=stop, target=target, target_label=label)
            self.phase, self.outcome = self.IN_TRADE, "traded"
            self._note(bar, f"ENTRY {self.direction} @ {entry:.2f}, stop {stop:.2f}, "
                            f"target {target:.2f} ({label})")
            return [Signal("entry", self.trade)]

        # 5 + 6 + 7. Manage the open trade
        if self.phase == self.IN_TRADE:
            return self._manage_trade(bar)
        return []

    def _manage_trade(self, bar: Bar) -> list[Signal]:
        """Stop / target / time exit. Shared by every strategy variant."""
        tr = self.trade
        exit_px, reason = None, None
        if tr.direction == "long":
            if bar.low <= tr.stop:                       # stop first (pessimistic)
                exit_px, reason = min(bar.open, tr.stop), self._stop_reason()   # gap down fills worse
            elif bar.high >= tr.target:
                exit_px, reason = max(bar.open, tr.target), "target"
        else:
            if bar.high >= tr.stop:
                exit_px, reason = max(bar.open, tr.stop), self._stop_reason()
            elif bar.low <= tr.target:
                exit_px, reason = min(bar.open, tr.target), "target"
        if exit_px is None and self._bar_end(bar) >= self.p.flat_time:
            exit_px, reason = bar.close, "time"
        if exit_px is None:
            # Breakeven: checked AFTER the stop, so the stop can only move on the next candle
            if self.p.breakeven_r > 0 and tr.stop != tr.entry:
                risk = abs(tr.entry - tr.initial_stop)
                best = (bar.high - tr.entry) if tr.direction == "long" else (tr.entry - bar.low)
                if best >= self.p.breakeven_r * risk:
                    tr.stop = tr.entry
                    self._note(bar, f"Stop moved to breakeven {tr.entry:.2f}")
            return []
        return [self._close(bar, exit_px, reason,
                            exit_time=bar.ts if reason != "time"
                            else bar.ts + timedelta(minutes=self.p.bar_minutes))]

    def _stop_reason(self) -> str:
        return "breakeven" if self.trade.stop == self.trade.entry else "stop"

    def _close(self, bar: Bar, price: float, reason: str, exit_time: datetime) -> Signal:
        tr = self.trade
        tr.exit, tr.exit_reason, tr.exit_time = price, reason, exit_time
        self.phase = self.DONE
        self._note(bar, f"EXIT {reason} @ {price:.2f} ({tr.r_multiple():+.2f}R)")
        return Signal("exit", tr)

    def finish(self, last_bar: Bar | None) -> list[Signal]:
        """Call when the day's data ends. Closes anything still open."""
        if self.phase == self.IN_TRADE and last_bar is not None:
            return [self._close(last_bar, last_bar.close, "eod",
                                last_bar.ts + timedelta(minutes=self.p.bar_minutes))]
        self.phase = self.DONE
        return []

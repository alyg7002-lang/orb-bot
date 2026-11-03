"""Prior-day high/low strategies and their paper bot, on hand-built candles (no network)."""
import unittest
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from orb import config, pdhl, pdhl_bot
from orb.journal import Journal
from orb.panel import Daily
from orb.strategy import Bar

ET = ZoneInfo("America/New_York")
DAY = date(2026, 11, 4)


def prev_days(n=25, hi=101.0, lo=99.0, close=100.0, open_=100.0, last=None):
    out = [Daily(DAY - timedelta(days=n - i), open_, hi, lo, close) for i in range(n)]
    if last:
        out[-1] = Daily(out[-1].day, *last)
    return out


def session(prices, day=DAY):
    """One 1-min bar per price (open=prev close, high/low = the two ends), padded flat to 16:00."""
    bars, t = [], datetime.combine(day, time(9, 30), ET)
    o = prices[0]
    for p in prices:
        bars.append(Bar(t, o, max(o, p), min(o, p), p, 1000)); o = p; t += timedelta(minutes=1)
    while t.time() < time(16, 0):
        bars.append(Bar(t, o, o, o, o, 1000)); t += timedelta(minutes=1)
    return bars


class TestStrategies(unittest.TestCase):
    def test_oops_long_fills_at_prior_low_and_exits_at_close(self):
        rth = session([98.0, 98.5, 99.2, 99.6, 100.0])          # opens below PDL 99, climbs back
        t = pdhl.run_day("oops", "SPY", DAY, rth, prev_days())
        self.assertEqual(t.side, "long")
        self.assertAlmostEqual(t.entry, 99.0)
        self.assertAlmostEqual(t.stop, 97.99)                   # day's low 98.00 - 1 cent
        self.assertEqual(t.exit_reason, "time")
        self.assertGreater(t.r, 0)

    def test_oops_needs_a_gap(self):
        self.assertIsNone(pdhl.run_day("oops", "SPY", DAY, session([100.0, 99.5, 100.5]), prev_days()))

    def test_eighty_twenty_needs_selling_bar_then_push_below(self):
        prev = prev_days(last=(100.9, 101.0, 99.0, 99.1))       # opened top 20%, closed bottom 20%
        rth = session([99.5, 99.2, 98.8, 98.6, 99.1, 99.6])
        t = pdhl.run_day("eighty_twenty", "SPY", DAY, rth, prev)
        self.assertEqual((t.side, t.entry), ("long", 99.0))
        self.assertIsNone(pdhl.run_day("eighty_twenty", "SPY", DAY, rth, prev_days()))   # no 80-20 bar

    def test_breakout_long_on_5min_close_above_pdh_hits_2r(self):
        rth = session([100.0] * 5 + [100.5, 100.8, 101.0, 101.2, 101.3] + [101.4, 102.0, 103.0, 103.5, 104.0])
        t = pdhl.run_day("pdhl_breakout", "SPY", DAY, rth, prev_days())
        self.assertEqual(t.side, "long")
        self.assertAlmostEqual(t.entry, 101.3)
        self.assertEqual(t.exit_reason, "target")
        self.assertAlmostEqual(t.r, 2 - 0.02 / (t.entry - t.stop), places=3)

    def test_fade_short_after_failed_break_of_pdh(self):
        rth = session([100.5, 100.8, 101.2, 101.4, 101.1] + [100.9, 100.7, 100.6, 100.5, 100.4] + [100.0] * 20)
        t = pdhl.run_day("pdhl_fade", "SPY", DAY, rth, prev_days())
        self.assertEqual(t.side, "short")
        self.assertAlmostEqual(t.stop, 101.41)

    def test_nr7_breakout_stop_is_mid_of_prior_day(self):
        prev = prev_days(hi=102, lo=98, last=(100, 100.5, 99.5, 100))
        t = pdhl.run_day("nr7", "SPY", DAY, session([100.0, 100.3, 100.6, 100.9]), prev)
        self.assertEqual((t.side, t.entry, t.stop), ("long", 100.51, 100.0))
        self.assertIsNone(pdhl.run_day("nr7", "SPY", DAY, session([100.0, 100.3, 100.6, 100.9]), prev_days(hi=102, lo=98)))

    def test_vol_breakout_stop_is_open(self):
        t = pdhl.run_day("vol_breakout", "SPY", DAY, session([100.0, 100.6, 101.1, 101.5]), prev_days())
        self.assertEqual((t.side, t.entry, t.stop), ("long", 101.0, 100.0))

    def test_min_stop_is_a_tenth_of_atr(self):
        rth = session([98.95, 99.0, 99.3])                        # tiny gap: stop would be 6 cents
        t = pdhl.run_day("oops", "SPY", DAY, rth, prev_days())
        self.assertAlmostEqual(t.entry - t.stop, 0.1 * pdhl.atr(prev_days()), places=6)

    def test_half_day_is_skipped(self):
        rth = [b for b in session([98.0, 99.5]) if b.ts.time() < time(13, 0)]
        self.assertIsNone(pdhl.run_day("oops", "SPY", DAY, rth, prev_days()))


class FakeBars:
    def __init__(self, by_day):
        self.by_day = by_day
    def minute_bars(self, symbols, start, end):
        return {s: [b for d in sorted(self.by_day) if start <= d < end for b in self.by_day[d]] for s in symbols}


def flat_day(day, px=100.0, hi=101.0, lo=99.0):
    bars, t = [], datetime.combine(day, time(9, 30), ET)
    while t.time() < time(16, 0):
        m = (t.hour * 60 + t.minute) % 4
        bars.append(Bar(t, px, hi if m == 0 else px, lo if m == 2 else px, px, 1000)); t += timedelta(minutes=1)
    return bars


class TestBot(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()
        self.cfg.symbols = ["SPY"]
        self.j = Journal(":memory:")
        days = {}
        d = pdhl_bot.START - timedelta(days=40)
        while d < pdhl_bot.START:
            if d.weekday() < 5:
                days[d] = flat_day(d)
            d += timedelta(days=1)
        last = max(days)
        days[last] = flat_day(last, hi=103.0)        # wide day: so the Oops day is not also an NR7 day
        days[pdhl_bot.START] = session([98.0, 98.5, 99.2, 99.6, 100.0], pdhl_bot.START)   # an Oops day
        self.data = FakeBars(days)

    def test_journals_once_and_is_idempotent(self):
        out = []
        n = pdhl_bot.run(self.cfg, self.j, ["oops", "nr7"], data=self.data, today=pdhl_bot.START + timedelta(days=1), say=out.append)
        self.assertEqual(n, 1)
        t = self.j.trades("paper_oops")[0]
        self.assertEqual((t["symbol"], t["direction"], t["entry"]), ("SPY", "long", 99.0))
        self.assertEqual(t["qty"], int(100 / (99.0 - t["stop"])))
        self.assertEqual([s["outcome"] for s in self.j.sessions("paper_nr7")], ["no_setup"])
        n2 = pdhl_bot.run(self.cfg, self.j, ["oops", "nr7"], data=self.data, today=pdhl_bot.START + timedelta(days=1), say=out.append)
        self.assertEqual(n2, 0)
        self.assertEqual(len(self.j.trades("paper_oops")), 1)

    def test_nothing_before_start(self):
        self.assertEqual(pdhl_bot.run(self.cfg, self.j, ["oops"], data=self.data, today=pdhl_bot.START, say=lambda m: None), 0)


if __name__ == "__main__":
    unittest.main()

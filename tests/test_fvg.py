import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from orb.strategy import Levels, StrategyParams
from orb.strategy_fvg import ORBFVG, make_engine
from orb.strategy import Bar, ORBRetest

ET = ZoneInfo("America/New_York")
D = date(2026, 10, 15)


def P(**kw):
    base = dict(name="orb_fvg", bar_minutes=1, stop_mode="fvg", fvg_mode="edge")
    base.update(kw)
    return StrategyParams(**base)


def bars(rows, start=(9, 30)):
    t = datetime(D.year, D.month, D.day, *start, tzinfo=ET)
    out = []
    for o, h, l, c in rows:
        out.append(Bar(t, o, h, l, c))
        t += timedelta(minutes=1)
    return out


# 15 one-minute range candles: high 101, low 99 (width 2 -> tolerance 0.2, min gap 0.1)
RANGE = [(100, 101, 99, 100)] + [(100.5, 100.95, 99.5, 100.6)] * 14

LONG_SETUP = [
    (100.6, 100.9, 100.5, 100.8),   # 9:45 candle 1 (high 100.9)
    (100.8, 101.8, 100.7, 101.7),   # 9:46 candle 2: green, closes above 101 -> long breakout
    (101.7, 102.0, 101.3, 101.9),   # 9:47 candle 3 (low 101.3) -> FVG 100.9-101.3, sits on the 101 level
    (101.9, 101.95, 101.2, 101.4),  # 9:48 dips into FVG, closes above it but RED -> no entry
    (101.35, 101.8, 101.25, 101.7), # 9:49 green close above FVG + range -> ENTRY 101.7
]


def run(rows, params=None, levels=None):
    e = ORBFVG("TEST", D, levels or Levels(), params or P())
    seq = bars(RANGE + rows)
    sigs = []
    for b in seq:
        sigs += e.on_bar(b)
    sigs += e.finish(seq[-1])
    return e, sigs


class TestORBFVG(unittest.TestCase):
    def test_range_from_first_15_one_minute_candles(self):
        e, _ = run([(100, 100.3, 99.8, 100.1)])
        self.assertEqual((e.or_high, e.or_low), (101, 99))

    def test_long_fvg_retest_rejection_entry(self):
        e, sigs = run(LONG_SETUP + [(101.7, 103.5, 101.6, 103.4)])
        t = e.trade
        self.assertEqual(e.outcome, "traded")
        self.assertEqual(t.direction, "long")
        self.assertAlmostEqual(t.entry, 101.7)
        self.assertEqual(t.entry_time.strftime("%H:%M"), "09:50")
        # stop one tick under the lower of FVG bottom (100.9) and retest low (101.2)
        self.assertAlmostEqual(t.stop, 100.89)
        self.assertAlmostEqual(t.target, 101.7 + 2 * 0.81)
        self.assertEqual(t.exit_reason, "target")
        self.assertEqual([s.kind for s in sigs], ["entry", "exit"])

    def test_red_rejection_candle_does_not_enter(self):
        e, _ = run(LONG_SETUP[:4])
        self.assertIsNone(e.trade)
        self.assertEqual(e.outcome, "no_retest")

    def test_fvg_fails_when_price_closes_through_it(self):
        e, _ = run(LONG_SETUP[:3] + [(101.9, 101.9, 100.6, 100.7)] + [(100.7, 100.9, 100.6, 100.8)] * 5)
        self.assertIsNone(e.trade)
        self.assertEqual(e.outcome, "fvg_failed")

    def test_breakout_without_gap_is_no_fvg(self):
        rows = [(100.8, 101.4, 100.7, 101.3), (101.3, 101.5, 100.9, 101.4),
                (101.4, 101.6, 101.3, 101.5), (101.5, 101.7, 101.4, 101.6)]   # every candle overlaps candle-before-last
        e, _ = run(rows)
        self.assertEqual(e.outcome, "no_fvg")
        self.assertIsNone(e.trade)

    def test_edge_mode_ignores_gap_far_from_range(self):
        rows = [(100.8, 101.3, 100.7, 101.2),     # 9:45 breakout long, no gap
                (101.2, 101.5, 100.9, 101.4),     # 9:46 no gap
                (101.4, 102.0, 101.3, 101.9),     # 9:47 no gap
                (101.9, 102.6, 101.8, 102.5),     # 9:48 FVG 101.5-101.8: above the range, not ON the 101 level
                (102.5, 103.2, 102.4, 103.1),
                (103.1, 103.2, 101.7, 102.0),     # retest into the gap, red
                (102.0, 102.6, 101.9, 102.5)]     # green rejection
        e, _ = run(rows)
        self.assertIsNone(e.trade)
        self.assertEqual(e.outcome, "no_fvg")
        e2, _ = run(rows, params=P(fvg_mode="any"))
        self.assertIsNotNone(e2.trade)
        self.assertAlmostEqual(e2.trade.entry, 102.5)

    def test_short_mirror(self):
        rows = [
            (99.4, 99.5, 99.1, 99.2),     # candle 1 (low 99.1)
            (99.2, 99.3, 98.2, 98.3),     # red, closes below 99 -> short
            (98.3, 98.7, 98.0, 98.1),     # candle 3 high 98.7 -> FVG 98.7-99.1 at the 99 level
            (98.1, 98.8, 98.05, 98.6),    # retest up into FVG
            (98.6, 98.65, 98.1, 98.2),    # red close below FVG + range -> ENTRY 98.2
            (98.2, 98.3, 96.0, 96.2),
        ]
        e, _ = run(rows, levels=Levels(prior_low=96.5))
        t = e.trade
        self.assertEqual(t.direction, "short")
        self.assertAlmostEqual(t.entry, 98.2)
        self.assertAlmostEqual(t.stop, 99.11)
        self.assertEqual(t.target_label, "prior day low")
        self.assertEqual(t.exit_reason, "target")

    def test_mid_stop_mode(self):
        e, _ = run(LONG_SETUP, params=P(stop_mode="mid"))
        self.assertAlmostEqual(e.trade.stop, 100.0)

    def test_no_entry_after_cutoff(self):
        idle = [(100, 100.5, 99.5, 100.2)] * 110      # sideways until 11:35
        e, _ = run(idle + LONG_SETUP)
        self.assertIsNone(e.trade)

    def test_breakeven_turns_reversal_into_scratch(self):
        # entry 101.7, risk 0.81 -> +1R = 102.51. Price tags 102.6, then collapses through the old stop.
        rows = LONG_SETUP + [(101.7, 102.6, 101.65, 102.5), (102.5, 102.5, 100.5, 100.6)]
        e, _ = run(rows, params=P(breakeven_r=1.0))
        self.assertEqual(e.trade.exit_reason, "breakeven")
        self.assertAlmostEqual(e.trade.r_multiple(), 0.0)
        e2, _ = run(rows)                                   # off: full loss
        self.assertEqual(e2.trade.exit_reason, "stop")
        self.assertAlmostEqual(e2.trade.r_multiple(), -1.0)

    def test_breakeven_not_triggered_in_same_candle_as_stop(self):
        rows = LONG_SETUP + [(101.7, 102.6, 100.5, 100.6)]  # +1R and stop in one candle -> stop wins
        e, _ = run(rows, params=P(breakeven_r=1.0))
        self.assertEqual(e.trade.exit_reason, "stop")

    def test_fixed_target_and_opposite_stop(self):
        e, _ = run(LONG_SETUP, params=P(stop_mode="opposite", target_mode="fixed", target_r=1.0),
                   levels=Levels(prior_high=105))
        t = e.trade
        self.assertAlmostEqual(t.stop, 99.0)                 # far side of the range
        self.assertAlmostEqual(t.target, 101.7 + 2.7)        # 1R, ignores the prior-day level
        self.assertEqual(t.target_label, "1R fixed")

    def test_confirm_bars_waits_for_follow_through(self):
        # rejection candle 9:49 (high 101.8). With 2 confirm candles: 9:50 holds, 9:51 closes above 101.8
        rows = LONG_SETUP + [(101.7, 101.75, 101.4, 101.6), (101.6, 102.0, 101.5, 101.95)]
        e, _ = run(rows, params=P(confirm_bars=2))
        self.assertAlmostEqual(e.trade.entry, 101.95)
        self.assertEqual(e.trade.entry_time.strftime("%H:%M"), "09:52")

    def test_confirm_keeps_waiting_until_close_beyond_rejection_high(self):
        rows = LONG_SETUP + [(101.7, 101.75, 101.4, 101.6), (101.6, 101.7, 101.5, 101.65),
                             (101.65, 101.9, 101.6, 101.85)]
        e, _ = run(rows, params=P(confirm_bars=1))
        self.assertAlmostEqual(e.trade.entry, 101.85)

    def test_confirm_cancelled_when_back_in_fvg(self):
        rows = LONG_SETUP + [(101.7, 101.7, 101.0, 101.1),       # closes back inside FVG (top 101.3)
                             (101.1, 101.9, 101.05, 101.85)]     # this is a fresh rejection, not an entry
        e, _ = run(rows, params=P(confirm_bars=1))
        self.assertIsNone(e.trade)
        e2, _ = run(rows + [(101.85, 102.2, 101.8, 102.1)], params=P(confirm_bars=1))
        self.assertAlmostEqual(e2.trade.entry, 102.1)

    def test_gap_filter_blocks_trade_against_gap(self):
        # day opens at 100 (first range candle); prior close 101 -> gap -0.99%, long is AGAINST it
        e, _ = run(LONG_SETUP, params=P(gap_filter=True), levels=Levels(prior_close=101.0))
        self.assertIsNone(e.trade)
        self.assertEqual(e.outcome, "filtered")
        e2, _ = run(LONG_SETUP, params=P(gap_filter=True), levels=Levels(prior_close=99.0))  # gap up: long OK
        self.assertIsNotNone(e2.trade)
        e3, _ = run(LONG_SETUP, params=P(gap_filter=True), levels=Levels(prior_close=100.05))  # tiny gap: OK
        self.assertIsNotNone(e3.trade)

    def test_skip_window(self):
        from datetime import time
        e, _ = run(LONG_SETUP, params=P(skip_start=time(9, 45), skip_end=time(10, 0)))
        self.assertIsNone(e.trade)
        e2, _ = run(LONG_SETUP, params=P(skip_start=time(10, 0), skip_end=time(10, 15)))
        self.assertIsNotNone(e2.trade)

    def test_rvol_filter(self):
        # every test candle has volume 0 -> opening volume 0; any positive average makes it "not in play"
        e, _ = run(LONG_SETUP, params=P(min_rvol=1.5), levels=Levels(or_volume_avg=1000))
        self.assertIsNone(e.trade)
        self.assertEqual(e.outcome, "low_volume")
        e2, _ = run(LONG_SETUP, params=P(min_rvol=1.5), levels=Levels())        # no history yet -> allowed
        self.assertIsNotNone(e2.trade)

    def test_make_engine(self):
        self.assertIsInstance(make_engine("X", D, Levels(), P()), ORBFVG)
        self.assertIs(type(make_engine("X", D, Levels(), StrategyParams())), ORBRetest)


if __name__ == "__main__":
    unittest.main()

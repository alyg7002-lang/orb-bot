import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from orb.strategy import Bar, Levels, ORBRetest, StrategyParams

ET = ZoneInfo("America/New_York")
D = date(2026, 10, 15)


def bars(rows, start="09:30"):
    """rows = [(open, high, low, close), ...] as consecutive 5-min bars."""
    h, m = map(int, start.split(":"))
    t = datetime(D.year, D.month, D.day, h, m, tzinfo=ET)
    out = []
    for o, hi, lo, c in rows:
        out.append(Bar(t, o, hi, lo, c))
        t += timedelta(minutes=5)
    return out


# Opening range: high 101, low 99 -> mid 100, width 2, tolerance 0.2
RANGE = [(100, 101, 99.5, 100.5), (100.5, 100.8, 99, 99.5), (99.5, 100.9, 99.2, 100.6)]


def run(rows, levels=None, params=None):
    s = ORBRetest("TEST", D, levels or Levels(), params)
    sigs = []
    seq = bars(RANGE + rows)
    for b in seq:
        sigs += s.on_bar(b)
    sigs += s.finish(seq[-1])
    return s, sigs


class TestORBRetest(unittest.TestCase):
    def test_range_built_from_first_15_minutes(self):
        s, _ = run([(100.6, 100.7, 100.2, 100.4)])
        self.assertEqual((s.or_high, s.or_low), (101, 99))

    def test_long_retest_hits_level_target(self):
        rows = [
            (100.6, 101.8, 100.5, 101.6),   # breakout close above 101
            (101.6, 101.7, 101.1, 101.3),   # pulls back, touches 101.2 zone
            (101.3, 101.9, 101.2, 101.8),   # green close above 101 -> ENTRY 101.8
            (101.8, 104.2, 101.7, 104.0),   # hits target
        ]
        s, sigs = run(rows, Levels(prior_high=104.0, premarket_high=102.0))
        t = s.trade
        self.assertEqual(s.outcome, "traded")
        self.assertEqual(t.direction, "long")
        self.assertAlmostEqual(t.entry, 101.8)
        self.assertAlmostEqual(t.stop, 100.0)          # range midpoint
        # risk 1.8; premarket high 102 is only 0.2 away (< 1R) so prior high wins
        self.assertEqual(t.target_label, "prior day high")
        self.assertEqual(t.exit_reason, "target")
        self.assertAlmostEqual(t.r_multiple(), (104 - 101.8) / 1.8)
        self.assertEqual([x.kind for x in sigs], ["entry", "exit"])

    def test_stop_wins_when_both_hit_in_same_bar(self):
        rows = [
            (100.6, 101.8, 100.5, 101.6),
            (101.6, 101.7, 101.1, 101.3),
            (101.3, 101.9, 101.2, 101.8),
            (101.8, 110.0, 99.0, 102.0),   # huge candle hits both
        ]
        s, _ = run(rows, Levels(prior_high=104.0))
        self.assertEqual(s.trade.exit_reason, "stop")
        self.assertAlmostEqual(s.trade.r_multiple(), -1.0)

    def test_fallback_target_when_no_level(self):
        rows = [
            (100.6, 101.8, 100.5, 101.6),
            (101.6, 101.7, 101.1, 101.3),
            (101.3, 101.9, 101.2, 101.8),
        ]
        s, _ = run(rows)
        self.assertEqual(s.trade.target_label, "2R fallback")
        self.assertAlmostEqual(s.trade.target, 101.8 + 2 * 1.8)

    def test_no_retest_if_price_runs_away(self):
        rows = [(100.6, 101.8, 100.5, 101.6)] + [(102 + i, 103 + i, 101.9 + i, 102.8 + i) for i in range(30)]
        s, sigs = run(rows)
        self.assertEqual(s.outcome, "no_retest")
        self.assertEqual(sigs, [])

    def test_red_candle_does_not_confirm(self):
        rows = [
            (100.6, 101.8, 100.5, 101.6),
            (101.6, 101.7, 101.1, 101.3),
            (101.7, 101.8, 101.2, 101.4),   # closes above 101 but RED -> no entry
        ]
        s, _ = run(rows)
        self.assertIsNone(s.trade)

    def test_invalidated_when_closing_through_mid(self):
        rows = [
            (100.6, 101.8, 100.5, 101.6),
            (101.6, 101.7, 99.5, 99.8),     # closes below midpoint 100
        ]
        s, _ = run(rows)
        self.assertEqual(s.outcome, "invalidated")

    def test_short_mirror(self):
        rows = [
            (99.4, 99.5, 98.2, 98.4),       # breakout down
            (98.4, 98.9, 98.3, 98.7),       # retest up near 99
            (98.7, 98.8, 98.1, 98.2),       # red close below 99 -> ENTRY 98.2
            (98.2, 98.3, 95.9, 96.1),
        ]
        s, _ = run(rows, Levels(prior_low=96.0))   # 2.2 away > 1R of 1.8
        t = s.trade
        self.assertEqual(t.direction, "short")
        self.assertAlmostEqual(t.stop, 100.0)
        self.assertEqual(t.target_label, "prior day low")
        self.assertEqual(t.exit_reason, "target")
        self.assertGreater(t.r_multiple(), 0)

    def test_time_exit_at_1555(self):
        rows = [
            (100.6, 101.8, 100.5, 101.6),
            (101.6, 101.7, 101.1, 101.3),
            (101.3, 101.9, 101.2, 101.8),
        ]
        # drift sideways all day until 15:50 bar
        n = int((datetime(2026, 1, 1, 15, 55) - datetime(2026, 1, 1, 10, 0)).seconds / 300)
        rows += [(101.8, 102.0, 101.6, 101.9)] * n
        s, _ = run(rows)
        self.assertEqual(s.trade.exit_reason, "time")
        self.assertEqual(s.trade.exit_time.strftime("%H:%M"), "15:55")

    def test_no_breakout_before_cutoff(self):
        rows = [(100, 100.5, 99.5, 100.1)] * 30
        s, _ = run(rows)
        self.assertEqual(s.outcome, "no_breakout")

    def test_opposite_stop_mode(self):
        rows = [
            (100.6, 101.8, 100.5, 101.6),
            (101.6, 101.7, 101.1, 101.3),
            (101.3, 101.9, 101.2, 101.8),
        ]
        s, _ = run(rows, params=StrategyParams(stop_mode="opposite"))
        self.assertAlmostEqual(s.trade.stop, 99.0)


if __name__ == "__main__":
    unittest.main()

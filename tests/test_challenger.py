"""The challenger bot, replayed on trade #9 (SPY short, Tue 10/27/2026).

Candles: Yahoo Finance 1-min SPY, 9:30-10:09 ET. On this feed the 9:51 candle only tags the
edge of the 9:48-9:50 FVG (about 4% of the way in) before price drops away again: exactly the
"didn't really go into the FVG" entry Elijah flagged.
"""
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from orb import config
from orb.challenger import review
from orb.strategy import Bar, Levels
from orb.strategy_fvg import ORBFVG

ET = ZoneInfo("America/New_York")
O = [767.34, 767.42, 767.58, 767.22, 767.41, 767.52, 767.29, 767.44, 768.13, 768.11, 768.19, 768.05, 767.93, 768.00, 767.63, 767.78, 767.47, 767.46, 767.47, 767.26, 766.36, 766.41, 766.41, 766.06, 766.11, 766.13, 765.99, 766.35, 766.38, 766.22, 766.22, 765.83, 765.70, 765.83, 765.97, 766.01, 766.05, 766.15, 766.32, 766.11]
H = [767.56, 767.77, 767.78, 767.48, 767.60, 767.53, 767.53, 767.87, 768.36, 768.27, 768.20, 768.09, 768.05, 768.02, 767.85, 768.07, 767.93, 767.56, 767.47, 767.27, 766.53, 766.55, 766.43, 766.12, 766.14, 766.21, 766.39, 766.41, 766.38, 766.35, 766.30, 765.95, 765.89, 766.13, 766.11, 766.06, 766.24, 766.34, 766.35, 766.21]
L = [767.22, 767.42, 767.09, 767.22, 767.35, 767.18, 767.23, 767.42, 767.83, 768.06, 768.03, 767.73, 767.75, 767.44, 767.53, 767.69, 767.30, 767.36, 767.10, 766.33, 766.16, 766.12, 766.04, 765.78, 765.80, 765.96, 765.98, 766.18, 766.09, 766.07, 765.79, 765.65, 765.57, 765.82, 765.87, 765.88, 765.96, 766.07, 766.11, 765.85]
C = [767.40, 767.56, 767.21, 767.41, 767.54, 767.29, 767.45, 767.81, 768.13, 768.20, 768.06, 767.95, 767.96, 767.63, 767.79, 767.93, 767.47, 767.49, 767.26, 766.36, 766.53, 766.41, 766.05, 766.03, 766.11, 765.97, 766.35, 766.39, 766.19, 766.23, 765.81, 765.71, 765.81, 765.98, 765.99, 766.05, 766.12, 766.31, 766.11, 765.95]
T0 = datetime(2026, 10, 27, 9, 30, tzinfo=ET)
BARS = [Bar(T0 + timedelta(minutes=i), O[i], H[i], L[i], C[i], 1000.0) for i in range(len(C))]


def replay(mode):
    p = replace(config.load().strategy, challenger=mode, skip_news=False)
    eng = ORBFVG("SPY", date(2026, 10, 27), Levels(), p)
    entries = []
    for b in BARS:
        for sig in eng.on_bar(b):
            if sig.kind == "entry":
                entries.append(sig.trade)
    return eng, entries


class TestChallenger(unittest.TestCase):
    def test_orb_bot_alone_takes_trade_9(self):
        eng, entries = replay("off")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].direction, "short")
        self.assertEqual(entries[0].entry_time.strftime("%H:%M"), "09:54")

    def test_challenger_vetoes_trade_9(self):
        eng, entries = replay("veto")
        self.assertEqual(entries, [])
        debate = "\n".join(eng.log)
        self.assertIn("Challenger: VETO", debate)
        self.assertIn("shallow retest", debate)

    def test_shadow_mode_records_but_still_trades(self):
        eng, entries = replay("shadow")
        self.assertEqual(len(entries), 1)
        self.assertIn("Challenger: VETO", "\n".join(eng.log))

    def test_strong_deep_rejection_is_double_accepted(self):
        p = replace(config.load().strategy, challenger="veto")
        prior = [Bar(T0 + timedelta(minutes=i), 100, 100.2, 99.8, 100, 1) for i in range(10)]   # 0.40 avg range
        rej = Bar(T0 + timedelta(minutes=10), 100.5, 100.55, 99.6, 99.62, 1)                    # big red, closes at low
        rv = review("short", (100.0, 101.0), 100.6, rej, prior, p)                                # 60% into the gap
        self.assertTrue(rv.approved, rv.objections)
        self.assertIn("DOUBLE ACCEPTED", "\n".join(rv.debate("SPY")))

    def test_each_objection_fires(self):
        p = replace(config.load().strategy, challenger="veto")
        prior = [Bar(T0 + timedelta(minutes=i), 100, 100.5, 99.5, 100, 1) for i in range(10)]   # 1.00 avg range
        rej = Bar(T0 + timedelta(minutes=10), 100.0, 100.3, 99.7, 99.9, 1)                      # small, wicky, mid close
        rv = review("long", (100.0, 101.0), 100.95, rej, prior, p)                               # 5% into the gap
        text = " | ".join(rv.objections)
        for k in ("shallow retest", "weak body", "close not at the high", "no force"):
            self.assertIn(k, text)
        self.assertFalse(rv.approved)


if __name__ == "__main__":
    unittest.main()

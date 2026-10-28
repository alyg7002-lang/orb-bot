"""RSI(2) pullback bot with fake market data (no network)."""
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

from orb import config, rsi2
from orb.journal import Journal
from orb.panel import Daily

ET = ZoneInfo("America/New_York")
TODAY = datetime.now(ET).date()


def uptrend_with_dip(n=220):
    """Steady uptrend (well above the 200-day average), then 3 sharp down days."""
    days, px = [], 100.0
    d0 = TODAY - timedelta(days=n + 5)
    for i in range(n):
        px *= 1.003 if i < n - 3 else 0.985
        days.append(Daily(d0 + timedelta(days=i), px, px * 1.01, px * 0.99, px))
    return days


class FakeData:
    def __init__(self, days, price):
        self.days, self.price = days, price
    def clock(self):
        return NS(is_open=True, next_open=datetime.now(ET))
    def daily(self, symbols, today):
        return {s: self.days for s in symbols}
    def latest(self, symbols):
        return {s: (self.price, datetime.now(ET)) for s in symbols}


class TestRSI2(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()
        self.cfg.symbols = ["SPY"]
        self.j = Journal(":memory:")
        self.out = []

    def run_bot(self, days, price):
        rsi2.run(self.cfg, self.j, data=FakeData(days, price), say=self.out.append, wait=False)

    def test_buys_the_dip_in_an_uptrend(self):
        days = uptrend_with_dip()
        self.run_bot(days, days[-1].close * 0.99)
        rows = self.j.open_trades(rsi2.MODE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["direction"], "long")
        self.assertGreater(rows[0]["qty"], 0)

    def test_no_entry_without_dip(self):
        days = uptrend_with_dip()[:-3]
        self.run_bot(days, days[-1].close * 1.003)
        self.assertEqual(self.j.open_trades(rsi2.MODE), [])

    def test_exit_when_price_closes_above_5_day_average(self):
        days = uptrend_with_dip()
        self.run_bot(days, days[-1].close * 0.99)
        row = self.j.open_trades(rsi2.MODE)[0]
        self.j.db.execute("UPDATE trades SET session_date=? WHERE id=?",
                          ((TODAY - timedelta(days=1)).isoformat(), row["id"]))   # entered yesterday
        self.run_bot(days, days[-4].close * 1.02)                                    # bounced
        closed = self.j.trades(rsi2.MODE)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["exit_reason"], "sma5")
        self.assertGreater(closed[0]["pnl"], 0)

    def test_second_run_same_day_does_nothing(self):
        days = uptrend_with_dip()
        self.run_bot(days, days[-1].close * 0.99)
        self.run_bot(days, days[-4].close * 1.02)
        self.assertEqual(len(self.j.open_trades(rsi2.MODE)), 1)
        self.assertEqual(self.j.trades(rsi2.MODE), [])

    def test_rsi_math(self):
        self.assertAlmostEqual(rsi2.rsi([1, 2, 3, 4, 5]), 100.0)
        self.assertLess(rsi2.rsi([5, 4, 3, 2, 1.5]), 5)


if __name__ == "__main__":
    unittest.main()

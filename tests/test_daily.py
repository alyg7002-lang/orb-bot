import unittest
from dataclasses import replace
from datetime import date

from orb import config
from orb.backtest import run_backtest
from orb.daily import DailyGuard
from orb.journal import Journal
from orb.synthetic import generate


class TestDailyGuard(unittest.TestCase):
    def test_profit_target_counts_open_profit(self):
        g = DailyGuard(200, 75)
        g.add(150)
        self.assertIsNone(g.check(unrealized=40))
        self.assertEqual(g.check(unrealized=55), "profit_target")
        self.assertEqual(g.check(unrealized=-500), "profit_target")   # stays halted

    def test_loss_limit_uses_closed_trades_only(self):
        g = DailyGuard(200, 75)
        self.assertIsNone(g.check(unrealized=-90))   # open loss alone doesn't halt
        g.add(-80)
        self.assertEqual(g.check(), "loss_limit")

    def test_allows_only_if_full_loss_stays_inside_limit(self):
        g = DailyGuard(250, 100)
        self.assertTrue(g.allows(99.5))
        g.add(-97)                      # a stop-out that rounded to less than $100
        self.assertFalse(g.allows(99.5))
        g2 = DailyGuard(250, 100)
        g2.add(90)                      # up $90: a $99 risk trade is fine
        self.assertTrue(g2.allows(99))

    def test_off_when_zero(self):
        g = DailyGuard(0, 0)
        g.add(-10_000)
        self.assertIsNone(g.check(unrealized=10_000))


class TestBacktestDailyLimits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = config.load()
        cls.bars = {s: generate(s, 250, end=date(2026, 9, 30), seed=21, minutes=1) for s in cfg.symbols}
        cls.off = replace(cfg, risk=replace(cfg.risk, daily_profit_target=0, daily_loss_limit=0, max_open_trades=0))
        # tiny limits so they trigger a lot on fake data (structure test, not a strategy test)
        cls.on = replace(cfg, risk=replace(cfg.risk, daily_profit_target=1, daily_loss_limit=1000, max_open_trades=0))
        cls.j = Journal(":memory:")
        cls.a = run_backtest(cls.bars, cls.off, cls.j, mode="demo", label="off")
        cls.b = run_backtest(cls.bars, cls.on, cls.j, mode="demo", label="on")

    def test_limits_only_remove_or_cut_trades(self):
        ta, tb = self.j.trades("demo", self.a), self.j.trades("demo", self.b)
        self.assertGreater(len(ta), len(tb))
        keys_a = {(t["symbol"], t["session_date"], t["entry"]) for t in ta}
        self.assertTrue({(t["symbol"], t["session_date"], t["entry"]) for t in tb} <= keys_a)

    def test_no_new_trade_after_limit_hit(self):
        by_day = {}
        for t in self.j.trades("demo", self.b):
            by_day.setdefault(t["session_date"], []).append(t)
        for day, ts in by_day.items():
            ts.sort(key=lambda t: t["entry_time"])
            realized, halted_at = 0.0, None
            for t in sorted(ts, key=lambda t: t["exit_time"]):
                realized += t["pnl"]
                if halted_at is None and realized >= 1:
                    halted_at = t["exit_time"]
            if halted_at:
                for t in ts:
                    self.assertLessEqual(t["entry_time"], halted_at, day)

    def test_one_trade_at_a_time_never_overlaps(self):
        cfg = config.load()
        one = replace(cfg, risk=replace(cfg.risk, max_open_trades=1, daily_profit_target=0, daily_loss_limit=0))
        rid = run_backtest(self.bars, one, self.j, mode="demo", label="one")
        ts = self.j.trades("demo", rid)
        by_day = {}
        for t in ts:
            by_day.setdefault(t["session_date"], []).append(t)
        for day, lst in by_day.items():
            lst.sort(key=lambda t: t["entry_time"])
            for a, b in zip(lst, lst[1:]):
                self.assertGreater(b["entry_time"], a["exit_time"], day)
        self.assertLess(len(ts), len(self.j.trades("demo", self.a)))
        self.assertIn("one_at_a_time", {s["outcome"] for s in self.j.sessions("demo", rid)})

    def test_skipped_days_marked(self):
        outcomes = {s["outcome"] for s in self.j.sessions("demo", self.b)}
        self.assertIn("daily_limit", outcomes)


if __name__ == "__main__":
    unittest.main()


class TestCompoundSizing(unittest.TestCase):
    def test_size_factor(self):
        from orb.config import RiskConfig
        from orb.risk import size_factor
        fixed = RiskConfig()
        self.assertEqual(size_factor(fixed, 5000), 1.0)
        c = RiskConfig(sizing="compound", starting_equity=10000, risk_pct=1.0, max_risk_per_trade=150)
        self.assertAlmostEqual(size_factor(c, 0), 1.0)          # 1% of $10k = $100
        self.assertAlmostEqual(size_factor(c, 2000), 1.2)       # up $2k -> $120
        self.assertAlmostEqual(size_factor(c, -1000), 0.9)      # down $1k -> $90
        self.assertAlmostEqual(size_factor(c, 90000), 1.5)      # capped at $150

    def test_backtest_risk_grows_with_profit(self):
        cfg = config.load()
        c = replace(cfg, risk=replace(cfg.risk, sizing="compound", starting_equity=10000, risk_pct=1.0,
                                      max_open_trades=0, daily_profit_target=0, daily_loss_limit=0))
        j = Journal(":memory:"); ds = {}
        bars = {s: generate(s, 120, end=date(2026, 9, 30), seed=4, minutes=1) for s in cfg.symbols}
        run_backtest(bars, c, j, mode="demo", label="cmp", day_stats=ds)
        days = sorted(ds)
        for a, b in zip(days, days[1:]):
            expect = (10000 + ds[a]["equity_end"]) * 0.01
            self.assertAlmostEqual(ds[b]["risk"], min(expect, c.risk.max_risk_per_trade), places=6)

"""End-to-end checks: backtest -> journal -> stats -> dashboard, on fake data."""
import random
import tempfile
import unittest
from dataclasses import replace as _replace
from pathlib import Path

from orb import config
from orb.backtest import run_backtest
from orb.journal import Journal
from orb.report import build
from orb.risk import pnl, position_size
from orb.stats import compliance, summarize
from orb.synthetic import generate


def _no_limits(cfg):
    """These tests check the strategy plumbing, so switch off the daily/portfolio limits."""
    return _replace(cfg, risk=_replace(cfg.risk, daily_profit_target=0, daily_loss_limit=0, max_open_trades=0,
                                       sizing="fixed"))


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from dataclasses import replace
        cfg = config.load()
        # original 5-min rules here (fast); the 1-min FVG rules get their own run below
        cls.cfg = replace(cfg, strategy=replace(cfg.strategy, name="orb_retest", bar_minutes=5,
                                                stop_mode="mid", skip_news=False),
                          risk=replace(cfg.risk, daily_profit_target=0, daily_loss_limit=0, max_open_trades=0,
                                       sizing="fixed"))
        cls.j = Journal(":memory:")
        bars = {s: generate(s, 300, seed=11) for s in cls.cfg.symbols}
        cls.run_id = run_backtest(bars, cls.cfg, cls.j, mode="demo")
        cls.trades = cls.j.trades("demo", cls.run_id)

    def test_trades_logged_with_complete_fields(self):
        self.assertGreater(len(self.trades), 50)
        for t in self.trades:
            for k in ("entry", "stop", "target", "exit", "exit_reason", "pnl", "r_multiple", "qty"):
                self.assertIsNotNone(t[k], k)

    def test_one_trade_per_symbol_per_day(self):
        keys = [(t["symbol"], t["session_date"]) for t in self.trades]
        self.assertEqual(len(keys), len(set(keys)))

    def test_losses_capped_near_1R_unless_gap(self):
        # A stop fill can only be worse than -1R on a gap through the stop
        worst = min(t["r_multiple"] for t in self.trades)
        self.assertGreater(worst, -3)

    def test_risk_per_trade_respected(self):
        for t in self.trades:
            self.assertLessEqual(abs(t["entry"] - t["stop"]) * t["qty"], self.cfg.risk.risk_per_trade + 1e-6)

    def test_no_edge_on_random_data(self):
        # Sanity check against lookahead bias: random walk must not look like a money printer
        m = summarize(self.trades)
        self.assertLess(m["expectancy_r"], 0.3)

    def test_compliance_counts_add_up(self):
        c = compliance(self.j.sessions("demo", self.run_id))
        for sym, v in c.items():
            from orb.stats import OUTCOMES
            parts = sum(v[k] for k in OUTCOMES)
            self.assertEqual(parts, v["days"])
            self.assertEqual(v["traded"], sum(1 for t in self.trades if t["symbol"] == sym))

    def test_dashboard_builds(self):
        with tempfile.TemporaryDirectory() as d:
            out = build(self.j, Path(d) / "dash.html")
            html = out.read_text(encoding="utf-8")
            self.assertIn('"mode": "demo"', html)
            self.assertNotIn("/*__DATA__*/null", html)


class TestFVGPipeline(unittest.TestCase):
    def test_fvg_backtest_skips_news_days_and_logs_trades(self):
        from datetime import date
        from orb import news
        cfg = _no_limits(config.load())                        # orb_fvg, 1-min, news filter on
        self.assertEqual((cfg.strategy.name, cfg.strategy.bar_minutes), ("orb_fvg", 1))
        j = Journal(":memory:")
        end = date(2026, 10, 16)
        bars = {s: generate(s, 60, end=end, seed=5, minutes=1) for s in cfg.symbols}
        rid = run_backtest(bars, cfg, j, mode="demo")
        sessions = j.sessions("demo", rid)
        table = news.load()
        for s in sessions:
            blocked = news.event_for(s["symbol"], date.fromisoformat(s["session_date"]), table)
            self.assertEqual(s["outcome"] == "news_day", blocked is not None, (s["symbol"], s["session_date"]))
        self.assertTrue(any(s["outcome"] == "news_day" for s in sessions))
        trades = j.trades("demo", rid)
        self.assertGreater(len(trades), 0)
        for t in trades:
            self.assertLessEqual(t["entry_time"][11:16], "11:31")


    def test_breakeven_changes_exits_not_entries(self):
        # Exit management must never add or drop trades (regression: BE used to zero the risk)
        from dataclasses import replace
        from datetime import date
        cfg = _no_limits(config.load())
        bars = {s: generate(s, 40, end=date(2026, 9, 30), seed=9, minutes=1) for s in cfg.symbols[:2]}
        j = Journal(":memory:")
        a = run_backtest(bars, cfg, j, mode="demo", label="a")
        cfg_be = replace(cfg, strategy=replace(cfg.strategy, breakeven_r=1.0))
        b = run_backtest(bars, cfg_be, j, mode="demo", label="b")
        ta, tb = j.trades("demo", a), j.trades("demo", b)
        self.assertGreater(len(ta), 0)
        self.assertEqual([(t["symbol"], t["session_date"], t["entry"], t["stop"], t["qty"]) for t in ta],
                         [(t["symbol"], t["session_date"], t["entry"], t["stop"], t["qty"]) for t in tb])
        self.assertTrue(all(t["r_multiple"] > -1.5 for t in tb))


class TestNews(unittest.TestCase):
    def test_calendar(self):
        from datetime import date
        from orb import news
        t = news.load()
        self.assertEqual(news.event_for("SPY", date(2026, 3, 18), t), "FOMC decision")
        self.assertIn("Nvidia", news.event_for("NVDA", date(2026, 2, 26), t))
        self.assertIsNone(news.event_for("SPY", date(2026, 2, 26), t))   # NVDA earnings don't block SPY
        self.assertIsNone(news.event_for("SPY", date(2026, 10, 15), t))


class TestRisk(unittest.TestCase):
    def test_sizing(self):
        self.assertEqual(position_size(500, 498, 100, 25000), 50)
        self.assertEqual(position_size(500, 499.9, 100, 25000), 50)   # capped by $25k position
        self.assertEqual(position_size(10, 10, 100, 25000), 0)

    def test_pnl_short(self):
        self.assertAlmostEqual(pnl("short", 100, 98, 10, 0.01), 20 - 0.2)


if __name__ == "__main__":
    unittest.main()

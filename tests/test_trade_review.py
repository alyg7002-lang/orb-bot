"""The trade review export Jarvis reads: which days each trade needs candles for, and the JSON rows."""
import importlib.util
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from orb.journal import Journal

_spec = importlib.util.spec_from_file_location("trade_review", Path(__file__).resolve().parents[1] / "scripts" / "trade_review.py")
tr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tr)
_spec2 = importlib.util.spec_from_file_location("reconcile", Path(__file__).resolve().parents[1] / "scripts" / "reconcile.py")
rc = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(rc)


class TestTradeReview(unittest.TestCase):
    def test_chart_days_skip_weekends(self):
        self.assertEqual(tr.chart_days(date(2026, 10, 23), date(2026, 10, 26)), [date(2026, 10, 23), date(2026, 10, 26)])
        self.assertEqual(tr.chart_days(date(2026, 10, 29), date(2026, 10, 29)), [date(2026, 10, 29)])

    def test_rows_cover_every_paper_bot_and_skip_backtests(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "j.db"
            j = Journal(db)
            ins = ("INSERT INTO trades (mode, symbol, session_date, direction, entry_time, entry, stop, target, qty, "
                   "exit_time, exit, exit_reason, pnl, r_multiple) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
            j.db.execute(ins, ("paper", "QQQ", "2026-10-23", "long", "2026-10-23T10:04:00-04:00", 705.77, 701.18,
                               713.42, 21, "2026-10-26T15:55:06-04:00", 712.33, "stale_close", 137.85, 1.43))
            j.db.execute(ins, ("paper_rsi2", "SPY", "2026-10-28", "long", "2026-10-28T15:50:00-04:00", 760.0, None,
                               None, 5, None, None, None, None, None))
            j.db.execute(ins, ("backtest", "SPY", "2026-10-01", "long", "2026-10-01T10:00:00-04:00", 1, 0.5, 2, 1,
                               "2026-10-01T11:00:00-04:00", 2, "target", 1, 2))
            j.db.commit()
            j.db.close()
            rows = tr.trades(db)
        self.assertEqual([r["bot"] for r in rows], ["ORB", "RSI(2)"])
        self.assertEqual(rows[0]["days"], ["2026-10-23", "2026-10-26"])
        self.assertEqual(rows[0]["status"], "closed")
        self.assertEqual(rows[1]["status"], "open")
        self.assertIsNone(rows[1]["stop"])
        self.assertEqual(rows[1]["days"][0], "2026-10-28")    # open trade runs through today


class TestShadowRule(unittest.TestCase):
    T = dict(direction="long", entry=100.0, stop=98.0, target=103.0, qty=10, entry_time="2026-11-04T10:00:00-05:00")

    def bars(self, *rows):
        """rows: (minutes after 10:00, open, high, low, close)"""
        base = datetime.fromisoformat(self.T["entry_time"])
        return [(base + timedelta(minutes=m), o, h, l, c) for m, o, h, l, c in rows]

    def test_green_at_the_mark_closes_at_that_candles_close(self):
        out = tr.replay(self.T, self.bars((0, 100, 100.5, 99.5, 100), (29, 100, 101, 100, 101), (40, 101, 103.5, 101, 103)), 30)
        self.assertEqual((out["why"], out["exit"]), ("green", 101))
        self.assertAlmostEqual(out["pnl"], 9.8)         # $1 x 10 shares less 2 cents a share

    def test_even_at_the_mark_counts_as_green(self):
        self.assertEqual(tr.replay(self.T, self.bars((29, 100, 101, 99, 100)), 30)["why"], "green")

    def test_red_at_the_mark_closes_when_back_at_entry(self):
        out = tr.replay(self.T, self.bars((29, 100, 100, 99, 99), (35, 99, 100.2, 99, 100), (40, 100, 103.5, 100, 103)), 30)
        self.assertEqual((out["why"], out["exit"]), ("even", 100))

    def test_red_that_never_recovers_still_hits_the_stop(self):
        self.assertEqual(tr.replay(self.T, self.bars((29, 100, 100, 99, 99), (35, 99, 99.5, 97.9, 98)), 30)["why"], "stop")

    def test_target_before_the_mark_is_untouched(self):
        self.assertEqual(tr.replay(self.T, self.bars((5, 100, 103.5, 100, 103)), 30)["why"], "target")

    def test_reds_only_version_lets_green_trades_run(self):
        out = tr.replay(self.T, self.bars((29, 100, 101, 100, 101), (40, 101, 103.5, 101, 103)), 30, green=False)
        self.assertEqual(out["why"], "target")

    def test_no_rule_runs_to_the_flat(self):
        base = datetime.fromisoformat("2026-11-04T15:54:00-05:00")
        self.assertEqual(tr.replay(self.T, [(base, 100, 101, 99, 100.5)])["why"], "time")


class TestReconcile(unittest.TestCase):
    TRADE = dict(id=1, symbol="NVDA", session_date="2026-10-23", direction="long", qty=38, entry=212.81, exit=212.81,
                 entry_time="2026-10-23T10:30:00-04:00", exit_time="2026-10-26T15:55:18-04:00", broker_order_id="A")
    PARENT = dict(id="A", symbol="NVDA", side="buy", qty="38", status="filled", order_class="bracket", filled_avg_price="212.81",
                  filled_at="2026-10-23T14:30:00+00:00", legs=[dict(type="limit", status="canceled", filled_avg_price=None, filled_at=None)])
    CLOSE = dict(id="B", symbol="NVDA", side="sell", qty="38", status="filled", order_class="simple", filled_avg_price="216.2",
                 filled_at="2026-10-26T19:55:20+00:00", legs=[])

    def test_wrong_exit_price_is_caught(self):
        out = rc.compare_orders([self.TRADE], [self.PARENT, self.CLOSE], [])
        self.assertEqual([i["kind"] for i in out["issues"]], ["exit_mismatch"])
        self.assertEqual(out["fills"][1]["exit_fill"], 216.2)

    def test_matching_trade_is_clean(self):
        out = rc.compare_orders([{**self.TRADE, "exit": 216.2}], [self.PARENT, self.CLOSE], [])
        self.assertEqual(out["issues"], [])

    def test_live_leg_on_a_closed_trade_is_caught(self):
        parent = {**self.PARENT, "legs": [dict(type="stop", status="new", filled_avg_price=None, filled_at=None)]}
        out = rc.compare_orders([{**self.TRADE, "exit": 216.2}], [parent, self.CLOSE], [])
        self.assertEqual([i["kind"] for i in out["issues"]], ["leftover_order"])

    def test_order_or_position_missing_from_journal_is_caught(self):
        kinds = [i["kind"] for i in rc.compare_orders([], [self.PARENT], ["SPY"])["issues"]]
        self.assertEqual(kinds, ["not_in_journal", "position_not_in_journal"])

    def test_journal_trade_without_order_is_caught(self):
        kinds = [i["kind"] for i in rc.compare_orders([self.TRADE], [], [])["issues"]]
        self.assertIn("no_order", kinds)

    def test_coverage_finds_a_missed_day_and_gives_replay_bots_a_day(self):
        days = [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]
        seen = {"paper": {("SPY", "2026-11-03")}, "paper_nr7": {("SPY", "2026-11-03")}}
        out = rc.coverage(seen, days, date(2026, 11, 6))
        self.assertEqual(out["paper"]["missing"], ["2026-11-04", "2026-11-05"])
        self.assertEqual(out["paper_nr7"]["missing"], ["2026-11-04"])     # 11/05 is journaled the next morning


class TestDashboardHasEveryBot(unittest.TestCase):
    def test_other_bots_get_a_tab_even_with_targetless_trades(self):
        from orb.report import PAPER_BOTS, build
        j = Journal(":memory:")
        j.db.execute("INSERT INTO trades (mode, symbol, session_date, direction, entry_time, entry, stop, target, qty, exit_time, exit, "
                     "exit_reason, pnl, r_multiple) VALUES ('paper_nr7','SPY','2026-11-03','long','2026-11-03T10:00:00-05:00',"
                     "10,9,NULL,5,'2026-11-03T15:55:00-05:00',11,'time',5,1)")
        j.db.commit()
        with tempfile.TemporaryDirectory() as d:
            html = build(j, Path(d) / "dash.html").read_text(encoding="utf-8")
        for mode in PAPER_BOTS:
            self.assertIn(f'"mode": "{mode}"', html)


if __name__ == "__main__":
    unittest.main()

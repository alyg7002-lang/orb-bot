"""The trade review export Jarvis reads: which days each trade needs candles for, and the JSON rows."""
import importlib.util
import tempfile
import unittest
from datetime import date
from pathlib import Path

from orb.journal import Journal

_spec = importlib.util.spec_from_file_location("trade_review", Path(__file__).resolve().parents[1] / "scripts" / "trade_review.py")
tr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tr)


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


if __name__ == "__main__":
    unittest.main()

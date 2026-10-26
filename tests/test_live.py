"""Live trader order handling against a fake Alpaca client (no network)."""
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS
from unittest import mock
from zoneinfo import ZoneInfo

from orb import config, live
from orb.journal import Journal
from orb.strategy import Levels, ORBRetest, Signal, Trade

ET = ZoneInfo("America/New_York")


class FakeTC:
    def __init__(self):
        self.submitted, self.cancelled, self.closed = [], [], []
        self.legs = [NS(id="tp", type=NS(value="limit"), status=NS(value="new"), filled_avg_price=None, filled_at=None),
                     NS(id="sl", type=NS(value="stop"), status=NS(value="held"), filled_avg_price=None, filled_at=None)]

    def submit_order(self, req):
        self.submitted.append(req)
        return NS(id="parent-1")

    def get_order_by_id(self, oid, filt=None):
        if oid == "close-1":
            return NS(filled_avg_price="501.10")
        return NS(filled_avg_price="500.05", legs=self.legs)

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)

    def replace_order_by_id(self, oid, req):
        self.replaced = (oid, req.stop_price)

    def close_position(self, sym):
        self.closed.append(sym)
        return NS(id="close-1")


def make_trader(use_broker=True):
    t = live.LiveTrader.__new__(live.LiveTrader)
    t.cfg, t.j, t.use_broker = config.load(), Journal(":memory:"), use_broker
    t.tc, t.open, t.today = FakeTC(), {}, date(2026, 10, 15)
    t.engines = {"SPY": ORBRetest("SPY", t.today, Levels())}
    t.fed_until = {}
    from orb.daily import DailyGuard
    t.guard = DailyGuard(0, 0)
    t.factor = 1.0
    return t


def trade():
    now = datetime.now(ET)
    return Trade("SPY", date(2026, 10, 15), "long", 501, 499, now, now, 500.0, 498.0, 504.0, "prior day high")


class TestLive(unittest.TestCase):
    def setUp(self):
        live.say = lambda m: None
        live._time.sleep = lambda s: None

    def test_entry_sends_bracket_and_logs(self):
        t = make_trader()
        t._handle(Signal("entry", trade()), datetime.now(ET), 500)
        req = t.tc.submitted[0]
        self.assertEqual(req.qty, 50)                       # $100 / $2 risk
        self.assertEqual(req.take_profit.limit_price, 504.0)
        self.assertEqual(req.stop_loss.stop_price, 498.0)
        self.assertIn("SPY", t.open)

    def test_stale_signal_skipped(self):
        t = make_trader()
        t._handle(Signal("entry", trade()), datetime.now(ET) - timedelta(minutes=10), 500)
        self.assertEqual(t.tc.submitted, [])
        self.assertEqual(t.engines["SPY"].outcome, "missed")

    def test_broker_target_fill_closes_trade_with_real_prices(self):
        t = make_trader()
        t._handle(Signal("entry", trade()), datetime.now(ET), 500)
        t.tc.legs[0].status.value, t.tc.legs[0].filled_avg_price = "filled", "504.02"
        t.tc.legs[0].filled_at = datetime.now(ET)
        t._check_broker("SPY")
        row = t.j.trades("paper")[0]
        self.assertEqual(row["entry"], 500.05)             # actual fill replaced the plan
        self.assertEqual(row["exit_reason"], "target")
        self.assertAlmostEqual(row["pnl"], (504.02 - 500.05) * 50, places=2)
        self.assertNotIn("SPY", t.open)

    def test_flatten_cancels_legs_then_closes(self):
        t = make_trader()
        t._handle(Signal("entry", trade()), datetime.now(ET), 500)
        t._flatten("SPY", 501.0)
        self.assertEqual(sorted(t.tc.cancelled), ["sl", "tp"])
        self.assertEqual(t.tc.closed, ["SPY"])
        self.assertEqual(t.j.trades("paper")[0]["exit"], 501.10)

    def test_simulated_mode_uses_engine_exit(self):
        t = make_trader(use_broker=False)
        tr = trade()
        t._handle(Signal("entry", tr), datetime.now(ET), 500)
        tr.exit, tr.exit_reason, tr.exit_time = 498.0, "stop", datetime.now(ET)
        t._handle(Signal("exit", tr), datetime.now(ET), 498)
        row = t.j.trades("paper")[0]
        self.assertEqual(t.tc.submitted, [])
        self.assertAlmostEqual(row["r_multiple"], -1.0)

    def test_breakeven_moves_alpaca_stop_leg(self):
        t = make_trader()
        t._handle(Signal("entry", trade()), datetime.now(ET), 500)
        t._sync_stop("SPY", 498.0)                          # unchanged -> no call
        self.assertFalse(hasattr(t.tc, "replaced"))
        t._sync_stop("SPY", 500.0)
        self.assertEqual(t.tc.replaced, ("sl", 500.0))

    def test_daily_profit_target_flattens_and_stops(self):
        from orb.daily import DailyGuard
        t = make_trader()
        t.guard = DailyGuard(200, 75)
        t.guard.add(161)                                      # earlier trades today
        t._handle(Signal("entry", trade()), datetime.now(ET), 500)
        t.tc.get_all_positions = lambda: [NS(symbol="SPY", unrealized_pl="45.0")]
        t._apply_daily_limits({})
        self.assertEqual(t.tc.closed, ["SPY"])                # open trade closed to lock the day
        self.assertEqual(t.j.trades("paper")[0]["exit_reason"], "daily_target")
        self.assertEqual(t.engines["SPY"].phase, ORBRetest.DONE)

    def test_daily_loss_limit_blocks_new_entries(self):
        from orb.daily import DailyGuard
        t = make_trader()
        t.guard = DailyGuard(200, 75)
        t.guard.add(-100)
        t.tc.get_all_positions = lambda: []
        t._apply_daily_limits({})
        self.assertEqual(t.engines["SPY"].phase, ORBRetest.DONE)
        self.assertEqual(t.engines["SPY"].outcome, "daily_limit")
        self.assertEqual(t.tc.closed, [])

    def test_one_trade_at_a_time_live(self):
        t = make_trader()
        t.cfg.risk.max_open_trades = 1
        t.engines["QQQ"] = ORBRetest("QQQ", t.today, Levels())
        t._handle(Signal("entry", trade()), datetime.now(ET), 500)
        q = trade(); q.symbol = "QQQ"
        t._handle(Signal("entry", q), datetime.now(ET), 500)
        self.assertEqual(len(t.tc.submitted), 1)
        self.assertEqual(t.engines["QQQ"].outcome, "one_at_a_time")

    def _with_open_trade(self, t):
        t._handle(Signal("entry", trade()), datetime.now(ET), 500)
        t.open.clear()                                    # new process: only the journal remembers
        t.engines.clear()
        return t

    def test_closeout_records_target_fill(self):
        t = self._with_open_trade(make_trader())
        t.tc.legs[0].status.value, t.tc.legs[0].filled_avg_price = "filled", "504.00"
        t.tc.legs[0].filled_at = datetime.now(ET)
        t.tc.get_clock = lambda: NS(is_open=True, next_close=datetime.now(ET) + timedelta(hours=3))
        t.closeout()
        row = t.j.trades("paper")[0]
        self.assertEqual(row["exit_reason"], "target")
        self.assertEqual(t.tc.closed, [])

    def test_closeout_flattens_at_flat_time(self):
        t = self._with_open_trade(make_trader())
        t.tc.get_clock = lambda: NS(is_open=True, next_close=datetime.now(ET) + timedelta(minutes=2))
        t.closeout()                                      # flat time (close - 5 min) already passed
        self.assertEqual(t.tc.closed, ["SPY"])
        self.assertEqual(t.j.trades("paper")[0]["exit_reason"], "time")

    def test_closeout_market_closed_leaves_trade(self):
        t = self._with_open_trade(make_trader())
        t.tc.get_clock = lambda: NS(is_open=False, next_close=datetime.now(ET) + timedelta(hours=20))
        t.closeout()
        self.assertEqual(t.tc.closed, [])
        self.assertEqual(len(t.j.open_trades("paper")), 1)

    def test_old_open_trade_closed_next_morning(self):
        t = self._with_open_trade(make_trader())
        t.today = date(2026, 10, 16)                      # trade's session_date is 10/15
        t._reconcile_old()
        self.assertEqual(t.tc.closed, ["SPY"])
        self.assertEqual(t.j.trades("paper")[0]["exit_reason"], "stale_close")

    def test_flatten_survives_pending_cancel(self):
        """10/23 bug: cancelling one bracket leg auto-cancels the other, and Alpaca rejects the
        second cancel with 'order pending cancel'. The close must still go through."""
        t = make_trader()
        t._handle(Signal("entry", trade()), datetime.now(ET), 500)
        def cancel(oid):
            t.tc.cancelled.append(oid)
            if oid == "tp":                                  # Alpaca: sibling goes pending_cancel itself
                t.tc.legs[0].status.value = "canceled"
                return
            t.tc.legs[1].status.value = "canceled"
            raise Exception('{"code":42210000,"message":"order pending cancel"}')
        t.tc.cancel_order_by_id = cancel
        t._flatten("SPY", 501.0)
        self.assertEqual(t.tc.cancelled, ["tp", "sl"])     # second cancel raised, was tolerated
        self.assertEqual(t.tc.closed, ["SPY"])
        self.assertEqual(t.j.trades("paper")[0]["exit_reason"], "time")

    def test_closeout_one_error_does_not_strand_others(self):
        t = self._with_open_trade(make_trader())
        t.tc.get_clock = lambda: NS(is_open=True, next_close=datetime.now(ET) + timedelta(minutes=2))
        def boom(sym):
            raise Exception("insufficient qty")
        t.tc.close_position = boom
        t.closeout()                                      # must not raise
        self.assertEqual(len(t.j.open_trades("paper")), 1)

    def test_closeout_marks_previous_day_trade_stale(self):
        t = self._with_open_trade(make_trader())
        t.today = date(2026, 10, 16)
        t.tc.get_clock = lambda: NS(is_open=True, next_close=datetime.now(ET) + timedelta(minutes=2))
        t.closeout()
        self.assertEqual(t.j.trades("paper")[0]["exit_reason"], "stale_close")

    def test_unreported_close_fill_uses_quote_not_entry(self):
        """10/26 bug: NVDA's close fill came back after 10 s and P&L was logged as $0 (entry price)."""
        t = make_trader()
        t._handle(Signal("entry", trade()), datetime.now(ET), 500)
        orig = t.tc.get_order_by_id
        t.tc.get_order_by_id = lambda oid, filt=None: NS(filled_avg_price=None) if oid == "close-1" else orig(oid, filt)
        t.tc.get_open_position = lambda sym: NS(current_price="502.50")
        t._flatten("SPY", 500.0)
        self.assertEqual(t.j.trades("paper")[0]["exit"], 502.50)

    def test_refuses_real_money(self):
        cfg = config.load()
        cfg.paper, cfg.api_key, cfg.secret_key = False, "k", "s"
        with self.assertRaises(SystemExit):
            live.LiveTrader(cfg, Journal(":memory:"))


if __name__ == "__main__":
    unittest.main()

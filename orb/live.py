"""Live paper trading. Start it any time before 9:45 ET on a market day.

Every 15 seconds it pulls the latest 5-min candles for all 5 tickers, feeds the
new finished candles into the strategy engine, and when a setup triggers it
sends a BRACKET order to your Alpaca PAPER account:
    market entry + stop-loss order + take-profit order, all linked.
Alpaca manages the stop and target, so the trade is protected even if your
laptop dies mid-trade. At 15:55 anything still open gets closed.

With --no-broker it skips Alpaca orders and simulates fills from the candles
(same as the backtester, but on today's live data).

This bot refuses to run against a real-money account. On purpose.
"""
from __future__ import annotations

import time as _time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Config
from .data import fetch_bars
from .journal import Journal
from .risk import pnl, position_size, size_factor
from .sessions import levels_for, split_days
from . import news
from .daily import DailyGuard
from .strategy import Levels, ORBRetest, Signal, Trade
from .strategy_fvg import make_engine

ET = ZoneInfo("America/New_York")
MODE = "paper"
RUN_ID = "paper"          # all paper trades live under one run, split by date
POLL_SECONDS = 15
STALE_SECONDS = 120       # don't take an entry signal that's older than this (e.g. after a restart)


def say(msg: str) -> None:
    print(f"[{datetime.now(ET):%H:%M:%S}] {msg}", flush=True)


class LiveTrader:
    def __init__(self, cfg: Config, journal: Journal, use_broker: bool = True):
        cfg.require_keys()
        if not cfg.paper:
            raise SystemExit("paper = false in config.toml. This bot only trades paper. Set it back to true.")
        from alpaca.trading.client import TradingClient
        self.cfg, self.j, self.use_broker = cfg, journal, use_broker
        self.tc = TradingClient(cfg.api_key, cfg.secret_key, paper=True)
        self.engines: dict[str, ORBRetest] = {}
        self.fed_until: dict[str, datetime] = {}
        self.open: dict[str, dict] = {}       # symbol -> journal row of today's open trade
        self.today = datetime.now(ET).date()
        closed = sum(t["pnl"] for t in journal.trades(MODE, closed_only=True)
                     if t["session_date"] < self.today.isoformat())
        self.factor = size_factor(cfg.risk, closed)         # compound sizing: set once per day
        self.guard = DailyGuard(cfg.risk.daily_profit_target * self.factor,
                                cfg.risk.daily_loss_limit * self.factor)

    # ---- setup ---------------------------------------------------------
    def _wait_for_market(self) -> datetime:
        clock = self.tc.get_clock()
        nxt_open = clock.next_open.astimezone(ET)
        close = clock.next_close.astimezone(ET)
        if not clock.is_open and nxt_open.date() != self.today:
            raise SystemExit(f"Market is closed today. Next open: {nxt_open:%a %b %d %H:%M} ET")
        if close.date() != self.today:
            raise SystemExit("Market already closed for today.")
        if not clock.is_open:
            wait = (nxt_open - datetime.now(ET)).total_seconds()
            say(f"Waiting {wait/60:.0f} min for the open...")
            _time.sleep(max(wait, 0) + 5)
        return close

    def _prior_day_levels(self) -> dict[str, list]:
        now = datetime.now(timezone.utc)
        hist = fetch_bars(self.cfg, self.cfg.symbols, now - timedelta(days=7),
                          now - timedelta(minutes=20), self.cfg.backtest_feed, 5)
        prev = {}
        for sym, bars in hist.items():
            days = [(d, p["rth"]) for d, p in split_days(bars).items() if d < self.today and p["rth"]]
            prev[sym] = days[-1][1] if days else None
        return prev

    def _build_engines(self, today_bars: dict, prev_rth: dict, flat_time) -> None:
        params = replace(self.cfg.strategy, flat_time=min(self.cfg.strategy.flat_time, flat_time))
        table = news.load() if params.skip_news else {}
        for sym in self.cfg.symbols:
            parts = split_days(today_bars.get(sym, [])).get(self.today, {"pre": [], "rth": []})
            lv = levels_for(prev_rth.get(sym), parts["pre"])
            self.engines[sym] = make_engine(sym, self.today, lv, params)
            event = news.event_for(sym, self.today, table)
            if event:
                self.engines[sym].phase, self.engines[sym].outcome = ORBRetest.DONE, "news_day"
                self.engines[sym].log.append(event)
                say(f"{sym}: NOT trading today - {event}")
            self.fed_until[sym] = datetime.combine(self.today, datetime.min.time(), ET)
            say(f"{sym}: levels {lv.named() or 'none'}")
        for row in self.j.trades(MODE, closed_only=True):   # restart: count today's closed trades
            if row["session_date"] == self.today.isoformat():
                self.guard.add(row["pnl"])
        if self.guard.realized:
            say(f"Today's P&L so far: ${self.guard.realized:+.2f}")
        for row in self.j.open_trades(MODE):          # resume after a restart
            if row["session_date"] == self.today.isoformat():
                self.open[row["symbol"]] = row
                say(f"{row['symbol']}: resuming open trade #{row['id']}")

    # ---- orders --------------------------------------------------------
    def _enter(self, tr: Trade) -> None:
        qty = position_size(tr.entry, tr.initial_stop, self.cfg.risk.risk_per_trade * self.factor,
                            self.cfg.risk.max_position_value * self.factor)
        if qty == 0:
            say(f"{tr.symbol}: setup triggered but size = 0 shares, skipped")
            return
        order_id = None
        if self.use_broker:
            from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
            from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
            try:
                order = self.tc.submit_order(MarketOrderRequest(
                    symbol=tr.symbol, qty=qty,
                    side=OrderSide.BUY if tr.direction == "long" else OrderSide.SELL,
                    time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitRequest(limit_price=round(tr.target, 2)),
                    stop_loss=StopLossRequest(stop_price=round(tr.stop, 2))))
                order_id = str(order.id)
            except Exception as e:     # e.g. price already moved past the stop
                say(f"{tr.symbol}: Alpaca rejected the order: {e}")
                return
        tid = self.j.open_trade(RUN_ID, MODE, tr, qty, order_id)
        self.open[tr.symbol] = {"id": tid, "qty": qty, "direction": tr.direction,
                                "entry": tr.entry, "stop": tr.initial_stop, "broker_order_id": order_id,
                                "stop_sent": round(tr.stop, 2)}
        say(f"{tr.symbol}: ENTERED {tr.direction} {qty} sh @ ~{tr.entry:.2f} "
            f"stop {tr.stop:.2f} target {tr.target:.2f} ({tr.target_label})")

    def _record_exit(self, sym: str, when: datetime, price: float, reason: str) -> None:
        row = self.open.pop(sym)
        p = pnl(row["direction"], row["entry"], price, row["qty"],
                0 if self.use_broker else self.cfg.risk.slippage_per_share)
        self.j.close_trade(row["id"], when, price, reason, p)
        self.guard.add(p)
        self.engines[sym].phase = ORBRetest.DONE
        say(f"{sym}: EXIT {reason} @ {price:.2f}  P&L ${p:+.2f}")

    def _check_broker(self, sym: str) -> None:
        """Alpaca owns the stop/target. Ask it what happened."""
        from alpaca.trading.requests import GetOrderByIdRequest
        row = self.open[sym]
        o = self.tc.get_order_by_id(row["broker_order_id"], GetOrderByIdRequest(nested=True))
        if o.filled_avg_price and float(o.filled_avg_price) != row["entry"]:
            row["entry"] = float(o.filled_avg_price)          # use the real fill price
            self.j.update_entry(row["id"], row["entry"])
        for leg in o.legs or []:
            if str(leg.status.value) == "filled":
                reason = "target" if "limit" in str(leg.type.value) else "stop"
                self._record_exit(sym, leg.filled_at.astimezone(ET), float(leg.filled_avg_price), reason)
                return

    def _unrealized(self) -> float:
        if not self.open or not self.use_broker:
            return 0.0
        total = 0.0
        for pos in self.tc.get_all_positions():
            if pos.symbol in self.open:
                total += float(pos.unrealized_pl)
        return total

    def _apply_daily_limits(self, today_bars: dict) -> None:
        was = self.guard.halted
        why = self.guard.check(self._unrealized())
        if not why:
            return
        if not was:
            say(self.guard.describe() + f" (closed P&L ${self.guard.realized:+.2f})")
        for sym, eng in self.engines.items():
            if why == "profit_target" and sym in self.open:
                bars = today_bars.get(sym) or []
                self._flatten(sym, bars[-1].close if bars else self.open[sym]["entry"], "daily_target")
            if sym not in self.open and eng.phase not in (ORBRetest.DONE,):
                eng.phase = ORBRetest.DONE
                if eng.outcome not in ("traded", "news_day"):
                    eng.outcome = "daily_limit"
                eng.log.append(self.guard.describe())

    def _sync_stop(self, sym: str, stop: float) -> None:
        """Breakeven rule moved the stop: move Alpaca's stop order to match."""
        row = self.open[sym]
        if round(stop, 2) == row.get("stop_sent", round(stop, 2)):
            return
        from alpaca.trading.requests import GetOrderByIdRequest, ReplaceOrderRequest
        o = self.tc.get_order_by_id(row["broker_order_id"], GetOrderByIdRequest(nested=True))
        for leg in o.legs or []:
            if "stop" in str(leg.type.value) and str(leg.status.value) in ("new", "accepted", "held"):
                self.tc.replace_order_by_id(leg.id, ReplaceOrderRequest(stop_price=round(stop, 2)))
                row["stop_sent"] = round(stop, 2)
                say(f"{sym}: stop moved to {stop:.2f} (breakeven)")

    def _flatten(self, sym: str, last_close: float, reason: str = "time") -> None:
        if not self.use_broker:
            self._record_exit(sym, datetime.now(ET), last_close, reason)
            return
        from alpaca.trading.requests import GetOrderByIdRequest
        row = self.open[sym]
        o = self.tc.get_order_by_id(row["broker_order_id"], GetOrderByIdRequest(nested=True))
        live_states = ("new", "accepted", "held", "partially_filled", "pending_cancel", "pending_replace")
        for leg in o.legs or []:
            if str(leg.status.value) in live_states[:4]:
                try:
                    self.tc.cancel_order_by_id(leg.id)
                except Exception as e:                  # cancelling one bracket leg auto-cancels the other
                    say(f"{sym}: cancel of {leg.type.value} leg skipped ({e})")
        for _ in range(15):                             # wait until no leg still holds the shares
            _time.sleep(1)
            o = self.tc.get_order_by_id(row["broker_order_id"], GetOrderByIdRequest(nested=True))
            legs = o.legs or []
            if any(str(l.status.value) == "filled" for l in legs):
                self._check_broker(sym)                 # a stop/target filled while cancelling
                if sym not in self.open:
                    return
            if not any(str(l.status.value) in live_states for l in legs):
                break
        close_order = None
        for attempt in range(3):
            try:
                close_order = self.tc.close_position(sym)
                break
            except Exception as e:
                say(f"{sym}: close attempt {attempt + 1} failed ({e})")
                _time.sleep(3)
        if close_order is None:
            say(f"{sym}: COULD NOT CLOSE - check Alpaca; left open in the journal so the next run retries")
            return
        price = last_close
        for _ in range(10):
            _time.sleep(1)
            c = self.tc.get_order_by_id(close_order.id)
            if c.filled_avg_price:
                price = float(c.filled_avg_price)
                break
        self._record_exit(sym, datetime.now(ET), price, reason)

    # ---- signals -------------------------------------------------------
    def _handle(self, sig: Signal, bar_close_time: datetime, last_close: float) -> None:
        sym = sig.trade.symbol
        if sig.kind == "entry":
            if sym in self.open:                                    # already have it (restart)
                return
            cap = self.cfg.risk.max_open_trades
            if cap and len(self.open) >= cap:
                say(f"{sym}: setup triggered but a trade is already open ({', '.join(self.open)}), skipped")
                self.engines[sym].phase, self.engines[sym].outcome = ORBRetest.DONE, "one_at_a_time"
                return
            tr = sig.trade
            qty = position_size(tr.entry, tr.initial_stop, self.cfg.risk.risk_per_trade * self.factor,
                                self.cfg.risk.max_position_value * self.factor)
            open_risk = sum(abs(r["entry"] - r.get("stop", r["entry"])) * r["qty"] for r in self.open.values())
            if not self.guard.allows(abs(tr.entry - tr.initial_stop) * qty + open_risk):
                say(f"{sym}: setup triggered but {self.guard.describe() or 'daily loss limit would be exceeded'}, skipped")
                self.engines[sym].phase, self.engines[sym].outcome = ORBRetest.DONE, "daily_limit"
                return
            age = (datetime.now(ET) - bar_close_time).total_seconds()
            if age > STALE_SECONDS:
                say(f"{sym}: entry signal from {bar_close_time:%H:%M} is stale (bot was offline), skipped")
                self.engines[sym].phase = ORBRetest.DONE
                self.engines[sym].outcome = "missed"
                return
            self._enter(sig.trade)
        elif sig.kind == "exit" and sym in self.open:
            if not self.use_broker:
                tr = sig.trade
                self._record_exit(sym, tr.exit_time, tr.exit, tr.exit_reason)
            elif sig.trade.exit_reason in ("time", "eod"):
                self._flatten(sym, last_close)
            # stop/target in broker mode: Alpaca's fill is the truth, see _check_broker

    # ---- main loop -----------------------------------------------------
    def _reconcile_old(self) -> None:
        """Trades left open from an earlier day (e.g. the close-out run didn't happen): close them now."""
        for row in self.j.open_trades(MODE):
            if row["session_date"] >= self.today.isoformat():
                continue
            sym = row["symbol"]
            say(f"{sym}: trade from {row['session_date']} still open, closing it now")
            self.open[sym] = row
            self.engines.setdefault(sym, ORBRetest(sym, self.today, Levels()))
            if self.use_broker:
                self._check_broker(sym)
            if sym in self.open:
                try:
                    self._flatten(sym, row["entry"], "stale_close")
                except Exception as e:
                    say(f"{sym}: stale close error ({e}); will retry")
            self.engines.pop(sym, None)

    def closeout(self) -> None:
        """Afternoon run for the cloud setup: wait until 15:55 ET, record stop/target fills Alpaca
        made since the morning run ended, close anything still open, done."""
        clock = self.tc.get_clock()
        rows = self.j.open_trades(MODE)
        if not rows:
            say("Close-out: no open trades.")
            return
        for row in rows:
            self.open[row["symbol"]] = row
            self.engines[row["symbol"]] = ORBRetest(row["symbol"], self.today, Levels())
        for sym in list(self.open):
            self._check_broker(sym)                 # stop or target already hit?
        if not self.open:
            say("Close-out: every trade already hit its stop or target.")
            return
        if not clock.is_open:
            say("Close-out: market is closed; open trades will be closed at the next morning run.")
            return
        flat_at = clock.next_close.astimezone(ET) - timedelta(minutes=5)
        wait = (flat_at - datetime.now(ET)).total_seconds()
        if wait > 0:
            say(f"Close-out: waiting {wait/60:.0f} min until {flat_at:%H:%M} ET")
            while datetime.now(ET) < flat_at:      # keep recording fills while we wait
                for sym in list(self.open):
                    self._check_broker(sym)
                if not self.open:
                    say("Close-out: all trades finished before the flat time.")
                    return
                _time.sleep(min(30, max((flat_at - datetime.now(ET)).total_seconds(), 1)))
        for sym in list(self.open):
            try:
                self._flatten(sym, self.open[sym]["entry"], "time")
            except Exception as e:                      # one ticker's error must not strand the others
                say(f"{sym}: close-out error ({e}); next run retries")
        say("Close-out done.")

    def run(self, until=None) -> None:
        say(f"Risk per trade today: ${self.cfg.risk.risk_per_trade * self.factor:,.2f}"
            + (f" (compound sizing, factor {self.factor:.2f})" if self.cfg.risk.sizing == "compound" else ""))
        say(f"ORB bot ({self.cfg.strategy.name}, {self.cfg.strategy.bar_minutes}-min) | paper | broker orders: {'ON' if self.use_broker else 'OFF (simulated)'}")
        close = self._wait_for_market()
        flat = (close - timedelta(minutes=5)).time()
        self._reconcile_old()
        end = close + timedelta(minutes=1)
        if until is not None:
            end = min(end, datetime.combine(self.today, until, ET))
            say(f"Morning run: taking entries until {end:%H:%M} ET; Alpaca holds stops/targets after that")
        prev_rth = self._prior_day_levels()
        start = datetime.combine(self.today, datetime.min.time(), ET).replace(hour=4)
        mins = self.cfg.strategy.bar_minutes
        today_bars = fetch_bars(self.cfg, self.cfg.symbols, start, datetime.now(ET), self.cfg.live_feed, mins)
        self._build_engines(today_bars, prev_rth, flat)

        bar_len = timedelta(minutes=self.cfg.strategy.bar_minutes)
        while datetime.now(ET) < end:
            now = datetime.now(ET)
            try:
                today_bars = fetch_bars(self.cfg, self.cfg.symbols, start, now, self.cfg.live_feed, mins)
                for sym in self.cfg.symbols:
                    eng = self.engines[sym]
                    done = [b for b in today_bars.get(sym, [])
                            if b.ts > self.fed_until[sym] and b.ts + bar_len <= now - timedelta(seconds=5)]
                    for b in done:
                        self.fed_until[sym] = b.ts
                        for sig in eng.on_bar(b):
                            self._handle(sig, b.ts + bar_len, b.close)
                    if self.use_broker and sym in self.open:
                        self._check_broker(sym)
                    if self.use_broker and sym in self.open and eng.trade:
                        self._sync_stop(sym, eng.trade.stop)
                    if sym in self.open and now.time() >= eng.p.flat_time:   # 15:55 hard flat
                        bars = today_bars.get(sym) or []
                        self._flatten(sym, bars[-1].close if bars else self.open[sym]["entry"])
                self._apply_daily_limits(today_bars)
            except Exception as e:      # network blip etc: log it and keep going
                say(f"error: {e}")
            _time.sleep(POLL_SECONDS)

        early_stop = end < close                         # cloud morning run: leave brackets working
        for sym, eng in self.engines.items():          # safety net, then save the day
            if sym in self.open and early_stop:
                if self.use_broker:
                    self._check_broker(sym)
                if sym in self.open:
                    say(f"{sym}: still open, Alpaca's stop/target protect it; close-out run handles 15:55")
            elif sym in self.open:
                bars = today_bars.get(sym) or []
                self._flatten(sym, bars[-1].close if bars else self.open[sym]["entry"])
            self.j.save_session(RUN_ID, MODE, sym, self.today, eng.outcome, eng.or_high,
                                eng.or_low, eng.log)
            say(f"{sym}: {eng.outcome}")
        say("Done for today.")

"""RSI(2) Pullback bot (Larry Connors & Cesar Alvarez, 'Short Term Trading Strategies That Work', 2008).

A second, separate paper bot. Swing trades held ~3 days, decided once a day near the close:

    ENTRY  today's price > its 200-day average AND 2-day RSI < 5  -> buy near the close
    EXIT   first close above the 5-day average, or after 10 trading days
    (shorts are off: in the 2023-26 test the short side broke even; long side carried it)

There is no hard stop in the original. For sizing and R the bot uses 2 x ATR(14) as the
"risk unit": 1% of the bot's own $10,000 account per trade.

It is journal-only paper trading: fills are recorded at the live price with $0.01/share
slippage and NO orders are sent to Alpaca, because the ORB bot trades the same tickers on the
same Alpaca account and their positions would collide.

Strategy-lab results (scripts/strategy_lab.py, long+short, rules fixed before testing):
    Oct 2023-Oct 2025: 73 trades, 68.5% winners, +0.14R/trade
    Oct 2025-Oct 2026: 50 trades, 86.0% winners, +0.43R/trade   (strong bull market: flattering)
"""
from __future__ import annotations

import math
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .panel import Daily
from .risk import pnl
from .strategy import Trade

ET = ZoneInfo("America/New_York")
MODE, RUN_ID = "paper_rsi2", "rsi2"
DECIDE_AT = time(15, 50)
SLIP = 0.01


@dataclass
class Settings:
    rsi_entry: float = 5.0
    rsi_short: float = 95.0
    shorts: bool = False
    max_hold: int = 10
    starting_equity: float = 10_000.0
    risk_pct: float = 1.0
    max_position_value: float = 25_000.0


def rsi(closes: list[float], n: int = 2) -> float | None:
    if len(closes) < n + 1:
        return None
    g = l = 0.0
    for a, b in zip(closes[:n], closes[1:n + 1]):
        g += max(b - a, 0); l += max(a - b, 0)
    ag, al = g / n, l / n
    for a, b in zip(closes[n:], closes[n + 1:]):
        ag = (ag * (n - 1) + max(b - a, 0)) / n
        al = (al * (n - 1) + max(a - b, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr(days: list[Daily], n: int = 14) -> float:
    trs = [max(d.high - d.low, abs(d.high - p.close), abs(d.low - p.close)) for p, d in zip(days[-n - 1:-1], days[-n:])]
    return sum(trs) / len(trs)


def entry_signal(days: list[Daily], price: float, s: Settings) -> tuple[str | None, str]:
    """days: completed daily bars (oldest first); price: today's price near the close."""
    if len(days) < 199:
        return None, f"only {len(days)} days of history (needs 200)"
    closes = [d.close for d in days[-199:]] + [price]
    sma200 = sum(closes) / 200
    r2 = rsi(closes[-30:], 2)
    info = f"price {price:.2f}, 200-day avg {sma200:.2f}, RSI(2) {r2:.1f}"
    if price > sma200 and r2 < s.rsi_entry:
        return "long", info
    if s.shorts and price < sma200 and r2 > s.rsi_short:
        return "short", info
    return None, info


def exit_signal(days: list[Daily], price: float, direction: str, held: int, s: Settings) -> tuple[str | None, str]:
    sma5 = (sum(d.close for d in days[-4:]) + price) / 5
    info = f"price {price:.2f}, 5-day avg {sma5:.2f}, held {held} day(s)"
    if (direction == "long" and price > sma5) or (direction == "short" and price < sma5):
        return "sma5", info
    if held >= s.max_hold:
        return "time", info
    return None, info


class AlpacaData:
    """Daily history (SIP, completed days) + latest trade price (IEX) + market clock."""
    def __init__(self, cfg):
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient
        self.cfg = cfg
        self.data = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
        self.tc = TradingClient(cfg.api_key, cfg.secret_key, paper=True)

    def clock(self):
        return self.tc.get_clock()

    def daily(self, symbols: list[str], today: date) -> dict[str, list[Daily]]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        start = datetime.combine(today - timedelta(days=330), time(0), ET)
        end = datetime.combine(today, time(0), ET)
        raw = self.data.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start.astimezone(timezone.utc),
            end=end.astimezone(timezone.utc), feed=DataFeed(self.cfg.backtest_feed),
            adjustment=Adjustment.SPLIT)).data
        out = {}
        for sym in symbols:
            rows = raw.get(sym, [])
            out[sym] = [Daily(r.timestamp.astimezone(ET).date(), float(r.open), float(r.high), float(r.low),
                              float(r.close)) for r in rows if r.timestamp.astimezone(ET).date() < today]
        return out

    def latest(self, symbols: list[str]) -> dict[str, tuple[float, datetime]]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest
        res = self.data.get_stock_latest_trade(StockLatestTradeRequest(
            symbol_or_symbols=symbols, feed=DataFeed(self.cfg.live_feed)))
        return {s: (float(res[s].price), res[s].timestamp) for s in symbols if s in res}


def _trading_days_held(days: list[Daily], entered: str) -> int:
    return sum(1 for d in days if d.day.isoformat() > entered) + 1     # +1 = today


def run(cfg, journal, data=None, s: Settings | None = None, say=print, wait: bool = True) -> None:
    s = s or Settings()
    data = data or AlpacaData(cfg)
    now = datetime.now(ET)
    clock = data.clock()
    decide = datetime.combine(now.date(), DECIDE_AT, ET)
    if wait and clock.is_open and now < decide:
        say(f"RSI(2): waiting {(decide - now).total_seconds() / 60:.0f} min until {DECIDE_AT:%H:%M} ET to decide")
        while datetime.now(ET) < decide:
            _time.sleep(min(60, max((decide - datetime.now(ET)).total_seconds(), 1)))
    today = datetime.now(ET).date()
    syms = cfg.symbols
    daily = data.daily(syms, today)
    quotes = data.latest(syms)                    # {sym: (price, time of that trade)}
    price = {k: v[0] for k, v in quotes.items() if v[1].astimezone(ET).date() == today}
    if not price:
        say("RSI(2): no trading session today (weekend/holiday), nothing to do.")
        return
    closed = sum(t["pnl"] for t in journal.trades(MODE, closed_only=True))
    equity = s.starting_equity + closed
    say(f"RSI(2) bot | account ${equity:,.2f} | risk {s.risk_pct:g}% per trade (2 x ATR risk unit) | journal-only paper")

    open_rows = {r["symbol"]: r for r in journal.open_trades(MODE)}
    t0 = today.isoformat()
    done_today = {r["symbol"] for r in journal.trades(MODE, closed_only=False)
                  if r["session_date"] == t0 or (r["exit_time"] or "")[:10] == t0}
    for sym in syms:
        if sym in done_today and not (sym in open_rows and open_rows[sym]["session_date"] < t0):
            say(f"RSI(2) {sym}: already handled today")
            continue
        if sym not in price or len(daily.get(sym, [])) < 20:
            say(f"RSI(2) {sym}: no data, skipped")
            continue
        px, days = price[sym], daily[sym]
        if sym in open_rows:
            row = open_rows[sym]
            held = _trading_days_held(days, row["session_date"])
            why, info = exit_signal(days, px, row["direction"], held, s)
            if why:
                fill = px - SLIP if row["direction"] == "long" else px + SLIP
                p = pnl(row["direction"], row["entry"], fill, row["qty"], 0)
                journal.close_trade(row["id"], datetime.now(ET), fill, why, p)
                say(f"RSI(2) {sym}: EXIT {row['direction']} @ {fill:.2f} ({why}: {info})  P&L ${p:+.2f}")
            else:
                say(f"RSI(2) {sym}: holding {row['direction']} from {row['entry']:.2f} ({info})")
            continue
        side, info = entry_signal(days, px, s)
        if not side:
            say(f"RSI(2) {sym}: no setup ({info})")
            continue
        unit = 2 * atr(days)
        risk = equity * s.risk_pct / 100
        qty = int(min(risk / unit, s.max_position_value / px)) if unit > 0 else 0
        if qty <= 0:
            say(f"RSI(2) {sym}: setup but size = 0, skipped")
            continue
        fill = px + SLIP if side == "long" else px - SLIP
        sma5 = (sum(d.close for d in days[-4:]) + px) / 5
        tr = Trade(symbol=sym, session_date=today, direction=side, or_high=None, or_low=None,
                   breakout_time=datetime.now(ET), entry_time=datetime.now(ET), entry=fill,
                   stop=fill - unit if side == "long" else fill + unit, target=sma5,
                   target_label="exit: close above 5-day avg (stop = 2xATR risk unit, not an order)")
        journal.open_trade(RUN_ID, MODE, tr, qty)
        say(f"RSI(2) {sym}: ENTERED {side} {qty} sh @ {fill:.2f} ({info}); risk unit ${unit:.2f}/sh")

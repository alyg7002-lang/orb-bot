"""Prior-day high/low paper bots (journal-only), forward-testing the strategies picked by the lab.

Which bots run is set in config.toml under [pdhl] bots = [...] (keys from orb/pdhl.py).

How a "trade" happens: every morning before the open (and again at the close-out, as a catch-up)
the bot takes each finished session it hasn't journaled yet, starting 11/03/2026, and runs the
EXACT backtest code (orb/pdhl.py run_day) on that session's full SIP 1-min bars. Any trade the
rules produce goes in data/journal.db, mode "paper_<key>".

Why replay instead of watching live: these bots send no orders (the ORB bot already trades these
tickers on the same Alpaca account, so positions would collide), and replaying the finished day
with full SIP data gives fills that are bar-for-bar identical to the backtest. The rules were
frozen before the first session they trade, so this is still a true out-of-sample test. The only
cost: a trade shows up on the dashboard the morning after it happens.

Sizing: each bot has its own $10,000 account and risks 1% of (10,000 + its closed P&L) per trade,
capped at $25,000 of stock. $0.01/share slippage on entry and exit.
"""
from __future__ import annotations

import tomllib
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import news, panel, pdhl
from .config import ROOT
from .risk import pnl, position_size
from .sessions import split_days
from .strategy import Trade

ET = ZoneInfo("America/New_York")
START = date(2026, 11, 3)          # first session these bots trade (rules frozen that morning)
STARTING_EQUITY, RISK_PCT, MAX_POSITION = 10_000.0, 1.0, 25_000.0


def mode_for(key: str) -> str:
    return f"paper_{key}"


def active_bots() -> list[str]:
    raw = tomllib.loads((ROOT / "config.toml").read_text())
    return [k for k in raw.get("pdhl", {}).get("bots", []) if k in pdhl.STRATEGIES]


class AlpacaBars:
    def __init__(self, cfg):
        self.cfg = cfg

    def minute_bars(self, symbols, start_day: date, end_day: date):
        from .data import fetch_bars
        start = datetime.combine(start_day, time(4, 0), ET)
        end = min(datetime.combine(end_day, time(0), ET), datetime.now(timezone.utc) - timedelta(minutes=16))
        return fetch_bars(self.cfg, symbols, start, end, self.cfg.backtest_feed, 1)


def run(cfg, journal, keys: list[str] | None = None, data=None, today: date | None = None, say=print,
        start: date = START) -> int:
    """Journal every finished, not-yet-handled session since `start` (START for real). Returns trades added."""
    keys = keys if keys is not None else active_bots()
    if not keys:
        say("Prior-day bots: none switched on in config.toml [pdhl] bots")
        return 0
    today = today or datetime.now(ET).date()
    if today <= start:
        say(f"Prior-day bots: first session to trade is {start}; nothing finished yet")
        return 0
    done = {k: {(r["symbol"], r["session_date"]) for r in journal.sessions(mode_for(k))} for k in keys}
    data = data or AlpacaBars(cfg)
    bars = data.minute_bars(cfg.symbols, start - timedelta(days=60), today)
    news_tbl = news.load()
    per_sym = {}
    for sym in cfg.symbols:
        days = {d: p["rth"] for d, p in split_days(bars.get(sym, [])).items() if p["rth"]}
        per_sym[sym] = (days, panel.daily_bars(days))
    new_days = sorted({d.day for _, dl in per_sym.values() for d in dl if start <= d.day < today})
    if not new_days:
        say("Prior-day bots: no finished sessions to journal yet")
        return 0
    added = 0
    for key in keys:
        mode, name = mode_for(key), pdhl.STRATEGIES[key][0]
        equity = STARTING_EQUITY + sum(t["pnl"] for t in journal.trades(mode, closed_only=True))
        for day in new_days:
            for sym in cfg.symbols:
                if (sym, day.isoformat()) in done[key]:
                    continue
                days, daily = per_sym[sym]
                idx = next((i for i, d in enumerate(daily) if d.day == day), None)
                if idx is None:
                    continue
                rth = days[day]
                if rth[-1].ts.time() < time(15, 45):
                    outcome, msg = "half_day", "short session, skipped"
                elif news.event_for(sym, day, news_tbl):
                    outcome, msg = "news", f"news day ({news.event_for(sym, day, news_tbl)}), skipped"
                else:
                    t = pdhl.run_day(key, sym, day, rth, daily[max(0, idx - 30):idx])
                    if t is None:
                        outcome, msg = "no_setup", "no setup"
                    else:
                        risk = equity * RISK_PCT / 100
                        qty = position_size(t.entry, t.stop, risk, MAX_POSITION)
                        if qty <= 0:
                            outcome, msg = "size_zero", "setup but size = 0"
                        else:
                            tr = Trade(symbol=sym, session_date=day, direction=t.side, or_high=None, or_low=None,
                                       breakout_time=t.entry_time, entry_time=t.entry_time, entry=t.entry,
                                       stop=t.stop, target=t.target, target_label=f"{name} (journal-only paper)")
                            tid = journal.open_trade(key, mode, tr, qty)
                            p = pnl(t.side, t.entry, t.exit, qty, pdhl.SLIP)
                            journal.close_trade(tid, t.exit_time, t.exit, t.exit_reason, p, r=t.r)
                            equity += p
                            added += 1
                            outcome = "traded"
                            msg = (f"{t.side} {qty} sh @ {t.entry:.2f} {t.entry_time:%H:%M}, stop {t.stop:.2f}, "
                                   f"exit {t.exit:.2f} {t.exit_time:%H:%M} ({t.exit_reason}) P&L ${p:+.2f}")
                journal.save_session(key, mode, sym, day, outcome, None, None, [msg])
                say(f"{name} {sym} {day}: {msg}")
        say(f"{name}: account ${equity:,.2f}")
    return added

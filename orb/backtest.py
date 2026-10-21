"""Replays history through the exact same strategy engine the live bot uses.

Two passes:
  1. Run every ticker through the rules for each day.
  2. Walk each day's trades in time order across all tickers, applying the daily
     profit target / loss limit (orb/daily.py) the way the live bot would.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, time

from . import news
from .config import Config
from .daily import DailyGuard
from .journal import Journal
from .risk import pnl, position_size, size_factor
from collections import deque

from .sessions import levels_for, opening_volume, split_days
from .strategy import Bar
from .strategy_fvg import make_engine

FULL_DAY_LAST_BAR = time(15, 45)   # sessions ending earlier are half days (holidays): skipped


def run_backtest(bars_by_symbol: dict[str, list[Bar]], cfg: Config, journal: Journal,
                 mode: str = "backtest", label: str = "", day_stats: dict | None = None,
                 first_day=None) -> str:
    p = cfg.strategy
    tag = label or f"{p.name}-{p.fvg_mode if p.name == 'orb_fvg' else p.bar_minutes}m-{p.stop_mode}"
    run_id = f"{mode}-{tag}-{datetime.now():%Y%m%d-%H%M%S}"
    settings = {"strategy": asdict(p), "risk": asdict(cfg.risk)}
    journal.start_run(run_id, mode, list(bars_by_symbol), settings)
    news_table = news.load() if p.skip_news else {}

    # ---- pass 1: rules per ticker per day ------------------------------
    by_day: dict = defaultdict(dict)
    for symbol, bars in bars_by_symbol.items():
        prev_rth = None
        recent_or_vol: deque = deque(maxlen=p.rvol_days)
        for day, parts in split_days(bars).items():
            rth = parts["rth"]
            avg_vol = (sum(recent_or_vol) / len(recent_or_vol)) if len(recent_or_vol) >= 5 else None
            if rth and rth[-1].ts.time() >= FULL_DAY_LAST_BAR:
                recent_or_vol.append(opening_volume(rth, p.range_minutes))
            if first_day and day < first_day:            # warm-up days: only used for prior-day levels
                prev_rth = rth or prev_rth
                continue
            if not rth or rth[-1].ts.time() < FULL_DAY_LAST_BAR:
                prev_rth = rth or prev_rth
                continue
            event = news.event_for(symbol, day, news_table)
            if event:
                by_day[day][symbol] = {"outcome": "news_day", "log": [event], "engine": None}
                prev_rth = rth
                continue
            lv = levels_for(prev_rth, parts["pre"])
            lv.or_volume_avg = avg_vol
            engine = make_engine(symbol, day, lv, p)
            for b in rth:
                engine.on_bar(b)
            engine.finish(rth[-1])
            by_day[day][symbol] = {"outcome": engine.outcome, "log": engine.log,
                                   "engine": engine, "rth": rth}
            prev_rth = rth

    # ---- pass 2: walk each day minute by minute across tickers -----------
    # Applies: max trades open at once, daily loss limit (closed trades), and the
    # daily profit target (closed + open profit, checked every minute, like live).
    max_open = cfg.risk.max_open_trades
    closed_profit = 0.0                               # running total, drives compound sizing
    for day in sorted(by_day):
        f = size_factor(cfg.risk, closed_profit)
        guard = DailyGuard(cfg.risk.daily_profit_target * f, cfg.risk.daily_loss_limit * f)
        trades = []
        for symbol, info in by_day[day].items():
            e = info["engine"]
            if e is not None and e.trade is not None and e.trade.exit is not None:
                tr = e.trade
                qty = position_size(tr.entry, tr.initial_stop, cfg.risk.risk_per_trade * f,
                                    cfg.risk.max_position_value * f)
                if qty > 0:
                    closes = {b.ts: b.close for b in info["rth"]}
                    trades.append({"symbol": symbol, "tr": tr, "qty": qty, "closes": closes})
        if not trades:
            _save_sessions(journal, run_id, mode, day, by_day[day])
            continue
        minutes = sorted({ts for t in trades for ts in t["closes"]}
                         | {t["tr"].entry_time for t in trades} | {t["tr"].exit_time for t in trades})
        open_now: list[dict] = []
        last_close: dict = {}
        peak = 0.0
        for ts in minutes:
            # entries fill at the start of this minute (the rejection candle's close)
            for t in trades:
                if t["tr"].entry_time != ts or t.get("state"):
                    continue
                risk_usd = abs(t["tr"].entry - t["tr"].initial_stop) * t["qty"]
                if max_open and len(open_now) >= max_open and not guard.halted:
                    blocked = "one_at_a_time"
                elif not guard.allows(risk_usd + sum(abs(o["tr"].entry - o["tr"].initial_stop) * o["qty"]
                                                     for o in open_now)):
                    blocked = "daily_limit"
                else:
                    blocked = None
                if blocked:
                    t["state"] = "skipped"
                    by_day[day][t["symbol"]]["outcome"] = blocked
                    by_day[day][t["symbol"]]["log"].append(
                        guard.describe() if blocked == "daily_limit" else "Skipped: another trade already open")
                    continue
                t["state"] = "open"
                open_now.append(t)
            # exits that happen during this minute (stop / target / 15:55)
            for t in list(open_now):
                if t["tr"].exit_time <= ts:
                    _close(t, t["tr"].exit, t["tr"].exit_reason, t["tr"].exit_time, cfg, guard, open_now)
            # mark open trades to this minute's close, then check the daily limits
            unreal = 0.0
            for t in open_now:
                px = t["closes"].get(ts, last_close.get(t["symbol"], t["tr"].entry))
                last_close[t["symbol"]] = px
                unreal += pnl(t["tr"].direction, t["tr"].entry, px, t["qty"], 0)
            peak = max(peak, guard.realized + unreal)
            if guard.check(unreal) == "profit_target":
                for t in list(open_now):
                    _close(t, last_close[t["symbol"]], "daily_target", ts, cfg, guard, open_now)

        closed_profit += guard.realized
        if day_stats is not None:
            day_stats[str(day)] = {"peak": peak, "final": guard.realized, "halted": guard.halted,
                                   "risk": cfg.risk.risk_per_trade * f, "equity_end": closed_profit}
        for t in trades:
            if t.get("state") != "closed":
                continue
            tid = journal.open_trade(run_id, mode, t["tr"], t["qty"])
            journal.close_trade(tid, t["exit_time"], t["exit"], t["reason"], t["pnl"])
        _save_sessions(journal, run_id, mode, day, by_day[day])
    return run_id


def _close(t: dict, price: float, reason: str, when, cfg: Config, guard: DailyGuard,
           open_now: list) -> None:
    t.update(state="closed", exit=price, reason=reason, exit_time=when,
             pnl=pnl(t["tr"].direction, t["tr"].entry, price, t["qty"], cfg.risk.slippage_per_share))
    guard.add(t["pnl"])
    open_now.remove(t)


def _save_sessions(journal: Journal, run_id: str, mode: str, day, infos: dict) -> None:
    for symbol, info in infos.items():
        e = info["engine"]
        journal.save_session(run_id, mode, symbol, day, info["outcome"],
                             e.or_high if e else None, e.or_low if e else None, info["log"])

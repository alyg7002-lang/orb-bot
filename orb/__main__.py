"""Command line.

    python -m orb demo                  fake data, no account needed (tests the plumbing)
    python -m orb backtest              real history from Alpaca (config.toml -> backtest_days)
    python -m orb backtest --days 90 --symbols SPY QQQ
    python -m orb backtest --strategy orb_retest       # the original 5-min rules, for comparison
    python -m orb backtest --fvg-mode any --stop-mode mid --no-news   # try one change at a time
    (1-min history is cached in data/cache after the first download, so reruns are fast)
    python -m orb live                  paper trade today on your Alpaca paper account
    python -m orb live --no-broker      watch live data, simulate fills, send no orders
    python -m orb report                rebuild reports/dashboard.html
    python -m orb export                trades -> reports/trade_log.csv (for your written log)
"""
from __future__ import annotations

import argparse
import csv
import webbrowser
from dataclasses import replace

from . import config as config_mod
from .config import ROOT
from .journal import Journal
from .report import build
from .stats import summarize


def _print_summary(j: Journal, mode: str, run_id: str | None) -> None:
    m = summarize(j.trades(mode, run_id))
    if not m["trades"]:
        print("No closed trades.")
        return
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    print(f"\n{m['trades']} trades | win rate {m['win_rate']:.1%} | loss rate {m['loss_rate']:.1%}")
    print(f"P&L ${m['total_pnl']:,.2f} | expectancy {m['expectancy_r']:+.2f}R | profit factor {pf} "
          f"| max drawdown ${m['max_drawdown']:,.2f}")


def _open_report(j: Journal, show: bool) -> None:
    path = build(j)
    print(f"Dashboard: {path}")
    if show:
        webbrowser.open(path.as_uri())


def _override(p, args):
    from .config import validate
    changes = {}
    if args.strategy:
        changes["name"] = args.strategy
        changes["bar_minutes"] = 1 if args.strategy == "orb_fvg" else 5
        if args.strategy == "orb_retest" and p.stop_mode == "fvg":
            changes["stop_mode"] = "mid"
    if args.fvg_mode:
        changes["fvg_mode"] = args.fvg_mode
    if args.stop_mode:
        changes["stop_mode"] = args.stop_mode
    if args.no_news:
        changes["skip_news"] = False
    if args.target:
        if args.target == "levels":
            changes["target_mode"] = "levels"
        else:
            changes["target_mode"], changes["target_r"] = "fixed", float(args.target)
    if args.gap_filter:
        changes["gap_filter"] = args.gap_filter == "on"
    if args.skip_window:
        from .config import _t
        if args.skip_window == "off":
            changes["skip_start"] = changes["skip_end"] = None
        else:
            a_, b_ = args.skip_window.split("-")
            changes["skip_start"], changes["skip_end"] = _t(a_), _t(b_)
    if args.confirm is not None:
        changes["confirm_bars"] = args.confirm
    if args.breakeven is not None:
        changes["breakeven_r"] = args.breakeven
    p = replace(p, **changes)
    validate(p)
    return p


def main() -> None:
    ap = argparse.ArgumentParser(prog="orb", description="ORB retest paper-trading bot")
    ap.add_argument("--config", help="path to a config.toml (default: the one in the project)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo"); d.add_argument("--days", type=int, default=120)
    b = sub.add_parser("backtest"); b.add_argument("--days", type=int); b.add_argument("--symbols", nargs="+")
    b.add_argument("--strategy", choices=["orb_fvg", "orb_retest"])
    b.add_argument("--fvg-mode", choices=["edge", "any"])
    b.add_argument("--stop-mode", choices=["fvg", "mid", "opposite"])
    b.add_argument("--no-news", action="store_true", help="trade news days too")
    b.add_argument("--breakeven", type=float, help="move stop to entry at this many R (0 = off)")
    b.add_argument("--gap-filter", choices=["on", "off"])
    b.add_argument("--skip-window", help='no entries in this ET window, e.g. 10:00-10:15, or "off"')
    b.add_argument("--confirm", type=int, help="extra candles to wait after the rejection candle")
    b.add_argument("--max-open", type=int, help="most trades open at once (0 = no limit)")
    b.add_argument("--profit-target", type=float, help="daily profit target in $ (0 = off)")
    b.add_argument("--loss-limit", type=float, help="daily loss limit in $ (0 = off)")
    b.add_argument("--target", help='"levels" or a number of R for a fixed target, e.g. 1.5')
    b.add_argument("--refresh", action="store_true", help="re-download instead of using the cache")
    b.add_argument("--cache-only", action="store_true", help="never download, only use cached data")
    b.add_argument("--label", default="", help="name for this run in the journal")
    b.add_argument("--start", help="fixed window start YYYY-MM-DD (out-of-sample tests)")
    b.add_argument("--end", help="fixed window end YYYY-MM-DD")
    b.add_argument("--mode", default="backtest", choices=["backtest", "oos"], help="journal section")
    b.add_argument("--report", help="write a separate dashboard to this path")
    b.add_argument("--title", default="", help="dashboard title for --report")
    lv = sub.add_parser("live"); lv.add_argument("--no-broker", action="store_true")
    lv.add_argument("--until", help="stop taking entries and exit at this ET time, e.g. 11:35 (cloud morning run)")
    sub.add_parser("close", help="cloud afternoon run: record fills, close anything open at 15:55")
    r2 = sub.add_parser("rsi2", help="RSI(2) pullback bot: decide entries/exits at 15:50 ET (journal-only paper)")
    r2.add_argument("--now", action="store_true", help="don't wait for 15:50 (testing)")
    r2.add_argument("--dry-run", action="store_true", help="use a throwaway journal (testing)")
    sub.add_parser("report")
    e = sub.add_parser("export"); e.add_argument("--mode", default="paper", choices=["paper", "backtest", "demo"])
    for p in (d, b, sub.choices["report"]):
        p.add_argument("--no-open", action="store_true", help="don't pop the dashboard open")
    args = ap.parse_args()

    cfg = config_mod.load(args.config)
    j = Journal()

    if args.cmd == "demo":
        from .backtest import run_backtest
        from .synthetic import generate
        bars = {s: generate(s, args.days, minutes=cfg.strategy.bar_minutes) for s in cfg.symbols}
        run_id = run_backtest(bars, cfg, j, mode="demo")
        print("Demo run on FAKE random data. It proves the bot works, nothing about the strategy.")
        _print_summary(j, "demo", run_id)
        _open_report(j, not args.no_open)

    elif args.cmd == "backtest":
        from .backtest import run_backtest
        from .data import fetch_history
        if args.symbols:
            cfg = replace(cfg, symbols=[s.upper() for s in args.symbols])
        cfg = replace(cfg, strategy=_override(cfg.strategy, args))
        if args.profit_target is not None:
            cfg = replace(cfg, risk=replace(cfg.risk, daily_profit_target=args.profit_target))
        if args.max_open is not None:
            cfg = replace(cfg, risk=replace(cfg.risk, max_open_trades=args.max_open))
        if args.loss_limit is not None:
            cfg = replace(cfg, risk=replace(cfg.risk, daily_loss_limit=args.loss_limit))
        p = cfg.strategy
        days = args.days or cfg.backtest_days
        print(f"Strategy: {p.name} | {p.bar_minutes}-min candles | stop {p.stop_mode}"
              + (f" | target {p.target_r:g}R" if p.target_mode == "fixed" else " | level targets")
              + (f" | breakeven at {p.breakeven_r:g}R" if p.breakeven_r else "")
              + (f" | FVG {p.fvg_mode}" if p.name == "orb_fvg" else "")
              + f" | news days {'skipped' if p.skip_news else 'traded'}"
              + f" | daily +${cfg.risk.daily_profit_target:g} / -${cfg.risk.daily_loss_limit:g}")
        if args.start:
            from datetime import date as _d
            from .data import fetch_range
            s_, e_ = _d.fromisoformat(args.start), _d.fromisoformat(args.end or str(_d.today()))
            print(f"Loading {p.bar_minutes}-min candles {s_} to {e_} for {', '.join(cfg.symbols)}...")
            bars = fetch_range(cfg, s_, e_, p.bar_minutes, refresh=args.refresh, cache_only=args.cache_only)
        else:
            print(f"Loading {days} days of {p.bar_minutes}-min candles for {', '.join(cfg.symbols)}...")
            bars = fetch_history(cfg, days, p.bar_minutes, refresh=args.refresh, cache_only=args.cache_only)
        first = __import__("datetime").date.fromisoformat(args.start) if args.start else None
        run_id = run_backtest(bars, cfg, j, mode=args.mode, label=args.label, first_day=first)
        if args.report:
            from pathlib import Path as _P
            from .report import build as _build
            out = _build(j, _P(args.report), modes=(args.mode,), title=args.title or "Backtest",
                         note=f"{args.start or ''} to {args.end or 'today'}".strip())
            _print_summary(j, args.mode, run_id)
            print(f"Dashboard: {out}")
            if not args.no_open:
                webbrowser.open(out.resolve().as_uri())
            return
        _print_summary(j, "backtest", run_id)
        _open_report(j, not args.no_open)

    elif args.cmd == "live":
        from .live import LiveTrader
        try:
            from .config import _t
            LiveTrader(cfg, j, use_broker=not args.no_broker).run(until=_t(args.until) if args.until else None)
        except KeyboardInterrupt:
            print("\nStopped. Open bracket orders stay on Alpaca and still protect the trade.")
        _open_report(j, False)

    elif args.cmd == "close":
        from .live import LiveTrader
        LiveTrader(cfg, j).closeout()
        _open_report(j, False)

    elif args.cmd == "rsi2":
        from . import rsi2
        from .live import say
        cfg.require_keys()
        if args.dry_run:
            say("RSI(2) DRY RUN: nothing is saved")
        rsi2.run(cfg, Journal(":memory:") if args.dry_run else j, say=say, wait=not args.now)

    elif args.cmd == "report":
        _open_report(j, not args.no_open)

    elif args.cmd == "export":
        mode = args.mode
        rows = j.trades(mode, None if mode == "paper" else j.latest_run(mode))
        out = ROOT / "reports" / f"trade_log_{mode}.csv"
        out.parent.mkdir(exist_ok=True)
        cols = ["session_date", "symbol", "direction", "entry_time", "entry", "stop", "target",
                "target_label", "exit_time", "exit", "exit_reason", "qty", "pnl", "r_multiple"]
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols + ["notes"])
            for r in rows:
                w.writerow([r[c] for c in cols] + [""])
        print(f"{len(rows)} trades -> {out}  (the notes column is for you: why it worked or didn't)")


if __name__ == "__main__":
    main()

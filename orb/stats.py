"""Turns the trade journal into numbers that actually tell you something.

Quick glossary:
    win rate       % of trades that made money
    avg R          average result measured in "risk units" (+1R = made what you risked)
    expectancy     what one trade is worth on average, in R. Positive = edge (maybe).
    profit factor  gross wins / gross losses. Above 1 makes money; 1.5+ is solid.
    max drawdown   worst peak-to-valley drop in the equity curve, in dollars.
"""
from __future__ import annotations

from collections import Counter, defaultdict


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    pnls = [t["pnl"] for t in trades]
    rs = [t["r_multiple"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)

    equity, peak, max_dd, curve = 0.0, 0.0, 0.0, []
    streak = worst_streak = 0
    for t in trades:
        equity += t["pnl"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        curve.append(round(equity, 2))
        streak = streak + 1 if t["pnl"] < 0 else 0
        worst_streak = max(worst_streak, streak)

    daily: dict[str, float] = defaultdict(float)
    for t in trades:
        daily[t["session_date"]] += t["pnl"]
    dv = list(daily.values())
    return {
        "days_traded": len(dv),
        "green_days": sum(v > 0 for v in dv),
        "red_days": sum(v < 0 for v in dv),
        "best_day": max(dv),
        "worst_day": min(dv),
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n,
        "loss_rate": len(losses) / n,
        "total_pnl": sum(pnls),
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "avg_r": sum(rs) / n,
        "expectancy_r": sum(rs) / n,
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "max_drawdown": max_dd,
        "worst_losing_streak": worst_streak,
        "best": max(pnls),
        "worst": min(pnls),
        "equity_curve": curve,
    }


def by_key(trades: list[dict], key: str) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[str(t[key])].append(t)
    return {k: summarize(v) for k, v in sorted(groups.items())}


OUTCOMES = ["traded", "no_retest", "no_fvg", "fvg_failed", "invalidated", "no_breakout",
            "filtered", "low_volume", "news_day", "daily_limit", "one_at_a_time", "missed", "no_data"]
OUTCOME_LABELS = {
    "traded": "Traded", "no_retest": "No retest", "no_fvg": "No FVG", "fvg_failed": "FVG failed",
    "invalidated": "Failed breakout", "no_breakout": "No breakout", "news_day": "News day (skipped)",
    "filtered": "Skipped (gap / time filter)",
    "low_volume": "Not in play (low volume)",
    "daily_limit": "Skipped (daily limit)",
    "one_at_a_time": "Skipped (trade already open)",
    "missed": "Missed (bot offline)", "no_data": "No data"}


def compliance(sessions: list[dict]) -> dict[str, dict]:
    """Per symbol: how many days produced each outcome. This is the 'does this
    ticker actually respect the setup?' table. Rates exclude skipped news days."""
    out: dict[str, Counter] = defaultdict(Counter)
    for s in sessions:
        out[s["symbol"]][s["outcome"]] += 1
    result = {}
    for sym, c in sorted(out.items()):
        days = sum(c.values())
        tradable = days - c["news_day"] - c["no_data"]
        breakouts = tradable - c["no_breakout"]
        result[sym] = {
            "days": days,
            "tradable_days": tradable,
            **{o: c[o] for o in OUTCOMES},
            "breakout_rate": breakouts / tradable if tradable else 0,
            "setup_rate": c["traded"] / tradable if tradable else 0,       # full setup completed
            "retest_rate": c["traded"] / breakouts if breakouts else 0,    # breakouts that became trades
        }
    return result


def _features(t: dict) -> dict:
    from datetime import datetime
    width = t["or_high"] - t["or_low"]
    mid = (t["or_high"] + t["or_low"]) / 2
    risk = abs(t["entry"] - t["stop"])
    entry_t = datetime.fromisoformat(t["entry_time"])
    edge = t["or_high"] if t["direction"] == "long" else t["or_low"]
    return {
        "Range size (% of price)": width / mid * 100,
        "Stop distance (% of price)": risk / t["entry"] * 100,
        "Stop distance (x range)": risk / width if width else 0,
        "Entry distance past range edge (x range)": abs(t["entry"] - edge) / width if width else 0,
        "Minutes from breakout to entry": (entry_t - datetime.fromisoformat(t["breakout_time"])).seconds / 60,
        "Entry time (minutes after 9:30)": entry_t.hour * 60 + entry_t.minute - 570,
        "Target distance (R)": abs(t["target"] - t["entry"]) / risk if risk else 0,
    }


def win_loss_profile(trades: list[dict]) -> list[dict]:
    """Median of each feature for winners vs losers. If they look the same, that feature
    doesn't help separate good setups from bad ones."""
    from statistics import median
    wins = [_features(t) for t in trades if t["pnl"] > 0]
    losses = [_features(t) for t in trades if t["pnl"] <= 0]
    if not wins or not losses:
        return []
    rows = []
    for k in wins[0]:
        w, l = median(x[k] for x in wins), median(x[k] for x in losses)
        rows.append({"feature": k, "win": w, "loss": l,
                     "diff_pct": (w - l) / abs(l) * 100 if l else 0.0})
    def long_share(group: list[dict]) -> float:
        return sum(t["direction"] == "long" for t in group) / len(group) * 100
    won = [t for t in trades if t["pnl"] > 0]
    lost = [t for t in trades if t["pnl"] <= 0]
    rows.append({"feature": "Share that were longs", "win": long_share(won),
                 "loss": long_share(lost), "diff_pct": 0.0, "pct": True})
    return rows

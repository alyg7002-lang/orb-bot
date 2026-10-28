"""Loads config.toml into plain Python objects."""
from __future__ import annotations

import os
try:
    import tomllib
except ModuleNotFoundError:          # Python 3.10
    import tomli as tomllib
from dataclasses import dataclass
from datetime import time
from pathlib import Path

from .strategy import StrategyParams

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RiskConfig:
    risk_per_trade: float = 100.0
    max_position_value: float = 25000.0
    slippage_per_share: float = 0.01
    daily_profit_target: float = 0.0   # stop for the day once up this much (0 = off)
    daily_loss_limit: float = 0.0      # no new trades once down this much (0 = off)
    max_open_trades: int = 0           # most trades open at once across all tickers (0 = no limit)
    sizing: str = "fixed"              # "fixed" = risk_per_trade every trade, "compound" = % of bot account
    starting_equity: float = 10000.0   # compound: the bot's own starting account
    risk_pct: float = 1.0              # compound: % of (starting_equity + closed profits) risked per trade
    max_risk_per_trade: float = 1000.0 # compound: safety ceiling on any one trade's risk


@dataclass
class Config:
    symbols: list[str]
    strategy: StrategyParams
    risk: RiskConfig
    backtest_days: int
    backtest_feed: str
    live_feed: str
    api_key: str
    secret_key: str
    paper: bool

    def require_keys(self) -> None:
        if not self.api_key or not self.secret_key:
            raise SystemExit(
                "No Alpaca keys found. Set ALPACA_API_KEY and ALPACA_SECRET_KEY "
                "(see README, step 2), or run `python -m orb demo` to try it with fake data.")


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def validate_risk(r: "RiskConfig") -> None:
    if r.sizing not in ("fixed", "compound"):
        raise SystemExit('sizing must be "fixed" or "compound"')
    if r.sizing == "compound" and not (0 < r.risk_pct <= 5):
        raise SystemExit("risk_pct must be between 0 and 5 (%)")


def validate(p: StrategyParams) -> None:
    if p.name not in ("orb_retest", "orb_fvg"):
        raise SystemExit('[strategy] name must be "orb_fvg" or "orb_retest"')
    if p.challenger not in ("off", "shadow", "veto"):
        raise SystemExit('challenger must be "off", "shadow" or "veto"')
    if p.stop_mode not in ("mid", "opposite", "fvg"):
        raise SystemExit('stop_mode must be "fvg", "mid" or "opposite"')
    if p.stop_mode == "fvg" and p.name != "orb_fvg":
        raise SystemExit('stop_mode "fvg" only works with name = "orb_fvg"')
    if p.fvg_mode not in ("edge", "any"):
        raise SystemExit('fvg_mode must be "edge" or "any"')
    if p.target_mode not in ("levels", "fixed"):
        raise SystemExit('target_mode must be "levels" or "fixed"')
    if p.target_mode == "fixed" and p.target_r <= 0:
        raise SystemExit("target_r must be above 0")
    if p.confirm_bars < 0:
        raise SystemExit("confirm_bars can't be negative")
    if p.breakeven_r < 0:
        raise SystemExit("breakeven_r can't be negative (0 = off)")
    if p.bar_minutes not in (1, 5):
        raise SystemExit("bar_minutes must be 1 or 5")


def load(path: str | Path | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.toml"
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    s, r, d, a = (raw.get(k, {}) for k in ("strategy", "risk", "data", "alpaca"))
    name = s.get("name", "orb_retest")
    params = StrategyParams(
        name=name,
        bar_minutes=s.get("bar_minutes", 1 if name == "orb_fvg" else 5),
        fvg_mode=s.get("fvg_mode", "edge"),
        min_fvg_frac=s.get("min_fvg_frac", 0.05),
        skip_news=s.get("skip_news", True),
        breakeven_r=s.get("breakeven_r", 0.0),
        confirm_bars=s.get("confirm_bars", 0),
        min_rvol=s.get("min_rvol", 0.0),
        rvol_days=s.get("rvol_days", 14),
        gap_filter=s.get("gap_filter", False),
        gap_min_pct=s.get("gap_min_pct", 0.1),
        skip_start=_t(s["skip_entries_from"]) if s.get("skip_entries_from") else None,
        skip_end=_t(s["skip_entries_until"]) if s.get("skip_entries_until") else None,
        range_minutes=s.get("range_minutes", 15),
        retest_tolerance=s.get("retest_tolerance", 0.10),
        stop_mode=s.get("stop_mode", "mid"),
        target_mode=s.get("target_mode", "levels"),
        target_r=s.get("target_r", 1.0),
        min_rr=s.get("min_rr", 1.0),
        fallback_rr=s.get("fallback_rr", 2.0),
        entry_cutoff=_t(s.get("entry_cutoff", "11:30")),
        flat_time=_t(s.get("flat_time", "15:55")),
        challenger=str(s.get("challenger", "off")).lower(),
        ch_min_depth=s.get("challenger_min_depth", 0.25),
        ch_min_body=s.get("challenger_min_body", 0.5),
        ch_close_zone=s.get("challenger_close_zone", 0.33),
        ch_min_range_mult=s.get("challenger_min_range_mult", 1.0),
    )
    validate(params)
    risk = RiskConfig(**r)
    validate_risk(risk)
    return Config(
        symbols=[x.upper() for x in raw.get("symbols", ["SPY", "QQQ", "NVDA", "TSLA", "AAPL"])],
        strategy=params,
        risk=risk,
        backtest_days=d.get("backtest_days", 365),
        backtest_feed=d.get("backtest_feed", "sip"),
        live_feed=d.get("live_feed", "iex"),
        api_key=os.environ.get("ALPACA_API_KEY") or a.get("api_key", ""),
        secret_key=os.environ.get("ALPACA_SECRET_KEY") or a.get("secret_key", ""),
        paper=a.get("paper", True),
    )

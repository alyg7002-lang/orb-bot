"""Position sizing. Risk a fixed dollar amount per trade, no matter the ticker.

Example: risk $100, entry $500, stop $498 -> $2 risk per share -> 50 shares.
If the stop hits you lose about $100. That's the whole idea of "1R".
"""
import math


def position_size(entry: float, stop: float, risk_dollars: float, max_position_value: float) -> int:
    per_share = abs(entry - stop)
    if per_share <= 0 or entry <= 0:
        return 0
    qty = math.floor(risk_dollars / per_share)
    qty = min(qty, math.floor(max_position_value / entry))
    return max(qty, 0)


def pnl(direction: str, entry: float, exit_px: float, qty: int, slippage_per_share: float) -> float:
    sign = 1 if direction == "long" else -1
    return (exit_px - entry) * sign * qty - 2 * slippage_per_share * qty


def size_factor(risk_cfg, closed_profit: float) -> float:
    """How much bigger (or smaller) than the base $ risk to trade today.

    fixed:    always 1.0
    compound: risk = risk_pct% of (starting_equity + all closed profit so far), capped at
              max_risk_per_trade. Grows after wins, shrinks after losses. Daily limits and the
              position-value cap scale by the same factor so the rules keep their shape.
    """
    if getattr(risk_cfg, "sizing", "fixed") != "compound":
        return 1.0
    equity = max(risk_cfg.starting_equity + closed_profit, 0.0)
    risk = min(equity * risk_cfg.risk_pct / 100.0, risk_cfg.max_risk_per_trade)
    return risk / risk_cfg.risk_per_trade

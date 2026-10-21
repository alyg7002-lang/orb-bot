"""Daily profit target and loss limit, shared by the backtest and the live bot.

profit_target: once the day is up this much (closed trades + open trades' current
               profit), close everything and stop for the day.
loss_limit:    the day can never lose more than this. A new trade is only taken if losing
               its full risk would still keep the day above -loss_limit. (Risk $100 and a
               $100 limit means: any loss and you're done for the day.)
0 turns either rule off.
"""
from __future__ import annotations


class DailyGuard:
    def __init__(self, profit_target: float = 0.0, loss_limit: float = 0.0):
        self.profit_target = profit_target
        self.loss_limit = loss_limit
        self.realized = 0.0
        self.halted: str | None = None      # None, "profit_target" or "loss_limit"

    def add(self, pnl: float) -> None:
        self.realized += pnl

    def check(self, unrealized: float = 0.0) -> str | None:
        """Returns why trading stopped (or None). Once halted, stays halted for the day."""
        if self.halted:
            return self.halted
        if self.profit_target and self.realized + unrealized >= self.profit_target:
            self.halted = "profit_target"
        elif self.loss_limit and self.realized <= -self.loss_limit:
            self.halted = "loss_limit"
        return self.halted

    def allows(self, trade_risk: float) -> bool:
        """Can we take a trade that loses `trade_risk` dollars if it hits its stop?"""
        if self.check():
            return False
        if self.loss_limit and self.realized - trade_risk < -self.loss_limit - 1e-6:
            return False
        return True

    def describe(self) -> str:
        if self.halted == "profit_target":
            return f"Daily profit target ${self.profit_target:,.0f} hit: closing everything, done for the day"
        if self.halted == "loss_limit":
            return f"Daily loss limit ${self.loss_limit:,.0f} hit: no new trades today"
        return ""

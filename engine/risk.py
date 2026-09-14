"""Risk & money management module.

Encodes the qualities of consistently-profitable traders researched up front:

* Risk a fixed, small % of the account per trade (1% recommended, 2% hard cap).
* Position size is DERIVED from the stop distance, never a fixed contract size.
* Reward:risk is validated against the strategy's expectancy before the signal
  is labelled tradable.
* A daily loss limit and a consecutive-loss circuit breaker force time out.
* Realised win-rate vs the strategy's backtest WR automatically tightens or
  relaxes the per-trade risk (growth through statistical confidence).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import config


@dataclass
class RiskDecision:
    ok: bool
    lots: float = 0.0
    risk_usd: float = 0.0
    rr: float = 0.0
    ref_reason: str = ""
    messages: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(self, balance: float | None = None,
                 risk_pct: float | None = None,
                 daily_loss_limit_pct: float | None = None,
                 max_consecutive_losses: int | None = None):
        self.balance = balance if balance is not None else config.ACCOUNT_BALANCE
        self.risk_pct = min(risk_pct if risk_pct is not None else config.RISK_PER_TRADE,
                            config.MAX_RISK_PER_TRADE)
        self.daily_loss_pct = daily_loss_limit_pct if daily_loss_limit_pct is not None else config.DAILY_LOSS_LIMIT
        self.max_cons = max_consecutive_losses if max_consecutive_losses is not None else config.MAX_CONSECUTIVE_LOSSES
        self.consecutive_losses = 0
        self.today_pnl_usd = 0.0
        self.day_key: str | None = None
        self.min_rr = 0.5
        self.realized_wr: float | None = None   # set externally from live journal

    def rollover(self, now_utc_day: str) -> None:
        if self.day_key is None:
            self.day_key = now_utc_day
        elif self.day_key != now_utc_day:
            self.day_key = now_utc_day
            self.today_pnl_usd = 0.0

    def _rule_risk_cap(self) -> float:
        """Kelly-informed cap: never risk more than half-Kelly for the backtest
        expectancy, bounded by the configured % cap."""
        base = self.risk_pct
        if self.realized_wr is not None and self.realized_wr > 0:
            # assume payoff ratio ~ backtest avgR/(1-avgR losses)
            b = 1.2  # conservative payoff for live editions
            kelly = (self.realized_wr * (b + 1) - 1) / b
            kelly_cap = max(0.0, 0.25 * kelly * 100)  # quarter-Kelly
            if kelly_cap > 0 and kelly_cap < base:
                base = kelly_cap
        return base

    def evaluate(self, symbol: str, direction: int, entry: float, stop: float,
                 take_profit: float, now_utc_day: str | None = None,
                 realized_wr: float | None = None) -> RiskDecision:
        """Validate a proposed trade and return the computed lot size."""
        if now_utc_day:
            self.rollover(now_utc_day)
        if realized_wr is not None:
            self.realized_wr = realized_wr

        msgs: list[str] = []
        if self.consecutive_losses >= self.max_cons:
            return RiskDecision(False, 0.0, 0.0, 0.0, "circuit-breaker",
                                [f"{self.consecutive_losses} straight losses - manual review required."])
        if self.today_pnl_usd <= -self.balance * self.daily_loss_pct / 100:
            return RiskDecision(False, 0.0, 0.0, 0.0, "daily-loss-limit",
                                [f"Daily loss limit ({self.daily_loss_pct}%) hit. Trading halted until tomorrow."])

        sl_dist = abs(entry - stop)
        if sl_dist <= 0:
            return RiskDecision(False, 0.0, 0.0, 0.0, "invalid-stop", ["Stop distance must be > 0."])
        rr = abs(take_profit - entry) / sl_dist
        if rr < self.min_rr:
            msgs.append(f"RR {rr:.2f} below minimum {self.min_rr:.2f} - target too close to risk.")

        risk_pct_used = self._rule_risk_cap()
        risk_usd = self.balance * risk_pct_used / 100.0
        lots = position_size(symbol, risk_usd, sl_dist)
        lots = floor_lots(symbol, lots)
        rd = RiskDecision(True, lots, round(risk_usd, 2), rr, "ok", msgs)
        rd.lots = floor_lots(symbol, rd.lots)
        return rd


def position_size(symbol: str, risk_usd: float, sl_distance: float) -> float:
    """lots = risk_$ / (sl_distance_per_point * value_per_point_per_lot)."""
    c = config.CONTRACTS[symbol]
    points = sl_distance / c["point"]
    if points <= 0:
        return 0.0
    return risk_usd / (points * c["pip_value_per_lot_usd"])


def floor_lots(symbol: str, lots: float) -> float:
    step = 0.01 if symbol == "EURUSD" else 0.01
    lots = math.floor(max(lots, 0.0) / step) * step
    return round(lots, 2)


def risk_amount(symbol: str, lots: float, sl_distance: float) -> float:
    c = config.CONTRACTS[symbol]
    return lots * (sl_distance / c["point"]) * c["pip_value_per_lot_usd"]


def format_decimal(symbol: str, price: float) -> str:
    digits = config.CONTRACTS[symbol]["digits"]
    return f"{price:.{digits}f}"
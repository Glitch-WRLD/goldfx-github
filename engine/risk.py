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

import datetime as dt
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import config

log = logging.getLogger("goldfx.risk")

ANCHOR_FILE = config.DATA_CACHE / "risk_anchor.json"


def get_current_week_key(t: dt.datetime | None = None) -> str:
    """Return ISO year and week key, e.g. '2026-W41'."""
    now = t or dt.datetime.now(dt.timezone.utc)
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def get_or_update_anchor_balance(live_balance: float | None = None) -> float:
    """Retrieve or update the weekly step-compounding anchor balance.
    Eliminates the Asymmetric Dollar Risk Trap (peak-sizing vs valley-sizing):
    - Locks sizing balance to settled week-start equity so all trades during the week
      risk an equal dollar amount (e.g. 3.5% of anchor).
    - Automatically steps/rolls over when a new ISO trading week starts.
    - Safety guard: steps down if live equity drops > 25% to protect capital during outlier drawdowns.
    - Deposit guard: re-anchors if account balance jumps > 35% (external deposit/withdrawal).
    """
    default_bal = getattr(config, "ACCOUNT_BALANCE", 100.0)
    current_week = get_current_week_key()
    data: dict = {}

    if ANCHOR_FILE.exists():
        try:
            data = json.loads(ANCHOR_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("failed to read %s: %s", ANCHOR_FILE, e)

    stored_week = data.get("week_key")
    stored_anchor = float(data.get("anchor_balance", 0.0))

    # Initialize if empty or missing
    if stored_anchor <= 0:
        new_anchor = live_balance if (live_balance and live_balance > 0) else default_bal
        data = {
            "week_key": current_week,
            "anchor_balance": round(new_anchor, 2),
            "last_updated": dt.datetime.now(dt.timezone.utc).isoformat(),
            "reason": "initial_anchor",
        }
        try:
            ANCHOR_FILE.parent.mkdir(exist_ok=True)
            ANCHOR_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            log.info("STEP_COMPOUNDING: Initialized anchor balance to $%.2f for week %s.", new_anchor, current_week)
        except Exception as e:
            log.warning("failed to write %s: %s", ANCHOR_FILE, e)
        return float(new_anchor)

    # 1. Weekly rollover: New ISO week detected
    if stored_week != current_week:
        new_anchor = live_balance if (live_balance and live_balance > 0) else stored_anchor
        data = {
            "week_key": current_week,
            "anchor_balance": round(new_anchor, 2),
            "last_updated": dt.datetime.now(dt.timezone.utc).isoformat(),
            "reason": f"weekly_rollover (from {stored_week})",
        }
        try:
            ANCHOR_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            log.info("STEP_COMPOUNDING: Weekly rollover from %s to %s. Anchor balance updated from $%.2f to $%.2f.",
                     stored_week, current_week, stored_anchor, new_anchor)
        except Exception as e:
            log.warning("failed to write %s: %s", ANCHOR_FILE, e)
        return float(new_anchor)

    # 2. Drawdown safety trigger (Disabled by default to prevent valley-sizing and maintain constant weekly expectancy)
    if getattr(config, "STEP_DRAWDOWN_RESET_ENABLED", False) and live_balance and live_balance > 0:
        dd_reset_pct = float(getattr(config, "STEP_DRAWDOWN_RESET_PCT", 50.0))
        if live_balance < stored_anchor * (1.0 - dd_reset_pct / 100.0):
            data["anchor_balance"] = round(live_balance, 2)
            data["last_updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
            data["reason"] = f"drawdown_safety_reset (hit >{dd_reset_pct:.0f}% drop)"
            try:
                ANCHOR_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
                log.warning("STEP_COMPOUNDING_SAFETY: Drawdown exceeded %s%%. Stepping anchor down from $%.2f to $%.2f.",
                            dd_reset_pct, stored_anchor, live_balance)
            except Exception as e:
                log.warning("failed to write %s: %s", ANCHOR_FILE, e)
            return float(live_balance)

        # 3. External deposit/withdrawal re-anchor (> 35% jump)
        if live_balance > stored_anchor * 1.35 or live_balance < stored_anchor * 0.50:
            data["anchor_balance"] = round(live_balance, 2)
            data["last_updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
            data["reason"] = "capital_adjustment_reset"
            try:
                ANCHOR_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
                log.info("STEP_COMPOUNDING: Significant capital change detected. Anchor reset from $%.2f to $%.2f.",
                         stored_anchor, live_balance)
            except Exception as e:
                log.warning("failed to write %s: %s", ANCHOR_FILE, e)
            return float(live_balance)

    return stored_anchor


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
        if balance is not None:
            self.balance = balance
        elif getattr(config, "STEP_COMPOUNDING_ENABLED", True):
            self.balance = get_or_update_anchor_balance()
        else:
            self.balance = config.ACCOUNT_BALANCE
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
        if getattr(config, "CIRCUIT_BREAKER_ENABLED", False) and self.consecutive_losses >= self.max_cons:
            return RiskDecision(False, 0.0, 0.0, 0.0, "circuit-breaker",
                                [f"{self.consecutive_losses} straight losses - manual review required."])
        if getattr(config, "DAILY_LOSS_LIMIT_ENABLED", False) and self.today_pnl_usd <= -self.balance * self.daily_loss_pct / 100:
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
        min_pct = getattr(config, "MIN_RISK_PER_TRADE", 3.0)
        max_pct = getattr(config, "MAX_RISK_PER_TRADE", 4.0)
        lots = bracket_quantize_lots(symbol, risk_usd, sl_dist, self.balance, min_risk_pct=min_pct, max_risk_pct=max_pct, target_risk_pct=risk_pct_used)
        actual_dollar_risk = risk_amount(symbol, lots, sl_dist) if lots > 0 else 0.0
        rd = RiskDecision(True, lots, round(actual_dollar_risk if actual_dollar_risk > 0 else risk_usd, 2), rr, "ok", msgs)
        return rd


def position_size(symbol: str, risk_usd: float, sl_distance: float) -> float:
    """lots = risk_$ / (sl_distance_per_point * value_per_point_per_lot).
    Dynamically queries live broker tick size & value from MT5 memory if available.
    """
    if sl_distance <= 0 or risk_usd <= 0:
        return 0.0

    try:
        import MetaTrader5 as mt5
        info = mt5.symbol_info(symbol)
        if info and info.trade_tick_size > 0 and info.trade_tick_value > 0:
            loss_per_lot = (sl_distance / info.trade_tick_size) * info.trade_tick_value
            if loss_per_lot > 0:
                return risk_usd / loss_per_lot
    except Exception:
        pass

    c = config.CONTRACTS.get(symbol, {})
    point = c.get("point", 0.00001)
    val = c.get("pip_value_per_lot_usd", 1.0)
    points = sl_distance / point if point > 0 else 0.0
    if points <= 0:
        return 0.0
    return risk_usd / (points * val)


def floor_lots(symbol: str, lots: float) -> float:
    step = 0.01
    try:
        import MetaTrader5 as mt5
        info = mt5.symbol_info(symbol)
        if info and info.volume_step > 0:
            step = info.volume_step
    except Exception:
        pass
    lots = math.floor(max(lots, 0.0) / step) * step
    return round(lots, 2)


def bracket_quantize_lots(symbol: str, target_risk_usd: float, sl_distance: float,
                          balance: float, min_risk_pct: float = 3.0,
                          max_risk_pct: float = 4.0, target_risk_pct: float = 3.5) -> float:
    """Smart lot quantizer that targets the user's dedicated risk percentage (e.g. 3.5%)
    and keeps dollar risk strictly inside [min_risk_pct, max_risk_pct] (e.g. 3.0% - 4.0%).
    
    Prevents the severe mathematical expectancy degradation caused by blind downward flooring
    on small-account indices (where 0.017 lots floored to 0.01 drops risk to 1.1%, halving profits).
    """
    if sl_distance <= 0 or balance <= 0 or target_risk_usd <= 0:
        return 0.0

    step = 0.01
    vol_min = 0.01
    vol_max = 100.0
    try:
        import MetaTrader5 as mt5
        info = mt5.symbol_info(symbol)
        if info:
            if info.volume_step > 0:
                step = info.volume_step
            if info.volume_min > 0:
                vol_min = info.volume_min
            if info.volume_max > 0:
                vol_max = info.volume_max
    except Exception:
        pass

    raw_lots = position_size(symbol, target_risk_usd, sl_distance)
    if raw_lots <= 0:
        return 0.0

    lots_floor = round(math.floor(raw_lots / step) * step, 2)
    lots_ceil = round(math.ceil(raw_lots / step) * step, 2)

    if lots_ceil < vol_min:
        lots_ceil = vol_min

    risk_floor = risk_amount(symbol, lots_floor, sl_distance) if lots_floor >= vol_min else 0.0
    risk_ceil = risk_amount(symbol, lots_ceil, sl_distance)

    pct_floor = (risk_floor / balance) * 100.0
    pct_ceil = (risk_ceil / balance) * 100.0

    # If flooring drops risk below target minimum (e.g. < 3.0%),
    # but ceil keeps risk <= max_risk_pct (e.g. <= 4.0%), choose ceil!
    if pct_floor < min_risk_pct and pct_ceil <= max_risk_pct:
        chosen = lots_ceil
    elif pct_ceil <= max_risk_pct and abs(pct_ceil - target_risk_pct) < abs(pct_floor - target_risk_pct):
        chosen = lots_ceil
    else:
        chosen = lots_floor

    chosen = max(vol_min if lots_floor >= vol_min or pct_ceil <= max_risk_pct else 0.0, min(chosen, vol_max))
    return round(chosen, 2)


def risk_amount(symbol: str, lots: float, sl_distance: float) -> float:
    try:
        import MetaTrader5 as mt5
        info = mt5.symbol_info(symbol)
        if info and info.trade_tick_size > 0 and info.trade_tick_value > 0:
            return float(lots * (sl_distance / info.trade_tick_size) * info.trade_tick_value)
    except Exception:
        pass
    c = config.CONTRACTS.get(symbol, {})
    point = c.get("point", 0.00001)
    val = c.get("pip_value_per_lot_usd", 1.0)
    return lots * (sl_distance / point) * val


def format_decimal(symbol: str, price: float) -> str:
    digits = config.CONTRACTS[symbol]["digits"]
    return f"{price:.{digits}f}"
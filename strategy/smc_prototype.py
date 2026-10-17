"""Prototype implementation of Anthony Ikechukwu's 4-Rule SMC Institutional Strategy.

Rules:
1. Trigger BOS / MSS with displacement (breaking prior structural swing).
2. Inducement (ind) formed and swept before POI tap.
3. Unmitigated Order Block (OB).
4. POI closest to liquidity with structural target (1:2.5R - 1:6R+).
"""

from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import pandas as pd
from strategy import indicators as ind
from strategy.backtester import Signal, run_backtest, BacktestResult
from data.tv_data import load_cached


@dataclass
class OrderBlock:
    dir: int                # +1 Bullish, -1 Bearish
    top: float              # proximal edge
    bottom: float           # distal edge
    created_idx: int
    created_ts: pd.Timestamp
    bos_level: float        # level that was broken to validate this OB
    ind_level: float | None = None  # inducement level
    swept: bool = False     # whether inducement has been swept
    mitigated: bool = False


def detect_swings(H: np.ndarray, L: np.ndarray, k: int = 4) -> tuple[np.ndarray, np.ndarray]:
    """Identify swing highs and swing lows with k-bar confirmation."""
    n = len(H)
    sh = np.full(n, np.nan)
    sl = np.full(n, np.nan)
    for i in range(k, n - k):
        # swing high
        if H[i] == np.max(H[i - k:i + k + 1]):
            sh[i] = H[i]
        # swing low
        if L[i] == np.min(L[i - k:i + k + 1]):
            sl[i] = L[i]
    return sh, sl


def smc_institutional_signaller(df: pd.DataFrame, p: dict) -> list[Signal]:
    """Generates signals following Anthony's 4 institutional rules."""
    H = df["high"].to_numpy()
    L = df["low"].to_numpy()
    O = df["open"].to_numpy()
    C = df["close"].to_numpy()
    times = df.index
    n = len(df)

    k = int(p.get("swing_k", 3))
    atr_len = int(p.get("atr_len", 14))
    atr = ind.atr(df, atr_len).to_numpy()

    # Precompute swings (shifted by k bars to avoid any lookahead)
    raw_sh, raw_sl = detect_swings(H, L, k=k)
    
    # Causal swing tracking: at bar i, we only know swings confirmed at or before i - k
    sh_history: list[tuple[int, float]] = []  # (bar_idx, price)
    sl_history: list[tuple[int, float]] = []

    active_obs: list[OrderBlock] = []
    signals: list[Signal] = []
    last_signal_idx = -10**9
    min_gap = int(p.get("min_gap", 6))
    max_ob_age = int(p.get("max_ob_age", 120))
    min_rr = float(p.get("min_rr", 2.0))
    max_rr = float(p.get("max_rr", 6.0))
    sl_buf_atr = float(p.get("sl_buf_atr", 0.15))

    for i in range(k * 2 + 10, n):
        # 1. Update confirmed swings available at bar i
        check_idx = i - k
        if not np.isnan(raw_sh[check_idx]):
            sh_history.append((check_idx, raw_sh[check_idx]))
        if not np.isnan(raw_sl[check_idx]):
            sl_history.append((check_idx, raw_sl[check_idx]))

        # Keep swing history manageable
        if len(sh_history) > 30:
            sh_history = sh_history[-30:]
        if len(sl_history) > 30:
            sl_history = sl_history[-30:]

        # 2. Check for Break of Structure (BOS) on current bar close
        # Bullish BOS: Close breaks above most recent confirmed Swing High
        if sh_history:
            recent_sh_idx, recent_sh_val = sh_history[-1]
            if C[i] > recent_sh_val and C[i - 1] <= recent_sh_val and (i - recent_sh_idx) >= 2:
                # Find the base origin candle of this move (the lowest candle before the expansion)
                lookback_range = range(max(0, recent_sh_idx), i)
                if lookback_range:
                    base_idx = min(lookback_range, key=lambda idx: L[idx])
                    # Check for displacement / imbalance out of this base
                    # Bullish OB zone: from low of base candle to body high/top of base candle
                    ob_bottom = float(L[base_idx])
                    ob_top = float(max(O[base_idx], C[base_idx]))
                    # Ensure zone is valid thickness
                    if ob_top > ob_bottom and (ob_top - ob_bottom) <= 2.5 * atr[i]:
                        active_obs.append(OrderBlock(
                            dir=+1,
                            top=ob_top,
                            bottom=ob_bottom,
                            created_idx=base_idx,
                            created_ts=times[base_idx],
                            bos_level=recent_sh_val
                        ))

        # Bearish BOS: Close breaks below most recent confirmed Swing Low
        if sl_history:
            recent_sl_idx, recent_sl_val = sl_history[-1]
            if C[i] < recent_sl_val and C[i - 1] >= recent_sl_val and (i - recent_sl_idx) >= 2:
                lookback_range = range(max(0, recent_sl_idx), i)
                if lookback_range:
                    base_idx = max(lookback_range, key=lambda idx: H[idx])
                    ob_top = float(H[base_idx])
                    ob_bottom = float(min(O[base_idx], C[base_idx]))
                    if ob_top > ob_bottom and (ob_top - ob_bottom) <= 2.5 * atr[i]:
                        active_obs.append(OrderBlock(
                            dir=-1,
                            top=ob_top,
                            bottom=ob_bottom,
                            created_idx=base_idx,
                            created_ts=times[base_idx],
                            bos_level=recent_sl_val
                        ))

        # 3. Prune active OBs (age, invalidation/breakout beyond distal edge)
        surviving_obs: list[OrderBlock] = []
        for ob in active_obs:
            if i - ob.created_idx > max_ob_age:
                continue
            # If price completely blew past the distal edge, the OB is invalidated
            if ob.dir == 1 and C[i] < ob.bottom - 0.5 * atr[i]:
                continue
            if ob.dir == -1 and C[i] > ob.top + 0.5 * atr[i]:
                continue
            surviving_obs.append(ob)
        active_obs = surviving_obs

        # 4. Check for Inducement (`ind`) formation & sweeps for active OBs
        for ob in active_obs:
            if ob.mitigated:
                continue

            # Bullish OB: Looking for an inducement low formed above ob.top
            if ob.dir == 1:
                # Find minor swing lows formed between the OB and current bar
                if ob.ind_level is None and len(sl_history) > 0:
                    for sl_idx, sl_val in reversed(sl_history):
                        if sl_idx > ob.created_idx and sl_val > ob.top:
                            ob.ind_level = sl_val
                            break
                
                # Check if inducement has been swept
                if ob.ind_level is not None and not ob.swept:
                    if L[i] < ob.ind_level:
                        ob.swept = True

                # Check if price taps the unmitigated OB (Rule 3 & 4) AFTER sweeping inducement
                if ob.swept and not ob.mitigated:
                    # Tap check: low touches into [ob.bottom, ob.top + 0.2*ATR]
                    if L[i] <= ob.top + 0.1 * atr[i] and L[i] >= ob.bottom - 0.2 * atr[i]:
                        # Confirmation check: rejection candle (close > open and close > ob.top)
                        # or close in top 50% of candle range
                        rng = H[i] - L[i]
                        wick = C[i] - L[i]
                        is_rejection = rng > 0 and (wick / rng) >= 0.40 and C[i] > O[i]

                        if is_rejection and (i - last_signal_idx) >= min_gap:
                            entry = float(C[i])
                            sl_price = ob.bottom - sl_buf_atr * atr[i]
                            sl_dist = entry - sl_price
                            if sl_dist > 0:
                                # Target structural high (the high of the impulse move)
                                target_high = float(np.max(H[ob.created_idx:i]))
                                # Calculate R:R
                                calc_rr = (target_high - entry) / sl_dist
                                if calc_rr >= min_rr:
                                    target_rr = min(calc_rr, max_rr)
                                    signals.append(Signal(
                                        ts=times[i],
                                        dir=+1,
                                        sl_offset=float(sl_dist),
                                        tp_r=float(target_rr),
                                        reason=f"SMC OB Tap + Inducement Sweep (RR {target_rr:.1f})",
                                        tp_price=float(target_high)
                                    ))
                                    last_signal_idx = i
                                    ob.mitigated = True
                                    break

            # Bearish OB: Looking for an inducement high formed below ob.bottom
            elif ob.dir == -1:
                if ob.ind_level is None and len(sh_history) > 0:
                    for sh_idx, sh_val in reversed(sh_history):
                        if sh_idx > ob.created_idx and sh_val < ob.bottom:
                            ob.ind_level = sh_val
                            break

                if ob.ind_level is not None and not ob.swept:
                    if H[i] > ob.ind_level:
                        ob.swept = True

                if ob.swept and not ob.mitigated:
                    if H[i] >= ob.bottom - 0.1 * atr[i] and H[i] <= ob.top + 0.2 * atr[i]:
                        rng = H[i] - L[i]
                        wick = H[i] - C[i]
                        is_rejection = rng > 0 and (wick / rng) >= 0.40 and C[i] < O[i]

                        if is_rejection and (i - last_signal_idx) >= min_gap:
                            entry = float(C[i])
                            sl_price = ob.top + sl_buf_atr * atr[i]
                            sl_dist = sl_price - entry
                            if sl_dist > 0:
                                target_low = float(np.min(L[ob.created_idx:i]))
                                calc_rr = (entry - target_low) / sl_dist
                                if calc_rr >= min_rr:
                                    target_rr = min(calc_rr, max_rr)
                                    signals.append(Signal(
                                        ts=times[i],
                                        dir=-1,
                                        sl_offset=float(sl_dist),
                                        tp_r=float(target_rr),
                                        reason=f"SMC OB Tap + Inducement Sweep (RR {target_rr:.1f})",
                                        tp_price=float(target_low)
                                    ))
                                    last_signal_idx = i
                                    ob.mitigated = True
                                    break

    return signals


if __name__ == "__main__":
    params = {
        "swing_k": 3,
        "atr_len": 14,
        "min_gap": 6,
        "max_ob_age": 100,
        "min_rr": 1.5,
        "max_rr": 5.0,
        "sl_buf_atr": 0.15,
    }

    pairs = [("GBPUSD", "H1"), ("AUDUSD", "H1"), ("EURUSD", "H1"), ("XAUUSD", "M30")]
    for sym, tf in pairs:
        df = load_cached(sym, tf)
        if df is not None:
            res = run_backtest(df, sym, tf, "smc_institutional", smc_institutional_signaller, params)
            print(res.summary())

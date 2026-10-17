"""Refined Anthony Ikechukwu SMC Strategy:
1. HTF Trend / Bias Alignment (4H/H1)
2. BOS/MSS with Displacement & Imbalance (FVG)
3. Inducement (ind) Formation & Liquidity Sweep
4. Limit Entry at Proximal Edge of Unmitigated OB
5. Structural Take Profit targeting Opposing External Liquidity
"""

import sys
from pathlib import Path
sys.path.insert(0, 'C:/goldfx-agent')

import numpy as np
import pandas as pd
from dataclasses import dataclass
from strategy import indicators as ind
from strategy.backtester import Signal, run_backtest
from data.tv_data import load_cached


@dataclass
class OrderBlock:
    dir: int
    top: float
    bottom: float
    created_idx: int
    created_ts: pd.Timestamp
    bos_level: float
    ind_level: float | None = None
    swept: bool = False
    mitigated: bool = False
    target: float = 0.0


def htf_bias_series(df: pd.DataFrame, htf: str = "H4", ema_len: int = 50) -> pd.Series:
    rule = {"H1": "1h", "H2": "2h", "H4": "4h", "D": "1d"}.get(htf, htf)
    h = df.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    e = ind.ema(h["close"], ema_len)
    hbias = np.where(h["close"] > e, 1.0, -1.0)
    hbias = pd.Series(hbias, index=h.index)
    hbias[e.isna()] = np.nan
    return hbias.reindex(df.index, method="ffill").ffill()


def detect_swings(H: np.ndarray, L: np.ndarray, k: int = 3) -> tuple[np.ndarray, np.ndarray]:
    n = len(H)
    sh = np.full(n, np.nan)
    sl = np.full(n, np.nan)
    for i in range(k, n - k):
        if H[i] == np.max(H[i - k:i + k + 1]):
            sh[i] = H[i]
        if L[i] == np.min(L[i - k:i + k + 1]):
            sl[i] = L[i]
    return sh, sl


def smc_refined_signaller(df: pd.DataFrame, p: dict) -> list[Signal]:
    H = df["high"].to_numpy()
    L = df["low"].to_numpy()
    O = df["open"].to_numpy()
    C = df["close"].to_numpy()
    times = df.index
    n = len(df)

    k = int(p.get("swing_k", 3))
    atr_len = int(p.get("atr_len", 14))
    atr = ind.atr(df, atr_len).to_numpy()

    # HTF Bias
    bias_htf = p.get("bias_htf", "H4")
    bias_ema = int(p.get("bias_ema", 50))
    bias = htf_bias_series(df, bias_htf, bias_ema).to_numpy()

    raw_sh, raw_sl = detect_swings(H, L, k=k)
    sh_history: list[tuple[int, float]] = []
    sl_history: list[tuple[int, float]] = []

    active_obs: list[OrderBlock] = []
    signals: list[Signal] = []
    last_sig_idx = -10**9
    min_gap = int(p.get("min_gap", 4))
    max_ob_age = int(p.get("max_ob_age", 80))
    min_rr = float(p.get("min_rr", 1.5))
    max_rr = float(p.get("max_rr", 5.0))
    sl_buf_atr = float(p.get("sl_buf_atr", 0.10))

    for i in range(k * 2 + 10, n):
        check_idx = i - k
        if not np.isnan(raw_sh[check_idx]):
            sh_history.append((check_idx, raw_sh[check_idx]))
        if not np.isnan(raw_sl[check_idx]):
            sl_history.append((check_idx, raw_sl[check_idx]))

        if len(sh_history) > 40:
            sh_history = sh_history[-40:]
        if len(sl_history) > 40:
            sl_history = sl_history[-40:]

        current_bias = bias[i] if not np.isnan(bias[i]) else 0

        # Bullish BOS: Close > Swing High (Only if HTF Bias is Bullish or Neutral)
        if current_bias >= 0 and sh_history:
            recent_sh_idx, recent_sh_val = sh_history[-1]
            if C[i] > recent_sh_val and C[i - 1] <= recent_sh_val and (i - recent_sh_idx) >= 2:
                # Origin of impulse
                lookback = range(max(0, recent_sh_idx), i)
                if lookback:
                    base_idx = min(lookback, key=lambda idx: L[idx])
                    ob_bottom = float(L[base_idx])
                    ob_top = float(max(O[base_idx], C[base_idx]))
                    # Displacement check: ensure high moved at least 1.5 ATR from base
                    if H[i] - ob_bottom >= 1.2 * atr[i] and (ob_top - ob_bottom) <= 2.0 * atr[i]:
                        active_obs.append(OrderBlock(
                            dir=+1,
                            top=ob_top,
                            bottom=ob_bottom,
                            created_idx=base_idx,
                            created_ts=times[base_idx],
                            bos_level=recent_sh_val,
                            target=float(H[i])
                        ))

        # Bearish BOS: Close < Swing Low (Only if HTF Bias is Bearish or Neutral)
        if current_bias <= 0 and sl_history:
            recent_sl_idx, recent_sl_val = sl_history[-1]
            if C[i] < recent_sl_val and C[i - 1] >= recent_sl_val and (i - recent_sl_idx) >= 2:
                lookback = range(max(0, recent_sl_idx), i)
                if lookback:
                    base_idx = max(lookback, key=lambda idx: H[idx])
                    ob_top = float(H[base_idx])
                    ob_bottom = float(min(O[base_idx], C[base_idx]))
                    if ob_top - L[i] >= 1.2 * atr[i] and (ob_top - ob_bottom) <= 2.0 * atr[i]:
                        active_obs.append(OrderBlock(
                            dir=-1,
                            top=ob_top,
                            bottom=ob_bottom,
                            created_idx=base_idx,
                            created_ts=times[base_idx],
                            bos_level=recent_sl_val,
                            target=float(L[i])
                        ))

        # Update and prune OBs
        surviving: list[OrderBlock] = []
        for ob in active_obs:
            if (i - ob.created_idx) > max_ob_age:
                continue
            # Invalidation: price closes through distal edge
            if ob.dir == 1 and C[i] < ob.bottom:
                continue
            if ob.dir == -1 and C[i] > ob.top:
                continue
            # Track external target
            if ob.dir == 1:
                ob.target = max(ob.target, float(H[i]))
            else:
                ob.target = min(ob.target, float(L[i]))
            surviving.append(ob)
        active_obs = surviving

        # Check Inducement & Mitigation
        for ob in active_obs:
            if ob.mitigated:
                continue

            if ob.dir == 1:
                # Find inducement swing low formed after OB creation
                if ob.ind_level is None and sl_history:
                    for s_idx, s_val in reversed(sl_history):
                        if s_idx > ob.created_idx and s_val > ob.top:
                            ob.ind_level = s_val
                            break

                # Inducement sweep check
                if ob.ind_level is not None and not ob.swept:
                    if L[i] < ob.ind_level:
                        ob.swept = True

                # Entry trigger: price sweeps into OB and candle closes above ob.top (reclaiming it)
                # or price wicks into OB after sweep
                if ob.swept and not ob.mitigated:
                    # Low enters the OB zone
                    if L[i] <= ob.top and L[i] >= ob.bottom - 0.2 * atr[i]:
                        # Rejection confirmation: close in upper half and close >= open
                        if C[i] >= (H[i] + L[i]) / 2 and C[i] >= O[i] and (i - last_sig_idx) >= min_gap:
                            entry = float(C[i])
                            sl = ob.bottom - sl_buf_atr * atr[i]
                            sl_dist = entry - sl
                            if sl_dist > 0:
                                rr = (ob.target - entry) / sl_dist
                                if rr >= min_rr:
                                    target_rr = min(rr, max_rr)
                                    signals.append(Signal(
                                        ts=times[i],
                                        dir=+1,
                                        sl_offset=float(sl_dist),
                                        tp_r=float(target_rr),
                                        reason=f"SMC OB Retest + Inducement Sweep (RR {target_rr:.1f})",
                                        tp_price=float(ob.target)
                                    ))
                                    ob.mitigated = True
                                    last_sig_idx = i
                                    break

            elif ob.dir == -1:
                if ob.ind_level is None and sh_history:
                    for s_idx, s_val in reversed(sh_history):
                        if s_idx > ob.created_idx and s_val < ob.bottom:
                            ob.ind_level = s_val
                            break

                if ob.ind_level is not None and not ob.swept:
                    if H[i] > ob.ind_level:
                        ob.swept = True

                if ob.swept and not ob.mitigated:
                    if H[i] >= ob.bottom and H[i] <= ob.top + 0.2 * atr[i]:
                        if C[i] <= (H[i] + L[i]) / 2 and C[i] <= O[i] and (i - last_sig_idx) >= min_gap:
                            entry = float(C[i])
                            sl = ob.top + sl_buf_atr * atr[i]
                            sl_dist = sl - entry
                            if sl_dist > 0:
                                rr = (entry - ob.target) / sl_dist
                                if rr >= min_rr:
                                    target_rr = min(rr, max_rr)
                                    signals.append(Signal(
                                        ts=times[i],
                                        dir=-1,
                                        sl_offset=float(sl_dist),
                                        tp_r=float(target_rr),
                                        reason=f"SMC OB Retest + Inducement Sweep (RR {target_rr:.1f})",
                                        tp_price=float(ob.target)
                                    ))
                                    ob.mitigated = True
                                    last_sig_idx = i
                                    break

    return signals


if __name__ == "__main__":
    params = {
        "swing_k": 3,
        "atr_len": 14,
        "min_gap": 4,
        "max_ob_age": 80,
        "min_rr": 1.2,
        "max_rr": 4.0,
        "sl_buf_atr": 0.10,
        "bias_htf": "H4",
        "bias_ema": 50,
    }

    pairs = [("GBPUSD", "H1"), ("AUDUSD", "H1"), ("EURUSD", "H1"), ("XAUUSD", "M30")]
    for sym, tf in pairs:
        df = load_cached(sym, tf)
        if df is not None:
            p = dict(params)
            if tf == "M30":
                p["bias_htf"] = "H2"
            res = run_backtest(df, sym, tf, "smc_refined", smc_refined_signaller, p)
            print(res.summary())

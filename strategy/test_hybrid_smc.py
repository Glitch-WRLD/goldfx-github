"""Hybrid Institutional SMC + FVG Strategy.

Combines the production FVG-retest engine with Anthony Ikechukwu's 4 core rules:
1. The FVG must be born from a leg that triggered a Break of Structure (BOS) or MSS.
2. The retest must occur after an Inducement (minor swing) was formed and swept.
3. The zone must be unmitigated prior to the tap.
4. Clean structural SL placed behind the sweep wick.
"""

import sys
from pathlib import Path
sys.path.insert(0, 'C:/goldfx-agent')

import numpy as np
import pandas as pd
from strategy import indicators as ind
from strategy.backtester import Signal, run_backtest
from strategy.candidates import _bias_for, _smart_tp_price
from data.tv_data import load_cached


def smc_fvg_confluence_signaller(df: pd.DataFrame, p: dict) -> list[Signal]:
    H = df["high"].to_numpy()
    L = df["low"].to_numpy()
    O = df["open"].to_numpy()
    C = df["close"].to_numpy()
    n = len(df)
    times = df.index

    k = int(p.get("swing_k", 3))
    atr_len = int(p.get("atr_len", 14))
    atr = ind.atr(df, atr_len).to_numpy()
    bias = _bias_for(df, p).to_numpy()

    # 1. Causal swing highs/lows
    sh = np.full(n, np.nan)
    sl = np.full(n, np.nan)
    for i in range(k, n - k):
        if H[i] == np.max(H[i - k:i + k + 1]):
            sh[i] = H[i]
        if L[i] == np.min(L[i - k:i + k + 1]):
            sl[i] = L[i]

    sh_hist: list[tuple[int, float]] = []
    sl_hist: list[tuple[int, float]] = []

    # Active institutional zones: (dir, bot, top, created_idx, ind_level, swept)
    zones: list[dict] = []
    signals: list[Signal] = []
    last_sig = -10**9

    min_gap = int(p.get("min_gap", 4))
    max_age = int(p.get("fvg_max_age", 40))
    require_bos = bool(p.get("require_bos", True))
    require_sweep = bool(p.get("require_sweep", True))
    sl_buf = float(p.get("sl_buf", 0.20))
    tp_lookback = int(p.get("tp_lookback", 60))
    smart_tp = bool(p.get("smart_tp", True))

    for i in range(k * 2 + 10, n):
        # Update confirmed swings
        c_idx = i - k
        if not np.isnan(sh[c_idx]):
            sh_hist.append((c_idx, sh[c_idx]))
        if not np.isnan(sl[c_idx]):
            sl_hist.append((c_idx, sl[c_idx]))

        b = bias[i] if not np.isnan(bias[i]) else 0

        # Check for new FVG creation on bar i - 1 (formed between i-2 and i)
        # Bullish FVG: H[i-2] < L[i]
        if H[i - 2] < L[i] and b >= 0:
            bot, top = float(H[i - 2]), float(L[i])
            # Check Rule 1: Did this impulse trigger a BOS?
            has_bos = True
            if require_bos:
                # Did high break a prior confirmed swing high?
                has_bos = False
                if sh_hist:
                    for s_i, s_val in reversed(sh_hist[-10:]):
                        if s_i < i - 1 and H[i] > s_val:
                            has_bos = True
                            break
            if has_bos:
                zones.append({
                    "dir": +1, "bot": bot, "top": top,
                    "created": i, "ind": None, "swept": not require_sweep,
                    "anchor_sl": float(min(L[i - 2], L[i - 1]))
                })

        # Bearish FVG: L[i-2] > H[i]
        elif L[i - 2] > H[i] and b <= 0:
            bot, top = float(H[i]), float(L[i - 2])
            has_bos = True
            if require_bos:
                has_bos = False
                if sl_hist:
                    for s_i, s_val in reversed(sl_hist[-10:]):
                        if s_i < i - 1 and L[i] < s_val:
                            has_bos = True
                            break
            if has_bos:
                zones.append({
                    "dir": -1, "bot": bot, "top": top,
                    "created": i, "ind": None, "swept": not require_sweep,
                    "anchor_sl": float(max(H[i - 2], H[i - 1]))
                })

        # Prune expired or invalidated zones
        valid_zones: list[dict] = []
        for z in zones:
            if i - z["created"] > max_age:
                continue
            if z["dir"] == 1 and C[i] < z["bot"] - 0.2 * atr[i]:
                continue
            if z["dir"] == -1 and C[i] > z["top"] + 0.2 * atr[i]:
                continue
            valid_zones.append(z)
        zones = valid_zones

        # Check Inducement and Tap
        for z in list(zones):
            # Bullish check
            if z["dir"] == 1:
                # Find inducement low formed after zone creation
                if z["ind"] is None and sl_hist:
                    for s_i, s_val in reversed(sl_hist):
                        if s_i > z["created"] and s_val > z["top"]:
                            z["ind"] = s_val
                            break

                if z["ind"] is not None and not z["swept"]:
                    if L[i] < z["ind"]:
                        z["swept"] = True

                # Retest trigger: candle dips into zone and closes bullishly
                filled = L[i] <= z["top"] and L[i] >= z["bot"] - 0.2 * atr[i]
                trigger = C[i] > z["top"] and C[i] > O[i]

                if filled and trigger and z["swept"]:
                    if (i - last_sig) >= min_gap:
                        entry = float(C[i])
                        sl_level = z["bot"] - sl_buf * atr[i]
                        sl_off = entry - sl_level
                        if sl_off > 0:
                            tp_p = _smart_tp_price(df, i, +1, tp_lookback) if smart_tp else None
                            signals.append(Signal(
                                ts=times[i], dir=+1, sl_offset=float(sl_off),
                                tp_r=float(p.get("tp_r", 1.5)),
                                reason="SMC FVG Retest + Sweep", tp_price=tp_p
                            ))
                            last_sig = i
                            zones.remove(z)
                            break

            # Bearish check
            elif z["dir"] == -1:
                if z["ind"] is None and sh_hist:
                    for s_i, s_val in reversed(sh_hist):
                        if s_i > z["created"] and s_val < z["bot"]:
                            z["ind"] = s_val
                            break

                if z["ind"] is not None and not z["swept"]:
                    if H[i] > z["ind"]:
                        z["swept"] = True

                filled = H[i] >= z["bot"] and H[i] <= z["top"] + 0.2 * atr[i]
                trigger = C[i] < z["bot"] and C[i] < O[i]

                if filled and trigger and z["swept"]:
                    if (i - last_sig) >= min_gap:
                        entry = float(C[i])
                        sl_level = z["top"] + sl_buf * atr[i]
                        sl_off = sl_level - entry
                        if sl_off > 0:
                            tp_p = _smart_tp_price(df, i, -1, tp_lookback) if smart_tp else None
                            signals.append(Signal(
                                ts=times[i], dir=-1, sl_offset=float(sl_off),
                                tp_r=float(p.get("tp_r", 1.5)),
                                reason="SMC FVG Retest + Sweep", tp_price=tp_p
                            ))
                            last_sig = i
                            zones.remove(z)
                            break

    return signals


if __name__ == "__main__":
    from strategy.profiles import BAL_PROFILE
    base_params = dict(BAL_PROFILE["params"])

    print("=================== BASELINE (NO SMC CONFLUENCE) ===================")
    from strategy.candidates import STRATEGIES
    pairs = [("GBPUSD", "H1"), ("AUDUSD", "H1"), ("EURUSD", "H1"), ("XAUUSD", "M30")]
    for sym, tf in pairs:
        df = load_cached(sym, tf)
        if df is not None:
            p = dict(base_params)
            p["bias_htf"] = "H4" if tf == "H1" else "H1"
            res = run_backtest(df, sym, tf, "baseline", STRATEGIES["fvg_retest"].signaller, p)
            print(res.summary())

    print("\n=================== UPGRADED SMC CONFLUENCE (BOS + INDUCEMENT SWEEP) ===================")
    for sym, tf in pairs:
        df = load_cached(sym, tf)
        if df is not None:
            p = dict(base_params)
            p["bias_htf"] = "H4" if tf == "H1" else "H1"
            p["require_bos"] = True
            p["require_sweep"] = True
            p["min_rr"] = 0.8
            p["max_rr"] = 2.5
            res = run_backtest(df, sym, tf, "smc_fvg_confluence", smc_fvg_confluence_signaller, p)
            print(res.summary())

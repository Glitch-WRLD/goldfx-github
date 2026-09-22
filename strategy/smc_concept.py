"""Experimental encoding of the SMC (Smart-Money Concepts) entry context from
the tutor's transcripts, used ONLY for a head-to-head backtest versus the
production FVG-retest concept. Not wired into the live bot.

Tutor's method (distilled from transcripts 1-5):
1. Top-down analysis: the HTF decides the only tradable direction. Price is
   "coming from" the HTF structure and "going to" the opposite liquidity.
2. Liquidity sweep / protected eye: price takes out a recent structural
   high/low (stop-run = the "protected eye"), reclaims it, then breaks
   structure (BOS) in the bias direction = the manipulation/impulse.
3. POI: sits BETWEEN the protected counter-level and the structural liquidity
   target. Canonical POIs are order blocks / breaker (failed-order-block / QMR)
   and fair-value gaps. Broadly: after sweep+BOS, the impulse that broke
   structure leaves an imbalance (FVG) that price retests.
4. Confirmation candle: entry only on a retest of the POI that prints a
   reclaim/rejection candle.
5. SL: "safe place" - just beyond the protected/swept level (structural),
   NOT at an obvious stop cluster that market makers hunt.
6. TP: the next structural liquidity pool (next swing high/low), unclamped -
   the source of the tutor's claimed 2.6-14R.

Encoding: we reuse the production FVG-retest signal 1:1 (identical POI and
retest logic, identical smart-TP machinery in the backtester) and add the SMC
*context gate* as parameters:
  require_sweep_bos : only fire when price swept a confirmed swing extreme
                      (opposite side) and then broke structure (bias side)
                      within the last ``sweep_age`` bars.
  confirm_reject    : additionally demand a rejection wick on the trigger bar.
The gate + same machinery isolates exactly what the SMC teaching adds.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategy import indicators as ind


def _sweep_bos_context(df: pd.DataFrame, p: dict, dir_: int):
    """Causal boolean array: bar i is inside an active SMC context for
    ``dir_`` when, within the last ``sweep_age`` bars, price swept the most
    recent confirmed swing extreme on the OPPOSITE side, reclaimed it, and has
    since closed through structure on the bias side (break of structure)."""
    H = df["high"].to_numpy()
    L = df["low"].to_numpy()
    C = df["close"].to_numpy()
    A = ind.atr(df, p["atr_len"]).to_numpy()
    n = len(df)
    k = int(p.get("swing_k", 5))
    sweep_buf = float(p.get("sweep_buf_atr", 0.2))
    age = int(p.get("sweep_age", 40))

    sw_low = np.full(n, np.nan)
    sw_high = np.full(n, np.nan)
    cur_l = cur_h = np.nan
    for i in range(k, n):
        if i - k >= k:
            lo, hi = i - 2 * k, i - k
            if L[i - k] == L[lo:hi + 1].min():
                cur_l = L[i - k]
            if H[i - k] == H[lo:hi + 1].max():
                cur_h = H[i - k]
        sw_low[i] = cur_l
        sw_high[i] = cur_h

    ctx = np.zeros(n, dtype=bool)
    if dir_ == 1:
        swept = (L < sw_low - sweep_buf * A) & (C > sw_low)
        for s in np.where(swept)[0]:
            ref = sw_low[s]
            buf = sweep_buf * A[s]
            end = min(s + age, n)
            seg = C[s + 1:end]
            mask = seg > ref + buf
            if np.any(mask):
                bos = int(np.argmax(mask))
                ctx[s + 1 + bos:end] = True
    else:
        swept = (H > sw_high + sweep_buf * A) & (C < sw_high)
        for s in np.where(swept)[0]:
            ref = sw_high[s]
            buf = sweep_buf * A[s]
            end = min(s + age, n)
            seg = C[s + 1:end]
            mask = seg < ref - buf
            if np.any(mask):
                bos = int(np.argmax(mask))
                ctx[s + 1 + bos:end] = True
    return ctx


def smc_gated_signaller(df: pd.DataFrame, p: dict) -> list:
    """Signaller compatible with the backtester: HTF-bias gated, both
    directions, structural smart-TP machinery reused from production."""
    from strategy.candidates import _bias_for, _smart_tp_price
    from strategy.backtester import Signal
    from strategy.filters import fvg_retest_signal

    atr = ind.atr(df, p["atr_len"])
    c = df["close"]
    bias = _bias_for(df, p)
    long_only = bool(p.get("long_only", False))
    tgt_look = int(p.get("tp_lookback", 60))
    require_sweep_bos = bool(p.get("require_sweep_bos", False))
    confirm_reject = bool(p.get("confirm_reject", False))
    idx_by_ts = {ts: i for i, ts in enumerate(df.index)}

    if require_sweep_bos:
        ctxL = _sweep_bos_context(df, p, +1)
        ctxS = _sweep_bos_context(df, p, -1)

    sigs: list = []

    # longs: run plain FVG, then keep only signals satisfying bias + SMC context
    for dir_, ctx, dfm in (
        (+1, ctxL if require_sweep_bos else None, bias <= 0),
        (-1, ctxS if require_sweep_bos else None, bias >= 0),
    ):
        if dir_ == -1 and long_only:
            continue
        d = df.copy()
        d.loc[dfm, ["high", "low", "close", "open"]] = np.nan
        res = fvg_retest_signal(d, p, dir_, atr, c, df.index, p["min_gap"], 4000)
        for ts, sl, reason in res:
            i = idx_by_ts.get(ts)
            if i is None:
                continue
            if ctx is not None and not ctx[i]:
                continue
            if confirm_reject:
                Hc = df["high"].to_numpy()
                Lc = df["low"].to_numpy()
                Oc = df["open"].to_numpy()
                Cc = c.to_numpy()
                rng = Hc[i] - Lc[i]
                if dir_ == 1:
                    wick = min(Oc[i], Cc[i]) - Lc[i]
                    ok = rng > 0 and wick / rng >= float(p.get("reject_frac", 0.35))
                else:
                    wick = Hc[i] - max(Oc[i], Cc[i])
                    ok = rng > 0 and wick / rng >= float(p.get("reject_frac", 0.35))
                if not ok:
                    continue
            tp_price = _smart_tp_price(df, i, dir_, tgt_look) if p.get("smart_tp") else None
            sigs.append(Signal(ts, dir_, sl, p["tp_r"], reason, tp_price))
    return sigs


def smc_sweep_signaller(df: pd.DataFrame, p: dict) -> list:
    """Production Institutional SMC Sweep signaller based on Anthony Ikechukwu's 4 Rules.
    
    1. Causal swing highs & lows (k=2).
    2. FVG creation aligned with HTF bias.
    3. Inducement (ind) swing formation & sweep requirement prior to POI mitigation.
    4. Rejection & reclaim trigger.
    """
    from strategy.backtester import Signal
    from strategy.candidates import _bias_for, _smart_tp_price

    H = df["high"].to_numpy()
    L = df["low"].to_numpy()
    O = df["open"].to_numpy()
    C = df["close"].to_numpy()
    n = len(df)
    times = df.index

    k = int(p.get("swing_k", 2))
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

    zones: list[dict] = []
    signals: list[Signal] = []
    last_sig = -10**9

    min_gap = int(p.get("min_gap", 4))
    max_age = int(p.get("fvg_max_age", 40))
    require_bos = bool(p.get("require_bos", False))
    require_sweep = bool(p.get("require_sweep", True))
    sl_buf = float(p.get("sl_buf", 0.20))
    tp_lookback = int(p.get("tp_lookback", 60))
    smart_tp = bool(p.get("smart_tp", True))

    for i in range(k * 2 + 10, n):
        c_idx = i - k
        if not np.isnan(sh[c_idx]):
            sh_hist.append((c_idx, sh[c_idx]))
        if not np.isnan(sl[c_idx]):
            sl_hist.append((c_idx, sl[c_idx]))

        b = bias[i] if not np.isnan(bias[i]) else 0

        # Bullish FVG
        if H[i - 2] < L[i] and b >= 0:
            bot, top = float(H[i - 2]), float(L[i])
            has_bos = True
            if require_bos:
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

        # Bearish FVG
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
            if z["dir"] == 1:
                if z["ind"] is None and sl_hist:
                    for s_i, s_val in reversed(sl_hist):
                        if s_i > z["created"] and s_val > z["top"]:
                            z["ind"] = s_val
                            break

                if z["ind"] is not None and not z["swept"]:
                    if L[i] < z["ind"]:
                        z["swept"] = True

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
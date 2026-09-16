"""Market-quality filters shared by strategies: higher-timeframe bias and
fair-value-gap (imbalance) detection. FVG scan is numpy-based for speed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategy import indicators as ind


def htf_bias(df: pd.DataFrame, htf: str = "H1", ema_len: int = 50) -> pd.Series:
    """Resample ``df`` (any intraday TF) to ``htf`` and return a forward-filled
    bias on the low-TF index: +1 close>EMA else -1, 0 during warmup."""
    rule = {"H1": "1h", "H2": "2h", "H4": "4h"}.get(htf, htf)
    h = df.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"})
    h = h.dropna()
    e = ind.ema(h["close"], ema_len)
    hbias = np.where(h["close"] > e, 1.0, -1.0)
    hbias = pd.Series(hbias, index=h.index)
    hbias[e.isna()] = np.nan
    fb = hbias.reindex(df.index, method="ffill").ffill()
    return fb


def fvg_retest_signal(df: pd.DataFrame, p: dict, dir_: int, atr: pd.Series,
                      close: pd.Series, times: pd.Index,
                      min_gap: int, max_signals: int) -> list:
    """Scan for fair-value-gap taps aligned with ``dir_`` (+1/-1).

    Returns list of (ts, sl_offset, reason).
    """
    H = df["high"].to_numpy()
    L = df["low"].to_numpy()
    O = df["open"].to_numpy()
    C = close.to_numpy()
    A = atr.to_numpy()
    n = len(df)
    zones: list[tuple[float, float, int]] = []  # (bottom, top, created_idx)
    out: list = []
    last_hit: dict[int, int] = {}
    max_age = int(p.get("fvg_max_age", 30))
    min_body = float(p.get("min_body_atr", 0.0))
    confirm = bool(p.get("confirm"))
    buf = 0.1

    for i in range(2, n):
        if dir_ == 1:
            if H[i - 2] < L[i]:
                zones.append((float(H[i - 2]), float(L[i]), i))
        elif L[i - 2] > H[i]:
            zones.append((float(H[i]), float(L[i - 2]), i))
        # prune stale zones
        zones = [z for z in zones if i - z[2] <= max_age]

        for z in zones:
            bot, top, created = z
            if dir_ == 1:
                filled = L[i - 1] <= top and L[i - 1] >= bot - buf * A[i]
                trigger = C[i] > top and C[i] > O[i]
            else:
                filled = H[i - 1] >= bot and H[i - 1] <= top + buf * A[i]
                trigger = C[i] < bot and C[i] < O[i]
            if min_body > 0:
                trigger = trigger and (C[i] - O[i]) * dir_ > min_body * A[i]
            if filled and trigger:
                key = created
                if last_hit.get(key, -10**9) != i and (i - last_hit.get(key, -10**9)) >= min_gap:
                    ts_i, ref = times[i], i
                    if confirm:
                        if i + 1 < n and (C[i + 1] - O[i + 1]) * dir_ > 0:
                            ts_i, ref = times[i + 1], i + 1
                        else:
                            trigger = False
                    if trigger:
                        sb = float(p.get("sl_buf", 0.2))
                        lo_edge = bot - sb * A[i] if dir_ == 1 else top + sb * A[i]
                        sl_off = abs(C[ref] - lo_edge)
                        if sl_off > 0:
                            out.append((ts_i, float(sl_off),
                                        "FVG retest" if dir_ == 1 else "FVG retest(sh)"))
                            last_hit[key] = i
                            zones.remove(z)
                break
        if len(out) >= max_signals:
            break
    return out
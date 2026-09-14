"""Candidate trading strategies.

Each signaller is a pure function: (df, params) -> list[Signal], decided on bar
closes. Signals are evaluated by the backtester with no lookahead (entry next
bar open). All indicators are causal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import pandas as pd

from strategy import indicators as ind
from strategy.backtester import Signal

Signaller = Callable[[pd.DataFrame, dict], list[Signal]]


def _gap_select(cond: pd.Series, min_gap: int,
                close: pd.Series, df: pd.DataFrame,
                atr: pd.Series, dir_: int,
                sl_atr: float, tp_r: float,
                reason: str, max_signals: int = 4000,
                skip_first: int = 60) -> list[Signal]:
    """Pick signal bars from a boolean condition, enforcing minimum gap and a
    lookback for SL distance."""
    out: list[Signal] = []
    last: int = -10**9
    times = df.index
    for i in range(skip_first, len(cond)):
        if not bool(cond.iloc[i]):
            continue
        if i - last < min_gap:
            continue
        sl_hint = atr.iloc[i] * sl_atr
        if sl_hint <= 0 or not np.isfinite(sl_hint):
            continue
        out.append(Signal(ts=times[i], dir=dir_, sl_offset=sl_hint, tp_r=tp_r, reason=reason))
        last = i
        if len(out) >= max_signals:
            break
    return out


@dataclass
class Strategy:
    key: str
    name: str
    logic: str
    signaller: Signaller
    param_grid: list[dict]


def _smart_tp_price(df: pd.DataFrame, i: int, dir_: int, lookback: int = 60,
                    swing: int = 5) -> float | None:
    """Structural TP: nearest swing high above (long) / swing low below (short)
    inside the prior ``lookback`` window."""
    lo, hi = i - lookback, i + 1
    if lo < 0:
        lo = 0
    if dir_ == 1:
        target = df["high"].iloc[lo:i].max()
        return float(target) if target > df["close"].iloc[i] else None
    target = df["low"].iloc[lo:i].min()
    return float(target) if target < df["close"].iloc[i] else None


def _pair_bias(symbol: str | None, p: dict) -> str:
    htf = p.get("bias_htf", "H1")
    if p.get("bias_htf_by_pair") and symbol == "EURUSD":
        htf = p.get("eur_bias_htf", "H2")
    return htf


def _bias_for(df: pd.DataFrame, p: dict) -> pd.Series:
    from strategy.filters import htf_bias
    htf = _pair_bias(p.get("_symbol"), p)
    return htf_bias(df, htf, p.get("bias_ema", 50))


def _signalize(cond: pd.Series, sl_dist: pd.Series, tp_r: float, min_gap: int,
               dir_: int, reason: str, times: pd.Index, max_signals: int = 4000,
               skip_first: int = 60,
               tp_prices: pd.Series | None = None) -> list[Signal]:
    out: list[Signal] = []
    last = -10**9
    n = len(cond)
    for i in range(skip_first, n):
        if not bool(cond.iloc[i]):
            continue
        if i - last < min_gap:
            continue
        d = sl_dist.iloc[i]
        if d != d or d <= 0 or not np.isfinite(d):
            continue
        tp_price = float(tp_prices.iloc[i]) if tp_prices is not None and tp_prices.notna().iloc[i] else None
        out.append(Signal(ts=times[i], dir=dir_, sl_offset=float(d), tp_r=tp_r,
                          reason=reason, tp_price=tp_price))
        last = i
        if len(out) >= max_signals:
            break
    return out


def _ema_pullback(df: pd.DataFrame, p: dict) -> list[Signal]:
    c = df["close"]
    atr = ind.atr(df, p["atr_len"])
    e21 = ind.ema(c, p["ema_fast"])
    e50 = ind.ema(c, p["ema_slow"])
    r = ind.rsi(c, p["rsi_len"])
    body = c - df["open"]
    lo = df["low"]
    hi = df["high"]

    if p.get("use_bias"):
        bias = _bias_for(df, p)
    else:
        bias = pd.Series(np.nan, index=df.index)

    ema_long = e21 > e50
    ema_short = e21 < e50
    pb_long = (c.shift(1) <= e21.shift(1)) & (c > e21) & (body > 0)
    pb_short = (c.shift(1) >= e21.shift(1)) & (c < e21) & (body < 0)
    mom_long = (r > p["rsi_fast_floor"]) & (r.shift(1) <= p["rsi_fast_floor"])
    mom_short = (r < 100 - p["rsi_fast_floor"]) & (r.shift(1) >= 100 - p["rsi_fast_floor"])
    not_overbought = r < p["rsi_cap"]
    not_oversold = r > 100 - p["rsi_cap"]
    # momentum-quality: the reclaim bar must have a real body (not a doji)
    body_atr = body.abs() / atr
    strong_body = body_atr > p.get("min_body_atr", 0.2)

    b_lt = (bias > 0) | bias.isna()
    b_st = (bias < 0) | bias.isna()

    cond_long = ema_long & pb_long & mom_long & not_overbought & b_lt & strong_body
    cond_short = ema_short & pb_short & mom_short & not_oversold & b_st & strong_body

    if p.get("sl_mode", "atr") == "low":
        sl_long = (c - lo.rolling(2).min()) + p.get("sl_buf", 0.2) * atr
        sl_short = (hi.rolling(2).max() - c) + p.get("sl_buf", 0.2) * atr
        sl_long = sl_long.clip(lower=p.get("sl_min_atr", 0.7) * atr)
        sl_short = sl_short.clip(lower=p.get("sl_min_atr", 0.7) * atr)
        if p.get("smart_tp"):
            tpL = pd.Series([_smart_tp_price(df, i, +1) for i in range(len(df))], index=df.index)
            tpS = pd.Series([_smart_tp_price(df, i, -1) for i in range(len(df))], index=df.index)
        else:
            tpL = tpS = None
        sigs = _signalize(cond_long, sl_long, p["tp_r"], p["min_gap"], +1,
                          "EMA21 reclaim (smart TP)", df.index, tp_prices=tpL)
        sigs += _signalize(cond_short, sl_short, p["tp_r"], p["min_gap"], -1,
                           "EMA21 reject (smart TP)", df.index, tp_prices=tpS)
        return sigs

    sigs = _gap_select(cond_long, p["min_gap"], c, df, atr, +1, p["sl_atr"], p["tp_r"],
                       "EMA21 pullback + RSI50 reclaim")
    sigs += _gap_select(cond_short, p["min_gap"], c, df, atr, -1, p["sl_atr"], p["tp_r"],
                        "EMA21 pullback + RSI50 break")
    return sigs


def _fvg_retest(df: pd.DataFrame, p: dict) -> list[Signal]:
    """Fair-value-gap continuation with HTF-bias alignment.

    ``long_only=True``: longs only (bullish bias). ``long_only=False``:
    BOTH directions - longs on bullish bias and shorts on bearish bias.
    """
    from strategy.filters import fvg_retest_signal
    c = df["close"]
    atr = ind.atr(df, p["atr_len"])
    bias = _bias_for(df, p)

    sigs: list[Signal] = []
    long_only = bool(p.get("long_only", True))

    # longs: FVG retests only while HTF bias is bullish
    df_l = df.copy()
    df_l.loc[bias <= 0, ["high", "low", "close", "open"]] = np.nan
    res = fvg_retest_signal(df_l, p, +1, atr, c, df.index, p["min_gap"], 4000)
    for ts, sl, reason in res:
        i = df.index.get_loc(ts)
        tp_price = _smart_tp_price(df, i, +1, p.get("tp_lookback", 60)) if p.get("smart_tp") else None
        sigs.append(Signal(ts, +1, sl, p["tp_r"], reason, tp_price))

    # shorts: FVG retests only while HTF bias is bearish
    if not long_only:
        df_s = df.copy()
        df_s.loc[bias >= 0, ["high", "low", "close", "open"]] = np.nan
        res = fvg_retest_signal(df_s, p, -1, atr, c, df.index, p["min_gap"], 2000)
        for ts, sl, reason in res:
            i = df.index.get_loc(ts)
            tp_price = _smart_tp_price(df, i, -1, p.get("tp_lookback", 60)) if p.get("smart_tp") else None
            sigs.append(Signal(ts, -1, sl, p["tp_r"], reason, tp_price))
    return sigs


def _swing_pullback(df: pd.DataFrame, p: dict) -> list[Signal]:
    """Swing structure bounce: trade bounces off recent swing high/low with
    HTF-EMA bias (vectorised)."""
    c = df["close"]
    atr = ind.atr(df, p["atr_len"])
    e50 = ind.ema(c, p["ema_slow"])
    r = ind.rsi(c, p["rsi_len"])
    window = p["lookback"]
    hi = df["high"]
    lo = df["low"]
    roll_min = lo.rolling(window, min_periods=1).min().shift(1)  # prior-window low
    roll_max = hi.rolling(window, min_periods=1).max().shift(1)  # prior-window high

    bull = c > e50
    bear = c < e50
    near_low = lo <= roll_min + p["touch_atr"] * atr
    reclaim = (c > hi.shift(1)) & (c > df["open"])
    near_hi = hi >= roll_max - p["touch_atr"] * atr
    reject = (c < lo.shift(1)) & (c < df["open"])

    touch_buff = 0.2 * atr
    cond_long = bull & near_low & reclaim & (r > p["rsi_floor"])
    cond_short = bear & near_hi & reject & (r < 100 - p["rsi_floor"])

    # SL: distance from close to prior-window extreme, or ATR floor
    sl_long = (c - roll_min).clip(lower=atr * p["sl_atr"]) + touch_buff
    sl_short = (roll_max - c).clip(lower=atr * p["sl_atr"]) + touch_buff

    out: list[Signal] = []
    last_long = last_short = -10**9
    times = df.index
    n = len(df)
    for i in range(60, n):
        if bool(cond_long.iloc[i]) and i - last_long >= p["min_gap"]:
            out.append(Signal(times[i], +1, float(sl_long.iloc[i]), p["tp_r"], "bounce @ swing low (bull bias)"))
            last_long = i
        elif bool(cond_short.iloc[i]) and i - last_short >= p["min_gap"]:
            out.append(Signal(times[i], -1, float(sl_short.iloc[i]), p["tp_r"], "rejection @ swing high (bear bias)"))
            last_short = i
        if len(out) > 4000:
            break
    return out


def _breakout_retest(df: pd.DataFrame, p: dict) -> list[Signal]:
    """Consolidation breakout with measured-move target (vectorised)."""
    atr = ind.atr(df, p["atr_len"])
    c = df["close"]
    k = p["range_n"]
    range_hi = df["high"].rolling(k, min_periods=k).max().shift(1)
    range_lo = df["low"].rolling(k, min_periods=k).min().shift(1)
    width = range_hi - range_lo
    squeeze = width < p["squeeze_atr"] * atr
    body = (c - df["open"]).abs()
    long_bo = squeeze & (c > range_hi) & (body > p["body_frac"] * width)
    short_bo = squeeze & (c < range_lo) & (body > p["body_frac"] * width)

    risk_long = c - (range_lo - 0.25 * atr)
    risk_short = (range_hi + 0.25 * atr) - c
    tp_r_long = (p["tp_r"] * width / risk_long).clip(upper=p["tp_r"] * 3)
    tp_r_short = (p["tp_r"] * width / risk_short).clip(upper=p["tp_r"] * 3)

    out: list[Signal] = []
    last = -10**9
    times = df.index
    for i in range(60, len(df)):
        if (bool(long_bo.iloc[i]) or bool(short_bo.iloc[i])) and (i - last) < p["min_gap"]:
            continue
        if bool(long_bo.iloc[i]) and risk_long.iloc[i] > 0:
            out.append(Signal(times[i], +1, float(risk_long.iloc[i]), float(tp_r_long.iloc[i]), "breakout long"))
            last = i
        elif bool(short_bo.iloc[i]) and risk_short.iloc[i] > 0:
            out.append(Signal(times[i], -1, float(risk_short.iloc[i]), float(tp_r_short.iloc[i]), "breakout short"))
            last = i
        if len(out) > 4000:
            break
    return out


def _mean_reversion(df: pd.DataFrame, p: dict) -> list[Signal]:
    """Bollinger-band + RSI extreme mean reversion, only in low-ADX regimes."""
    c = df["close"]
    atr = ind.atr(df, p["atr_len"])
    mid, up, lo = ind.bollinger(c, p["bb_len"], p["bb_std"])
    r = ind.rsi(c, p["rsi_len"])
    ad = ind.adx(df, p["adx_len"])

    ext_long = (r < p["rsi_ext"]) & (df["close"] <= lo) & (c > df["open"]) & (ad < p["adx_max"])
    ext_short = (r > 100 - p["rsi_ext"]) & (df["close"] >= up) & (c < df["open"]) & (ad < p["adx_max"])

    sigs = _gap_select(ext_long, p["min_gap"], c, df, atr, +1, p["sl_atr"], p["tp_r"],
                       "oversold BB/RSI fade")
    sigs += _gap_select(ext_short, p["min_gap"], c, df, atr, -1, p["sl_atr"], p["tp_r"],
                        "overbought BB/RSI fade")
    return sigs


def _trend_slope(df: pd.DataFrame, p: dict) -> list[Signal]:
    """Trend continuation on strong ADX + MACD momentum, pullback to fast EMA."""
    c = df["close"]
    atr = ind.atr(df, p["atr_len"])
    e9 = ind.ema(c, p["ema_fast"])
    e21 = ind.ema(c, p["ema_mid"])
    ad = ind.adx(df, p["adx_len"])
    macd_line, sig_line, hist = ind.macd(c)

    pb_long = (df["low"].shift(1) <= e9.shift(1)) & (c > e9) & (hist > 0) & (hist.shift(1) <= hist) & \
              (e9 > e21) & (ad > p["adx_min"])
    pb_short = (df["high"].shift(1) >= e9.shift(1)) & (c < e9) & (hist < 0) & (hist.shift(1) >= hist) & \
               (e9 < e21) & (ad > p["adx_min"])

    sigs = _gap_select(pb_long, p["min_gap"], c, df, atr, +1, p["sl_atr"], p["tp_r"],
                       "ADX trend continuation")
    sigs += _gap_select(pb_short, p["min_gap"], c, df, atr, -1, p["sl_atr"], p["tp_r"],
                        "ADX trend continuation")
    return sigs


STRATEGIES: dict[str, Strategy] = {
    "ema_pullback": Strategy(
        key="ema_pullback",
        name="EMA-Pullback Momentum",
        logic="HTF EMAs stacked; price pulls back through EMA(21) and reclaims it with a bullish/bearish "
              "candle while RSI flips back through 50.",
        signaller=_ema_pullback,
        param_grid=[
            {"ema_fast": 21, "ema_slow": 50, "rsi_len": 14, "rsi_fast_floor": 50, "rsi_cap": 72,
             "tp_r": 1.0, "atr_len": 14, "min_gap": 4, "use_bias": True, "bias_htf": "H1",
             "bias_ema": 50, "bias_htf_by_pair": True, "eur_bias_htf": "H2",
             "sl_mode": "low", "sl_buf": 0.2, "sl_min_atr": 0.7, "min_body_atr": 0.3,
             "smart_tp": True, "min_rr": 0.6, "max_rr": 1.5},
            {"ema_fast": 21, "ema_slow": 50, "rsi_len": 14, "rsi_fast_floor": 50, "rsi_cap": 72,
             "tp_r": 1.0, "atr_len": 14, "min_gap": 4, "use_bias": True, "bias_htf": "H1",
             "bias_ema": 50, "bias_htf_by_pair": True, "eur_bias_htf": "H2",
             "sl_mode": "low", "sl_buf": 0.2, "sl_min_atr": 0.7, "min_body_atr": 0.4,
             "smart_tp": True, "min_rr": 0.5, "max_rr": 1.0},
            {"ema_fast": 21, "ema_slow": 50, "rsi_len": 14, "rsi_fast_floor": 50, "rsi_cap": 72,
             "tp_r": 1.5, "atr_len": 14, "min_gap": 4, "use_bias": True, "bias_htf": "H1",
             "bias_ema": 50, "bias_htf_by_pair": True, "eur_bias_htf": "H2",
             "sl_mode": "low", "sl_buf": 0.2, "sl_min_atr": 0.7, "min_body_atr": 0.3,
             "smart_tp": True, "min_rr": 0.7, "max_rr": 2.0},
            {"ema_fast": 21, "ema_slow": 50, "rsi_len": 14, "rsi_fast_floor": 50, "rsi_cap": 70,
             "sl_atr": 1.6, "tp_r": 1.0, "atr_len": 14, "min_gap": 3, "use_bias": True,
             "bias_htf": "H1", "bias_ema": 50, "bias_htf_by_pair": True, "eur_bias_htf": "H2",
             "min_body_atr": 0.2},
        ],
    ),
    "fvg_retest": Strategy(
        key="fvg_retest",
        name="Imbalance (FVG) Retest",
        logic="Fair-value gaps in the direction of HTF bias, filled and reclaimed.",
        signaller=_fvg_retest,
        param_grid=[
            {"atr_len": 14, "fvg_lookback": 3, "fvg_max_age": 30, "min_gap": 4,
             "tp_r": 1.0, "bias_htf": "H1", "bias_ema": 50, "long_only": True,
             "bias_htf_by_pair": True, "eur_bias_htf": "H2", "smart_tp": True,
             "min_rr": 0.6, "max_rr": 1.5, "tp_lookback": 60},
            {"atr_len": 14, "fvg_lookback": 3, "fvg_max_age": 30, "min_gap": 4,
             "tp_r": 1.0, "bias_htf": "H1", "bias_ema": 50, "long_only": False,
             "bias_htf_by_pair": True, "eur_bias_htf": "H2", "smart_tp": True,
             "min_rr": 0.5, "max_rr": 1.0, "tp_lookback": 60},
            {"atr_len": 14, "fvg_lookback": 4, "fvg_max_age": 40, "min_gap": 5,
             "tp_r": 1.5, "bias_htf": "H1", "bias_ema": 50, "long_only": True,
             "bias_htf_by_pair": True, "eur_bias_htf": "H2", "smart_tp": True,
             "min_rr": 0.7, "max_rr": 2.0, "tp_lookback": 80},
            {"atr_len": 14, "fvg_lookback": 3, "fvg_max_age": 40, "min_gap": 5,
             "tp_r": 1.0, "bias_htf": "H1", "bias_ema": 50, "long_only": True,
             "bias_htf_by_pair": True, "eur_bias_htf": "H2", "smart_tp": False},
        ],
    ),
    "swing_pullback": Strategy(
        key="swing_pullback",
        name="Swing-Structure Pullback",
        logic="Rides bounces off recent swing highs/lows while price is on the right side of EMA(50).",
        signaller=_swing_pullback,
        param_grid=[
            {"ema_slow": 50, "rsi_len": 14, "rsi_floor": 40, "touch_atr": 0.4,
             "sl_atr": 1.5, "tp_r": 1.0, "atr_len": 14, "min_gap": 3, "lookback": 12},
            {"ema_slow": 50, "rsi_len": 14, "rsi_floor": 40, "touch_atr": 0.5,
             "sl_atr": 1.5, "tp_r": 1.5, "atr_len": 14, "min_gap": 3, "lookback": 12},
            {"ema_slow": 50, "rsi_len": 14, "rsi_floor": 45, "touch_atr": 0.6,
             "sl_atr": 1.5, "tp_r": 2.0, "atr_len": 14, "min_gap": 4, "lookback": 10},
        ],
    ),
    "breakout_retest": Strategy(
        key="breakout_retest",
        name="Squeeze Breakout",
        logic="Tight consolidation expansion; target measured move of the range.",
        signaller=_breakout_retest,
        param_grid=[
            {"range_n": 12, "squeeze_atr": 1.1, "body_frac": 0.4, "sl_atr": 1.5,
             "tp_r": 1.0, "atr_len": 14, "min_gap": 4},
            {"range_n": 12, "squeeze_atr": 1.3, "body_frac": 0.5, "sl_atr": 1.5,
             "tp_r": 1.5, "atr_len": 14, "min_gap": 4},
            {"range_n": 16, "squeeze_atr": 1.3, "body_frac": 0.5, "sl_atr": 1.5,
             "tp_r": 2.0, "atr_len": 14, "min_gap": 5},
        ],
    ),
    "mean_reversion": Strategy(
        key="mean_reversion",
        name="BB/RSI Mean-Reversion",
        logic="Fade extended extremes back toward the mean in quiet regimes.",
        signaller=_mean_reversion,
        param_grid=[
            {"bb_len": 20, "bb_std": 2.0, "rsi_len": 14, "rsi_ext": 30, "adx_len": 14,
             "adx_max": 25, "sl_atr": 1.5, "tp_r": 1.0, "atr_len": 14, "min_gap": 3},
            {"bb_len": 20, "bb_std": 2.0, "rsi_len": 14, "rsi_ext": 28, "adx_len": 14,
             "adx_max": 28, "sl_atr": 1.5, "tp_r": 1.5, "atr_len": 14, "min_gap": 3},
            {"bb_len": 20, "bb_std": 2.2, "rsi_len": 14, "rsi_ext": 25, "adx_len": 14,
             "adx_max": 30, "sl_atr": 1.5, "tp_r": 2.0, "atr_len": 14, "min_gap": 4},
        ],
    ),
    "trend_slope": Strategy(
        key="trend_slope",
        name="ADX Trend Continuation",
        logic="Strong ADX trend, MACD histogram rolling over, price tags the fast EMA.",
        signaller=_trend_slope,
        param_grid=[
            {"ema_fast": 9, "ema_mid": 21, "adx_len": 14, "adx_min": 22,
             "sl_atr": 1.6, "tp_r": 1.0, "atr_len": 14, "min_gap": 3},
            {"ema_fast": 9, "ema_mid": 21, "adx_len": 14, "adx_min": 25,
             "sl_atr": 1.6, "tp_r": 1.5, "atr_len": 14, "min_gap": 3},
            {"ema_fast": 9, "ema_mid": 21, "adx_len": 14, "adx_min": 28,
             "sl_atr": 1.6, "tp_r": 2.0, "atr_len": 14, "min_gap": 4},
        ],
    ),
}


def get_signaller(key: str) -> Optional[Signaller]:
    s = STRATEGIES.get(key)
    return s.signaller if s else None


def available_strategies() -> list[str]:
    return list(STRATEGIES)
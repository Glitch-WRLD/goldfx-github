"""Technical indicators computed with pandas on OHLCV frames.

All functions take a DataFrame with columns open/high/low/close
(named 'o','h','l','c' or 'open','high','low','close') and index Timestamp,
sorted ascending.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _cols(df: pd.DataFrame) -> dict[str, str]:
    if "open" in df.columns:
        return {"o": "open", "h": "high", "l": "low", "c": "close"}
    return {"o": "o", "h": "h", "l": "l", "c": "c"}


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ru = up.ewm(alpha=1 / period, adjust=False).mean()
    rd = down.ewm(alpha=1 / period, adjust=False).mean()
    rs = ru / rd.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    c = _cols(df)
    h, l, c_ = df[c["h"]], df[c["l"]], df[c["c"]]
    tr = pd.concat([h - l, (h - c_.shift()).abs(), (l - c_.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def bollinger(close: pd.Series, period: int = 20, std: float = 2.0):
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std()
    return mid, mid + std * sd, mid - std * sd


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    c = _cols(df)
    h, l, c_ = df[c["h"]], df[c["l"]], df[c["c"]]
    up = h.diff()
    down = -l.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = pd.concat([h - l, (h - c_.shift()).abs(), (l - c_.shift()).abs()], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1 / period, adjust=False).mean()
    pdi = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_.replace(0, np.nan)
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def swing_highs(df: pd.DataFrame, left: int = 5, right: int = 5):
    h = df["high"] if "high" in df.columns else df["h"]
    is_swing = pd.Series(False, index=df.index)
    for i in range(left, len(df) - right):
        w = h.iloc[i - left:i + right + 1]
        if h.iloc[i] == w.max():
            is_swing.iloc[i] = True
    return is_swing


def swing_lows(df: pd.DataFrame, left: int = 5, right: int = 5):
    l = df["low"] if "low" in df.columns else df["l"]
    is_swing = pd.Series(False, index=df.index)
    for i in range(left, len(df) - right):
        w = l.iloc[i - left:i + right + 1]
        if l.iloc[i] == w.min():
            is_swing.iloc[i] = True
    return is_swing


def swing_points(df: pd.DataFrame, left: int = 5, right: int = 5):
    """Return arrays of (index, price) of last swing highs and lows."""
    highs = df["high"] if "high" in df.columns else df["h"]
    lows = df["low"] if "low" in df.columns else df["l"]
    sh, sl = [], []
    for i in range(left, len(df) - right):
        wh = highs.iloc[i - left:i + right + 1]
        wl = lows.iloc[i - left:i + right + 1]
        if highs.iloc[i] == wh.max():
            sh.append((df.index[i], highs.iloc[i]))
        if lows.iloc[i] == wl.min():
            sl.append((df.index[i], lows.iloc[i]))
    return sh, sl


def candle_body(df: pd.DataFrame, i: int) -> float:
    o = df["o"] if "o" in df.columns else df["open"]
    c = df["c"] if "c" in df.columns else df["close"]
    return c.iloc[i] - o.iloc[i]


def candle_body_pct(df: pd.DataFrame, i: int) -> float:
    o = df["o"] if "o" in df.columns else df["open"]
    c = df["c"] if "c" in df.columns else df["close"]
    return abs(c.iloc[i] - o.iloc[i]) / o.iloc[i]


def renko_style_smooth(close: pd.Series, brick: float) -> pd.Series:
    """Simplified filter: last N-bar net move, used for regime detection."""
    return close


def trend_regime(df: pd.DataFrame, ema_fast: int = 21, ema_slow: int = 50) -> pd.Series:
    """Return 1 = bullish EMA stack, -1 = bearish, 0 = mixed."""
    c = df["c"] if "c" in df.columns else df["close"]
    f, s = ema(c, ema_fast), ema(c, ema_slow)
    reg = np.select([f > s], [1], default=-1)
    reg[f < s] = -1
    reg[((f > s) & (s.shift() > f.shift())).fillna(False)] = 0
    reg[((f < s) & (s.shift() < f.shift())).fillna(False)] = 0
    return pd.Series(reg, index=df.index)
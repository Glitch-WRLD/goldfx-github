"""TradingView OHLCV data layer.

Fetches historical intraday candles for XAU/USD and EUR/USD straight from
TradingView's public (anonymous) chart-session websocket via the
``tradingview-sdk`` package, and caches them locally as parquet.
"""

from __future__ import annotations

import asyncio
import threading
import sys
from pathlib import Path

import pandas as pd
from tradingview_sdk import AsyncTradingView, Interval

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import DATA_CACHE, SYMBOLS, TIMEFRAMES

_INTERVAL = {
    "M15": Interval.MIN_15,
    "M30": Interval.MIN_30,
    "H1": Interval.HOUR_1,
    "H2": Interval.HOUR_2,
}


def _cache_path(symbol: str, tf: str) -> Path:
    return DATA_CACHE / f"{symbol}_{tf}.parquet"


def load_cached(symbol: str, tf: str) -> pd.DataFrame | None:
    p = _cache_path(symbol, tf)
    if p.exists():
        try:
            df = pd.read_parquet(p)
        except Exception:
            # engine missing (no pyarrow/fastparquet on lean runners) or corrupt cache
            return None
        for col in ("time", "index", "Time"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], utc=True)
                df = df.set_index(col).sort_index()
                break
        if not df.empty and isinstance(df.index, pd.DatetimeIndex):
            return df
    return None


def save_cached(symbol: str, tf: str, df: pd.DataFrame) -> None:
    out = df.copy()
    out.index = pd.to_datetime(out.index, utc=True)
    out.index.name = "time"
    try:
        out.reset_index().to_parquet(_cache_path(symbol, tf), index=False)
    except Exception:
        # no parquet engine available (lean runner): skip caching, fetch-only
        pass


async def fetch_bars(symbol: str, tf: str, bars: int = 40000) -> pd.DataFrame:
    """Pull up to ``bars`` most-recent OHLCV candles for symbol/timeframe."""
    interval = _INTERVAL[tf]
    async with AsyncTradingView(timeout=30) as tv:
        bs = await tv.get_bars(SYMBOLS[symbol], interval=interval, bars=bars, timeout=60)
    df = bs.to_dataframe()
    df.index = pd.to_datetime(df.index, utc=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


async def download_all(symbols: list[str] | None = None,
                       timeframes: list[str] | None = None,
                       refresh: bool = False) -> None:
    symbols = symbols or list(SYMBOLS)
    timeframes = timeframes or TIMEFRAMES
    for sym in symbols:
        for tf in timeframes:
            if not refresh and load_cached(sym, tf) is not None:
                print(f"[cache] {sym} {tf} (skipped fetch)")
                continue
            df = await fetch_bars(sym, tf)
            save_cached(sym, tf, df)
            print(f"[ok]    {sym} {tf}: {len(df)} bars  {df.index[0]} -> {df.index[-1]}")


def _run_async(coro) -> pd.DataFrame:
    """Run a coroutine even when an event loop is already active (bot context)."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        box: dict = {}

        def _runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                box["value"] = loop.run_until_complete(coro)
            finally:
                loop.close()

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()
        return box["value"]


def get_df(symbol: str, tf: str, refresh: bool = False) -> pd.DataFrame:
    cached = None if refresh else load_cached(symbol, tf)
    if cached is not None:
        return cached
    df = _run_async(fetch_bars(symbol, tf))
    save_cached(symbol, tf, df)
    return df


if __name__ == "__main__":
    asyncio.run(download_all(refresh=False))
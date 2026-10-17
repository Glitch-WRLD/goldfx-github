import asyncio
import sys
from pathlib import Path
import pandas as pd
from tradingview_sdk import AsyncTradingView, Interval

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import SYMBOLS, DATA_CACHE

async def fetch_m5():
    sym_code = SYMBOLS["XAUUSD"]
    print(f"Fetching M5 bars for {sym_code}...")
    async with AsyncTradingView(timeout=30) as tv:
        bs = await tv.get_bars(sym_code, interval=Interval.MIN_5, bars=5000, timeout=60)
    df = bs.to_dataframe()
    df.index = pd.to_datetime(df.index, utc=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    out_path = DATA_CACHE / "XAUUSD_M5.parquet"
    df.reset_index().to_parquet(out_path, index=False)
    print(f"Saved {len(df)} M5 bars to {out_path}: {df.index[0]} to {df.index[-1]}")

if __name__ == "__main__":
    asyncio.run(fetch_m5())

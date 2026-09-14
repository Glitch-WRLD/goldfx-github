"""Fetch fresh OHLC for XAUUSD/EURUSD on M15/M30/H1/H2 and refresh parquet cache.

Usage: python scripts/download_data.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.tv_data import download_all, load_cached


def main():
    print("Fetching fresh candles from TradingView ...")
    download_all(refresh=True)
    print("Cached frame sizes:")
    for sym, tf in [("XAUUSD", "M30"), ("XAUUSD", "H2"), ("EURUSD", "M30"), ("EURUSD", "H2")]:
        df = load_cached(sym, tf)
        if df is not None:
            print(f"  {sym}:{tf}  n={len(df):>6}  {df.index.min()} .. {df.index.max()}")


if __name__ == "__main__":
    main()
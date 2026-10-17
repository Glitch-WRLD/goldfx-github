"""Analyze historical signals from state.json against M15 intraday candle data.
Classifies SL hits:
  - wick: tapped SL by < 0.15R and reversed toward TP
  - breach: clean through SL without hesitation
  - giveback (avoidable SL): reached >= +0.5R before reversing to SL
  - bad_entry: never exceeded +0.1R, immediate reversal
Also simulates:
  - Half A: LTF M15 confirmation entry vs HTF immediate entry
  - Half B: BE @ 0.5R, Partial 50% @ 0.5R, LTF reversal exit
"""
import json
import sys
from pathlib import Path
import pandas as pd
import numpy as np

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from data.tv_data import load_cached

def main():
    state_file = BASE_DIR / "gha_state" / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    history = state.get("history", [])
    outcomes = state.get("outcomes", {})

    print(f"Loaded {len(history)} history setups, {len(outcomes)} outcomes.")

    # Load M15 candles for each symbol
    m15_data = {}
    for sym in ["XAUUSD", "EURUSD", "GBPUSD", "AUDUSD"]:
        df = load_cached(sym, "M15")
        if df is not None:
            m15_data[sym] = df
            print(f"Loaded M15 for {sym}: {len(df)} bars ({df.index[0]} to {df.index[-1]})")

    matched = []
    for h in history:
        key = f"{h['symbol']}:{h['ts']}"
        if key in outcomes:
            matched.append((h, outcomes[key]))

    print(f"\n--- MATCHED DELIVERED SETUPS (n={len(matched)}) ---")
    
    # Analyze price excursion on M15 bars between signal ts and outcome when
    sl_classifications = {"wick": 0, "breach": 0, "giveback_sl": 0, "bad_entry": 0}
    excursions = []

    for h, o in matched:
        sym = h["symbol"]
        df_m15 = m15_data.get(sym)
        if df_m15 is None:
            continue

        sig_ts = pd.to_datetime(h["ts"], utc=True)
        hit = o.get("hit")
        when = pd.to_datetime(o.get("when", o.get("ts", sig_ts)), utc=True)
        
        direction = 1 if h["dir"] == "LONG" else -1
        entry = float(h["entry"])
        sl = float(h["sl"])
        tp = float(h["tp"])
        risk = abs(entry - sl)
        if risk <= 0:
            continue

        # Look at M15 bars starting immediately after sig_ts up to when + 2 bars
        trade_bars = df_m15[(df_m15.index >= sig_ts) & (df_m15.index <= when + pd.Timedelta(hours=4))]
        if trade_bars.empty:
            continue

        # Maximum Favorable Excursion (MFE) in R
        # Maximum Adverse Excursion (MAE) in R
        if direction == 1:
            mfe_price = trade_bars["high"].max()
            mae_price = trade_bars["low"].min()
            mfe_r = (mfe_price - entry) / risk
            mae_r = (entry - mae_price) / risk
        else:
            mfe_price = trade_bars["low"].min()
            mae_price = trade_bars["high"].max()
            mfe_r = (entry - mfe_price) / risk
            mae_r = (mae_price - entry) / risk

        classification = ""
        if hit == "sl":
            if mfe_r >= 0.5:
                classification = "giveback_sl" # reached +0.5R, then reversed to hit SL!
            elif mfe_r < 0.15:
                classification = "bad_entry"   # went straight to SL
            elif mae_r <= 1.15:
                classification = "wick"        # tapped SL by <= 0.15R
            else:
                classification = "breach"      # clean blast through SL
            sl_classifications[classification] += 1

        excursions.append({
            "ref": h.get("ref"),
            "symbol": sym,
            "dir": h["dir"],
            "ts": h["ts"],
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": h.get("rr", 0.7),
            "hit": hit,
            "mfe_r": round(mfe_r, 2),
            "mae_r": round(mae_r, 2),
            "classification": classification,
        })

    print("\nSL Hit Breakdown on Delivered Trades:")
    total_sl = sum(sl_classifications.values())
    for k, v in sl_classifications.items():
        pct = (v / total_sl * 100) if total_sl > 0 else 0
        print(f"  {k:15s}: {v:2d} ({pct:5.1f}%)")

    # Inspect the giveback SLs in detail
    givebacks = [e for e in excursions if e["classification"] == "giveback_sl"]
    print(f"\nAvoidable Giveback SLs (Hit >= +0.5R before SL): {len(givebacks)} of {total_sl} SLs")
    for g in givebacks:
        print(f"  Ref #{g['ref']} {g['symbol']} {g['dir']} MFE=+{g['mfe_r']}R -> Hit SL!")

if __name__ == "__main__":
    main()

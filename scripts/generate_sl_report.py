#!/usr/bin/env python3
"""Detailed Statistical Reporter for Stop Loss (SL) Forensic Results."""

import json
from pathlib import Path
import pandas as pd
import numpy as np

DATA_FILE = Path("C:/goldfx-agent/data/sl_forensic_results.json")

def main():
    with open(DATA_FILE, "r") as f:
        data = json.load(f)

    df = pd.DataFrame(data)
    print(f"Total SL setups in dataset: {len(df)}")
    print(f"This week (Sep 28 - Oct 2): {df['is_this_week'].sum()} setups")
    print(f"Prior weeks: {(~df['is_this_week']).sum()} setups\n")

    for scope_name, sub in [("THIS WEEK (Sep 28 - Oct 2, 2026)", df[df["is_this_week"]]), ("ALL-TIME (Sep 14 - Oct 2, 2026)", df)]:
        n = len(sub)
        print("=" * 80)
        print(f"REPORT: {scope_name}  (Total Analyzed SL Setups = {n})")
        print("=" * 80)

        # 1. Multi-Timeframe Alignment
        h1_aligned = sub["h1_aligned"].sum()
        h4_aligned = sub["h4_aligned"].sum()
        d1_aligned = sub["d1_aligned"].sum()
        both_htf_aligned = (sub["h1_aligned"] & sub["h4_aligned"]).sum()
        both_htf_counter = (sub["htf_counter_trend"]).sum()

        print("\n--- 1. MULTI-TIMEFRAME (HTF) TREND ALIGNMENT AT ENTRY ---")
        print(f"  * Aligned with H4 Macro Trend: {h4_aligned:3d} / {n} ({h4_aligned/n*100:5.1f}%)")
        print(f"  * Aligned with H1 Swing Trend: {h1_aligned:3d} / {n} ({h1_aligned/n*100:5.1f}%)")
        print(f"  * Aligned with D1 Daily Trend: {d1_aligned:3d} / {n} ({d1_aligned/n*100:5.1f}%)")
        print(f"  * FULL HTF CONFLUENCE (Pro H4 + H1): {both_htf_aligned:3d} / {n} ({both_htf_aligned/n*100:5.1f}%)")
        print(f"  * FULL HTF COUNTER-TREND (Against H4 + H1): {both_htf_counter:3d} / {n} ({both_htf_counter/n*100:5.1f}%)")

        # 2. What Happened After Hitting Stop Loss?
        print("\n--- 2. POST-STOP LOSS PRICE ACTION FORENSIC ---")
        vc = sub["post_behavior"].value_counts()
        for k, v in vc.items():
            print(f"  * {k:30s}: {v:3d} / {n} ({v/n*100:5.1f}%)")

        # Swept to TP
        hit_tp = sub["hit_original_tp_after_sl"].sum()
        rev_1r = (sub["max_favorable_after_sl_r"] >= 1.0).sum()
        print(f"\n  >>> KEY DISCOVERY 1: {hit_tp} out of {n} ({hit_tp/n*100:.1f}%) hit FULL ORIGINAL TP after stopping out!")
        print(f"  >>> KEY DISCOVERY 2: {rev_1r} out of {n} ({rev_1r/n*100:.1f}%) reached >= +1.0R in intended direction after stopping out!")

        # Inversion FVG
        ifvg = sub["retested_fvg_as_inversion"].sum()
        print(f"  >>> KEY DISCOVERY 3: {ifvg} out of {n} ({ifvg/n*100:.1f}%) successfully retested broken zone as Inversion FVG (IFVG)!")

        # 3. Overshoot Distance Distribution (How much beyond the SL did price go before turning?)
        print("\n--- 3. SL OVERSHOOT DISTRIBUTION (LIQUIDITY DEPTH BEYOND SL) ---")
        overshoots = sub["max_overshoot_r"]
        print(f"  * Median Overshoot beyond SL: {overshoots.median():.2f} R")
        print(f"  * Mean Overshoot beyond SL:   {overshoots.mean():.2f} R")
        print(f"  * Overshoot <= 0.3R (Tight Wicks / Fakes): {(overshoots <= 0.3).sum():3d} / {n} ({(overshoots <= 0.3).sum()/n*100:5.1f}%)")
        print(f"  * Overshoot <= 0.6R (Shallow Inducement):   {(overshoots <= 0.6).sum():3d} / {n} ({(overshoots <= 0.6).sum()/n*100:5.1f}%)")
        print(f"  * Overshoot <= 1.0R (Structural Retest):    {(overshoots <= 1.0).sum():3d} / {n} ({(overshoots <= 1.0).sum()/n*100:5.1f}%)")
        print(f"  * Overshoot >  1.5R (True Trend Reversal):   {(overshoots > 1.5).sum():3d} / {n} ({(overshoots > 1.5).sum()/n*100:5.1f}%)")

        # 4. Session Clustering
        print("\n--- 4. SESSION & TIMING CLUSTERING ---")
        sess_vc = sub["session"].value_counts()
        for k, v in sess_vc.items():
            print(f"  * {k:25s}: {v:3d} / {n} ({v/n*100:5.1f}%)")

        # 5. Symbol Breakdown
        print("\n--- 5. SYMBOL FAILURE & SWEEP RATES ---")
        print(f"  {'Symbol':<12} {'Total SL':<10} {'Swept to TP':<15} {'Sweep %':<10} {'Avg Stop (Pips)':<18}")
        print("  " + "-" * 65)
        for sym in sub["symbol"].unique():
            sym_df = sub[sub["symbol"] == sym]
            sym_sl = len(sym_df)
            sym_swept = sym_df["hit_original_tp_after_sl"].sum()
            sym_sweep_pct = sym_swept / sym_sl * 100 if sym_sl else 0
            avg_pips = sym_df["risk_pips"].mean()
            print(f"  {sym:<12} {sym_sl:<10} {sym_swept:<15} {sym_sweep_pct:<10.1f}% {avg_pips:<18.1f}")

        # 6. Directional Bias
        print("\n--- 6. DIRECTIONAL BIAS (LONG VS SHORT SLs) ---")
        dir_vc = sub["direction"].value_counts()
        for k, v in dir_vc.items():
            print(f"  * {k:10s}: {v:3d} / {n} ({v/n*100:5.1f}%)")

        print("\n")

if __name__ == "__main__":
    main()

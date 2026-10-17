#!/usr/bin/env python3
"""Simulation and Quantification of the New Proposed Strategies on Historical SL Setups."""

import json
from pathlib import Path
import pandas as pd
import numpy as np

DATA_FILE = Path("C:/goldfx-agent/data/sl_forensic_results.json")

def main():
    with open(DATA_FILE, "r") as f:
        data = json.load(f)

    df = pd.DataFrame(data)
    n = len(df)
    n_week = df['is_this_week'].sum()

    print("=" * 80)
    print("NEW STRATEGY SIMULATION ON STOPPED OUT TRADES")
    print("=" * 80)

    # 1. Asian Session Filter Impact
    # If we pruned Asian session (00:00 - 06:50 UTC)
    asian_sls = (df["session"] == "Asian").sum()
    asian_sls_week = (df[df["is_this_week"]]["session"] == "Asian").sum()

    print("\n[STRATEGY 1: ASIAN SESSION PRUNING]")
    print(f"  - Total SLs eliminated all-time: {asian_sls} / {n} ({asian_sls/n*100:.1f}%)")
    print(f"  - Total SLs eliminated this week: {asian_sls_week} / {n_week} ({asian_sls_week/n_week*100:.1f}%)")

    # 2. Turtle Soup / Inducement Sweep Strategy
    # Condition: Setup hits SL by <= 0.6R (shallow sweep), then reverses and hits TP
    # If we waited for the sweep (entry at SL price after sweep candle) with a 0.5R stop:
    # Win rate of trades that swept and hit TP:
    shallow_sweeps = df[df["post_behavior"] == "LIQUIDITY_SWEEP_TO_TP"]
    deep_sweeps = df[df["post_behavior"] == "DEEP_SWEEP_TO_TP"]
    all_sweeps = df[df["hit_original_tp_after_sl"]]

    print("\n[STRATEGY 2: TURTLE SOUP / LIQUIDITY INDUCEMENT RE-ENTRY]")
    print(f"  - All-Time Setups that Swept SL and hit TP: {len(all_sweeps)} / {n} ({len(all_sweeps)/n*100:.1f}%)")
    print(f"  - This Week Setups that Swept SL and hit TP: {len(df[df['is_this_week'] & df['hit_original_tp_after_sl']])} / {n_week} ({len(df[df['is_this_week'] & df['hit_original_tp_after_sl']])/n_week*100:.1f}%)")
    print(f"  - Shallow Sweeps (<= 0.6R overshoot): {len(shallow_sweeps)} all-time, {len(df[df['is_this_week'] & (df['post_behavior'] == 'LIQUIDITY_SWEEP_TO_TP')])} this week")

    # 3. Inversion FVG (IFVG) Break & Retest
    ifvgs = df[df["retested_fvg_as_inversion"]]
    ifvgs_week = df[df["is_this_week"] & df["retested_fvg_as_inversion"]]

    print("\n[STRATEGY 3: INVERSION FVG (IFVG) S/R FLIP]")
    print(f"  - All-Time Failed FVGs that gave >= 1.0R opposite trade: {len(ifvgs)} / {n} ({len(ifvgs)/n*100:.1f}%)")
    print(f"  - This Week Failed FVGs that gave >= 1.0R opposite trade: {len(ifvgs_week)} / {n_week} ({len(ifvgs_week)/n_week*100:.1f}%)")

    # Combined Strategy Potential:
    # What if we combined HTF Pro-Trend + Liquidity Inducement Entry?
    pro_trend_sweeps = df[df["h4_aligned"] & df["hit_original_tp_after_sl"]]
    print("\n[COMBINED INSTITUTIONAL ALPHA: H4 PRO-TREND + INDUCEMENT SWEEP]")
    print(f"  - Of the {df['h4_aligned'].sum()} trades aligned with H4, {len(pro_trend_sweeps)} ({len(pro_trend_sweeps)/df['h4_aligned'].sum()*100:.1f}%) swept the SL and then flew to Full TP!")

if __name__ == "__main__":
    main()

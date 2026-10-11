#!/usr/bin/env python3
"""Forensic Turtle Soup & Stop-Loss Reversal Analysis Script.

Analyzes:
1. What distinguishes setups that hit SL and reversed vs those that continued into traps.
2. The "Green Light" signal: Candle wick ratio, close confirmation, M5 MSS/CHoCH, M5 FVG displacement, RSI divergence, time window, and HTF trend.
3. Movement from reversal point toward lost TP.
4. Optimal TP placement: Original Entry vs Midpoint vs Original TP vs Fixed 2R/3R.
"""
from __future__ import annotations

import sys
from pathlib import Path
from dataclasses import dataclass
import json
import logging

BASE_DIR = Path("C:/goldfx-agent")
sys.path.insert(0, str(BASE_DIR))

# Force UTF-8 output
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
import MetaTrader5 as mt5

from strategy.candidates import STRATEGIES
from strategy.profiles import BAL_PROFILE, SYMBOL_RUNTIME

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("turtle_soup_forensic")

def init_mt5() -> bool:
    if not mt5.initialize():
        log.error("Failed to initialize MT5: %s", mt5.last_error())
        return False
    return True

def get_rates(symbol: str, tf, count: int = 25000) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    return df

def get_pip_scale(symbol: str) -> float:
    if "JPY" in symbol:
        return 0.01
    if symbol == "XAUUSD":
        return 0.1
    if any(idx in symbol for idx in ["100", "500", "30"]):
        return 1.0
    return 0.0001

def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

@dataclass
class SLCase:
    symbol: str
    dir: int                # 1 = LONG, -1 = SHORT
    sig_ts: pd.Timestamp
    sl_ts: pd.Timestamp
    entry_price: float
    sl_price: float
    tp_price: float
    orig_risk: float
    orig_reward: float
    orig_rr: float
    h1_aligned: bool
    h4_aligned: bool
    
    # Sweep telemetry
    sweep_extreme: float
    overshoot_dist: float
    overshoot_r: float
    reclaimed_sl: bool
    reclaim_ts: pd.Timestamp | None
    bars_to_reclaim: int
    
    # Green light features
    sweep_candle_wick_ratio: float   # Wick % on the bar that hit extreme
    sweep_closed_inside_sl: bool     # Bar close stayed on safe side of SL
    m5_close_reclaim: bool           # Did an M5 bar close back across SL
    m5_mss_break: bool               # Market Structure Shift (broke prior swing)
    m5_fvg_created: bool             # FVG created on the reversal leg
    rsi_at_sweep: float              # RSI(14) on M5 at sweep bar
    rsi_divergence: bool             # Bullish/bearish divergence
    
    # Subsequent outcome if re-entered on reclaim with tight stop behind sweep extreme
    new_sl_price: float
    new_risk: float
    
    # Path tracking
    max_favorable_price: float
    mfe_r_orig: float                # MFE in terms of original risk
    mfe_r_new: float                 # MFE in terms of new tight risk
    reached_entry: bool              # Reached original entry
    pct_of_tp_path: float            # Max distance reached as % of (entry -> tp)
    reached_25_tp: bool
    reached_50_tp: bool              # Midpoint
    reached_75_tp: bool
    reached_100_tp: bool             # Full original TP
    exceeded_tp: bool
    
    # Re-entry execution outcomes
    reentry_success: bool            # Did it reach at least orig entry or 1.0R new before breaking sweep extreme?
    reentry_hit_sweep_stop: bool     # Did price blow through sweep extreme first? (Double loss)
    
    # Outcomes for 5 specific TP targets
    pnl_r_target_entry: float        # TP = Original Entry
    pnl_r_target_midpoint: float     # TP = 50% path between Entry & TP
    pnl_r_target_orig_tp: float      # TP = Original TP
    pnl_r_target_2r: float           # TP = 2.0R (tight stop)
    pnl_r_target_3r: float           # TP = 3.0R (tight stop)

def analyze_sl_cases(sl_cases: list[SLCase]):
    total = len(sl_cases)
    if total == 0:
        print("No SL cases to analyze.")
        return

    print("=" * 80)
    print(f"DELIBERATE FORENSIC TURTLE SOUP & SL REVERSAL AUDIT (Total SL Cases: {total})")
    print("=" * 80)

    # 1. Broad Reversal Classification
    reclaimed = [c for c in sl_cases if c.reclaimed_sl]
    non_reclaimed = [c for c in sl_cases if not c.reclaimed_sl]
    print(f"\n1. BROAD REVERSAL BEHAVIOR:")
    print(f"   - Reclaimed Original SL level: {len(reclaimed)} / {total} ({len(reclaimed)/total*100:.1f}%)")
    print(f"   - Pure Blow-through (Never reclaimed SL): {len(non_reclaimed)} / {total} ({len(non_reclaimed)/total*100:.1f}%)")

    # For reclaimed cases: how many were Genuine Reversals vs Fake Reversals (Double-Loss Bleed)?
    genuine_reversals = [c for c in reclaimed if not c.reentry_hit_sweep_stop and c.mfe_r_new >= 1.0]
    fake_reversals = [c for c in reclaimed if c.reentry_hit_sweep_stop]
    print(f"\n2. THE DOUBLE-LOSS BLEED BREAKDOWN (Among {len(reclaimed)} Reclaimed Trades):")
    print(f"   - Genuine Reversals (Moved >= 1.0R in trade dir without breaking sweep wick): {len(genuine_reversals)} ({len(genuine_reversals)/len(reclaimed)*100:.1f}%)")
    print(f"   - Trap / Fake Reversals (Wicked across SL then blew through sweep extreme): {len(fake_reversals)} ({len(fake_reversals)/len(reclaimed)*100:.1f}%)")
    print(f"   -> EXPLANATION OF LIVE TRADING FAILURE: Blind reclaim re-entry suffered a {len(fake_reversals)/len(reclaimed)*100:.1f}% failure rate without confirmation!")

    # 3. DISSECTING THE "GREEN LIGHT" SIGNALS
    print("\n" + "=" * 80)
    print("3. FORENSIC AUDIT: DISSECTING THE 'GREEN LIGHT' SIGNALS")
    print("=" * 80)
    print(f"{'Filter / Signal':<35} | {'Trades':<8} | {'Win% (New)':<10} | {'Fake Reversals':<14} | {'PF':<6} | {'Net R':<8}")
    print("-" * 85)

    def eval_filter(name: str, subset: list[SLCase], tp_target: str = "orig_tp"):
        if not subset:
            return
        n = len(subset)
        if tp_target == "orig_tp":
            pnls = [c.pnl_r_target_orig_tp for c in subset]
        elif tp_target == "entry":
            pnls = [c.pnl_r_target_entry for c in subset]
        elif tp_target == "midpoint":
            pnls = [c.pnl_r_target_midpoint for c in subset]
        elif tp_target == "2r":
            pnls = [c.pnl_r_target_2r for c in subset]
        else:
            pnls = [c.pnl_r_target_3r for c in subset]

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        fakes = sum(1 for c in subset if c.reentry_hit_sweep_stop)
        wr = len(wins) / n * 100 if n > 0 else 0
        gross_w = sum(wins)
        gross_l = abs(sum(losses))
        pf = gross_w / gross_l if gross_l > 0 else (99.0 if gross_w > 0 else 0.0)
        net_r = sum(pnls)
        print(f"{name:<35} | {n:<8} | {wr:<9.1f}% | {fakes:<14} | {pf:<6.2f} | {net_r:+7.1f}R")

    # Baseline: Blind Reclaim (Any reclaim)
    eval_filter("Baseline (Blind Reclaim Any)", reclaimed, "orig_tp")

    # A: Sweep Depth Cuts
    eval_filter("Overshoot <= 0.20R", [c for c in reclaimed if c.overshoot_r <= 0.20], "orig_tp")
    eval_filter("Overshoot <= 0.35R", [c for c in reclaimed if c.overshoot_r <= 0.35], "orig_tp")
    eval_filter("Overshoot <= 0.50R", [c for c in reclaimed if c.overshoot_r <= 0.50], "orig_tp")
    eval_filter("Overshoot > 0.50R (Too Deep)", [c for c in reclaimed if c.overshoot_r > 0.50], "orig_tp")

    # B: Candle Wick Rejection Signature
    eval_filter("Sweep Wick Ratio >= 50%", [c for c in reclaimed if c.sweep_candle_wick_ratio >= 0.50], "orig_tp")
    eval_filter("Sweep Wick Ratio >= 60%", [c for c in reclaimed if c.sweep_candle_wick_ratio >= 0.60], "orig_tp")
    eval_filter("Sweep Body Closed Inside SL", [c for c in reclaimed if c.sweep_closed_inside_sl], "orig_tp")

    # C: M5 Confirmation Signatures
    eval_filter("M5 Candle CLOSE Reclaim", [c for c in reclaimed if c.m5_close_reclaim], "orig_tp")
    eval_filter("M5 Market Structure Shift (MSS)", [c for c in reclaimed if c.m5_mss_break], "orig_tp")
    eval_filter("M5 FVG Displacement Created", [c for c in reclaimed if c.m5_fvg_created], "orig_tp")
    eval_filter("RSI Divergence at Sweep", [c for c in reclaimed if c.rsi_divergence], "orig_tp")
    eval_filter("Quick Reclaim (<= 3 M5 Bars / 15m)", [c for c in reclaimed if c.bars_to_reclaim <= 3], "orig_tp")
    eval_filter("Slow Reclaim (> 6 M5 Bars / 30m)", [c for c in reclaimed if c.bars_to_reclaim > 6], "orig_tp")

    # D: Trend Confluence
    eval_filter("H1 EMA50 Aligned Original", [c for c in reclaimed if c.h1_aligned], "orig_tp")
    eval_filter("H4 EMA50 Aligned Original", [c for c in reclaimed if c.h4_aligned], "orig_tp")

    # E: Combo "Golden Green Light"
    golden_green_1 = [c for c in reclaimed if c.overshoot_r <= 0.40 and c.m5_close_reclaim and c.sweep_candle_wick_ratio >= 0.50]
    eval_filter("COMBO: Sweep<=0.4R + Wick>=50% + M5 Close", golden_green_1, "orig_tp")

    golden_green_mss = [c for c in reclaimed if c.overshoot_r <= 0.45 and c.m5_mss_break]
    eval_filter("COMBO: Sweep<=0.45R + M5 MSS Break", golden_green_mss, "orig_tp")

    golden_green_full = [c for c in reclaimed if c.overshoot_r <= 0.45 and c.m5_mss_break and c.h1_aligned]
    eval_filter("COMBO: Sweep<=0.45R + M5 MSS + H1 Aligned", golden_green_full, "orig_tp")

    # 4. MOVEMENT FROM REVERSAL POINT TOWARDS LOST TP
    print("\n" + "=" * 80)
    print("4. MOVEMENT FROM REVERSAL POINT TOWARDS LOST TP (Among Genuine Reversals, n=" + str(len(genuine_reversals)) + ")")
    print("=" * 80)
    
    # Excursion metrics
    mfe_orig_vals = [c.mfe_r_orig for c in genuine_reversals]
    mfe_new_vals = [c.mfe_r_new for c in genuine_reversals]
    pct_tp_vals = [c.pct_of_tp_path for c in genuine_reversals]

    print(f"Average Peak Excursion (Original R units): {np.mean(mfe_orig_vals):.2f} R (Median: {np.median(mfe_orig_vals):.2f} R)")
    print(f"Average Peak Excursion (New Tight Stop R units): {np.mean(mfe_new_vals):.2f} R (Median: {np.median(mfe_new_vals):.2f} R)")
    print(f"Average Distance Traveled Toward Lost TP: {np.mean(pct_tp_vals):.1f}% (Median: {np.median(pct_tp_vals):.1f}%)")

    # Milestones reached
    reached_entry = sum(1 for c in genuine_reversals if c.reached_entry)
    reached_25 = sum(1 for c in genuine_reversals if c.reached_25_tp)
    reached_50 = sum(1 for c in genuine_reversals if c.reached_50_tp)
    reached_75 = sum(1 for c in genuine_reversals if c.reached_75_tp)
    reached_100 = sum(1 for c in genuine_reversals if c.reached_100_tp)
    exceeded = sum(1 for c in genuine_reversals if c.exceeded_tp)

    print(f"\nMILESTONES REACHED BY REVERSALS:")
    print(f"   - Reached Original Entry: {reached_entry} / {len(genuine_reversals)} ({reached_entry/len(genuine_reversals)*100:.1f}%)")
    print(f"   - Reached 25% of Path to TP: {reached_25} / {len(genuine_reversals)} ({reached_25/len(genuine_reversals)*100:.1f}%)")
    print(f"   - Reached 50% of Path to TP (Midpoint): {reached_50} / {len(genuine_reversals)} ({reached_50/len(genuine_reversals)*100:.1f}%)")
    print(f"   - Reached 75% of Path to TP: {reached_75} / {len(genuine_reversals)} ({reached_75/len(genuine_reversals)*100:.1f}%)")
    print(f"   - Reached 100% of Original TP: {reached_100} / {len(genuine_reversals)} ({reached_100/len(genuine_reversals)*100:.1f}%)")
    print(f"   - Exceeded Original TP: {exceeded} / {len(genuine_reversals)} ({exceeded/len(genuine_reversals)*100:.1f}%)")

    # 5. WHERE TO PUT THE TP WHEN IT REVERSES
    print("\n" + "=" * 80)
    print("5. WHERE TO PUT THE TAKE PROFIT: TARGET STRATEGY COMPARISON")
    print("=" * 80)
    print(f"{'Target Strategy':<30} | {'Win%':<8} | {'Avg Win R':<10} | {'Avg Loss R':<11} | {'PF':<6} | {'Net R (All)':<12} | {'Net R (Filtered)'}")
    print("-" * 95)

    strategies = [
        ("Target 1: Original Entry", "entry"),
        ("Target 2: Midpoint (50% TP)", "midpoint"),
        ("Target 3: Original TP (Full)", "orig_tp"),
        ("Target 4: Fixed 2.0R (Tight)", "2r"),
        ("Target 5: Fixed 3.0R (Tight)", "3r"),
    ]

    for strat_label, strat_key in strategies:
        if strat_key == "entry":
            pnl_all = [c.pnl_r_target_entry for c in reclaimed]
            pnl_filt = [c.pnl_r_target_entry for c in golden_green_mss]
        elif strat_key == "midpoint":
            pnl_all = [c.pnl_r_target_midpoint for c in reclaimed]
            pnl_filt = [c.pnl_r_target_midpoint for c in golden_green_mss]
        elif strat_key == "orig_tp":
            pnl_all = [c.pnl_r_target_orig_tp for c in reclaimed]
            pnl_filt = [c.pnl_r_target_orig_tp for c in golden_green_mss]
        elif strat_key == "2r":
            pnl_all = [c.pnl_r_target_2r for c in reclaimed]
            pnl_filt = [c.pnl_r_target_2r for c in golden_green_mss]
        else:
            pnl_all = [c.pnl_r_target_3r for c in reclaimed]
            pnl_filt = [c.pnl_r_target_3r for c in golden_green_mss]

        wins = [p for p in pnl_filt if p > 0]
        losses = [p for p in pnl_filt if p < 0]
        wr = len(wins) / len(pnl_filt) * 100 if pnl_filt else 0
        avg_w = np.mean(wins) if wins else 0
        avg_l = abs(np.mean(losses)) if losses else 0
        gw = sum(wins)
        gl = abs(sum(losses))
        pf = gw / gl if gl > 0 else (99.0 if gw > 0 else 0.0)
        net_all = sum(pnl_all)
        net_filt = sum(pnl_filt)

        print(f"{strat_label:<30} | {wr:<7.1f}% | {avg_w:<9.2f}R | {avg_l:<10.2f}R | {pf:<6.2f} | {net_all:+11.1f}R | {net_filt:+11.1f}R")

    # 6. ASSET-SPECIFIC REVERSAL LEAGUE
    print("\n" + "=" * 80)
    print("6. ASSET-SPECIFIC REVERSAL ALPHA (Which symbols actually reverse clean?)")
    print("=" * 80)
    syms = sorted(list(set(c.symbol for c in sl_cases)))
    print(f"{'Symbol':<12} | {'SL Total':<9} | {'Reclaims':<9} | {'Genuine Rev':<12} | {'Trap Bleed':<11} | {'Rev Win Rate'}")
    print("-" * 75)
    for s in syms:
        s_cases = [c for c in sl_cases if c.symbol == s]
        s_rec = [c for c in s_cases if c.reclaimed_sl]
        s_gen = [c for c in s_rec if not c.reentry_hit_sweep_stop and c.mfe_r_new >= 1.0]
        s_trap = [c for c in s_rec if c.reentry_hit_sweep_stop]
        wr_str = f"{len(s_gen)/len(s_rec)*100:.1f}%" if s_rec else "N/A"
        print(f"{s:<12} | {len(s_cases):<9} | {len(s_rec):<9} | {len(s_gen):<12} | {len(s_trap):<11} | {wr_str}")

def trace_sl_event(symbol: str, direction: int, sig_ts: pd.Timestamp, sl_ts: pd.Timestamp,
                   entry_price: float, sl_price: float, tp_price: float,
                   df_m5: pd.DataFrame, df_h1: pd.DataFrame | None, df_h4: pd.DataFrame | None) -> SLCase | None:
    orig_risk = abs(entry_price - sl_price)
    orig_reward = abs(tp_price - entry_price)
    if orig_risk <= 0:
        return None
    orig_rr = orig_reward / orig_risk
    pip_scale = get_pip_scale(symbol)
    buffer = 1.5 * pip_scale

    # HTF EMA50 checks
    h1_aligned = True
    h4_aligned = True
    if df_h1 is not None:
        h1_sub = df_h1.loc[df_h1.index <= sl_ts]
        if len(h1_sub) > 50:
            h1_ema = h1_sub["close"].ewm(span=50).mean().iloc[-1]
            h1_bull = h1_sub["close"].iloc[-1] > h1_ema
            h1_aligned = (h1_bull and direction == 1) or ((not h1_bull) and direction == -1)
    if df_h4 is not None:
        h4_sub = df_h4.loc[df_h4.index <= sl_ts]
        if len(h4_sub) > 50:
            h4_ema = h4_sub["close"].ewm(span=50).mean().iloc[-1]
            h4_bull = h4_sub["close"].iloc[-1] > h4_ema
            h4_aligned = (h4_bull and direction == 1) or ((not h4_bull) and direction == -1)

    # Slice forward M5 bars starting from sl_ts for up to 48 bars (4 hours)
    post_m5 = df_m5.loc[df_m5.index >= sl_ts].head(48)
    if len(post_m5) < 3:
        return None

    # Compute RSI on M5
    rsi_series = compute_rsi(df_m5["close"].loc[df_m5.index <= post_m5.index[-1]])

    if direction == 1:
        # LONG: Stopped out on lows below sl_price
        sweep_extreme = float(post_m5["low"].min())
        overshoot_dist = max(0.0, sl_price - sweep_extreme)
        overshoot_r = overshoot_dist / orig_risk
        sweep_bar_idx = post_m5["low"].idxmin()
        sweep_bar = post_m5.loc[sweep_bar_idx]
        
        # Wick ratio on sweep bar
        rng = float(sweep_bar["high"] - sweep_bar["low"])
        lower_wick = float(min(sweep_bar["open"], sweep_bar["close"]) - sweep_bar["low"])
        sweep_candle_wick_ratio = lower_wick / rng if rng > 0 else 0.0
        sweep_closed_inside_sl = float(sweep_bar["close"]) > sl_price

        # Check reclaim of sl_price
        reclaim_bars = post_m5.loc[(post_m5.index >= sweep_bar_idx) & (post_m5["high"] >= sl_price)]
        if len(reclaim_bars) == 0:
            reclaimed_sl = False
            reclaim_ts = None
            bars_to_reclaim = 99
            m5_close_reclaim = False
            m5_mss_break = False
            m5_fvg_created = False
            rsi_at_sweep = float(rsi_series.loc[sweep_bar_idx]) if sweep_bar_idx in rsi_series.index else 50.0
            rsi_divergence = False
            
            return SLCase(
                symbol=symbol, dir=direction, sig_ts=sig_ts, sl_ts=sl_ts,
                entry_price=entry_price, sl_price=sl_price, tp_price=tp_price,
                orig_risk=orig_risk, orig_reward=orig_reward, orig_rr=orig_rr,
                h1_aligned=h1_aligned, h4_aligned=h4_aligned,
                sweep_extreme=sweep_extreme, overshoot_dist=overshoot_dist, overshoot_r=overshoot_r,
                reclaimed_sl=False, reclaim_ts=None, bars_to_reclaim=bars_to_reclaim,
                sweep_candle_wick_ratio=sweep_candle_wick_ratio, sweep_closed_inside_sl=sweep_closed_inside_sl,
                m5_close_reclaim=False, m5_mss_break=False, m5_fvg_created=False,
                rsi_at_sweep=rsi_at_sweep, rsi_divergence=False,
                new_sl_price=sweep_extreme - buffer, new_risk=orig_risk,
                max_favorable_price=float(post_m5["high"].max()),
                mfe_r_orig=0.0, mfe_r_new=0.0, reached_entry=False, pct_of_tp_path=0.0,
                reached_25_tp=False, reached_50_tp=False, reached_75_tp=False, reached_100_tp=False, exceeded_tp=False,
                reentry_success=False, reentry_hit_sweep_stop=True,
                pnl_r_target_entry=-1.0, pnl_r_target_midpoint=-1.0, pnl_r_target_orig_tp=-1.0,
                pnl_r_target_2r=-1.0, pnl_r_target_3r=-1.0
            )

        reclaimed_sl = True
        reclaim_ts = reclaim_bars.index[0]
        bars_to_reclaim = post_m5.index.get_loc(reclaim_ts) - post_m5.index.get_loc(sweep_bar_idx)
        reclaim_bar = post_m5.loc[reclaim_ts]
        m5_close_reclaim = float(reclaim_bar["close"]) >= sl_price

        # MSS: Broke the high of the sweep extreme candle
        sweep_high = float(sweep_bar["high"])
        mss_bars = post_m5.loc[(post_m5.index > sweep_bar_idx) & (post_m5["close"] > sweep_high)]
        m5_mss_break = len(mss_bars) > 0

        # FVG creation between sweep and reclaim
        m5_fvg_created = False
        sw_loc = post_m5.index.get_loc(sweep_bar_idx)
        rec_loc = post_m5.index.get_loc(reclaim_ts)
        for i in range(sw_loc + 2, min(len(post_m5), rec_loc + 3)):
            if post_m5["low"].iloc[i] > post_m5["high"].iloc[i - 2]:
                m5_fvg_created = True
                break

        rsi_at_sweep = float(rsi_series.loc[sweep_bar_idx]) if sweep_bar_idx in rsi_series.index else 50.0
        rsi_sl = float(rsi_series.loc[sl_ts]) if sl_ts in rsi_series.index else 50.0
        rsi_divergence = (rsi_at_sweep > rsi_sl) or (rsi_at_sweep < 30.0)

        # Re-entry simulation from reclaim_ts
        entry_re = sl_price
        new_sl = sweep_extreme - buffer
        new_risk = entry_re - new_sl
        if new_risk <= 0:
            new_risk = orig_risk

        target_entry = entry_price
        target_midpoint = entry_price + 0.5 * (tp_price - entry_price)
        target_orig_tp = tp_price
        target_2r = entry_re + 2.0 * new_risk
        target_3r = entry_re + 3.0 * new_risk

        # Walk bars after reclaim
        bars_after = post_m5.loc[post_m5.index >= reclaim_ts]
        max_fav = entry_re
        hit_new_sl = False
        
        hit_entry = False
        hit_midpoint = False
        hit_orig_tp = False
        hit_2r = False
        hit_3r = False

        for _, b in bars_after.iterrows():
            b_high = float(b["high"])
            b_low = float(b["low"])
            if b_high > max_fav:
                max_fav = b_high
            
            # Check targets before stop
            if not hit_new_sl:
                if b_low <= new_sl:
                    hit_new_sl = True
                else:
                    if b_high >= target_entry: hit_entry = True
                    if b_high >= target_midpoint: hit_midpoint = True
                    if b_high >= target_orig_tp: hit_orig_tp = True
                    if b_high >= target_2r: hit_2r = True
                    if b_high >= target_3r: hit_3r = True

        mfe_r_orig = (max_fav - entry_re) / orig_risk
        mfe_r_new = (max_fav - entry_re) / new_risk
        dist_to_tp = tp_price - entry_price
        pct_path = ((max_fav - entry_price) / dist_to_tp * 100.0) if dist_to_tp > 0 else 0.0

        rr_entry = abs(target_entry - entry_re) / new_risk
        rr_midpoint = abs(target_midpoint - entry_re) / new_risk
        rr_orig_tp = abs(target_orig_tp - entry_re) / new_risk

        pnl_entry = rr_entry if hit_entry else -1.0
        pnl_midpoint = rr_midpoint if hit_midpoint else -1.0
        pnl_orig_tp = rr_orig_tp if hit_orig_tp else -1.0
        pnl_2r = 2.0 if hit_2r else -1.0
        pnl_3r = 3.0 if hit_3r else -1.0

        return SLCase(
            symbol=symbol, dir=direction, sig_ts=sig_ts, sl_ts=sl_ts,
            entry_price=entry_price, sl_price=sl_price, tp_price=tp_price,
            orig_risk=orig_risk, orig_reward=orig_reward, orig_rr=orig_rr,
            h1_aligned=h1_aligned, h4_aligned=h4_aligned,
            sweep_extreme=sweep_extreme, overshoot_dist=overshoot_dist, overshoot_r=overshoot_r,
            reclaimed_sl=True, reclaim_ts=reclaim_ts, bars_to_reclaim=bars_to_reclaim,
            sweep_candle_wick_ratio=sweep_candle_wick_ratio, sweep_closed_inside_sl=sweep_closed_inside_sl,
            m5_close_reclaim=m5_close_reclaim, m5_mss_break=m5_mss_break, m5_fvg_created=m5_fvg_created,
            rsi_at_sweep=rsi_at_sweep, rsi_divergence=rsi_divergence,
            new_sl_price=new_sl, new_risk=new_risk,
            max_favorable_price=max_fav, mfe_r_orig=mfe_r_orig, mfe_r_new=mfe_r_new,
            reached_entry=(max_fav >= entry_price), pct_of_tp_path=pct_path,
            reached_25_tp=(pct_path >= 25.0), reached_50_tp=(pct_path >= 50.0),
            reached_75_tp=(pct_path >= 75.0), reached_100_tp=(max_fav >= tp_price),
            exceeded_tp=(max_fav > tp_price),
            reentry_success=(mfe_r_new >= 1.0 and not hit_new_sl),
            reentry_hit_sweep_stop=hit_new_sl,
            pnl_r_target_entry=pnl_entry, pnl_r_target_midpoint=pnl_midpoint,
            pnl_r_target_orig_tp=pnl_orig_tp, pnl_r_target_2r=pnl_2r, pnl_r_target_3r=pnl_3r
        )

    else:
        # SHORT: Stopped out on highs above sl_price
        sweep_extreme = float(post_m5["high"].max())
        overshoot_dist = max(0.0, sweep_extreme - sl_price)
        overshoot_r = overshoot_dist / orig_risk
        sweep_bar_idx = post_m5["high"].idxmax()
        sweep_bar = post_m5.loc[sweep_bar_idx]

        # Wick ratio on sweep bar
        rng = float(sweep_bar["high"] - sweep_bar["low"])
        upper_wick = float(sweep_bar["high"] - max(sweep_bar["open"], sweep_bar["close"]))
        sweep_candle_wick_ratio = upper_wick / rng if rng > 0 else 0.0
        sweep_closed_inside_sl = float(sweep_bar["close"]) < sl_price

        # Check reclaim of sl_price
        reclaim_bars = post_m5.loc[(post_m5.index >= sweep_bar_idx) & (post_m5["low"] <= sl_price)]
        if len(reclaim_bars) == 0:
            reclaimed_sl = False
            reclaim_ts = None
            bars_to_reclaim = 99
            rsi_at_sweep = float(rsi_series.loc[sweep_bar_idx]) if sweep_bar_idx in rsi_series.index else 50.0
            return SLCase(
                symbol=symbol, dir=direction, sig_ts=sig_ts, sl_ts=sl_ts,
                entry_price=entry_price, sl_price=sl_price, tp_price=tp_price,
                orig_risk=orig_risk, orig_reward=orig_reward, orig_rr=orig_rr,
                h1_aligned=h1_aligned, h4_aligned=h4_aligned,
                sweep_extreme=sweep_extreme, overshoot_dist=overshoot_dist, overshoot_r=overshoot_r,
                reclaimed_sl=False, reclaim_ts=None, bars_to_reclaim=bars_to_reclaim,
                sweep_candle_wick_ratio=sweep_candle_wick_ratio, sweep_closed_inside_sl=sweep_closed_inside_sl,
                m5_close_reclaim=False, m5_mss_break=False, m5_fvg_created=False,
                rsi_at_sweep=rsi_at_sweep, rsi_divergence=False,
                new_sl_price=sweep_extreme + buffer, new_risk=orig_risk,
                max_favorable_price=float(post_m5["low"].min()),
                mfe_r_orig=0.0, mfe_r_new=0.0, reached_entry=False, pct_of_tp_path=0.0,
                reached_25_tp=False, reached_50_tp=False, reached_75_tp=False, reached_100_tp=False, exceeded_tp=False,
                reentry_success=False, reentry_hit_sweep_stop=True,
                pnl_r_target_entry=-1.0, pnl_r_target_midpoint=-1.0, pnl_r_target_orig_tp=-1.0,
                pnl_r_target_2r=-1.0, pnl_r_target_3r=-1.0
            )

        reclaimed_sl = True
        reclaim_ts = reclaim_bars.index[0]
        bars_to_reclaim = post_m5.index.get_loc(reclaim_ts) - post_m5.index.get_loc(sweep_bar_idx)
        reclaim_bar = post_m5.loc[reclaim_ts]
        m5_close_reclaim = float(reclaim_bar["close"]) <= sl_price

        # MSS: Broke the low of the sweep extreme candle
        sweep_low = float(sweep_bar["low"])
        mss_bars = post_m5.loc[(post_m5.index > sweep_bar_idx) & (post_m5["close"] < sweep_low)]
        m5_mss_break = len(mss_bars) > 0

        # FVG creation between sweep and reclaim
        m5_fvg_created = False
        sw_loc = post_m5.index.get_loc(sweep_bar_idx)
        rec_loc = post_m5.index.get_loc(reclaim_ts)
        for i in range(sw_loc + 2, min(len(post_m5), rec_loc + 3)):
            if post_m5["high"].iloc[i] < post_m5["low"].iloc[i - 2]:
                m5_fvg_created = True
                break

        rsi_at_sweep = float(rsi_series.loc[sweep_bar_idx]) if sweep_bar_idx in rsi_series.index else 50.0
        rsi_sl = float(rsi_series.loc[sl_ts]) if sl_ts in rsi_series.index else 50.0
        rsi_divergence = (rsi_at_sweep < rsi_sl) or (rsi_at_sweep > 70.0)

        # Re-entry simulation from reclaim_ts
        entry_re = sl_price
        new_sl = sweep_extreme + buffer
        new_risk = new_sl - entry_re
        if new_risk <= 0:
            new_risk = orig_risk

        target_entry = entry_price
        target_midpoint = entry_price - 0.5 * (entry_price - tp_price)
        target_orig_tp = tp_price
        target_2r = entry_re - 2.0 * new_risk
        target_3r = entry_re - 3.0 * new_risk

        # Walk bars after reclaim
        bars_after = post_m5.loc[post_m5.index >= reclaim_ts]
        min_fav = entry_re
        hit_new_sl = False

        hit_entry = False
        hit_midpoint = False
        hit_orig_tp = False
        hit_2r = False
        hit_3r = False

        for _, b in bars_after.iterrows():
            b_high = float(b["high"])
            b_low = float(b["low"])
            if b_low < min_fav:
                min_fav = b_low

            if not hit_new_sl:
                if b_high >= new_sl:
                    hit_new_sl = True
                else:
                    if b_low <= target_entry: hit_entry = True
                    if b_low <= target_midpoint: hit_midpoint = True
                    if b_low <= target_orig_tp: hit_orig_tp = True
                    if b_low <= target_2r: hit_2r = True
                    if b_low <= target_3r: hit_3r = True

        mfe_r_orig = (entry_re - min_fav) / orig_risk
        mfe_r_new = (entry_re - min_fav) / new_risk
        dist_to_tp = entry_price - tp_price
        pct_path = ((entry_price - min_fav) / dist_to_tp * 100.0) if dist_to_tp > 0 else 0.0

        rr_entry = abs(target_entry - entry_re) / new_risk
        rr_midpoint = abs(target_midpoint - entry_re) / new_risk
        rr_orig_tp = abs(target_orig_tp - entry_re) / new_risk

        pnl_entry = rr_entry if hit_entry else -1.0
        pnl_midpoint = rr_midpoint if hit_midpoint else -1.0
        pnl_orig_tp = rr_orig_tp if hit_orig_tp else -1.0
        pnl_2r = 2.0 if hit_2r else -1.0
        pnl_3r = 3.0 if hit_3r else -1.0

        return SLCase(
            symbol=symbol, dir=direction, sig_ts=sig_ts, sl_ts=sl_ts,
            entry_price=entry_price, sl_price=sl_price, tp_price=tp_price,
            orig_risk=orig_risk, orig_reward=orig_reward, orig_rr=orig_rr,
            h1_aligned=h1_aligned, h4_aligned=h4_aligned,
            sweep_extreme=sweep_extreme, overshoot_dist=overshoot_dist, overshoot_r=overshoot_r,
            reclaimed_sl=True, reclaim_ts=reclaim_ts, bars_to_reclaim=bars_to_reclaim,
            sweep_candle_wick_ratio=sweep_candle_wick_ratio, sweep_closed_inside_sl=sweep_closed_inside_sl,
            m5_close_reclaim=m5_close_reclaim, m5_mss_break=m5_mss_break, m5_fvg_created=m5_fvg_created,
            rsi_at_sweep=rsi_at_sweep, rsi_divergence=rsi_divergence,
            new_sl_price=new_sl, new_risk=new_risk,
            max_favorable_price=min_fav, mfe_r_orig=mfe_r_orig, mfe_r_new=mfe_r_new,
            reached_entry=(min_fav <= entry_price), pct_of_tp_path=pct_path,
            reached_25_tp=(pct_path >= 25.0), reached_50_tp=(pct_path >= 50.0),
            reached_75_tp=(pct_path >= 75.0), reached_100_tp=(min_fav <= tp_price),
            exceeded_tp=(min_fav < tp_price),
            reentry_success=(mfe_r_new >= 1.0 and not hit_new_sl),
            reentry_hit_sweep_stop=hit_new_sl,
            pnl_r_target_entry=pnl_entry, pnl_r_target_midpoint=pnl_midpoint,
            pnl_r_target_orig_tp=pnl_orig_tp, pnl_r_target_2r=pnl_2r, pnl_r_target_3r=pnl_3r
        )

def run_delivered_setups_study():
    state_file = BASE_DIR / "gha_state" / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    history = state.get("history", [])
    outcomes = state.get("outcomes", {})

    sl_trades = []
    for h in history:
        key = f"{h['symbol']}:{h['ts']}"
        out = outcomes.get(key)
        if out and out.get("hit") == "sl":
            sl_trades.append((h, out))

    log.info("Found %d delivered SL setups in gha_state/state.json", len(sl_trades))
    
    if not init_mt5():
        return

    # Cache M5 and H1 data
    symbols = sorted(list(set(h["symbol"] for h, _ in sl_trades)))
    m5_cache = {}
    h1_cache = {}
    h4_cache = {}
    for s in symbols:
        m5_cache[s] = get_rates(s, mt5.TIMEFRAME_M5, 30000)
        h1_cache[s] = get_rates(s, mt5.TIMEFRAME_H1, 6000)
        h4_cache[s] = get_rates(s, mt5.TIMEFRAME_H4, 2500)

    cases = []
    for h, out in sl_trades:
        sym = h["symbol"]
        df_m5 = m5_cache.get(sym)
        if df_m5 is None:
            continue
        sig_ts = pd.to_datetime(h["ts"], utc=True)
        sl_ts = pd.to_datetime(out.get("when", out.get("ts", sig_ts)), utc=True)
        direction = 1 if h["dir"] == "LONG" else -1
        entry_price = float(h["entry"])
        sl_price = float(h["sl"])
        tp_price = float(h["tp"])

        c = trace_sl_event(sym, direction, sig_ts, sl_ts, entry_price, sl_price, tp_price,
                           df_m5, h1_cache.get(sym), h4_cache.get(sym))
        if c:
            cases.append(c)

    mt5.shutdown()

    print("\n" + "#" * 80)
    print("DATASET 1: DELIVERED BOT SETUPS (Real Out-of-Sample Project History)")
    print("#" * 80)
    analyze_sl_cases(cases)

def run_deep_historical_study():
    if not init_mt5():
        return

    symbols = list(SYMBOL_RUNTIME.keys())
    log.info("Running deep multi-month study across %d symbols...", len(symbols))

    base_params = dict(BAL_PROFILE["params"])
    all_cases: list[SLCase] = []

    for sym in symbols:
        cfg = SYMBOL_RUNTIME[sym]
        entry_tf_str = cfg["entry_tf"]
        bias_htf_str = cfg["bias_htf"]
        strat_key = cfg["strategy"]

        tf_map = {
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1":  mt5.TIMEFRAME_H1,
            "H4":  mt5.TIMEFRAME_H4,
        }

        entry_tf_mt5 = tf_map[entry_tf_str]
        df_entry = get_rates(sym, entry_tf_mt5, 6000)
        df_m5 = get_rates(sym, mt5.TIMEFRAME_M5, 30000)
        df_h1 = get_rates(sym, mt5.TIMEFRAME_H1, 6000)
        df_h4 = get_rates(sym, mt5.TIMEFRAME_H4, 2500)

        if df_entry is None or df_m5 is None or len(df_entry) < 500:
            continue

        p = dict(base_params)
        p["bias_htf"] = bias_htf_str
        p["_symbol"] = sym

        strat = STRATEGIES.get(strat_key, STRATEGIES["fvg_retest"])
        signals = strat.signaller(df_entry, p)

        entry_times = df_entry.index
        n_bars = len(df_entry)

        for sig in signals:
            try:
                sig_idx = entry_times.get_loc(sig.ts)
            except KeyError:
                continue

            trade_idx = sig_idx + 1
            if trade_idx >= n_bars:
                continue

            entry_bar = df_entry.iloc[trade_idx]
            entry_ts = entry_times[trade_idx]
            entry_price = float(entry_bar["open"])
            sl_dist = float(sig.sl_offset)
            if sl_dist <= 0:
                continue

            if sig.dir == 1:
                sl_price = entry_price - sl_dist
                tp_price = float(sig.tp_price) if sig.tp_price and sig.tp_price > entry_price else entry_price + (sl_dist * sig.tp_r)
            else:
                sl_price = entry_price + sl_dist
                tp_price = float(sig.tp_price) if sig.tp_price and sig.tp_price < entry_price else entry_price - (sl_dist * sig.tp_r)

            # Trace outcome on entry timeframe
            hit_sl = False
            sl_ts = None
            for k in range(trade_idx, min(n_bars, trade_idx + 300)):
                bar = df_entry.iloc[k]
                b_high = float(bar["high"])
                b_low = float(bar["low"])
                b_ts = entry_times[k]

                if sig.dir == 1:
                    if b_low <= sl_price:
                        hit_sl = True
                        sl_ts = b_ts
                        break
                    elif b_high >= tp_price:
                        break
                else:
                    if b_high >= sl_price:
                        hit_sl = True
                        sl_ts = b_ts
                        break
                    elif b_low <= tp_price:
                        break

            if hit_sl and sl_ts:
                c = trace_sl_event(sym, sig.dir, entry_ts, sl_ts, entry_price, sl_price, tp_price,
                                   df_m5, df_h1, df_h4)
                if c:
                    all_cases.append(c)

    mt5.shutdown()

    print("\n" + "#" * 80)
    print("DATASET 2: DEEP MULTI-MONTH HISTORICAL BACKTEST (Thousands of Signals)")
    print("#" * 80)
    analyze_sl_cases(all_cases)

if __name__ == "__main__":
    run_delivered_setups_study()
    run_deep_historical_study()



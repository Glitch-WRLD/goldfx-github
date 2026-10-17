"""Comprehensive Head-to-Head Backtest across all Available Forex & Metal Pairs:

Concept 1: Baseline Delivery Bot Strategy (FVG-Retest)
Concept 2: Anthony Ikechukwu's Institutional SMC Concept (Order Block + Inducement / Liquidity Sweep)

Pairs Tested:
1. GBPJPY  (Anthony's primary video pair)
2. GBPCAD  (Anthony's primary video pair)
3. USDJPY
4. EURJPY
5. USDCAD
6. USDCHF
7. NZDUSD
8. EURUSD
9. GBPUSD
10. AUDUSD
11. XAUUSD
"""

import sys
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

import numpy as np
import pandas as pd
import MetaTrader5 as mt5

from strategy.backtester import run_backtest
from strategy.candidates import STRATEGIES
from strategy.test_hybrid_smc import smc_fvg_confluence_signaller
from strategy.profiles import BAL_PROFILE
from data.tv_data import load_cached, save_cached


SYMBOLS_TO_EVALUATE = [
    ("GBPJPY", "H1", "H4"),
    ("GBPCAD", "H1", "H4"),
    ("USDJPY", "H1", "H4"),
    ("EURJPY", "H1", "H4"),
    ("USDCAD", "H1", "H4"),
    ("USDCHF", "H1", "H4"),
    ("NZDUSD", "H1", "H4"),
    ("EURUSD", "H1", "H4"),
    ("GBPUSD", "H1", "H4"),
    ("AUDUSD", "H1", "H4"),
    ("XAUUSD", "M30", "H1"),
]


def fetch_and_cache_symbol(symbol: str, tf_str: str) -> pd.DataFrame | None:
    # First check cache
    df = load_cached(symbol, tf_str)
    if df is not None and len(df) >= 3000:
        return df

    # Fetch from MT5
    tf_mt5 = mt5.TIMEFRAME_H1 if tf_str == "H1" else (mt5.TIMEFRAME_M30 if tf_str == "M30" else mt5.TIMEFRAME_M15)
    rates = mt5.copy_rates_from_pos(symbol, tf_mt5, 0, 4000)
    if rates is None or len(rates) == 0:
        print(f"[WARN] Failed to fetch {symbol} {tf_str} from MT5")
        return None

    df_mt5 = pd.DataFrame(rates)
    df_mt5["time"] = pd.to_datetime(df_mt5["time"], unit="s", utc=True)
    df_mt5 = df_mt5.rename(columns={"tick_volume": "volume"}).set_index("time").sort_index()
    # Save to cache
    save_cached(symbol, tf_str, df_mt5)
    print(f"[FETCHED] {symbol} {tf_str}: {len(df_mt5)} bars cached ({df_mt5.index[0]:%Y-%m-%d} to {df_mt5.index[-1]:%Y-%m-%d})")
    return df_mt5


def main():
    print("=" * 80)
    print("RUNNING COMPREHENSIVE HEAD-TO-HEAD BACKTEST ACROSS ALL PAIRS")
    print("Concept 1: Production Baseline (FVG-Retest)")
    print("Concept 2: Institutional SMC Concept (Order Block + Inducement Sweep)")
    print("=" * 80)

    mt5_initialized = mt5.initialize()
    if not mt5_initialized:
        print("[WARN] Could not initialize MT5, relying only on existing cached data.")

    base_params = dict(BAL_PROFILE["params"])
    results_summary = []

    for sym, tf, bias_htf in SYMBOLS_TO_EVALUATE:
        df = fetch_and_cache_symbol(sym, tf)
        if df is None or len(df) < 500:
            print(f"Skipping {sym} (insufficient data)")
            continue

        # --- Concept 1: Baseline FVG Retest ---
        p1 = dict(base_params)
        p1["bias_htf"] = bias_htf
        res1 = run_backtest(df, sym, tf, "baseline_fvg", STRATEGIES["fvg_retest"].signaller, p1)
        m1 = res1.metrics()

        # --- Concept 2: Institutional SMC (BOS + Inducement Sweep) ---
        p2 = dict(base_params)
        p2["bias_htf"] = bias_htf
        p2["require_bos"] = False
        p2["require_sweep"] = True
        p2["swing_k"] = 2
        p2["min_rr"] = 0.8
        p2["max_rr"] = 2.5
        res2 = run_backtest(df, sym, tf, "smc_institutional", smc_fvg_confluence_signaller, p2)
        m2 = res2.metrics()

        results_summary.append({
            "symbol": sym,
            "tf": tf,
            "n_bars": len(df),
            # Baseline
            "b_n": m1.get("n", 0),
            "b_wr": m1.get("win_rate", 0.0),
            "b_pf": m1.get("profit_factor", 0.0),
            "b_tot_r": m1.get("total_r", 0.0),
            "b_max_dd": m1.get("max_drawdown_r", 0.0),
            "b_cons_loss": m1.get("max_consec_losses", 0),
            "b_tr_wk": m1.get("trades_per_week", 0.0),
            # SMC
            "s_n": m2.get("n", 0),
            "s_wr": m2.get("win_rate", 0.0),
            "s_pf": m2.get("profit_factor", 0.0),
            "s_tot_r": m2.get("total_r", 0.0),
            "s_max_dd": m2.get("max_drawdown_r", 0.0),
            "s_cons_loss": m2.get("max_consec_losses", 0),
            "s_tr_wk": m2.get("trades_per_week", 0.0),
        })

    if mt5_initialized:
        mt5.shutdown()

    # Print Table
    print("\n" + "=" * 115)
    print(f"{'SYMBOL':<8} {'TF':<4} | {'--- BASELINE (FVG RETEST) ---':<40} | {'--- UPGRADED SMC (SWEEP) ---':<40} | {'VERDICT'}")
    print(f"{'':<13} | {'N':<4} {'WR':<6} {'PF':<5} {'TotR':<7} {'MaxDD':<6} {'Cons':<4} | {'N':<4} {'WR':<6} {'PF':<5} {'TotR':<7} {'MaxDD':<6} {'Cons':<4} |")
    print("-" * 115)

    for r in results_summary:
        # Verdict calculation
        s_pf = r["s_pf"] if np.isfinite(r["s_pf"]) else 99.0
        b_pf = r["b_pf"] if np.isfinite(r["b_pf"]) else 99.0
        
        if s_pf >= 1.5 and r["s_tot_r"] > 5.0 and r["s_max_dd"] <= 5.0:
            verdict = "[TOP TIER] SMC ARSENAL"
        elif s_pf > b_pf and r["s_max_dd"] < r["b_max_dd"] and r["s_tot_r"] > 0:
            verdict = "[ADD] SMC Dominates"
        elif b_pf >= 1.3 and r["b_tot_r"] > 15.0:
            verdict = "[KEEP] Baseline Wins"
        elif s_pf >= 1.1 and r["s_tot_r"] > 0:
            verdict = "[VIABLE] SMC Positive"
        elif b_pf >= 1.1 and r["b_tot_r"] > 0:
            verdict = "[VIABLE] Baseline Positive"
        else:
            verdict = "[AVOID] Low Edge"

        print(f"{r['symbol']:<8} {r['tf']:<4} | "
              f"{r['b_n']:<4} {r['b_wr']:<6.1%} {r['b_pf']:<5.2f} {r['b_tot_r']:+6.1f}R {r['b_max_dd']:<6.1f}R {r['b_cons_loss']:<4} | "
              f"{r['s_n']:<4} {r['s_wr']:<6.1%} {r['s_pf']:<5.2f} {r['s_tot_r']:+6.1f}R {r['s_max_dd']:<6.1f}R {r['s_cons_loss']:<4} | "
              f"{verdict}")

    print("=" * 115)


if __name__ == "__main__":
    main()

"""Comprehensive Multi-Timeframe Backtest (H1, M30, M15, M5)

Answers user questions:
1. Did the backtest cover all timeframes lower than 4H? (Yes, now explicitly showing H1, M30, M15, M5)
2. How many setups were used for win-rate and metrics? (Explicit setup counts N per timeframe and strategy)
"""

import sys
from pathlib import Path
sys.path.insert(0, 'C:/goldfx-agent')

import numpy as np
import pandas as pd
import MetaTrader5 as mt5

from strategy.backtester import run_backtest
from strategy.candidates import STRATEGIES
from strategy.test_hybrid_smc import smc_fvg_confluence_signaller
from strategy.profiles import BAL_PROFILE


PAIRS = ["USDCAD", "EURUSD", "XAUUSD", "GBPUSD", "AUDUSD"]
TIMEFRAMES = [
    ("H1", mt5.TIMEFRAME_H1, "H4"),
    ("M30", mt5.TIMEFRAME_M30, "H2"),
    ("M15", mt5.TIMEFRAME_M15, "H1"),
    ("M5", mt5.TIMEFRAME_M5, "M30"),
]


def run_mtf():
    mt5.initialize()
    base_params = dict(BAL_PROFILE["params"])

    print("=" * 115)
    print(f"{'PAIR':<7} {'TF':<4} {'BARS & PERIOD':<22} | {'--- BASELINE (FVG RETEST) ---':<36} | {'--- UPGRADED SMC (SWEEP) ---':<36}")
    print(f"{'':<35} | {'Setups':<7} {'WR':<6} {'PF':<5} {'TotR':<8} {'MaxDD':<6} | {'Setups':<7} {'WR':<6} {'PF':<5} {'TotR':<8} {'MaxDD':<6}")
    print("-" * 115)

    all_rows = []

    for sym in PAIRS:
        for tf_str, tf_code, bias_htf in TIMEFRAMES:
            rates = mt5.copy_rates_from_pos(sym, tf_code, 0, 4000)
            if rates is None or len(rates) < 500:
                continue

            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.rename(columns={"tick_volume": "volume"}).set_index("time").sort_index()

            days = (df.index[-1] - df.index[0]).days
            period_str = f"4k bars (~{days}d)"

            # Baseline
            p1 = dict(base_params)
            p1["bias_htf"] = bias_htf
            res1 = run_backtest(df, sym, tf_str, "baseline", STRATEGIES["fvg_retest"].signaller, p1)
            m1 = res1.metrics()

            # SMC Sweep
            p2 = dict(base_params)
            p2["bias_htf"] = bias_htf
            p2["require_bos"] = False
            p2["require_sweep"] = True
            p2["swing_k"] = 2
            p2["min_rr"] = 0.8
            p2["max_rr"] = 2.5
            res2 = run_backtest(df, sym, tf_str, "smc", smc_fvg_confluence_signaller, p2)
            m2 = res2.metrics()

            b_n = m1.get("n", 0)
            b_wr = m1.get("win_rate", 0.0)
            b_pf = m1.get("profit_factor", 0.0)
            b_r = m1.get("total_r", 0.0)
            b_dd = m1.get("max_drawdown_r", 0.0)

            s_n = m2.get("n", 0)
            s_wr = m2.get("win_rate", 0.0)
            s_pf = m2.get("profit_factor", 0.0)
            s_r = m2.get("total_r", 0.0)
            s_dd = m2.get("max_drawdown_r", 0.0)

            print(f"{sym:<7} {tf_str:<4} {period_str:<22} | "
                  f"{b_n:<7} {b_wr:<6.1%} {b_pf:<5.2f} {b_r:+7.1f}R {b_dd:<6.1f}R | "
                  f"{s_n:<7} {s_wr:<6.1%} {s_pf:<5.2f} {s_r:+7.1f}R {s_dd:<6.1f}R")

            all_rows.append({
                "symbol": sym, "tf": tf_str, "days": days,
                "b_n": b_n, "b_wr": b_wr, "b_pf": b_pf, "b_tot_r": b_r, "b_dd": b_dd,
                "s_n": s_n, "s_wr": s_wr, "s_pf": s_pf, "s_tot_r": s_r, "s_dd": s_dd
            })

        print("-" * 115)

    mt5.shutdown()


if __name__ == "__main__":
    run_mtf()

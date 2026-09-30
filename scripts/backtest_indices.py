import sys
from pathlib import Path
import numpy as np
import pandas as pd
import MetaTrader5 as mt5

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from strategy.backtester import run_backtest
from strategy.candidates import STRATEGIES
from strategy.test_hybrid_smc import smc_fvg_confluence_signaller
from strategy.profiles import BAL_PROFILE

INDICES_TO_EVALUATE = [
    # (Symbol, Timeframe, Bias HTF)
    ("US500", "H1", "H4"),
    ("US500", "M30", "H1"),
    ("US500", "M15", "H1"),
    ("NASDAQ-100", "H1", "H4"),
    ("NASDAQ-100", "M30", "H1"),
    ("NASDAQ-100", "M15", "H1"),
    ("DJ30", "H1", "H4"),
    ("DJ30", "M30", "H1"),
    ("DJ30", "M15", "H1"),
]

def fetch_index_data(symbol: str, tf_str: str) -> pd.DataFrame | None:
    tf_map = {
        "H4": mt5.TIMEFRAME_H4,
        "H1": mt5.TIMEFRAME_H1,
        "M30": mt5.TIMEFRAME_M30,
        "M15": mt5.TIMEFRAME_M15,
    }
    tf_mt5 = tf_map.get(tf_str, mt5.TIMEFRAME_H1)
    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, tf_mt5, 0, 4500)
    if rates is None or len(rates) == 0:
        print(f"[WARN] Failed to fetch {symbol} {tf_str} from MT5")
        return None

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={"tick_volume": "volume"}).set_index("time").sort_index()
    return df

def main():
    print("=" * 105)
    print("BACKTESTING GLOBAL EQUITY INDICES (US500, NASDAQ-100, DJ30) ON HEADWAY MT5")
    print("Testing Strategy 1: Momentum FVG Retest")
    print("Testing Strategy 2: Institutional SMC Liquidity Sweep")
    print("=" * 105)

    if not mt5.initialize():
        print("[ERROR] MT5 could not be initialized.")
        return

    base_params = dict(BAL_PROFILE["params"])
    results = []

    for sym, tf, bias_htf in INDICES_TO_EVALUATE:
        df = fetch_index_data(sym, tf)
        if df is None or len(df) < 500:
            print(f"[SKIP] {sym} {tf}: Insufficient bars ({len(df) if df is not None else 0})")
            continue

        date_range = f"{df.index[0]:%Y-%m-%d} to {df.index[-1]:%Y-%m-%d}"

        # 1. Baseline FVG Retest
        p1 = dict(base_params)
        p1["bias_htf"] = bias_htf
        res1 = run_backtest(df, sym, tf, "fvg_retest", STRATEGIES["fvg_retest"].signaller, p1)
        m1 = res1.metrics()

        # 2. Institutional SMC Sweep
        p2 = dict(base_params)
        p2["bias_htf"] = bias_htf
        p2["require_bos"] = False
        p2["require_sweep"] = True
        p2["swing_k"] = 2
        p2["min_rr"] = 0.8
        p2["max_rr"] = 2.5
        res2 = run_backtest(df, sym, tf, "smc_sweep", smc_fvg_confluence_signaller, p2)
        m2 = res2.metrics()

        results.append({
            "sym": sym,
            "tf": tf,
            "bars": len(df),
            "range": date_range,
            # FVG
            "fvg_n": m1.get("n", 0),
            "fvg_wr": m1.get("win_rate", 0.0) * 100,
            "fvg_pf": m1.get("profit_factor", 0.0),
            "fvg_r": m1.get("total_r", 0.0),
            "fvg_dd": m1.get("max_drawdown_r", 0.0),
            "fvg_consec": m1.get("max_consec_losses", 0),
            # SMC
            "smc_n": m2.get("n", 0),
            "smc_wr": m2.get("win_rate", 0.0) * 100,
            "smc_pf": m2.get("profit_factor", 0.0),
            "smc_r": m2.get("total_r", 0.0),
            "smc_dd": m2.get("max_drawdown_r", 0.0),
            "smc_consec": m2.get("max_consec_losses", 0),
        })

    mt5.shutdown()

    # Print Table
    print("\n" + "=" * 115)
    print(f"{'SYMBOL':<10} {'TF':<4} {'BARS':<5} | {'--- MOMENTUM FVG RETEST ---':<38} | {'--- INSTITUTIONAL SMC SWEEP ---':<38} | {'BEST'}")
    print(f"{'':<21} | {'Trades':<6} {'WR':<6} {'PF':<6} {'TotR':<7} {'MaxDD':<6} | {'Trades':<6} {'WR':<6} {'PF':<6} {'TotR':<7} {'MaxDD':<6} |")
    print("-" * 115)

    for r in results:
        f_pf = r['fvg_pf'] if np.isfinite(r['fvg_pf']) else 99.0
        s_pf = r['smc_pf'] if np.isfinite(r['smc_pf']) else 99.0

        best = "NONE"
        if r['fvg_r'] > 0 and r['fvg_r'] > r['smc_r'] and f_pf > 1.2:
            best = "FVG"
        elif r['smc_r'] > 0 and r['smc_r'] >= r['fvg_r'] and s_pf > 1.2:
            best = "SMC"

        print(f"{r['sym']:<10} {r['tf']:<4} {r['bars']:<5} | "
              f"{r['fvg_n']:<6} {r['fvg_wr']:<5.1f}% {r['fvg_pf']:<6.2f} {r['fvg_r']:<+7.2f} {r['fvg_dd']:<6.2f} | "
              f"{r['smc_n']:<6} {r['smc_wr']:<5.1f}% {r['smc_pf']:<6.2f} {r['smc_r']:<+7.2f} {r['smc_dd']:<6.2f} | "
              f"{best}")
    print("=" * 115)

if __name__ == "__main__":
    main()

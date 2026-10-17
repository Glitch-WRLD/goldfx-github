import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from data.tv_data import load_cached
from strategy import candidates
from strategy.profiles import PROFILES, SYMBOL_RUNTIME

def simulate_trade(df_entry, df_ltf, sig, min_mfe_for_reversal=0.5, use_be=True, be_threshold=0.8, use_reversal_exit=True):
    """Simulate a single trade on M15/M5 bars after entry."""
    pos = df_entry.index.get_loc(sig.ts)
    if pos + 2 >= len(df_entry):
        return None
    entry = float(df_entry["open"].iloc[pos + 1])
    sl = entry - sig.dir * sig.sl_offset
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    tp = entry + sig.dir * risk * sig.tp_r if sig.tp_price is None else sig.tp_price
    rr = abs(tp - entry) / risk
    
    sub = df_ltf[df_ltf.index > sig.ts]
    if len(sub) < 3:
        return None

    sl_active = sl
    be_active = False
    peak_mfe = 0.0

    prev_row = None
    for idx, (t, row) in enumerate(sub.iterrows()):
        hi, lo, cl, op = row["high"], row["low"], row["close"], row["open"]
        
        # Current bar MFE
        cur_mfe = (hi - entry) / risk if sig.dir == 1 else (entry - lo) / risk
        peak_mfe = max(peak_mfe, cur_mfe)
        
        # Check BE activation
        if use_be and peak_mfe >= be_threshold and not be_active:
            be_active = True
            sl_active = entry
            
        # Check Reversal Early Exit
        if use_reversal_exit and peak_mfe >= min_mfe_for_reversal and prev_row is not None:
            # Bullish reversal against a SHORT trade
            if sig.dir == -1:
                # Strong green candle closing above previous high
                if cl > op and cl > prev_row["high"]:
                    realized_r = (entry - cl) / risk
                    return {"exit": "REVERSAL_EXIT", "r": realized_r, "peak_mfe": peak_mfe}
            # Bearish reversal against a LONG trade
            elif sig.dir == 1:
                # Strong red candle closing below previous low
                if cl < op and cl < prev_row["low"]:
                    realized_r = (cl - entry) / risk
                    return {"exit": "REVERSAL_EXIT", "r": realized_r, "peak_mfe": peak_mfe}

        # Check SL hit
        if (sig.dir == 1 and lo <= sl_active) or (sig.dir == -1 and hi >= sl_active):
            r_val = 0.0 if be_active else -1.0
            return {"exit": "BE" if be_active else "SL", "r": r_val, "peak_mfe": peak_mfe}
            
        # Check TP hit
        if (sig.dir == 1 and hi >= tp) or (sig.dir == -1 and lo <= tp):
            return {"exit": "TP", "r": rr, "peak_mfe": peak_mfe}
            
        prev_row = row
        
    return None

def main():
    print("=== BACKTESTING SMART REVERSAL EARLY EXIT vs BREAKEVEN vs BASELINE ===", flush=True)
    p = PROFILES["balanced"]["params"]
    strat = candidates.STRATEGIES["fvg_retest"]
    
    symbols = ["XAUUSD", "GBPUSD", "EURUSD", "USDCAD", "AUDUSD"]
    
    modes = [
        ("1. Baseline (Hold to TP or SL)", False, 0.0, False, 0.0),
        ("2. Pure BE @ 0.8R (Current Live)", True, 0.8, False, 0.0),
        ("3. Smart Reversal @ 0.5R (No BE)", False, 0.0, True, 0.5),
        ("4. Smart Reversal @ 0.5R + BE @ 0.8R", True, 0.8, True, 0.5),
        ("5. Smart Reversal @ 0.7R + BE @ 0.8R", True, 0.8, True, 0.7),
    ]
    
    overall_results = {m[0]: [] for m in modes}
    
    for sym in symbols:
        rt = SYMBOL_RUNTIME[sym]
        etf = rt["entry_tf"]
        df_entry = load_cached(sym, etf)
        df_ltf = load_cached(sym, "M15" if etf in ("M30", "H1") else "M5")
        if df_entry is None or df_ltf is None:
            continue
            
        params = dict(p)
        params["_symbol"] = sym
        params["bias_htf"] = rt["bias_htf"]
        
        # If SMC pair, use smc signaller
        if rt.get("strategy") == "smc_sweep":
            sigs = candidates.STRATEGIES["smc_sweep"].signaller(df_entry, params)
        else:
            sigs = strat.signaller(df_entry, params)
            
        print(f"\n--- {sym} ({rt.get('strategy', 'fvg_retest')}, n={len(sigs)} signals) ---", flush=True)
        print(f"{'Exit Strategy':<38} {'Win Rate':<10} {'Profit Factor':<15} {'Total R':<12} {'Avg R':<10}", flush=True)
        
        for name, u_be, be_th, u_rev, rev_th in modes:
            trade_rs = []
            for s in sigs:
                res = simulate_trade(df_entry, df_ltf, s, min_mfe_for_reversal=rev_th,
                                     use_be=u_be, be_threshold=be_th, use_reversal_exit=u_rev)
                if res is not None:
                    trade_rs.append(res["r"])
                    overall_results[name].append(res["r"])
                    
            arr = np.array(trade_rs)
            if len(arr) == 0:
                continue
            w = arr[arr > 0]
            l = arr[arr < 0]
            pf = w.sum() / -l.sum() if -l.sum() > 0 else 99.0
            wr = len(w) / len(arr)
            tot_r = arr.sum()
            avg_r = arr.mean()
            print(f"{name:<38} {wr:<10.1%} {pf:<15.2f} {tot_r:<+12.1f}R {avg_r:<+10.3f}R", flush=True)

    print("\n==========================================================================", flush=True)
    print("=== OVERALL PORTFOLIO SUMMARY (Across All Pairs & Thousands of Trades) ===", flush=True)
    print("==========================================================================", flush=True)
    print(f"{'Exit Strategy':<38} {'Win Rate':<10} {'Profit Factor':<15} {'Total R':<12} {'Avg R':<10}", flush=True)
    for name in overall_results:
        arr = np.array(overall_results[name])
        if len(arr) == 0: continue
        w = arr[arr > 0]
        l = arr[arr < 0]
        pf = w.sum() / -l.sum() if -l.sum() > 0 else 99.0
        wr = len(w) / len(arr)
        tot_r = arr.sum()
        avg_r = arr.mean()
        print(f"{name:<38} {wr:<10.1%} {pf:<15.2f} {tot_r:<+12.1f}R {avg_r:<+10.3f}R", flush=True)

if __name__ == "__main__":
    main()

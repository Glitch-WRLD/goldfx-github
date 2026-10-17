import json
import sys
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.tv_data import load_cached

df_m15 = load_cached("XAUUSD", "M15")
state = json.loads(Path("gha_state/state.json").read_text(encoding="utf-8"))
history = state.get("history", [])
outcomes = state.get("outcomes", {})

xau_trades = []
for h in history:
    if h["symbol"] != "XAUUSD":
        continue
    k = f"{h['symbol']}:{h['ts']}"
    if k in outcomes:
        xau_trades.append((h, outcomes[k]))

print(f"Loaded {len(xau_trades)} matched XAUUSD setups with M15 candle cache ({len(df_m15)} bars).")

def simulate_retrace(trades, max_risk_dollars=6.50, tp_invalidation_pct=0.75, max_wait_bars=16):
    results = []
    
    for h, o in trades:
        sig_ts = pd.to_datetime(h["ts"], utc=True)
        dir_ = 1 if h["dir"] == "LONG" else -1
        entry = float(h["entry"])
        sl = float(h["sl"])
        tp = float(h["tp"])
        orig_rr = float(h.get("rr", 0.7))
        orig_risk = abs(entry - sl)
        orig_reward = abs(tp - entry)
        
        # When outcome concluded
        when = pd.to_datetime(o.get("when", o.get("ts", sig_ts)), utc=True)
        # Get candles after signal up to resolution or max 48 hours
        post_bars = df_m15[(df_m15.index >= sig_ts) & (df_m15.index <= when + pd.Timedelta(hours=24))]
        if post_bars.empty:
            continue
            
        # Target threshold for 75% TP
        inval_price = entry + (tp - entry) * tp_invalidation_pct
        
        # Discount zone threshold price: where distance to SL is <= max_risk_dollars
        if dir_ == 1:
            discount_price = sl + max_risk_dollars
        else:
            discount_price = sl - max_risk_dollars
            
        status = "PENDING"
        filled = False
        fill_price = None
        fill_bar_idx = None
        fill_ts = None
        
        # Walk M15 bars
        bars_list = list(post_bars.iterrows())
        for idx, (bar_ts, row) in enumerate(bars_list[:max_wait_bars]):
            b_open, b_high, b_low, b_close = row["open"], row["high"], row["low"], row["close"]
            
            # Check Invalidation: did price hit 75% TP before entering discount zone?
            if dir_ == 1 and b_high >= inval_price:
                status = "INVALIDATED_TP75"
                break
            elif dir_ == -1 and b_low <= inval_price:
                status = "INVALIDATED_TP75"
                break
                
            # Check Invalidation: did price breach SL entirely before confirmation?
            if dir_ == 1 and b_low <= sl:
                # Did it breach SL directly?
                # Check if it touched discount first or just blasted SL
                status = "BLASTED_SL"
                break
            elif dir_ == -1 and b_high >= sl:
                status = "BLASTED_SL"
                break
                
            # Check if bar touched the discount zone
            touched_discount = False
            if dir_ == 1 and b_low <= discount_price:
                touched_discount = True
            elif dir_ == -1 and b_high >= discount_price:
                touched_discount = True
                
            if touched_discount:
                # Mode 1: Limit Order fill at exact discount threshold
                fill_price = discount_price
                filled = True
                fill_bar_idx = idx
                fill_ts = bar_ts
                status = "FILLED"
                break
                
        if not filled and status == "PENDING":
            status = "EXPIRED_NO_PULLBACK"
            
        # If filled, evaluate subsequent outcome from fill_ts onward
        trade_result = {
            "ref": h.get("ref"),
            "ts": str(sig_ts),
            "dir": h["dir"],
            "orig_entry": entry,
            "orig_sl": sl,
            "orig_tp": tp,
            "orig_risk": orig_risk,
            "orig_outcome": o.get("hit"),
            "status": status,
            "fill_price": fill_price,
            "fill_ts": str(fill_ts) if fill_ts else None,
            "realized_r": 0.0,
            "outcome": None
        }
        
        if filled:
            # Walk from fill_bar_idx forward to determine hit
            subsequent_bars = bars_list[fill_bar_idx:]
            retrace_risk = abs(fill_price - sl)
            retrace_reward = abs(tp - fill_price)
            new_rr = retrace_reward / retrace_risk if retrace_risk > 0 else orig_rr
            
            for b_ts, b_row in subsequent_bars:
                h_b, l_b = b_row["high"], b_row["low"]
                
                # Check SL hit
                sl_hit = (l_b <= sl) if dir_ == 1 else (h_b >= sl)
                # Check TP hit
                tp_hit = (h_b >= tp) if dir_ == 1 else (l_b <= tp)
                
                if sl_hit and tp_hit:
                    # In same bar: conservative assumes SL
                    trade_result["outcome"] = "sl"
                    trade_result["realized_r"] = -1.0
                    break
                elif sl_hit:
                    trade_result["outcome"] = "sl"
                    trade_result["realized_r"] = -1.0
                    break
                elif tp_hit:
                    trade_result["outcome"] = "tp"
                    trade_result["realized_r"] = new_rr
                    break
            if trade_result["outcome"] is None:
                # End of data: mark open or current R
                trade_result["outcome"] = "open"
                trade_result["realized_r"] = 0.0
                
        results.append(trade_result)
        
    return results

# Run baseline simulation across different risk budgets and invalidation parameters
for budget in [6.0, 6.5, 7.0, 8.0, 10.0]:
    res = simulate_retrace(xau_trades, max_risk_dollars=budget, tp_invalidation_pct=0.75)
    filled = [r for r in res if r["status"] == "FILLED"]
    inval_tp = [r for r in res if r["status"] == "INVALIDATED_TP75"]
    blasted = [r for r in res if r["status"] == "BLASTED_SL"]
    expired = [r for r in res if r["status"] == "EXPIRED_NO_PULLBACK"]
    
    tp_wins = [r for r in filled if r["outcome"] == "tp"]
    sl_losses = [r for r in filled if r["outcome"] == "sl"]
    
    total_r = sum(r["realized_r"] for r in filled)
    wr = len(tp_wins) / len(filled) * 100 if filled else 0
    
    print(f"\n--- BUDGET: ${budget:.2f} (0.01 lot = ${budget:.2f} max risk) ---")
    print(f"Total Setups: {len(res)} | Filled: {len(filled)} | Missed (75% TP takeoff): {len(inval_tp)} | Blasted SL directly: {len(blasted)} | Expired: {len(expired)}")
    print(f"Of Filled ({len(filled)}): Wins: {len(tp_wins)} ({wr:.1f}%), Losses: {len(sl_losses)}")
    print(f"Total R on Filled: {total_r:+.2f}R")
    if tp_wins:
        avg_win_r = sum(r["realized_r"] for r in tp_wins) / len(tp_wins)
        print(f"Avg Winning R: {avg_win_r:.2f}R (vs baseline ~0.7R - 1.4R)")

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

def simulate_advanced_sniper(trades, 
                             discount_trigger_dist=7.50, # price enters zone where dist to SL <= 7.50
                             max_entry_risk=8.50,        # max allowed risk on confirmation close
                             tp_inval_pct=0.75,          # cancel if 75% TP reached before pullback
                             use_confirmation=True,      # wait for reversal candle
                             be_at_orig_entry=True):     # move SL to BE when original entry reached
    results = []
    
    for h, o in trades:
        sig_ts = pd.to_datetime(h["ts"], utc=True)
        dir_ = 1 if h["dir"] == "LONG" else -1
        entry = float(h["entry"])
        sl = float(h["sl"])
        tp = float(h["tp"])
        orig_rr = float(h.get("rr", 0.7))
        orig_risk = abs(entry - sl)
        
        when = pd.to_datetime(o.get("when", o.get("ts", sig_ts)), utc=True)
        post_bars = df_m15[(df_m15.index >= sig_ts) & (df_m15.index <= when + pd.Timedelta(hours=36))]
        if post_bars.empty:
            continue
            
        inval_price = entry + (tp - entry) * tp_inval_pct
        zone_price = sl + discount_trigger_dist if dir_ == 1 else sl - discount_trigger_dist
        
        status = "PENDING"
        filled = False
        fill_price = None
        fill_bar_idx = None
        in_zone = False
        
        bars_list = list(post_bars.iterrows())
        for idx, (bar_ts, row) in enumerate(bars_list[:24]): # max 6 hours wait (24 M15 bars)
            b_open, b_high, b_low, b_close = row["open"], row["high"], row["low"], row["close"]
            
            # Check Invalidation: 75% TP hit first
            if (dir_ == 1 and b_high >= inval_price) or (dir_ == -1 and b_low <= inval_price):
                status = "INVALIDATED_TP75"
                break
                
            # Check Invalidation: SL breached before confirmation
            if (dir_ == 1 and b_low <= sl) or (dir_ == -1 and b_high >= sl):
                # If price penetrated SL
                status = "SL_BREACHED_BEFORE_ENTRY"
                break
                
            # Check if entered discount zone
            if not in_zone:
                if (dir_ == 1 and b_low <= zone_price) or (dir_ == -1 and b_high >= zone_price):
                    in_zone = True
                    
            if in_zone:
                if not use_confirmation:
                    fill_price = zone_price
                    filled = True
                    fill_bar_idx = idx
                    status = "FILLED"
                    break
                else:
                    # Check confirmation candle:
                    # For Long: candle closes bullish (close > open) or strong lower wick
                    # For Short: candle closes bearish (close < open) or strong upper wick
                    is_bullish_reversal = (b_close > b_open) and (b_close > (b_low + (b_high - b_low) * 0.5))
                    is_bearish_reversal = (b_close < b_open) and (b_close < (b_low + (b_high - b_low) * 0.5))
                    
                    confirmed = is_bullish_reversal if dir_ == 1 else is_bearish_reversal
                    if confirmed:
                        conf_risk = abs(b_close - sl)
                        # Only enter if the confirmation close is still within acceptable risk
                        if conf_risk <= max_entry_risk:
                            fill_price = b_close
                            filled = True
                            fill_bar_idx = idx
                            status = "FILLED"
                            break
                        else:
                            # Reversal candle exploded too fast, pushed risk outside budget!
                            status = "REVERSAL_TOO_EXPENSIVE"
                            break
                            
        if not filled and status == "PENDING":
            status = "EXPIRED"
            
        trade_res = {
            "ref": h.get("ref"),
            "status": status,
            "filled": filled,
            "orig_outcome": o.get("hit"),
            "orig_risk": orig_risk,
            "fill_price": fill_price,
            "realized_r": 0.0,
            "exit_type": None
        }
        
        if filled:
            actual_risk = abs(fill_price - sl)
            actual_reward = abs(tp - fill_price)
            sniper_rr = actual_reward / actual_risk if actual_risk > 0 else orig_rr
            
            subsequent = bars_list[fill_bar_idx + 1:]
            hit_orig_entry = False
            exit_r = None
            
            for b_ts, b_row in subsequent:
                h_b, l_b = b_row["high"], b_row["low"]
                
                # Check if reached original entry (Breakeven trigger)
                if be_at_orig_entry:
                    if (dir_ == 1 and h_b >= entry) or (dir_ == -1 and l_b <= entry):
                        hit_orig_entry = True
                        
                # If hit orig entry, our SL is moved to fill_price (BE)
                current_sl = fill_price if hit_orig_entry else sl
                
                # Check SL
                sl_hit = (l_b <= current_sl) if dir_ == 1 else (h_b >= current_sl)
                # Check TP
                tp_hit = (h_b >= tp) if dir_ == 1 else (l_b <= tp)
                
                if sl_hit and hit_orig_entry:
                    exit_r = 0.0
                    trade_res["exit_type"] = "BE"
                    break
                elif sl_hit and not hit_orig_entry:
                    exit_r = -1.0
                    trade_res["exit_type"] = "SL"
                    break
                elif tp_hit:
                    exit_r = sniper_rr
                    trade_res["exit_type"] = "TP"
                    break
                    
            if exit_r is None:
                exit_r = 0.0
                trade_res["exit_type"] = "TIMEOUT"
                
            trade_res["realized_r"] = exit_r
            trade_res["sniper_rr"] = sniper_rr
            trade_res["actual_risk"] = actual_risk
            
        results.append(trade_res)
        
    return results

print("\n================================================================================")
print("EXPERIMENT A: LIMIT ORDER (NO CONFIRMATION) vs CONFIRMED REVERSAL vs CONFIRMED + BE")
print("================================================================================")

configs = [
    ("1. Pure Limit Order ($7.00 Zone, No Conf, No BE)", 7.0, 7.0, False, False),
    ("2. Pure Limit Order ($7.00 Zone, No Conf, WITH BE @ Orig Entry)", 7.0, 7.0, False, True),
    ("3. Confirmed M15 Reversal (Zone $7.50, Max Risk $8.50, No BE)", 7.5, 8.5, True, False),
    ("4. Confirmed M15 Reversal (Zone $7.50, Max Risk $8.50, WITH BE @ Orig Entry)", 7.5, 8.5, True, True),
    ("5. Confirmed M15 Reversal (Zone $6.50, Max Risk $7.50, WITH BE @ Orig Entry)", 6.5, 7.5, True, True),
]

for name, zone, max_r, conf, be in configs:
    res = simulate_advanced_sniper(xau_trades, discount_trigger_dist=zone, max_entry_risk=max_r, 
                                   use_confirmation=conf, be_at_orig_entry=be)
    filled = [r for r in res if r["filled"]]
    tp_wins = [r for r in filled if r["exit_type"] == "TP"]
    be_exits = [r for r in filled if r["exit_type"] == "BE"]
    sl_losses = [r for r in filled if r["exit_type"] == "SL"]
    
    total_r = sum(r["realized_r"] for r in filled)
    wr = len(tp_wins) / len(filled) * 100 if filled else 0
    
    print(f"\n>>> {name}")
    print(f"Total Setups: {len(res)} | Filled: {len(filled)} (Fill Rate: {len(filled)/len(res)*100:.1f}%)")
    print(f"Outcomes: TP Wins = {len(tp_wins)}, Breakeven = {len(be_exits)}, Hard SL = {len(sl_losses)}")
    print(f"Win Rate (TP): {wr:.1f}% | Win + BE Rate: {(len(tp_wins)+len(be_exits))/len(filled)*100 if filled else 0:.1f}%")
    print(f"Total Realized R: {total_r:+.2f}R")
    if tp_wins:
        avg_win_r = sum(r["realized_r"] for r in tp_wins) / len(tp_wins)
        print(f"Avg Realized R on Wins: {avg_win_r:.2f}R")

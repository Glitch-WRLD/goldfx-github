import json
import sys
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.tv_data import load_cached

df_m5 = load_cached("XAUUSD", "M5")
state = json.loads(Path("gha_state/state.json").read_text(encoding="utf-8"))
history = state.get("history", [])
outcomes = state.get("outcomes", {})

xau_trades = [(h, outcomes[f"{h['symbol']}:{h['ts']}"]) for h in history if h["symbol"] == "XAUUSD" and f"{h['symbol']}:{h['ts']}" in outcomes]

def simulate_micro_rebound(touch_zone=6.00, rebound_buffer=1.00, use_be=True):
    # touch_zone: price must reach SL + touch_zone (e.g. $6.00)
    # rebound_buffer: entry triggers on bounce to SL + touch_zone + rebound_buffer (e.g. $7.00 risk)
    # If price breaches SL before bouncing to rebound_buffer -> avoided!
    
    results = []
    
    for h, o in xau_trades:
        sig_ts = pd.to_datetime(h["ts"], utc=True)
        dir_ = 1 if h["dir"] == "LONG" else -1
        entry = float(h["entry"])
        sl = float(h["sl"])
        tp = float(h["tp"])
        orig_rr = float(h.get("rr", 0.7))
        
        when = pd.to_datetime(o.get("when", o.get("ts", sig_ts)), utc=True)
        post_bars = df_m5[(df_m5.index >= sig_ts) & (df_m5.index <= when + pd.Timedelta(hours=36))]
        if post_bars.empty:
            continue
            
        inval_price = entry + (tp - entry) * 0.75
        sweep_level = sl + touch_zone if dir_ == 1 else sl - touch_zone
        fill_trigger = sl + touch_zone + rebound_buffer if dir_ == 1 else sl - touch_zone - rebound_buffer
        
        status = "PENDING"
        swept = False
        filled = False
        fill_price = None
        fill_bar_idx = None
        
        bars_list = list(post_bars.iterrows())
        for idx, (b_ts, row) in enumerate(bars_list[:72]):
            b_open, b_high, b_low, b_close = row["open"], row["high"], row["low"], row["close"]
            
            # Invalidation: 75% TP first
            if (dir_ == 1 and b_high >= inval_price) or (dir_ == -1 and b_low <= inval_price):
                status = "INVALIDATED_TP75"
                break
                
            # Check sweep
            if not swept:
                if (dir_ == 1 and b_low <= sweep_level) or (dir_ == -1 and b_high >= sweep_level):
                    swept = True
                    
            if swept:
                # Check if SL breached before rebound
                if (dir_ == 1 and b_low <= sl) or (dir_ == -1 and b_high >= sl):
                    # Price sliced through SL!
                    # Did it hit the rebound trigger first in the same bar, or just crash?
                    # If open was above sweep and low breached SL, check if it bounced
                    status = "BLASTED_SL_AVOIDED"
                    break
                    
                # Check if rebounded to trigger
                if (dir_ == 1 and b_high >= fill_trigger) or (dir_ == -1 and b_low <= fill_trigger):
                    fill_price = fill_trigger
                    filled = True
                    fill_bar_idx = idx
                    status = "FILLED"
                    break
                    
        if not filled and status == "PENDING":
            status = "EXPIRED"
            
        trade_res = {
            "ref": h.get("ref"),
            "status": status,
            "filled": filled,
            "fill_price": fill_price,
            "exit_type": None,
            "realized_r": 0.0
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
                if use_be:
                    if (dir_ == 1 and h_b >= entry) or (dir_ == -1 and l_b <= entry):
                        hit_orig_entry = True
                current_sl = fill_price if hit_orig_entry else sl
                
                sl_hit = (l_b <= current_sl) if dir_ == 1 else (h_b >= current_sl)
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
                trade_res["exit_type"] = "OPEN"
                
            trade_res["realized_r"] = exit_r
            trade_res["sniper_rr"] = sniper_rr
            trade_res["actual_risk"] = actual_risk
            
        results.append(trade_res)
    return results

print("\n" + "="*80)
print("EXPERIMENT: TOUCH + MICRO-REBOUND TRIGGER (TICK REVERSAL)")
print("="*80)

rebound_configs = [
    ("Sweep to $6.00 + Rebound $0.80 (Entry Risk $6.80, WITH BE)", 6.0, 0.80, True),
    ("Sweep to $6.00 + Rebound $1.00 (Entry Risk $7.00, WITH BE)", 6.0, 1.00, True),
    ("Sweep to $6.50 + Rebound $0.80 (Entry Risk $7.30, WITH BE)", 6.5, 0.80, True),
    ("Sweep to $6.50 + Rebound $1.00 (Entry Risk $7.50, WITH BE)", 6.5, 1.00, True),
    ("Sweep to $7.00 + Rebound $1.00 (Entry Risk $8.00, WITH BE)", 7.0, 1.00, True),
]

for name, sweep, reb, be in rebound_configs:
    res = simulate_micro_rebound(touch_zone=sweep, rebound_buffer=reb, use_be=be)
    filled = [r for r in res if r["filled"]]
    tp_wins = [r for r in filled if r["exit_type"] == "TP"]
    be_exits = [r for r in filled if r["exit_type"] == "BE"]
    sl_losses = [r for r in filled if r["exit_type"] == "SL"]
    avoided_sl = [r for r in res if r["status"] == "BLASTED_SL_AVOIDED"]
    
    total_r = sum(r["realized_r"] for r in filled)
    wr = len(tp_wins) / len(filled) * 100 if filled else 0
    safe_rate = (len(tp_wins) + len(be_exits)) / len(filled) * 100 if filled else 0
    
    print(f"\n>>> {name}")
    print(f"Total Setups: {len(res)} | Filled: {len(filled)} | Smashed SL without rebound (AVOIDED): {len(avoided_sl)}")
    print(f"Outcomes: TP Wins = {len(tp_wins)}, Breakeven = {len(be_exits)}, Hard SL = {len(sl_losses)}")
    print(f"Win Rate (TP): {wr:.1f}% | Win + BE (Capital Preserved): {safe_rate:.1f}%")
    print(f"Total Realized R: {total_r:+.2f}R")
    if tp_wins:
        avg_win_r = sum(r["realized_r"] for r in tp_wins) / len(tp_wins)
        print(f"Avg Realized R on Wins: {avg_win_r:.2f}R")

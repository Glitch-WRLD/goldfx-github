import json
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.tv_data import load_cached

df_m5 = load_cached("XAUUSD", "M5")
state = json.loads(Path("gha_state/state.json").read_text(encoding="utf-8"))
history = state.get("history", [])
outcomes = state.get("outcomes", {})

xau_trades = [(h, outcomes[f"{h['symbol']}:{h['ts']}"]) for h in history if h["symbol"] == "XAUUSD" and f"{h['symbol']}:{h['ts']}" in outcomes]

def simulate_trailing_exit(touch_zone=6.50, trail_mode="NONE"):
    # trail_mode:
    # "NONE": hold to TP or SL
    # "BE_AT_ENTRY": SL to BE when price touches original entry
    # "HALF_SL_AT_ENTRY": SL moved to -0.5R when price touches original entry
    # "BE_AT_50PCT_TP": SL to BE when price reaches 50% distance to TP
    
    results = []
    
    for h, o in xau_trades:
        sig_ts = pd.to_datetime(h["ts"], utc=True)
        dir_ = 1 if h["dir"] == "LONG" else -1
        entry = float(h["entry"])
        sl = float(h["sl"])
        tp = float(h["tp"])
        orig_rr = float(h.get("rr", 0.7))
        
        when = pd.to_datetime(o.get("when", o.get("ts", sig_ts)), utc=True)
        post_bars = df_m5[(df_m5.index >= sig_ts) & (df_m5.index <= when + pd.Timedelta(hours=48))]
        if post_bars.empty:
            continue
            
        inval_price = entry + (tp - entry) * 0.75
        fill_price = sl + touch_zone if dir_ == 1 else sl - touch_zone
        
        status = "PENDING"
        filled = False
        fill_bar_idx = None
        
        bars_list = list(post_bars.iterrows())
        for idx, (b_ts, row) in enumerate(bars_list[:72]):
            b_high, b_low = row["high"], row["low"]
            
            # Invalidation: 75% TP first
            if (dir_ == 1 and b_high >= inval_price) or (dir_ == -1 and b_low <= inval_price):
                status = "INVALIDATED_TP75"
                break
                
            # Check touch
            if (dir_ == 1 and b_low <= fill_price) or (dir_ == -1 and b_high >= fill_price):
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
            "exit_type": None,
            "realized_r": 0.0
        }
        
        if filled:
            actual_risk = abs(fill_price - sl)
            actual_reward = abs(tp - fill_price)
            sniper_rr = actual_reward / actual_risk if actual_risk > 0 else orig_rr
            
            subsequent = bars_list[fill_bar_idx + 1:]
            active_sl = sl
            tp_50_price = fill_price + (tp - fill_price) * 0.50
            
            for b_ts, b_row in subsequent:
                h_b, l_b = b_row["high"], b_row["low"]
                
                # Check trail triggers
                if trail_mode == "BE_AT_ENTRY":
                    if (dir_ == 1 and h_b >= entry) or (dir_ == -1 and l_b <= entry):
                        active_sl = fill_price # BE
                elif trail_mode == "HALF_SL_AT_ENTRY":
                    if (dir_ == 1 and h_b >= entry) or (dir_ == -1 and l_b <= entry):
                        active_sl = fill_price - (fill_price - sl) * 0.5 if dir_ == 1 else fill_price + (sl - fill_price) * 0.5
                elif trail_mode == "BE_AT_50PCT_TP":
                    if (dir_ == 1 and h_b >= tp_50_price) or (dir_ == -1 and l_b <= tp_50_price):
                        active_sl = fill_price # BE
                        
                sl_hit = (l_b <= active_sl) if dir_ == 1 else (h_b >= active_sl)
                tp_hit = (h_b >= tp) if dir_ == 1 else (l_b <= tp)
                
                if sl_hit:
                    if active_sl == fill_price:
                        trade_res["exit_type"] = "BE"
                        trade_res["realized_r"] = 0.0
                    elif active_sl != sl:
                        trade_res["exit_type"] = "HALF_SL"
                        trade_res["realized_r"] = -0.5
                    else:
                        trade_res["exit_type"] = "FULL_SL"
                        trade_res["realized_r"] = -1.0
                    break
                elif tp_hit:
                    trade_res["exit_type"] = "TP"
                    trade_res["realized_r"] = sniper_rr
                    break
                    
            if trade_res["exit_type"] is None:
                trade_res["exit_type"] = "OPEN"
                trade_res["realized_r"] = 0.0
                
            trade_res["sniper_rr"] = sniper_rr
            trade_res["actual_risk"] = actual_risk
            
        results.append(trade_res)
    return results

print("="*80)
print("EXPERIMENT: TRAILING EXIT STRATEGIES ON RETRACED ENTRIES (Zone = $6.50)")
print("="*80)

for mode, label in [
    ("NONE", "1. Pure Asymmetric Hold (TP or Hard SL)"),
    ("HALF_SL_AT_ENTRY", "2. Reduce Risk to -0.5R ($3.25) when Original Entry Reached"),
    ("BE_AT_ENTRY", "3. Move SL to BE ($0.0) when Original Entry Reached"),
    ("BE_AT_50PCT_TP", "4. Move SL to BE ($0.0) when 50% of Distance to TP Reached"),
]:
    res = simulate_trailing_exit(touch_zone=6.50, trail_mode=mode)
    filled = [r for r in res if r["filled"]]
    tp_wins = [r for r in filled if r["exit_type"] == "TP"]
    be_exits = [r for r in filled if r["exit_type"] == "BE"]
    half_sl = [r for r in filled if r["exit_type"] == "HALF_SL"]
    full_sl = [r for r in filled if r["exit_type"] == "FULL_SL"]
    
    total_r = sum(r["realized_r"] for r in filled)
    wr = len(tp_wins) / len(filled) * 100 if filled else 0
    
    print(f"\n>>> {label}")
    print(f"Filled: {len(filled)} | TP Wins: {len(tp_wins)} ({wr:.1f}%) | BE Exits: {len(be_exits)} | Half-SL: {len(half_sl)} | Full SL: {len(full_sl)}")
    print(f"Total Realized R: {total_r:+.2f}R")
    if tp_wins:
        avg_win_r = sum(r["realized_r"] for r in tp_wins) / len(tp_wins)
        print(f"Avg Realized R on Wins: {avg_win_r:.2f}R")

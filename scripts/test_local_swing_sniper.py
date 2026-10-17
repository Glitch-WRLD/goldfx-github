import sys
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.tv_data import load_cached
from strategy import candidates
from strategy.profiles import PROFILES

df_m30 = load_cached("XAUUSD", "M30")
df_m5 = load_cached("XAUUSD", "M5")
df_m15 = load_cached("XAUUSD", "M15")

# Test on 29 live delivered setups first
import json
state = json.loads(Path("gha_state/state.json").read_text(encoding="utf-8"))
history = state.get("history", [])
outcomes = state.get("outcomes", {})

xau_live = [(h, outcomes[f"{h['symbol']}:{h['ts']}"]) for h in history if h["symbol"] == "XAUUSD" and f"{h['symbol']}:{h['ts']}" in outcomes]

def evaluate_local_swing_sniper(trades_source, max_allowed_risk=6.50, max_wait_m5_bars=36):
    results = []
    
    for h, o in trades_source:
        sig_ts = pd.to_datetime(h["ts"], utc=True)
        dir_ = 1 if h["dir"] == "LONG" else -1
        entry_base = float(h["entry"])
        orig_sl = float(h["sl"])
        tp = float(h["tp"])
        
        post_m5 = df_m5[(df_m5.index >= sig_ts) & (df_m5.index <= sig_ts + pd.Timedelta(hours=18))]
        if len(post_m5) < 10:
            continue
            
        tp_75 = entry_base + (tp - entry_base) * 0.75
        
        filled = False
        fill_price = None
        fill_sl = None
        fill_idx = None
        status = "PENDING"
        
        bars_list = list(post_m5.iterrows())
        
        # Look for pullback into discount (at least 25% of distance to orig_sl)
        pullback_threshold = entry_base - dir_ * (abs(entry_base - orig_sl) * 0.25)
        had_pullback = False
        
        for idx in range(1, min(len(bars_list), max_wait_m5_bars)):
            cur_ts, cur_bar = bars_list[idx]
            prev_ts, prev_bar = bars_list[idx - 1]
            
            # Invalidation: TP75 reached before entry
            if (dir_ == 1 and cur_bar["high"] >= tp_75) or (dir_ == -1 and cur_bar["low"] <= tp_75):
                status = "INVALIDATED_TP75"
                break
                
            # Invalidation: original SL breached
            if (dir_ == 1 and cur_bar["low"] <= orig_sl) or (dir_ == -1 and cur_bar["high"] >= orig_sl):
                status = "ORIG_SL_BREACHED"
                break
                
            # Check pullback
            if not had_pullback:
                if (dir_ == 1 and cur_bar["low"] <= pullback_threshold) or (dir_ == -1 and cur_bar["high"] >= pullback_threshold):
                    had_pullback = True
                    
            if had_pullback:
                # Look for M5 reversal confirmation:
                # Long: cur_bar closes green (close > open) AND close > prev_bar high (engulfing/shift)
                # Short: cur_bar closes red (close < open) AND close < prev_bar low
                is_reversal = False
                candidate_sl = None
                
                if dir_ == 1:
                    if cur_bar["close"] > cur_bar["open"] and cur_bar["close"] >= prev_bar["high"]:
                        is_reversal = True
                        candidate_sl = min(cur_bar["low"], prev_bar["low"]) - 0.50 # 50 cent buffer
                else:
                    if cur_bar["close"] < cur_bar["open"] and cur_bar["close"] <= prev_bar["low"]:
                        is_reversal = True
                        candidate_sl = max(cur_bar["high"], prev_bar["high"]) + 0.50
                        
                if is_reversal and candidate_sl is not None:
                    candidate_risk = abs(cur_bar["close"] - candidate_sl)
                    if candidate_risk <= max_allowed_risk and candidate_risk >= 1.50: # sensible risk range: $1.50 to $6.50
                        fill_price = cur_bar["close"]
                        fill_sl = candidate_sl
                        fill_idx = idx
                        filled = True
                        status = "FILLED"
                        break
                        
        if not filled and status == "PENDING":
            status = "EXPIRED"
            
        trade_res = {
            "ref": h.get("ref"),
            "status": status,
            "filled": filled,
            "fill_price": fill_price,
            "fill_sl": fill_sl,
            "realized_r": 0.0,
            "exit_type": None
        }
        
        if filled:
            risk = abs(fill_price - fill_sl)
            reward = abs(tp - fill_price)
            rr = reward / risk if risk > 0 else 1.0
            
            subsequent = bars_list[fill_idx + 1:]
            for b_ts, b_row in subsequent:
                # Check SL
                sl_hit = (b_row["low"] <= fill_sl) if dir_ == 1 else (b_row["high"] >= fill_sl)
                # Check TP
                tp_hit = (b_row["high"] >= tp) if dir_ == 1 else (b_row["low"] <= tp)
                
                if sl_hit and tp_hit:
                    trade_res["exit_type"] = "SL"
                    trade_res["realized_r"] = -1.0
                    break
                elif sl_hit:
                    trade_res["exit_type"] = "SL"
                    trade_res["realized_r"] = -1.0
                    break
                elif tp_hit:
                    trade_res["exit_type"] = "TP"
                    trade_res["realized_r"] = rr
                    break
                    
            if trade_res["exit_type"] is None:
                trade_res["exit_type"] = "TIMEOUT"
                trade_res["realized_r"] = 0.0
                
            trade_res["sniper_rr"] = rr
            trade_res["risk"] = risk
            
        results.append(trade_res)
    return results

print("="*80)
print("EXPERIMENT: LOCAL M5 SWING SNIPER ON LIVE DELIVERED SETUPS (n=29)")
print("="*80)

for max_r in [5.0, 6.0, 6.5, 7.5]:
    res = evaluate_local_swing_sniper(xau_live, max_allowed_risk=max_r)
    filled = [r for r in res if r["filled"]]
    tp_wins = [r for r in filled if r["exit_type"] == "TP"]
    sl_losses = [r for r in filled if r["exit_type"] == "SL"]
    
    total_r = sum(r["realized_r"] for r in filled)
    wr = len(tp_wins) / len(filled) * 100 if filled else 0
    
    print(f"\n>>> Max Allowed Risk: ${max_r:.2f}")
    print(f"Total: {len(res)} | Filled: {len(filled)} (Fill Rate: {len(filled)/len(res)*100:.1f}%) | Wins: {len(tp_wins)} ({wr:.1f}%) | Losses: {len(sl_losses)}")
    print(f"Total Realized R: {total_r:+.2f}R")
    if tp_wins:
        avg_w = sum(r["realized_r"] for r in tp_wins) / len(tp_wins)
        avg_risk = sum(r["risk"] for r in filled) / len(filled)
        print(f"Avg Realized R on Wins: {avg_w:.2f}R | Avg Actual Risk: ${avg_risk:.2f}")

import sys
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.tv_data import load_cached
import json

df_m5 = load_cached("XAUUSD", "M5")
state = json.loads(Path("gha_state/state.json").read_text(encoding="utf-8"))
history = state.get("history", [])
outcomes = state.get("outcomes", {})
xau_live = [(h, outcomes[f"{h['symbol']}:{h['ts']}"]) for h in history if h["symbol"] == "XAUUSD" and f"{h['symbol']}:{h['ts']}" in outcomes]

def evaluate_with_be(max_allowed_risk=7.50, be_mode="50PCT_TP"):
    results = []
    for h, o in xau_live:
        sig_ts = pd.to_datetime(h["ts"], utc=True)
        dir_ = 1 if h["dir"] == "LONG" else -1
        entry_base = float(h["entry"])
        orig_sl = float(h["sl"])
        tp = float(h["tp"])
        
        post_m5 = df_m5[(df_m5.index >= sig_ts) & (df_m5.index <= sig_ts + pd.Timedelta(hours=24))]
        if len(post_m5) < 10:
            continue
            
        tp_75 = entry_base + (tp - entry_base) * 0.75
        pullback_threshold = entry_base - dir_ * (abs(entry_base - orig_sl) * 0.25)
        
        filled = False
        fill_price, fill_sl, fill_idx = None, None, None
        had_pullback = False
        bars_list = list(post_m5.iterrows())
        
        for idx in range(1, min(len(bars_list), 36)):
            cur_ts, cur_bar = bars_list[idx]
            prev_ts, prev_bar = bars_list[idx - 1]
            
            if (dir_ == 1 and cur_bar["high"] >= tp_75) or (dir_ == -1 and cur_bar["low"] <= tp_75):
                break
            if (dir_ == 1 and cur_bar["low"] <= orig_sl) or (dir_ == -1 and cur_bar["high"] >= orig_sl):
                break
                
            if not had_pullback:
                if (dir_ == 1 and cur_bar["low"] <= pullback_threshold) or (dir_ == -1 and cur_bar["high"] >= pullback_threshold):
                    had_pullback = True
                    
            if had_pullback:
                is_reversal = False
                candidate_sl = None
                if dir_ == 1:
                    if cur_bar["close"] > cur_bar["open"] and cur_bar["close"] >= prev_bar["high"]:
                        is_reversal = True
                        candidate_sl = min(cur_bar["low"], prev_bar["low"]) - 0.50
                else:
                    if cur_bar["close"] < cur_bar["open"] and cur_bar["close"] <= prev_bar["low"]:
                        is_reversal = True
                        candidate_sl = max(cur_bar["high"], prev_bar["high"]) + 0.50
                        
                if is_reversal and candidate_sl is not None:
                    candidate_risk = abs(cur_bar["close"] - candidate_sl)
                    if candidate_risk <= max_allowed_risk and candidate_risk >= 1.50:
                        fill_price, fill_sl, fill_idx = cur_bar["close"], candidate_sl, idx
                        filled = True
                        break
                        
        if filled:
            risk = abs(fill_price - fill_sl)
            reward = abs(tp - fill_price)
            rr = reward / risk if risk > 0 else 1.0
            
            subsequent = bars_list[fill_idx + 1:]
            hit_be_trigger = False
            exit_r = None
            exit_type = None
            
            be_level = (fill_price + (tp - fill_price) * 0.40) if be_mode == "40PCT_TP" else entry_base
            
            for b_ts, b_row in subsequent:
                # check trigger
                if (dir_ == 1 and b_row["high"] >= be_level) or (dir_ == -1 and b_row["low"] <= be_level):
                    hit_be_trigger = True
                active_sl = fill_price if hit_be_trigger else fill_sl
                
                sl_hit = (b_row["low"] <= active_sl) if dir_ == 1 else (b_row["high"] >= active_sl)
                tp_hit = (b_row["high"] >= tp) if dir_ == 1 else (b_row["low"] <= tp)
                
                if sl_hit:
                    if hit_be_trigger:
                        exit_r = 0.0
                        exit_type = "BE"
                    else:
                        exit_r = -1.0
                        exit_type = "SL"
                    break
                elif tp_hit:
                    exit_r = rr
                    exit_type = "TP"
                    break
            if exit_r is not None:
                results.append({"exit_type": exit_type, "realized_r": exit_r, "risk": risk})
    return results

for mode in ["ENTRY", "40PCT_TP"]:
    res = evaluate_with_be(max_allowed_risk=7.50, be_mode=mode)
    wins = [r for r in res if r["exit_type"] == "TP"]
    bes = [r for r in res if r["exit_type"] == "BE"]
    losses = [r for r in res if r["exit_type"] == "SL"]
    tot_r = sum(r["realized_r"] for r in res)
    print(f"Mode: {mode} | Filled: {len(res)} | Wins: {len(wins)} | BE: {len(bes)} | Losses: {len(losses)} | TotR: {tot_r:+.2f}R")

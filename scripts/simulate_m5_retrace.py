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

print(f"Loaded {len(xau_trades)} XAUUSD setups. M5 bars: {len(df_m5)} from {df_m5.index[0]} to {df_m5.index[-1]}")

def test_m5_strategy(discount_zone=7.00, max_entry_risk=8.00, use_m5_conf=True, use_be=True):
    results = []
    
    for h, o in xau_trades:
        sig_ts = pd.to_datetime(h["ts"], utc=True)
        dir_ = 1 if h["dir"] == "LONG" else -1
        entry = float(h["entry"])
        sl = float(h["sl"])
        tp = float(h["tp"])
        orig_rr = float(h.get("rr", 0.7))
        orig_risk = abs(entry - sl)
        
        when = pd.to_datetime(o.get("when", o.get("ts", sig_ts)), utc=True)
        post_bars = df_m5[(df_m5.index >= sig_ts) & (df_m5.index <= when + pd.Timedelta(hours=36))]
        if post_bars.empty:
            continue
            
        inval_price = entry + (tp - entry) * 0.75
        zone_price = sl + discount_zone if dir_ == 1 else sl - discount_zone
        
        status = "PENDING"
        filled = False
        fill_price = None
        fill_bar_idx = None
        in_zone = False
        
        bars_list = list(post_bars.iterrows())
        # Max wait 72 M5 bars = 6 hours
        for idx, (bar_ts, row) in enumerate(bars_list[:72]):
            b_open, b_high, b_low, b_close = row["open"], row["high"], row["low"], row["close"]
            
            # 1. Invalidation: 75% TP reached before discount zone
            if (dir_ == 1 and b_high >= inval_price) or (dir_ == -1 and b_low <= inval_price):
                status = "INVALIDATED_TP75"
                break
                
            # 2. Invalidation: SL breached before confirmation
            if (dir_ == 1 and b_low <= sl) or (dir_ == -1 and b_high >= sl):
                status = "SL_BREACHED"
                break
                
            # 3. Check if entered discount zone
            if not in_zone:
                if (dir_ == 1 and b_low <= zone_price) or (dir_ == -1 and b_high >= zone_price):
                    in_zone = True
                    
            if in_zone:
                if not use_m5_conf:
                    # Pure limit order
                    fill_price = zone_price
                    filled = True
                    fill_bar_idx = idx
                    status = "FILLED"
                    break
                else:
                    # M5 confirmation:
                    # Bullish candle (close > open) or lower rejection wick
                    is_bull = (b_close > b_open) or ((b_close - b_low) > 0.6 * (b_high - b_low) and (b_high - b_low) > 1.0)
                    is_bear = (b_close < b_open) or ((b_high - b_close) > 0.6 * (b_high - b_low) and (b_high - b_low) > 1.0)
                    confirmed = is_bull if dir_ == 1 else is_bear
                    
                    if confirmed:
                        c_risk = abs(b_close - sl)
                        if c_risk <= max_entry_risk:
                            fill_price = b_close
                            filled = True
                            fill_bar_idx = idx
                            status = "FILLED"
                            break
                        else:
                            # Too expensive on this bar, keep watching next bar if still in zone
                            pass
                            
        if not filled and status == "PENDING":
            status = "EXPIRED"
            
        trade_res = {
            "ref": h.get("ref"),
            "status": status,
            "filled": filled,
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
                
                # Check Breakeven trigger: price touched original entry
                if use_be:
                    if (dir_ == 1 and h_b >= entry) or (dir_ == -1 and l_b <= entry):
                        hit_orig_entry = True
                        
                current_sl = fill_price if hit_orig_entry else sl
                
                # SL hit
                sl_hit = (l_b <= current_sl) if dir_ == 1 else (h_b >= current_sl)
                # TP hit
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
print("EXPERIMENT: M5 CONFIRMATION VS LIMIT ORDER UNDER DIFFERENT RISK BUDGETS")
print("="*80)

scenarios = [
    ("A. Limit Order at $6.50 (No Conf, No BE)", 6.5, 6.5, False, False),
    ("B. Limit Order at $6.50 (No Conf, WITH BE @ Orig Entry)", 6.5, 6.5, False, True),
    ("C. Limit Order at $7.50 (No Conf, WITH BE @ Orig Entry)", 7.5, 7.5, False, True),
    ("D. M5 Reversal Candle (Zone $6.50, Max Risk $7.50, WITH BE)", 6.5, 7.5, True, True),
    ("E. M5 Reversal Candle (Zone $7.00, Max Risk $8.00, WITH BE)", 7.0, 8.0, True, True),
    ("F. M5 Reversal Candle (Zone $7.50, Max Risk $8.50, WITH BE)", 7.5, 8.5, True, True),
    ("G. M5 Reversal Candle (Zone $8.00, Max Risk $9.00, WITH BE)", 8.0, 9.0, True, True),
]

for name, zone, max_r, m5_conf, be in scenarios:
    res = test_m5_strategy(discount_zone=zone, max_entry_risk=max_r, use_m5_conf=m5_conf, use_be=be)
    filled = [r for r in res if r["filled"]]
    tp_wins = [r for r in filled if r["exit_type"] == "TP"]
    be_exits = [r for r in filled if r["exit_type"] == "BE"]
    sl_losses = [r for r in filled if r["exit_type"] == "SL"]
    
    total_r = sum(r["realized_r"] for r in filled)
    wr = len(tp_wins) / len(filled) * 100 if filled else 0
    safe_rate = (len(tp_wins) + len(be_exits)) / len(filled) * 100 if filled else 0
    
    print(f"\n>>> {name}")
    print(f"Total Setups: {len(res)} | Filled: {len(filled)} (Fill Rate: {len(filled)/len(res)*100:.1f}%)")
    print(f"Outcomes: TP Wins = {len(tp_wins)}, Breakeven = {len(be_exits)}, Hard SL = {len(sl_losses)}")
    print(f"Win Rate (TP): {wr:.1f}% | Win + BE (Capital Preserved): {safe_rate:.1f}%")
    print(f"Total Realized R: {total_r:+.2f}R")
    if tp_wins:
        avg_win_r = sum(r["realized_r"] for r in tp_wins) / len(tp_wins)
        avg_risk = sum(r["actual_risk"] for r in filled) / len(filled)
        print(f"Avg Realized R on Wins: {avg_win_r:.2f}R | Avg Actual Risk: ${avg_risk:.2f}")

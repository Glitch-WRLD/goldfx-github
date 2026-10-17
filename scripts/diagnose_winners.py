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

# Identify the 5 winning trades under Limit Order at $6.50
from scripts.simulate_m5_retrace import test_m5_strategy

res_limit = test_m5_strategy(discount_zone=6.50, max_entry_risk=6.50, use_m5_conf=False, use_be=False)
win_indices = [i for i, r in enumerate(res_limit) if r["exit_type"] == "TP"]
print(f"Indices of winning trades in Limit Order: {win_indices}")

for idx in win_indices:
    h, o = xau_trades[idx]
    r = res_limit[idx]
    print(f"\n--- WINNING TRADE: Ref {h.get('ref')} ({h['dir']}) ---")
    print(f"Entry={h['entry']}, SL={h['sl']}, TP={h['tp']}")
    print(f"Original Risk: {abs(float(h['entry']) - float(h['sl'])):.2f}")
    print(f"Limit Fill Price: {r['fill_price']:.2f}, Realized R: {r['realized_r']:.2f}R")
    
    # Now check what happened to this trade in the M5 confirmation scenario
    sig_ts = pd.to_datetime(h["ts"], utc=True)
    dir_ = 1 if h["dir"] == "LONG" else -1
    sl = float(h["sl"])
    tp = float(h["tp"])
    entry = float(h["entry"])
    zone_price = sl + 6.50 if dir_ == 1 else sl - 6.50
    post_bars = df_m5[(df_m5.index >= sig_ts) & (df_m5.index <= sig_ts + pd.Timedelta(hours=24))]
    
    print("M5 bars around the touch:")
    for b_idx, (b_ts, row) in enumerate(post_bars.iterrows()):
        b_low, b_high, b_open, b_close = row["low"], row["high"], row["open"], row["close"]
        touched = (b_low <= zone_price) if dir_ == 1 else (b_high >= zone_price)
        if touched:
            print(f"  Bar {b_idx} ({b_ts}): O={b_open:.2f}, H={b_high:.2f}, L={b_low:.2f}, C={b_close:.2f} | Dist to SL at close: {abs(b_close - sl):.2f}")
            # Print next 3 bars
            next_3 = post_bars.iloc[b_idx:b_idx+4]
            for n_ts, n_r in next_3.iterrows():
                print(f"    -> {n_ts}: O={n_r['open']:.2f}, H={n_r['high']:.2f}, L={n_r['low']:.2f}, C={n_r['close']:.2f} | Dist to SL: {abs(n_r['close'] - sl):.2f}")
            break

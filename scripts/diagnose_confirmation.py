import json
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.tv_data import load_cached

df_m15 = load_cached("XAUUSD", "M15")
state = json.loads(Path("gha_state/state.json").read_text(encoding="utf-8"))
history = state.get("history", [])
outcomes = state.get("outcomes", {})

xau_trades = [(h, outcomes[f"{h['symbol']}:{h['ts']}"]) for h in history if h["symbol"] == "XAUUSD" and f"{h['symbol']}:{h['ts']}" in outcomes]

zone_dist = 7.50
max_risk = 8.50

status_counts = {}
details = []

for h, o in xau_trades:
    sig_ts = pd.to_datetime(h["ts"], utc=True)
    dir_ = 1 if h["dir"] == "LONG" else -1
    entry = float(h["entry"])
    sl = float(h["sl"])
    tp = float(h["tp"])
    
    when = pd.to_datetime(o.get("when", o.get("ts", sig_ts)), utc=True)
    post_bars = df_m15[(df_m15.index >= sig_ts) & (df_m15.index <= when + pd.Timedelta(hours=36))]
    if post_bars.empty:
        continue
        
    inval_price = entry + (tp - entry) * 0.75
    zone_price = sl + zone_dist if dir_ == 1 else sl - zone_dist
    
    status = "PENDING"
    in_zone = False
    reasons = []
    
    for idx, (b_ts, row) in enumerate(list(post_bars.iterrows())[:24]):
        b_open, b_high, b_low, b_close = row["open"], row["high"], row["low"], row["close"]
        
        # 75% TP
        if (dir_ == 1 and b_high >= inval_price) or (dir_ == -1 and b_low <= inval_price):
            status = "INVALIDATED_TP75"
            break
            
        # SL breach
        if (dir_ == 1 and b_low <= sl) or (dir_ == -1 and b_high >= sl):
            status = "SL_BREACHED"
            reasons.append(f"Bar {idx} breached SL ({sl:.2f}): high={b_high:.2f}, low={b_low:.2f}")
            break
            
        # Check zone touch
        if not in_zone:
            if (dir_ == 1 and b_low <= zone_price) or (dir_ == -1 and b_high >= zone_price):
                in_zone = True
                reasons.append(f"Bar {idx} touched zone ({zone_price:.2f})")
                
        if in_zone:
            is_bull = (b_close > b_open)
            is_bear = (b_close < b_open)
            conf = is_bull if dir_ == 1 else is_bear
            if conf:
                c_risk = abs(b_close - sl)
                if c_risk <= max_risk:
                    status = "FILLED"
                    reasons.append(f"Bar {idx} CONFIRMED! Close={b_close:.2f}, Risk={c_risk:.2f}")
                    break
                else:
                    status = "TOO_EXPENSIVE"
                    reasons.append(f"Bar {idx} confirmed but close={b_close:.2f} has risk={c_risk:.2f} > max {max_risk}")
                    break
            else:
                reasons.append(f"Bar {idx} in zone but candle not confirmed (dir={dir_}, O={b_open:.2f}, C={b_close:.2f})")
                
    if status == "PENDING":
        status = "EXPIRED"
        
    status_counts[status] = status_counts.get(status, 0) + 1
    details.append((h.get("ref"), h["dir"], status, reasons[:3]))

print("Status counts for M15 confirmation:")
for s, c in status_counts.items():
    print(f"  {s}: {c}")

print("\nSample trade traces:")
for ref, d, s, r in details[:10]:
    print(f"Ref {ref} ({d}): {s} -> {r}")

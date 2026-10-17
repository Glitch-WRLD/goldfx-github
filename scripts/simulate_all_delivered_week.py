import urllib.request
import json
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.tv_data import get_df

state = json.loads(urllib.request.urlopen("https://raw.githubusercontent.com/Glitch-WRLD/goldfx-github/main/gha_state/state.json").read().decode())
history = state.get("history", [])

week_signals = [
    h for h in history 
    if h.get("ts") and h.get("ref") is not None and pd.to_datetime(h["ts"], utc=True) >= pd.to_datetime("2026-09-21 00:00:00+00:00")
]
week_signals.sort(key=lambda x: int(x["ref"]))

results = []
cached_dfs = {}

for s in week_signals:
    ref = s["ref"]
    sym = s["symbol"]
    tf = s.get("tf", "H1")
    direction = 1 if s["dir"] == "LONG" else -1
    entry = float(s["entry"])
    sl = float(s["sl"])
    tp = float(s["tp"])
    rr = float(s.get("rr", 0.0))
    ts = pd.to_datetime(s["ts"], utc=True)

    if (sym, tf) not in cached_dfs:
        try:
            cached_dfs[(sym, tf)] = get_df(sym, tf, refresh=False)
        except Exception as e:
            cached_dfs[(sym, tf)] = None

    df = cached_dfs.get((sym, tf))
    if df is None:
        results.append({**s, "outcome": "UNKNOWN", "win": False, "pnl_r": 0.0})
        continue

    # Find bar index
    if ts in df.index:
        idx = int(df.index.get_loc(ts))
    else:
        diffs = abs(df.index - ts)
        idx = int(diffs.argmin())

    hit = None
    for i in range(idx + 1, len(df)):
        hi = float(df["high"].iloc[i])
        lo = float(df["low"].iloc[i])
        if direction == 1:
            if hi >= tp:
                hit = "TP"
                break
            if lo <= sl:
                hit = "SL"
                break
        else:
            if lo <= tp:
                hit = "TP"
                break
            if hi >= sl:
                hit = "SL"
                break

    if hit is None:
        # Check current price
        cur = float(df["close"].iloc[-1])
        unrealized_r = ((cur - entry) / abs(entry - sl)) * direction if abs(entry - sl) > 0 else 0.0
        results.append({**s, "outcome": "RUNNING", "win": None, "pnl_r": unrealized_r, "hit": "RUNNING"})
    elif hit == "TP":
        results.append({**s, "outcome": "TP", "win": True, "pnl_r": rr, "hit": "TP"})
    else:
        results.append({**s, "outcome": "SL", "win": False, "pnl_r": -1.0, "hit": "SL"})

# Calculate Statistics
resolved = [r for r in results if r["outcome"] in ("TP", "SL")]
wins = [r for r in resolved if r["win"] is True]
losses = [r for r in resolved if r["win"] is False]
running = [r for r in results if r["outcome"] == "RUNNING"]

win_rate = (len(wins) / len(resolved) * 100) if resolved else 0.0

# Simulate Fixed Risk ($600 per trade = 6% of $10k initial)
fixed_pnl = sum(r["pnl_r"] * 600.0 for r in resolved)
gross_win_fixed = sum(r["pnl_r"] * 600.0 for r in wins)
gross_loss_fixed = abs(sum(r["pnl_r"] * 600.0 for r in losses))
pf_fixed = (gross_win_fixed / gross_loss_fixed) if gross_loss_fixed > 0 else 999.0

# Simulate Compounding 6% Risk starting at $10,000
balance_comp = 10000.0
peak_bal = 10000.0
max_dd = 0.0
equity_curve = [balance_comp]

for r in resolved:
    risk_amount = balance_comp * 0.06
    pnl = risk_amount * r["pnl_r"]
    balance_comp += pnl
    if balance_comp > peak_bal:
        peak_bal = balance_comp
    dd = (peak_bal - balance_comp) / peak_bal * 100
    if dd > max_dd:
        max_dd = dd
    equity_curve.append(balance_comp)

# Group by Symbol
by_sym = {}
for r in resolved:
    sym = r["symbol"]
    by_sym.setdefault(sym, {"trades": 0, "wins": 0, "losses": 0, "net_r": 0.0, "fixed_pnl": 0.0})
    by_sym[sym]["trades"] += 1
    if r["win"]:
        by_sym[sym]["wins"] += 1
    else:
        by_sym[sym]["losses"] += 1
    by_sym[sym]["net_r"] += r["pnl_r"]
    by_sym[sym]["fixed_pnl"] += r["pnl_r"] * 600.0

print("=== ALL DELIVERED SIGNALS SIMULATION (Sep 21 - Sep 25, 2026) ===")
print(f"Total Signals Delivered: {len(results)} (Ref #{results[0]['ref']} to #{results[-1]['ref']})")
print(f"Resolved Setups: {len(resolved)} | Still Running: {len(running)}")
print(f"Wins: {len(wins)} | Losses: {len(losses)}")
print(f"Delivered Win Rate: {win_rate:.1f}%")
print(f"Total Net R Generated: {sum(r['pnl_r'] for r in resolved):+.2f}R")
print(f"\n--- Hypothetical Financial Results (Fixed 6% = $600/trade) ---")
print(f"Gross Profit:  ${gross_win_fixed:,.2f}")
print(f"Gross Loss:    ${gross_loss_fixed:,.2f}")
print(f"Net Profit:    ${fixed_pnl:+,.2f} ({fixed_pnl/10000*100:+.1f}% ROI)")
print(f"Profit Factor: {pf_fixed:.2f}")

print(f"\n--- Hypothetical Financial Results (Compounded 6% Risk) ---")
print(f"Starting Balance: $10,000.00")
print(f"Final Balance:    ${balance_comp:,.2f} ({(balance_comp-10000)/10000*100:+.1f}% Compounded ROI)")
print(f"Max Drawdown:     {max_dd:.1f}%")

print("\n=== BREAKDOWN BY SYMBOL (ALL DELIVERED) ===")
for sym, d in sorted(by_sym.items(), key=lambda x: x[1]["fixed_pnl"], reverse=True):
    wr = (d["wins"] / d["trades"] * 100) if d["trades"] else 0.0
    print(f"{sym:7s} | {d['trades']:2d} trades | {d['wins']:2d}W - {d['losses']:2d}L ({wr:5.1f}%) | Net R: {d['net_r']:+6.2f}R | PnL ($600 risk): ${d['fixed_pnl']:+8.2f}")

# Detail table of all trades
print("\n=== DETAIL LOG OF ALL 55 SETUPS ===")
for r in results:
    status = r["outcome"]
    res_str = f"WIN (+{r['pnl_r']:.2f}R)" if r["win"] is True else (f"LOSS (-1.00R)" if r["win"] is False else "RUNNING")
    print(f"#{r['ref']:02d} | {r['symbol']:7s} | {r['dir']:5s} | Entry:{r['entry']} | SL:{r['sl']:.5f} | TP:{r['tp']:.5f} | RR 1:{r.get('rr',0):.2f} | {status:7s} -> {res_str}")

import sys
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.tv_data import load_cached
from strategy import candidates
from strategy.profiles import PROFILES

strat = candidates.STRATEGIES["fvg_retest"]
p = PROFILES["balanced"]["params"]
params = dict(p)
params["_symbol"] = "XAUUSD"
params["bias_htf"] = "H1"

df_m30 = load_cached("XAUUSD", "M30")
sigs = strat.signaller(df_m30, params)

risks = []
for sig in sigs:
    if sig.ts not in df_m30.index:
        continue
    pos = df_m30.index.get_loc(sig.ts)
    if pos + 2 >= len(df_m30):
        continue
    entry_base = float(df_m30["open"].iloc[pos + 1])
    sl = entry_base - sig.dir * sig.sl_offset
    risk = abs(entry_base - sl)
    risks.append(risk)

print(f"Total signals: {len(risks)}")
print(f"Avg initial stop distance: ${np.mean(risks):.2f}")
print(f"Median initial stop distance: ${np.median(risks):.2f}")
print(f"Min: ${np.min(risks):.2f}, Max: ${np.max(risks):.2f}")
print(f"Signals with stop <= $6.50: {sum(1 for r in risks if r <= 6.50)} ({sum(1 for r in risks if r <= 6.50)/len(risks)*100:.1f}%)")
print(f"Signals with stop > $15.00: {sum(1 for r in risks if r > 15.00)} ({sum(1 for r in risks if r > 15.00)/len(risks)*100:.1f}%)")

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.tv_data import load_cached

state = json.loads(Path("gha_state/state.json").read_text(encoding="utf-8"))
history = state.get("history", [])
outcomes = state.get("outcomes", {})

xau = [h for h in history if h["symbol"] == "XAUUSD"]
outs = []
for h in xau:
    k = f"{h['symbol']}:{h['ts']}"
    if k in outcomes:
        outs.append((h, outcomes[k]))

print(f"Total XAUUSD signals: {len(xau)}")
print(f"Total matched XAUUSD outcomes: {len(outs)}")

tp_count = sum(1 for h, o in outs if o.get("hit") == "tp")
sl_count = sum(1 for h, o in outs if o.get("hit") == "sl")
print(f"Baseline TP: {tp_count} ({tp_count/len(outs)*100:.1f}%), SL: {sl_count} ({sl_count/len(outs)*100:.1f}%)")

# Inspect initial risk distances
risks = [abs(float(h["entry"]) - float(h["sl"])) for h in xau]
print(f"Avg Stop Distance: ${sum(risks)/len(risks):.2f}")
print(f"Min Stop Distance: ${min(risks):.2f}, Max Stop Distance: ${max(risks):.2f}")

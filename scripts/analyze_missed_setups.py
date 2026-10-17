import json
import urllib.request
from collections import Counter

# Fetch remote state
req = urllib.request.urlopen("https://raw.githubusercontent.com/Glitch-WRLD/goldfx-github/main/gha_state/state.json")
remote_state = json.loads(req.read().decode())
with open("agent/ledger.json", "r", encoding="utf-8") as f:
    ledger_data = json.load(f)

week_history = [h for h in remote_state.get("history", []) if h.get("ts", "") >= "2026-10-04"]
outcomes = remote_state.get("outcomes", {})

print(f"=== TOTAL DELIVERED SETUPS THIS WEEK: {len(week_history)} ===")

# Delivery bot stats
bot_tp = 0
bot_sl = 0
bot_open = 0
for h in week_history:
    k = f"{h['symbol']}:{h['ts']}"
    hit = outcomes.get(k, {}).get("hit")
    if hit == "tp":
        bot_tp += 1
    elif hit == "sl":
        bot_sl += 1
    else:
        bot_open += 1

resolved = bot_tp + bot_sl
print(f"Delivery Bot Outcomes: {bot_tp} TP ({bot_tp/resolved*100:.1f}%), {bot_sl} SL ({bot_sl/resolved*100:.1f}%), {bot_open} Open")

# Categorize into Taken vs Missed
taken = []
missed = []

for h in week_history:
    ref = str(h.get("ref"))
    sym = h.get("symbol")
    ts = h.get("ts")
    dir_ = h.get("dir")
    ledg = ledger_data.get(ref, {})
    status = ledg.get("status", "NOT_IN_LEDGER")
    hit = ledg.get("hit", "")
    pnl = ledg.get("pnl_usd", None)
    out_key = f"{sym}:{ts}"
    bot_outcome = outcomes.get(out_key, {}).get("hit", "OPEN")

    item = {
        "ref": ref,
        "symbol": sym,
        "dir": dir_,
        "ts": ts,
        "status": status,
        "ledger_hit": hit,
        "pnl": pnl,
        "bot_outcome": bot_outcome,
        "classification": ledg.get("classification", ""),
        "skip_reason": hit if status in ("skipped", "quarantined") else ("not_in_ledger" if status == "NOT_IN_LEDGER" else "")
    }
    if status in ("closed", "open") and status != "NOT_IN_LEDGER":
        taken.append(item)
    else:
        missed.append(item)

print(f"\n=== TRADES TAKEN BY AGENT: {len(taken)} ===")
taken_tp = sum(1 for t in taken if t["bot_outcome"] == "tp")
taken_sl = sum(1 for t in taken if t["bot_outcome"] == "sl")
print(f"Agent Taken Delivery Outcomes: {taken_tp} TP, {taken_sl} SL, {len(taken)-taken_tp-taken_sl} Open")
agent_wins = sum(1 for t in taken if (t["pnl"] is not None and t["pnl"] > 0))
agent_losses = sum(1 for t in taken if (t["pnl"] is not None and t["pnl"] < 0))
agent_pnl = sum(t["pnl"] for t in taken if t["pnl"] is not None)
print(f"Agent Closed Trades: {agent_wins} Wins, {agent_losses} Losses | Total Net PnL: ${agent_pnl:.2f}")

print(f"\n=== TRADES MISSED / SKIPPED BY AGENT: {len(missed)} ===")
missed_tp = sum(1 for m in missed if m["bot_outcome"] == "tp")
missed_sl = sum(1 for m in missed if m["bot_outcome"] == "sl")
missed_open = sum(1 for m in missed if m["bot_outcome"] not in ("tp", "sl"))
print(f"Missed Setups Outcomes: {missed_tp} TP ({missed_tp/(missed_tp+missed_sl)*100 if missed_tp+missed_sl else 0:.1f}%), {missed_sl} SL, {missed_open} Open")

print("\nDetailed Breakdown of ALL MISSED Setups:")
for m in missed:
    print(f"Ref {m['ref']:4s} | {m['symbol']:10s} {m['dir']:5s} | TS: {m['ts']} | BotOutcome: {m['bot_outcome']:5s} | SkipReason: {m['skip_reason']:25s} | Class: {m['classification']}")

# Group missed by skip reason
print("\n=== MISSED SETUPS GROUPED BY REASON ===")
reasons = Counter(m["skip_reason"] for m in missed)
for r, count in reasons.most_common():
    tps = sum(1 for m in missed if m["skip_reason"] == r and m["bot_outcome"] == "tp")
    sls = sum(1 for m in missed if m["skip_reason"] == r and m["bot_outcome"] == "sl")
    print(f"{r:25s}: {count:2d} missed -> {tps} TP, {sls} SL")

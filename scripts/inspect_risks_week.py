import sys
sys.path.insert(0, ".")
import json
import MetaTrader5 as mt5
from agent.mt5_broker import build_mt5_broker

ex = build_mt5_broker()
ledger_path = "agent/ledger.json"
d = json.load(open(ledger_path))

print(f"{'REF':<5} {'SYM':<11} {'DIR':<5} {'LOTS':<6} {'SL_DIST':<10} {'BUDGET_$':<10} {'ACTUAL_SL_$':<12} {'ACTUAL_%':<9} {'PNL_$':<10} {'OUTCOME'}")
print("-" * 95)

for k, v in sorted(d.items(), key=lambda x: str(x[1].get('ts', ''))):
    if v.get('ts', '') < '2026-10-05':
        continue
    sym = v.get('symbol')
    lots = float(v.get('lots', 0.0))
    fill = float(v.get('fill_price', 0.0))
    sl = float(v.get('sl', 0.0))
    risk_usd = float(v.get('risk_usd', 0.0))
    sl_dist = abs(fill - sl)
    
    info = mt5.symbol_info(sym)
    if info:
        tick_val = info.trade_tick_value
        tick_size = info.trade_tick_size
        point = info.point
        actual_loss = lots * (sl_dist / tick_size) * tick_val if tick_size > 0 else 0.0
    else:
        actual_loss = 0.0
        
    pnl = v.get('pnl_usd', None)
    hit = v.get('hit', v.get('status', ''))
    classif = v.get('classification', '')
    outcome_str = f"{hit} ({classif})" if classif else str(hit)
    
    # Implied balance at entry
    implied_bal = (risk_usd / 0.06) if risk_usd > 0 else 1000.0
    actual_pct = (actual_loss / implied_bal * 100) if implied_bal > 0 else 0.0
    pnl_str = f"${pnl:+.2f}" if pnl is not None else "open"
    
    print(f"{k:<5} {sym:<11} {v.get('direction', 1):<5} {lots:<6.2f} {sl_dist:<10.5f} ${risk_usd:<9.2f} ${actual_loss:<11.2f} {actual_pct:<8.2f}% {pnl_str:<10} {outcome_str}")

import sys
sys.path.insert(0, ".")
import config
import MetaTrader5 as mt5

mt5.initialize()
print(f"{'SYMBOL':<12} {'CFG_POINT':<10} {'MT5_POINT':<10} {'CFG_PIP_VAL':<12} {'MT5_TICK_VAL':<12} {'MT5_TICK_SZ':<12} {'RATIO'}")
print("-" * 85)

for sym, c in config.CONTRACTS.items():
    info = mt5.symbol_info(sym)
    if not info:
        print(f"{sym:<12} (not found in MT5)")
        continue
    
    # Calculate how many lots 1 point sl distance with $60 risk would give:
    # Config way:
    cfg_pts = 100.0 / c["point"]
    cfg_lots = 60.0 / (cfg_pts * c["pip_value_per_lot_usd"])
    
    # MT5 real way:
    # loss for 1 lot with sl_dist = 100 * c["point"]:
    sl_dist = 100.0 * info.point
    mt5_loss_per_lot = (sl_dist / info.trade_tick_size) * info.trade_tick_value
    mt5_lots = 60.0 / mt5_loss_per_lot if mt5_loss_per_lot > 0 else 0.0
    
    ratio = cfg_lots / mt5_lots if mt5_lots > 0 else 1.0
    print(f"{sym:<12} {c['point']:<10} {info.point:<10} {c['pip_value_per_lot_usd']:<12} {info.trade_tick_value:<12.4f} {info.trade_tick_size:<12} {ratio:<.2f}x")

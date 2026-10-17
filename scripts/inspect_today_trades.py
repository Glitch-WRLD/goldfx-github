import MetaTrader5 as mt5
import datetime as dt
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

mt5.initialize()
today_utc = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
deals = mt5.history_deals_get(today_utc, dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
print("=== ALL DEALS TODAY (from 00:00 UTC) ===")
if deals:
    for d in deals:
        t = dt.datetime.fromtimestamp(d.time, dt.timezone.utc).strftime("%H:%M:%S")
        entry_str = "ENTRY_IN" if d.entry == 0 else "ENTRY_OUT"
        print(f"Time: {t} | Deal: {d.ticket} | Order: {d.order} | {d.symbol} | {entry_str} | Lots: {d.volume} | Price: {d.price} | Profit: ${d.profit:.2f} | Swap: ${d.swap:.2f} | Comment: '{d.comment}'")
else:
    print("No deals found.")

print("\n=== ALL CLOSED/FILLED ORDERS TODAY ===")
orders = mt5.history_orders_get(today_utc, dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
if orders:
    for o in orders:
        t = dt.datetime.fromtimestamp(o.time_done if o.time_done else o.time_setup, dt.timezone.utc).strftime("%H:%M:%S")
        type_str = "BUY" if o.type == 0 else ("SELL" if o.type == 1 else str(o.type))
        print(f"Time: {t} | Order: {o.ticket} | {o.symbol} | {type_str} | Lots: {o.volume_initial} | SL: {o.sl} | TP: {o.tp} | State: {o.state} | Comment: '{o.comment}'")

print("\n=== CURRENT OPEN POSITIONS ===")
pos = mt5.positions_get()
if pos:
    for p in pos:
        type_str = "BUY" if p.type == 0 else "SELL"
        print(f"Ticket: {p.ticket} | {p.symbol} | {type_str} | Lots: {p.volume} | Open: {p.price_open} | Cur: {p.price_current} | SL: {p.sl} | TP: {p.tp} | Profit: ${p.profit:.2f} | Swap: ${p.swap:.2f} | Comment: '{p.comment}'")
else:
    print("No active positions.")

acc = mt5.account_info()
print(f"\nAccount Balance: ${acc.balance:.2f} | Equity: ${acc.equity:.2f}")
mt5.shutdown()

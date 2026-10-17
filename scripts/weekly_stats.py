import MetaTrader5 as mt5
import datetime as dt
import json

mt5.initialize()
week_start = dt.datetime(2026, 9, 21, 0, 0, 0, tzinfo=dt.timezone.utc)
week_end = dt.datetime(2026, 9, 25, 23, 59, 59, tzinfo=dt.timezone.utc)

deals = mt5.history_deals_get(week_start, week_end)
acc = mt5.account_info()

exit_deals = [d for d in (deals or []) if d.entry == 1]

wins = [d for d in exit_deals if d.profit > 0]
losses = [d for d in exit_deals if d.profit < 0]
be_trades = [d for d in exit_deals if d.profit == 0]

total_pnl = sum(d.profit + d.swap + d.commission for d in exit_deals)
gross_profit = sum(d.profit for d in wins)
gross_loss = abs(sum(d.profit for d in losses)) if losses else 0
pf = (gross_profit / gross_loss) if gross_loss > 0 else 999.0

by_sym = {}
for d in exit_deals:
    sym = d.symbol
    by_sym.setdefault(sym, {"trades": 0, "wins": 0, "losses": 0, "be": 0, "pnl": 0.0})
    by_sym[sym]["trades"] += 1
    if d.profit > 0:
        by_sym[sym]["wins"] += 1
    elif d.profit < 0:
        by_sym[sym]["losses"] += 1
    else:
        by_sym[sym]["be"] += 1
    by_sym[sym]["pnl"] += (d.profit + d.swap + d.commission)

# Daily Breakdown
by_day = {}
for d in exit_deals:
    date_str = dt.datetime.fromtimestamp(int(d.time), dt.timezone.utc).strftime("%Y-%m-%d (%a)")
    by_day.setdefault(date_str, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    by_day[date_str]["trades"] += 1
    if d.profit > 0:
        by_day[date_str]["wins"] += 1
    elif d.profit < 0:
        by_day[date_str]["losses"] += 1
    by_day[date_str]["pnl"] += (d.profit + d.swap + d.commission)

print("=== WEEKLY PERFORMANCE SUMMARY (Sep 21 - Sep 25, 2026) ===")
print(f"Starting Balance: $10,000.00 (Demo Initial)")
print(f"Current Balance:  ${acc.balance:,.2f}")
print(f"Current Equity:   ${acc.equity:,.2f}")
print(f"Net Realized PnL: ${total_pnl:+,.2f} ({(acc.balance - 10000) / 10000 * 100:+.1f}% Total ROI)")
print(f"Total Closed Trades: {len(exit_deals)}")
print(f"Wins: {len(wins)} | Losses: {len(losses)} | Breakeven: {len(be_trades)}")
win_rate = (len(wins) / len(exit_deals) * 100) if exit_deals else 0
print(f"Win Rate: {win_rate:.1f}%")
print(f"Gross Profit: ${gross_profit:,.2f}")
print(f"Gross Loss:   ${gross_loss:,.2f}")
print(f"Profit Factor: {pf:.2f}")
avg_win = (gross_profit / len(wins)) if wins else 0
avg_loss = (gross_loss / len(losses)) if losses else 0
print(f"Avg Win: ${avg_win:,.2f} | Avg Loss: ${avg_loss:,.2f} | Win/Loss Ratio: {avg_win/avg_loss:.2f}" if avg_loss else "N/A")

print("\n=== PERFORMANCE BY PAIR ===")
for sym, d in sorted(by_sym.items(), key=lambda x: x[1]["pnl"], reverse=True):
    wr = (d["wins"] / d["trades"] * 100) if d["trades"] else 0
    print(f"{sym:7s} | {d['trades']:2d} trades | {d['wins']:2d}W - {d['losses']:2d}L ({wr:4.1f}%) | Net PnL: ${d['pnl']:+9.2f}")

print("\n=== PERFORMANCE BY DAY ===")
for day, d in sorted(by_day.items()):
    wr = (d["wins"] / d["trades"] * 100) if d["trades"] else 0
    print(f"{day:16s} | {d['trades']:2d} trades | {d['wins']:2d}W - {d['losses']:2d}L ({wr:4.1f}%) | Net PnL: ${d['pnl']:+9.2f}")

mt5.shutdown()

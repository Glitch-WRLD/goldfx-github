import MetaTrader5 as mt5
import datetime as dt
import pandas as pd

mt5.initialize()

week_start = dt.datetime(2026, 9, 27, 20, 0, 0, tzinfo=dt.timezone.utc)
now = dt.datetime(2026, 9, 30, 23, 59, 59, tzinfo=dt.timezone.utc)
deals = mt5.history_deals_get(week_start, now)

positions = {}
for d in deals:
    if not d.symbol or d.position_id == 0:
        continue
    pid = d.position_id
    if pid not in positions:
        positions[pid] = {"in": None, "out": None}
    if d.entry == 0:
        positions[pid]["in"] = d
    elif d.entry in (1, 3) or d.profit != 0:
        positions[pid]["out"] = d

trade_list = []

for pid, p in positions.items():
    d_in = p["in"]
    d_out = p["out"]
    if not d_in or not d_out:
        continue
    
    sym = d_in.symbol
    t_in = dt.datetime.fromtimestamp(d_in.time, dt.timezone.utc)
    t_out = dt.datetime.fromtimestamp(d_out.time, dt.timezone.utc)
    entry_p = d_in.price
    exit_p = d_out.price
    dir_str = "BUY" if d_in.type == 0 else "SELL"
    direction = 1 if d_in.type == 0 else -1
    profit = d_out.profit
    is_win = profit > 0
    
    orders = mt5.history_orders_get(position=pid)
    sl_p = None
    tp_p = None
    if orders:
        for o in orders:
            if o.sl > 0:
                sl_p = o.sl
            if o.tp > 0:
                tp_p = o.tp
    if not sl_p:
        sl_p = exit_p if not is_win else (entry_p - 0.0020 if direction == 1 else entry_p + 0.0020)
        
    risk = abs(entry_p - sl_p)
    if risk == 0:
        risk = 0.0010
        
    rates = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M1, t_in, t_out)
    if rates is None or len(rates) == 0:
        max_mfe_r = 0.0
        retraced_after_05 = False
        retraced_after_08 = False
    else:
        df_rates = pd.DataFrame(rates)
        if direction == 1:
            mfe_series = (df_rates['high'] - entry_p) / risk
            max_mfe_r = mfe_series.max()
            
            # Check 0.5R
            hit_05_idx = df_rates[mfe_series >= 0.5].index
            if len(hit_05_idx) > 0:
                retrace_bars = df_rates.iloc[hit_05_idx[0]:]
                retraced_after_05 = (retrace_bars['low'] <= entry_p).any()
            else:
                retraced_after_05 = False
                
            # Check 0.8R
            hit_08_idx = df_rates[mfe_series >= 0.8].index
            if len(hit_08_idx) > 0:
                retrace_bars = df_rates.iloc[hit_08_idx[0]:]
                retraced_after_08 = (retrace_bars['low'] <= entry_p).any()
            else:
                retraced_after_08 = False
        else:
            mfe_series = (entry_p - df_rates['low']) / risk
            max_mfe_r = mfe_series.max()
            
            # Check 0.5R
            hit_05_idx = df_rates[mfe_series >= 0.5].index
            if len(hit_05_idx) > 0:
                retrace_bars = df_rates.iloc[hit_05_idx[0]:]
                retraced_after_05 = (retrace_bars['high'] >= entry_p).any()
            else:
                retraced_after_05 = False
                
            # Check 0.8R
            hit_08_idx = df_rates[mfe_series >= 0.8].index
            if len(hit_08_idx) > 0:
                retrace_bars = df_rates.iloc[hit_08_idx[0]:]
                retraced_after_08 = (retrace_bars['high'] >= entry_p).any()
            else:
                retraced_after_08 = False

    trade_list.append({
        "pid": pid,
        "sym": sym,
        "dir": dir_str,
        "t_in": t_in,
        "t_out": t_out,
        "entry": entry_p,
        "exit": exit_p,
        "profit": profit,
        "is_win": is_win,
        "max_mfe_r": max_mfe_r,
        "hit_05": max_mfe_r >= 0.5,
        "hit_08": max_mfe_r >= 0.8,
        "retraced_after_05": retraced_after_05,
        "retraced_after_08": retraced_after_08,
        "comment": d_out.comment
    })

print("=== ANALYSIS OF LOSING TRADES ===")
losses = [t for t in trade_list if not t['is_win']]
saved_08 = [t for t in losses if t['hit_08']]
saved_05 = [t for t in losses if t['hit_05']]

print(f"Total Losses This Week: {len(losses)}")
print(f"Losses that reached >= 0.8R (Saved at 0.8R BE): {len(saved_08)} trades | Money saved: {abs(sum(t['profit'] for t in saved_08)):.2f} USC (${abs(sum(t['profit'] for t in saved_08))/100:.2f} USD)")
print(f"Losses that reached >= 0.5R (Saved at 0.5R BE): {len(saved_05)} trades | Money saved: {abs(sum(t['profit'] for t in saved_05)):.2f} USC (${abs(sum(t['profit'] for t in saved_05))/100:.2f} USD)")

print("\nDetail of every single loss and its peak profit:")
for t in losses:
    t_str = t['t_in'].strftime('%m-%d %H:%M')
    saved_tag = "SAVED at 0.5R & 0.8R" if t['hit_08'] else ("SAVED at 0.5R only" if t['hit_05'] else "Not saved (never reached 0.5R)")
    print(f"{t_str} | {t['sym']:7s} {t['dir']:4s} | Loss: {t['profit']:8.2f} USC | Peak MFE: +{t['max_mfe_r']:.2f}R | {saved_tag}")

print("\n=== CHECKING WINNING TRADES: WOULD BE HAVE PREMATURELY CUT ANY WINNERS? ===")
wins = [t for t in trade_list if t['is_win']]
for t in wins:
    t_str = t['t_in'].strftime('%m-%d %H:%M')
    status_05 = "STOPPED AT BE ($0)" if t['retraced_after_05'] else "SAFE (Reached TP)"
    status_08 = "STOPPED AT BE ($0)" if t['retraced_after_08'] else "SAFE (Reached TP)"
    print(f"{t_str} | {t['sym']:7s} {t['dir']:4s} | Win: {t['profit']:+8.2f} USC | Peak MFE: +{t['max_mfe_r']:.2f}R | At 0.5R: {status_05:18s} | At 0.8R: {status_08}")

mt5.shutdown()

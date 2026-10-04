import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import datetime as dt

def get_point(symbol):
    return 0.01 if 'JPY' in symbol or symbol == 'XAUUSD' or any(x in symbol for x in ['100', '500', '30']) else 0.0001

def run_isde_portfolio(count_m5=20000):
    if not mt5.initialize():
        print("Failed to initialize MT5")
        return
    
    # Portfolio definition: (symbol, start_hour, end_hour, tf_htf, min_gap_pts)
    portfolio = [
        ('XAUUSD', 7, 9, 'H1', 50.0),
        ('USDJPY', 7, 9, 'H1', 1.5),
        ('DJ30', 14, 15, 'H1', 2.0),
        ('EURUSD', 14, 15, 'H1', 1.5),
        ('GBPUSD', 14, 15, 'H1', 1.5),
    ]
    
    all_trades = []
    
    for sym, sh, eh, htf, min_gap_pts in portfolio:
        mt5.symbol_select(sym, True)
        r_m5 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M5, 0, count_m5)
        r_h1 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H1, 0, 8000)
        if r_m5 is None or r_h1 is None:
            print(f"Missing data for {sym}")
            continue
        df_m5 = pd.DataFrame(r_m5)
        df_m5['time'] = pd.to_datetime(df_m5['time'], unit='s', utc=True)
        df_m5.set_index('time', inplace=True)
        
        df_h1 = pd.DataFrame(r_h1)
        df_h1['time'] = pd.to_datetime(df_h1['time'], unit='s', utc=True)
        df_h1.set_index('time', inplace=True)
        h1_ema = df_h1['close'].ewm(span=50).mean()
        
        point = get_point(sym)
        days = np.unique(df_m5.index.date)
        
        for d in days:
            day_m5 = df_m5[df_m5.index.date == d]
            if len(day_m5) < 30:
                continue
            
            ts_ref = pd.Timestamp(dt.datetime.combine(d, dt.time(sh, 0)), tz='UTC')
            h1_sub = h1_ema[h1_ema.index <= ts_ref]
            if len(h1_sub) == 0:
                continue
            h1_bull = df_h1.loc[df_h1.index <= ts_ref, 'close'].iloc[-1] > h1_sub.iloc[-1]
            
            win = day_m5[(day_m5.index.hour >= sh) & (day_m5.index.hour < eh)]
            if len(win) < 4:
                continue
            
            traded = False
            for i in range(2, len(win)):
                if traded:
                    break
                b0 = win.iloc[i-2]
                b1 = win.iloc[i-1]
                b2 = win.iloc[i]
                cur_t = win.index[i]
                
                # Bullish setup
                if h1_bull and b2['low'] > b0['high']:
                    gap = (b2['low'] - b0['high']) / (0.01 if sym in ['XAUUSD', 'DJ30'] else point)
                    if gap >= min_gap_pts:
                        entry = float(b2['low'])
                        sl = float(b1['low']) - (1.5 * point)
                        sl_dist = abs(entry - sl)
                        if sl_dist > 0:
                            rr = 2.0
                            tp = entry + (rr * sl_dist)
                            forward = day_m5[day_m5.index > cur_t]
                            hit = 'open'
                            for _, fb in forward.iterrows():
                                if fb['low'] <= sl:
                                    hit = 'sl'; break
                                if fb['high'] >= tp:
                                    hit = 'tp'; break
                            if hit in ('tp', 'sl'):
                                all_trades.append({
                                    'date': cur_t, 'symbol': sym, 'dir': 'BUY',
                                    'window': f'{sh:02d}-{eh:02d} UTC',
                                    'hit': hit, 'r': rr if hit == 'tp' else -1.0
                                })
                                traded = True
                elif (not h1_bull) and b0['low'] > b2['high']:
                    gap = (b0['low'] - b2['high']) / (0.01 if sym in ['XAUUSD', 'DJ30'] else point)
                    if gap >= min_gap_pts:
                        entry = float(b2['high'])
                        sl = float(b1['high']) + (1.5 * point)
                        sl_dist = abs(entry - sl)
                        if sl_dist > 0:
                            rr = 2.0
                            tp = entry - (rr * sl_dist)
                            forward = day_m5[day_m5.index > cur_t]
                            hit = 'open'
                            for _, fb in forward.iterrows():
                                if fb['high'] >= sl:
                                    hit = 'sl'; break
                                if fb['low'] <= tp:
                                    hit = 'tp'; break
                            if hit in ('tp', 'sl'):
                                all_trades.append({
                                    'date': cur_t, 'symbol': sym, 'dir': 'SELL',
                                    'window': f'{sh:02d}-{eh:02d} UTC',
                                    'hit': hit, 'r': rr if hit == 'tp' else -1.0
                                })
                                traded = True
                                
    df_all = pd.DataFrame(all_trades)
    df_all.sort_values(by='date', inplace=True)
    
    n = len(df_all)
    n_tp = (df_all['hit'] == 'tp').sum()
    n_sl = (df_all['hit'] == 'sl').sum()
    wr = (n_tp / n) * 100
    gw = df_all[df_all['r'] > 0]['r'].sum()
    gl = abs(df_all[df_all['r'] < 0]['r'].sum())
    pf = gw / gl if gl > 0 else 99.0
    net = df_all['r'].sum()
    
    d_start = df_all['date'].min()
    d_end = df_all['date'].max()
    weeks = (d_end - d_start).days / 7.0
    r_per_wk = net / weeks
    
    # Cumulative equity curve & Max Drawdown
    df_all['equity'] = df_all['r'].cumsum()
    df_all['peak'] = df_all['equity'].cummax()
    df_all['dd'] = df_all['peak'] - df_all['equity']
    max_dd = df_all['dd'].max()
    
    print('=' * 85)
    print('INSTITUTIONAL SESSION DELIVERY ENGINE (ISDE): MASTER PORTFOLIO BACKTEST')
    print('=' * 85)
    print(f'Backtest Period:      {d_start:%Y-%m-%d} -> {d_end:%Y-%m-%d} ({weeks:.1f} weeks)')
    print(f'Total Trades:         {n}')
    print(f'Winning Trades:       {n_tp} (Full 2.0R TP)')
    print(f'Losing Trades:        {n_sl} (Stop Losses)')
    print(f'Win Rate:             {wr:.1f}%')
    print(f'Payoff Ratio:         2.00 (Fixed 1:2.0 RR)')
    print(f'Profit Factor:        {pf:.2f}')
    print(f'Total Net Profit:     {net:+.1f} R')
    print(f'Expected R / Week:    {r_per_wk:+.2f} R/wk')
    print(f'Maximum Drawdown:     {max_dd:.1f} R')
    print(f'Return / Max DD:      {net / max_dd if max_dd > 0 else 99.0:.2f}x')
    print('=' * 85)
    
    # By symbol breakdown
    print('\nBREAKDOWN BY ASSET & WINDOW:')
    for sym in ['XAUUSD', 'USDJPY', 'DJ30', 'EURUSD', 'GBPUSD']:
        sub = df_all[df_all['symbol'] == sym]
        if len(sub) == 0:
            continue
        s_wr = (sub['hit'] == 'tp').mean() * 100
        s_net = sub['r'].sum()
        s_gw = sub[sub['r'] > 0]['r'].sum()
        s_gl = abs(sub[sub['r'] < 0]['r'].sum())
        s_pf = s_gw / s_gl if s_gl > 0 else 99.0
        win_str = sub['window'].iloc[0]
        print(f'{sym:8} ({win_str}): Trades={len(sub):2} | WR={s_wr:4.1f}% | PF={s_pf:4.2f} | Net={s_net:+5.1f}R')

    mt5.shutdown()

if __name__ == "__main__":
    run_isde_portfolio()

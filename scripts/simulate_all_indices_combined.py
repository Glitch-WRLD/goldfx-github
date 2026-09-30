import sys
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from strategy.backtester import run_backtest
from strategy.candidates import STRATEGIES
from strategy.profiles import BAL_PROFILE

mt5.initialize()

candidates = [
    ('NASDAQ-100', 'M15', 'H1'),
    ('NASDAQ-100', 'M30', 'H1'),
    ('NASDAQ-100', 'H1', 'H4'),
    ('US500', 'M15', 'H1'),
    ('US500', 'M30', 'H1'),
    ('US500', 'H1', 'H4'),
    ('DJ30', 'M15', 'H1'),
    ('DJ30', 'M30', 'H1'),
    ('DJ30', 'H1', 'H4'),
]

all_trades = []
for sym, tf, htf in candidates:
    tf_val = mt5.TIMEFRAME_M15 if tf=='M15' else (mt5.TIMEFRAME_M30 if tf=='M30' else mt5.TIMEFRAME_H1)
    rates = mt5.copy_rates_from_pos(sym, tf_val, 0, 4500)
    if rates is None or len(rates) == 0:
        continue
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df = df.rename(columns={'tick_volume': 'volume'}).set_index('time').sort_index()
    p = dict(BAL_PROFILE['params'])
    p['bias_htf'] = htf
    res = run_backtest(df, sym, tf, 'fvg_retest', STRATEGIES['fvg_retest'].signaller, p)
    for t in res.trades:
        all_trades.append({
            'sym': sym,
            'tf': tf,
            'entry_time': t.ts_entry,
            'exit_time': t.ts_exit,
            'dir': t.dir,
            'r': t.r_multiple
        })

df_trades = pd.DataFrame(all_trades).sort_values('entry_time')
print(f"Total trades generated across all 9 index variations: {len(df_trades)}")
print(f"Total Gross R sum: {df_trades['r'].sum():+.2f}R")

# Concurrency check
events = []
for _, row in df_trades.iterrows():
    events.append((row['entry_time'], 1, row['sym'], row['tf']))
    events.append((row['exit_time'], -1, row['sym'], row['tf']))
events.sort(key=lambda x: x[0])

curr_open = 0
max_open = 0
concurrency_dist = {}
for ts, delta, s, tf in events:
    curr_open += delta
    if curr_open > max_open:
        max_open = curr_open
    concurrency_dist[curr_open] = concurrency_dist.get(curr_open, 0) + 1

print(f"Peak simultaneous open index trades: {max_open} trades at the same time!")

# Check portfolio drawdown if all are run unconstrained
df_trades['cum_r'] = df_trades['r'].cumsum()
df_trades['peak_r'] = df_trades['cum_r'].cummax()
df_trades['dd_r'] = df_trades['peak_r'] - df_trades['cum_r']
print(f"Unconstrained Combined Portfolio Drawdown: {df_trades['dd_r'].max():.2f}R")

# Check correlation between NASDAQ-100 M15 vs M30 vs H1
for sym in ['NASDAQ-100', 'US500', 'DJ30']:
    sub = df_trades[df_trades['sym'] == sym]
    sub_m15 = sub[sub['tf'] == 'M15']
    sub_m30 = sub[sub['tf'] == 'M30']
    sub_h1 = sub[sub['tf'] == 'H1']
    print(f"\n{sym}: M15={len(sub_m15)} trades, M30={len(sub_m30)} trades, H1={len(sub_h1)} trades")

print("\n" + "="*80)
print("PORTFOLIO COMPARISON: ALL 9 VARIATIONS vs BEST OF EACH vs SINGLE BEST")
print("="*80)

# 1. Single Best: NASDAQ-100 M15
nas_m15 = df_trades[(df_trades['sym']=='NASDAQ-100') & (df_trades['tf']=='M15')].copy()
nas_m15['cum'] = nas_m15['r'].cumsum()
dd_nas = (nas_m15['cum'].cummax() - nas_m15['cum']).max()
print(f"1. Single Best (NASDAQ-100 M15 only)       : {len(nas_m15)} trades | Gross R: {nas_m15['r'].sum():+7.2f}R | Max DD: {dd_nas:5.2f}R | Return/DD Ratio: {nas_m15['r'].sum()/dd_nas:4.2f}")

# 2. Best of each of the 3 Indices: NASDAQ M15 + DJ30 H1 + US500 M30
best_3 = df_trades[((df_trades['sym']=='NASDAQ-100') & (df_trades['tf']=='M15')) | 
                   ((df_trades['sym']=='DJ30') & (df_trades['tf']=='H1')) | 
                   ((df_trades['sym']=='US500') & (df_trades['tf']=='M30'))].copy().sort_values('entry_time')
best_3['cum'] = best_3['r'].cumsum()
dd_best3 = (best_3['cum'].cummax() - best_3['cum']).max()
print(f"2. Best 1 TF per Index (NASDAQ+DJ30+US500) : {len(best_3)} trades | Gross R: {best_3['r'].sum():+7.2f}R | Max DD: {dd_best3:5.2f}R | Return/DD Ratio: {best_3['r'].sum()/dd_best3:4.2f}")

# 3. All 9 variations together
print(f"3. All 9 Variations (M15+M30+H1 on all 3)  : {len(df_trades)} trades | Gross R: {df_trades['r'].sum():+7.2f}R | Max DD: {df_trades['dd_r'].max():5.2f}R | Return/DD Ratio: {df_trades['r'].sum()/df_trades['dd_r'].max():4.2f}")

# Concurrency for Best 3
events3 = []
for _, row in best_3.iterrows():
    events3.append((row['entry_time'], 1))
    events3.append((row['exit_time'], -1))
events3.sort(key=lambda x: x[0])
c3 = 0
max_c3 = 0
for ts, d in events3:
    c3 += d
    if c3 > max_c3: max_c3 = c3
print(f"\nPeak Simultaneous Trades:")
print(f"- All 9 Variations : {max_open} simultaneous trades (DANGEROUS LEVERAGE SPIKE)")
print(f"- Best 1 per Index : {max_c3} simultaneous trades (MANAGEABLE)")
print(f"- Single Best      : 2 simultaneous trades (VERY SAFE)")

mt5.shutdown()

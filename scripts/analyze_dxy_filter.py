import json
import urllib.request
import pandas as pd
import numpy as np
import datetime as dt

# 1. Fetch DXY historical data from Yahoo Finance
print("Fetching DXY 1-hour and 15-minute historical data from Yahoo Finance...")
headers = {'User-Agent': 'Mozilla/5.0'}

# 1-hour DXY
url_1h = 'https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?range=1mo&interval=1h'
req_1h = urllib.request.Request(url_1h, headers=headers)
with urllib.request.urlopen(req_1h) as resp:
    data_1h = json.loads(resp.read().decode())
    res_1h = data_1h['chart']['result'][0]
    ts_1h = [dt.datetime.fromtimestamp(t, dt.timezone.utc) for t in res_1h['timestamp']]
    quote_1h = res_1h['indicators']['quote'][0]
    df_dxy_1h = pd.DataFrame({
        'open': quote_1h['open'],
        'high': quote_1h['high'],
        'low': quote_1h['low'],
        'close': quote_1h['close']
    }, index=ts_1h).dropna()

df_dxy_1h['ema20'] = df_dxy_1h['close'].ewm(span=20, adjust=False).mean()
df_dxy_1h['ema50'] = df_dxy_1h['close'].ewm(span=50, adjust=False).mean()
df_dxy_1h['trend_h1'] = np.where(df_dxy_1h['close'] > df_dxy_1h['ema20'], 1, -1)

# 15-min DXY
url_15m = 'https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?range=1mo&interval=15m'
req_15m = urllib.request.Request(url_15m, headers=headers)
with urllib.request.urlopen(req_15m) as resp:
    data_15m = json.loads(resp.read().decode())
    res_15m = data_15m['chart']['result'][0]
    ts_15m = [dt.datetime.fromtimestamp(t, dt.timezone.utc) for t in res_15m['timestamp']]
    quote_15m = res_15m['indicators']['quote'][0]
    df_dxy_15m = pd.DataFrame({
        'open': quote_15m['open'],
        'high': quote_15m['high'],
        'low': quote_15m['low'],
        'close': quote_15m['close']
    }, index=ts_15m).dropna()

df_dxy_15m['ema21'] = df_dxy_15m['close'].ewm(span=21, adjust=False).mean()
df_dxy_15m['trend_m15'] = np.where(df_dxy_15m['close'] > df_dxy_15m['ema21'], 1, -1)

print(f"DXY 1H bars: {len(df_dxy_1h)} | Range: {df_dxy_1h.index[0]} to {df_dxy_1h.index[-1]}")
print(f"DXY 15M bars: {len(df_dxy_15m)} | Range: {df_dxy_15m.index[0]} to {df_dxy_15m.index[-1]}")

# 2. Load historical bot setups from state.json
state = json.load(open('gha_state/state.json', encoding='utf-8'))
history = state.get('history', [])
outcomes = state.get('outcomes', {})

trade_records = []
for h in history:
    sym = h.get('symbol')
    sig_ts_str = h.get('ts')
    key = f"{sym}:{sig_ts_str}"
    if key not in outcomes:
        continue
    o = outcomes[key]
    hit = o.get('hit')
    if hit not in ('tp', 'sl'):
        continue

    sig_ts = pd.to_datetime(sig_ts_str, utc=True)
    dir_str = h.get('dir') # 'LONG' or 'SHORT'
    direction = 1 if dir_str == 'LONG' else -1
    rr = float(h.get('rr', 1.0))
    pnl_r = rr if hit == 'tp' else -1.0

    # Determine expected USD direction:
    # If sym is EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD:
    #   LONG setup expects USD to drop (DXY Bearish: -1)
    #   SHORT setup expects USD to rise (DXY Bullish: +1)
    # If sym is USDCAD, USDCHF, USDJPY:
    #   LONG setup expects USD to rise (DXY Bullish: +1)
    #   SHORT setup expects USD to drop (DXY Bearish: -1)
    is_direct = sym in ('EURUSD', 'GBPUSD', 'AUDUSD', 'NZDUSD', 'XAUUSD')
    if is_direct:
        expected_dxy = -1 if direction == 1 else 1
    else:
        expected_dxy = 1 if direction == 1 else -1

    # Find DXY state at sig_ts
    past_dxy_1h = df_dxy_1h[df_dxy_1h.index <= sig_ts]
    past_dxy_15m = df_dxy_15m[df_dxy_15m.index <= sig_ts]

    if past_dxy_1h.empty:
        continue

    dxy_1h_trend = past_dxy_1h.iloc[-1]['trend_h1']
    dxy_15m_trend = past_dxy_15m.iloc[-1]['trend_m15'] if not past_dxy_15m.empty else dxy_1h_trend

    aligned_h1 = (dxy_1h_trend == expected_dxy)
    aligned_15m = (dxy_15m_trend == expected_dxy)

    trade_records.append({
        'key': key,
        'sym': sym,
        'ts': sig_ts,
        'dir': dir_str,
        'hit': hit,
        'rr': rr,
        'pnl_r': pnl_r,
        'expected_dxy': 'BULLISH' if expected_dxy == 1 else 'BEARISH',
        'dxy_1h_trend': 'BULLISH' if dxy_1h_trend == 1 else 'BEARISH',
        'dxy_15m_trend': 'BULLISH' if dxy_15m_trend == 1 else 'BEARISH',
        'aligned_h1': aligned_h1,
        'aligned_15m': aligned_15m
    })

df_res = pd.DataFrame(trade_records)
print(f"\nTotal historical setups evaluated against DXY: {len(df_res)}")

print("\n" + "="*50)
print("BASELINE (ALL DELIVERED TRADES WITHOUT DXY FILTER)")
print("="*50)
total_trades = len(df_res)
wins = len(df_res[df_res['hit'] == 'tp'])
losses = len(df_res[df_res['hit'] == 'sl'])
winrate = (wins / total_trades) * 100 if total_trades else 0
net_r = df_res['pnl_r'].sum()
print(f"Total Trades: {total_trades} | Wins: {wins} | Losses: {losses} | Win Rate: {winrate:.1f}% | Net Profit: {net_r:+.2f}R")

print("\n" + "="*50)
print("FILTER 1: DXY H1 TREND FILTER (Only take setups ALIGNED with DXY H1 Trend)")
print("="*50)
f1 = df_res[df_res['aligned_h1'] == True]
f1_total = len(f1)
f1_wins = len(f1[f1['hit'] == 'tp'])
f1_losses = len(f1[f1['hit'] == 'sl'])
f1_wr = (f1_wins / f1_total) * 100 if f1_total else 0
f1_net_r = f1['pnl_r'].sum()
print(f"Filtered Setups Kept: {f1_total} ({f1_total/total_trades*100:.1f}% kept, {total_trades - f1_total} discarded)")
print(f"Wins: {f1_wins} | Losses: {f1_losses} | Win Rate: {f1_wr:.1f}% | Net Profit: {f1_net_r:+.2f}R")

# Look at the discarded trades
d1 = df_res[df_res['aligned_h1'] == False]
d1_wins = len(d1[d1['hit'] == 'tp'])
d1_losses = len(d1[d1['hit'] == 'sl'])
print(f"Discarded Trades breakdown: {d1_wins} Wins discarded vs {d1_losses} Losses avoided (Net avoided R: {d1['pnl_r'].sum():+.2f}R)")

print("\n" + "="*50)
print("FILTER 2: DXY M15 MOMENTUM FILTER (Only take setups ALIGNED with DXY M15 Trend)")
print("="*50)
f2 = df_res[df_res['aligned_15m'] == True]
f2_total = len(f2)
f2_wins = len(f2[f2['hit'] == 'tp'])
f2_losses = len(f2[f2['hit'] == 'sl'])
f2_wr = (f2_wins / f2_total) * 100 if f2_total else 0
f2_net_r = f2['pnl_r'].sum()
print(f"Filtered Setups Kept: {f2_total} ({f2_total/total_trades*100:.1f}% kept, {total_trades - f2_total} discarded)")
print(f"Wins: {f2_wins} | Losses: {f2_losses} | Win Rate: {f2_wr:.1f}% | Net Profit: {f2_net_r:+.2f}R")

d2 = df_res[df_res['aligned_15m'] == False]
d2_wins = len(d2[d2['hit'] == 'tp'])
d2_losses = len(d2[d2['hit'] == 'sl'])
print(f"Discarded Trades breakdown: {d2_wins} Wins discarded vs {d2_losses} Losses avoided (Net avoided R: {d2['pnl_r'].sum():+.2f}R)")

print("\n" + "="*50)
print("PAIR BY PAIR BREAKDOWN UNDER DXY H1 FILTER")
print("="*50)
for s in sorted(df_res['sym'].unique()):
    sub = df_res[df_res['sym'] == s]
    sub_f = sub[sub['aligned_h1'] == True]
    wr_base = (len(sub[sub['hit']=='tp']) / len(sub) * 100) if len(sub) else 0
    wr_f = (len(sub_f[sub_f['hit']=='tp']) / len(sub_f) * 100) if len(sub_f) else 0
    r_base = sub['pnl_r'].sum()
    r_f = sub_f['pnl_r'].sum()
    print(f"{s:7s} | Base: {len(sub):2d} trades (WR {wr_base:4.1f}%, {r_base:+5.2f}R) -> With DXY: {len(sub_f):2d} trades (WR {wr_f:4.1f}%, {r_f:+5.2f}R)")

print("\n" + "="*50)
print("DXY FILTER ON FOREX PAIRS ONLY (EXCLUDING GOLD XAUUSD)")
print("="*50)
fx_all = df_res[df_res['sym'] != 'XAUUSD']
fx_h1 = fx_all[fx_all['aligned_h1'] == True]
fx_m15 = fx_all[fx_all['aligned_15m'] == True]

wr_fx_base = len(fx_all[fx_all['hit']=='tp']) / len(fx_all) * 100
wr_fx_h1 = len(fx_h1[fx_h1['hit']=='tp']) / len(fx_h1) * 100
wr_fx_m15 = len(fx_m15[fx_m15['hit']=='tp']) / len(fx_m15) * 100

print(f"Forex Baseline (No Filter)    : {len(fx_all)} trades | Win Rate: {wr_fx_base:.1f}% | Net Profit: {fx_all['pnl_r'].sum():+.2f}R")
print(f"Forex With DXY H1 Trend Filter: {len(fx_h1)} trades | Win Rate: {wr_fx_h1:.1f}% | Net Profit: {fx_h1['pnl_r'].sum():+.2f}R")
print(f"Forex With DXY M15 Momentum   : {len(fx_m15)} trades | Win Rate: {wr_fx_m15:.1f}% | Net Profit: {fx_m15['pnl_r'].sum():+.2f}R")

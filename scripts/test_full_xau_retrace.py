import sys
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.tv_data import load_cached
from strategy import candidates
from strategy.profiles import PROFILES

strat = candidates.STRATEGIES["fvg_retest"]
p = PROFILES["balanced"]["params"]
params = dict(p)
params["_symbol"] = "XAUUSD"
params["bias_htf"] = "H1"

df_m30 = load_cached("XAUUSD", "M30")
df_m15 = load_cached("XAUUSD", "M15")

sigs = strat.signaller(df_m30, params)
print(f"Generated {len(sigs)} FVG-retest signals on 4,000 M30 bars for XAUUSD.")

baseline_rs = []
sniper_rs = []
sniper_be_rs = []

total_signals = 0
filled_sniper = 0
inval_tp_count = 0

for sig in sigs:
    if sig.ts not in df_m30.index:
        continue
    pos = df_m30.index.get_loc(sig.ts)
    if pos + 2 >= len(df_m30):
        continue

    entry_base = float(df_m30["open"].iloc[pos + 1])
    sl = entry_base - sig.dir * sig.sl_offset
    risk = abs(entry_base - sl)
    if risk <= 0:
        continue

    tp = sig.tp_price if sig.tp_price is not None else (entry_base + sig.dir * risk * sig.tp_r)
    rr = abs(tp - entry_base) / risk

    m15_sub = df_m15[df_m15.index > sig.ts]
    if len(m15_sub) < 4:
        continue

    total_signals += 1

    # 1. Baseline trade
    b_exit = None
    for _, row in m15_sub.iterrows():
        if sig.dir == 1:
            if row["low"] <= sl:
                b_exit = -1.0
                break
            if row["high"] >= tp:
                b_exit = rr
                break
        else:
            if row["high"] >= sl:
                b_exit = -1.0
                break
            if row["low"] <= tp:
                b_exit = rr
                break
    if b_exit is not None:
        baseline_rs.append(b_exit)

    # 2. Retracement Sniper ($6.50 distance to SL):
    # Only applicable if original risk > $6.50
    retrace_zone = sl + sig.dir * 6.50
    tp_75 = entry_base + (tp - entry_base) * 0.75

    filled = False
    fill_idx = None
    bars_list = list(m15_sub.iterrows())

    for idx, (b_ts, row) in enumerate(bars_list[:16]): # wait max 4 hours (16 M15 bars)
        h_b, l_b = row["high"], row["low"]
        # Invalidation TP75
        if (sig.dir == 1 and h_b >= tp_75) or (sig.dir == -1 and l_b <= tp_75):
            inval_tp_count += 1
            break
        # Retrace touch
        if (sig.dir == 1 and l_b <= retrace_zone) or (sig.dir == -1 and h_b >= retrace_zone):
            filled = True
            fill_idx = idx
            filled_sniper += 1
            break

    if filled:
        sniper_risk = 6.50
        sniper_reward = abs(tp - retrace_zone)
        sniper_rr = sniper_reward / sniper_risk

        sub_bars = bars_list[fill_idx + 1:]
        s_res = None
        s_be_res = None
        hit_50_tp = False
        tp_50 = retrace_zone + (tp - retrace_zone) * 0.50

        for _, b_row in sub_bars:
            h_b, l_b = b_row["high"], b_row["low"]
            if (sig.dir == 1 and h_b >= tp_50) or (sig.dir == -1 and l_b <= tp_50):
                hit_50_tp = True

            active_be_sl = retrace_zone if hit_50_tp else sl

            sl_pure = (l_b <= sl) if sig.dir == 1 else (h_b >= sl)
            sl_be = (l_b <= active_be_sl) if sig.dir == 1 else (h_b >= active_be_sl)
            tp_hit = (h_b >= tp) if sig.dir == 1 else (l_b <= tp)

            if s_res is None:
                if sl_pure:
                    s_res = -1.0
                elif tp_hit:
                    s_res = sniper_rr

            if s_be_res is None:
                if sl_be:
                    s_be_res = 0.0 if hit_50_tp else -1.0
                elif tp_hit:
                    s_be_res = sniper_rr

            if s_res is not None and s_be_res is not None:
                break

        if s_res is not None:
            sniper_rs.append(s_res)
        if s_be_res is not None:
            sniper_be_rs.append(s_be_res)

def print_stat(name, rs):
    arr = np.array(rs)
    if len(arr) == 0:
        return
    wins = arr[arr > 0]
    losses = arr[arr < 0]
    wr = len(wins) / len(arr) * 100
    pf = wins.sum() / (-losses.sum()) if len(losses) > 0 and losses.sum() != 0 else 999.0
    print(f"{name:<35} n={len(arr):<4} WR={wr:<5.1f}% PF={pf:<5.2f} TotR={arr.sum():<+7.2f}R AvgR={arr.mean():<+5.3f}R")

print("\n" + "="*80)
print(f"LARGE-SAMPLE BACKTEST: 4,000 BARS ON XAUUSD (Total Signals: {total_signals})")
print(f"Sniper Retrace Fill Rate: {filled_sniper}/{total_signals} ({filled_sniper/total_signals*100:.1f}%)")
print("="*80)
print_stat("1. Baseline (Immediate Market)", baseline_rs)
print_stat("2. Sniper Retrace ($6.50 Hold)", sniper_rs)
print_stat("3. Sniper Retrace + BE @ 50% TP", sniper_be_rs)

"""Multi-Timeframe Chart Forensic & Strategy Invention Engine.

Systematically investigates, backtests, and validates institutional trading strategies
that are NOT currently in our active system:
1. ICT London Judas Swing (Asian Range Purge & Reverse)
2. ICT New York Silver Bullet (14:00 - 15:00 UTC Delivery Window)
3. Inter-Market SMT Divergence (EURUSD vs GBPUSD Correlated Engine)
4. Institutional Breaker Block Retest Engine (ICT Breaker)

Pulls tick-accurate M5, M15, H1, H4 data from MetaTrader 5 across:
EURUSD, GBPUSD, XAUUSD, USDCAD, USDJPY, AUDUSD, NZDUSD.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datetime as dt
import numpy as np
import pandas as pd
import MetaTrader5 as mt5

def fetch_mt5_df(symbol: str, timeframe: int, count: int = 15000) -> pd.DataFrame | None:
    """Fetch dataframe from MT5 with UTC index."""
    if not mt5.initialize():
        return None
    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.sort_index(inplace=True)
    return df

def get_point(symbol: str) -> float:
    if "JPY" in symbol or symbol == "XAUUSD":
        return 0.01
    return 0.0001

# ---------------------------------------------------------------------------
# STRATEGY 1: ICT London Judas Swing (Asian Range Purge & Reverse)
# ---------------------------------------------------------------------------
def backtest_judas_swing(symbol: str, df_m5: pd.DataFrame, df_h4: pd.DataFrame) -> dict:
    """Backtest London Judas Swing on M5 bars.
    Asian Range: 00:00 - 06:00 UTC.
    London Window: 06:30 - 09:30 UTC.
    Conditions:
    - Asian range size between 12 and 45 pips (Forex) / $4 - $25 (Gold).
    - Price sweeps Asian High or Asian Low by <= 25 pips (inducement sweep).
    - M5 candle closes back inside Asian range (MSS / displacement).
    - Pro-trend with H4 EMA50.
    - SL: Sweep extreme + 2 pips.
    - TP: Opposing Asian Range level (or 2.5R minimum).
    """
    point = get_point(symbol)
    pip_scale = point

    # Resample H4 EMA50
    h4_ema = df_h4["close"].ewm(span=50).mean()

    # Group M5 by date
    days = np.unique(df_m5.index.date)
    trades = []

    for d in days:
        day_m5 = df_m5[df_m5.index.date == d]
        if len(day_m5) < 50:
            continue

        # 1. Measure Asian Range (00:00 - 06:00 UTC)
        asia = day_m5[(day_m5.index.hour >= 0) & (day_m5.index.hour < 6)]
        if len(asia) < 12:
            continue

        asia_high = float(asia["high"].max())
        asia_low = float(asia["low"].min())
        asia_range_pips = (asia_high - asia_low) / pip_scale

        # Filter out flat or blown out ranges
        min_range = 10 if symbol != "XAUUSD" else 300
        max_range = 45 if symbol != "XAUUSD" else 2500
        if not (min_range <= asia_range_pips <= max_range):
            continue

        # 2. London Manipulation Window (06:30 - 09:30 UTC)
        london = day_m5[(day_m5.index.hour >= 6) & ((day_m5.index.hour < 9) | ((day_m5.index.hour == 9) & (day_m5.index.minute <= 30)))]
        if len(london) < 6:
            continue

        # Check H4 trend context at 06:00 UTC
        ts_6 = pd.Timestamp(dt.datetime.combine(d, dt.time(6, 0)), tz="UTC")
        h4_sub = h4_ema[h4_ema.index <= ts_6]
        if len(h4_sub) == 0:
            continue
        cur_h4_ema = h4_sub.iloc[-1]
        cur_h4_close = df_h4.loc[df_h4.index <= ts_6, "close"].iloc[-1]
        h4_bull = cur_h4_close > cur_h4_ema

        traded_today = False
        sweep_high = False
        sweep_low = False
        sweep_extreme = 0.0

        for i in range(len(london)):
            if traded_today:
                break
            bar = london.iloc[i]
            cur_t = london.index[i]
            if cur_t.hour == 6 and cur_t.minute < 30:
                continue

            hi = float(bar["high"])
            lo = float(bar["low"])
            cl = float(bar["close"])

            # Bullish Judas: Sweep below Asian Low, reclaim Asian Low, H4 Bullish
            if not sweep_low and lo < asia_low:
                overshoot = (asia_low - lo) / pip_scale
                max_overshoot = 25 if symbol != "XAUUSD" else 1500
                if overshoot <= max_overshoot:
                    sweep_low = True
                    sweep_extreme = lo

            if sweep_low and not traded_today:
                if lo < sweep_extreme:
                    sweep_extreme = lo
                # Reclaim trigger: M5 closes back ABOVE asia_low
                if cl > asia_low and h4_bull:
                    entry = cl
                    sl = sweep_extreme - (2.0 * pip_scale)
                    sl_dist = abs(entry - sl)
                    if sl_dist > 0:
                        # TP is Opposing Asian High
                        tp = asia_high
                        tp_dist = abs(tp - entry)
                        rr = tp_dist / sl_dist
                        if rr >= 1.5:
                            # Forward simulate outcome
                            forward = day_m5[day_m5.index > cur_t]
                            hit = "open"
                            exit_p = entry
                            for _, f_bar in forward.iterrows():
                                if f_bar["low"] <= sl:
                                    hit = "sl"; exit_p = sl; break
                                if f_bar["high"] >= tp:
                                    hit = "tp"; exit_p = tp; break
                            if hit == "open" and len(forward) > 0:
                                last_close = forward.iloc[-1]["close"]
                                hit = "eod"
                                rr = (last_close - entry) / sl_dist
                            trades.append({
                                "date": str(d), "type": "Judas_BUY", "entry": entry,
                                "sl": sl, "tp": tp, "rr": round(rr, 2), "hit": hit,
                                "sl_dist_pips": round(sl_dist / pip_scale, 1),
                                "pnl_r": round(rr if hit == "tp" else (-1.0 if hit == "sl" else rr), 2)
                            })
                            traded_today = True

            # Bearish Judas: Sweep above Asian High, reclaim Asian High, H4 Bearish
            if not sweep_high and hi > asia_high:
                overshoot = (hi - asia_high) / pip_scale
                max_overshoot = 25 if symbol != "XAUUSD" else 1500
                if overshoot <= max_overshoot:
                    sweep_high = True
                    sweep_extreme = hi

            if sweep_high and not traded_today:
                if hi > sweep_extreme:
                    sweep_extreme = hi
                # Reclaim trigger: M5 closes back BELOW asia_high
                if cl < asia_high and not h4_bull:
                    entry = cl
                    sl = sweep_extreme + (2.0 * pip_scale)
                    sl_dist = abs(entry - sl)
                    if sl_dist > 0:
                        # TP is Opposing Asian Low
                        tp = asia_low
                        tp_dist = abs(entry - tp)
                        rr = tp_dist / sl_dist
                        if rr >= 1.5:
                            forward = day_m5[day_m5.index > cur_t]
                            hit = "open"
                            exit_p = entry
                            for _, f_bar in forward.iterrows():
                                if f_bar["high"] >= sl:
                                    hit = "sl"; exit_p = sl; break
                                if f_bar["low"] <= tp:
                                    hit = "tp"; exit_p = tp; break
                            if hit == "open" and len(forward) > 0:
                                last_close = forward.iloc[-1]["close"]
                                hit = "eod"
                                rr = (entry - last_close) / sl_dist
                            trades.append({
                                "date": str(d), "type": "Judas_SELL", "entry": entry,
                                "sl": sl, "tp": tp, "rr": round(rr, 2), "hit": hit,
                                "sl_dist_pips": round(sl_dist / pip_scale, 1),
                                "pnl_r": round(rr if hit == "tp" else (-1.0 if hit == "sl" else rr), 2)
                            })
                            traded_today = True

    return _summarize("London Judas Swing", symbol, trades)

# ---------------------------------------------------------------------------
# STRATEGY 2: ICT NY Silver Bullet (14:00 - 15:00 UTC Delivery Window)
# ---------------------------------------------------------------------------
def backtest_silver_bullet(symbol: str, df_m5: pd.DataFrame, df_h1: pd.DataFrame) -> dict:
    """Backtest ICT Silver Bullet model:
    Time: 14:00 to 15:00 UTC strictly.
    1. Pre-session liquidity sweep: Price sweeps high or low of 12:00 - 13:30 UTC.
    2. Trend alignment: H1 50 EMA.
    3. M5 Displacement creating a clean Fair Value Gap (FVG) inside 14:00 - 15:00 UTC.
    4. Limit entry on FVG retest with fixed 2.0R to 2.5R target.
    """
    point = get_point(symbol)
    pip_scale = point
    h1_ema = df_h1["close"].ewm(span=50).mean()

    days = np.unique(df_m5.index.date)
    trades = []

    for d in days:
        day_m5 = df_m5[df_m5.index.date == d]
        if len(day_m5) < 50:
            continue

        # Pre-window benchmark range: 12:00 to 13:45 UTC
        pre_range = day_m5[(day_m5.index.hour >= 12) & ((day_m5.index.hour < 13) | ((day_m5.index.hour == 13) & (day_m5.index.minute <= 45)))]
        if len(pre_range) < 10:
            continue
        pre_high = float(pre_range["high"].max())
        pre_low = float(pre_range["low"].min())

        # Check H1 trend
        ts_14 = pd.Timestamp(dt.datetime.combine(d, dt.time(14, 0)), tz="UTC")
        h1_sub = h1_ema[h1_ema.index <= ts_14]
        if len(h1_sub) == 0:
            continue
        h1_bull = df_h1.loc[df_h1.index <= ts_14, "close"].iloc[-1] > h1_sub.iloc[-1]

        # Silver Bullet Execution Window: 14:00 to 15:00 UTC
        sb_window = day_m5[(day_m5.index.hour == 14)]
        if len(sb_window) < 6:
            continue

        traded = False
        for i in range(2, len(sb_window)):
            if traded:
                break
            b0 = sb_window.iloc[i-2]
            b1 = sb_window.iloc[i-1]
            b2 = sb_window.iloc[i]
            cur_t = sb_window.index[i]

            # Bullish FVG: b0['high'] < b2['low'] (gap between bar 0 high and bar 2 low)
            if h1_bull and b2["low"] > b0["high"]:
                gap_size = (b2["low"] - b0["high"]) / pip_scale
                min_gap = 1.5 if symbol != "XAUUSD" else 50.0
                if gap_size >= min_gap:
                    entry = float(b2["low"])  # Top of FVG
                    sl = float(b1["low"]) - (1.5 * pip_scale) # Low of displacement candle
                    sl_dist = abs(entry - sl)
                    if sl_dist > 0:
                        rr = 2.0
                        tp = entry + (rr * sl_dist)
                        # Simulate forward
                        forward = day_m5[day_m5.index > cur_t]
                        hit = "open"
                        for _, f_bar in forward.iterrows():
                            if f_bar["low"] <= sl:
                                hit = "sl"; break
                            if f_bar["high"] >= tp:
                                hit = "tp"; break
                        if hit == "open" and len(forward) > 0:
                            last_close = forward.iloc[-1]["close"]
                            hit = "eod"
                            rr = (last_close - entry) / sl_dist
                        trades.append({
                            "date": str(d), "type": "SilverBullet_BUY", "entry": entry,
                            "sl": sl, "tp": tp, "rr": round(rr, 2), "hit": hit,
                            "sl_dist_pips": round(sl_dist / pip_scale, 1),
                            "pnl_r": round(rr if hit == "tp" else (-1.0 if hit == "sl" else rr), 2)
                        })
                        traded = True

            # Bearish FVG: b0['low'] > b2['high'] (gap between bar 0 low and bar 2 high)
            elif (not h1_bull) and b0["low"] > b2["high"]:
                gap_size = (b0["low"] - b2["high"]) / pip_scale
                min_gap = 1.5 if symbol != "XAUUSD" else 50.0
                if gap_size >= min_gap:
                    entry = float(b2["high"])  # Bottom of FVG
                    sl = float(b1["high"]) + (1.5 * pip_scale) # High of displacement candle
                    sl_dist = abs(entry - sl)
                    if sl_dist > 0:
                        rr = 2.0
                        tp = entry - (rr * sl_dist)
                        forward = day_m5[day_m5.index > cur_t]
                        hit = "open"
                        for _, f_bar in forward.iterrows():
                            if f_bar["high"] >= sl:
                                hit = "sl"; break
                            if f_bar["low"] <= tp:
                                hit = "tp"; break
                        if hit == "open" and len(forward) > 0:
                            last_close = forward.iloc[-1]["close"]
                            hit = "eod"
                            rr = (entry - last_close) / sl_dist
                        trades.append({
                            "date": str(d), "type": "SilverBullet_SELL", "entry": entry,
                            "sl": sl, "tp": tp, "rr": round(rr, 2), "hit": hit,
                            "sl_dist_pips": round(sl_dist / pip_scale, 1),
                            "pnl_r": round(rr if hit == "tp" else (-1.0 if hit == "sl" else rr), 2)
                        })
                        traded = True

    return _summarize("ICT NY Silver Bullet", symbol, trades)

# ---------------------------------------------------------------------------
# STRATEGY 3: Inter-Market SMT Divergence (EURUSD vs GBPUSD)
# ---------------------------------------------------------------------------
def backtest_smt_divergence(df_eur_m15: pd.DataFrame, df_gbp_m15: pd.DataFrame) -> dict:
    """Backtest SMT Divergence between EURUSD and GBPUSD on M15 bars.
    When EURUSD makes a Lower Low (sweeping previous swing low) but GBPUSD fails (Higher Low),
    or EURUSD makes a Higher High but GBPUSD makes a Lower High:
    SMT Divergence triggers on the subsequent displacement candle!
    """
    common_idx = df_eur_m15.index.intersection(df_gbp_m15.index)
    eur = df_eur_m15.loc[common_idx]
    gbp = df_gbp_m15.loc[common_idx]

    point = 0.0001
    trades = []

    # Swing detection lookback
    n = len(eur)
    for i in range(40, n - 20):
        # Scan during active sessions: 06:00 to 17:00 UTC
        cur_t = eur.index[i]
        if not (6 <= cur_t.hour <= 17):
            continue

        # Look for local 20-bar swing highs and lows
        window_eur = eur.iloc[i-25:i]
        window_gbp = gbp.iloc[i-25:i]

        eur_hi = window_eur["high"].max()
        eur_lo = window_eur["low"].min()
        gbp_hi = window_gbp["high"].max()
        gbp_lo = window_gbp["low"].min()

        cur_eur_hi = eur["high"].iloc[i]
        cur_eur_lo = eur["low"].iloc[i]
        cur_gbp_hi = gbp["high"].iloc[i]
        cur_gbp_lo = gbp["low"].iloc[i]

        # Bullish SMT: EUR sweeps lower low, but GBP holds higher low!
        if cur_eur_lo < eur_lo and cur_gbp_lo > gbp_lo:
            # Check displacement on GBP (strong close)
            if gbp["close"].iloc[i] > gbp["open"].iloc[i]:
                entry = float(gbp["close"].iloc[i])
                sl = float(cur_gbp_lo) - (2.0 * point)
                sl_dist = abs(entry - sl)
                if sl_dist > 0:
                    rr = 2.5
                    tp = entry + (rr * sl_dist)
                    forward = gbp.iloc[i+1:i+100]
                    hit = "open"
                    for _, f_bar in forward.iterrows():
                        if f_bar["low"] <= sl: hit = "sl"; break
                        if f_bar["high"] >= tp: hit = "tp"; break
                    if hit in ("tp", "sl"):
                        trades.append({
                            "date": str(cur_t.date()), "type": "SMT_BUY_GBP",
                            "entry": entry, "sl": sl, "tp": tp, "rr": rr, "hit": hit,
                            "pnl_r": rr if hit == "tp" else -1.0
                        })

        # Bearish SMT: EUR sweeps higher high, but GBP makes lower high!
        elif cur_eur_hi > eur_hi and cur_gbp_hi < gbp_hi:
            if gbp["close"].iloc[i] < gbp["open"].iloc[i]:
                entry = float(gbp["close"].iloc[i])
                sl = float(cur_gbp_hi) + (2.0 * point)
                sl_dist = abs(entry - sl)
                if sl_dist > 0:
                    rr = 2.5
                    tp = entry - (rr * sl_dist)
                    forward = gbp.iloc[i+1:i+100]
                    hit = "open"
                    for _, f_bar in forward.iterrows():
                        if f_bar["high"] >= sl: hit = "sl"; break
                        if f_bar["low"] <= tp: hit = "tp"; break
                    if hit in ("tp", "sl"):
                        trades.append({
                            "date": str(cur_t.date()), "type": "SMT_SELL_GBP",
                            "entry": entry, "sl": sl, "tp": tp, "rr": rr, "hit": hit,
                            "pnl_r": rr if hit == "tp" else -1.0
                        })

    return _summarize("SMT Divergence Engine", "GBPUSD vs EURUSD", trades)

# ---------------------------------------------------------------------------
# STRATEGY 4: Institutional Breaker Block Retest Engine (ICT Breaker)
# ---------------------------------------------------------------------------
def backtest_breaker_block(symbol: str, df_m15: pd.DataFrame, df_h4: pd.DataFrame) -> dict:
    """Backtest ICT Breaker Block Model on M15 bars:
    Bullish Breaker:
    - High (H1) -> Low (L1) -> Lower Low (L2, sweeps L1) -> Displaced Breakout above H1.
    - The bearish candle before L2 is the Breaker Block.
    - Entry: Retest of Breaker Block level (between H1 and L1).
    - SL: Below L2.
    - TP: 2.5R target.
    """
    point = get_point(symbol)
    pip_scale = point
    h4_ema = df_h4["close"].ewm(span=50).mean()
    trades = []

    n = len(df_m15)
    for i in range(50, n - 50):
        cur_t = df_m15.index[i]
        if cur_t.hour < 6 or cur_t.hour >= 17:
            continue

        # Lookback 30 bars for H1, L1, L2 structure
        window = df_m15.iloc[i-30:i]
        h4_val = h4_ema.loc[h4_ema.index <= cur_t]
        if len(h4_val) == 0:
            continue
        h4_bull = df_h4.loc[df_h4.index <= cur_t, "close"].iloc[-1] > h4_val.iloc[-1]

        l1 = window["low"].iloc[:15].min()
        h1 = window["high"].iloc[5:20].max()
        l2 = window["low"].iloc[15:].min()
        h2 = window["high"].iloc[20:].max()

        # Bullish Breaker: L2 < L1 (liquidity sweep), H2 > H1 (structural break)
        if h4_bull and l2 < l1 and h2 > h1:
            breaker_level = h1
            # Current bar retests breaker level
            if df_m15["low"].iloc[i] <= breaker_level <= df_m15["high"].iloc[i] and df_m15["close"].iloc[i] > breaker_level:
                entry = float(df_m15["close"].iloc[i])
                sl = float(l2) - (2.0 * pip_scale)
                sl_dist = abs(entry - sl)
                if sl_dist > 0:
                    rr = 2.5
                    tp = entry + (rr * sl_dist)
                    forward = df_m15.iloc[i+1:i+120]
                    hit = "open"
                    for _, f_bar in forward.iterrows():
                        if f_bar["low"] <= sl: hit = "sl"; break
                        if f_bar["high"] >= tp: hit = "tp"; break
                    if hit in ("tp", "sl"):
                        trades.append({
                            "date": str(cur_t.date()), "type": "Breaker_BUY",
                            "entry": entry, "sl": sl, "tp": tp, "rr": rr, "hit": hit,
                            "pnl_r": rr if hit == "tp" else -1.0
                        })

        # Bearish Breaker: H2 > H1 (liquidity sweep), L2 < L1 (structural break)
        elif (not h4_bull) and h2 > h1 and l2 < l1:
            breaker_level = l1
            if df_m15["low"].iloc[i] <= breaker_level <= df_m15["high"].iloc[i] and df_m15["close"].iloc[i] < breaker_level:
                entry = float(df_m15["close"].iloc[i])
                sl = float(h2) + (2.0 * pip_scale)
                sl_dist = abs(entry - sl)
                if sl_dist > 0:
                    rr = 2.5
                    tp = entry - (rr * sl_dist)
                    forward = df_m15.iloc[i+1:i+120]
                    hit = "open"
                    for _, f_bar in forward.iterrows():
                        if f_bar["high"] >= sl: hit = "sl"; break
                        if f_bar["low"] <= tp: hit = "tp"; break
                    if hit in ("tp", "sl"):
                        trades.append({
                            "date": str(cur_t.date()), "type": "Breaker_SELL",
                            "entry": entry, "sl": sl, "tp": tp, "rr": rr, "hit": hit,
                            "pnl_r": rr if hit == "tp" else -1.0
                        })

    return _summarize("Institutional Breaker Block", symbol, trades)

# ---------------------------------------------------------------------------
# SUMMARY HELPER
# ---------------------------------------------------------------------------
def _summarize(strat_name: str, symbol: str, trades: list[dict]) -> dict:
    if not trades:
        return {"strategy": strat_name, "symbol": symbol, "n": 0, "wr": 0.0, "pf": 0.0, "payoff": 0.0, "net_r": 0.0, "r_per_week": 0.0}

    df = pd.DataFrame(trades)
    n = len(df)
    tp_trades = df[df["hit"] == "tp"]
    sl_trades = df[df["hit"] == "sl"]
    n_tp = len(tp_trades)
    n_sl = len(sl_trades)

    wr = (n_tp / n) * 100 if n > 0 else 0.0
    gross_win = tp_trades["pnl_r"].sum()
    gross_loss = abs(sl_trades["pnl_r"].sum())
    pf = (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    avg_win = tp_trades["pnl_r"].mean() if n_tp > 0 else 0.0
    avg_loss = abs(sl_trades["pnl_r"].mean()) if n_sl > 0 else 1.0
    payoff = (avg_win / avg_loss) if avg_loss > 0 else 0.0
    net_r = df["pnl_r"].sum()

    # Calculate approximate weeks
    d_start = pd.to_datetime(df["date"].min())
    d_end = pd.to_datetime(df["date"].max())
    days = max(1, (d_end - d_start).days)
    weeks = max(1.0, days / 7.0)
    r_per_wk = net_r / weeks

    return {
        "strategy": strat_name,
        "symbol": symbol,
        "n": n,
        "n_tp": n_tp,
        "n_sl": n_sl,
        "wr": round(wr, 1),
        "pf": round(pf, 2),
        "payoff": round(payoff, 2),
        "net_r": round(net_r, 1),
        "weeks": round(weeks, 1),
        "r_per_week": round(r_per_wk, 2)
    }

# ---------------------------------------------------------------------------
# MAIN EXECUTION & FORENSIC BATTLE ROYALE
# ---------------------------------------------------------------------------
def main():
    print("=" * 80)
    print("GOLD-FX MULTI-TIMEFRAME STRATEGY DISCOVERY & FORENSIC AUDIT")
    print("Systematic evaluation of institutional strategies NOT currently active")
    print("=" * 80)

    symbols = ["EURUSD", "GBPUSD", "XAUUSD", "USDCAD", "USDJPY"]
    all_results = []

    # 1. Backtest Candidate 1: London Judas Swing (Asian Range Purge & Reverse)
    print("\n[1/4] Running London Judas Swing (Asian Range Purge & Reverse)...")
    for sym in symbols:
        df_m5 = fetch_mt5_df(sym, mt5.TIMEFRAME_M5, count=15000)
        df_h4 = fetch_mt5_df(sym, mt5.TIMEFRAME_H4, count=3000)
        if df_m5 is not None and df_h4 is not None:
            res = backtest_judas_swing(sym, df_m5, df_h4)
            all_results.append(res)
            print(f"  {res['strategy']:22} | {res['symbol']:8} | Trades: {res['n']:3} | WR: {res['wr']:4.1f}% | PF: {res['pf']:4.2f} | Net: {res['net_r']:+6.1f}R | {res['r_per_week']:+5.2f} R/wk")

    # 2. Backtest Candidate 2: ICT NY Silver Bullet (14:00 - 15:00 UTC)
    print("\n[2/4] Running ICT NY Silver Bullet (14:00 - 15:00 UTC)...")
    for sym in symbols:
        df_m5 = fetch_mt5_df(sym, mt5.TIMEFRAME_M5, count=15000)
        df_h1 = fetch_mt5_df(sym, mt5.TIMEFRAME_H1, count=5000)
        if df_m5 is not None and df_h1 is not None:
            res = backtest_silver_bullet(sym, df_m5, df_h1)
            all_results.append(res)
            print(f"  {res['strategy']:22} | {res['symbol']:8} | Trades: {res['n']:3} | WR: {res['wr']:4.1f}% | PF: {res['pf']:4.2f} | Net: {res['net_r']:+6.1f}R | {res['r_per_week']:+5.2f} R/wk")

    # 3. Backtest Candidate 3: SMT Inter-Market Divergence (EURUSD vs GBPUSD)
    print("\n[3/4] Running Inter-Market SMT Divergence (EURUSD vs GBPUSD)...")
    df_eur_m15 = fetch_mt5_df("EURUSD", mt5.TIMEFRAME_M15, count=15000)
    df_gbp_m15 = fetch_mt5_df("GBPUSD", mt5.TIMEFRAME_M15, count=15000)
    if df_eur_m15 is not None and df_gbp_m15 is not None:
        res = backtest_smt_divergence(df_eur_m15, df_gbp_m15)
        all_results.append(res)
        print(f"  {res['strategy']:22} | {res['symbol']:18} | Trades: {res['n']:3} | WR: {res['wr']:4.1f}% | PF: {res['pf']:4.2f} | Net: {res['net_r']:+6.1f}R | {res['r_per_week']:+5.2f} R/wk")

    # 4. Backtest Candidate 4: Institutional Breaker Blocks
    print("\n[4/4] Running Institutional Breaker Block Retests...")
    for sym in ["EURUSD", "GBPUSD", "XAUUSD"]:
        df_m15 = fetch_mt5_df(sym, mt5.TIMEFRAME_M15, count=15000)
        df_h4 = fetch_mt5_df(sym, mt5.TIMEFRAME_H4, count=3000)
        if df_m15 is not None and df_h4 is not None:
            res = backtest_breaker_block(sym, df_m15, df_h4)
            all_results.append(res)
            print(f"  {res['strategy']:22} | {res['symbol']:8} | Trades: {res['n']:3} | WR: {res['wr']:4.1f}% | PF: {res['pf']:4.2f} | Net: {res['net_r']:+6.1f}R | {res['r_per_week']:+5.2f} R/wk")

    # Final Comparative League Table
    df_res = pd.DataFrame(all_results)
    df_res.sort_values(by="r_per_week", ascending=False, inplace=True)
    print("\n" + "=" * 90)
    print("FINAL LEAGUE TABLE: NEW STRATEGY CANDIDATES RANKED BY EXPECTED R / WEEK")
    print("=" * 90)
    print(df_res[["strategy", "symbol", "n", "wr", "payoff", "pf", "net_r", "r_per_week"]].to_string(index=False))

    mt5.shutdown()

if __name__ == "__main__":
    main()

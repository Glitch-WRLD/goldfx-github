"""Comprehensive Head-to-Head Cross-Currency Pairs & Exit Management Backtest:

1. Evaluates all 18 non-USD Forex Cross Pairs:
   - JPY Crosses: GBPJPY, EURJPY, AUDJPY, CADJPY, CHFJPY, NZDJPY
   - GBP Crosses: EURGBP, GBPAUD, GBPCAD, GBPCHF, GBPNZD
   - EUR Crosses: EURAUD, EURCAD, EURCHF, EURNZD
   - Commodity Crosses: AUDCAD, AUDNZD, NZDCAD

2. Compares 4 Exit Management Paradigms:
   - Naked Hold (Baseline: Hold to original TP or SL)
   - Method A (Fixed R: BE @ +0.8R, Lock +0.5R @ +1.3R, Lock +1.0R @ +1.6R)
   - Method B (Percentage-Wise: BE @ 50% TP, Lock 50% @ 75% TP, Lock 75% @ 90% TP)
   - Method C (Percentage-Wise Conservative: BE @ 60% TP, Lock 60% @ 80% TP)

3. Evaluates both:
   - Real delivered setups from gha_state/state.json
   - Full 4,000-bar historical data per cross pair from MT5
"""
import sys
import os
import json
from pathlib import Path
import numpy as np
import pandas as pd
import MetaTrader5 as mt5

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from strategy.candidates import STRATEGIES
from strategy.profiles import BAL_PROFILE
from data.tv_data import load_cached, save_cached

CROSS_SYMBOLS = [
    # Top Tier Liquid Crosses
    ("GBPJPY", "H1", "H4"),
    ("EURJPY", "H1", "H4"),
    ("AUDJPY", "H1", "H4"),
    ("CADJPY", "H1", "H4"),
    ("CHFJPY", "H1", "H4"),
    ("NZDJPY", "H1", "H4"),
    # GBP Crosses
    ("EURGBP", "H1", "H4"),
    ("GBPAUD", "H1", "H4"),
    ("GBPCAD", "H1", "H4"),
    ("GBPCHF", "H1", "H4"),
    ("GBPNZD", "H1", "H4"),
    # EUR Crosses
    ("EURAUD", "H1", "H4"),
    ("EURCAD", "H1", "H4"),
    ("EURCHF", "H1", "H4"),
    ("EURNZD", "H1", "H4"),
    # Commodity Crosses
    ("AUDCAD", "H1", "H4"),
    ("AUDNZD", "H1", "H4"),
    ("NZDCAD", "H1", "H4"),
]


def fetch_symbol_data(symbol: str, tf_str: str = "H1", count: int = 4000) -> pd.DataFrame | None:
    df = load_cached(symbol, tf_str)
    if df is not None and len(df) >= 3000:
        return df

    tf_mt5 = mt5.TIMEFRAME_H1 if tf_str == "H1" else (mt5.TIMEFRAME_M30 if tf_str == "M30" else mt5.TIMEFRAME_M15)
    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, tf_mt5, 0, count)
    if rates is None or len(rates) == 0:
        return None

    df_mt5 = pd.DataFrame(rates)
    df_mt5["time"] = pd.to_datetime(df_mt5["time"], unit="s", utc=True)
    df_mt5 = df_mt5.rename(columns={"tick_volume": "volume"}).set_index("time").sort_index()
    save_cached(symbol, tf_str, df_mt5)
    return df_mt5


def simulate_trade_with_trailing(df, pos_idx, side, entry, sl, tp, rr, slip, pip_buffer=0.0):
    """Walk subsequent bars and simulate 4 exit strategies."""
    risk = abs(entry - sl)
    target_dist = abs(tp - entry)
    if risk <= 0 or target_dist <= 0:
        return None

    # Trackers for the 4 methods:
    # 0: Naked Hold
    # 1: Fixed R (BE @ 0.8R, Trail 0.5R @ 1.3R, Trail 1.0R @ 1.6R)
    # 2: Pct 50/75/90 (BE @ 50% TP, Trail 50% @ 75% TP, Trail 75% @ 90% TP)
    # 3: Pct 60/80 (BE @ 60% TP, Trail 60% @ 80% TP)

    sl_levels = [sl, sl, sl, sl]
    state = ["initial", "initial", "initial", "initial"]
    finished = [False, False, False, False]
    r_results = [0.0, 0.0, 0.0, 0.0]

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()

    peak_mfe_r = 0.0
    peak_pct_tp = 0.0

    for j in range(pos_idx + 2, len(df)):
        h = highs[j]
        l = lows[j]
        c = closes[j]

        # Calculate current bar extremes
        if side == 1:
            bar_mfe = max(0.0, h - entry)
            bar_mae = max(0.0, entry - l)
        else:
            bar_mfe = max(0.0, entry - l)
            bar_mae = max(0.0, h - entry)

        bar_mfe_r = bar_mfe / risk
        bar_pct_tp = (bar_mfe / target_dist) * 100.0

        if bar_mfe_r > peak_mfe_r:
            peak_mfe_r = bar_mfe_r
        if bar_pct_tp > peak_pct_tp:
            peak_pct_tp = bar_pct_tp

        # Update SL rules during this bar
        # --- Method 1: Fixed R ---
        if not finished[1]:
            if peak_mfe_r >= 1.6 and state[1] != "trail_10":
                state[1] = "trail_10"
                sl_levels[1] = entry + side * (1.0 * risk)
            elif peak_mfe_r >= 1.3 and state[1] not in ("trail_05", "trail_10"):
                state[1] = "trail_05"
                sl_levels[1] = entry + side * (0.5 * risk)
            elif peak_mfe_r >= 0.8 and state[1] not in ("be", "trail_05", "trail_10"):
                state[1] = "be"
                sl_levels[1] = entry + side * pip_buffer

        # --- Method 2: Percentage-Wise 50/75/90 ---
        if not finished[2]:
            if peak_pct_tp >= 90.0 and state[2] != "trail_75pct":
                state[2] = "trail_75pct"
                sl_levels[2] = entry + side * (0.75 * target_dist)
            elif peak_pct_tp >= 75.0 and state[2] not in ("trail_50pct", "trail_75pct"):
                state[2] = "trail_50pct"
                sl_levels[2] = entry + side * (0.50 * target_dist)
            elif peak_pct_tp >= 50.0 and state[2] not in ("be", "trail_50pct", "trail_75pct"):
                state[2] = "be"
                sl_levels[2] = entry + side * pip_buffer

        # --- Method 3: Percentage-Wise Conservative 60/80 ---
        if not finished[3]:
            if peak_pct_tp >= 80.0 and state[3] != "trail_60pct":
                state[3] = "trail_60pct"
                sl_levels[3] = entry + side * (0.60 * target_dist)
            elif peak_pct_tp >= 60.0 and state[3] not in ("be", "trail_60pct"):
                state[3] = "be"
                sl_levels[3] = entry + side * pip_buffer

        # Check exits for all methods
        for m in range(4):
            if finished[m]:
                continue

            cur_sl = sl_levels[m]
            hit_sl = (side == 1 and l <= cur_sl) or (side == -1 and h >= cur_sl)
            hit_tp = (side == 1 and h >= tp) or (side == -1 and l <= tp)

            if hit_tp and not hit_sl:
                r_results[m] = rr
                finished[m] = True
            elif hit_sl and not hit_tp:
                if state[m] == "be":
                    r_results[m] = 0.0
                elif state[m] == "trail_05":
                    r_results[m] = 0.5
                elif state[m] == "trail_10":
                    r_results[m] = 1.0
                elif state[m] == "trail_50pct":
                    r_results[m] = 0.50 * rr
                elif state[m] == "trail_75pct":
                    r_results[m] = 0.75 * rr
                elif state[m] == "trail_60pct":
                    r_results[m] = 0.60 * rr
                else:
                    r_results[m] = -1.0
                finished[m] = True
            elif hit_tp and hit_sl:
                # Conservative: both touched in same bar -> assume worst (SL)
                if state[m] == "be":
                    r_results[m] = 0.0
                elif "trail" in state[m]:
                    r_results[m] = side * (cur_sl - entry) / risk
                else:
                    r_results[m] = -1.0
                finished[m] = True

        if all(finished):
            break

    # If trade still open at end of data, mark mark-to-market
    for m in range(4):
        if not finished[m]:
            c_last = closes[-1]
            r_results[m] = side * (c_last - entry) / risk

    return {
        "rr": rr,
        "peak_mfe_r": peak_mfe_r,
        "peak_pct_tp": peak_pct_tp,
        "m0_naked": r_results[0],
        "m1_fixed_r": r_results[1],
        "m2_pct_50": r_results[2],
        "m3_pct_60": r_results[3],
    }


def compute_metrics(r_list: list[float]) -> dict:
    if not r_list:
        return {"n": 0, "win_rate": 0.0, "profit_factor": 0.0, "total_r": 0.0, "max_drawdown": 0.0, "avg_r": 0.0}
    arr = np.array(r_list)
    n = len(arr)
    wins = arr[arr > 0.01]
    losses = arr[arr < -0.01]
    bes = arr[np.abs(arr) <= 0.01]

    win_rate = len(wins) / n if n > 0 else 0.0
    tot_win = wins.sum() if len(wins) > 0 else 0.0
    tot_loss = abs(losses.sum()) if len(losses) > 0 else 0.0

    pf = tot_win / tot_loss if tot_loss > 0 else (99.0 if tot_win > 0 else 0.0)
    total_r = arr.sum()
    avg_r = total_r / n if n > 0 else 0.0

    cum = np.cumsum(arr)
    peak = np.maximum.accumulate(cum)
    dd = peak - cum
    max_dd = dd.max() if len(dd) > 0 else 0.0

    return {
        "n": n,
        "win_rate": win_rate,
        "profit_factor": pf,
        "total_r": total_r,
        "max_drawdown": max_dd,
        "avg_r": avg_r,
        "be_count": len(bes),
    }


def main():
    print("=" * 110, flush=True)
    print("INSTITUTIONAL AUDIT: CROSS PAIRS & PERCENTAGE VS FIXED-R TRAILING", flush=True)
    print("=" * 110, flush=True)

    mt5.initialize()

    # Get live spreads for all cross pairs
    spreads_info = {}
    for sym, tf, _ in CROSS_SYMBOLS:
        mt5.symbol_select(sym, True)
        info = mt5.symbol_info(sym)
        tick = mt5.symbol_info_tick(sym)
        pip_size = 0.01 if "JPY" in sym else 0.0001
        spread_pips = (tick.ask - tick.bid) / pip_size if (tick and info) else 2.0
        spreads_info[sym] = round(spread_pips, 1)

    base_params = dict(BAL_PROFILE["params"])
    signaller = STRATEGIES["fvg_retest"].signaller

    pair_results = []

    for sym, tf, bias_htf in CROSS_SYMBOLS:
        print(f"Fetching & analyzing {sym} ({tf})...", end=" ", flush=True)
        df = fetch_symbol_data(sym, tf, count=2000)
        if df is None or len(df) < 300:
            print("[SKIP] Insufficient bars", flush=True)
            continue

        p = dict(base_params)
        p["bias_htf"] = bias_htf
        p["_symbol"] = sym

        try:
            signals = signaller(df, p)
        except Exception as e:
            print(f"[ERR] {sym}: {e}")
            continue

        if not signals:
            continue

        cost = {"spread": spreads_info.get(sym, 2.0) * (0.01 if "JPY" in sym else 0.0001), "slippage": 0.00005}
        slip = cost["slippage"]
        pip_buffer = 0.01 if "JPY" in sym else 0.0001

        df_idx = df.index
        opens = df["open"].to_numpy()

        m0_list, m1_list, m2_list, m3_list = [], [], [], []

        for sig in signals:
            if sig.ts not in df_idx:
                continue
            pos = df_idx.get_loc(sig.ts)
            if pos + 2 >= len(df):
                continue

            entry = opens[pos + 1] + sig.dir * (cost["spread"] + slip) / 2.0
            sl = entry - sig.dir * sig.sl_offset
            risk = abs(entry - sl)
            if risk <= 0:
                continue

            tp = entry + sig.dir * risk * sig.tp_r
            if sig.tp_price is not None:
                tp = sig.tp_price
                rr = sig.dir * (tp - entry) / risk
                if rr < p.get("min_rr", 0.7):
                    tp = entry + sig.dir * risk * p.get("min_rr", 0.7)
                elif rr > p.get("max_rr", 1.4):
                    tp = entry + sig.dir * risk * p.get("max_rr", 1.4)

            actual_rr = abs(tp - entry) / risk
            if actual_rr < p.get("min_rr_post", 0.50):
                continue

            res = simulate_trade_with_trailing(df, pos, sig.dir, entry, sl, tp, actual_rr, slip, pip_buffer)
            if res:
                m0_list.append(res["m0_naked"])
                m1_list.append(res["m1_fixed_r"])
                m2_list.append(res["m2_pct_50"])
                m3_list.append(res["m3_pct_60"])

        met0 = compute_metrics(m0_list)
        met1 = compute_metrics(m1_list)
        met2 = compute_metrics(m2_list)
        met3 = compute_metrics(m3_list)

        pair_results.append({
            "symbol": sym,
            "tf": tf,
            "spread": spreads_info.get(sym, 0.0),
            "n": met0["n"],
            "m0": met0,
            "m1": met1,
            "m2": met2,
            "m3": met3,
        })
        print(f"done ({met0['n']} trades | Naked PF: {met0['profit_factor']:.2f} | FixedR PF: {met1['profit_factor']:.2f} | Pct50 PF: {met2['profit_factor']:.2f})", flush=True)

    mt5.shutdown()

    # Print Comparison Table
    print("\n" + "=" * 125)
    print(f"{'PAIR':<8} {'SPREAD':<7} {'N':<4} | {'NAKED HOLD':<20} | {'FIXED R (0.8R BE)':<22} | {'PCT 50% TP (BE)':<22} | {'RECOMMENDATION'}")
    print(f"{'':<21} | {'WR':<6} {'PF':<5} {'TotR':<7} | {'WR':<6} {'PF':<5} {'TotR':<8} | {'WR':<6} {'PF':<5} {'TotR':<8} |")
    print("-" * 125)

    for r in pair_results:
        m0 = r["m0"]
        m1 = r["m1"]
        m2 = r["m2"]
        m3 = r["m3"]

        # Classification
        best_pf = max(m0["profit_factor"], m1["profit_factor"], m2["profit_factor"])
        best_tot = max(m0["total_r"], m1["total_r"], m2["total_r"])

        if best_pf >= 1.40 and best_tot > 15.0 and r["spread"] <= 2.2:
            rec = "[TOP TIER] Strong Edge"
        elif best_pf >= 1.25 and best_tot > 10.0 and r["spread"] <= 2.5:
            rec = "[VIABLE] Good Performer"
        elif best_pf >= 1.10 and best_tot > 0.0:
            rec = "[MARGINAL] Low Edge"
        else:
            rec = "[AVOID] Poor / Wide Spread"

        print(f"{r['symbol']:<8} {r['spread']:>4.1f}p  {r['n']:<4} | "
              f"{m0['win_rate']:<6.1%} {m0['profit_factor']:<5.2f} {m0['total_r']:+6.1f}R | "
              f"{m1['win_rate']:<6.1%} {m1['profit_factor']:<5.2f} {m1['total_r']:+7.1f}R | "
              f"{m2['win_rate']:<6.1%} {m2['profit_factor']:<5.2f} {m2['total_r']:+7.1f}R | "
              f"{rec}")

    print("=" * 125)

    # 4. Also backtest the 53 real delivered setups from gha_state/state.json
    print("\n" + "=" * 80)
    print("BACKTEST ON REAL DELIVERED BOT SETUPS (gha_state/state.json)")
    print("=" * 80)

    state_file = BASE_DIR / "gha_state" / "state.json"
    if state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))
        history = state.get("history", [])
        outcomes = state.get("outcomes", {})

        # Compare real outcomes under Fixed R vs Percentage-Wise
        # We check peak MFE recorded in state
        real_fixed_r_pnl = []
        real_pct_pnl = []
        real_naked_pnl = []

        for h in history:
            ref = str(h.get("ref"))
            sig_ts = h.get("ts")
            key = f"{h.get('symbol')}:{sig_ts}"
            o = outcomes.get(key)
            if not o or o.get("hit") not in ("tp", "sl"):
                continue

            hit = o.get("hit")
            rr = float(h.get("rr", 1.0))
            peak_mfe = float(o.get("peak_mfe", rr if hit == "tp" else 0.3))

            # 1. Naked
            r_naked = rr if hit == "tp" else -1.0
            real_naked_pnl.append(r_naked)

            # 2. Fixed R
            if hit == "tp":
                r_fixed = rr
            elif peak_mfe >= 1.6:
                r_fixed = 1.0
            elif peak_mfe >= 1.3:
                r_fixed = 0.5
            elif peak_mfe >= 0.8:
                r_fixed = 0.0
            else:
                r_fixed = -1.0
            real_fixed_r_pnl.append(r_fixed)

            # 3. Percentage-Wise (50% TP -> BE, 75% -> 50% lock, 90% -> 75% lock)
            pct_reached = (peak_mfe / rr * 100.0) if rr > 0 else 0.0
            if hit == "tp":
                r_pct = rr
            elif pct_reached >= 90.0:
                r_pct = 0.75 * rr
            elif pct_reached >= 75.0:
                r_pct = 0.50 * rr
            elif pct_reached >= 50.0:
                r_pct = 0.0
            else:
                r_pct = -1.0
            real_pct_pnl.append(r_pct)

        met_del_naked = compute_metrics(real_naked_pnl)
        met_del_fixed = compute_metrics(real_fixed_r_pnl)
        met_del_pct = compute_metrics(real_pct_pnl)

        print(f"Sample: {met_del_naked['n']} Real Bot Delivered Setups")
        print(f"1. Naked Hold:            WR: {met_del_naked['win_rate']:.1%} | PF: {met_del_naked['profit_factor']:.2f} | Tot R: {met_del_naked['total_r']:+.2f}R | MaxDD: {met_del_naked['max_drawdown']:.2f}R")
        print(f"2. Fixed R (0.8R BE):     WR: {met_del_fixed['win_rate']:.1%} | PF: {met_del_fixed['profit_factor']:.2f} | Tot R: {met_del_fixed['total_r']:+.2f}R | MaxDD: {met_del_fixed['max_drawdown']:.2f}R (BE Avoided Losses: {met_del_fixed['be_count']})")
        print(f"3. Pct-Wise (50% TP BE):  WR: {met_del_pct['win_rate']:.1%} | PF: {met_del_pct['profit_factor']:.2f} | Tot R: {met_del_pct['total_r']:+.2f}R | MaxDD: {met_del_pct['max_drawdown']:.2f}R (BE Avoided Losses: {met_del_pct['be_count']})")
        print("=" * 80)


if __name__ == "__main__":
    main()

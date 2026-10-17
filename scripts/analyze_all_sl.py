#!/usr/bin/env python3
"""Deep Forensic Multi-Timeframe Analysis of all Stop Loss (SL) Setups.

Analyzes:
1. All SL setups across the entire project history + this week.
2. Multi-timeframe trend alignment (M15, H1, H4, D1).
3. Post-SL price behavior (Did price sweep SL then fly to TP? Or continue in SL dir?).
4. Inversion FVG (IFVG) behavior (Did failed FVG flip into S/R for opposite trade?).
5. Session, killzone, and symbol failure clustering.
6. Stop distance and structural placement analysis.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
import MetaTrader5 as mt5
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("analyze_sl")

BASE_DIR = Path("C:/goldfx-agent")
STATE_FILE = BASE_DIR / "gha_state" / "state.json"

def init_mt5():
    if not mt5.initialize():
        log.error("Failed to initialize MT5: %s", mt5.last_error())
        return False
    return True

def get_rates(symbol: str, tf, count: int = 5000):
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    return df

def get_point(symbol: str) -> float:
    info = mt5.symbol_info(symbol)
    if info:
        return info.point
    if "JPY" in symbol or symbol == "XAUUSD":
        return 0.01
    if any(idx in symbol for idx in ["100", "500", "30"]):
        return 0.1
    return 0.00001

def get_pip_scale(symbol: str) -> float:
    if "JPY" in symbol:
        return 0.01
    if symbol == "XAUUSD":
        return 0.1
    if any(idx in symbol for idx in ["100", "500", "30"]):
        return 1.0
    return 0.0001

def main():
    if not init_mt5():
        return

    with open(STATE_FILE, "r") as f:
        st = json.load(f)

    history_list = st.get("history", [])
    history_map = {f"{h['symbol']}:{h['ts']}": h for h in history_list}
    outcomes = st.get("outcomes", {})

    log.info("Loaded %d history setups, %d outcomes", len(history_map), len(outcomes))

    # Also check MT5 deals/orders to ensure we catch all executed trades
    deals = mt5.history_deals_get(datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 5, tzinfo=timezone.utc))

    # Cache candle data per symbol and timeframe
    all_symbols = set([h["symbol"] for h in history_list] + [k.split(":")[0] for k in outcomes.keys()])
    candles = {}
    for s in all_symbols:
        log.info("Loading candle history for %s...", s)
        candles[s] = {
            "M5": get_rates(s, mt5.TIMEFRAME_M5, 6000),
            "M15": get_rates(s, mt5.TIMEFRAME_M15, 4000),
            "M30": get_rates(s, mt5.TIMEFRAME_M30, 3000),
            "H1": get_rates(s, mt5.TIMEFRAME_H1, 2000),
            "H4": get_rates(s, mt5.TIMEFRAME_H4, 1500),
            "D1": get_rates(s, mt5.TIMEFRAME_D1, 500),
        }

    # Extract all SL setups
    sl_setups = []
    tp_setups = []

    for key, oc in outcomes.items():
        hit = oc.get("hit")
        sym, ts_str = key.split(":", 1)
        h = history_map.get(key)
        
        # If not in history_map, reconstruct what we can
        entry_ts = pd.to_datetime(ts_str, utc=True)
        outcome_ts = pd.to_datetime(oc.get("when"), utc=True) if oc.get("when") else None
        
        setup_data = {
            "key": key,
            "symbol": sym,
            "entry_ts": entry_ts,
            "outcome_ts": outcome_ts,
            "hit": hit,
            "outcome_price": oc.get("price"),
            "ref": h.get("ref") if h else None,
            "direction": h.get("dir") if h else None,
            "entry": h.get("entry") if h else None,
            "sl": h.get("sl") if h else None,
            "tp": h.get("tp") if h else None,
            "rr": h.get("rr") if h else None,
            "tf": h.get("tf") if h else None,
            "strategy_type": h.get("strategy_type", "fvg_retest") if h else "unknown",
            "is_this_week": entry_ts >= pd.to_datetime("2026-09-28 00:00:00", utc=True),
        }

        # If direction or prices are missing from history, infer from M15/M30 candle at entry_ts
        if setup_data["direction"] is None or setup_data["entry"] is None:
            c_m15 = candles.get(sym, {}).get("M15")
            if c_m15 is not None and entry_ts in c_m15.index:
                row = c_m15.loc[entry_ts]
                setup_data["entry"] = row["close"]
                # Infer direction from outcome price vs entry
                if setup_data["outcome_price"] and setup_data["entry"]:
                    if hit == "sl":
                        setup_data["direction"] = "LONG" if setup_data["outcome_price"] < setup_data["entry"] else "SHORT"
                    else:
                        setup_data["direction"] = "LONG" if setup_data["outcome_price"] > setup_data["entry"] else "SHORT"

        if hit == "sl":
            sl_setups.append(setup_data)
        elif hit == "tp":
            tp_setups.append(setup_data)

    log.info("Total SL setups identified: %d (This week: %d)", len(sl_setups), sum(1 for s in sl_setups if s["is_this_week"]))
    log.info("Total TP setups identified: %d (This week: %d)", len(tp_setups), sum(1 for s in tp_setups if s["is_this_week"]))

    # Detailed Multi-Timeframe Forensic Analysis on each SL Setup
    sl_analysis_results = []

    for s in sl_setups:
        sym = s["symbol"]
        entry_ts = s["entry_ts"]
        dir_str = s["direction"]
        entry_price = s["entry"]
        sl_price = s["sl"]
        tp_price = s["tp"]
        outcome_ts = s["outcome_ts"]
        pip_scale = get_pip_scale(sym)

        if not dir_str or entry_price is None or sl_price is None or tp_price is None:
            continue

        is_buy = dir_str.upper() in ["BUY", "LONG"]
        direction = 1 if is_buy else -1
        risk_dist = abs(entry_price - sl_price)
        target_dist = abs(tp_price - entry_price)
        risk_pips = risk_dist / pip_scale if pip_scale else 0

        # --- 1. Higher Timeframe Alignment Analysis (H1, H4, D1) ---
        c_h1 = candles.get(sym, {}).get("H1")
        c_h4 = candles.get(sym, {}).get("H4")
        c_d1 = candles.get(sym, {}).get("D1")
        c_m15 = candles.get(sym, {}).get("M15")

        h1_trend = "UNKNOWN"
        h1_aligned = False
        h4_trend = "UNKNOWN"
        h4_aligned = False
        d1_trend = "UNKNOWN"
        d1_aligned = False

        if c_h1 is not None and not c_h1.empty:
            past_h1 = c_h1.loc[c_h1.index <= entry_ts]
            if len(past_h1) >= 50:
                ema20 = past_h1["close"].ewm(span=20).mean().iloc[-1]
                ema50 = past_h1["close"].ewm(span=50).mean().iloc[-1]
                close_h1 = past_h1["close"].iloc[-1]
                h1_bull = (close_h1 > ema50 and ema20 > ema50)
                h1_bear = (close_h1 < ema50 and ema20 < ema50)
                h1_trend = "BULL" if h1_bull else ("BEAR" if h1_bear else "CHOP")
                h1_aligned = (h1_trend == "BULL" and is_buy) or (h1_trend == "BEAR" and not is_buy)

        if c_h4 is not None and not c_h4.empty:
            past_h4 = c_h4.loc[c_h4.index <= entry_ts]
            if len(past_h4) >= 50:
                ema20 = past_h4["close"].ewm(span=20).mean().iloc[-1]
                ema50 = past_h4["close"].ewm(span=50).mean().iloc[-1]
                close_h4 = past_h4["close"].iloc[-1]
                h4_bull = (close_h4 > ema50 and ema20 > ema50)
                h4_bear = (close_h4 < ema50 and ema20 < ema50)
                h4_trend = "BULL" if h4_bull else ("BEAR" if h4_bear else "CHOP")
                h4_aligned = (h4_trend == "BULL" and is_buy) or (h4_trend == "BEAR" and not is_buy)

        if c_d1 is not None and not c_d1.empty:
            past_d1 = c_d1.loc[c_d1.index <= entry_ts]
            if len(past_d1) >= 20:
                ema20 = past_d1["close"].ewm(span=20).mean().iloc[-1]
                close_d1 = past_d1["close"].iloc[-1]
                d1_trend = "BULL" if close_d1 > ema20 else "BEAR"
                d1_aligned = (d1_trend == "BULL" and is_buy) or (d1_trend == "BEAR" and not is_buy)

        # --- 2. Session / Hour Analysis ---
        hour_utc = entry_ts.hour
        if 0 <= hour_utc < 7:
            session = "Asian"
        elif 7 <= hour_utc < 8:
            session = "London_Open_Whipsaw" # 07:00-08:00 UTC
        elif 8 <= hour_utc < 12:
            session = "London_Morning"
        elif 12 <= hour_utc < 16:
            session = "London_NY_Overlap" # Peak institutional volume
        elif 16 <= hour_utc < 21:
            session = "NY_Afternoon"
        else:
            session = "Late_Rollover"

        # --- 3. Post-SL Price Action Classification ---
        # Analyze candles after outcome_ts (or after entry_ts if outcome_ts missing)
        eval_start_ts = outcome_ts if outcome_ts else entry_ts
        post_behavior = "UNKNOWN"
        max_overshoot_r = 0.0
        max_favorable_after_sl_r = 0.0
        hit_original_tp_after_sl = False
        retested_fvg_as_inversion = False

        if c_m15 is not None and not c_m15.empty:
            post_bars = c_m15.loc[c_m15.index >= eval_start_ts].head(96) # Up to 24 hours of M15 bars after SL
            if len(post_bars) > 0 and risk_dist > 0:
                if is_buy:
                    # SL was below entry
                    min_price = post_bars["low"].min()
                    max_price = post_bars["high"].max()
                    # How deep did it go below SL?
                    sl_overshoot = max(0.0, sl_price - min_price)
                    max_overshoot_r = sl_overshoot / risk_dist
                    # Did price then reverse back up above entry / into TP?
                    favorable_dist = max(0.0, max_price - entry_price)
                    max_favorable_after_sl_r = favorable_dist / risk_dist
                    if max_price >= tp_price:
                        hit_original_tp_after_sl = True
                else:
                    # SL was above entry
                    max_price = post_bars["high"].max()
                    min_price = post_bars["low"].min()
                    sl_overshoot = max(0.0, max_price - sl_price)
                    max_overshoot_r = sl_overshoot / risk_dist
                    favorable_dist = max(0.0, entry_price - min_price)
                    max_favorable_after_sl_r = favorable_dist / risk_dist
                    if min_price <= tp_price:
                        hit_original_tp_after_sl = True

                # Classification
                if hit_original_tp_after_sl:
                    if max_overshoot_r <= 0.6: # Stopped out by shallow sweep, then hit TP
                        post_behavior = "LIQUIDITY_SWEEP_TO_TP" # Turtle Soup / Inducement
                    else:
                        post_behavior = "DEEP_SWEEP_TO_TP"
                elif max_favorable_after_sl_r >= 1.0:
                    post_behavior = "REVERSED_TO_1R_PLUS" # Rebound after stop
                elif max_overshoot_r >= 1.5:
                    post_behavior = "STRONG_CONTINUATION_OPPOSITE" # Trend ran against setup
                else:
                    post_behavior = "CHOP_OR_STAGNATION"

                # Check Inversion FVG behavior:
                # If it was a BUY setup, FVG zone was between entry and SL.
                # When price broke below SL, did it pull back up to retest the broken FVG zone from underneath and drop?
                if not is_buy: # SELL setup failed (broke above SL). Did price retest broken zone from above and rally?
                    retest_bars = post_bars.loc[post_bars["low"] <= sl_price]
                    if len(retest_bars) > 0 and post_bars["high"].max() > sl_price + (1.0 * risk_dist):
                        retested_fvg_as_inversion = True
                else: # BUY setup failed (broke below SL). Did price retest broken zone from below and plunge?
                    retest_bars = post_bars.loc[post_bars["high"] >= sl_price]
                    if len(retest_bars) > 0 and post_bars["low"].min() < sl_price - (1.0 * risk_dist):
                        retested_fvg_as_inversion = True

        sl_analysis_results.append({
            "key": s["key"],
            "symbol": sym,
            "is_this_week": s["is_this_week"],
            "ref": s["ref"],
            "direction": dir_str,
            "entry_ts": str(entry_ts),
            "entry": entry_price,
            "sl": sl_price,
            "tp": tp_price,
            "rr": s["rr"],
            "risk_pips": round(risk_pips, 1),
            "session": session,
            "hour_utc": hour_utc,
            "h1_trend": h1_trend,
            "h1_aligned": h1_aligned,
            "h4_trend": h4_trend,
            "h4_aligned": h4_aligned,
            "d1_trend": d1_trend,
            "d1_aligned": d1_aligned,
            "htf_counter_trend": (not h1_aligned and not h4_aligned),
            "post_behavior": post_behavior,
            "max_overshoot_r": round(max_overshoot_r, 2),
            "max_favorable_after_sl_r": round(max_favorable_after_sl_r, 2),
            "hit_original_tp_after_sl": hit_original_tp_after_sl,
            "retested_fvg_as_inversion": retested_fvg_as_inversion,
        })

    df_sl = pd.DataFrame(sl_analysis_results)
    
    # Save raw json output
    output_path = BASE_DIR / "data" / "sl_forensic_results.json"
    output_path.parent.mkdir(exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(sl_analysis_results, f, indent=2)
    log.info("Saved forensic analysis to %s", output_path)

    # --- Print Aggregate Statistics ---
    print("\n" + "="*80)
    print("MASTER FORENSIC AUDIT OF ALL STOP LOSS (SL) SETUPS")
    print("="*80)

    for scope_name, df_subset in [("THIS WEEK (Sep 28 - Oct 2)", df_sl[df_sl["is_this_week"]]), ("ALL-TIME (Project Inception)", df_sl)]:
        print(f"\n>>> SCOPE: {scope_name} (Total SLs: {len(df_subset)})")
        print("-" * 60)

        # 1. Multi-Timeframe Alignment
        h1_aligned_pct = (df_subset["h1_aligned"].sum() / len(df_subset) * 100) if len(df_subset) else 0
        h4_aligned_pct = (df_subset["h4_aligned"].sum() / len(df_subset) * 100) if len(df_subset) else 0
        counter_htf_pct = (df_subset["htf_counter_trend"].sum() / len(df_subset) * 100) if len(df_subset) else 0

        print("\n1. HIGHER-TIMEFRAME (HTF) TREND ALIGNMENT:")
        print(f"  - Aligned with H1 Trend:  {h1_aligned_pct:.1f}% ({df_subset['h1_aligned'].sum()}/{len(df_subset)})")
        print(f"  - Aligned with H4 Trend:  {h4_aligned_pct:.1f}% ({df_subset['h4_aligned'].sum()}/{len(df_subset)})")
        print(f"  - COUNTER-TREND to BOTH H1 & H4: {counter_htf_pct:.1f}% ({df_subset['htf_counter_trend'].sum()}/{len(df_subset)})")

        # 2. Post-SL Price Behavior
        print("\n2. POST-SL BEHAVIOR (WHAT HAPPENED AFTER STOPPING OUT?):")
        behavior_counts = df_subset["post_behavior"].value_counts()
        for beh, count in behavior_counts.items():
            pct = count / len(df_subset) * 100
            print(f"  - {beh:30s}: {count:3d} ({pct:5.1f}%)")

        swept_and_hit_tp = df_subset["hit_original_tp_after_sl"].sum()
        swept_pct = swept_and_hit_tp / len(df_subset) * 100
        print(f"\n  ★ LIQUIDITY SWEEP DISCOVERY: {swept_and_hit_tp} out of {len(df_subset)} ({swept_pct:.1f}%) "
              f"WENT ON TO HIT FULL ORIGINAL TP AFTER TRIGGERING SL!")

        # 3. Inversion FVG (IFVG) Behavior
        ifvg_count = df_subset["retested_fvg_as_inversion"].sum()
        ifvg_pct = ifvg_count / len(df_subset) * 100
        print(f"  ★ INVERSION FVG FLIP: {ifvg_count} out of {len(df_subset)} ({ifvg_pct:.1f}%) "
              f"retested the broken zone as S/R and expanded $\\ge 1.0R$ in the opposite direction!")

        # 4. Session Breakdown
        print("\n3. SESSION / TIME-OF-DAY CLUSTERING:")
        session_counts = df_subset["session"].value_counts()
        for sess, count in session_counts.items():
            pct = count / len(df_subset) * 100
            print(f"  - {sess:25s}: {count:3d} ({pct:5.1f}%)")

        # 5. Symbol Breakdown
        print("\n4. SYMBOL BREAKDOWN:")
        sym_counts = df_subset["symbol"].value_counts()
        for sym, count in sym_counts.items():
            pct = count / len(df_subset) * 100
            print(f"  - {sym:12s}: {count:3d} ({pct:5.1f}%)")

        # 6. Stop Distance (Pips)
        print("\n5. STOP LOSS DISTANCE (AVERAGE PIPS/POINTS):")
        for sym in df_subset["symbol"].unique():
            sym_df = df_subset[df_subset["symbol"] == sym]
            avg_pips = sym_df["risk_pips"].mean()
            print(f"  - {sym:12s}: Avg SL Distance = {avg_pips:6.1f} pips/pts (n={len(sym_df)})")

    mt5.shutdown()

if __name__ == "__main__":
    main()

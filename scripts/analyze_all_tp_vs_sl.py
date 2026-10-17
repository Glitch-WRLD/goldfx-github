#!/usr/bin/env python3
"""Master Comparative Forensic Analysis: ALL Take Profit (TP) vs ALL Stop Loss (SL) Setups.

Analyzes all 218 historical setups (111 TPs vs 107 SLs) across:
1. Multi-timeframe trend alignment (H1, H4, D1).
2. Session & time-of-day win rates.
3. Maximum Adverse Excursion (MAE) and drawdown dynamics on winning trades.
4. Stop Loss size (pips), Planned RR, and duration to target.
5. Symbol-by-symbol edge and expectancy.
6. Identifying true predictive filters (that boost win rate and profit without killing net R).
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
import MetaTrader5 as mt5
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tp_vs_sl")

BASE_DIR = Path("C:/goldfx-agent")
STATE_FILE = BASE_DIR / "gha_state" / "state.json"

def init_mt5():
    if not mt5.initialize():
        log.error("Failed to initialize MT5: %s", mt5.last_error())
        return False
    return True

def get_rates(symbol: str, tf, count: int = 4000):
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    return df

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

    log.info("Loaded %d history entries, %d recorded outcomes", len(history_map), len(outcomes))

    all_symbols = sorted(list(set([h["symbol"] for h in history_list] + [k.split(":")[0] for k in outcomes.keys()])))

    # Load candles for all symbols
    candles = {}
    for s in all_symbols:
        log.info("Loading candles for %s...", s)
        candles[s] = {
            "M15": get_rates(s, mt5.TIMEFRAME_M15, 3500),
            "H1":  get_rates(s, mt5.TIMEFRAME_H1, 2000),
            "H4":  get_rates(s, mt5.TIMEFRAME_H4, 1500),
            "D1":  get_rates(s, mt5.TIMEFRAME_D1, 500),
        }

    records = []
    skipped = 0

    for key, oc in outcomes.items():
        hit = oc.get("hit")
        if hit not in ["tp", "sl"]:
            continue

        sym, ts_str = key.split(":", 1)
        entry_ts = pd.to_datetime(ts_str, utc=True)
        outcome_ts = pd.to_datetime(oc.get("when"), utc=True) if oc.get("when") else None
        h = history_map.get(key)

        dir_str = h.get("dir") if h else None
        entry_price = h.get("entry") if h else None
        sl_price = h.get("sl") if h else None
        tp_price = h.get("tp") if h else None
        rr = h.get("rr") if h else None
        strategy_type = h.get("strategy_type", "fvg_retest") if h else "unknown"

        # Reconstruct missing fields from M15 candle if not in history
        c_m15 = candles.get(sym, {}).get("M15")
        if (dir_str is None or entry_price is None) and c_m15 is not None:
            if entry_ts in c_m15.index:
                row = c_m15.loc[entry_ts]
                entry_price = row["close"]
                oc_price = oc.get("price")
                if oc_price and entry_price:
                    if hit == "tp":
                        dir_str = "LONG" if oc_price > entry_price else "SHORT"
                    else:
                        dir_str = "LONG" if oc_price < entry_price else "SHORT"

        if dir_str is None or entry_price is None:
            skipped += 1
            continue

        is_buy = dir_str.upper() in ["BUY", "LONG"]
        direction = 1 if is_buy else -1
        pip_scale = get_pip_scale(sym)

        # Risk & Reward distance
        risk_dist = abs(entry_price - sl_price) if sl_price else None
        target_dist = abs(tp_price - entry_price) if tp_price else None
        risk_pips = (risk_dist / pip_scale) if (risk_dist and pip_scale) else None
        reward_pips = (target_dist / pip_scale) if (target_dist and pip_scale) else None
        if rr is None and risk_dist and target_dist and risk_dist > 0:
            rr = target_dist / risk_dist

        # Duration in hours
        duration_hours = None
        if outcome_ts:
            duration_hours = (outcome_ts - entry_ts).total_seconds() / 3600.0

        # --- 1. Multi-Timeframe Trend Confluence (H1, H4, D1) ---
        c_h1 = candles.get(sym, {}).get("H1")
        c_h4 = candles.get(sym, {}).get("H4")
        c_d1 = candles.get(sym, {}).get("D1")

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
                h1_trend = "BULL" if (close_h1 > ema50 and ema20 > ema50) else ("BEAR" if (close_h1 < ema50 and ema20 < ema50) else "CHOP")
                h1_aligned = (h1_trend == "BULL" and is_buy) or (h1_trend == "BEAR" and not is_buy)

        if c_h4 is not None and not c_h4.empty:
            past_h4 = c_h4.loc[c_h4.index <= entry_ts]
            if len(past_h4) >= 50:
                ema20 = past_h4["close"].ewm(span=20).mean().iloc[-1]
                ema50 = past_h4["close"].ewm(span=50).mean().iloc[-1]
                close_h4 = past_h4["close"].iloc[-1]
                h4_trend = "BULL" if (close_h4 > ema50 and ema20 > ema50) else ("BEAR" if (close_h4 < ema50 and ema20 < ema50) else "CHOP")
                h4_aligned = (h4_trend == "BULL" and is_buy) or (h4_trend == "BEAR" and not is_buy)

        if c_d1 is not None and not c_d1.empty:
            past_d1 = c_d1.loc[c_d1.index <= entry_ts]
            if len(past_d1) >= 20:
                ema20 = past_d1["close"].ewm(span=20).mean().iloc[-1]
                close_d1 = past_d1["close"].iloc[-1]
                d1_trend = "BULL" if close_d1 > ema20 else "BEAR"
                d1_aligned = (d1_trend == "BULL" and is_buy) or (d1_trend == "BEAR" and not is_buy)

        # --- 2. Session & Timing ---
        hour_utc = entry_ts.hour
        day_of_week = entry_ts.strftime("%A")
        if 0 <= hour_utc < 7:
            session = "Asian"
        elif 7 <= hour_utc < 8:
            session = "London_Open_Whipsaw"
        elif 8 <= hour_utc < 12:
            session = "London_Morning"
        elif 12 <= hour_utc < 16:
            session = "London_NY_Overlap"
        elif 16 <= hour_utc < 21:
            session = "NY_Afternoon"
        else:
            session = "Late_Rollover"

        # --- 3. Drawdown Dynamics (MAE during trade) ---
        # How deep did price pull back against entry before hitting outcome?
        mae_r = 0.0
        if c_m15 is not None and not c_m15.empty and outcome_ts and risk_dist and risk_dist > 0:
            trade_bars = c_m15.loc[(c_m15.index >= entry_ts) & (c_m15.index <= outcome_ts)]
            if len(trade_bars) > 0:
                if is_buy:
                    worst_price = trade_bars["low"].min()
                    max_adverse_dist = max(0.0, entry_price - worst_price)
                else:
                    worst_price = trade_bars["high"].max()
                    max_adverse_dist = max(0.0, worst_price - entry_price)
                mae_r = max_adverse_dist / risk_dist

        records.append({
            "key": key,
            "symbol": sym,
            "is_this_week": entry_ts >= pd.to_datetime("2026-09-28 00:00:00", utc=True),
            "hit": hit,
            "is_win": 1 if hit == "tp" else 0,
            "direction": dir_str,
            "entry_ts": str(entry_ts),
            "outcome_ts": str(outcome_ts) if outcome_ts else None,
            "duration_hours": round(duration_hours, 1) if duration_hours is not None else None,
            "risk_pips": round(risk_pips, 1) if risk_pips is not None else None,
            "reward_pips": round(reward_pips, 1) if reward_pips is not None else None,
            "planned_rr": round(rr, 2) if rr is not None else None,
            "strategy_type": strategy_type,
            "session": session,
            "hour_utc": hour_utc,
            "day_of_week": day_of_week,
            "h1_trend": h1_trend,
            "h1_aligned": h1_aligned,
            "h4_trend": h4_trend,
            "h4_aligned": h4_aligned,
            "d1_trend": d1_trend,
            "d1_aligned": d1_aligned,
            "both_h1_h4_aligned": (h1_aligned and h4_aligned),
            "all_htf_aligned": (h1_aligned and h4_aligned and d1_aligned),
            "counter_all_htf": (not h1_aligned and not h4_aligned),
            "mae_r": round(mae_r, 2),
        })

    df = pd.DataFrame(records)
    out_file = BASE_DIR / "data" / "tp_vs_sl_master.json"
    with open(out_file, "w") as f:
        json.dump(records, f, indent=2)
    log.info("Saved %d analyzed setups to %s (skipped %d)", len(df), out_file, skipped)

    # --- Comparative Reporting ---
    print("\n" + "=" * 90)
    print("MASTER COMPARATIVE AUDIT: TAKE PROFIT (TP) VS STOP LOSS (SL) SETUPS")
    print("=" * 90)

    for scope_name, sub in [("THIS WEEK (Sep 28 - Oct 2, 2026)", df[df["is_this_week"]]), ("ALL-TIME (Sep 14 - Oct 2, 2026)", df)]:
        n_total = len(sub)
        n_tp = (sub["hit"] == "tp").sum()
        n_sl = (sub["hit"] == "sl").sum()
        baseline_wr = (n_tp / n_total * 100) if n_total else 0

        print(f"\n{'#' * 90}")
        print(f"SCOPE: {scope_name}  |  Total Setups: {n_total} (TP: {n_tp}, SL: {n_sl})  |  Baseline WR: {baseline_wr:.1f}%")
        print(f"{'#' * 90}")

        # 1. Multi-Timeframe Alignment: Win Rate by Alignment Category
        print("\n[1. MULTI-TIMEFRAME (HTF) CONFLUENCE VS WIN RATE]")
        print(f"{'HTF Category':<35} {'Total':<8} {'TPs':<6} {'SLs':<6} {'Win Rate':<10} {'Edge vs Base':<12}")
        print("-" * 77)

        categories = [
            ("All HTF Aligned (H1 + H4 + D1)", sub["all_htf_aligned"]),
            ("Pro H4 + H1 Trend", sub["both_h1_h4_aligned"]),
            ("Pro H4 Trend Only", sub["h4_aligned"]),
            ("Pro H1 Trend Only", sub["h1_aligned"]),
            ("Pro D1 Trend Only", sub["d1_aligned"]),
            ("Counter to Both H1 & H4", sub["counter_all_htf"]),
        ]

        for cat_name, mask in categories:
            cnt = mask.sum()
            if cnt == 0:
                continue
            tp_cnt = sub[mask & (sub["hit"] == "tp")].shape[0]
            sl_cnt = sub[mask & (sub["hit"] == "sl")].shape[0]
            wr = (tp_cnt / cnt * 100)
            edge = wr - baseline_wr
            print(f"{cat_name:<35} {cnt:<8} {tp_cnt:<6} {sl_cnt:<6} {wr:5.1f}%     {edge:+5.1f}%")

        # 2. Session / Time-of-Day Win Rates
        print("\n[2. SESSION / TIME-OF-DAY WIN RATE & PROFILE]")
        print(f"{'Session Bucket':<25} {'Total':<8} {'TPs':<6} {'SLs':<6} {'Win Rate':<10} {'Edge vs Base':<12} {'% of All TPs':<14} {'% of All SLs':<14}")
        print("-" * 95)
        for sess in ["Asian", "London_Open_Whipsaw", "London_Morning", "London_NY_Overlap", "NY_Afternoon", "Late_Rollover"]:
            mask = (sub["session"] == sess)
            cnt = mask.sum()
            if cnt == 0:
                continue
            tp_cnt = sub[mask & (sub["hit"] == "tp")].shape[0]
            sl_cnt = sub[mask & (sub["hit"] == "sl")].shape[0]
            wr = (tp_cnt / cnt * 100)
            edge = wr - baseline_wr
            tp_share = (tp_cnt / n_tp * 100) if n_tp else 0
            sl_share = (sl_cnt / n_sl * 100) if n_sl else 0
            print(f"{sess:<25} {cnt:<8} {tp_cnt:<6} {sl_cnt:<6} {wr:5.1f}%     {edge:+5.1f}%     {tp_share:5.1f}%        {sl_share:5.1f}%")

        # 3. Symbol by Symbol Performance
        print("\n[3. SYMBOL-BY-SYMBOL PERFORMANCE & WIN RATES]")
        print(f"{'Symbol':<14} {'Total':<8} {'TPs':<6} {'SLs':<6} {'Win Rate':<10} {'Edge vs Base':<12} {'Avg Win Dur(h)':<16} {'Avg Loss Dur(h)':<16}")
        print("-" * 88)
        for sym in sorted(sub["symbol"].unique()):
            sym_df = sub[sub["symbol"] == sym]
            cnt = len(sym_df)
            tp_cnt = (sym_df["hit"] == "tp").sum()
            sl_cnt = (sym_df["hit"] == "sl").sum()
            wr = (tp_cnt / cnt * 100)
            edge = wr - baseline_wr
            avg_win_dur = sym_df[sym_df["hit"] == "tp"]["duration_hours"].mean()
            avg_loss_dur = sym_df[sym_df["hit"] == "sl"]["duration_hours"].mean()
            w_str = f"{avg_win_dur:.1f}h" if pd.notnull(avg_win_dur) else "N/A"
            l_str = f"{avg_loss_dur:.1f}h" if pd.notnull(avg_loss_dur) else "N/A"
            print(f"{sym:<14} {cnt:<8} {tp_cnt:<6} {sl_cnt:<6} {wr:5.1f}%     {edge:+5.1f}%     {w_str:<16} {l_str:<16}")

        # 4. MAE (Drawdown) Comparison on Winning Trades
        tp_sub = sub[sub["hit"] == "tp"]
        if len(tp_sub) > 0 and tp_sub["mae_r"].notnull().sum() > 0:
            print("\n[4. DRAWDOWN DYNAMICS: HOW MUCH DID WINNERS PULL BACK BEFORE TP?]")
            mae = tp_sub["mae_r"]
            print(f"  * Median Drawdown on Winning Trades: {mae.median():.2f} R")
            print(f"  * Mean Drawdown on Winning Trades:   {mae.mean():.2f} R")
            print(f"  * Winners with MAE <= 0.25R (Immediate impulse to TP): {(mae <= 0.25).sum()} / {len(tp_sub)} ({(mae <= 0.25).sum()/len(tp_sub)*100:.1f}%)")
            print(f"  * Winners with MAE <= 0.50R (Shallow pullback before TP): {(mae <= 0.50).sum()} / {len(tp_sub)} ({(mae <= 0.50).sum()/len(tp_sub)*100:.1f}%)")
            print(f"  * Winners with MAE >  0.75R (Deep sweat before TP):      {(mae > 0.75).sum()} / {len(tp_sub)} ({(mae > 0.75).sum()/len(tp_sub)*100:.1f}%)")

        # 5. Day of Week Breakdown
        print("\n[5. DAY-OF-WEEK BREAKDOWN]")
        print(f"{'Day':<12} {'Total':<8} {'TPs':<6} {'SLs':<6} {'Win Rate':<10} {'Edge vs Base':<12}")
        print("-" * 55)
        for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]:
            mask = (sub["day_of_week"] == day)
            cnt = mask.sum()
            if cnt == 0:
                continue
            tp_cnt = sub[mask & (sub["hit"] == "tp")].shape[0]
            sl_cnt = sub[mask & (sub["hit"] == "sl")].shape[0]
            wr = (tp_cnt / cnt * 100)
            edge = wr - baseline_wr
            print(f"{day:<12} {cnt:<8} {tp_cnt:<6} {sl_cnt:<6} {wr:5.1f}%     {edge:+5.1f}%")

    mt5.shutdown()

if __name__ == "__main__":
    main()

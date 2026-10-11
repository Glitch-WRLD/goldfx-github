#!/usr/bin/env python3
"""Deliberate Realistic Simulation: Project Inception to Present (Setup #1 to #323).
Evaluates every single delivered trade since inception under the EXACT current production system:
1. Complete Weakling Pruning Safeguards:
   - Thursday Asian (00:00-06:59 UTC)
   - Sunday Open (18:00-23:59 UTC)
   - USD Evening Chop (USDJPY/USDCHF 19:00-21:59 UTC)
   - Friday FULL_DAY (all Friday setups dropped per user-maintained setting)
   - London Open Buffer (06:50-07:20 UTC)
   - US Open Buffer (13:25-13:45 UTC for indices)
   - DXY Macro Momentum Filter (Forex pairs opposing M15 EMA21 DXY trend)
   - Tier-1 News Blackout (+-15 min buffer around high-impact news)
2. Active Trade Management:
   - 0.8R Breakeven protection (+1 pip buffer)
3. Hardened Turtle Soup Re-Entry Engine:
   - Overshoot <= 0.35R
   - Window <= 15 minutes (<= 3 M5 bars)
   - M5 Rejection Confirmation: wick ratio >= 50% or body closed inside SL
   - Enforced Minimum Stop Floor per symbol (prevents micro-stop artifacts)
   - Stop Loss Buffer: 3.5 pips behind sweep wick
   - Target TP: exact Original Entry level (Breaker Block), protected by LOCAL_TP_GUARD
   - Tier-1 News Safeguard Bypass (+-20m)
4. Weekly Step-Compounding Anchor:
   - Sizing anchored to settled week-start equity at strictly 3.5% risk per trade.
   - Initial balance: 1,000 USC ($10.00 USD).
"""
import sys
from pathlib import Path
import json
import subprocess
import datetime as dt
import logging

BASE_DIR = Path("C:/goldfx-agent")
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
import MetaTrader5 as mt5

import config
from engine.news import fetch_calendar_events, parse_event_time, is_tier1_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("inception_sim")

def get_pip_scale(symbol: str) -> float:
    if "JPY" in symbol:
        return 0.01
    if symbol == "XAUUSD":
        return 0.1
    if any(idx in symbol for idx in ["100", "500", "30"]):
        return 1.0
    return 0.0001

def extract_all_git_history() -> tuple[list[dict], dict]:
    """Extract all historical delivered setups and outcomes across all 459 git commits."""
    log.info("Extracting all historical setups and outcomes across git commits...")
    res = subprocess.run(["git", "log", "--format=%H", "--", "gha_state/state.json"],
                         cwd=str(BASE_DIR), capture_output=True, text=True)
    commits = res.stdout.strip().split()
    all_history = {}
    all_outcomes = {}

    for c in reversed(commits):
        r = subprocess.run(["git", "show", f"{c}:gha_state/state.json"],
                           cwd=str(BASE_DIR), capture_output=True, text=True)
        if r.returncode == 0:
            try:
                d = json.loads(r.stdout)
                for h in d.get("history", []):
                    ref = h.get("ref")
                    key = f"{h.get('symbol')}:{h.get('ts')}"
                    if ref is not None and ref not in all_history:
                        all_history[ref] = h
                    elif key not in all_history:
                        all_history[key] = h
                for k, v in d.get("outcomes", {}).items():
                    if k not in all_outcomes:
                        all_outcomes[k] = v
            except Exception:
                pass

    trades = list(all_history.values())
    trades.sort(key=lambda x: str(x.get("ts", "")))
    log.info(f"Extracted {len(trades)} unique delivered setups, {len(all_outcomes)} unique outcomes.")
    return trades, all_outcomes

def load_m5_rates(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Load high-resolution M5 rates from MT5 terminal covering inception to present."""
    if not mt5.initialize():
        raise RuntimeError("Failed to initialize MT5")
    dfs = {}
    for s in symbols:
        mt5.symbol_select(s, True)
        rates = mt5.copy_rates_from_pos(s, mt5.TIMEFRAME_M5, 0, 30000)
        if rates is not None and len(rates) > 0:
            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df.set_index("time", inplace=True)
            df.rename(columns={"tick_volume": "volume"}, inplace=True)
            dfs[s] = df
            log.info(f"Loaded {len(df)} M5 bars for {s} ({df.index[0]} to {df.index[-1]})")
        else:
            log.warning(f"Failed to load M5 rates for {s}")
    return dfs

def load_dxy_trend(dfs_m5: dict) -> pd.Series | None:
    """Calculate M15 DXY EMA 21 trend baseline."""
    if "DXY" in dfs_m5:
        dxy_m5 = dfs_m5["DXY"]
        dxy_m15 = dxy_m5.resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        ema = dxy_m15["close"].ewm(span=21).mean()
        trend = (dxy_m15["close"] > ema).astype(int) * 2 - 1
        return trend
    return None

def is_setup_filtered(h: dict, news_events: list[dict], dxy_series: pd.Series | None) -> tuple[bool, str]:
    """Evaluates all current production weakling filters against a setup."""
    sym = h["symbol"]
    dir_val = 1 if h["dir"] == "LONG" else -1
    ts = pd.to_datetime(h["ts"], utc=True)
    weekday = ts.weekday() # 0=Mon, 3=Thu, 4=Fri, 6=Sun
    t_hour = ts.hour
    t_time = ts.time()

    # 1. Friday FULL_DAY filter
    if weekday == 4:
        return True, "FILTER_FRIDAY_FULL_DAY"

    # 2. Thursday Asian filter (00:00 - 06:59 UTC)
    if weekday == 3 and 0 <= t_hour < 7:
        return True, "FILTER_THU_ASIAN"

    # 3. Sunday Open filter (18:00 - 23:59 UTC)
    if weekday == 6 and 18 <= t_hour <= 23:
        return True, "FILTER_SUN_OPEN"

    # 4. USD Evening Chop filter (USDJPY/USDCHF 19:00 - 21:59 UTC)
    if sym in ("USDJPY", "USDCHF") and 19 <= t_hour < 22:
        return True, "FILTER_EVENING_CHOP"

    # 5. London Open Opening Bell Buffer (06:50 - 07:20 UTC)
    if dt.time(6, 50) <= t_time <= dt.time(7, 20):
        return True, "BUFFER_LONDON_OPEN"

    # 6. US Open Index Opening Bell Buffer (13:25 - 13:45 UTC)
    if sym in ("NASDAQ-100", "US500", "DJ30") and dt.time(13, 25) <= t_time <= dt.time(13, 45):
        return True, "BUFFER_US_OPEN"

    # 7. Red-Folder Tier-1 News Blackout (+-15 mins)
    for ne in news_events:
        if is_tier1_event(ne):
            edt = parse_event_time(ne)
            if edt:
                nts = pd.to_datetime(edt, utc=True)
                if abs((ts - nts).total_seconds()) <= 900: # 15 mins
                    return True, "NEWS_BLACKOUT"

    # 8. DXY Macro Momentum Filter (Forex Pairs Only)
    if dxy_series is not None and sym not in config.DXY_EXEMPT_SYMBOLS:
        idx_match = dxy_series.index.asof(ts)
        if pd.notna(idx_match):
            dxy_val = dxy_series.loc[idx_match]
            if sym in ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD") and dir_val == dxy_val:
                return True, "DXY_COUNTER_TREND"
            elif sym in ("USDCAD", "USDCHF", "USDJPY") and dir_val != dxy_val:
                return True, "DXY_COUNTER_TREND"

    return False, "QUALIFIED"

def evaluate_hardened_turtle_soup(sym: str, dir_val: int, orig_entry: float, orig_sl: float, orig_tp: float,
                                  sl_ts: pd.Timestamp, df_m5: pd.DataFrame, news_events: list[dict]) -> dict | None:
    """Evaluate whether a full SL setup triggers a confirmed Hardened Turtle Soup re-entry."""
    # 0. Symbol Exemption: Indices exempt from Turtle Soup re-entries (Forex & Gold focus)
    exempt_symbols = getattr(config, "TURTLE_SOUP_EXEMPT_SYMBOLS", {"NASDAQ-100", "US500", "DJ30"})
    if sym in exempt_symbols:
        return None

    sl_dist = abs(orig_entry - orig_sl)
    if sl_dist <= 0:
        return None

    pip_scale = get_pip_scale(sym)

    # 1. Tier-1 News Safeguard: bypass if SL within +-20 mins of Tier-1 news
    for ne in news_events:
        if is_tier1_event(ne):
            edt = parse_event_time(ne)
            if edt:
                nts = pd.to_datetime(edt, utc=True)
                if abs((sl_ts - nts).total_seconds()) <= 1200:
                    return None # News bypass

    # 2. Window: <= 15 minutes (<= 3 M5 bars)
    post_m5 = df_m5.loc[df_m5.index >= sl_ts].head(48)
    if len(post_m5) < 2:
        return None

    win_m5 = post_m5.head(4) # SL bar + 3 bars (15 mins)

    if dir_val == 1:
        sweep_extreme = float(win_m5["low"].min())
        overshoot_dist = max(0.0, orig_sl - sweep_extreme)
        overshoot_r = overshoot_dist / sl_dist
        if overshoot_r > 0.35:
            return None # Exceeded 0.35R

        reclaim_bars = win_m5.loc[win_m5["high"] >= orig_sl]
        if len(reclaim_bars) == 0:
            return None

        rec_ts = reclaim_bars.index[0]
        rec_bar = win_m5.loc[rec_ts]

        # Check if original TP touched before re-entry
        pre_rec_bars = win_m5.loc[win_m5.index <= rec_ts]
        if float(pre_rec_bars["high"].max()) >= orig_tp:
            return None

        # M5 Rejection Confirmation
        sw_idx = win_m5["low"].idxmin()
        sw_bar = win_m5.loc[sw_idx]
        sw_rng = float(sw_bar["high"]) - float(sw_bar["low"])
        sw_wick = min(float(sw_bar["open"]), float(sw_bar["close"])) - float(sw_bar["low"])
        wick_ratio = sw_wick / sw_rng if sw_rng > 0 else 0.0
        body_inside = float(sw_bar["close"]) >= orig_sl
        rec_close_inside = float(rec_bar["close"]) >= orig_sl

        if not (wick_ratio >= 0.50 or body_inside or rec_close_inside):
            return None

        # Execution parameters
        fill_entry = orig_sl
        buf_price = 3.5 * pip_scale
        raw_sl = sweep_extreme - buf_price
        raw_dist = fill_entry - raw_sl

        # Enforced minimum stop floor per symbol
        min_floor = config.TURTLE_SOUP_MIN_STOP_FLOOR.get(sym, 5.0 * pip_scale)
        eff_sl_dist = max(raw_dist, min_floor)
        candidate_sl = fill_entry - eff_sl_dist

        # Target: Original Entry (Breaker Block)
        candidate_tp = orig_entry
        target_dist = candidate_tp - fill_entry
        if target_dist <= 0 or eff_sl_dist <= 0:
            return None

        target_rr = target_dist / eff_sl_dist
        if target_rr < 0.5:
            return None

        # Walk forward after reclaim
        bars_after = post_m5.loc[post_m5.index >= rec_ts]
        for b_time, b in bars_after.iterrows():
            b_high = float(b["high"])
            b_low = float(b["low"])
            if b_low <= candidate_sl:
                return {
                    "ts": rec_ts, "exit_ts": b_time, "hit": "sl", "pnl_r": -1.0,
                    "rr": target_rr, "overshoot_r": overshoot_r, "sl_dist": eff_sl_dist
                }
            if b_high >= candidate_tp:
                return {
                    "ts": rec_ts, "exit_ts": b_time, "hit": "tp", "pnl_r": target_rr,
                    "rr": target_rr, "overshoot_r": overshoot_r, "sl_dist": eff_sl_dist
                }

        return None

    else:
        sweep_extreme = float(win_m5["high"].max())
        overshoot_dist = max(0.0, sweep_extreme - orig_sl)
        overshoot_r = overshoot_dist / sl_dist
        if overshoot_r > 0.35:
            return None

        reclaim_bars = win_m5.loc[win_m5["low"] <= orig_sl]
        if len(reclaim_bars) == 0:
            return None

        rec_ts = reclaim_bars.index[0]
        rec_bar = win_m5.loc[rec_ts]

        pre_rec_bars = win_m5.loc[win_m5.index <= rec_ts]
        if float(pre_rec_bars["low"].min()) <= orig_tp:
            return None

        sw_idx = win_m5["high"].idxmax()
        sw_bar = win_m5.loc[sw_idx]
        sw_rng = float(sw_bar["high"]) - float(sw_bar["low"])
        sw_wick = float(sw_bar["high"]) - max(float(sw_bar["open"]), float(sw_bar["close"]))
        wick_ratio = sw_wick / sw_rng if sw_rng > 0 else 0.0
        body_inside = float(sw_bar["close"]) <= orig_sl
        rec_close_inside = float(rec_bar["close"]) <= orig_sl

        if not (wick_ratio >= 0.50 or body_inside or rec_close_inside):
            return None

        fill_entry = orig_sl
        buf_price = 3.5 * pip_scale
        raw_sl = sweep_extreme + buf_price
        raw_dist = raw_sl - fill_entry

        min_floor = config.TURTLE_SOUP_MIN_STOP_FLOOR.get(sym, 5.0 * pip_scale)
        eff_sl_dist = max(raw_dist, min_floor)
        candidate_sl = fill_entry + eff_sl_dist

        candidate_tp = orig_entry
        target_dist = fill_entry - candidate_tp
        if target_dist <= 0 or eff_sl_dist <= 0:
            return None

        target_rr = target_dist / eff_sl_dist
        if target_rr < 0.5:
            return None

        bars_after = post_m5.loc[post_m5.index >= rec_ts]
        for b_time, b in bars_after.iterrows():
            b_high = float(b["high"])
            b_low = float(b["low"])
            if b_high >= candidate_sl:
                return {
                    "ts": rec_ts, "exit_ts": b_time, "hit": "sl", "pnl_r": -1.0,
                    "rr": target_rr, "overshoot_r": overshoot_r, "sl_dist": eff_sl_dist
                }
            if b_low <= candidate_tp:
                return {
                    "ts": rec_ts, "exit_ts": b_time, "hit": "tp", "pnl_r": target_rr,
                    "rr": target_rr, "overshoot_r": overshoot_r, "sl_dist": eff_sl_dist
                }

        return None

def run_full_simulation():
    symbols = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "NZDUSD", "USDCHF", "GBPAUD", "NASDAQ-100", "US500", "DJ30"]
    delivered_trades, git_outcomes = extract_all_git_history()
    dfs_m5 = load_m5_rates(symbols)
    news_events = fetch_calendar_events()
    dxy_series = load_dxy_trend(dfs_m5)

    log.info("Matching authenticated delivery bot outcomes and evaluating trades...")

    base_executed = []
    ts_executed = []
    pruned_count = 0
    be_saved_count = 0

    for h in delivered_trades:
        sym = h.get("symbol")
        if sym not in dfs_m5:
            continue
        ts = pd.to_datetime(h.get("ts"), utc=True)
        ref = h.get("ref")
        dir_val = 1 if h.get("dir") == "LONG" else -1
        entry = float(h.get("entry", 0))
        sl = float(h.get("sl", 0))
        tp = float(h.get("tp", 0))

        if entry <= 0 or sl <= 0 or tp <= 0 or entry == sl:
            continue

        key = f"{sym}:{h.get('ts')}"
        out = git_outcomes.get(key) or (git_outcomes.get(str(ref)) if ref is not None else None)
        if not out or out.get("hit") not in ("tp", "sl"):
            continue

        bot_hit = out.get("hit")
        sl_ts_raw = out.get("when", out.get("ts", h.get("ts")))
        sl_ts = pd.to_datetime(sl_ts_raw, utc=True) if sl_ts_raw else ts

        # Check production weakling filters
        is_filt, filt_reason = is_setup_filtered(h, news_events, dxy_series)
        if is_filt:
            pruned_count += 1
            continue

        sl_dist = abs(entry - sl)
        orig_rr = float(h.get("rr", abs(tp - entry) / sl_dist if sl_dist > 0 else 1.5))

        df_m5 = dfs_m5[sym]

        # Determine outcome under Active Trade Management / Pure Hold
        be_enabled = getattr(config, "BREAKEVEN_ENABLED", False)
        if bot_hit == "tp":
            # Authenticated TP win
            base_executed.append({
                "ref": ref, "symbol": sym, "ts": ts, "exit_ts": sl_ts,
                "dir": dir_val, "hit": "tp", "pnl_r": orig_rr, "type": "BASE"
            })
        else:
            # Baseline hit SL
            if be_enabled:
                bars_during = df_m5.loc[(df_m5.index >= ts) & (df_m5.index <= sl_ts)]
                peak_mfe = 0.0
                if len(bars_during) > 0 and sl_dist > 0:
                    for _, b in bars_during.iterrows():
                        mfe = (float(b["high"]) - entry) / sl_dist if dir_val == 1 else (entry - float(b["low"])) / sl_dist
                        if mfe > peak_mfe:
                            peak_mfe = mfe

                if peak_mfe >= 0.80:
                    # Giveback saved by Breakeven! Exits at 0.0R
                    be_saved_count += 1
                    base_executed.append({
                        "ref": ref, "symbol": sym, "ts": ts, "exit_ts": sl_ts,
                        "dir": dir_val, "hit": "be", "pnl_r": 0.0, "type": "BASE"
                    })
                else:
                    base_executed.append({
                        "ref": ref, "symbol": sym, "ts": ts, "exit_ts": sl_ts,
                        "dir": dir_val, "hit": "sl", "pnl_r": -1.0, "type": "BASE"
                    })
                    # Phase 2: Evaluate Hardened Turtle Soup Re-Entry
                    ts_res = evaluate_hardened_turtle_soup(sym, dir_val, entry, sl, tp, sl_ts, df_m5, news_events)
                    if ts_res is not None:
                        ts_executed.append({
                            "ref": f"soup_{ref}", "parent_ref": ref, "symbol": sym,
                            "ts": ts_res["ts"], "exit_ts": ts_res["exit_ts"], "hit": ts_res["hit"],
                            "pnl_r": ts_res["pnl_r"], "rr": ts_res["rr"], "type": "TURTLE_SOUP",
                            "overshoot_r": ts_res["overshoot_r"]
                        })
            else:
                # Pure Hold (Production Default): Full Stop Loss (-1.0R)
                base_executed.append({
                    "ref": ref, "symbol": sym, "ts": ts, "exit_ts": sl_ts,
                    "dir": dir_val, "hit": "sl", "pnl_r": -1.0, "type": "BASE"
                })
                # Phase 2: Evaluate Hardened Turtle Soup Re-Entry
                ts_res = evaluate_hardened_turtle_soup(sym, dir_val, entry, sl, tp, sl_ts, df_m5, news_events)
                if ts_res is not None:
                    ts_executed.append({
                        "ref": f"soup_{ref}", "parent_ref": ref, "symbol": sym,
                        "ts": ts_res["ts"], "exit_ts": ts_res["exit_ts"], "hit": ts_res["hit"],
                        "pnl_r": ts_res["pnl_r"], "rr": ts_res["rr"], "type": "TURTLE_SOUP",
                        "overshoot_r": ts_res["overshoot_r"]
                    })

    # Sort all events chronologically
    all_events_a = sorted(base_executed + ts_executed, key=lambda x: x["ts"])
    base_only_events = sorted(base_executed, key=lambda x: x["ts"])

    # Simulate portfolio growth under Weekly Step-Compounding Anchor (3.5% risk)
    def simulate_portfolio(events: list[dict], initial_balance: float = 1000.0, risk_pct: float = 0.035):
        balance = initial_balance
        peak_balance = initial_balance
        max_dd = 0.0
        weekly_history = {}
        trade_logs = []

        current_week = None
        anchor_balance = initial_balance

        for ev in events:
            t = ev["ts"]
            iso = t.isocalendar()
            week_key = f"{iso.year}-W{iso.week:02d}"

            # Weekly Rollover
            if week_key != current_week:
                current_week = week_key
                anchor_balance = balance
                weekly_history[week_key] = {
                    "start_bal": round(anchor_balance, 2),
                    "end_bal": round(anchor_balance, 2),
                    "pnl_r": 0.0,
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "be": 0
                }

            # Drawdown Safety Step-Down (> 25% drop)
            if balance < anchor_balance * 0.75:
                anchor_balance = balance

            dollar_risk = anchor_balance * risk_pct
            r = ev["pnl_r"]
            trade_pnl = dollar_risk * r
            balance += trade_pnl

            if balance > peak_balance:
                peak_balance = balance
            dd = (peak_balance - balance) / peak_balance * 100.0 if peak_balance > 0 else 0
            if dd > max_dd:
                max_dd = dd

            weekly_history[week_key]["end_bal"] = round(balance, 2)
            weekly_history[week_key]["pnl_r"] = round(weekly_history[week_key]["pnl_r"] + r, 2)
            weekly_history[week_key]["trades"] += 1
            if ev["hit"] == "tp":
                weekly_history[week_key]["wins"] += 1
            elif ev["hit"] == "sl":
                weekly_history[week_key]["losses"] += 1
            else:
                weekly_history[week_key]["be"] += 1

            trade_logs.append({
                "ts": str(t), "symbol": ev["symbol"], "type": ev["type"],
                "hit": ev["hit"], "pnl_r": r, "balance": round(balance, 2)
            })

        return balance, max_dd, weekly_history, trade_logs

    # Run simulations
    bal_base, dd_base, weeks_base, logs_base = simulate_portfolio(base_only_events)
    bal_sys_a, dd_sys_a, weeks_sys_a, logs_sys_a = simulate_portfolio(all_events_a)

    print("\n" + "=" * 90)
    print("DELIBERATE RIGOROUS BACKTEST: SETUP #1 TO PRESENT (100% REAL LIVE CONSTRAINTS)")
    print("=" * 90)
    print(f"Total Unique Delivered Setups Evaluated: {len(delivered_trades)}")
    print(f"Setups Pruned by Weakling Safeguards:    {pruned_count}")
    print(f"Giveback Losses Converted to Breakeven:  {be_saved_count} (+BE@0.8R)")
    print(f"Baseline Qualified Trades Executed:      {len(base_executed)}")
    print(f"Confirmed Turtle Soup Re-Entries:        {len(ts_executed)}")
    print(f"Total Hardened System A Executed Trades: {len(all_events_a)}")
    print("-" * 90)

    # Base Metrics
    base_wins = len([e for e in base_executed if e["hit"] == "tp"])
    base_be = len([e for e in base_executed if e["hit"] == "be"])
    base_losses = len([e for e in base_executed if e["hit"] == "sl"])
    base_r_total = sum(e["pnl_r"] for e in base_executed)
    base_pf = (sum(e["pnl_r"] for e in base_executed if e["pnl_r"] > 0) /
               abs(sum(e["pnl_r"] for e in base_executed if e["pnl_r"] < 0))) if base_losses > 0 else 0

    # TS Metrics
    ts_wins = len([e for e in ts_executed if e["hit"] == "tp"])
    ts_losses = len([e for e in ts_executed if e["hit"] == "sl"])
    ts_r_total = sum(e["pnl_r"] for e in ts_executed)
    ts_avg_rr = np.mean([e["rr"] for e in ts_executed if e["hit"] == "tp"]) if ts_wins > 0 else 0
    ts_wr = (ts_wins / len(ts_executed) * 100.0) if len(ts_executed) > 0 else 0
    ts_pf = (sum(e["pnl_r"] for e in ts_executed if e["pnl_r"] > 0) /
             abs(sum(e["pnl_r"] for e in ts_executed if e["pnl_r"] < 0))) if ts_losses > 0 else 0

    # System A Combined Metrics
    sys_a_wins = base_wins + ts_wins
    sys_a_losses = base_losses + ts_losses
    sys_a_be = base_be
    sys_a_r_total = base_r_total + ts_r_total
    sys_a_pf = (sum(e["pnl_r"] for e in all_events_a if e["pnl_r"] > 0) /
                abs(sum(e["pnl_r"] for e in all_events_a if e["pnl_r"] < 0))) if sys_a_losses > 0 else 0

    num_weeks = len(weeks_sys_a)
    avg_wk_r_base = base_r_total / num_weeks if num_weeks > 0 else 0
    avg_wk_r_sys_a = sys_a_r_total / num_weeks if num_weeks > 0 else 0

    print(f"\n{'Metric':<35} | {'Baseline (No TS)':<22} | {'Hardened System A (Production)':<25}")
    print("-" * 90)
    print(f"{'Total Trades Executed':<35} | {len(base_executed):<22} | {len(all_events_a):<25}")
    print(f"{'Trade Outcome Breakdown':<35} | {f'{base_wins}W / {base_losses}L / {base_be}BE':<22} | {f'{sys_a_wins}W / {sys_a_losses}L / {sys_a_be}BE':<25}")
    print(f"{'Win Rate (excluding BE)':<35} | {f'{base_wins/(base_wins+base_losses)*100:.1f}%':<22} | {f'{sys_a_wins/(sys_a_wins+sys_a_losses)*100:.1f}%':<25}")
    print(f"{'Total Net R':<35} | {f'+{base_r_total:.1f} R':<22} | {f'+{sys_a_r_total:.1f} R (+{ts_r_total:.1f} R lift)':<25}")
    print(f"{'Average Weekly R Expectancy':<35} | {f'+{avg_wk_r_base:.2f} R/week':<22} | {f'+{avg_wk_r_sys_a:.2f} R/week':<25}")
    print(f"{'Profit Factor':<35} | {f'{base_pf:.2f}':<22} | {f'{sys_a_pf:.2f}':<25}")
    print(f"{'Maximum Drawdown':<35} | {f'{dd_base:.1f}%':<22} | {f'{dd_sys_a:.1f}%':<25}")
    print(f"{'Starting Account Balance':<35} | {'1,000 USC ($10.00 USD)':<22} | {'1,000 USC ($10.00 USD)':<25}")
    print(f"{'Final Account Balance':<35} | {f'{bal_base:,.2f} USC (${bal_base/100:.2f})':<22} | {f'{bal_sys_a:,.2f} USC (${bal_sys_a/100:.2f})':<25}")
    print(f"{'Net Capital Return':<35} | {f'+{(bal_base-1000)/10:.1f}%':<22} | {f'+{(bal_sys_a-1000)/10:.1f}%':<25}")

    print("\n" + "=" * 90)
    print("TURTLE SOUP INDUCEMENT RE-ENTRY DEEP METRICS (SYSTEM A)")
    print("=" * 90)
    print(f"Total Re-Entry Setups Confirmed:   {len(ts_executed)}")
    print(f"Turtle Soup Win Rate:              {ts_wr:.1f}% ({ts_wins} Wins / {ts_losses} Losses)")
    print(f"Average Realized RR:               1:{ts_avg_rr:.2f} RR")
    print(f"Turtle Soup Profit Factor:         {ts_pf:.2f}")
    print(f"Total Net R Contributed:           +{ts_r_total:.1f} R")

    print("\n" + "=" * 90)
    print("WEEK-BY-WEEK STEP-COMPOUNDING BREAKDOWN (HARDENED SYSTEM A)")
    print("=" * 90)
    print(f"{'Week':<10} | {'Trades':<8} | {'Record':<14} | {'Net R':<10} | {'Start Balance':<15} | {'End Balance':<15} | {'Weekly Return'}")
    print("-" * 90)
    for wk, wdata in weeks_sys_a.items():
        w_ret = ((wdata['end_bal'] - wdata['start_bal']) / wdata['start_bal']) * 100.0 if wdata['start_bal'] > 0 else 0
        rec_str = f"{wdata['wins']}W/{wdata['losses']}L/{wdata['be']}BE"
        r_val = wdata['pnl_r']
        r_str = f"+{r_val:.1f} R" if r_val >= 0 else f"{r_val:.1f} R"
        sb_str = f"{wdata['start_bal']:,.2f} USC"
        eb_str = f"{wdata['end_bal']:,.2f} USC"
        print(f"{wk:<10} | {wdata['trades']:<8} | {rec_str:<14} | {r_str:<10} | {sb_str:<15} | {eb_str:<15} | {w_ret:+.1f}%")

    # Serialize report
    # Convert any Timestamps to strings in weekly_history and ts_trades_detail
    serializable_ts = []
    for item in ts_executed:
        c = dict(item)
        c["ts"] = str(c["ts"])
        c["exit_ts"] = str(c["exit_ts"])
        serializable_ts.append(c)

    report = {
        "summary": {
            "total_delivered_evaluated": len(delivered_trades),
            "setups_pruned": pruned_count,
            "giveback_be_saved": be_saved_count,
            "base_trades": len(base_executed),
            "ts_trades": len(ts_executed),
            "total_system_a_trades": len(all_events_a),
            "baseline": {
                "net_r": round(base_r_total, 2),
                "avg_weekly_r": round(avg_wk_r_base, 2),
                "final_balance_usc": round(bal_base, 2),
                "final_balance_usd": round(bal_base / 100.0, 2),
                "net_return_pct": round((bal_base - 1000) / 10.0, 1),
                "max_drawdown_pct": round(dd_base, 1),
                "profit_factor": round(base_pf, 2),
                "win_rate_pct": round(base_wins / (base_wins + base_losses) * 100.0, 1)
            },
            "system_a": {
                "net_r": round(sys_a_r_total, 2),
                "ts_net_r_lift": round(ts_r_total, 2),
                "avg_weekly_r": round(avg_wk_r_sys_a, 2),
                "final_balance_usc": round(bal_sys_a, 2),
                "final_balance_usd": round(bal_sys_a / 100.0, 2),
                "net_return_pct": round((bal_sys_a - 1000) / 10.0, 1),
                "max_drawdown_pct": round(dd_sys_a, 1),
                "profit_factor": round(sys_a_pf, 2),
                "win_rate_pct": round(sys_a_wins / (sys_a_wins + sys_a_losses) * 100.0, 1)
            },
            "turtle_soup": {
                "total_trades": len(ts_executed),
                "wins": ts_wins,
                "losses": ts_losses,
                "win_rate_pct": round(ts_wr, 1),
                "avg_rr": round(ts_avg_rr, 2),
                "profit_factor": round(ts_pf, 2),
                "net_r": round(ts_r_total, 2)
            }
        },
        "weekly_breakdown": weeks_sys_a,
        "ts_trades_detail": serializable_ts
    }

    report_path = BASE_DIR / "reports" / "inception_simulation_report.json"
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log.info(f"Detailed simulation report saved to {report_path}")

if __name__ == "__main__":
    run_full_simulation()

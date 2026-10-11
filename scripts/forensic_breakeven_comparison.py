#!/usr/bin/env python3
"""Forensic Breakeven Audit & Symmetrical Comparison (Setup #1 to Present).
Investigates the true physical effect of Breakeven on BOTH winning and losing trades:
1. For Winning Trades: Did price reach the BE threshold and then pull back to entry before reaching TP?
   -> If YES, the win was CHOKED to 0.0R (destroyed R).
2. For Losing Trades: Did price reach the BE threshold before hitting SL, and exit at entry?
   -> If YES, the loss was SAVED to 0.0R (saved R).
3. Compares the full portfolio under:
   - Mode 1: Pure Hold (Breakeven DISABLED - Current config.py default)
   - Mode 2: Breakeven @ 0.8R (Symmetrically evaluated on both winners and losers)
   - Mode 3: Breakeven @ 65% TP (Symmetrically evaluated on both winners and losers)
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
log = logging.getLogger("be_forensic")

def get_pip_scale(symbol: str) -> float:
    if "JPY" in symbol:
        return 0.01
    if symbol == "XAUUSD":
        return 0.1
    if any(idx in symbol for idx in ["100", "500", "30"]):
        return 1.0
    return 0.0001

def extract_all_git_history() -> tuple[list[dict], dict]:
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
    return trades, all_outcomes

def load_m5_rates(symbols: list[str]) -> dict[str, pd.DataFrame]:
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
    return dfs

def load_dxy_trend(dfs_m5: dict) -> pd.Series | None:
    if "DXY" in dfs_m5:
        dxy_m5 = dfs_m5["DXY"]
        dxy_m15 = dxy_m5.resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        ema = dxy_m15["close"].ewm(span=21).mean()
        trend = (dxy_m15["close"] > ema).astype(int) * 2 - 1
        return trend
    return None

def is_setup_filtered(h: dict, news_events: list[dict], dxy_series: pd.Series | None) -> tuple[bool, str]:
    sym = h["symbol"]
    dir_val = 1 if h["dir"] == "LONG" else -1
    ts = pd.to_datetime(h["ts"], utc=True)
    weekday = ts.weekday()
    t_hour = ts.hour
    t_time = ts.time()

    if weekday == 4: return True, "FILTER_FRIDAY_FULL_DAY"
    if weekday == 3 and 0 <= t_hour < 7: return True, "FILTER_THU_ASIAN"
    if weekday == 6 and 18 <= t_hour <= 23: return True, "FILTER_SUN_OPEN"
    if sym in ("USDJPY", "USDCHF") and 19 <= t_hour < 22: return True, "FILTER_EVENING_CHOP"
    if dt.time(6, 50) <= t_time <= dt.time(7, 20): return True, "BUFFER_LONDON_OPEN"
    if sym in ("NASDAQ-100", "US500", "DJ30") and dt.time(13, 25) <= t_time <= dt.time(13, 45): return True, "BUFFER_US_OPEN"

    for ne in news_events:
        if is_tier1_event(ne):
            edt = parse_event_time(ne)
            if edt:
                nts = pd.to_datetime(edt, utc=True)
                if abs((ts - nts).total_seconds()) <= 900: return True, "NEWS_BLACKOUT"

    if dxy_series is not None and sym not in config.DXY_EXEMPT_SYMBOLS:
        idx_match = dxy_series.index.asof(ts)
        if pd.notna(idx_match):
            dxy_val = dxy_series.loc[idx_match]
            if sym in ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD") and dir_val == dxy_val: return True, "DXY_COUNTER_TREND"
            elif sym in ("USDCAD", "USDCHF", "USDJPY") and dir_val != dxy_val: return True, "DXY_COUNTER_TREND"

    return False, "QUALIFIED"

def evaluate_trade_path(sym: str, dir_val: int, entry: float, sl: float, tp: float,
                        ts: pd.Timestamp, sl_ts_bot: pd.Timestamp, bot_hit: str,
                        df_m5: pd.DataFrame) -> dict:
    """Walk forward M5 bars to examine the exact path of the trade and measure BE impacts."""
    sl_dist = abs(entry - sl)
    target_dist = abs(tp - entry)
    orig_rr = target_dist / sl_dist if sl_dist > 0 else 1.5
    pip_scale = get_pip_scale(sym)
    be_price = entry + (1.0 * pip_scale) if dir_val == 1 else entry - (1.0 * pip_scale)

    bars = df_m5.loc[(df_m5.index >= ts) & (df_m5.index <= sl_ts_bot)]
    if len(bars) == 0:
        bars = df_m5.loc[df_m5.index >= ts].head(100)

    # Walk bars and detect:
    # 1. Did price reach >= 0.8R?
    # 2. Did price reach >= 65% TP?
    # 3. After reaching threshold, did price touch be_price?
    hit_08r = False
    hit_65tp = False
    touched_be_after_08r = False
    touched_be_after_65tp = False

    peak_mfe_r = 0.0

    for _, b in bars.iterrows():
        b_hi = float(b["high"])
        b_lo = float(b["low"])

        cur_mfe = (b_hi - entry) / sl_dist if dir_val == 1 else (entry - b_lo) / sl_dist
        if cur_mfe > peak_mfe_r:
            peak_mfe_r = cur_mfe

        if cur_mfe >= 0.80:
            hit_08r = True
        if target_dist > 0 and (cur_mfe * sl_dist / target_dist) >= 0.65:
            hit_65tp = True

        if hit_08r:
            if dir_val == 1 and b_lo <= be_price:
                touched_be_after_08r = True
            elif dir_val == -1 and b_hi >= be_price:
                touched_be_after_08r = True

        if hit_65tp:
            if dir_val == 1 and b_lo <= be_price:
                touched_be_after_65tp = True
            elif dir_val == -1 and b_hi >= be_price:
                touched_be_after_65tp = True

    return {
        "bot_hit": bot_hit,
        "orig_rr": orig_rr,
        "peak_mfe_r": peak_mfe_r,
        "hit_08r": hit_08r,
        "hit_65tp": hit_65tp,
        "touched_be_after_08r": touched_be_after_08r,
        "touched_be_after_65tp": touched_be_after_65tp,
    }

def evaluate_hardened_turtle_soup(sym: str, dir_val: int, orig_entry: float, orig_sl: float, orig_tp: float,
                                  sl_ts: pd.Timestamp, df_m5: pd.DataFrame, news_events: list[dict]) -> dict | None:
    sl_dist = abs(orig_entry - orig_sl)
    if sl_dist <= 0: return None
    pip_scale = get_pip_scale(sym)

    for ne in news_events:
        if is_tier1_event(ne):
            edt = parse_event_time(ne)
            if edt and abs((sl_ts - pd.to_datetime(edt, utc=True)).total_seconds()) <= 1200:
                return None

    post_m5 = df_m5.loc[df_m5.index >= sl_ts].head(48)
    if len(post_m5) < 2: return None
    win_m5 = post_m5.head(4)

    if dir_val == 1:
        sweep_extreme = float(win_m5["low"].min())
        overshoot_r = max(0.0, orig_sl - sweep_extreme) / sl_dist
        if overshoot_r > 0.35: return None
        reclaim_bars = win_m5.loc[win_m5["high"] >= orig_sl]
        if len(reclaim_bars) == 0: return None
        rec_ts = reclaim_bars.index[0]
        rec_bar = win_m5.loc[rec_ts]
        if float(win_m5.loc[win_m5.index <= rec_ts]["high"].max()) >= orig_tp: return None

        sw_idx = win_m5["low"].idxmin()
        sw_bar = win_m5.loc[sw_idx]
        sw_rng = float(sw_bar["high"]) - float(sw_bar["low"])
        sw_wick = min(float(sw_bar["open"]), float(sw_bar["close"])) - float(sw_bar["low"])
        wick_ratio = sw_wick / sw_rng if sw_rng > 0 else 0.0
        body_inside = float(sw_bar["close"]) >= orig_sl
        rec_close_inside = float(rec_bar["close"]) >= orig_sl
        if not (wick_ratio >= 0.50 or body_inside or rec_close_inside): return None

        fill_entry = orig_sl
        buf_price = 3.5 * pip_scale
        min_floor = config.TURTLE_SOUP_MIN_STOP_FLOOR.get(sym, 5.0 * pip_scale)
        eff_sl_dist = max(fill_entry - (sweep_extreme - buf_price), min_floor)
        candidate_sl = fill_entry - eff_sl_dist
        candidate_tp = orig_entry
        target_dist = candidate_tp - fill_entry
        if target_dist <= 0 or eff_sl_dist <= 0: return None
        target_rr = target_dist / eff_sl_dist
        if target_rr < 0.5: return None

        bars_after = post_m5.loc[post_m5.index >= rec_ts]
        for b_time, b in bars_after.iterrows():
            if float(b["low"]) <= candidate_sl:
                return {"ts": rec_ts, "exit_ts": b_time, "hit": "sl", "pnl_r": -1.0, "rr": target_rr}
            if float(b["high"]) >= candidate_tp:
                return {"ts": rec_ts, "exit_ts": b_time, "hit": "tp", "pnl_r": target_rr, "rr": target_rr}
        return None

    else:
        sweep_extreme = float(win_m5["high"].max())
        overshoot_r = max(0.0, sweep_extreme - orig_sl) / sl_dist
        if overshoot_r > 0.35: return None
        reclaim_bars = win_m5.loc[win_m5["low"] <= orig_sl]
        if len(reclaim_bars) == 0: return None
        rec_ts = reclaim_bars.index[0]
        rec_bar = win_m5.loc[rec_ts]
        if float(win_m5.loc[win_m5.index <= rec_ts]["low"].min()) <= orig_tp: return None

        sw_idx = win_m5["high"].idxmax()
        sw_bar = win_m5.loc[sw_idx]
        sw_rng = float(sw_bar["high"]) - float(sw_bar["low"])
        sw_wick = float(sw_bar["high"]) - max(float(sw_bar["open"]), float(sw_bar["close"]))
        wick_ratio = sw_wick / sw_rng if sw_rng > 0 else 0.0
        body_inside = float(sw_bar["close"]) <= orig_sl
        rec_close_inside = float(rec_bar["close"]) <= orig_sl
        if not (wick_ratio >= 0.50 or body_inside or rec_close_inside): return None

        fill_entry = orig_sl
        buf_price = 3.5 * pip_scale
        min_floor = config.TURTLE_SOUP_MIN_STOP_FLOOR.get(sym, 5.0 * pip_scale)
        eff_sl_dist = max((sweep_extreme + buf_price) - fill_entry, min_floor)
        candidate_sl = fill_entry + eff_sl_dist
        candidate_tp = orig_entry
        target_dist = fill_entry - candidate_tp
        if target_dist <= 0 or eff_sl_dist <= 0: return None
        target_rr = target_dist / eff_sl_dist
        if target_rr < 0.5: return None

        bars_after = post_m5.loc[post_m5.index >= rec_ts]
        for b_time, b in bars_after.iterrows():
            if float(b["high"]) >= candidate_sl:
                return {"ts": rec_ts, "exit_ts": b_time, "hit": "sl", "pnl_r": -1.0, "rr": target_rr}
            if float(b["low"]) <= candidate_tp:
                return {"ts": rec_ts, "exit_ts": b_time, "hit": "tp", "pnl_r": target_rr, "rr": target_rr}
        return None

def main():
    symbols = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "NZDUSD", "USDCHF", "GBPAUD", "NASDAQ-100", "US500", "DJ30"]
    delivered_trades, git_outcomes = extract_all_git_history()
    dfs_m5 = load_m5_rates(symbols)
    news_events = fetch_calendar_events()
    dxy_series = load_dxy_trend(dfs_m5)

    print("=" * 95)
    print("FORENSIC BREAKEVEN AUDIT & SYMMETRICAL COMPARISON (INCEPTION TO PRESENT)")
    print("=" * 95)

    # Forensic counters for Breakeven impact
    choked_wins_08r = 0
    destroyed_r_08r = 0.0
    saved_losses_08r = 0
    saved_r_08r = 0.0

    choked_wins_65tp = 0
    destroyed_r_65tp = 0.0
    saved_losses_65tp = 0
    saved_r_65tp = 0.0

    # Trade datasets for full portfolio simulation:
    # 1. Pure Hold (No BE, config default)
    trades_pure_hold = []
    # 2. Symmetrical BE @ 0.8R
    trades_be_08r = []
    # 3. Symmetrical BE @ 65% TP
    trades_be_65tp = []

    ts_pure_hold = []
    ts_be_08r = []

    for h in delivered_trades:
        sym = h.get("symbol")
        if sym not in dfs_m5: continue
        ts = pd.to_datetime(h.get("ts"), utc=True)
        ref = h.get("ref")
        dir_val = 1 if h.get("dir") == "LONG" else -1
        entry = float(h.get("entry", 0))
        sl = float(h.get("sl", 0))
        tp = float(h.get("tp", 0))
        if entry <= 0 or sl <= 0 or tp <= 0 or entry == sl: continue

        key = f"{sym}:{h.get('ts')}"
        out = git_outcomes.get(key) or (git_outcomes.get(str(ref)) if ref is not None else None)
        if not out or out.get("hit") not in ("tp", "sl"): continue

        bot_hit = out.get("hit")
        sl_ts_raw = out.get("when", out.get("ts", h.get("ts")))
        sl_ts = pd.to_datetime(sl_ts_raw, utc=True) if sl_ts_raw else ts

        is_filt, filt_reason = is_setup_filtered(h, news_events, dxy_series)
        if is_filt: continue

        path = evaluate_trade_path(sym, dir_val, entry, sl, tp, ts, sl_ts, bot_hit, dfs_m5[sym])
        orig_rr = path["orig_rr"]

        # --- A. Pure Hold (No Breakeven) ---
        if bot_hit == "tp":
            trades_pure_hold.append({"ref": ref, "symbol": sym, "ts": ts, "hit": "tp", "pnl_r": orig_rr, "type": "BASE"})
        else:
            trades_pure_hold.append({"ref": ref, "symbol": sym, "ts": ts, "hit": "sl", "pnl_r": -1.0, "type": "BASE"})
            # In Pure Hold, ALL 100% full SLs qualify to test Turtle Soup!
            ts_res = evaluate_hardened_turtle_soup(sym, dir_val, entry, sl, tp, sl_ts, dfs_m5[sym], news_events)
            if ts_res is not None:
                ts_pure_hold.append({"ref": f"soup_{ref}", "symbol": sym, "ts": ts_res["ts"], "hit": ts_res["hit"], "pnl_r": ts_res["pnl_r"], "type": "TURTLE_SOUP"})

        # --- B. Symmetrical BE @ 0.8R ---
        if bot_hit == "tp":
            if path["touched_be_after_08r"]:
                # Choked win!
                choked_wins_08r += 1
                destroyed_r_08r += orig_rr
                trades_be_08r.append({"ref": ref, "symbol": sym, "ts": ts, "hit": "be", "pnl_r": 0.0, "type": "BASE"})
            else:
                trades_be_08r.append({"ref": ref, "symbol": sym, "ts": ts, "hit": "tp", "pnl_r": orig_rr, "type": "BASE"})
        else:
            if path["touched_be_after_08r"]:
                # Saved loss!
                saved_losses_08r += 1
                saved_r_08r += 1.0
                trades_be_08r.append({"ref": ref, "symbol": sym, "ts": ts, "hit": "be", "pnl_r": 0.0, "type": "BASE"})
                # Since it exited at BE, it did NOT hit SL -> NO Turtle Soup!
            else:
                trades_be_08r.append({"ref": ref, "symbol": sym, "ts": ts, "hit": "sl", "pnl_r": -1.0, "type": "BASE"})
                ts_res = evaluate_hardened_turtle_soup(sym, dir_val, entry, sl, tp, sl_ts, dfs_m5[sym], news_events)
                if ts_res is not None:
                    ts_be_08r.append({"ref": f"soup_{ref}", "symbol": sym, "ts": ts_res["ts"], "hit": ts_res["hit"], "pnl_r": ts_res["pnl_r"], "type": "TURTLE_SOUP"})

        # --- C. Symmetrical BE @ 65% TP ---
        if bot_hit == "tp":
            if path["touched_be_after_65tp"]:
                choked_wins_65tp += 1
                destroyed_r_65tp += orig_rr
                trades_be_65tp.append({"ref": ref, "symbol": sym, "ts": ts, "hit": "be", "pnl_r": 0.0, "type": "BASE"})
            else:
                trades_be_65tp.append({"ref": ref, "symbol": sym, "ts": ts, "hit": "tp", "pnl_r": orig_rr, "type": "BASE"})
        else:
            if path["touched_be_after_65tp"]:
                saved_losses_65tp += 1
                saved_r_65tp += 1.0
                trades_be_65tp.append({"ref": ref, "symbol": sym, "ts": ts, "hit": "be", "pnl_r": 0.0, "type": "BASE"})
            else:
                trades_be_65tp.append({"ref": ref, "symbol": sym, "ts": ts, "hit": "sl", "pnl_r": -1.0, "type": "BASE"})

    print("\n--- 1. FORENSIC BREAKEVEN ANATOMY: SAVED LOSSES VS DESTROYED WINS ---")
    print(f"Breakeven at 0.8R:")
    print(f"  * Losses Saved from SL to 0.0R:    {saved_losses_08r} trades (+{saved_r_08r:.1f} R saved)")
    print(f"  * Wins Choked from TP to 0.0R:      {choked_wins_08r} trades (-{destroyed_r_08r:.1f} R destroyed)")
    net_be_08r = saved_r_08r - destroyed_r_08r
    print(f"  * NET IMPACT OF BE @ 0.8R:          {net_be_08r:+.1f} R ({'NET DEGRADATION' if net_be_08r < 0 else 'NET BENEFIT'})")

    print(f"\nBreakeven at 65% TP:")
    print(f"  * Losses Saved from SL to 0.0R:    {saved_losses_65tp} trades (+{saved_r_65tp:.1f} R saved)")
    print(f"  * Wins Choked from TP to 0.0R:      {choked_wins_65tp} trades (-{destroyed_r_65tp:.1f} R destroyed)")
    net_be_65tp = saved_r_65tp - destroyed_r_65tp
    print(f"  * NET IMPACT OF BE @ 65% TP:        {net_be_65tp:+.1f} R ({'NET DEGRADATION' if net_be_65tp < 0 else 'NET BENEFIT'})")

    # Full Portfolio Simulation with Weekly Anchor
    def simulate(events, name):
        events_sorted = sorted(events, key=lambda x: x["ts"])
        bal = 1000.0
        peak = 1000.0
        max_dd = 0.0
        cur_wk = None
        anchor = 1000.0
        for ev in events_sorted:
            iso = ev["ts"].isocalendar()
            wk = f"{iso.year}-W{iso.week:02d}"
            if wk != cur_wk:
                cur_wk = wk
                anchor = bal
            if bal < anchor * 0.75:
                anchor = bal
            risk_usd = anchor * 0.035
            bal += risk_usd * ev["pnl_r"]
            if bal > peak: peak = bal
            dd = (peak - bal) / peak * 100.0 if peak > 0 else 0
            if dd > max_dd: max_dd = dd

        wins = len([e for e in events if e["hit"] == "tp"])
        losses = len([e for e in events if e["hit"] == "sl"])
        be = len([e for e in events if e["hit"] == "be"])
        net_r = sum(e["pnl_r"] for e in events)
        pf = (sum(e["pnl_r"] for e in events if e["pnl_r"] > 0) /
              abs(sum(e["pnl_r"] for e in events if e["pnl_r"] < 0))) if losses > 0 else 0
        wr = wins / (wins + losses) * 100.0 if (wins + losses) > 0 else 0
        return {
            "name": name, "trades": len(events), "record": f"{wins}W / {losses}L / {be}BE",
            "wr": wr, "net_r": net_r, "pf": pf, "max_dd": max_dd, "final_bal": bal,
            "return_pct": (bal - 1000.0) / 10.0
        }

    # Compare 3 Portfolio Architectures:
    # 1. Pure Hold (Baseline Only)
    res_base_pure = simulate(trades_pure_hold, "Baseline (Pure Hold - No BE)")
    # 2. Pure Hold + Hardened Turtle Soup (Production Setting)
    res_sys_a_pure = simulate(trades_pure_hold + ts_pure_hold, "Hardened System A (Pure Hold + TS)")
    # 3. Breakeven @ 0.8R + Turtle Soup (Symmetric BE)
    res_sys_a_be08 = simulate(trades_be_08r + ts_be_08r, "Hardened System A (BE @ 0.8R + TS)")

    print("\n" + "=" * 95)
    print("--- 2. HEAD-TO-HEAD PORTFOLIO COMPARISON: PURE HOLD VS BREAKEVEN ---")
    print("=" * 95)
    print(f"{'Metric':<30} | {'Baseline (Pure Hold)':<23} | {'System A (Pure Hold + TS)':<26} | {'System A (BE@0.8R + TS)':<26}")
    print("-" * 110)
    print(f"{'Total Executed Trades':<30} | {res_base_pure['trades']:<23} | {res_sys_a_pure['trades']:<26} | {res_sys_a_be08['trades']:<26}")
    print(f"{'Record (Wins/Losses/BE)':<30} | {res_base_pure['record']:<23} | {res_sys_a_pure['record']:<26} | {res_sys_a_be08['record']:<26}")
    print(f"{'Win Rate (excluding BE)':<30} | {res_base_pure['wr']:.1f}%{'':<18} | {res_sys_a_pure['wr']:.1f}%{'':<21} | {res_sys_a_be08['wr']:.1f}%")
    print(f"{'Total Net R':<30} | {res_base_pure['net_r']:+.1f} R{'':<16} | {res_sys_a_pure['net_r']:+.1f} R{'':<19} | {res_sys_a_be08['net_r']:+.1f} R")
    print(f"{'Profit Factor':<30} | {res_base_pure['pf']:.2f}{'':<19} | {res_sys_a_pure['pf']:.2f}{'':<22} | {res_sys_a_be08['pf']:.2f}")
    print(f"{'Maximum Drawdown':<30} | {res_base_pure['max_dd']:.1f}%{'':<18} | {res_sys_a_pure['max_dd']:.1f}%{'':<21} | {res_sys_a_be08['max_dd']:.1f}%")
    print(f"{'Final Balance (USC)':<30} | {res_base_pure['final_bal']:,.2f} USC{'':<8} | {res_sys_a_pure['final_bal']:,.2f} USC{'':<11} | {res_sys_a_be08['final_bal']:,.2f} USC")
    print(f"{'Final Balance (USD)':<30} | ${res_base_pure['final_bal']/100:.2f}{'':<17} | ${res_sys_a_pure['final_bal']/100:.2f}{'':<20} | ${res_sys_a_be08['final_bal']/100:.2f}")
    print(f"{'Net Return (%)':<30} | {res_base_pure['return_pct']:+,.1f}%{'':<14} | {res_sys_a_pure['return_pct']:+,.1f}%{'':<17} | {res_sys_a_be08['return_pct']:+,.1f}%")

if __name__ == "__main__":
    main()

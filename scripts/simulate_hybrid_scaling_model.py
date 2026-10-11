#!/usr/bin/env python3
"""Simulation of the Hybrid Scaling Model for Turtle Soup.

Mechanics:
1. Re-entry executed upon confirmed M5 reclaim of SL.
2. Stop Loss: 3.5 pips behind sweep extreme (enforcing minimum spread-proof floor).
3. Position Scaling:
   - Leg 1 (60% volume): Target = Original Entry (Breaker Block).
     * When Target 1 is hit, bank profit on 60% of position.
     * Move Stop Loss on remaining 40% to Breakeven (+1 pip buffer).
   - Leg 2 (40% volume): Target = Original Full TP.
     * Runs risk-free to full macro target.
     * If price retraces to BE, exits at 0.0R (no giveback).
     * If price reaches Full TP, banks full high-RR win on 40%.
   - Full Loss: If price hits initial stop before Target 1, entire position loses -1.0R.

Evaluated across:
1. Dataset 1: Delivered Bot Setups (gha_state/state.json)
2. Dataset 2: Deep Multi-Month Historical Backtest (all 12 symbols)
"""
from __future__ import annotations

import sys
from pathlib import Path
import json
import logging

BASE_DIR = Path("C:/goldfx-agent")
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
import MetaTrader5 as mt5

from strategy.candidates import STRATEGIES
from strategy.profiles import BAL_PROFILE, SYMBOL_RUNTIME
from engine.news import fetch_calendar_events, parse_event_time, is_tier1_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hybrid_scaling_sim")

def init_mt5() -> bool:
    if not mt5.initialize():
        log.error("Failed to initialize MT5: %s", mt5.last_error())
        return False
    return True

def get_rates(symbol: str, tf, count: int = 30000) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    return df

def get_pip_scale(symbol: str) -> float:
    if "JPY" in symbol:
        return 0.01
    if symbol == "XAUUSD":
        return 0.1
    if any(idx in symbol for idx in ["100", "500", "30"]):
        return 1.0
    return 0.0001

def get_min_stop_distance(symbol: str) -> float:
    """Enforces spread-proof minimum stop distance in price units."""
    if symbol == "XAUUSD":
        return 1.50   # $1.50 minimum stop on Gold
    if "30" in symbol:
        return 15.0   # 15 points on DJ30
    if "100" in symbol:
        return 8.0    # 8 points on NAS100
    if "500" in symbol:
        return 4.0    # 4 points on US500
    if "JPY" in symbol:
        return 0.05   # 5 pips on JPY pairs
    return 0.0005     # 5 pips on standard FX pairs

def evaluate_hybrid_ts(symbol: str, direction: int, entry_orig: float, sl_orig: float, tp_orig: float,
                       sl_ts: pd.Timestamp, df_m5: pd.DataFrame, news_events: list[dict],
                       leg1_weight: float = 0.60) -> dict | None:
    """Evaluates the Hybrid Scaling Model for a stopped-out setup."""
    orig_risk = abs(entry_orig - sl_orig)
    if orig_risk <= 0:
        return None

    pip_scale = get_pip_scale(symbol)
    min_stop = get_min_stop_distance(symbol)

    # 1. News Blackout Guard: Reject sweeps triggered during Tier-1 news
    for ne in news_events:
        if is_tier1_event(ne):
            dt_obj = parse_event_time(ne)
            if dt_obj:
                nts = pd.to_datetime(dt_obj, utc=True)
                if abs((sl_ts - nts).total_seconds()) <= 1200:
                    return None

    post_m5 = df_m5.loc[df_m5.index >= sl_ts].head(48)
    if len(post_m5) < 3:
        return None

    if direction == 1:
        # LONG SETUP
        sweep_extreme = float(post_m5["low"].min())
        overshoot_dist = max(0.0, sl_orig - sweep_extreme)
        overshoot_r = overshoot_dist / orig_risk
        if overshoot_r > 0.35:
            return None # Swept too deep

        sw_idx = post_m5["low"].idxmin()
        sw_bar = post_m5.loc[sw_idx]

        # Rejection candle signature: lower wick >= 50% or body closed inside SL
        rng = float(sw_bar["high"] - sw_bar["low"])
        lower_wick = float(min(sw_bar["open"], sw_bar["close"]) - sw_bar["low"])
        wick_ratio = lower_wick / rng if rng > 0 else 0
        body_inside = float(sw_bar["close"]) >= sl_orig
        if not (wick_ratio >= 0.50 or body_inside):
            return None

        # Reclaim check within <= 3 bars
        reclaim_bars = post_m5.loc[(post_m5.index >= sw_idx) & (post_m5["high"] >= sl_orig)]
        if len(reclaim_bars) == 0:
            return None
        rec_ts = reclaim_bars.index[0]
        bars_to_rec = post_m5.index.get_loc(rec_ts) - post_m5.index.get_loc(sw_idx)
        if bars_to_rec > 3:
            return None

        fill_entry = sl_orig
        calc_stop = (fill_entry - sweep_extreme) + (3.5 * pip_scale)
        stop_dist = max(min_stop, calc_stop)
        initial_sl = fill_entry - stop_dist

        target_leg1 = entry_orig # Original Entry
        target_leg2 = tp_orig    # Original Full TP

        dist_leg1 = target_leg1 - fill_entry
        dist_leg2 = target_leg2 - fill_entry
        if dist_leg1 <= 0 or dist_leg2 <= 0:
            return None

        rr_leg1 = dist_leg1 / stop_dist
        rr_leg2 = dist_leg2 / stop_dist

        # Bar-by-bar walk
        bars_after = post_m5.loc[post_m5.index >= rec_ts]
        hit_initial_sl = False
        hit_leg1 = False
        hit_leg2 = False
        hit_be_leg2 = False

        be_price = fill_entry + (1.0 * pip_scale) # BE + 1 pip spread buffer

        for _, b in bars_after.iterrows():
            b_high = float(b["high"])
            b_low = float(b["low"])

            if not hit_leg1:
                # Still in initial state (both legs open with initial SL)
                if b_low <= initial_sl:
                    hit_initial_sl = True
                    break
                elif b_high >= target_leg1:
                    hit_leg1 = True
                    # Leg 1 hits Target 1!
                    # SL for Leg 2 is moved to BE (be_price)
            else:
                # Leg 1 already banked! Leg 2 is running risk-free
                if b_low <= be_price:
                    hit_be_leg2 = True
                    break
                elif b_high >= target_leg2:
                    hit_leg2 = True
                    break

        leg2_weight = 1.0 - leg1_weight
        if hit_initial_sl:
            pnl_hybrid = -1.0
            pnl_pure_entry = -1.0
            pnl_pure_tp = -1.0
            outcome = "sl"
        elif hit_leg1:
            # Leg 1 banked!
            r_leg1_contrib = rr_leg1 * leg1_weight
            pnl_pure_entry = rr_leg1
            if hit_leg2:
                # Runner hit full TP!
                r_leg2_contrib = rr_leg2 * leg2_weight
                pnl_pure_tp = rr_leg2
                outcome = "tp_both"
            else:
                # Runner stopped at Breakeven
                r_leg2_contrib = 0.0
                pnl_pure_tp = -1.0 if not hit_initial_sl else -1.0
                outcome = "tp_leg1_only"
            pnl_hybrid = r_leg1_contrib + r_leg2_contrib
        else:
            pnl_hybrid = -1.0
            pnl_pure_entry = -1.0
            pnl_pure_tp = -1.0
            outcome = "sl"

        return {
            "symbol": symbol,
            "ts": rec_ts,
            "outcome": outcome,
            "pnl_hybrid": pnl_hybrid,
            "pnl_pure_entry": pnl_pure_entry,
            "pnl_pure_tp": pnl_pure_tp,
            "rr_leg1": rr_leg1,
            "rr_leg2": rr_leg2,
            "hit_leg1": hit_leg1,
            "hit_leg2": hit_leg2,
            "stop_dist": stop_dist
        }

    else:
        # SHORT SETUP
        sweep_extreme = float(post_m5["high"].max())
        overshoot_dist = max(0.0, sweep_extreme - sl_orig)
        overshoot_r = overshoot_dist / orig_risk
        if overshoot_r > 0.35:
            return None

        sw_idx = post_m5["high"].idxmax()
        sw_bar = post_m5.loc[sw_idx]

        rng = float(sw_bar["high"] - sw_bar["low"])
        upper_wick = float(sw_bar["high"] - max(sw_bar["open"], sw_bar["close"]))
        wick_ratio = upper_wick / rng if rng > 0 else 0
        body_inside = float(sw_bar["close"]) <= sl_orig
        if not (wick_ratio >= 0.50 or body_inside):
            return None

        reclaim_bars = post_m5.loc[(post_m5.index >= sw_idx) & (post_m5["low"] <= sl_orig)]
        if len(reclaim_bars) == 0:
            return None
        rec_ts = reclaim_bars.index[0]
        bars_to_rec = post_m5.index.get_loc(rec_ts) - post_m5.index.get_loc(sw_idx)
        if bars_to_rec > 3:
            return None

        fill_entry = sl_orig
        calc_stop = (sweep_extreme - fill_entry) + (3.5 * pip_scale)
        stop_dist = max(min_stop, calc_stop)
        initial_sl = fill_entry + stop_dist

        target_leg1 = entry_orig
        target_leg2 = tp_orig

        dist_leg1 = fill_entry - target_leg1
        dist_leg2 = fill_entry - target_leg2
        if dist_leg1 <= 0 or dist_leg2 <= 0:
            return None

        rr_leg1 = dist_leg1 / stop_dist
        rr_leg2 = dist_leg2 / stop_dist

        bars_after = post_m5.loc[post_m5.index >= rec_ts]
        hit_initial_sl = False
        hit_leg1 = False
        hit_leg2 = False
        hit_be_leg2 = False

        be_price = fill_entry - (1.0 * pip_scale)

        for _, b in bars_after.iterrows():
            b_high = float(b["high"])
            b_low = float(b["low"])

            if not hit_leg1:
                if b_high >= initial_sl:
                    hit_initial_sl = True
                    break
                elif b_low <= target_leg1:
                    hit_leg1 = True
            else:
                if b_high >= be_price:
                    hit_be_leg2 = True
                    break
                elif b_low <= target_leg2:
                    hit_leg2 = True
                    break

        leg2_weight = 1.0 - leg1_weight
        if hit_initial_sl:
            pnl_hybrid = -1.0
            pnl_pure_entry = -1.0
            pnl_pure_tp = -1.0
            outcome = "sl"
        elif hit_leg1:
            r_leg1_contrib = rr_leg1 * leg1_weight
            pnl_pure_entry = rr_leg1
            if hit_leg2:
                r_leg2_contrib = rr_leg2 * leg2_weight
                pnl_pure_tp = rr_leg2
                outcome = "tp_both"
            else:
                r_leg2_contrib = 0.0
                pnl_pure_tp = -1.0
                outcome = "tp_leg1_only"
            pnl_hybrid = r_leg1_contrib + r_leg2_contrib
        else:
            pnl_hybrid = -1.0
            pnl_pure_entry = -1.0
            pnl_pure_tp = -1.0
            outcome = "sl"

        return {
            "symbol": symbol,
            "ts": rec_ts,
            "outcome": outcome,
            "pnl_hybrid": pnl_hybrid,
            "pnl_pure_entry": pnl_pure_entry,
            "pnl_pure_tp": pnl_pure_tp,
            "rr_leg1": rr_leg1,
            "rr_leg2": rr_leg2,
            "hit_leg1": hit_leg1,
            "hit_leg2": hit_leg2,
            "stop_dist": stop_dist
        }

def run_simulation(dataset_name: str, base_trades: list[dict], ts_trades: list[dict]):
    print("=" * 105)
    print(f"{dataset_name.upper()}: COMPREHENSIVE HYBRID SCALING MODEL AUDIT")
    print("Comparing 4 Strategies: Baseline vs 100% Entry vs 100% Full TP vs Hybrid (60% Entry + 40% Runner)")
    print("=" * 105)

    # 1. Telemetry of the Isolated Hybrid Turtle Soup Engine
    n_ts = len(ts_trades)
    hit_leg1_count = sum(1 for t in ts_trades if t["hit_leg1"])
    hit_both_count = sum(1 for t in ts_trades if t["hit_leg2"])
    be_runner_count = sum(1 for t in ts_trades if t["hit_leg1"] and not t["hit_leg2"])
    full_sl_count = sum(1 for t in ts_trades if not t["hit_leg1"])

    print(f"\n1. ISOLATED TURTLE SOUP EXECUTION ANATOMY (Total Triggered: {n_ts} trades):")
    print(f"   * Reached Target 1 (Original Entry): {hit_leg1_count} / {n_ts} ({hit_leg1_count/n_ts*100:.1f}%)")
    print(f"     - Retraced to Breakeven on Runner: {be_runner_count} / {n_ts} ({be_runner_count/n_ts*100:.1f}%) -> Protected by BE!")
    print(f"     - Continued to Full Macro TP:     {hit_both_count} / {n_ts} ({hit_both_count/n_ts*100:.1f}%) -> Mega Win Banked!")
    print(f"   * Full Initial Stop Loss Hit:       {full_sl_count} / {n_ts} ({full_sl_count/n_ts*100:.1f}%)")

    # 2. Strategy Comparison Simulation (Compounding 1,000 USC at 3.5% risk)
    def simulate_strategy(pnl_key: str | None):
        all_events = []
        for bt in base_trades:
            all_events.append({"ts": bt["ts"], "pnl_r": bt["pnl_r"]})
        if pnl_key is not None:
            for tt in ts_trades:
                all_events.append({"ts": tt["ts"], "pnl_r": tt[pnl_key]})
        all_events.sort(key=lambda x: x["ts"])

        bal = 1000.0
        peak = 1000.0
        max_dd = 0.0
        wins = 0
        pnls = []

        for ev in all_events:
            r = ev["pnl_r"]
            pnls.append(r)
            if r > 0: wins += 1
            risk_usd = bal * 0.035
            bal += risk_usd * r
            if bal > peak: peak = bal
            dd = (peak - bal) / peak * 100.0
            if dd > max_dd: max_dd = dd

        w = [p for p in pnls if p > 0]
        l = [p for p in pnls if p < 0]
        n = len(pnls)
        wr = wins / n * 100.0
        avg_w = np.mean(w) if w else 0
        avg_l = abs(np.mean(l)) if l else 0
        payoff = avg_w / avg_l if avg_l > 0 else 0
        gw = sum(w)
        gl = abs(sum(l))
        pf = gw / gl if gl > 0 else 99.0
        net_r = sum(pnls)
        ret_pct = (bal - 1000.0) / 1000.0 * 100.0

        return {
            "trades": n, "wins": len(w), "losses": len(l), "wr": wr,
            "avg_w": avg_w, "avg_l": avg_l, "payoff": payoff,
            "gw": gw, "gl": gl, "pf": pf, "net_r": net_r,
            "bal": bal, "ret_pct": ret_pct, "max_dd": max_dd,
            "ret_dd": ret_pct / max(0.1, max_dd)
        }

    m_base = simulate_strategy(None)
    m_pure_entry = simulate_strategy("pnl_pure_entry")
    m_pure_tp = simulate_strategy("pnl_pure_tp")
    m_hybrid = simulate_strategy("pnl_hybrid")

    print(f"\n2. SYSTEM COMPARATIVE PERFORMANCE TABLE (Initial Deposit: 1,000.00 USC / $10.00 USD):")
    print(f"{'Performance Metric':<32} | {'Baseline (No TS)':<18} | {'100% Entry Target':<18} | {'100% Full TP Target':<19} | {'HYBRID (60/40)'}")
    print("-" * 115)
    print(f"{'Total Trades':<32} | {m_base['trades']:<18} | {m_pure_entry['trades']:<18} | {m_pure_tp['trades']:<19} | {m_hybrid['trades']}")
    print(f"{'Win Rate (%)':<32} | {m_base['wr']:<17.1f}% | {m_pure_entry['wr']:<17.1f}% | {m_pure_tp['wr']:<18.1f}% | {m_hybrid['wr']:.1f}%")
    print(f"{'Payoff Ratio (Win/Loss)':<32} | {m_base['payoff']:<18.2f} | {m_pure_entry['payoff']:<18.2f} | {m_pure_tp['payoff']:<19.2f} | {m_hybrid['payoff']:.2f}")
    print(f"{'Gross Wins Banked (R)':<32} | +{m_base['gw']:<17.1f}R | +{m_pure_entry['gw']:<17.1f}R | +{m_pure_tp['gw']:<18.1f}R | +{m_hybrid['gw']:.1f}R")
    print(f"{'Gross Losses Sustained (R)':<32} | -{m_base['gl']:<17.1f}R | -{m_pure_entry['gl']:<17.1f}R | -{m_pure_tp['gl']:<18.1f}R | -{m_hybrid['gl']:.1f}R")
    print(f"{'Profit Factor':<32} | {m_base['pf']:<18.2f} | {m_pure_entry['pf']:<18.2f} | {m_pure_tp['pf']:<19.2f} | {m_hybrid['pf']:.2f}")
    print(f"{'Total Net Profit (R)':<32} | +{m_base['net_r']:<17.1f}R | +{m_pure_entry['net_r']:<17.1f}R | +{m_pure_tp['net_r']:<18.1f}R | +{m_hybrid['net_r']:.1f}R")
    bal_base_str = f"{m_base['bal']:.2f} USC"
    bal_entry_str = f"{m_pure_entry['bal']:.2f} USC"
    bal_tp_str = f"{m_pure_tp['bal']:.2f} USC"
    bal_hyb_str = f"{m_hybrid['bal']:.2f} USC"

    usd_base_str = f"${m_base['bal']/100:.2f} USD"
    usd_entry_str = f"${m_pure_entry['bal']/100:.2f} USD"
    usd_tp_str = f"${m_pure_tp['bal']/100:.2f} USD"
    usd_hyb_str = f"${m_hybrid['bal']/100:.2f} USD"

    ret_base_str = f"{m_base['ret_pct']:+.1f}%"
    ret_entry_str = f"{m_pure_entry['ret_pct']:+.1f}%"
    ret_tp_str = f"{m_pure_tp['ret_pct']:+.1f}%"
    ret_hyb_str = f"{m_hybrid['ret_pct']:+.1f}%"

    print("-" * 115)
    print(f"{'Final Balance':<32} | {bal_base_str:<18} | {bal_entry_str:<18} | {bal_tp_str:<19} | {bal_hyb_str}")
    print(f"{'Final USD Value':<32} | {usd_base_str:<18} | {usd_entry_str:<18} | {usd_tp_str:<19} | {usd_hyb_str}")
    print(f"{'Net Capital Return (%)':<32} | {ret_base_str:<18} | {ret_entry_str:<18} | {ret_tp_str:<19} | {ret_hyb_str}")
    print(f"{'Maximum Drawdown (%)':<32} | {m_base['max_dd']:<17.1f}% | {m_pure_entry['max_dd']:<17.1f}% | {m_pure_tp['max_dd']:<18.1f}% | {m_hybrid['max_dd']:.1f}%")
    print(f"{'Return / Max Drawdown Ratio':<32} | {m_base['ret_dd']:<17.2f}x | {m_pure_entry['ret_dd']:<17.2f}x | {m_pure_tp['ret_dd']:<18.2f}x | {m_hybrid['ret_dd']:.2f}x")

def main():
    if not init_mt5():
        return

    # 1. Delivered Setups Study
    state_file = BASE_DIR / "gha_state" / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    history = state.get("history", [])
    outcomes = state.get("outcomes", {})
    news_events = fetch_calendar_events()

    symbols = sorted(list(set(h["symbol"] for h in history)))
    m5_cache = {s: get_rates(s, mt5.TIMEFRAME_M5, 30000) for s in symbols}

    delivered_base = []
    delivered_ts = []

    for h in history:
        sym = h["symbol"]
        key = f"{sym}:{h['ts']}"
        out = outcomes.get(key)
        if not out:
            continue
        sig_ts = pd.to_datetime(h["ts"], utc=True)
        dir_val = 1 if h["dir"] == "LONG" else -1
        entry_p = float(h["entry"])
        sl_p = float(h["sl"])
        tp_p = float(h["tp"])
        orig_risk = abs(entry_p - sl_p)
        orig_rr = float(h.get("rr", abs(tp_p - entry_p)/orig_risk if orig_risk > 0 else 1.5))
        hit = out.get("hit")
        delivered_base.append({"ts": sig_ts, "symbol": sym, "hit": hit, "pnl_r": orig_rr if hit == "tp" else -1.0})

        if hit == "sl":
            sl_ts = pd.to_datetime(out.get("when", out.get("ts", sig_ts)), utc=True)
            df_m5 = m5_cache.get(sym)
            if df_m5 is not None:
                res = evaluate_hybrid_ts(sym, dir_val, entry_p, sl_p, tp_p, sl_ts, df_m5, news_events, leg1_weight=0.60)
                if res:
                    delivered_ts.append(res)

    run_simulation("Dataset 1: Delivered Bot Setups (Real Project History)", delivered_base, delivered_ts)

    # 2. Deep Historical Study
    all_syms = list(SYMBOL_RUNTIME.keys())
    base_params = dict(BAL_PROFILE["params"])
    hist_base = []
    hist_ts = []

    for sym in all_syms:
        cfg = SYMBOL_RUNTIME[sym]
        entry_tf_str = cfg["entry_tf"]
        bias_htf_str = cfg["bias_htf"]
        strat_key = cfg["strategy"]

        tf_map = {
            "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
            "H1":  mt5.TIMEFRAME_H1,  "H4":  mt5.TIMEFRAME_H4,
        }
        df_entry = get_rates(sym, tf_map[entry_tf_str], 6000)
        df_m5 = get_rates(sym, mt5.TIMEFRAME_M5, 30000)

        if df_entry is None or df_m5 is None or len(df_entry) < 500:
            continue

        p = dict(base_params)
        p["bias_htf"] = bias_htf_str
        p["_symbol"] = sym

        strat = STRATEGIES.get(strat_key, STRATEGIES["fvg_retest"])
        signals = strat.signaller(df_entry, p)
        entry_times = df_entry.index
        n_bars = len(df_entry)

        for sig in signals:
            try:
                sig_idx = entry_times.get_loc(sig.ts)
            except KeyError:
                continue
            trade_idx = sig_idx + 1
            if trade_idx >= n_bars:
                continue

            entry_bar = df_entry.iloc[trade_idx]
            entry_ts = entry_times[trade_idx]
            entry_price = float(entry_bar["open"])
            sl_dist = float(sig.sl_offset)
            if sl_dist <= 0:
                continue

            if sig.dir == 1:
                sl_price = entry_price - sl_dist
                tp_price = float(sig.tp_price) if sig.tp_price and sig.tp_price > entry_price else entry_price + (sl_dist * sig.tp_r)
            else:
                sl_price = entry_price + sl_dist
                tp_price = float(sig.tp_price) if sig.tp_price and sig.tp_price < entry_price else entry_price - (sl_dist * sig.tp_r)

            target_dist = abs(tp_price - entry_price)
            planned_rr = target_dist / sl_dist

            hit_sl = False
            sl_ts = None
            outcome = None
            for k in range(trade_idx, min(n_bars, trade_idx + 300)):
                bar = df_entry.iloc[k]
                b_high = float(bar["high"])
                b_low = float(bar["low"])
                b_ts = entry_times[k]
                if sig.dir == 1:
                    if b_low <= sl_price:
                        outcome = 'sl'; hit_sl = True; sl_ts = b_ts; break
                    elif b_high >= tp_price:
                        outcome = 'tp'; break
                else:
                    if b_high >= sl_price:
                        outcome = 'sl'; hit_sl = True; sl_ts = b_ts; break
                    elif b_low <= tp_price:
                        outcome = 'tp'; break

            if outcome is None:
                continue

            hist_base.append({"ts": entry_ts, "symbol": sym, "hit": outcome, "pnl_r": planned_rr if outcome == "tp" else -1.0})
            if hit_sl and sl_ts:
                res = evaluate_hybrid_ts(sym, sig.dir, entry_price, sl_price, tp_price, sl_ts, df_m5, news_events, leg1_weight=0.60)
                if res:
                    hist_ts.append(res)

    print("\n")
    run_simulation("Dataset 2: Deep Multi-Month Historical Data (All 12 Symbols)", hist_base, hist_ts)
    mt5.shutdown()

if __name__ == "__main__":
    main()

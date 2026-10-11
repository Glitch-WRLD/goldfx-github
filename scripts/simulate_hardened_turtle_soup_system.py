#!/usr/bin/env python3
"""Simulation of the Hardened System A:
- Current Production System + Confirmed Turtle Soup Upgrade
- Hardened Parameter Specification:
  1. Overshoot <= 0.35R
  2. M5 Rejection Confirmation: M5 candle close inside SL within <= 15 mins (<= 3 M5 bars)
  3. Stop Loss Buffer: 3.5 pips behind sweep wick (spread armor)
  4. Adverse Fill Slippage: 2.0 pips adverse penalty on entry execution
  5. TP Target: 1.0 pip before Original Entry (Breaker Block front-running)
  6. News Safeguard: Tier-1 news blackout active (excludes news-triggered sweeps)

Evaluated across:
1. Dataset 1: Real Delivered Bot Setups (gha_state/state.json)
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
log = logging.getLogger("hardened_ts_sim")

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

def evaluate_hardened_reversal(symbol: str, direction: int, entry_orig: float, sl_orig: float,
                               sl_ts: pd.Timestamp, df_m5: pd.DataFrame, news_events: list[dict],
                               max_overshoot_r: float = 0.35, sl_buf_pips: float = 3.5,
                               slippage_pips: float = 2.0, frontrun_pips: float = 1.0) -> dict | None:
    """Evaluates whether an SL setup qualifies for the hardened Turtle Soup and returns outcome."""
    orig_risk = abs(entry_orig - sl_orig)
    if orig_risk <= 0:
        return None

    pip_scale = get_pip_scale(symbol)
    pip_dir = 1 if direction == 1 else -1

    # 1. News Safeguard: Check if SL occurred within 20 mins of Tier-1 news
    for ne in news_events:
        if is_tier1_event(ne):
            dt_obj = parse_event_time(ne)
            if dt_obj:
                nts = pd.to_datetime(dt_obj, utc=True)
                if abs((sl_ts - nts).total_seconds()) <= 1200: # 20 mins buffer
                    return None # Excluded by news safeguard

    post_m5 = df_m5.loc[df_m5.index >= sl_ts].head(48)
    if len(post_m5) < 3:
        return None

    if direction == 1:
        # LONG: Swept below sl_orig
        sweep_extreme = float(post_m5["low"].min())
        overshoot_dist = max(0.0, sl_orig - sweep_extreme)
        overshoot_r = overshoot_dist / orig_risk
        if overshoot_r > max_overshoot_r:
            return None # Overshoot exceeded 0.35R

        sw_idx = post_m5["low"].idxmin()
        reclaim_bars = post_m5.loc[(post_m5.index >= sw_idx) & (post_m5["high"] >= sl_orig)]
        if len(reclaim_bars) == 0:
            return None

        rec_ts = reclaim_bars.index[0]
        rec_bar = post_m5.loc[rec_ts]
        bars_to_rec = post_m5.index.get_loc(rec_ts) - post_m5.index.get_loc(sw_idx)
        # M5 Rejection Confirmation: Close inside SL within <= 3 bars (15 mins)
        if bars_to_rec > 3 or float(rec_bar["close"]) < sl_orig:
            return None

        # Hardened Execution Parameters
        eff_entry = sl_orig + (slippage_pips * pip_scale) # 2.0 pips adverse fill
        eff_sl = sweep_extreme - (sl_buf_pips * pip_scale) # 3.5 pips wide buffer
        new_risk = eff_entry - eff_sl
        if new_risk <= 0:
            return None

        # Target: 1.0 pip before Original Entry
        target_tp = entry_orig - (frontrun_pips * pip_scale)
        target_rr = (target_tp - eff_entry) / new_risk

        # Walk bars after reclaim
        bars_after = post_m5.loc[post_m5.index >= rec_ts]
        hit_sl = False
        hit_tp = False

        for _, b in bars_after.iterrows():
            b_high = float(b["high"])
            b_low = float(b["low"])
            if b_low <= eff_sl:
                hit_sl = True
                break
            elif b_high >= target_tp:
                hit_tp = True
                break

        pnl_r = target_rr if hit_tp else -1.0
        return {
            "symbol": symbol,
            "ts": rec_ts,
            "hit": "tp" if hit_tp else "sl",
            "pnl_r": pnl_r,
            "rr": target_rr,
            "new_risk": new_risk,
            "overshoot_r": overshoot_r,
        }

    else:
        # SHORT: Swept above sl_orig
        sweep_extreme = float(post_m5["high"].max())
        overshoot_dist = max(0.0, sweep_extreme - sl_orig)
        overshoot_r = overshoot_dist / orig_risk
        if overshoot_r > max_overshoot_r:
            return None

        sw_idx = post_m5["high"].idxmax()
        reclaim_bars = post_m5.loc[(post_m5.index >= sw_idx) & (post_m5["low"] <= sl_orig)]
        if len(reclaim_bars) == 0:
            return None

        rec_ts = reclaim_bars.index[0]
        rec_bar = post_m5.loc[rec_ts]
        bars_to_rec = post_m5.index.get_loc(rec_ts) - post_m5.index.get_loc(sw_idx)
        if bars_to_rec > 3 or float(rec_bar["close"]) > sl_orig:
            return None

        eff_entry = sl_orig - (slippage_pips * pip_scale) # 2.0 pips adverse fill
        eff_sl = sweep_extreme + (sl_buf_pips * pip_scale) # 3.5 pips wide buffer
        new_risk = eff_sl - eff_entry
        if new_risk <= 0:
            return None

        target_tp = entry_orig + (frontrun_pips * pip_scale) # 1.0 pip before entry
        target_rr = (eff_entry - target_tp) / new_risk

        bars_after = post_m5.loc[post_m5.index >= rec_ts]
        hit_sl = False
        hit_tp = False

        for _, b in bars_after.iterrows():
            b_high = float(b["high"])
            b_low = float(b["low"])
            if b_high >= eff_sl:
                hit_sl = True
                break
            elif b_low <= target_tp:
                hit_tp = True
                break

        pnl_r = target_rr if hit_tp else -1.0
        return {
            "symbol": symbol,
            "ts": rec_ts,
            "hit": "tp" if hit_tp else "sl",
            "pnl_r": pnl_r,
            "rr": target_rr,
            "new_risk": new_risk,
            "overshoot_r": overshoot_r,
        }

def simulate_portfolio_growth(base_trades: list[dict], ts_trades: list[dict], initial_balance: float = 1000.0, risk_pct: float = 0.035):
    """Simulates portfolio growth comparing Baseline vs Hardened System A."""
    # Combine trades chronologically
    all_events = []
    for bt in base_trades:
        all_events.append({
            "ts": bt["ts"],
            "type": "BASE",
            "pnl_r": bt["pnl_r"],
            "hit": bt["hit"],
        })
    for tt in ts_trades:
        all_events.append({
            "ts": tt["ts"],
            "type": "TURTLE_SOUP",
            "pnl_r": tt["pnl_r"],
            "hit": tt["hit"],
        })

    all_events.sort(key=lambda x: x["ts"])

    # 1. Baseline System (Base trades ONLY)
    bal_base = initial_balance
    peak_base = initial_balance
    max_dd_base = 0.0
    wins_base = 0
    trades_base = 0
    pnls_base = []

    for ev in all_events:
        if ev["type"] == "BASE":
            r = ev["pnl_r"]
            pnls_base.append(r)
            trades_base += 1
            if r > 0: wins_base += 1
            dollar_risk = bal_base * risk_pct
            bal_base += dollar_risk * r
            if bal_base > peak_base: peak_base = bal_base
            dd = (peak_base - bal_base) / peak_base * 100.0
            if dd > max_dd_base: max_dd_base = dd

    # 2. Hardened System A (Base + Hardened Turtle Soup)
    bal_sys_a = initial_balance
    peak_sys_a = initial_balance
    max_dd_sys_a = 0.0
    wins_sys_a = 0
    trades_sys_a = 0
    pnls_sys_a = []

    for ev in all_events:
        r = ev["pnl_r"]
        pnls_sys_a.append(r)
        trades_sys_a += 1
        if r > 0: wins_sys_a += 1
        dollar_risk = bal_sys_a * risk_pct
        bal_sys_a += dollar_risk * r
        if bal_sys_a > peak_sys_a: peak_sys_a = bal_sys_a
        dd = (peak_sys_a - bal_sys_a) / peak_sys_a * 100.0
        if dd > max_dd_sys_a: max_dd_sys_a = dd

    def metrics(pnls, trades, wins, bal, max_dd):
        w = [p for p in pnls if p > 0]
        l = [p for p in pnls if p < 0]
        wr = wins / trades * 100.0 if trades > 0 else 0
        avg_w = np.mean(w) if w else 0
        avg_l = abs(np.mean(l)) if l else 0
        payoff = avg_w / avg_l if avg_l > 0 else 0
        gw = sum(w)
        gl = abs(sum(l))
        pf = gw / gl if gl > 0 else 99.0
        net_r = sum(pnls)
        ret_pct = (bal - initial_balance) / initial_balance * 100.0
        return {
            "trades": trades,
            "wins": len(w),
            "losses": len(l),
            "wr": wr,
            "avg_w": avg_w,
            "avg_l": avg_l,
            "payoff": payoff,
            "gw": gw,
            "gl": gl,
            "pf": pf,
            "net_r": net_r,
            "bal": bal,
            "ret_pct": ret_pct,
            "max_dd": max_dd,
            "ret_dd": ret_pct / max(0.1, max_dd)
        }

    return metrics(pnls_base, trades_base, wins_base, bal_base, max_dd_base), \
           metrics(pnls_sys_a, trades_sys_a, wins_sys_a, bal_sys_a, max_dd_sys_a)

def run_delivered_setups_simulation():
    state_file = BASE_DIR / "gha_state" / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    history = state.get("history", [])
    outcomes = state.get("outcomes", {})
    news_events = fetch_calendar_events()

    symbols = sorted(list(set(h["symbol"] for h in history)))
    m5_cache = {}
    for s in symbols:
        m5_cache[s] = get_rates(s, mt5.TIMEFRAME_M5, 30000)

    base_trades = []
    ts_trades = []

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
        orig_rr = float(h.get("rr", abs(tp_p - entry_p) / orig_risk if orig_risk > 0 else 1.5))
        hit = out.get("hit")

        base_trades.append({
            "ts": sig_ts,
            "symbol": sym,
            "hit": hit,
            "pnl_r": orig_rr if hit == "tp" else -1.0
        })

        if hit == "sl":
            sl_ts = pd.to_datetime(out.get("when", out.get("ts", sig_ts)), utc=True)
            df_m5 = m5_cache.get(sym)
            if df_m5 is not None:
                ts_res = evaluate_hardened_reversal(
                    sym, dir_val, entry_p, sl_p, sl_ts, df_m5, news_events,
                    max_overshoot_r=0.35, sl_buf_pips=3.5, slippage_pips=2.0, frontrun_pips=1.0
                )
                if ts_res:
                    ts_trades.append(ts_res)

    print("=" * 95)
    print("DATASET 1: DELIVERED BOT SETUPS (Real Out-of-Sample Project History)")
    print(f"Delivered Setups Evaluated: {len(base_trades)} | Hardened Turtle Soup Triggered: {len(ts_trades)}")
    print("=" * 95)

    base_m, sys_a_m = simulate_portfolio_growth(base_trades, ts_trades, initial_balance=1000.0, risk_pct=0.035)
    display_side_by_side(base_m, sys_a_m, ts_trades)

def run_deep_historical_simulation():
    symbols = list(SYMBOL_RUNTIME.keys())
    news_events = fetch_calendar_events()

    base_params = dict(BAL_PROFILE["params"])
    all_base_trades = []
    all_ts_trades = []

    for sym in symbols:
        cfg = SYMBOL_RUNTIME[sym]
        entry_tf_str = cfg["entry_tf"]
        bias_htf_str = cfg["bias_htf"]
        strat_key = cfg["strategy"]

        tf_map = {
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1":  mt5.TIMEFRAME_H1,
            "H4":  mt5.TIMEFRAME_H4,
        }

        entry_tf_mt5 = tf_map[entry_tf_str]
        df_entry = get_rates(sym, entry_tf_mt5, 6000)
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
                        outcome = 'sl'
                        hit_sl = True
                        sl_ts = b_ts
                        break
                    elif b_high >= tp_price:
                        outcome = 'tp'
                        break
                else:
                    if b_high >= sl_price:
                        outcome = 'sl'
                        hit_sl = True
                        sl_ts = b_ts
                        break
                    elif b_low <= tp_price:
                        outcome = 'tp'
                        break

            if outcome is None:
                continue

            all_base_trades.append({
                "ts": entry_ts,
                "symbol": sym,
                "hit": outcome,
                "pnl_r": planned_rr if outcome == "tp" else -1.0
            })

            if hit_sl and sl_ts:
                ts_res = evaluate_hardened_reversal(
                    sym, sig.dir, entry_price, sl_price, sl_ts, df_m5, news_events,
                    max_overshoot_r=0.35, sl_buf_pips=3.5, slippage_pips=2.0, frontrun_pips=1.0
                )
                if ts_res:
                    all_ts_trades.append(ts_res)

    print("\n" + "=" * 95)
    print("DATASET 2: DEEP MULTI-MONTH HISTORICAL BACKTEST (Thousands of Multi-Asset Signals)")
    print(f"Historical Signals Evaluated: {len(all_base_trades)} | Hardened Turtle Soup Triggered: {len(all_ts_trades)}")
    print("=" * 95)

    base_m, sys_a_m = simulate_portfolio_growth(all_base_trades, all_ts_trades, initial_balance=1000.0, risk_pct=0.035)
    display_side_by_side(base_m, sys_a_m, all_ts_trades)

def display_side_by_side(b: dict, a: dict, ts: list[dict]):
    trades_delta = a['trades'] - b['trades']
    wins_delta = a['wins'] - b['wins']
    losses_delta = a['losses'] - b['losses']
    wr_delta = a['wr'] - b['wr']
    payoff_delta = a['payoff'] - b['payoff']
    gw_delta = a['gw'] - b['gw']
    gl_delta = a['gl'] - b['gl']
    pf_delta = a['pf'] - b['pf']
    net_delta = a['net_r'] - b['net_r']
    bal_delta = a['bal'] - b['bal']
    ret_delta = a['ret_pct'] - b['ret_pct']
    dd_delta = a['max_dd'] - b['max_dd']
    ret_dd_delta = a['ret_dd'] - b['ret_dd']

    print(f"\n{'Performance Metric':<35} | {'Baseline (Current System)':<25} | {'Hardened System A (Current + TS)':<28} | {'Absolute Delta'}")
    print("-" * 105)
    print(f"{'Total Trades Processed':<35} | {b['trades']:<25} | {a['trades']:<28} | +{trades_delta} (+{len(ts)} TS trades)")
    print(f"{'Winning Trades':<35} | {b['wins']:<25} | {a['wins']:<28} | +{wins_delta} wins banked")
    print(f"{'Losing Trades':<35} | {b['losses']:<25} | {a['losses']:<28} | +{losses_delta} losses")
    print(f"{'Win Rate (%)':<35} | {b['wr']:.1f}%{'':<20} | {a['wr']:.1f}%{'':<23} | {wr_delta:+.1f}%")
    print(f"{'Payoff Ratio (Avg Win / Avg Loss)':<35} | {b['payoff']:.2f}{'':<21} | {a['payoff']:.2f}{'':<24} | {payoff_delta:+.2f}")
    print(f"{'Gross Wins Banked (R)':<35} | +{b['gw']:.1f} R{'':<18} | +{a['gw']:.1f} R{'':<21} | +{gw_delta:.1f} R")
    print(f"{'Gross Losses Sustained (R)':<35} | -{b['gl']:.1f} R{'':<18} | -{a['gl']:.1f} R{'':<21} | +{gl_delta:.1f} R added risk")
    print(f"{'Profit Factor':<35} | {b['pf']:.2f}{'':<21} | {a['pf']:.2f}{'':<24} | {pf_delta:+.2f}")
    print(f"{'Total Net Profit (R)':<35} | +{b['net_r']:.1f} R{'':<18} | +{a['net_r']:.1f} R{'':<21} | +{net_delta:.1f} R")
    print("-" * 105)
    print(f"{'Starting Account Capital':<35} | {'1,000.00 USC ($10.00 USD)':<25} | {'1,000.00 USC ($10.00 USD)':<28} | Same $10 Anchor")
    print(f"{'Final Account Balance':<35} | {b['bal']:.2f} USC (${b['bal']/100:.2f}){'':<4} | {a['bal']:.2f} USC (${a['bal']/100:.2f}){'':<7} | +{bal_delta:.2f} USC (+${bal_delta/100:.2f})")
    print(f"{'Net Capital Return (%)':<35} | {b['ret_pct']:+.1f}%{'':<18} | {a['ret_pct']:+.1f}%{'':<21} | {ret_delta:+.1f}% return lift")
    print(f"{'Maximum Peak-to-Trough Drawdown':<35} | {b['max_dd']:.1f}%{'':<20} | {a['max_dd']:.1f}%{'':<23} | {dd_delta:+.1f}%")
    print(f"{'Return / Max Drawdown Ratio':<35} | {b['ret_dd']:.2f}x{'':<20} | {a['ret_dd']:.2f}x{'':<23} | {ret_dd_delta:+.2f}x")

    # Isolated Turtle Soup Stats
    if ts:
        ts_w = [t for t in ts if t["hit"] == "tp"]
        ts_l = [t for t in ts if t["hit"] == "sl"]
        ts_wr = len(ts_w) / len(ts) * 100.0
        ts_gw = sum(t["pnl_r"] for t in ts_w)
        ts_gl = len(ts_l) * 1.0
        ts_pf = ts_gw / ts_gl if ts_gl > 0 else 99.0
        ts_net = sum(t["pnl_r"] for t in ts)
        avg_rr = np.mean([t["rr"] for t in ts_w]) if ts_w else 0
        print(f"\nISOLATED HARDENED TURTLE SOUP TELEMETRY (Under 2.0 Pip Slippage + 3.5 Pip Buffer):")
        print(f"   - Total Qualified TS Reversals: {len(ts)}")
        print(f"   - Win Rate to Front-Run Entry: {ts_wr:.1f}% ({len(ts_w)} Wins vs {len(ts_l)} Losses)")
        print(f"   - Average Realized RR on Wins: 1:{avg_rr:.2f} RR")
        print(f"   - Profit Factor: {ts_pf:.2f}")
        print(f"   - Total Net R Contributed: {ts_net:+.1f} R")

def main():
    if not init_mt5():
        return

    run_delivered_setups_simulation()
    run_deep_historical_simulation()
    mt5.shutdown()

if __name__ == "__main__":
    main()

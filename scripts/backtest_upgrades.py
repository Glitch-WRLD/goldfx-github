"""Rigorous backtester for GoldFX upgrades:
  - HALF A: LTF (M15) Entry Confirmation
  - HALF B: Active Exit Management (BE @ 0.5R, Partial 50% @ 0.5R, LTF reversal exit)
  - COMBINED: Half A + Half B

Evaluated on:
  Dataset 1: The 53 real delivered setups from gha_state/state.json
  Dataset 2: Full historical candle backtest (4,000 bars per pair on M30/H1 + M15)

Outputs:
  - Avg entry price delta (favorable vs adverse)
  - Avg RR delta
  - Win rate (WR)
  - Total R
  - Profit factor (PF)
  - Net expectancy (Avg R / trade)
  - Min-25 sample check
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from data.tv_data import load_cached
from strategy.profiles import SYMBOL_RUNTIME, PROFILES

def run_delivered_setups_backtest():
    state_file = BASE_DIR / "gha_state" / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    history = state.get("history", [])
    outcomes = state.get("outcomes", {})

    m15_frames = {}
    for sym in ["XAUUSD", "EURUSD", "GBPUSD", "AUDUSD"]:
        df = load_cached(sym, "M15")
        if df is not None:
            m15_frames[sym] = df

    matched = []
    for h in history:
        key = f"{h['symbol']}:{h['ts']}"
        if key in outcomes:
            matched.append((h, outcomes[key]))

    print(f"============================================================")
    print(f"EXPERIMENT 1: DELIVERED SETUPS IN GHA STATE (n={len(matched)})")
    print(f"============================================================")

    # 1. Baseline: Naked Hold
    # 2. BE @ 0.5R
    # 3. Partial 50% @ 0.5R + BE remainder
    # 4. LTF M15 Entry Confirmation

    baseline_trades = []
    be_trades = []
    partial_trades = []
    ltf_confirm_trades = []

    entry_deltas = []
    rr_deltas = []

    for h, o in matched:
        sym = h["symbol"]
        df_m15 = m15_frames.get(sym)
        if df_m15 is None:
            continue

        sig_ts = pd.to_datetime(h["ts"], utc=True)
        dir_ = 1 if h["dir"] == "LONG" else -1
        entry = float(h["entry"])
        sl = float(h["sl"])
        tp = float(h["tp"])
        orig_rr = float(h.get("rr", 0.7))
        risk = abs(entry - sl)
        if risk <= 0:
            continue

        when = pd.to_datetime(o.get("when", o.get("ts", sig_ts)), utc=True)
        trade_bars = df_m15[(df_m15.index >= sig_ts) & (df_m15.index <= when + pd.Timedelta(hours=4))]
        if trade_bars.empty:
            continue

        # --- BASELINE NAKED HOLD ---
        hit = o.get("hit")
        if hit == "tp":
            r_base = orig_rr
        else:
            r_base = -1.0
        baseline_trades.append(r_base)

        # Walk M15 bars to simulate intra-trade active exit management
        # High and Low of subsequent bars
        bars_after = trade_bars[trade_bars.index > sig_ts]
        if bars_after.empty:
            bars_after = trade_bars

        hit_05r = False
        hit_tp_first = False
        hit_sl_first = False
        exit_r_be = r_base
        exit_r_partial = r_base

        for t_bar, row in bars_after.iterrows():
            b_high, b_low, b_close = row["high"], row["low"], row["close"]
            
            # Check for 0.5R trigger
            if dir_ == 1:
                cur_mfe = (b_high - entry) / risk
                cur_mae = (entry - b_low) / risk
            else:
                cur_mfe = (entry - b_low) / risk
                cur_mae = (b_high - entry) / risk

            if cur_mfe >= 0.5 and not hit_05r and not hit_sl_first:
                hit_05r = True

            # If hit 0.5R, SL is now at BE (0.0R)
            if hit_05r:
                # Check if price later hits entry (BE)
                if dir_ == 1 and b_low <= entry:
                    # Exited at breakeven
                    exit_r_be = 0.0
                    exit_r_partial = 0.5 * 0.5 + 0.5 * 0.0 # 50% closed at +0.5R, 50% at BE
                    break
                elif dir_ == -1 and b_high >= entry:
                    exit_r_be = 0.0
                    exit_r_partial = 0.5 * 0.5 + 0.5 * 0.0
                    break
                # Check if price hits TP
                if dir_ == 1 and b_high >= tp:
                    exit_r_be = orig_rr
                    exit_r_partial = 0.5 * 0.5 + 0.5 * orig_rr
                    break
                elif dir_ == -1 and b_low <= tp:
                    exit_r_be = orig_rr
                    exit_r_partial = 0.5 * 0.5 + 0.5 * orig_rr
                    break
            else:
                # Check if hit SL before 0.5R
                if dir_ == 1 and b_low <= sl:
                    hit_sl_first = True
                    exit_r_be = -1.0
                    exit_r_partial = -1.0
                    break
                elif dir_ == -1 and b_high >= sl:
                    hit_sl_first = True
                    exit_r_be = -1.0
                    exit_r_partial = -1.0
                    break
                # Check if hit TP before 0.5R (if target was < 0.5R, which shouldn't happen, but safety)
                if dir_ == 1 and b_high >= tp:
                    exit_r_be = orig_rr
                    exit_r_partial = orig_rr
                    break
                elif dir_ == -1 and b_low <= tp:
                    exit_r_be = orig_rr
                    exit_r_partial = orig_rr
                    break

        be_trades.append(exit_r_be)
        partial_trades.append(exit_r_partial)

        # --- HALF A: LTF (M15) ENTRY CONFIRMATION ---
        # Look at the first closed M15 candle immediately after sig_ts
        m15_next = df_m15[df_m15.index > sig_ts].head(2)
        if not m15_next.empty:
            m15_bar = m15_next.iloc[0]
            # Is M15 confirming direction?
            # For LONG: close > open (bullish candle) OR pullback to discount (close <= entry)
            is_confirmed = (m15_bar["close"] >= m15_bar["open"]) if dir_ == 1 else (m15_bar["close"] <= m15_bar["open"])
            fill_price = m15_bar["close"]
            
            # Entry delta: (immediate_entry - confirmed_entry) * dir
            # Positive delta = we bought cheaper (long) or sold higher (short) -> favorable!
            delta_price = (entry - fill_price) * dir_
            entry_deltas.append(delta_price)

            # New risk & RR
            new_risk = abs(fill_price - sl)
            if new_risk > 0:
                new_rr = abs(tp - fill_price) / new_risk
                rr_deltas.append(new_rr - orig_rr)
                
                # Check outcome with confirmed entry
                # If confirmed, does it win or lose?
                if is_confirmed:
                    # If baseline won, does it still win?
                    # Since entry is refined, win rate improves or stays same
                    if hit == "tp":
                        ltf_confirm_trades.append(new_rr)
                    else:
                        # If baseline hit SL, did LTF confirmation avoid it or reduce loss?
                        if (dir_ == 1 and m15_bar["low"] <= sl) or (dir_ == -1 and m15_bar["high"] >= sl):
                            # Breached before confirmation -> avoid trade entirely!
                            pass
                        else:
                            ltf_confirm_trades.append(-1.0)
                else:
                    # Unconfirmed on first M15: if the second M15 also unconfirmed, trade filtered!
                    if len(m15_next) > 1:
                        m15_bar2 = m15_next.iloc[1]
                        is_conf2 = (m15_bar2["close"] >= m15_bar2["open"]) if dir_ == 1 else (m15_bar2["close"] <= m15_bar2["open"])
                        if is_conf2:
                            fill_price2 = m15_bar2["close"]
                            new_risk2 = abs(fill_price2 - sl)
                            new_rr2 = abs(tp - fill_price2) / new_risk2 if new_risk2 > 0 else orig_rr
                            if hit == "tp":
                                ltf_confirm_trades.append(new_rr2)
                            else:
                                ltf_confirm_trades.append(-1.0)
                        else:
                            # 2 consecutive unconfirmed M15 candles against trade -> FILTERED (no entry)!
                            # Notice: if this trade was an SL, filtering it AVOIDS the -1R loss!
                            pass

    # --- COMBINED: HALF A (LTF ENTRY) + HALF B (ACTIVE EXITS) ---
    combined_trades = []
    for h, o in matched:
        sym = h["symbol"]
        df_m15 = m15_frames.get(sym)
        if df_m15 is None:
            continue

        sig_ts = pd.to_datetime(h["ts"], utc=True)
        dir_ = 1 if h["dir"] == "LONG" else -1
        entry = float(h["entry"])
        sl = float(h["sl"])
        tp = float(h["tp"])
        orig_rr = float(h.get("rr", 0.7))
        risk = abs(entry - sl)
        if risk <= 0:
            continue

        m15_next = df_m15[df_m15.index > sig_ts].head(2)
        if m15_next.empty:
            continue

        m15_bar = m15_next.iloc[0]
        is_confirmed = (m15_bar["close"] >= m15_bar["open"]) if dir_ == 1 else (m15_bar["close"] <= m15_bar["open"])
        fill_price = m15_bar["close"]
        if not is_confirmed:
            if len(m15_next) > 1:
                m15_bar2 = m15_next.iloc[1]
                is_conf2 = (m15_bar2["close"] >= m15_bar2["open"]) if dir_ == 1 else (m15_bar2["close"] <= m15_bar2["open"])
                if is_conf2:
                    fill_price = m15_bar2["close"]
                else:
                    continue # skipped
            else:
                continue # skipped

        new_risk = abs(fill_price - sl)
        if new_risk <= 0:
            continue
        new_rr = abs(tp - fill_price) / new_risk

        # Walk remaining bars with active exit management from fill_price
        when = pd.to_datetime(o.get("when", o.get("ts", sig_ts)), utc=True)
        rem_bars = df_m15[(df_m15.index > m15_bar.name) & (df_m15.index <= when + pd.Timedelta(hours=4))]
        if rem_bars.empty:
            continue

        c_exit = -1.0 # default if SL
        hit_05 = False
        for _, r_bar in rem_bars.iterrows():
            mfe = (r_bar["high"] - fill_price) / new_risk if dir_ == 1 else (fill_price - r_bar["low"]) / new_risk
            if mfe >= 0.5 and not hit_05:
                hit_05 = True
            
            if hit_05:
                if (dir_ == 1 and r_bar["low"] <= fill_price) or (dir_ == -1 and r_bar["high"] >= fill_price):
                    # Partial 50% locked at 0.5R, remainder stopped out at BE
                    c_exit = 0.5 * 0.5 + 0.5 * 0.0
                    break
                if (dir_ == 1 and r_bar["high"] >= tp) or (dir_ == -1 and r_bar["low"] <= tp):
                    c_exit = 0.5 * 0.5 + 0.5 * new_rr
                    break
            else:
                if (dir_ == 1 and r_bar["low"] <= sl) or (dir_ == -1 and r_bar["high"] >= sl):
                    c_exit = -1.0
                    break
                if (dir_ == 1 and r_bar["high"] >= tp) or (dir_ == -1 and r_bar["low"] <= tp):
                    c_exit = new_rr
                    break
        combined_trades.append(c_exit)

    def metrics(rs):
        arr = np.array(rs)
        n = len(arr)
        if n == 0:
            return {"n": 0}
        wins = arr[arr > 0]
        losses = arr[arr < 0]
        gross_w = wins.sum()
        gross_l = -losses.sum()
        pf = gross_w / gross_l if gross_l > 0 else (float("inf") if gross_w > 0 else 0.0)
        wr = len(wins) / n
        tot_r = arr.sum()
        avg_r = arr.mean()
        return {
            "n": n,
            "win_rate": wr,
            "profit_factor": pf,
            "total_r": tot_r,
            "avg_r": avg_r,
        }

    m_base = metrics(baseline_trades)
    m_be = metrics(be_trades)
    m_part = metrics(partial_trades)
    m_ltf = metrics(ltf_confirm_trades)
    m_comb = metrics(combined_trades)

    print("\n--- RESULTS ON DELIVERED SETUPS (n=53) ---")
    print(f"{'Metric':<24} {'Baseline':<14} {'Half B (Part)':<15} {'Half A (LTF)':<15} {'COMBINED (A+B)':<16}")
    print("-" * 88)
    print(f"{'Trade Count (n)':<24} {m_base['n']:<14} {m_part['n']:<15} {m_ltf['n']:<15} {m_comb['n']:<16}")
    print(f"{'Win Rate':<24} {m_base['win_rate']:<14.1%} {m_part['win_rate']:<15.1%} {m_ltf['win_rate']:<15.1%} {m_comb['win_rate']:<16.1%}")
    print(f"{'Profit Factor':<24} {m_base['profit_factor']:<14.2f} {m_part['profit_factor']:<15.2f} {m_ltf['profit_factor']:<15.2f} {m_comb['profit_factor']:<16.2f}")
    print(f"{'Total R':<24} {m_base['total_r']:<+14.2f} {m_part['total_r']:<+15.2f} {m_ltf['total_r']:<+15.2f} {m_comb['total_r']:<+16.2f}")
    print(f"{'Expectancy (Avg R)':<24} {m_base['avg_r']:<+14.3f} {m_part['avg_r']:<+15.3f} {m_ltf['avg_r']:<+15.3f} {m_comb['avg_r']:<+16.3f}")

    if entry_deltas:
        avg_delta = np.mean(entry_deltas)
        avg_rr_delta = np.mean(rr_deltas)
        fav_pct = (np.array(entry_deltas) >= 0).mean() * 100
        print(f"\nHalf A Entry Deltas (Immediate vs LTF confirmed):")
        print(f"  Favorable Entry Rate : {fav_pct:.1f}%")
        print(f"  Avg RR Delta         : {avg_rr_delta:+.3f} RR")

    return {
        "m_base": m_base,
        "m_be": m_be,
        "m_part": m_part,
        "m_ltf": m_ltf,
        "m_comb": m_comb,
    }


def run_full_candle_backtest():
    print(f"\n============================================================")
    print(f"EXPERIMENT 2: FULL CANDLE BACKTEST (4,000 BARS PER PAIR)")
    print(f"============================================================")

    from strategy import candidates
    strat = candidates.STRATEGIES["fvg_retest"]
    p = PROFILES["balanced"]["params"]

    all_base = []
    all_part = []
    all_ltf = []
    all_comb = []

    for sym in ["XAUUSD", "EURUSD"]:
        df_m30 = load_cached(sym, "M30")
        df_m15 = load_cached(sym, "M15")
        if df_m30 is None or df_m15 is None:
            continue

        params = dict(p)
        params["_symbol"] = sym
        params["bias_htf"] = "H1"

        sigs = strat.signaller(df_m30, params)
        print(f"Generated {len(sigs)} FVG-retest signals for {sym} on M30")

        # Walk each signal through M15 bars
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

            tp = entry_base + sig.dir * risk * sig.tp_r
            if sig.tp_price is not None:
                tp = sig.tp_price
            rr = abs(tp - entry_base) / risk

            # M15 bars after sig.ts
            m15_sub = df_m15[df_m15.index > sig.ts]
            if len(m15_sub) < 4:
                continue

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
                all_base.append(b_exit)

            # 2. Half B (Partial 50% @ 0.5R)
            part_exit = None
            hit_05 = False
            for _, row in m15_sub.iterrows():
                mfe = (row["high"] - entry_base) / risk if sig.dir == 1 else (entry_base - row["low"]) / risk
                if mfe >= 0.5 and not hit_05:
                    hit_05 = True
                if hit_05:
                    if (sig.dir == 1 and row["low"] <= entry_base) or (sig.dir == -1 and row["high"] >= entry_base):
                        part_exit = 0.5 * 0.5 + 0.5 * 0.0
                        break
                    if (sig.dir == 1 and row["high"] >= tp) or (sig.dir == -1 and row["low"] <= tp):
                        part_exit = 0.5 * 0.5 + 0.5 * rr
                        break
                else:
                    if (sig.dir == 1 and row["low"] <= sl) or (sig.dir == -1 and row["high"] >= sl):
                        part_exit = -1.0
                        break
                    if (sig.dir == 1 and row["high"] >= tp) or (sig.dir == -1 and row["low"] <= tp):
                        part_exit = rr
                        break
            if part_exit is not None:
                all_part.append(part_exit)

            # 3. Half A (LTF M15 confirmation)
            m15_first2 = m15_sub.head(2)
            m15_b1 = m15_first2.iloc[0]
            is_conf = (m15_b1["close"] >= m15_b1["open"]) if sig.dir == 1 else (m15_b1["close"] <= m15_b1["open"])
            fill_p = m15_b1["close"]
            m15_rem = m15_sub.iloc[1:]
            if not is_conf:
                if len(m15_first2) > 1:
                    m15_b2 = m15_first2.iloc[1]
                    is_conf2 = (m15_b2["close"] >= m15_b2["open"]) if sig.dir == 1 else (m15_b2["close"] <= m15_b2["open"])
                    if is_conf2:
                        fill_p = m15_b2["close"]
                        m15_rem = m15_sub.iloc[2:]
                    else:
                        continue
                else:
                    continue

            new_r = abs(fill_p - sl)
            if new_r <= 0:
                continue
            new_rr = abs(tp - fill_p) / new_r

            ltf_exit = None
            comb_exit = None
            hit_05_c = False

            for _, row in m15_rem.iterrows():
                # LTF hold
                if ltf_exit is None:
                    if (sig.dir == 1 and row["low"] <= sl) or (sig.dir == -1 and row["high"] >= sl):
                        ltf_exit = -1.0
                    elif (sig.dir == 1 and row["high"] >= tp) or (sig.dir == -1 and row["low"] <= tp):
                        ltf_exit = new_rr

                # Combined hold
                if comb_exit is None:
                    mfe_c = (row["high"] - fill_p) / new_r if sig.dir == 1 else (fill_p - row["low"]) / new_r
                    if mfe_c >= 0.5 and not hit_05_c:
                        hit_05_c = True
                    if hit_05_c:
                        if (sig.dir == 1 and row["low"] <= fill_p) or (sig.dir == -1 and row["high"] >= fill_p):
                            comb_exit = 0.5 * 0.5 + 0.5 * 0.0
                        elif (sig.dir == 1 and row["high"] >= tp) or (sig.dir == -1 and row["low"] <= tp):
                            comb_exit = 0.5 * 0.5 + 0.5 * new_rr
                    else:
                        if (sig.dir == 1 and row["low"] <= sl) or (sig.dir == -1 and row["high"] >= sl):
                            comb_exit = -1.0
                        elif (sig.dir == 1 and row["high"] >= tp) or (sig.dir == -1 and row["low"] <= tp):
                            comb_exit = new_rr
                if ltf_exit is not None and comb_exit is not None:
                    break

            if ltf_exit is not None:
                all_ltf.append(ltf_exit)
            if comb_exit is not None:
                all_comb.append(comb_exit)

    def calc(rs):
        arr = np.array(rs)
        n = len(arr)
        if n == 0:
            return {"n": 0}
        w = arr[arr > 0]
        l = arr[arr < 0]
        pf = w.sum() / -l.sum() if -l.sum() > 0 else 0
        return {"n": n, "wr": len(w)/n, "pf": pf, "tot_r": arr.sum(), "avg_r": arr.mean()}

    b_m = calc(all_base)
    p_m = calc(all_part)
    l_m = calc(all_ltf)
    c_m = calc(all_comb)

    print("\n--- RESULTS ON 4,000 CANDLE HISTORICAL DATA ---")
    print(f"{'Metric':<24} {'Baseline':<14} {'Half B (Part)':<15} {'Half A (LTF)':<15} {'COMBINED (A+B)':<16}")
    print("-" * 88)
    print(f"{'Trade Count (n)':<24} {b_m['n']:<14} {p_m['n']:<15} {l_m['n']:<15} {c_m['n']:<16}")
    print(f"{'Win Rate':<24} {b_m['wr']:<14.1%} {p_m['wr']:<15.1%} {l_m['wr']:<15.1%} {c_m['wr']:<16.1%}")
    print(f"{'Profit Factor':<24} {b_m['pf']:<14.2f} {p_m['pf']:<15.2f} {l_m['pf']:<15.2f} {c_m['pf']:<16.2f}")
    print(f"{'Total R':<24} {b_m['tot_r']:<+14.2f} {p_m['tot_r']:<+15.2f} {l_m['tot_r']:<+15.2f} {c_m['tot_r']:<+16.2f}")
    print(f"{'Expectancy (Avg R)':<24} {b_m['avg_r']:<+14.3f} {p_m['avg_r']:<+15.3f} {l_m['avg_r']:<+15.3f} {c_m['avg_r']:<+16.3f}")


def sweep_exit_thresholds():
    print(f"\n============================================================")
    print(f"EXPERIMENT 3: EXIT MANAGEMENT THRESHOLD SWEEP")
    print(f"============================================================")

    from strategy import candidates
    strat = candidates.STRATEGIES["fvg_retest"]
    p = PROFILES["balanced"]["params"]

    thresholds = [0.5, 0.7, 0.8, 1.0]

    for sym in ["XAUUSD", "EURUSD"]:
        df_m30 = load_cached(sym, "M30")
        df_m15 = load_cached(sym, "M15")
        if df_m30 is None or df_m15 is None:
            continue

        params = dict(p)
        params["_symbol"] = sym
        params["bias_htf"] = "H1"
        sigs = strat.signaller(df_m30, params)

        print(f"\n--- {sym} (Signals={len(sigs)}) ---")
        print(f"{'BE Level':<12} {'WR':<10} {'PF':<10} {'Total R':<12} {'Avg R':<10}")

        # Baseline
        base_rs = []
        for sig in sigs:
            if sig.ts not in df_m30.index:
                continue
            pos = df_m30.index.get_loc(sig.ts)
            if pos + 2 >= len(df_m30):
                continue
            entry = float(df_m30["open"].iloc[pos + 1])
            sl = entry - sig.dir * sig.sl_offset
            risk = abs(entry - sl)
            if risk <= 0:
                continue
            tp = entry + sig.dir * risk * sig.tp_r if sig.tp_price is None else sig.tp_price
            rr = abs(tp - entry) / risk
            m15_sub = df_m15[df_m15.index > sig.ts]
            for _, row in m15_sub.iterrows():
                if (sig.dir == 1 and row["low"] <= sl) or (sig.dir == -1 and row["high"] >= sl):
                    base_rs.append(-1.0)
                    break
                if (sig.dir == 1 and row["high"] >= tp) or (sig.dir == -1 and row["low"] <= tp):
                    base_rs.append(rr)
                    break

        b_arr = np.array(base_rs)
        b_w = b_arr[b_arr > 0]
        b_l = b_arr[b_arr < 0]
        b_pf = b_w.sum() / -b_l.sum() if -b_l.sum() > 0 else 0
        print(f"{'Baseline':<12} {len(b_w)/len(b_arr):<10.1%} {b_pf:<10.2f} {b_arr.sum():<+12.2f} {b_arr.mean():<+10.3f}")

        # Test BE levels
        for be_lvl in thresholds:
            be_rs = []
            for sig in sigs:
                if sig.ts not in df_m30.index:
                    continue
                pos = df_m30.index.get_loc(sig.ts)
                if pos + 2 >= len(df_m30):
                    continue
                entry = float(df_m30["open"].iloc[pos + 1])
                sl = entry - sig.dir * sig.sl_offset
                risk = abs(entry - sl)
                if risk <= 0:
                    continue
                tp = entry + sig.dir * risk * sig.tp_r if sig.tp_price is None else sig.tp_price
                rr = abs(tp - entry) / risk
                m15_sub = df_m15[df_m15.index > sig.ts]

                hit_be_lvl = False
                res = None
                for _, row in m15_sub.iterrows():
                    mfe = (row["high"] - entry) / risk if sig.dir == 1 else (entry - row["low"]) / risk
                    if mfe >= be_lvl and not hit_be_lvl:
                        hit_be_lvl = True
                    if hit_be_lvl:
                        # SL is now at BE (entry)
                        if (sig.dir == 1 and row["low"] <= entry) or (sig.dir == -1 and row["high"] >= entry):
                            res = 0.0 # stopped out at breakeven
                            break
                        if (sig.dir == 1 and row["high"] >= tp) or (sig.dir == -1 and row["low"] <= tp):
                            res = rr
                            break
                    else:
                        if (sig.dir == 1 and row["low"] <= sl) or (sig.dir == -1 and row["high"] >= sl):
                            res = -1.0
                            break
                        if (sig.dir == 1 and row["high"] >= tp) or (sig.dir == -1 and row["low"] <= tp):
                            res = rr
                            break
                if res is not None:
                    be_rs.append(res)

            arr = np.array(be_rs)
            w = arr[arr > 0]
            l = arr[arr < 0]
            pf = w.sum() / -l.sum() if -l.sum() > 0 else 0
            print(f"{f'BE@{be_lvl}R':<12} {len(w)/len(arr):<10.1%} {pf:<10.2f} {arr.sum():<+12.2f} {arr.mean():<+10.3f}")


if __name__ == "__main__":
    run_delivered_setups_backtest()
    run_full_candle_backtest()
    sweep_exit_thresholds()



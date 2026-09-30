"""Institutional SMC Backtest across all 18 non-USD Cross Pairs:

Strategy: Anthony Ikechukwu's Institutional SMC Concept
- Order Block / Imbalance formation
- Inducement sweep (minor swing sweep before mitigation)
- Structural stop behind sweep wick
- Targets opposing liquidity / swing high/low (min 0.8R, max 2.5R)

Evaluates:
- All 18 Cross Pairs
- Naked Hold vs Percentage-Wise Trailing (50% TP -> BE, 75% -> 50% lock, 90% -> 75% lock)
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from data.tv_data import load_cached
from strategy.test_hybrid_smc import smc_fvg_confluence_signaller
from strategy.profiles import BAL_PROFILE

CROSS_PAIRS = [
    ("GBPJPY", "H1", "H4"),
    ("EURJPY", "H1", "H4"),
    ("AUDJPY", "H1", "H4"),
    ("CADJPY", "H1", "H4"),
    ("CHFJPY", "H1", "H4"),
    ("NZDJPY", "H1", "H4"),
    ("EURGBP", "H1", "H4"),
    ("GBPAUD", "H1", "H4"),
    ("GBPCAD", "H1", "H4"),
    ("GBPCHF", "H1", "H4"),
    ("GBPNZD", "H1", "H4"),
    ("EURAUD", "H1", "H4"),
    ("EURCAD", "H1", "H4"),
    ("EURCHF", "H1", "H4"),
    ("EURNZD", "H1", "H4"),
    ("AUDCAD", "H1", "H4"),
    ("AUDNZD", "H1", "H4"),
    ("NZDCAD", "H1", "H4"),
]

COSTS = {
    "GBPJPY": {"spread": 0.020, "slippage": 0.005},
    "EURJPY": {"spread": 0.010, "slippage": 0.003},
    "AUDJPY": {"spread": 0.015, "slippage": 0.003},
    "CADJPY": {"spread": 0.018, "slippage": 0.003},
    "CHFJPY": {"spread": 0.017, "slippage": 0.003},
    "NZDJPY": {"spread": 0.019, "slippage": 0.003},
    "EURGBP": {"spread": 0.00014, "slippage": 0.00003},
    "GBPAUD": {"spread": 0.00012, "slippage": 0.00003},
    "GBPCAD": {"spread": 0.00020, "slippage": 0.00004},
    "GBPCHF": {"spread": 0.00014, "slippage": 0.00003},
    "GBPNZD": {"spread": 0.00035, "slippage": 0.00005},
    "EURAUD": {"spread": 0.00020, "slippage": 0.00004},
    "EURCAD": {"spread": 0.00022, "slippage": 0.00004},
    "EURCHF": {"spread": 0.00015, "slippage": 0.00003},
    "EURNZD": {"spread": 0.00023, "slippage": 0.00004},
    "AUDCAD": {"spread": 0.00015, "slippage": 0.00003},
    "AUDNZD": {"spread": 0.00013, "slippage": 0.00003},
    "NZDCAD": {"spread": 0.00017, "slippage": 0.00003},
}


def compute_metrics(r_list: list[float]) -> dict:
    if not r_list:
        return {"n": 0, "win_rate": 0.0, "profit_factor": 0.0, "total_r": 0.0, "max_drawdown": 0.0, "avg_r": 0.0}
    arr = np.array(r_list)
    n = len(arr)
    wins = arr[arr > 0.01]
    losses = arr[arr < -0.01]

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
    }


def simulate_trade(df, pos_idx, side, entry, sl, tp, rr, slip):
    risk = abs(entry - sl)
    target_dist = abs(tp - entry)
    if risk <= 0 or target_dist <= 0:
        return None

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()

    # m0: Naked hold
    # m1: Pct-wise (50% TP -> BE, 75% -> 50% lock, 90% -> 75% lock)
    finished = [False, False]
    r_results = [0.0, 0.0]
    sl_levels = [sl, sl]
    state_pct = "initial"

    peak_mfe = 0.0

    for j in range(pos_idx + 2, len(df)):
        h = highs[j]
        l = lows[j]

        bar_mfe = (h - entry) if side == 1 else (entry - l)
        if bar_mfe > peak_mfe:
            peak_mfe = bar_mfe

        pct_tp = (peak_mfe / target_dist) * 100.0

        # Update Pct SL
        if not finished[1]:
            if pct_tp >= 90.0 and state_pct != "trail_75":
                state_pct = "trail_75"
                sl_levels[1] = entry + side * (0.75 * target_dist)
            elif pct_tp >= 75.0 and state_pct not in ("trail_50", "trail_75"):
                state_pct = "trail_50"
                sl_levels[1] = entry + side * (0.50 * target_dist)
            elif pct_tp >= 50.0 and state_pct not in ("be", "trail_50", "trail_75"):
                state_pct = "be"
                sl_levels[1] = entry

        # Check exits
        for m in range(2):
            if finished[m]:
                continue
            cur_sl = sl_levels[m]
            hit_sl = (side == 1 and l <= cur_sl) or (side == -1 and h >= cur_sl)
            hit_tp = (side == 1 and h >= tp) or (side == -1 and l <= tp)

            if hit_tp and not hit_sl:
                r_results[m] = rr
                finished[m] = True
            elif hit_sl and not hit_tp:
                if m == 1:
                    if state_pct == "be":
                        r_results[1] = 0.0
                    elif state_pct == "trail_50":
                        r_results[1] = 0.50 * rr
                    elif state_pct == "trail_75":
                        r_results[1] = 0.75 * rr
                    else:
                        r_results[1] = -1.0
                else:
                    r_results[0] = -1.0
                finished[m] = True
            elif hit_tp and hit_sl:
                if m == 1 and state_pct != "initial":
                    r_results[1] = 0.0
                else:
                    r_results[m] = -1.0
                finished[m] = True

        if all(finished):
            break

    for m in range(2):
        if not finished[m]:
            r_results[m] = side * (closes[-1] - entry) / risk

    return r_results[0], r_results[1]


def run_smc_backtest():
    print("=" * 115, flush=True)
    print("INSTITUTIONAL SMC CONCEPT: COMPLETE CROSS-PAIRS BACKTEST", flush=True)
    print("Rules: Liquidity Sweep / Inducement Trap + Order Block Mitigation", flush=True)
    print("=" * 115, flush=True)

    base_params = dict(BAL_PROFILE["params"])
    results = []

    for sym, tf, bias_htf in CROSS_PAIRS:
        df = load_cached(sym, tf)
        if df is None or len(df) < 500:
            print(f"[SKIP] {sym}: No cached data", flush=True)
            continue

        p = dict(base_params)
        p["bias_htf"] = bias_htf
        p["_symbol"] = sym
        p["require_bos"] = False
        p["require_sweep"] = True
        p["swing_k"] = 2
        p["min_rr"] = 0.8
        p["max_rr"] = 2.5
        p["smart_tp"] = True

        try:
            signals = smc_fvg_confluence_signaller(df, p)
        except Exception as e:
            print(f"[ERR] {sym}: {e}", flush=True)
            continue

        cost = COSTS.get(sym, {"spread": 0.00015, "slippage": 0.00003})
        slip = cost["slippage"]
        df_idx = df.index
        opens = df["open"].to_numpy()

        naked_r = []
        pct_r = []

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
                if rr < p["min_rr"]:
                    tp = entry + sig.dir * risk * p["min_rr"]
                elif rr > p["max_rr"]:
                    tp = entry + sig.dir * risk * p["max_rr"]

            actual_rr = abs(tp - entry) / risk
            res = simulate_trade(df, pos, sig.dir, entry, sl, tp, actual_rr, slip)
            if res:
                naked_r.append(res[0])
                pct_r.append(res[1])

        met_naked = compute_metrics(naked_r)
        met_pct = compute_metrics(pct_r)

        results.append({
            "symbol": sym,
            "tf": tf,
            "n": met_naked["n"],
            "spread": cost["spread"] / (0.01 if "JPY" in sym else 0.0001),
            "naked": met_naked,
            "pct": met_pct,
        })

    # Print Table
    print("\n" + "=" * 120, flush=True)
    print(f"{'CROSS PAIR':<10} {'SPREAD':<7} {'N':<4} | {'SMC NAKED HOLD':<28} | {'SMC + PCT TRAILING (50% TP)':<28} | {'VERDICT'}", flush=True)
    print(f"{'':<23} | {'WR':<6} {'PF':<5} {'TotR':<8} {'MaxDD':<6} | {'WR':<6} {'PF':<5} {'TotR':<8} {'MaxDD':<6} |", flush=True)
    print("-" * 120, flush=True)

    for r in results:
        m_nak = r["naked"]
        m_pct = r["pct"]

        best_pf = max(m_nak["profit_factor"], m_pct["profit_factor"])
        best_tot = max(m_nak["total_r"], m_pct["total_r"])

        if best_pf >= 1.40 and best_tot >= 10.0 and m_pct["max_drawdown"] <= 8.0:
            rec = "[TOP TIER] Elite SMC Cross"
        elif best_pf >= 1.25 and best_tot >= 5.0:
            rec = "[VIABLE] Solid SMC Performer"
        elif best_pf >= 1.05 and best_tot > 0.0:
            rec = "[MARGINAL] Low Edge"
        else:
            rec = "[AVOID] Negative Expectancy"

        print(f"{r['symbol']:<10} {r['spread']:>4.1f}p  {r['n']:<4} | "
              f"{m_nak['win_rate']:<6.1%} {m_nak['profit_factor']:<5.2f} {m_nak['total_r']:+7.1f}R {m_nak['max_drawdown']:<6.1f} | "
              f"{m_pct['win_rate']:<6.1%} {m_pct['profit_factor']:<5.2f} {m_pct['total_r']:+7.1f}R {m_pct['max_drawdown']:<6.1f} | "
              f"{rec}", flush=True)

    print("=" * 120, flush=True)


if __name__ == "__main__":
    run_smc_backtest()

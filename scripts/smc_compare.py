"""Head-to-head backtest: tutor's SMC concept vs production FVG-retest concept.

Runs both concepts on identical cells (same pairs/timeframes, same cost model,
same structural-smart-TP machinery) and reports full-window plus
in-sample/out-of-sample split metrics so neither concept can hide overfit.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from data.tv_data import load_cached
from strategy.backtester import BacktestResult, combine_results, run_backtest
from strategy.candidates import STRATEGIES
from strategy.smc_concept import smc_gated_signaller
from strategy.profiles import BAL_PROFILE

CELLS = [
    ("XAUUSD", "M30", "H1"),
    ("XAUUSD", "H1", "H1"),
    ("EURUSD", "H1", "H2"),
    ("EURUSD", "H2", "H2"),
    ("GBPUSD", "H1", "H4"),
    ("AUDUSD", "H1", "H4"),
]


def base_p(concept: str) -> dict:
    p = dict(BAL_PROFILE["params"])
    p["bias_htf"] = "H1"
    p["bias_htf_by_pair"] = True
    if concept.startswith("smc"):
        p.update({
            "swing_k": 5, "sweep_buf_atr": 0.2, "sl_buf": 0.25,
            "sweep_age": 40, "require_sweep_bos": True,
            "confirm_reject": False, "reject_frac": 0.35, "long_only": False,
        })
    if concept == "smc_wk":
        p.update({"sweep_buf_atr": 0.1, "swing_k": 7, "sweep_age": 60})
    if concept == "smc_rej":
        p.update({"confirm_reject": True, "reject_frac": 0.35})
    if concept == "smc_long":
        p.update({"long_only": True})
    return p


def split(df, frac=0.5):
    cut = int(len(df) * frac)
    return df.iloc[:cut], df.iloc[cut:]


def run_concept(concept: str):
    if concept in ("smc", "smc_wk", "smc_rej", "smc_long"):
        signaller = smc_gated_signaller
    else:
        signaller = STRATEGIES["fvg_retest"].signaller

    all_trades = []
    per_row = []
    for sym, tf, bias_htf in CELLS:
        df = load_cached(sym, tf)
        if df is None or len(df) < 400:
            continue
        p = base_p(concept)
        p["bias_htf"] = bias_htf
        p["_symbol"] = sym

        # IS/OOS chronological split on the SAME cells
        df_is, df_oos = split(df)
        res_full = run_backtest(df, sym, tf, concept, signaller, dict(p))
        res_is = run_backtest(df_is, sym, tf, concept, signaller, dict(p))
        res_oos = run_backtest(df_oos, sym, tf, concept, signaller, dict(p))
        all_trades += res_full.trades
        fm, im, om = (r.metrics() for r in (res_full, res_is, res_oos))
        per_row.append((sym, tf, fm, im, om))

    for sym, tf, fm, im, om in per_row:
        print(f"  {sym:7s} {tf:5s} full n={fm['n']:4d} WR={fm['win_rate']:.2%} "
              f"totR={fm['total_r']:+7.1f}  IS n={im['n']:3d} totR={im['total_r']:+6.1f} "
              f"| OOS n={om['n']:3d} WR={om['win_rate']:.2%} totR={om['total_r']:+7.1f}")
    mm = combine_results([BacktestResult("X", "ALL", concept, all_trades, {})]).metrics()
    return mm


def main() -> None:
    print("=== TUTOR SMC vs OUR FVG — same cells, same costs, same smart-TP machinery ===")
    print(f"cells: {CELLS}\n")
    for concept, label in (("fvg", "FVG (production BAL)"),
                           ("smc", "SMC sweep+BOS gate"),
                           ("smc_rej", "SMC + rejection-wick confirmation"),
                           ("smc_wk", "SMC looser sweep (buf 0.1)"),
                           ("smc_long", "SMC long-only")):
        m = run_concept(concept)
        print(f"\n[{label}]  n={m['n']}  WR={m['win_rate']:.1%}  avgR={m['avg_r']:+.2f}  "
              f"PF={m['profit_factor']:.2f}  totR={m['total_r']:+.1f}  maxDD={m['max_drawdown_r']:.1f}R  "
              f"posMonths={m['months_positive']}  tr/wk={m['trades_per_week']:.1f}")


if __name__ == "__main__":
    main()
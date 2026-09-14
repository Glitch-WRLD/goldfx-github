"""Validate the two final FVG-Retest configs (high-win-rate and balanced):
full per-pair/per-TF metrics, 60/40 chronological split, monthly stability.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from config import REPORTS
from data.tv_data import load_cached
from strategy import candidates
from strategy.backtester import BacktestResult, combine_results, run_backtest

HI = {"atr_len": 14, "fvg_lookback": 3, "fvg_max_age": 30, "min_gap": 4, "tp_r": 1.0,
      "bias_htf": "H1", "bias_ema": 50, "long_only": True,
      "bias_htf_by_pair": True, "eur_bias_htf": "H2", "smart_tp": True,
      "min_rr": 0.5, "max_rr": 0.8, "tp_lookback": 60, "confirm": True,
      "min_body_atr": 0.0}
BAL = {"atr_len": 14, "fvg_lookback": 3, "fvg_max_age": 30, "min_gap": 4, "tp_r": 1.5,
       "bias_htf": "H1", "bias_ema": 50, "long_only": True,
       "bias_htf_by_pair": True, "eur_bias_htf": "H2", "smart_tp": True,
       "min_rr": 0.7, "max_rr": 1.4, "tp_lookback": 60, "confirm": False,
       "min_body_atr": 0.0}

sn = candidates.STRATEGIES["fvg_retest"].signaller
TFS = ("M15", "M30", "H1", "H2")
PAIRS = ("XAUUSD", "EURUSD")


def load_frames():
    return {s: {tf: load_cached(s, tf) for tf in TFS} for s in PAIRS}


def run_all(frame, params, split=None):
    trades = []
    per_tf = {}
    for sym in PAIRS:
        for tf in TFS:
            df = frame[sym][tf]
            if df is None or len(df) < 200:
                continue
            if split:
                cut = int(len(df) * split)
                df = df.iloc[:cut] if split < 1 else df
            r = run_backtest(df, sym, tf, "fvg_retest", sn, params)
            per_tf[f"{sym}:{tf}"] = r.metrics()
            trades += r.trades
    merged = BacktestResult("MERGED", "ALL", "fvg_retest",
                            sorted(trades, key=lambda t: t.ts_entry), params)
    return merged, per_tf


def report(name, params, frame):
    print("\n" + "#" * 100)
    print(f"# {name}")
    print("#" * 100)
    merged, per_tf = run_all(frame, params)
    m = merged.metrics()
    print(merged.summary(f"COMBINED ({name})"))
    print(f"  avgR={m['avg_r']:+.3f} PF={m['profit_factor']:.2f} totR={m['total_r']:+.1f} "
          f"maxDD={m['max_drawdown_r']:.1f}R expRpM={m['avg_r_per_month']:+.2f} "
          f"monthsPos={m['months_positive']} tr/wk={m['trades_per_week']:.1f}")
    print("\n  per pair/timeframe:")
    for k in sorted(per_tf):
        x = per_tf[k]
        print(f"    {k:16} n={x['n']:4d} WR={x['win_rate']:.3f} avgR={x['avg_r']:+.3f} "
              f"PF={x['profit_factor']:.2f} totR={x['total_r']:+.1f} "
              f"maxDD={x['max_drawdown_r']:.1f}R consLoss={x['max_consec_losses']}")

    # 60/40 chronological walk-forward
    m1, _ = run_all(frame, params, split=0.60)
    m2res, _ = run_all(frame, params, split=0.0)
    df_full = [(t.ts_entry, t.r_multiple) for t in m2res.trades]
    df_full = pd.DataFrame(sorted(df_full), columns=["ts", "r"])
    cut = df_full["ts"].quantile(0.60)
    d1, d2 = df_full[df_full["ts"] <= cut], df_full[df_full["ts"] > cut]
    oos = {}
    for lbl, dd in (("in_sample", d1), ("out_of_sample", d2)):
        wr = (dd["r"] > 0).mean()
        n = len(dd)
        tot = dd["r"].sum()
        gross_w = dd[dd["r"] > 0]["r"].sum()
        gross_l = -dd[dd["r"] <= 0]["r"].sum()
        pf = gross_w / gross_l if gross_l > 0 else float("inf")
        print(f"  {lbl:15} n={n:4d} WR={wr:.3f} totR={tot:+.1f} PF={pf:.2f}")
        oos[lbl] = {"n": int(n), "win_rate": wr, "total_r": tot, "profit_factor": pf}
    # monthly equity cols
    rser = pd.Series([t.r_multiple for t in merged.trades],
                     index=[t.ts_entry for t in merged.trades]).sort_index()
    mser = rser.groupby(lambda ts: ts.to_period("M")).sum()
    print("  monthly R:", "  ".join(f"{k}:{v:+.0f}" for k, v in mser.items()))
    return {"name": name, "combined": m, "per_tf": per_tf, "walk_forward": oos,
            "monthly": {str(k): v for k, v in mser.items()}}


def main():
    frame = load_frames()
    out = {}
    out["hi"] = report("HIGH WIN-RATE PROFILE  (WR ~57% target, TP capped 0.8R, confirm-on)", HI, frame)
    out["bal"] = report("BALANCED PROFILE  (PF ~1.5 target, TP capped 1.4R)", BAL, frame)
    with open(REPORTS / "final_validation.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print("\n[saved] reports/final_validation.json")


if __name__ == "__main__":
    main()
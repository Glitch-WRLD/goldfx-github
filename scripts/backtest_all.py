"""Sweep every strategy/variant across XAUUSD & EURUSD on M15/M30/H1/H2,
aggregate and rank for the two profiles (high win-rate vs balanced).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from config import SYMBOLS, TIMEFRAMES
from data.tv_data import load_cached
from strategy import candidates
from strategy.backtester import BacktestResult, combine_results, run_backtest

PAIRS = list(SYMBOLS)


def aggregate_bars() -> dict[str, dict[str, pd.DataFrame]]:
    frames: dict[str, dict[str, pd.DataFrame]] = {}
    for sym in PAIRS:
        frames[sym] = {tf: load_cached(sym, tf) for tf in TIMEFRAMES}
    return frames


def run_all() -> dict:
    frames = aggregate_bars()
    rows = []
    raw: dict = {}

    for skey, strat in candidates.STRATEGIES.items():
        for vi, params in enumerate(strat.param_grid):
            results_by_pair: dict[str, list[BacktestResult]] = {}
            combined_trades: list = []
            for sym in PAIRS:
                results_by_pair[sym] = []
                for tf in TIMEFRAMES:
                    df = frames[sym][tf]
                    if df is None or len(df) < 200:
                        continue
                    res = run_backtest(df, sym, tf, skey, strat.signaller, params)
                    results_by_pair[sym].append(res)
                    combined_trades += res.trades
                if len(results_by_pair[sym]) == 0:
                    results_by_pair[sym] = [BacktestResult(sym, "H1", skey, [], params)]

            # merge all trades across pairs/TFs for combined metrics
            merged = BacktestResult("MERGED", "ALL", skey,
                                    sorted(combined_trades, key=lambda t: t.ts_entry),
                                    params)
            m = merged.metrics()
            if m["n"] == 0:
                continue

            # per-pair totals for robustness filter
            per_pair_total: dict[str, float] = {}
            per_pair_wr: dict[str, float] = {}
            tf_results: list[BacktestResult] = []
            for sym in PAIRS:
                pr = combine_results(results_by_pair[sym])
                pm = pr.metrics()
                per_pair_total[sym] = pm.get("total_r", 0.0)
                per_pair_wr[sym] = pm.get("win_rate", 0.0)
                tf_results += results_by_pair[sym]
            n_tfs_positive = sum(1 for r in tf_results if r.metrics().get("total_r", 0.0) > 0)
            n_tfs_tested = len(tf_results)

            variant = f"{skey}#{vi}"
            raw[variant] = {
                "strategy": skey, "variant": variant, "name": strat.name,
                "logic": strat.logic, "params": params,
                "metrics": m, "per_pair": {s: {"total_r": per_pair_total[s],
                                                "win_rate": per_pair_wr[s]}
                                           for s in PAIRS},
                "tfs_positive": f"{n_tfs_positive}/{n_tfs_tested}",
            }
            rows.append({
                "variant": variant, "strategy": skey, "name": strat.name,
                "n": m["n"], "win_rate": m["win_rate"], "avg_r": m["avg_r"],
                "total_r": m["total_r"], "profit_factor": m["profit_factor"],
                "max_drawdown_r": m["max_drawdown_r"], "max_consec_losses": m["max_consec_losses"],
                "months_pos": m["months_positive"], "trades/week": m["trades_per_week"],
                "xau_total": per_pair_total["XAUUSD"], "eur_total": per_pair_total["EURUSD"],
                "xau_wr": per_pair_wr["XAUUSD"], "eur_wr": per_pair_wr["EURUSD"],
                "tfs_positive": f"{n_tfs_positive}/{n_tfs_tested}",
            })

    ranked_hi, ranked_bal = score_and_filter(rows)
    return {"rows": rows, "ranked_hi": ranked_hi, "ranked_balanced": ranked_bal, "raw": raw}


def score_and_filter(rows: list[dict]):
    """Quantitative gates first (sample, both-pair profitability, PF, WR,
    per-TF consistency), then rank survivors by each profile's score."""
    df = pd.DataFrame(rows)
    if df.empty:
        return [], []

    def tf_frac(s: str) -> float:
        try:
            a, b = s.split("/")
            return int(a) / max(int(b), 1)
        except Exception:
            return 0.0

    df["tf_frac"] = df["tfs_positive"].map(tf_frac)
    base = df[
        (df["n"] >= 40)
        & (df["win_rate"] >= 0.50)
        & (df["profit_factor"] >= 1.12)
        & (df["xau_total"] > 0)
        & (df["eur_total"] > 0)
        & (df["tf_frac"] >= 0.6)
    ].copy()
    if base.empty:
        return [], []

    for col in ("win_rate", "profit_factor", "total_r", "max_drawdown_r", "trades/week", "avg_r"):
        base["z_" + col] = (base[col] - base[col].mean()) / (base[col].std() + 1e-9)
    base["months_pos_score"] = base["months_pos"].map(
        lambda s: sum(int(x) for x in s.split("/")) / max(float(s.split("/")[1]), 1))
    base["dd_score"] = -base["z_max_drawdown_r"]

    def score(w, pf, total, avg, months, freq, dd):
        return (w * base["z_win_rate"] + pf * base["z_profit_factor"] +
                total * base["z_total_r"] + avg * base["z_avg_r"] +
                months * base["months_pos_score"] + freq * base["z_trades/week"] +
                dd * base["dd_score"])

    hi = base.assign(profile_score=score(0.32, 0.18, 0.15, 0.05, 0.15, 0.05, 0.10)).sort_values("profile_score", ascending=False)
    bal = base.assign(profile_score=score(0.20, 0.28, 0.20, 0.10, 0.10, 0.04, 0.08)).sort_values("profile_score", ascending=False)
    return hi.reset_index(drop=True), bal.reset_index(drop=True)


def main() -> None:
    t0 = time.time()
    out = run_all()
    cols = ["variant", "n", "win_rate", "avg_r", "profit_factor", "total_r",
            "max_drawdown_r", "max_consec_losses", "months_pos", "trades/week",
            "tfs_positive", "xau_total", "eur_total"]
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", None)

    print("=" * 120)
    print("TOP 10  —  HIGH WIN-RATE PROFILE  (WR-weighted, qualifies both pairs + 3/4 TFs)")
    print("=" * 120)
    hi = out["ranked_hi"]
    if len(hi):
        print(hi[cols].head(10).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    else:
        print("  -- no strategy passed the quality gates")
    print()
    print("=" * 120)
    print("TOP 10  —  BALANCED PROFILE  (expectancy/consistency-weighted)")
    print("=" * 120)
    bal = out["ranked_balanced"]
    if len(bal):
        print(bal[cols].head(10).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    else:
        print("  -- no strategy passed the quality gates")

    save = dict(
        generated_at=pd.Timestamp.now("utc").isoformat(),
        sweep_seconds=round(time.time() - t0, 1),
        ranked_high_winrate=hi[cols].to_dict("records")[:15] if len(hi) else [],
        ranked_balanced=bal[cols].to_dict("records")[:15] if len(bal) else [],
        raw=out["raw"],
    )
    from config import REPORTS
    with open(REPORTS / "sweep_results.json", "w") as f:
        json.dump(save, f, indent=2, default=str)
    print(f"\n[saved] reports/sweep_results.json in {save['sweep_seconds']}s")


if __name__ == "__main__":
    main()
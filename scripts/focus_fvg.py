"""Focused, fast sweep over FVG-retest variants for quality tuning."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.tv_data import load_cached
from strategy.candidates import STRATEGIES
from strategy.backtester import BacktestResult, combine_results, run_backtest

def base_params() -> dict:
    p = {"atr_len": 14, "fvg_lookback": 3, "min_gap": 4, "tp_r": 1.0,
         "bias_htf": "H1", "bias_ema": 50, "long_only": True,
         "bias_htf_by_pair": True, "eur_bias_htf": "H2", "smart_tp": True,
         "min_rr": 0.5, "max_rr": 1.0, "tp_lookback": 60}
    return p


def variants() -> list[dict]:
    v = []
    b = base_params()
    # high-win-rate profile sweep
    for age in (16, 30):
        for body in (0.0, 0.3):
            for rr in (1.0,):
                for confirm in (False, True):
                    p = dict(b)
                    p.update({"fvg_max_age": age, "min_body_atr": body, "max_rr": rr,
                              "confirm": confirm,
                              "name": f"HI_age{age}_body{body}_rr{rr}_cf{int(confirm)}"})
                    v.append(p)
    # balanced profile sweep
    for rr in (1.4, 2.0):
        for confirm in (False, True):
            p = dict(b)
            p.update({"fvg_max_age": 30, "min_body_atr": 0.0, "max_rr": rr,
                      "min_rr": 0.7, "tp_r": 1.5, "confirm": confirm,
                      "name": f"BAL_rr{rr}_cf{int(confirm)}"})
            v.append(p)
    # ultra-high WR (0.8R cap) probes
    for rr in (0.8,):
        for confirm in (True,):
            p = dict(b)
            p.update({"fvg_max_age": 30, "min_body_atr": 0.0, "max_rr": rr,
                      "min_rr": 0.5, "tp_r": 1.0, "confirm": True,
                      "name": f"WR08_rr{rr}_cf1"})
            v.append(p)
    return v


def main() -> None:
    sn = STRATEGIES["fvg_retest"].signaller
    frames = {s: {tf: load_cached(s, tf) for tf in ("M15", "M30", "H1", "H2")}
              for s in ("XAUUSD", "EURUSD")}
    results = []
    for params in variants():
        combined, xau_r, eur_r, tf_pos = [], [], [], 0
        tested = 0
        for sym in ("XAUUSD", "EURUSD"):
            pair_trades = []
            for tf in ("M15", "M30", "H1", "H2"):
                df = frames[sym][tf]
                if df is None or len(df) < 200:
                    continue
                res = run_backtest(df, sym, tf, "fvg", sn, params)
                r = res.metrics()
                tested += 1
                if r["total_r"] > 0:
                    tf_pos += 1
                combined += res.trades
                pair_trades += res.trades
            pm = combine_results([BacktestResult(sym, "ALL", "fvg", pair_trades, params)]).metrics()
            xau_r.append(pm) if sym == "XAUUSD" else eur_r.append(pm)
        mm = combine_results([BacktestResult("X", "ALL", "fvg", combined, params)]).metrics()
        results.append({
            "name": params["name"], "n": mm["n"], "wr": mm["win_rate"],
            "avgR": mm["avg_r"], "pf": mm["profit_factor"], "totR": mm["total_r"],
            "xau": xau_r[0]["total_r"], "xw": xau_r[0]["win_rate"],
            "eur": eur_r[0]["total_r"], "ew": eur_r[0]["win_rate"],
            "tf+": f"{tf_pos}/{tested}", "dd": mm["max_drawdown_r"],
            "cons": mm["max_consec_losses"],
        })
    results.sort(key=lambda r: (r["eur"] > 0, r["xau"] > 0, r["wr"] + r["pf"]), reverse=True)
    print(f"{'variant':30} {'n':>6} {'wr':>6} {'avgR':>6} {'pf':>6} {'totR':>7} "
          f"{'xau':>7} {'xw':>5} {'eur':>7} {'ew':>5} {'tf+':>5} {'dd':>6}")
    for r in results:
        print(f"{r['name']:30} {r['n']:6d} {r['wr']:6.3f} {r['avgR']:6.3f} {r['pf']:6.2f} "
              f"{r['totR']:7.1f} {r['xau']:7.1f} {r['xw']:5.2f} {r['eur']:7.1f} "
              f"{r['ew']:5.2f} {r['tf+']:>5} {r['dd']:6.1f}")


if __name__ == "__main__":
    main()
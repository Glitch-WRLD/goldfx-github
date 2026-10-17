"""Param sweep across SMC confluences."""

import sys
from pathlib import Path
sys.path.insert(0, 'C:/goldfx-agent')

from strategy.test_hybrid_smc import smc_fvg_confluence_signaller
from data.tv_data import load_cached
from strategy.backtester import run_backtest
from strategy.profiles import BAL_PROFILE

base_params = dict(BAL_PROFILE['params'])
pairs = [('GBPUSD', 'H1'), ('AUDUSD', 'H1'), ('EURUSD', 'H1'), ('XAUUSD', 'M30')]

for req_bos, req_sw in [(True, False), (False, True), (True, True)]:
    for k in [2, 3]:
        print(f"=== require_bos={req_bos}, require_sweep={req_sw}, k={k} ===")
        for sym, tf in pairs:
            df = load_cached(sym, tf)
            if df is None:
                continue
            p = dict(base_params)
            p['bias_htf'] = 'H4' if tf == 'H1' else 'H1'
            p['require_bos'] = req_bos
            p['require_sweep'] = req_sw
            p['swing_k'] = k
            p['min_rr'] = 0.8
            p['max_rr'] = 2.5
            res = run_backtest(df, sym, tf, 'hybrid', smc_fvg_confluence_signaller, p)
            m = res.metrics()
            if m.get('n', 0) > 0:
                print(f"  {sym:6s} n={m['n']:3d} WR={m['win_rate']:5.1%} PF={m['profit_factor']:4.2f} totR={m['total_r']:+5.1f} maxDD={m['max_drawdown_r']:4.1f}R cons={m['max_consec_losses']:2d}")

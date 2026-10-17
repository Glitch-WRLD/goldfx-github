"""Deep dive optimization for GBPUSD and XAUUSD with SMC Liquidity Sweep."""

import sys
from pathlib import Path
sys.path.insert(0, 'C:/goldfx-agent')

from strategy.test_hybrid_smc import smc_fvg_confluence_signaller
from data.tv_data import load_cached
from strategy.backtester import run_backtest
from strategy.profiles import BAL_PROFILE

base_params = dict(BAL_PROFILE['params'])

for sym, tf in [('GBPUSD', 'H1'), ('XAUUSD', 'M30')]:
    df = load_cached(sym, tf)
    if df is None:
        continue
    for long_only in [True, False]:
        for tp_r in [1.2, 1.5, 2.0]:
            for sl_buf in [0.15, 0.25]:
                p = dict(base_params)
                p['bias_htf'] = 'H4' if tf == 'H1' else 'H1'
                p['require_bos'] = False
                p['require_sweep'] = True
                p['swing_k'] = 2
                p['long_only'] = long_only
                p['tp_r'] = tp_r
                p['sl_buf'] = sl_buf
                p['min_rr'] = 0.7
                p['max_rr'] = 3.0
                res = run_backtest(df, sym, tf, 'hybrid', smc_fvg_confluence_signaller, p)
                m = res.metrics()
                if m.get('n', 0) > 0 and m['profit_factor'] >= 1.0:
                    print(f"{sym} long_only={long_only} tp_r={tp_r} sl_buf={sl_buf} | n={m['n']:2d} WR={m['win_rate']:5.1%} PF={m['profit_factor']:4.2f} totR={m['total_r']:+5.1f} maxDD={m['max_drawdown_r']:4.1f}R cons={m['max_consec_losses']:2d}")

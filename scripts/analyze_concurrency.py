#!/usr/bin/env python3
"""GoldFX Concurrency & Capacity Analysis Suite.

Simulates the exact historical sequence of all delivered setups (132 setups)
under various concurrent trade caps (3, 4, 5, 6, 7, 8, 10, Unlimited)
and trades-per-pair caps (1, 2, 3, Unlimited).

Evaluates:
- How many winners were missed due to trade capacity limits.
- Total Realized R and Win Rate.
- Maximum concurrent trades active at any point in time.
- Maximum drawdown and open exposure.
"""

import json
import datetime as dt
from pathlib import Path
import httpx

def parse_iso(s: str) -> dt.datetime:
    s = s.replace(" ", "T")
    if "+00:00" not in s and "Z" not in s:
        s += "+00:00"
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))

def load_data():
    r = httpx.get('https://raw.githubusercontent.com/Glitch-WRLD/goldfx-github/main/gha_state/state.json', timeout=15)
    state = r.json()
    history = state.get('history', [])
    outcomes = state.get('outcomes', {})

    trades = []
    for h in history:
        sym = h.get('symbol')
        ts_str = h.get('ts')
        ref = h.get('ref')
        rr = float(h.get('rr', 1.4))
        direction = h.get('dir')
        
        o_key = f"{sym}:{ts_str}"
        outcome = outcomes.get(o_key) or outcomes.get(str(ref))
        if not outcome or outcome.get('hit') not in ('tp', 'sl'):
            continue
        
        entry_time = parse_iso(ts_str)
        exit_time = parse_iso(outcome.get('when', ts_str))
        if exit_time <= entry_time:
            exit_time = entry_time + dt.timedelta(hours=2) # fallback
        
        trades.append({
            'ref': ref,
            'symbol': sym,
            'dir': direction,
            'rr': rr,
            'entry_time': entry_time,
            'exit_time': exit_time,
            'hit': outcome.get('hit'),
            'duration_hours': (exit_time - entry_time).total_seconds() / 3600.0,
        })
    
    # Sort chronologically by entry time
    trades.sort(key=lambda x: x['entry_time'])
    return trades

def simulate(trades, max_concurrent, max_per_sym, risk_pct=6.0):
    open_trades = [] # list of active trade dicts
    taken = []
    skipped = []
    
    timeline_load = [] # (time, active_count)
    peak_concurrent = 0
    peak_sym_concurrent = {}
    
    # Track equity curve in R and %
    equity_r = 0.0
    peak_equity_r = 0.0
    max_dd_r = 0.0
    
    for t in trades:
        now = t['entry_time']
        # 1. Close any open trades that exited before 'now'
        active_still = []
        for op in open_trades:
            if op['exit_time'] <= now:
                # Trade resolved before this new trade arrives
                pnl_r = op['rr'] if op['hit'] == 'tp' else -1.0
                equity_r += pnl_r
                if equity_r > peak_equity_r:
                    peak_equity_r = equity_r
                dd = peak_equity_r - equity_r
                if dd > max_dd_r:
                    max_dd_r = dd
            else:
                active_still.append(op)
        open_trades = active_still
        
        # Count open
        active_total = len(open_trades)
        sym_open = sum(1 for op in open_trades if op['symbol'] == t['symbol'])
        
        # Check capacity
        if active_total >= max_concurrent:
            skipped.append((t, f"max_concurrent ({active_total}/{max_concurrent})"))
            continue
        if sym_open >= max_per_sym:
            skipped.append((t, f"max_per_sym ({sym_open}/{max_per_sym} on {t['symbol']})"))
            continue
        
        # Take the trade
        open_trades.append(t)
        taken.append(t)
        
        if len(open_trades) > peak_concurrent:
            peak_concurrent = len(open_trades)
        
        s_count = sum(1 for op in open_trades if op['symbol'] == t['symbol'])
        if s_count > peak_sym_concurrent.get(t['symbol'], 0):
            peak_sym_concurrent[t['symbol']] = s_count
            
    # Close any remaining open trades at the end of simulation
    for op in open_trades:
        pnl_r = op['rr'] if op['hit'] == 'tp' else -1.0
        equity_r += pnl_r
        if equity_r > peak_equity_r:
            peak_equity_r = equity_r
        dd = peak_equity_r - equity_r
        if dd > max_dd_r:
            max_dd_r = dd
            
    tp_count = sum(1 for x in taken if x['hit'] == 'tp')
    sl_count = sum(1 for x in taken if x['hit'] == 'sl')
    wr = (tp_count / len(taken) * 100.0) if taken else 0.0
    
    skipped_tp = sum(1 for x, _ in skipped if x['hit'] == 'tp')
    skipped_sl = sum(1 for x, _ in skipped if x['hit'] == 'sl')
    
    return {
        'max_concurrent': max_concurrent,
        'max_per_sym': max_per_sym,
        'taken_total': len(taken),
        'skipped_total': len(skipped),
        'tp_count': tp_count,
        'sl_count': sl_count,
        'win_rate': wr,
        'net_r': equity_r,
        'max_dd_r': max_dd_r,
        'max_dd_pct': max_dd_r * risk_pct,
        'skipped_tp': skipped_tp,
        'skipped_sl': skipped_sl,
        'peak_concurrent': peak_concurrent,
        'peak_sym_concurrent': peak_sym_concurrent,
    }

def main():
    trades = load_data()
    print("=" * 80)
    print(f"GOLDFX CONCURRENCY & CAPACITY ANALYSIS ({len(trades)} resolved historical trades)")
    print("=" * 80)
    
    # Natural load analysis (if unlimited)
    unlimited = simulate(trades, max_concurrent=999, max_per_sym=999)
    print(f"\n[1] NATURAL SYSTEM CONCURRENCY (Unconstrained Raw Demand):")
    print(f"    • All-time Peak Concurrent Trades reached:  {unlimited['peak_concurrent']} trades simultaneously")
    print(f"    • Peak Concurrent by Symbol:")
    for sym, count in sorted(unlimited['peak_sym_concurrent'].items(), key=lambda x: x[1], reverse=True):
        print(f"      - {sym:7s}: max {count} simultaneous setups")
        
    avg_duration = sum(t['duration_hours'] for t in trades) / len(trades)
    max_duration = max(t['duration_hours'] for t in trades)
    print(f"    • Average Trade Hold Duration:              {avg_duration:.1f} hours (~{avg_duration*60:.0f} mins)")
    print(f"    • Longest Trade Hold Duration:              {max_duration:.1f} hours")
    
    print("\n" + "=" * 80)
    print("[2] COMPARATIVE SIMULATION MATRIX ACROSS CONCURRENCY CAPS:")
    print("=" * 80)
    
    configs = [
        (3, 1),
        (4, 1),
        (4, 2),
        (5, 1),
        (5, 2),
        (6, 2),
        (7, 2),
        (7, 3),
        (8, 2),
        (8, 3),
        (10, 3),
        (999, 999)
    ]
    
    header = (
        f"{'Cap (Total/Sym)':17s} | {'Taken':5s} | {'Skipped':7s} | {'W - L':9s} | "
        f"{'WR%':6s} | {'Net R':7s} | {'Max DD (R)':10s} | {'Max DD (%)':10s} | {'Missed TP':9s}"
    )
    print(header)
    print("-" * len(header))
    
    for max_c, max_s in configs:
        res = simulate(trades, max_c, max_s, risk_pct=6.0)
        label = "Unlimited" if max_c == 999 else f"{max_c} max ({max_s}/sym)"
        wl = f"{res['tp_count']}W-{res['sl_count']}L"
        print(
            f"{label:17s} | {res['taken_total']:5d} | {res['skipped_total']:7d} | {wl:9s} | "
            f"{res['win_rate']:5.1f}% | {res['net_r']:+6.1f}R | {res['max_dd_r']:9.1f}R | "
            f"{res['max_dd_pct']:9.1f}% | {res['skipped_tp']:8d} TPs"
        )
    print("=" * 80)

if __name__ == "__main__":
    main()

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from engine.scanner import FVGScanner, format_message
from strategy.profiles import SYMBOL_RUNTIME

def main():
    sc = FVGScanner("balanced")
    print("=== TESTING DUAL-ENGINE ARSENAL SCAN ===")
    for sym, rt in SYMBOL_RUNTIME.items():
        strat = rt.get("strategy", "fvg_retest")
        etf = rt["entry_tf"]
        htf = rt["bias_htf"]
        try:
            sigs = sc.scan_catchup(sym, etf, htf, lookback=80)
            print(f"\n[+] {sym} ({strat}, {etf}/{htf}): {len(sigs)} signal(s) in last 80 bars")
            if sigs:
                s = sigs[-1]
                print(f"    Latest: {s.ts} | dir={s.direction} | RR={s.rr} | Conf={s.confidence}% {s.confidence_label}")
                print(f"    Concept Badge: {s.strategy_badge}")
                msg = format_message(s, ref=99)
                print("    --- Message Preview ---")
                for line in msg.splitlines()[:6]:
                    print(f"    {line}")
        except Exception as e:
            print(f"[-] {sym} ERROR: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    main()

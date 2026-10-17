"""Weekend candle puller - exports REAL Headway MT5 history to CSV for backtesting.
Run from the interactive desktop session (that is the ONLY IPC=True context on this box).
Writes one CSV per (symbol,timeframe):  C:\\goldfx-agent\\data\\candles\\<SYM>_<TF>.csv  """
import csv, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import MetaTrader5 as m

LOGIN, PWD, SVR = 5962339, "Test1234!", "Headway-Demo"
TERM = r"C:\Program Files\MetaTrader 5\terminal64.exe"
OUT = r"C:\goldfx-agent\data\candles"
SYMS = ["XAUUSD", "XAGUSD"]
TFS = {"H1": m.TIMEFRAME_H1, "M15": m.TIMEFRAME_M15,
       "M5": m.TIMEFRAME_M5, "D1": m.TIMEFRAME_D1}
ROWS_AVAILABLE = 60_000  # headway demo brokers often cap history; pull what exists


def main():
    os.makedirs(OUT, exist_ok=True)
    t = m.initialize(TERM, login=LOGIN, password=PWD, server=SVR, timeout=120000)
    print("IPC=", t, "ERR=", m.last_error(), flush=True)
    if not t:
        m.shutdown(); sys.exit(2)
    print("ACCT=", (m.account_info().login if m.account_info() else None),
          "BROKER=", (m.account_info().company if m.account_info() else None), flush=True)
    total_rows = 0
    for sym in SYMS:
        for tf_name, tf in TFS.items():
            bars = m.copy_rates_from_pos(sym, tf, 0, ROWS_AVAILABLE)
            if bars is None or len(bars) == 0:
                print("EMPTY", sym, tf_name, m.last_error(), flush=True)
                continue
            path = os.path.join(OUT, f"{sym}_{tf_name}.csv")
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time", "open", "high", "low", "close", "tick_volume", "spread"])
                for b in bars:
                    w.writerow([b[0], f"{b[1]:.5f}", f"{b[2]:.5f}", f"{b[3]:.5f}",
                                f"{b[4]:.5f}", b[5], b[6]])
            n = len(bars)
            total_rows += n
            first = bars[0][0]; last = bars[-1][0]
            print("EXPORTED", sym, tf_name, "rows=", n,
                  "range=", time.strftime("%Y-%m-%d", time.gmtime(first)),
                  "->", time.strftime("%Y-%m-%d", time.gmtime(last)), flush=True)
    print("TOTAL_ROWS=", total_rows, flush=True)
    m.shutdown()


if __name__ == "__main__":
    main()

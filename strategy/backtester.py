"""Next-bar-intrabar backtester.

Model: a strategy produces a signal at bar close (decision time). The trade
enters at the NEXT bar's open plus half-spread/slippage, and SL/TP are
resolved by walking subsequent bars' OHLC (conservative: if a bar touches both
SL and TP, SL wins). Exits are full-position at a static R-multiple target.

Every strategy result is expressed in R-multiples so results are comparable
across pairs/timeframes regardless of price scale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

COSTS = {
    "XAUUSD": {"spread": 0.35, "slippage": 0.10, "point": 0.01},   # $/oz
    "EURUSD": {"spread": 0.00008, "slippage": 0.00002, "point": 0.00001},  # ~0.8 pip
}


@dataclass
class Trade:
    symbol: str
    tf: str
    dir: int
    ts_entry: pd.Timestamp
    ts_exit: pd.Timestamp
    entry: float
    exit_price: float
    sl: float
    tp: float
    r_multiple: float
    reason: str = ""


@dataclass
class Signal:
    """A signal decided on the close of bar at `ts` (that bar is excluded from
    the trade window)."""
    ts: pd.Timestamp      # decision timestamp = close time of signal bar
    dir: int              # +1 long, -1 short
    sl_offset: float      # stop distance in price units from current close
    tp_r: float           # reward:risk multiple (fallback if no tp_price)
    reason: str = ""
    tp_price: float | None = None  # optional structural take-profit level


@dataclass
class BacktestResult:
    symbol: str
    tf: str
    strategy: str
    trades: list[Trade] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.trades)

    def metrics(self) -> dict:
        if not self.trades:
            return {"n": 0}
        r = np.array([t.r_multiple for t in self.trades])
        wins = r[r > 0]
        losses = r[r <= 0]
        gross_win = wins.sum()
        gross_loss = -losses.sum()
        pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
        equity = np.cumsum(r)
        peak = np.maximum.accumulate(equity)
        dd = peak - equity
        # consecutive losses
        cons = 0
        max_cons = 0
        for x in r:
            if x <= 0:
                cons += 1
                max_cons = max(max_cons, cons)
            else:
                cons = 0
        # monthly returns table (R sum per calendar month)
        per_month = pd.Series(r, index=[t.ts_entry for t in self.trades]).groupby(
            lambda ts: ts.to_period("M")
        ).sum()
        pos_months = int((per_month > 0).sum())
        return {
            "n": self.n,
            "win_rate": float((r > 0).mean()),
            "avg_r": float(r.mean()),
            "total_r": float(r.sum()),
            "profit_factor": float(pf),
            "max_drawdown_r": float(dd.max()),
            "max_consec_losses": max_cons,
            "best_trade_r": float(r.max()),
            "worst_trade_r": float(r.min()),
            "months_positive": f"{pos_months}/{len(per_month)}",
            "avg_r_per_month": float(per_month.mean()) if len(per_month) else 0.0,
            "trades_per_week": self.n / max(len(set(f"{t.ts_entry:%Y-%W}" for t in self.trades)), 1),
            "per_month_series": {str(k): v for k, v in per_month.to_dict().items()},
        }

    def summary(self, title: str | None = None) -> str:
        m = self.metrics()
        head = title or f"{self.symbol} {self.tf} {self.strategy}"
        if m["n"] == 0:
            return f"{head}: no trades"
        return (
            f"{head}  n={m['n']}  WR={m['win_rate']:.1%}  avgR={m['avg_r']:+.2f}  "
            f"PF={m['profit_factor']:.2f}  totR={m['total_r']:+.1f}  "
            f"maxDD={m['max_drawdown_r']:.1f}R  maxConsLoss={m['max_consec_losses']} "
            f"posMonths={m['months_positive']}  tr/wk={m['trades_per_week']:.1f}"
        )


def run_backtest(df: pd.DataFrame, symbol: str, tf: str, strategy: str,
                 signaller: Callable[[pd.DataFrame], list[Signal]],
                 params: dict | None = None,
                 max_trades: int = 5000) -> BacktestResult:
    """Walk bars; expand indicators vis a fresh frame; evaluate signals at bar
    close, enter next open."""
    cost = COSTS[symbol]
    df = df.copy()
    params = dict(params or {})
    params["_symbol"] = symbol
    signals = signaller(df, params)
    trades: list[Trade] = []
    df_idx = df.index
    sig_by_ts = {s.ts: s for s in signals}

    times = df_idx.to_numpy()
    opens = df["open"].to_numpy() if "open" in df.columns else df["o"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()

    for sig in signals:
        if sig.ts not in sig_by_ts or sig.ts not in df_idx:
            continue
        pos = df_idx.get_loc(sig.ts)
        if pos + 2 >= len(df):
            continue
        entry = opens[pos + 1]
        spread = cost["spread"]
        slip = cost["slippage"]
        side = sig.dir
        entry = entry + side * (spread + slip) / 2
        sl = sig.ts and (entry - side * sig.sl_offset)
        sl = entry - side * sig.sl_offset
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        tp = entry + side * risk * sig.tp_r
        if sig.tp_price is not None:
            tp = sig.tp_price
            rr = side * (tp - entry) / risk
            min_rr = params.get("min_rr", 0.0)
            max_rr = params.get("max_rr", 3.0)
            if rr < min_rr:
                tp = entry + side * risk * min_rr
            elif rr > max_rr:
                tp = entry + side * risk * max_rr

        exit_ts = None
        exit_price = None
        r = 0.0
        for j in range(pos + 2, len(df)):
            if side == 1:
                if lows[j] <= sl:
                    exit_ts, exit_price, r = times[j], sl - slip, -1.0
                    break
                if highs[j] >= tp:
                    exit_ts, exit_price, r = times[j], tp + slip, sig.tp_r
                    break
            else:
                if highs[j] >= sl:
                    exit_ts, exit_price, r = times[j], sl + slip, -1.0
                    break
                if lows[j] <= tp:
                    exit_ts, exit_price, r = times[j], tp - slip, sig.tp_r
                    break
        if exit_ts is None:
            # bar-by-bar close at last available close
            exit_ts = times[-1]
            exit_price = closes[-1] - side * slip
            r = side * (exit_price - entry) / risk

        trades.append(Trade(symbol, tf, side, pd.Timestamp(df_idx[pos + 1]), pd.Timestamp(exit_ts),
                            float(entry), float(exit_price), float(sl), float(tp), float(r), sig.reason))
        if len(trades) >= max_trades:
            break

    return BacktestResult(symbol=symbol, tf=tf, strategy=strategy, trades=trades, params=params or {})


def combine_results(results: list[BacktestResult]) -> BacktestResult:
    """Merge trades from multiple runs (used to fuse pairs / TFs)."""
    all_trades = [t for r in results for t in r.trades]
    return BacktestResult("MERGED", "ALL", results[0].strategy if results else "", all_trades)
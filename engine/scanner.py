"""Live signal scanner.

Fetches fresh candles from TradingView, applies the FVG-Retest strategy to the
most recent CLOSED candle, computes structural SL/TP levels, and produces a
formatted signal for the Telegram bot. Single-shot per symbol per bar (the bot
drives the loop and persists state).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.tv_data import get_df
from engine.risk import RiskManager, format_decimal
from strategy import candidates
from strategy.profiles import SYMBOL_RUNTIME, profile_for

FVG_SIGNALLER = candidates.STRATEGIES["fvg_retest"].signaller


@dataclass
class ScanSignal:
    symbol: str
    direction: int               # +1 long / -1 short
    entry_tf: str
    bias_htf: str
    ts: pd.Timestamp
    entry: float
    stop: float
    take_profit: float
    rr: float
    reason: str
    profile: str
    bias: int
    lots: float = 0.0
    risk_usd: float = 0.0
    risk_pct: float = 0.0
    messages: list[str] = field(default_factory=list)


class FVGScanner:
    def __init__(self, profile_name: str = "hi"):
        self.profile = profile_for(profile_name)
        self.risk = RiskManager()

    def _build_signal(self, symbol, entry_tf, bias_htf, sub, sig, entry) -> ScanSignal:
        """Convert a strategy Signal (on bar index within ``sub``) to a live one.

        Mirrors ``backtester.run_backtest`` TP resolution exactly: structural
        ``tp_price`` is used as-is; otherwise the fallback ``tp_r`` is clamped
        into the profile's [min_rr, max_rr] band.
        """
        params = dict(self.profile["params"])
        params["bias_htf"] = bias_htf
        side = sig.dir
        sl = entry - side * sig.sl_offset
        risk = abs(entry - sl)
        if risk <= 0:
            return None
        tp = entry + side * risk * sig.tp_r
        if sig.tp_price is not None:
            tp = sig.tp_price
        else:
            min_rr = params.get("min_rr", 0.0)
            max_rr = params.get("max_rr", 3.0)
            if sig.tp_r < min_rr:
                tp = entry + side * risk * min_rr
            elif sig.tp_r > max_rr:
                tp = entry + side * risk * max_rr
        rr = abs(tp - entry) / risk
        rd = self.risk.evaluate(symbol, side, entry, sl, tp, now_utc_day=None,
                                realized_wr=None)
        if not rd.ok:
            return None
        bias = 1 if params.get("long_only") else 0
        return ScanSignal(
            symbol=symbol, direction=side, entry_tf=entry_tf, bias_htf=bias_htf,
            ts=sig.ts, entry=entry, stop=sl, take_profit=tp, rr=round(float(rr), 2),
            reason=sig.reason, profile=self.profile["name"], bias=bias,
            lots=rd.lots, risk_usd=rd.risk_usd, risk_pct=self.risk.risk_pct,
            messages=rd.messages,
        )

    def scan_symbol(self, symbol: str, entry_tf: str | None = None,
                    bias_htf: str | None = None) -> ScanSignal | None:
        rt = SYMBOL_RUNTIME[symbol]
        entry_tf = entry_tf or rt["entry_tf"]
        bias_htf = bias_htf or rt["bias_htf"]
        params = dict(self.profile["params"])
        params["bias_htf"] = bias_htf

        df = get_df(symbol, entry_tf, refresh=True)
        if df is None or len(df) < 300:
            return None

        window = min(len(df), 500)
        sub = df.iloc[-window:].copy()
        sigs = FVG_SIGNALLER(sub, params)

        last_ts = sub.index[-1]
        hit = next((s for s in sigs if s.ts == last_ts), None)
        if hit is None:
            return None

        # Entry reference: next market open ~ last close
        entry = float(sub["close"].iloc[-1])
        return self._build_signal(symbol, entry_tf, bias_htf, sub, hit, entry)

    def scan_catchup(self, symbol: str, entry_tf: str | None = None,
                     bias_htf: str | None = None, lookback: int = 40,
                     only_volume: bool = True) -> list[ScanSignal]:
        """Return signals on the last ``lookback`` CLOSED bars (oldest first).

        Used by the scheduler-based GitHub Actions bot: each run re-scans a
        trailing window so bars between runs are not missed, only delayed.
        """
        rt = SYMBOL_RUNTIME[symbol]
        entry_tf = entry_tf or rt["entry_tf"]
        bias_htf = bias_htf or rt["bias_htf"]
        params = dict(self.profile["params"])
        params["bias_htf"] = bias_htf

        df = get_df(symbol, entry_tf, refresh=True)
        if df is None or len(df) < 300:
            return []

        window = min(len(df), 2000)
        sub = df.iloc[-window:].copy()
        sigs = FVG_SIGNALLER(sub, params)

        out: list[ScanSignal] = []
        last_closed_idx = len(sub) - 2   # last bar may be in progress
        for s in sigs:
            try:
                idx = int(sub.index.get_loc(s.ts))
            except (KeyError, TypeError):
                continue
            if idx > last_closed_idx:
                continue
            if last_closed_idx - idx >= lookback:
                continue
            row = sub.iloc[idx]
            if only_volume and row.get("volume", 0) <= 0:
                continue
            sig = self._build_signal(symbol, entry_tf, bias_htf, sub, s,
                                     float(row["close"]))
            if sig is not None:
                out.append(sig)
        out.sort(key=lambda x: x.ts)
        return out

    def current_bias(self, symbol: str, entry_tf: str | None = None,
                     bias_htf: str | None = None) -> str:
        rt = SYMBOL_RUNTIME[symbol]
        entry_tf = entry_tf or rt["entry_tf"]
        bias_htf = bias_htf or rt["bias_htf"]
        from strategy.filters import htf_bias
        df = get_df(symbol, entry_tf, refresh=False)
        if df is None or len(df) < 200:
            return "n/a"
        b = htf_bias(df, bias_htf, self.profile["params"]["bias_ema"])
        v = b.iloc[-1]
        return "BULLISH" if v > 0 else "BEARISH" if v < 0 else "NEUTRAL"


def format_message(sig: ScanSignal) -> str:
    sym = sig.symbol
    d = "LONG" if sig.direction == 1 else "SHORT"
    emoji = "\U0001F7E2" if sig.direction == 1 else "\U0001F534"
    e = format_decimal(sym, sig.entry)
    sl = format_decimal(sym, sig.stop)
    tp = format_decimal(sym, sig.take_profit)
    header = f"{emoji} {sym} — {d} SETUP\n\U0001F50D {sig.reason}\n"
    body = (
        f"Timeframe: {sig.entry_tf} (bias {sig.bias_htf})\n"
        f"Entry zone: {e} @ market open\n"
        f"Stop-loss:  {sl}\n"
        f"Take-profit: {tp}\n"
        f"R:R = 1 : {sig.rr:.2f}\n"
    )
    if sig.lots > 0:
        riskline = (f"Risk: {sig.risk_usd:.2f} USD ({sig.risk_pct:.1f}%)  "
                    f"Size: {sig.lots:.2f} lots")
    else:
        riskline = "Risk sizing unavailable - check account config."
    tail = f"\n{riskline}\n\n"
    tail += "Not financial advice. Manage risk. Confirm with your broker's quotes."
    return header + body + tail
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
from strategy import candidates, indicators as ind
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
    confidence: int = 0
    confidence_label: str = ""


@dataclass
class ZoneAlert:
    symbol: str
    ts: pd.Timestamp
    bottom: float
    top: float
    bias_htf: str
    bias: int


class FVGScanner:
    def __init__(self, profile_name: str = "balanced"):
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
        try:
            idx = int(sub.index.get_loc(sig.ts))
        except (KeyError, TypeError):
            return None
        side = sig.dir
        sl = entry - side * sig.sl_offset
        risk = abs(entry - sl)
        if risk <= 0:
            return None
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
        else:
            min_rr = params.get("min_rr", 0.0)
            max_rr = params.get("max_rr", 3.0)
            if sig.tp_r < min_rr:
                tp = entry + side * risk * min_rr
            elif sig.tp_r > max_rr:
                tp = entry + side * risk * max_rr
        rr = abs(tp - entry) / risk
        min_rr_post = params.get("min_rr_post", 0.0)
        if min_rr_post > 0 and rr < min_rr_post:
            return None
        rd = self.risk.evaluate(symbol, side, entry, sl, tp, now_utc_day=None,
                                realized_wr=None)
        if not rd.ok:
            return None
        bias = 1 if params.get("long_only") else 0
        conf, conf_label = self._compute_confidence(sub, sig, idx, bias_htf)
        return ScanSignal(
            symbol=symbol, direction=side, entry_tf=entry_tf, bias_htf=bias_htf,
            ts=sig.ts, entry=entry, stop=sl, take_profit=tp, rr=round(float(rr), 2),
            reason=sig.reason, profile=self.profile["name"], bias=bias,
            lots=rd.lots, risk_usd=rd.risk_usd, risk_pct=self.risk.risk_pct,
            messages=rd.messages, confidence=conf, confidence_label=conf_label,
        )

    def _compute_confidence(self, sub, sig, idx, bias_htf) -> tuple[int, str]:
        """0-100 conviction score from confluence at the signal bar:
        imbalance strength, zone freshness, HTF-bias pull, MACD momentum and
        volume impulse. NaN components are dropped and weights renormalised.
        """
        p = self.profile["params"]
        H, L, C = sub["high"], sub["low"], sub["close"]
        ATR = ind.atr(sub, int(p.get("atr_len", 14)))
        a = float(ATR.iloc[idx]) if pd.notna(ATR.iloc[idx]) else float("nan")
        parts: list[tuple[float, float]] = []

        def add(score, w):
            if score is not None and score == score and score >= 0:
                parts.append((float(score), float(w)))

        if a > 0:
            # nearest FVG zone: thickness / ATR + freshness
            th = age = None
            for j in range(idx, max(2, idx - 120), -1):
                if sig.dir == 1 and H.iloc[j - 2] < L.iloc[j]:
                    th, age = float(L.iloc[j] - H.iloc[j - 2]), idx - j
                    break
                if sig.dir == -1 and L.iloc[j - 2] > H.iloc[j]:
                    th, age = float(L.iloc[j - 2] - H.iloc[j]), idx - j
                    break
            if th is not None and age is not None:
                add(min(100.0, th / a * 60.0), 0.25)
                add(max(0.0, (1 - age / max(float(p.get("fvg_max_age", 30)), 1)) * 100.0), 0.20)
            # HTF-bias conviction: distance from price to the bias-TF EMA50
            try:
                rule = {"H1": "1h", "H2": "2h", "H4": "4h"}.get(bias_htf, bias_htf)
                hdf = sub.resample(rule).agg(
                    {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
                e = ind.ema(hdf["close"], int(p.get("bias_ema", 50)))
                e = e.reindex(sub.index, method="ffill").ffill()
                ev = e.iloc[idx] if pd.notna(e.iloc[idx]) else None
                if ev is not None:
                    dist = (C.iloc[idx] - ev) * sig.dir / a
                    add(min(100.0, max(0.0, 40 + dist * 40)), 0.25)
            except Exception:
                pass
            # MACD momentum agreement
            line, sigl, _ = ind.macd(C, 12, 26, 9)
            if pd.notna(line.iloc[idx]):
                bull = line.iloc[idx] > sigl.iloc[idx]
                add(100.0 if (bull and line.iloc[idx] * sig.dir > 0)
                    else (55.0 if bull else 0.0), 0.20)
            # volume impulse vs 20-bar average
            if "volume" in sub.columns:
                v20 = ind.sma(sub["volume"].astype(float), 20)
                v = float(sub["volume"].iloc[idx])
                if pd.notna(v20.iloc[idx]) and v20.iloc[idx] > 0:
                    add(min(100.0, v / v20.iloc[idx] * 50.0), 0.10)

        tot = sum(w for _, w in parts) or 1.0
        score = int(round(sum(s * w for s, w in parts) / tot))
        label = ("VERY HIGH" if score >= 82 else
                 "HIGH" if score >= 65 else
                 "MEDIUM" if score >= 45 else "LOW")
        return score, label

    def scan_new_zones(self, symbol: str, entry_tf: str, bias_htf: str,
                       lookback: int = 40) -> list[ZoneAlert]:
        """Detect FVG zones formed on the most recent CLOSED bars, aligned with
        the current HTF bias. Bull-bias => long zones, bear-bias => short zones.
        Used for the "watch zone" pre-alert sent before a retest confirms.
        """
        df = get_df(symbol, entry_tf, refresh=False)
        if df is None or len(df) < 60:
            return []
        sub = df.iloc[-lookback:].copy()
        bias = self.current_bias(symbol, entry_tf, bias_htf)
        H, L = sub["high"], sub["low"]
        n = len(sub)
        last = n - 2  # last fully closed bar
        out: list[ZoneAlert] = []
        for i in range(max(2, last - 1), last + 1):
            if bias == 1 and H.iloc[i - 2] < L.iloc[i]:
                bot, top = float(H.iloc[i - 2]), float(L.iloc[i])
                out.append(ZoneAlert(symbol=symbol, ts=sub.index[i],
                                     bottom=bot, top=top,
                                     bias_htf=bias_htf, bias=1))
            elif bias == -1 and L.iloc[i - 2] > H.iloc[i]:
                bot, top = float(H.iloc[i]), float(L.iloc[i - 2])
                out.append(ZoneAlert(symbol=symbol, ts=sub.index[i],
                                     bottom=bot, top=top,
                                     bias_htf=bias_htf, bias=-1))
        return out

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

    n = max(1, min(5, round(sig.confidence / 20)))
    conf_bar = "\u25B0" * n + "\u25B1" * (5 - n) if sig.confidence > 0 else "\u2013"

    header = (
        f"{emoji} {sym} \u2014 {d} SETUP\n"
        f"Confidence: {sig.confidence}% {sig.confidence_label}  [{conf_bar}]\n"
        f"{'\u2500' * 26}"
    )
    body = (
        f"\n\U0001F4CC Entry zone   {e}  @ market open"
        f"\n\U0001F6D1 Stop-loss    {sl}   (risk {sig.risk_pct:.1f}%)"
        f"\n\U0001F3AF Take-profit  {tp}   (R:R 1 : {sig.rr:.2f})\n"
        f"{'\u2500' * 26}\n"
        f"\U0001F4CA {sig.reason} \u00b7 {sig.entry_tf} setup, {sig.bias_htf} bias\n"
    )
    if sig.lots > 0:
        riskline = (f"\U0001F4B5 Risk {sig.risk_usd:.2f} USD ({sig.risk_pct:.1f}%) "
                    f"\u00b7 Size {sig.lots:.2f} lots")
    else:
        riskline = "Risk sizing unavailable - check account config."
    tail = f"{riskline}\n\n\u26A0\ufe0f Not financial advice. Confirm quotes with your broker."
    return header + body + tail


def format_zone_alert(za: ZoneAlert) -> str:
    sym = za.symbol
    bot = format_decimal(sym, za.bottom)
    top = format_decimal(sym, za.top)
    bias = "BULLISH" if za.bias == 1 else "BEARISH"
    return (
        f"\u26A0\ufe0f {sym} \u2014 FVG ZONE FORMED (watch for retest)\n"
        f"{'\u2500' * 26}\n"
        f"\U0001F4CA New imbalance on {za.bias_htf} bias \u00b7 {bias}\n"
        f"\U0001F3AF Zone  {bot} \u2013 {top}\n"
        f"\U0001F553 Price may retest this zone next bars.\n"
        f"Full {sym} setup posts automatically when the retest confirms.\n"
        f"{'\u2500' * 26}\n"
        f"\u26A0\ufe0f Heads-up only \u2014 not a signal. Confirm quotes with your broker."
    )
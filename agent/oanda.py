"""OANDA v20 REST executor for the GoldFX auto-trading agent.

Two modes:
* paper  : fully simulated fills (no network) - the default, used to prove the
           pipeline before any real/demo money moves.
* demo   : real OANDA practice account (https://api-fxpractice.oanda.com).
* live   : real OANDA live account (blocked by an explicit override flag).

Usage (from the watcher/runner):
    ex = Executor.for_env("paper")
    fill = ex.place_market_order(symbol, direction, units, sl, tp, label)
    which returns a dict with at least: ok, order_id, fill_price, units,
    direction, symbol, ts, extra.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field

import httpx

# OANDA instrument names (OANDA uses underscores).
OANDA_INSTRUMENT = {
    "XAUUSD": "XAU_USD",
    "EURUSD": "EUR_USD",
    "GBPUSD": "GBP_USD",
    "AUDUSD": "AUD_USD",
    "USDJPY": "USD_JPY",
}

# OANDA "1 lot" in base-currency units per symbol (used to convert the bot's
# lots to OANDA units).
LOT_UNITS = {
    "XAUUSD": 100.0,   # 1 lot = 100 oz
    "EURUSD": 100000.0,
    "GBPUSD": 100000.0,
    "AUDUSD": 100000.0,
    "USDJPY": 100000.0,
}

API_URLS = {
    "demo": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}


@dataclass
class Fill:
    ok: bool
    symbol: str = ""
    direction: int = 0
    lots: float = 0.0
    units: float = 0.0
    fill_price: float = 0.0
    order_id: str = ""
    broker: str = "paper"
    ts: str = ""
    message: str = ""
    extra: dict = field(default_factory=dict)


class PaperBroker:
    """Simulated fills: fill at the requested entry (the delivered level)."""

    def __init__(self, seed_price: float = 0.0):
        self.seed_price = seed_price

    def place_market_order(self, symbol: str, direction: int, lots: float,
                           entry: float, sl: float, tp: float,
                           label: str) -> Fill:
        fill_price = entry if entry and entry > 0 else self.seed_price
        sym_disp = OANDA_INSTRUMENT.get(symbol, symbol)
        return Fill(
            ok=True,
            symbol=symbol,
            direction=direction,
            lots=lots,
            fill_price=round(fill_price, 5) if fill_price else 0.0,
            order_id="PAPER-" + f"{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M%S%f}",
            broker="paper",
            ts=dt.datetime.now(dt.timezone.utc).isoformat(),
            message=f"PAPER FILL {sym_disp} {'LONG' if direction == 1 else 'SHORT'} "
                    f"{lots:g} lots @ {fill_price:.5f}",
            extra={"entry": entry, "sl": sl, "tp": tp, "label": label},
        )

    def close_position(self, symbol: str, direction: int, lots: float) -> Fill:
        return Fill(
            ok=True,
            symbol=symbol,
            direction=direction,
            lots=lots,
            fill_price=0.0,
            order_id="PAPER-CLOSE",
            broker="paper",
            ts=dt.datetime.now(dt.timezone.utc).isoformat(),
            message=f"PAPER CLOSE {OANDA_INSTRUMENT.get(symbol)}",
            extra={},
        )

    def current_price(self, symbol: str, direction: int | None = None) -> float:
        return self.seed_price if self.seed_price else 0.0

    def get_candles(self, symbol: str, tf: str = "M5", count: int = 5) -> list[dict]:
        try:
            from data.tv_data import load_cached, get_df
            df = load_cached(symbol, tf) or get_df(symbol, tf)
            if df is not None and not df.empty:
                sub = df.tail(count)
                candles = []
                for idx, row in sub.iterrows():
                    candles.append({
                        "time": int(idx.timestamp()) if hasattr(idx, "timestamp") else 0,
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                    })
                return candles
        except Exception:
            pass
        return []

    def modify_position(self, symbol: str, ticket: int | str | None = None,
                        sl: float = 0.0, tp: float = 0.0) -> bool:
        return True

    def account_snapshot(self) -> dict:
        return {"mode": "paper", "balance": None, "currency": "USD"}



class OandaBroker:
    """Real OANDA v20 REST client (demo or live)."""

    def __init__(self, token: str, account_id: str, env: str = "demo"):
        self.token = token
        self.account_id = account_id
        self.env = env
        self.base = API_URLS[env]
        self._client = httpx.Client(
            base_url=self.base,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )

    def _instrument(self, symbol: str) -> str:
        inst = OANDA_INSTRUMENT.get(symbol)
        if not inst:
            raise ValueError(f"no OANDA instrument mapping for {symbol}")
        return inst

    def place_market_order(self, symbol: str, direction: int, lots: float,
                           entry: float, sl: float, tp: float,
                           label: str) -> Fill:
        """Fire a market order with attached SL/TP at the delivered levels.

        OANDA units are signed (units < 0 = short). ``lots`` is converted to
        base-currency units via LOT_UNITS so the same sizing (risk.py) feeds
        every broker consistently.
        """
        inst = self._instrument(symbol)
        units = lots * LOT_UNITS[symbol]
        signed = units if direction == 1 else -units
        payload = {
            "order": {
                "type": "MARKET",
                "instrument": inst,
                "units": str(int(signed)),
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
                "stopLossOnFill": {"price": f"{sl:.5f}"},
                "takeProfitOnFill": {"price": f"{tp:.5f}"},
                "clientExtensions": {"tag": "goldfx", "comment": label[:30]},
            }
        }
        r = self._client.post(f"/v3/accounts/{self.account_id}/orders", json=payload)
        body = r.json()
        if r.status_code not in (200, 201) or "orderFillTransaction" not in body:
            return Fill(
                ok=False,
                symbol=symbol,
                direction=direction,
                lots=lots,
                message=f"OANDA {r.status_code}: {json.dumps(body)[:400]}",
                extra=body,
            )
        fill = body["orderFillTransaction"]
        return Fill(
            ok=True,
            symbol=symbol,
            direction=direction,
            lots=lots,
            fill_price=float(fill.get("price", 0.0)),
            order_id=fill.get("id", ""),
            broker=self.env,
            ts=fill.get("time", dt.datetime.now(dt.timezone.utc).isoformat()),
            message=f"{self.env.upper()} FILL {inst} {'LONG' if direction == 1 else 'SHORT'} "
                    f"{lots:g} lots @ {fill.get('price')}",
            extra=dict(fill),
        )

    def close_position(self, symbol: str, direction: int, lots: float) -> Fill:
        inst = self._instrument(symbol)
        units = lots * LOT_UNITS[symbol]
        r = self._client.post(
            f"/v3/accounts/{self.account_id}/positions/{inst}/close",
            json={"longUnits": "ALL", "shortUnits": "ALL"})
        body = r.json()
        if r.status_code not in (200, 201):
            return Fill(ok=False, symbol=symbol, direction=direction, lots=lots,
                        message=f"OANDA close {r.status_code}: {json.dumps(body)[:300]}",
                        extra=body)
        return Fill(ok=True, symbol=symbol, direction=direction, lots=lots,
                    order_id="CLOSE", broker=self.env,
                    ts=dt.datetime.now(dt.timezone.utc).isoformat(),
                    message=f"{self.env.upper()} CLOSE {inst}",
                    extra=dict(body))

    def account_snapshot(self) -> dict:
        r = self._client.get(f"/v3/accounts/{self.account_id}/summary")
        if r.status_code != 200:
            return {"mode": self.env, "error": r.text[:200]}
        a = r.json().get("account", {})
        return {
            "mode": self.env,
            "balance": float(a.get("balance", 0.0)),
            "currency": a.get("currency", "USD"),
            "unrealized_pl": float(a.get("unrealizedPL", 0.0)),
        }

    def current_price(self, symbol: str, direction: int | None = None) -> float:
        """Last bid/ask from the pricing stream; returns the mid."""
        inst = self._instrument(symbol)
        r = self._client.get(f"/v3/instruments/{inst}/candles", params={"count": "1", "price": "MBA"})
        if r.status_code != 200:
            return 0.0
        c = r.json().get("candles", [])
        if not c or not c[0].get("mid"):
            return 0.0
        m = c[0]["mid"]
        return (float(m["a"]) + float(m["b"])) / 2.0

    def get_candles(self, symbol: str, tf: str = "M5", count: int = 5) -> list[dict]:
        inst = self._instrument(symbol)
        gran = "M5" if tf == "M5" else ("M15" if tf == "M15" else "M1")
        r = self._client.get(f"/v3/instruments/{inst}/candles", params={"count": str(count), "granularity": gran, "price": "M"})
        if r.status_code != 200:
            return []
        candles = []
        for c in r.json().get("candles", []):
            if not c.get("complete", False):
                continue
            m = c.get("mid", {})
            candles.append({
                "time": int(dt.datetime.fromisoformat(c["time"].replace("Z", "+00:00")).timestamp()),
                "open": float(m.get("o", 0)),
                "high": float(m.get("h", 0)),
                "low": float(m.get("l", 0)),
                "close": float(m.get("c", 0)),
            })
        return candles

    def close(self):
        self._client.close()


def executor_for_env(env: str | None = None, token: str = "",
                     account_id: str = "", seed_price: float = 0.0):
    """Factory: returns a broker executor for the given env.

    ``env`` falls back to config.AUTO_TRADE_ENV:
      * "paper" -> PaperBroker (simulated fills, for testing)
      * "mt5"   -> MT5 terminal broker (Windows, MetaTrader5 package)
      * "demo"  -> OANDA practice REST (api-fxpractice.oanda.com)
      * "live"  -> OANDA live REST, refused unless ALLOW_LIVE=1
    """
    import config

    env = env or config.AUTO_TRADE_ENV
    if env == "paper":
        return PaperBroker(seed_price=seed_price)
    if env == "mt5":
        from agent.mt5_broker import build_mt5_broker
        return build_mt5_broker()
    if env in ("demo", "live"):
        if env == "live" and not config.ALLOW_LIVE:
            raise RuntimeError("live trading disabled - set ALLOW_LIVE=1 to explicitly enable")
        token = token or config.OANDA_TOKEN
        account = account_id or config.OANDA_ACCOUNT_ID
        if not token or not account:
            raise RuntimeError(f"OANDA {env} requires OANDA_TOKEN and OANDA_ACCOUNT_ID")
        return OandaBroker(token, account, env)
    raise ValueError(f"unknown AUTO_TRADE_ENV: {env!r}")
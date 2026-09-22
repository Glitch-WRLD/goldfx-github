"""MT5 broker executor for the GoldFX auto-trading agent (Windows).

Uses the official MetaTrader5 Python package, which runs wherever the MT5
terminal runs (Windows).  Two sub-modes are both supported:

* attach    : connect to an already-running, already-logged-in MT5 terminal
              (mt5.initialize()). Use this if you keep Headway MT5 open and
              logged into your demo account.
* login     : additionally call mt5.login(login, password, server) when
              MT5_LOGIN / MT5_PASSWORD / MT5_SERVER are provided.

Import of ``MetaTrader5`` is lazy so this module can be imported on any OS
(e.g. for paper-mode testing on Linux); it is only required when an order is
actually placed through the MT5 broker.
"""

from __future__ import annotations

import datetime as dt
import sys

import config
from agent.oanda import Fill

# MT5 trade return codes we accept as a fill.
_DONE_CODES = {10009, 10010, 10011, 10012, 10013}  # done / done partial / no changes


def _import_mt5():
    """Return the MetaTrader5 module or raise a clear error on non-Windows."""
    try:
        import MetaTrader5 as mt5
    except ImportError as e:
        raise RuntimeError(
            "MetaTrader5 package not installed. Run: pip install MetaTrader5 "
            "(Windows + 64-bit install of Python only)."
        ) from e
    return mt5


def _filling_modes(symbol_info, mt5) -> list:
    """Preferred order-filling modes for a symbol; falls back to a sane set."""
    f = getattr(symbol_info, "filling_mode", None)
    modes = []
    if f is not None:
        if f & 1:
            modes.append(mt5.ORDER_FILLING_FOK)
        if f & 2:
            modes.append(mt5.ORDER_FILLING_IOC)
        if f & 4:
            modes.append(mt5.ORDER_FILLING_RETURN)
    if not modes:
        modes = [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK]
    # Order by FOK -> IOC -> RETURN
    preference = {mt5.ORDER_FILLING_FOK: 0, mt5.ORDER_FILLING_IOC: 1,
                  mt5.ORDER_FILLING_RETURN: 2}
    return sorted(set(modes), key=lambda m: preference.get(m, 3))



class Mt5Broker:
    def __init__(self, login: int | None = None, password: str = "",
                 server: str = "", path: str = ""):
        self.login_id = login
        self.password = password
        self.server = server
        self.path = path
        self.broker = "mt5"

    # -- lifecycle ----------------------------------------------------------
    def connect(self) -> str:
        mt5 = _import_mt5()
        kwargs = {}
        if self.path:
            kwargs["path"] = self.path
        if not mt5.initialize(**kwargs):
            err = mt5.last_error()
            raise RuntimeError(f"MT5 initialize failed: {err}")
        if self.login_id is not None:
            params = {"login": self.login_id, "password": self.password}
            if self.server:
                params["server"] = self.server
            ok = mt5.login(**params)
            if not ok:
                raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")
        info = mt5.account_info()
        if info is None:
            raise RuntimeError("MT5 connected but no account_info (not logged in?)")
        acc = info.login
        return f"MT5 account {acc} ({info._asdict().get('server', '') if hasattr(info, '_asdict') else ''})"

    def shutdown(self) -> None:
        try:
            mt5 = _import_mt5()
            mt5.shutdown()
        except Exception:
            pass

    # -- prices -------------------------------------------------------------
    def current_price(self, symbol: str) -> float:
        mt5 = _import_mt5()
        mt5.symbol_select(symbol, True)
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None and getattr(tick, "ask", 0.0) > 0:
            return float(tick.ask)
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 1)
        if rates is not None and len(rates) > 0:
            return float(rates[0][4])
        if tick is not None:
            return float(tick.ask)
        raise RuntimeError(f"no tick for {symbol}")

    def get_candles(self, symbol: str, tf: str = "M5", count: int = 5) -> list[dict]:
        """Fetch recent candles for a symbol/timeframe from MT5."""
        mt5 = _import_mt5()
        tf_map = {
            "M1": getattr(mt5, "TIMEFRAME_M1", 1),
            "M5": getattr(mt5, "TIMEFRAME_M5", 5),
            "M15": getattr(mt5, "TIMEFRAME_M15", 15),
            "M30": getattr(mt5, "TIMEFRAME_M30", 30),
            "H1": getattr(mt5, "TIMEFRAME_H1", 16385),
        }
        mt5_tf = tf_map.get(tf, getattr(mt5, "TIMEFRAME_M5", 5))
        rates = mt5.copy_rates_from_pos(symbol, mt5_tf, 0, count)
        if rates is None or len(rates) == 0:
            return []
        candles = []
        for r in rates:
            candles.append({
                "time": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
            })
        return candles

    # -- orders -------------------------------------------------------------
    def place_market_order(self, symbol: str, direction: int, lots: float,
                           entry: float, sl: float, tp: float,
                           label: str) -> Fill:
        mt5 = _import_mt5()
        # ensure symbol is loaded & visible
        info = mt5.symbol_info(symbol)
        if info is None:
            return Fill(ok=False, symbol=symbol, direction=direction, lots=lots,
                        message=f"MT5 no symbol_info for {symbol}")
        if not info.visible:
            if not mt5.symbol_select(symbol, True):
                return Fill(ok=False, symbol=symbol, direction=direction, lots=lots,
                            message=f"MT5 cannot select {symbol}")

        tick = mt5.symbol_info_tick(symbol)
        price = 0.0
        if tick is not None and (tick.ask > 0 or tick.bid > 0):
            price = float(tick.ask) if direction == 1 else float(tick.bid)
        else:
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 1)
            if rates is not None and len(rates) > 0:
                price = float(rates[0][4])
        # Idempotency check: if an open position with this label already exists, do not duplicate
        positions = mt5.positions_get(symbol=symbol)
        if positions:
            for p in positions:
                if getattr(p, "comment", "") == label[:26]:
                    return Fill(
                        ok=True,
                        symbol=symbol,
                        direction=direction,
                        lots=float(p.volume),
                        fill_price=float(p.price_open),
                        order_id=str(p.ticket),
                        broker="mt5",
                        ts=dt.datetime.now(dt.timezone.utc).isoformat(),
                        message=f"MT5 EXISTING_FILL {symbol} {'BUY' if direction == 1 else 'SELL'} "
                                f"{p.volume:g} lots @ {float(p.price_open):g}",
                        extra={"retcode": 10009, "ticket": p.ticket},
                    )

        request = {

            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lots),
            "type": mt5.ORDER_TYPE_BUY if direction == 1 else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "deviation": int(getattr(config, "MT5_DEVIATION", 20)),
            "magic": int(getattr(config, "MT5_MAGIC", 7710)),
            "comment": label[:26],
            "type_time": mt5.ORDER_TIME_GTC,
        }

        # try each supported filling mode until one is accepted
        last_err = ""
        for fill_mode in _filling_modes(info, mt5):
            request["type_filling"] = fill_mode
            res = mt5.order_send(request)
            if res is None:
                last_err = f"order_send returned None: {mt5.last_error()}"
                continue
            retcode = getattr(res, "retcode", None) if not isinstance(res, dict) else res.get("retcode")
            if retcode in _DONE_CODES:
                price_fill = getattr(res, "price", 0.0) if not isinstance(res, dict) else res.get("price", 0.0)
                order_id = getattr(res, "order", "") if not isinstance(res, dict) else res.get("order", "")
                return Fill(
                    ok=True,
                    symbol=symbol,
                    direction=direction,
                    lots=lots,
                    fill_price=float(price_fill),
                    order_id=str(order_id),
                    broker="mt5",
                    ts=dt.datetime.now(dt.timezone.utc).isoformat(),
                    message=f"MT5 FILL {symbol} {'BUY' if direction == 1 else 'SELL'} "
                            f"{lots:g} lots @ {float(price_fill):g}",
                    extra={"retcode": retcode, "ticket": order_id},
                )
            comment = getattr(res, "comment", "no comment") if not isinstance(res, dict) else res.get("comment", "no comment")
            last_err = f"retcode {retcode} ({comment})"
        return Fill(ok=False, symbol=symbol, direction=direction, lots=lots,
                    message=f"MT5 order rejected: {last_err}")

    def get_spread(self, symbol: str) -> float:
        """Return the current spread in price units (ask - bid)."""
        mt5 = _import_mt5()
        mt5.symbol_select(symbol, True)
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None and getattr(tick, "ask", 0.0) > 0 and getattr(tick, "bid", 0.0) > 0:
            return float(tick.ask - tick.bid)
        return 0.0

    def get_open_positions(self) -> list[dict]:
        """Return all active positions in the terminal."""
        mt5 = _import_mt5()
        positions = mt5.positions_get()
        if not positions:
            return []
        res = []
        for p in positions:
            res.append({
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": p.type,
                "lots": float(p.volume),
                "open_price": float(p.price_open),
                "cur_price": float(p.price_current),
                "profit": float(p.profit),
                "sl": float(p.sl),
                "tp": float(p.tp),
                "comment": getattr(p, "comment", ""),
            })
        return res

    def close_position_by_ticket(self, ticket: int | str) -> Fill:
        """Close a specific position by its ticket number."""
        mt5 = _import_mt5()
        positions = mt5.positions_get()
        if not positions:
            return Fill(ok=False, symbol="", direction=0, lots=0.0,
                        message=f"MT5 no open positions, cannot close ticket {ticket}")
        target = None
        for p in positions:
            if str(p.ticket) == str(ticket):
                target = p
                break
        if target is None:
            return Fill(ok=False, symbol="", direction=0, lots=0.0,
                        message=f"MT5 ticket {ticket} not found in open positions")

        symbol = target.symbol
        direction = 1 if target.type == 0 else -1  # 0 is BUY, 1 is SELL
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        close_price = float(tick.bid) if target.type == 0 else float(tick.ask)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(target.volume),
            "type": mt5.ORDER_TYPE_SELL if target.type == 0 else mt5.ORDER_TYPE_BUY,
            "position": target.ticket,
            "price": close_price,
            "deviation": int(getattr(config, "MT5_DEVIATION", 20)),
            "magic": int(getattr(config, "MT5_MAGIC", 7710)),
            "comment": "goldfx close",
            "type_time": mt5.ORDER_TIME_GTC,
        }
        for fill_mode in _filling_modes(info, mt5):
            request["type_filling"] = fill_mode
            res = mt5.order_send(request)
            retcode = getattr(res, "retcode", None) if not isinstance(res, dict) else res.get("retcode")
            if res is not None and retcode in _DONE_CODES:
                return Fill(ok=True, symbol=symbol, direction=direction,
                            lots=float(target.volume), order_id=str(target.ticket), broker="mt5",
                            ts=dt.datetime.now(dt.timezone.utc).isoformat(),
                            message=f"MT5 CLOSE {symbol} ticket {target.ticket} ({target.volume} lots)")
        return Fill(ok=False, symbol=symbol, direction=direction, lots=float(target.volume),
                    message=f"MT5 close failed for ticket {ticket}: {mt5.last_error()}")

    def close_position(self, symbol: str, direction: int, lots: float) -> Fill:
        mt5 = _import_mt5()
        positions = mt5.positions_get(symbol=symbol)
        if not positions:
            return Fill(ok=True, symbol=symbol, direction=direction, lots=lots,
                        order_id="none", broker="mt5",
                        message=f"MT5 no open position for {symbol} to close")
        for pos in positions:
            if getattr(pos, "type", None) == (1 if direction == 1 else 0):
                return self.close_position_by_ticket(pos.ticket)
        return Fill(ok=True, symbol=symbol, direction=direction, lots=lots,
                    order_id="none", broker="mt5",
                    message=f"MT5 no matching {symbol} position to close")

    def modify_position(self, symbol: str, ticket: int | str | None = None,
                        sl: float = 0.0, tp: float = 0.0) -> bool:
        """Modify SL/TP on an open position (e.g. move SL to breakeven)."""
        mt5 = _import_mt5()
        positions = mt5.positions_get(symbol=symbol)
        if not positions:
            return False
        for pos in positions:
            if ticket is None or str(pos.ticket) == str(ticket):
                req = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": pos.ticket,
                    "symbol": symbol,
                    "sl": float(sl),
                    "tp": float(tp if tp > 0 else getattr(pos, "tp", 0.0)),
                }
                res = mt5.order_send(req)
                retcode = getattr(res, "retcode", None) if not isinstance(res, dict) else res.get("retcode")
                return bool(res is not None and retcode in _DONE_CODES)
        return False

    def account_snapshot(self) -> dict:
        mt5 = _import_mt5()
        info = mt5.account_info()
        if info is None:
            return {"mode": "mt5", "error": "not logged in"}
        d = info._asdict() if hasattr(info, "_asdict") else info
        return {
            "mode": "mt5",
            "balance": float(d.get("balance", 0.0)),
            "currency": d.get("currency", "USD"),
            "equity": float(d.get("equity", 0.0)),
            "margin": float(d.get("margin", 0.0)),
            "free_margin": float(d.get("margin_free", 0.0)),
            "margin_level": float(d.get("margin_level", 0.0)),
        }



def build_mt5_broker():
    login = getattr(config, "MT5_LOGIN", None)
    if login:
        try:
            login = int(login)
        except (ValueError, TypeError):
            pass
    password = getattr(config, "MT5_PASSWORD", "")
    server = getattr(config, "MT5_SERVER", "")
    path = getattr(config, "MT5_PATH", "")
    b = Mt5Broker(login=login, password=password, server=server, path=path)
    b.connect()
    return b
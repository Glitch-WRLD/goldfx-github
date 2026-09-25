"""GoldFX auto-trading agent: watcher loop.

Polls ``state.json`` from the public GitHub repo on a short interval, diffs
against the local ledger to discover new confirmed setups, executes them
(paper or demo) via the OANDA executor, and posts fill + outcome messages
to Telegram.  All risk guards from ``engine/risk.py`` apply identically.

State contract consumed from ``gha_state/state.json`` (public, readable
without auth):
    - ``history[]``  — newest-first list of delivered setups, each with:
        symbol, dir ("LONG"/"SHORT"), entry, sl, tp, rr, ref, tf, ts, profile
    - ``outcomes{}`` — keyed by ``"{symbol}:{ts}"``, contains "hit" (tp/sl)
        once the delivery bot resolves the follow-up.

Idempotency: each ``ref`` is fired exactly once.  Outcomes are reconciled
automatically to close the position and record P&L.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import time

import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from agent.oanda import executor_for_env, Fill
from agent.ledger import load_ledger
from engine.risk import RiskManager, position_size, floor_lots
from strategy.profiles import SYMBOL_RUNTIME

log = logging.getLogger("goldfx.agent")

TOKEN = config.BOT_TOKEN
CHAT_ID = config.CHAT_ID


def tg(method: str, **params) -> dict:
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    body = json.dumps(params).encode()
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body,
                                        headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = str(e)
            time.sleep(2 * (attempt + 1))
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    return {"ok": False, "description": str(last)}


PENDING_PATH = Path(__file__).resolve().parents[1] / "data" / "pending_telegram.json"
PENDING_RETRACE_PATH = Path(__file__).resolve().parents[1] / "data" / "pending_retrace.json"


def _load_pending_retrace() -> dict:
    try:
        return json.loads(PENDING_RETRACE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_pending_retrace(pending: dict) -> None:
    try:
        PENDING_RETRACE_PATH.parent.mkdir(exist_ok=True)
        PENDING_RETRACE_PATH.write_text(json.dumps(pending, indent=2, ensure_ascii=False),
                                        encoding="utf-8")
    except Exception as e:
        log.warning("pending retrace save failed: %s", e)


def _load_pending() -> list[dict]:
    try:
        return json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_pending(messages: list[dict]) -> None:
    try:
        PENDING_PATH.parent.mkdir(exist_ok=True)
        PENDING_PATH.write_text(json.dumps(messages, ensure_ascii=False),
                                encoding="utf-8")
    except Exception as e:
        log.warning("pending save failed: %s", e)


def _enqueue(chat_id: str | int, text: str) -> None:
    pending = _load_pending()
    pending.append({"chat_id": str(chat_id), "text": text,
                    "ts": time.time()})
    _save_pending(pending)
    log.warning("telegram send failed -> QUEUED %d pending (tail: %s)",
                len(pending), text[:60])


def send(chat_id: str | int, text: str) -> bool:
    """Deliver immediately; if it fails after all retries, persist to the
    pending queue so a spotty uplink can never silently eat a fill."""
    res = tg("sendMessage", chat_id=chat_id, text=text)
    ok = bool(res.get("ok"))
    if not ok:
        _enqueue(chat_id, text)
        return False
    return True


def flush_pending() -> int:
    """Replay oldest queued messages first, oldest-first; only pop entries
    confirmed delivered (ok=True). Returns number delivered this pass."""
    pending = _load_pending()
    if not pending:
        return 0
    delivered = 0
    still = []
    for m in pending:
        ok = bool(tg("sendMessage", chat_id=m["chat_id"],
                     text=m["text"]).get("ok"))
        if ok:
            delivered += 1
            log.info("replayed queued telegram msg (age %.0fs)",
                     time.time() - float(m.get("ts", time.time())))
        else:
            still.append(m)
    if still:
        _save_pending(still)
    else:
        try:
            PENDING_PATH.unlink(missing_ok=True)
        except Exception:
            pass
    return delivered


# ---------------------------------------------------------------------------
# State fetcher: reads the public raw GitHub file (no auth required)
# ---------------------------------------------------------------------------
_STATE_CACHE = config.AGENT_STATE_URL
_HTTP = None


def _get_http():
    global _HTTP
    if _HTTP is None:
        import httpx
        _HTTP = httpx.Client(timeout=20.0, follow_redirects=True)
    return _HTTP


def fetch_state() -> dict:
    """GET state.json from the public raw GitHub URL."""
    try:
        r = _get_http().get(_STATE_CACHE)
        if r.status_code == 200:
            return json.loads(r.text)
        log.warning("state fetch HTTP %d: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("state fetch failed: %s", e)
    return {}


def fetch_outcomes(state: dict) -> dict:
    """Return the ``outcomes`` section from the latest state.json."""
    return state.get("outcomes", {})


def fetch_history(state: dict, limit: int = 50) -> list[dict]:
    """Return the most recent ``limit`` history entries (newest first) from the
    latest state.  Each entry is a dict with keys:
        symbol, dir, entry, sl, tp, rr, ref, tf, ts, profile.
    """
    return state.get("history", [])[:limit]


# ---------------------------------------------------------------------------
# Risk guard state (persists across polls via ledger)
# ---------------------------------------------------------------------------
_risk: RiskManager | None = None
_ex: object | None = None


def risk() -> RiskManager:
    global _risk
    if _risk is None:
        _risk = RiskManager()
    return _risk


def get_executor():
    """Cached executor (paper or OANDA demo/live)."""
    global _ex
    if _ex is None:
        _ex = executor_for_env()
    return _ex


# ---------------------------------------------------------------------------
# Core execution loop
# ---------------------------------------------------------------------------
def _to_ref_key(entry: dict) -> str:
    """Canonical ref key: ``ref`` if present, else ``symbol:ts``."""
    ref = entry.get("ref")
    if ref is not None:
        return str(ref)
    return f"{entry.get('symbol')}:{entry.get('ts')}"


def _entry_dir(entry: dict) -> int:
    d = entry.get("dir", "")
    if d == "LONG" or d == "+1":
        return 1
    return -1


def _format_fill_message(fill: Fill, entry: dict) -> str:
    d = "LONG" if fill.direction == 1 else "SHORT"
    emoji = "\U0001F7E2" if fill.direction == 1 else "\U0001F534"
    ref = entry.get("ref")
    ref_line = f"  \u00b7 setup #{int(ref):04d}" if ref is not None else ""
    ltf_line = "\n\u2705 LTF Confirmation: M15 confirmed entry direction" if entry.get("ltf_confirmed") else ""
    return (
        f"{emoji} {fill.symbol} \u2014 AUTO-FILLED {d}{ref_line}"
        f"\n{'\u2500' * 26}"
        f"\n{d} \u00b7 fill {fill.fill_price:.5f} \u00b7 {fill.lots:.2f} lots"
        f"\nSL {entry.get('sl', 0):.5f} \u00b7 TP {entry.get('tp', 0):.5f} "
        f"\u00b7 RR 1:{entry.get('rr', 0):.2f}{ltf_line}"
        f"\nBroker: {fill.broker} \u00b7 {fill.ts}"
        f"\n{'\u2500' * 26}"
        f"\n\u26A0\ufe0f Auto-traded by GoldFX agent."
    )


def _is_rollover_blackout() -> bool:
    """True if current UTC time is within the daily rollover blackout window (e.g. 20:55 - 22:15 UTC)."""
    now_utc = dt.datetime.now(dt.timezone.utc).time()
    try:
        sh, sm = [int(x) for x in getattr(config, "ROLLOVER_START_UTC", "23:45").split(":")]
        eh, em = [int(x) for x in getattr(config, "ROLLOVER_END_UTC", "00:25").split(":")]
        start = dt.time(sh, sm)
        end = dt.time(eh, em)
        if start <= end:
            return start <= now_utc <= end
        return now_utc >= start or now_utc <= end
    except Exception:
        return False


def _is_prerollover_window() -> bool:
    """True if within the 15-minute pre-rollover de-risking window (23:30 - 23:45 UTC)."""
    now_utc = dt.datetime.now(dt.timezone.utc).time()
    try:
        sh, sm = [int(x) for x in getattr(config, "ROLLOVER_START_UTC", "23:45").split(":")]
        start_min = sh * 60 + sm
        preroll_min = max(0, start_min - 15)
        curr_min = now_utc.hour * 60 + now_utc.minute
        return preroll_min <= curr_min < start_min
    except Exception:
        return False


def handle_prerollover_guards(ledger, ex):
    """Execute Defense 2 (Margin Stress-Test) and Defense 3 (Pre-Rollover Profit Lock)."""
    if not _is_prerollover_window():
        return

    open_pos = ledger.open_entries()
    if not open_pos:
        return

    # Defense 3: Pre-Rollover Profit Lock
    if getattr(config, "CLOSE_IN_PROFIT_BEFORE_ROLLOVER", True):
        for ref_key, pos in list(open_pos.items()):
            symbol = pos.get("symbol", "")
            direction = int(pos.get("direction", 1))
            entry_p = float(pos.get("fill_price", pos.get("entry_delivered", 0.0)))
            cur_p = ex.current_price(symbol) if hasattr(ex, "current_price") else 0.0
            order_id = pos.get("order_id")
            if cur_p <= 0 or entry_p <= 0:
                continue

            # Check if trade is in profit
            is_profitable = (direction == 1 and cur_p > entry_p) or (direction == -1 and cur_p < entry_p)
            peak_mfe = float(pos.get("peak_mfe_r", 0.0))
            if is_profitable or peak_mfe >= 0.3:
                log.info("PRE_ROLLOVER_PROFIT_LOCK: Closing %s ref=%s (cur=%.5f, entry=%.5f, MFE=+%.2fR) before rollover",
                         symbol, ref_key, cur_p, entry_p, peak_mfe)
                if hasattr(ex, "close_position_by_ticket") and order_id and str(order_id).isdigit():
                    close_res = ex.close_position_by_ticket(int(order_id))
                else:
                    close_res = ex.close_position(symbol, direction, float(pos.get("lots", 0.01)))
                if close_res and close_res.ok:
                    profit_pnl = float(pos.get("risk_usd", 100.0)) * max(peak_mfe, 0.3)
                    ledger.record_outcome(ref_key, status="closed", hit="preroll_profit_lock",
                                          exit_price=cur_p, pnl_usd=profit_pnl,
                                          classification="preroll_profit_lock")
                    msg = (
                        f"\U0001F7E2 {symbol} \u2014 PRE-ROLLOVER PROFIT LOCKED"
                        f"\n{'\u2500' * 26}"
                        f"\nClosed setup #{ref_key} ahead of daily rollover."
                        f"\nProfit banked & 85-pip spread spike avoided."
                    )
                    if CHAT_ID:
                        send(CHAT_ID, msg)

    # Defense 2: Pre-Rollover Margin Stress-Test
    remaining_open = ledger.open_entries()
    if not remaining_open or not hasattr(ex, "account_snapshot"):
        return

    snap = ex.account_snapshot()
    equity = float(snap.get("equity", 0.0))
    used_margin = float(snap.get("margin", 0.0))
    if used_margin <= 0 or equity <= 0:
        return

    # Calculate worst-case spread blowout loss (80 pips on all open lots)
    total_lots = sum(float(p.get("lots", 0.01)) for p in remaining_open.values())
    projected_spread_loss = total_lots * 80.0 * 10.0  # $10/pip standard lot
    projected_equity = equity - projected_spread_loss
    projected_margin_level = (projected_equity / used_margin) * 100.0
    min_safe_level = getattr(config, "ROLLOVER_MIN_MARGIN_LEVEL_PCT", 250.0)

    if projected_margin_level < min_safe_level:
        log.warning("PRE_ROLLOVER_MARGIN_STRESS_ALERT: Projected Margin Level %.1f%% < %.1f%% minimum! De-risking now.",
                    projected_margin_level, min_safe_level)
        # De-risk: close largest remaining position to restore safe margin
        for ref_key, pos in sorted(remaining_open.items(), key=lambda x: x[1].get("lots", 0), reverse=True):
            symbol = pos.get("symbol", "")
            direction = int(pos.get("direction", 1))
            order_id = pos.get("order_id")
            cur_p = ex.current_price(symbol) if hasattr(ex, "current_price") else 0.0
            log.info("PRE_ROLLOVER_DERISK: Force-closing %s ref=%s (lots=%.2f) to protect margin",
                     symbol, ref_key, pos.get("lots", 0.01))
            if hasattr(ex, "close_position_by_ticket") and order_id and str(order_id).isdigit():
                close_res = ex.close_position_by_ticket(int(order_id))
            else:
                close_res = ex.close_position(symbol, direction, float(pos.get("lots", 0.01)))
            if close_res and close_res.ok:
                ledger.record_outcome(ref_key, status="closed", hit="preroll_derisk",
                                      exit_price=cur_p, pnl_usd=0.0, classification="preroll_margin_derisk")
                msg = (
                    f"\U0001F6E1\uFE0F {symbol} \u2014 PRE-ROLLOVER MARGIN SHIELD ACTIVATED"
                    f"\n{'\u2500' * 26}"
                    f"\nDe-risked position #{ref_key} ({pos.get('lots', 0.01):.2f} lots)."
                    f"\nAccount protected from rollover spread blowout."
                )
                if CHAT_ID:
                    send(CHAT_ID, msg)
                break


def reconcile_outcomes(ledger, outcomes: dict) -> int:

    """Check state.json outcomes for TP/SL hits on open ledger positions.
    Returns count of positions closed this tick."""
    closed = 0
    for key, o in outcomes.items():
        hit = o.get("hit")
        if hit not in ("tp", "sl"):
            continue
        for ref, pos in ledger.open_entries().items():
            # outcome key = "SYMBOL:delivered_ts"; match on the ts stored at fill
            if pos.get("signal_ts") and f"{pos.get('symbol')}:{pos.get('signal_ts')}" != key:
                continue
            # Determine PnL from the RR/risk already captured at fill time
            rr = float(pos.get("rr", 0.0))
            risk_usd = float(pos.get("risk_usd", 0.0))
            if hit == "tp":
                pnl = risk_usd * rr if rr else 0.0
                sl_class = ""
            else:
                pnl = -risk_usd if risk_usd else 0.0
                peak_mfe = float(pos.get("peak_mfe_r", 0.0))
                if peak_mfe >= 0.5:
                    sl_class = "giveback_sl"
                elif peak_mfe < 0.15:
                    sl_class = "bad_entry"
                else:
                    sl_class = "breach"
                log.info("SL hit classified ref=%s: %s (peak MFE was +%.2fR)",
                         ref, sl_class, peak_mfe)
            ledger.record_outcome(ref, status="closed", hit=hit,
                                  exit_price=float(o.get("exit_price", 0.0)),
                                  pnl_usd=pnl, classification=sl_class)
            closed += 1
            break

    # Clean up any pending retracements that concluded in outcomes before sniper entry
    pending_retrace = _load_pending_retrace()
    if pending_retrace:
        changed = False
        for key, o in outcomes.items():
            hit = o.get("hit")
            if hit not in ("tp", "sl"):
                continue
            for ref_str, item in list(pending_retrace.items()):
                if item.get("signal_ts") and f"{item.get('symbol')}:{item.get('signal_ts')}" == key:
                    log.info("RETRACE_CANCELLED ref=%s concluded in delivery outcomes as %s before sniper entry",
                             ref_str, hit)
                    del pending_retrace[ref_str]
                    changed = True
                    ledger.record_outcome(ref_str, status="skipped", hit=f"pre_entry_{hit}",
                                          pnl_usd=0.0, classification=f"retrace_concluded_{hit}")
        if changed:
            _save_pending_retrace(pending_retrace)

    return closed


def manage_open_positions(ledger) -> int:
    """Active trade management (Half B):
    - Trails SL to Break-Even when MFE reaches >= 0.8R (preventing 50% avoidable SL givebacks)
    - Exits at BE (0.0R) if price reverses back to entry after reaching BE protection
    - Tracks peak MFE for SL classification
    """
    open_trades = list(ledger.open_entries().items())
    if not open_trades:
        return 0

    ex = get_executor()
    actions = 0

    # Query active broker positions to auto-reconcile manually closed or broker-closed tickets
    active_broker_tickets = None
    if hasattr(ex, "get_open_positions"):
        try:
            bps = ex.get_open_positions()
            active_broker_tickets = {str(p["ticket"]) for p in bps} if bps is not None else None
        except Exception as be:
            log.debug("get_open_positions check failed: %s", be)

    for ref, pos in open_trades:
        order_id = pos.get("order_id")
        if active_broker_tickets is not None and order_id and str(order_id).isdigit():
            if str(order_id) not in active_broker_tickets:
                log.info("RECONCILE: ref=%s ticket=%s already closed in MT5. Marking closed.", ref, order_id)
                ledger.record_outcome(ref, status="closed", hit="broker_exit", exit_price=pos.get("fill_price", 0.0), pnl_usd=0.0, classification="broker_closed")
                actions += 1
                continue

        symbol = pos.get("symbol")
        direction = int(pos.get("direction", 1))
        fill_price = float(pos.get("fill_price", 0.0))
        sl = float(pos.get("sl", 0.0))
        tp = float(pos.get("tp", 0.0))
        risk = abs(fill_price - sl)
        if not (symbol and fill_price and sl and risk > 0):
            continue


        try:
            cur = ex.current_price(symbol) if hasattr(ex, "current_price") else 0.0
        except Exception:
            cur = 0.0
        if not cur:
            continue

        # Current MFE in R
        mfe_r = (cur - fill_price) / risk if direction == 1 else (fill_price - cur) / risk
        peak_mfe = max(float(pos.get("peak_mfe_r", 0.0)), mfe_r)
        pos["peak_mfe_r"] = round(peak_mfe, 2)

        # 0. Local TP Guard (Spread-Proof Take Profit Execution)
        # If market price reached or passed TP, close immediately to prevent missing TP due to Ask/Bid spread hover
        if getattr(config, "LOCAL_TP_GUARD_ENABLED", True):
            is_at_or_past_tp = (direction == 1 and cur >= tp) or (direction == -1 and cur <= tp)
            if is_at_or_past_tp:
                order_id = pos.get("order_id")
                lots = float(pos.get("lots", 0.01))
                rr = float(pos.get("rr", 0.0))
                log.info("LOCAL_TP_GUARD_TRIGGER ref=%s %s touched TP (cur=%.5f, tp=%.5f). Market-closing to bank TP.",
                         ref, symbol, cur, tp)
                if hasattr(ex, "close_position_by_ticket") and order_id and str(order_id).isdigit():
                    close_res = ex.close_position_by_ticket(int(order_id))
                else:
                    close_res = ex.close_position(symbol, direction, lots)

                if close_res and getattr(close_res, "ok", False):
                    profit_usd = float(pos.get("risk_usd", 100.0)) * rr if rr else 0.0
                    ledger.record_outcome(ref, status="closed", hit="tp", exit_price=cur,
                                          pnl_usd=profit_usd, classification="local_tp_guard")
                    actions += 1
                    msg = (
                        f"🎯 {symbol} — TAKE-PROFIT HIT (LOCAL TP GUARD)"
                        f"\n{'─' * 26}"
                        f"\nSetup #{ref} · Target reached at {cur:.5f} (TP: {tp:.5f})"
                        f"\nProfit: +{rr:.2f}R · ~${profit_usd:.2f} USD"
                        f"\nSpread-Proof Guard: Closed instantly at market."
                        f"\nBroker: {pos.get('broker', 'mt5')}"
                        f"\n{'─' * 26}"
                        f"\n🏆 Executed by GoldFX agent."
                    )
                    if CHAT_ID:
                        send(CHAT_ID, msg)
                    continue

        sl_state = pos.get("sl_state", "initial")

        # 1. Gold Smart Reversal Early Exit (Asset-Specific: XAUUSD only)
        # Gold has high mean-reversion whipsaws. If profit reaches >= 0.5R and an opposing M15 reversal
        # candle confirms against our position, close early to bank profit before a giveback reversal.
        if symbol == "XAUUSD" and getattr(config, "GOLD_REVERSAL_EXIT_ENABLED", True):
            min_rev_mfe = float(getattr(config, "GOLD_REVERSAL_MIN_MFE_R", 0.5))
            if mfe_r >= min_rev_mfe:
                tf_rev = getattr(config, "GOLD_REVERSAL_TF", "M15")
                candles = []
                if hasattr(ex, "get_candles"):
                    try:
                        candles = ex.get_candles(symbol, tf=tf_rev, count=3)
                    except Exception as ce:
                        log.warning("get_candles error for reversal check ref=%s: %s", ref, ce)
                if len(candles) >= 2:
                    cur_bar = candles[-1]
                    prev_bar = candles[-2]
                    is_opposing_reversal = False
                    # Opposing reversal against LONG: strong red candle closing below previous low
                    if direction == 1 and cur_bar["close"] < cur_bar["open"] and cur_bar["close"] < prev_bar["low"]:
                        is_opposing_reversal = True
                    # Opposing reversal against SHORT: strong green candle closing above previous high
                    elif direction == -1 and cur_bar["close"] > cur_bar["open"] and cur_bar["close"] > prev_bar["high"]:
                        is_opposing_reversal = True

                    if is_opposing_reversal:
                        order_id = pos.get("order_id")
                        lots = float(pos.get("lots", 0.01))
                        if hasattr(ex, "close_position_by_ticket") and order_id and str(order_id).isdigit():
                            close_res = ex.close_position_by_ticket(int(order_id))
                        else:
                            close_res = ex.close_position(symbol, direction, lots)

                        if close_res and getattr(close_res, "ok", False):
                            realized_pnl = float(pos.get("risk_usd", 100.0)) * mfe_r
                            ledger.record_outcome(ref, status="closed", hit="smart_reversal_exit",
                                                  exit_price=cur, pnl_usd=realized_pnl,
                                                  classification="gold_smart_reversal_exit")
                            actions += 1
                            msg = (
                                f"\U0001F6E1\uFE0F {symbol} \u2014 SMART REVERSAL PROFIT BANKED"
                                f"\n{'\u2500' * 26}"
                                f"\nSetup #{ref} \u00b7 Opposing {tf_rev} reversal structure detected!"
                                f"\nEarly Exit at {cur:.2f} (Banked +{mfe_r:.2f}R \u00b7 ~${realized_pnl:.2f})"
                                f"\nAvoided potential Gold liquidity giveback to SL."
                                f"\nBroker: {pos.get('broker', 'mt5')}"
                                f"\n{'\u2500' * 26}"
                                f"\n\U0001F3AF Auto-protected by GoldFX smart agent."
                            )
                            if CHAT_ID:
                                send(CHAT_ID, msg)
                            log.info("SMART_REVERSAL_EXIT ref=%s %s @ %.2f (Banked +%.2fR / $%.2f)",
                                     ref, symbol, cur, mfe_r, realized_pnl)
                            continue

        # 2. Breakeven protection: activate at >= 0.8R (Forex & Gold standard)
        if mfe_r >= 0.8 and sl_state != "be":
            pos["sl_state"] = "be"
            pos["sl_protected"] = fill_price
            if hasattr(ex, "modify_position"):
                try:
                    ex.modify_position(symbol, pos.get("order_id"), sl=fill_price, tp=tp)
                except Exception as me:
                    log.warning("modify_position ref=%s error: %s", ref, me)
            ledger.save()
            actions += 1
            msg = (
                f"\U0001F6E1\uFE0F {symbol} \u2014 SL MOVED TO BREAKEVEN"
                f"\n{'\u2500' * 26}"
                f"\nSetup #{ref} \u00b7 Peak profit +{mfe_r:.2f}R"
                f"\nStop-loss adjusted to entry {fill_price:.5f} (Risk-Free)"
                f"\nBroker: {pos.get('broker', 'mt5')}"
                f"\n{'\u2500' * 26}"
                f"\n\u26A0\ufe0f Auto-managed by GoldFX agent."
            )
            if CHAT_ID:
                send(CHAT_ID, msg)
            log.info("PROTECTED ref=%s %s SL->BE @ %.5f (peak +%.2fR)",
                     ref, symbol, fill_price, mfe_r)

        # 3. Breakeven exit: if price retraces back to entry after BE activation
        elif sl_state == "be":
            hit_be = (direction == 1 and cur <= fill_price) or (direction == -1 and cur >= fill_price)
            if hit_be:
                try:
                    ex.close_position(symbol, direction, float(pos.get("lots", 0.01)))
                except Exception as ce:
                    log.error("BE close error ref=%s: %s", ref, ce)
                ledger.record_outcome(ref, status="closed", hit="be",
                                      exit_price=fill_price, pnl_usd=0.0,
                                      classification="be_avoided_sl")
                actions += 1
                msg = (
                    f"\u26AA {symbol} \u2014 EXITED AT BREAKEVEN"
                    f"\n{'\u2500' * 26}"
                    f"\nSetup #{ref} \u00b7 Exited at entry {fill_price:.5f}"
                    f"\nP&L: 0.00 USD (0.00R) \u00b7 Avoided full -1R Stop Loss!"
                    f"\nBroker: {pos.get('broker', 'mt5')}"
                    f"\n{'\u2500' * 26}"
                    f"\n\u26A0\ufe0f Auto-managed by GoldFX agent."
                )
                if CHAT_ID:
                    send(CHAT_ID, msg)
                log.info("EXITED_BE ref=%s %s @ %.5f (Avoided SL)", ref, symbol, fill_price)

    return actions


def process_pending_retracements(ledger) -> int:
    """Evaluate active pending retracement setups (Option 1: Local M5 Swing Sniper).
    - Checks invalidation: 75% TP reached before entry -> expire
    - Checks invalidation: original SL breached before entry -> save full loss ($0 loss)
    - Checks age timeout: 6 hours
    - Detects 25%–50% pullback into setup range
    - Looks for M5 reversal candle (engulfing/shift)
    - Sets tight structural SL behind local M5 swing low/high
    - Fires 0.01 lot order if risk <= RETRACE_MAX_RISK_USD ($7.50 cap, ~$4.50 avg)
    """
    pending = _load_pending_retrace()
    if not pending:
        return 0

    ex = get_executor()
    rm = risk()
    if getattr(config, "DYNAMIC_BALANCE", True):
        try:
            if hasattr(ex, "account_snapshot"):
                snap = ex.account_snapshot()
                live_bal = float(snap.get("balance", 0.0))
                if live_bal and live_bal > 0:
                    rm.balance = live_bal
        except Exception:
            pass
    now = time.time()
    filled_count = 0
    still_pending = {}

    for ref_key, item in pending.items():
        symbol = item.get("symbol", "XAUUSD")
        direction = int(item.get("direction", 1))
        entry_delivered = float(item.get("entry_delivered", 0))
        sl_orig = float(item.get("sl_orig", 0))
        tp = float(item.get("tp", 0))
        tp_75 = float(item.get("tp_75", 0))
        pullback_threshold = float(item.get("pullback_min_dist", 0))
        first_seen = float(item.get("first_seen_ts", now))
        had_pullback = bool(item.get("had_pullback", False))
        label = item.get("label", f"goldfx #{ref_key}")

        # A. Timeout check (e.g. 6 hours)
        max_wait_sec = float(getattr(config, "RETRACE_MAX_WAIT_HOURS", 6.0)) * 3600
        if now - first_seen > max_wait_sec:
            log.info("RETRACE_EXPIRED ref=%s (age %.1fh > %.1fh)",
                     ref_key, (now - first_seen) / 3600, max_wait_sec / 3600)
            ledger.record_outcome(ref_key, status="skipped", hit="expired",
                                  pnl_usd=0.0, classification="retrace_timeout")
            continue

        # Get current price
        try:
            cur = ex.current_price(symbol) if hasattr(ex, "current_price") else 0.0
        except Exception:
            cur = 0.0

        if not cur or cur <= 0:
            still_pending[ref_key] = item
            continue

        # B. Invalidation check: 75% TP reached before pullback entry
        if (direction == 1 and cur >= tp_75) or (direction == -1 and cur <= tp_75):
            log.info("RETRACE_INVALIDATED ref=%s — hit 75%% TP before pullback (cur %.2f, tp75 %.2f)",
                     ref_key, cur, tp_75)
            ledger.record_outcome(ref_key, status="skipped", hit="invalidated_tp75",
                                  pnl_usd=0.0, classification="retrace_inval_tp75")
            msg = (
                f"\u26AA {symbol} \u2014 SETUP #{ref_key} CANCELLED (75% TP REACHED)"
                f"\n{'\u2500' * 26}"
                f"\nPrice reached 75% of target before retracing into safe discount."
                f"\nOrder cancelled to avoid chasing exhausted move."
                f"\n{'\u2500' * 26}"
                f"\n\U0001F6E1\uFE0F Capital preserved."
            )
            if CHAT_ID:
                send(CHAT_ID, msg)
            continue

        # C. Invalidation check: original SL breached before entry
        if (direction == 1 and cur <= sl_orig) or (direction == -1 and cur >= sl_orig):
            log.info("RETRACE_AVOIDED_SL ref=%s — original SL breached before entry (cur %.2f, sl %.2f)",
                     ref_key, cur, sl_orig)
            ledger.record_outcome(ref_key, status="skipped", hit="sl_breached_pre_entry",
                                  pnl_usd=0.0, classification="retrace_avoided_sl")
            msg = (
                f"\U0001F6E1\uFE0F {symbol} \u2014 SETUP #{ref_key} SL BREACH AVOIDED"
                f"\n{'\u2500' * 26}"
                f"\nMarket crashed through original SL without confirmation."
                f"\nAvoided -$25+ loss! Capital preserved: $0.00 loss."
                f"\n{'\u2500' * 26}"
                f"\n\u26A0\ufe0f Auto-protected by GoldFX agent."
            )
            if CHAT_ID:
                send(CHAT_ID, msg)
            continue

        # D. Check if pullback threshold reached
        if not had_pullback:
            if (direction == 1 and cur <= pullback_threshold) or (direction == -1 and cur >= pullback_threshold):
                had_pullback = True
                item["had_pullback"] = True
                log.info("RETRACE_PULLBACK_DETECTED ref=%s %s (cur %.2f reached threshold %.2f)",
                         ref_key, symbol, cur, pullback_threshold)

        if not had_pullback:
            still_pending[ref_key] = item
            continue

        # E. Look for M5 reversal structure across all pairs
        candles = []
        if hasattr(ex, "get_candles"):
            try:
                candles = ex.get_candles(symbol, tf="M5", count=4)
            except Exception as ce:
                log.warning("get_candles error ref=%s %s: %s", ref_key, symbol, ce)

        if len(candles) < 2:
            still_pending[ref_key] = item
            continue

        # Identify latest closed candle and previous candle
        cur_bar = candles[-1]
        prev_bar = candles[-2]

        c_info = config.CONTRACTS.get(symbol, {})
        point = float(c_info.get("point", 0.00001))
        digits = int(c_info.get("digits", 5))
        pip_val = float(c_info.get("pip_value_per_lot_usd", 1.0))
        buffer_val = float(getattr(config, "RETRACE_BUFFER_USD", 0.50)) if symbol == "XAUUSD" else (point * 15 if digits in (4, 5) else point * 2)

        is_reversal = False
        candidate_sl = None

        if direction == 1:
            # Bullish reversal: Bullish engulfing OR hammer pinbar rejection
            is_bull_engulf = (cur_bar["close"] > cur_bar["open"] and cur_bar["close"] >= prev_bar["high"])
            c_range = cur_bar["high"] - cur_bar["low"]
            lower_wick = min(cur_bar["open"], cur_bar["close"]) - cur_bar["low"]
            is_bull_pin = (c_range > 0 and lower_wick / c_range >= 0.45 and cur_bar["close"] >= cur_bar["open"])
            if is_bull_engulf or is_bull_pin:
                is_reversal = True
                candidate_sl = min(cur_bar["low"], prev_bar["low"]) - buffer_val
        else:
            # Bearish reversal: Bearish engulfing OR shooting star pinbar rejection
            is_bear_engulf = (cur_bar["close"] < cur_bar["open"] and cur_bar["close"] <= prev_bar["low"])
            c_range = cur_bar["high"] - cur_bar["low"]
            upper_wick = cur_bar["high"] - max(cur_bar["open"], cur_bar["close"])
            is_bear_pin = (c_range > 0 and upper_wick / c_range >= 0.45 and cur_bar["close"] <= cur_bar["open"])
            if is_bear_engulf or is_bear_pin:
                is_reversal = True
                candidate_sl = max(cur_bar["high"], prev_bar["high"]) + buffer_val

        if not is_reversal or candidate_sl is None:
            still_pending[ref_key] = item
            continue

        # Calculate stop distance with the new local swing stop
        candidate_sl_dist = abs(cur_bar["close"] - candidate_sl)
        min_stop_dist = point * 25 if symbol != "XAUUSD" else 1.00
        if candidate_sl_dist < min_stop_dist:
            candidate_sl_dist = min_stop_dist
            candidate_sl = cur_bar["close"] - min_stop_dist if direction == 1 else cur_bar["close"] + min_stop_dist

        # Dynamic Lot Sizing based on portfolio risk budget (6%)
        risk_usd = rm.balance * rm.risk_pct / 100.0
        lots_raw = position_size(symbol, risk_usd, candidate_sl_dist)
        lots = floor_lots(symbol, lots_raw)
        min_lot = float(c_info.get("min_lot", 0.01))

        if lots < min_lot:
            min_risk = min_lot * (candidate_sl_dist / point) * pip_val
            max_allowed = float(getattr(config, "RETRACE_MAX_RISK_USD", 7.50))
            if rm.balance < 150.0 and min_risk > max_allowed:
                log.info("RETRACE_SKIP_BAR ref=%s %s min lot risk $%.2f exceeds small account cap $%.2f",
                         ref_key, symbol, min_risk, max_allowed)
                still_pending[ref_key] = item
                continue
            lots = min_lot

        fill_entry = cur_bar["close"]
        sniper_reward = abs(tp - fill_entry)
        sniper_rr = sniper_reward / candidate_sl_dist if candidate_sl_dist > 0 else 1.0
        if sniper_rr < 0.70:
            log.info("RETRACE_SKIP_BAR ref=%s %s sniper RR %.2f < 0.70 floor", ref_key, symbol, sniper_rr)
            still_pending[ref_key] = item
            continue

        dollar_risk = lots * (candidate_sl_dist / point) * pip_val

        try:
            fill = ex.place_market_order(symbol, direction, lots, fill_entry, candidate_sl, tp, label)
        except Exception as e:
            log.error("Sniper order execution failed ref=%s: %s", ref_key, e)
            still_pending[ref_key] = item
            continue

        if not fill.ok:
            log.warning("Sniper order rejected ref=%s: %s", ref_key, fill.message[:200])
            still_pending[ref_key] = item
            continue

        # Record in ledger!
        fmt_digits = 2 if symbol == "XAUUSD" else digits
        ledger.data[str(ref_key)] = {
            "ref": ref_key,
            "symbol": symbol,
            "direction": direction,
            "lots": lots,
            "fill_price": fill.fill_price,
            "entry_delivered": entry_delivered,
            "sl": candidate_sl,
            "orig_sl": sl_orig,
            "tp": tp,
            "rr": round(sniper_rr, 2),
            "risk_usd": round(dollar_risk, 2),
            "order_id": fill.order_id,
            "broker": fill.broker,
            "ts": fill.ts,
            "signal_ts": item.get("signal_ts", ""),
            "status": "open",
            "sl_state": "initial",
            "peak_mfe_r": 0.0,
            "profile": item.get("profile", ""),
            "sniper_entry": True,
            "ltf_confirmed": True,
        }
        ledger.save()

        # Format Telegram announcement
        d_str = "LONG" if direction == 1 else "SHORT"
        emoji = "🟢" if direction == 1 else "🔴"
        orig_dist = abs(entry_delivered - sl_orig)
        msg = (
            f"{emoji} {symbol} — SNIPER M5 MICRO-SHIFT FILLED {d_str}\n"
            f"{'─' * 26}\n"
            f"Setup #{ref_key} · Fill: {fill.fill_price:.{fmt_digits}f} · {lots:.2f} lots\n"
            f"New SL: {candidate_sl:.{fmt_digits}f} (Local M5 Swing · Risk: ${dollar_risk:.2f})\n"
            f"TP: {tp:.{fmt_digits}f} · Sniper R:R: 1:{sniper_rr:.2f}\n"
            f"Original Stop Distance was {orig_dist:.{fmt_digits}f}\n"
            f"Broker: {fill.broker} · {fill.ts}\n"
            f"{'─' * 26}\n"
            f"🎯 Executed via GoldFX M5 Micro-Shift Engine."
        )
        if CHAT_ID:
            send(CHAT_ID, msg)
        log.info("SNIPER_FILLED ref=%s %s %s @ %.5f (SL %.5f, Risk $%.2f, RR 1:%.2f)",
                 ref_key, symbol, d_str, fill.fill_price, candidate_sl, dollar_risk, sniper_rr)
        filled_count += 1

    _save_pending_retrace(still_pending)
    return filled_count


def tick() -> bool:
    """One poll cycle: fetch state → fire new fills → manage open positions → reconcile outcomes.
    Returns True if any new fill was fired."""
    state = fetch_state()
    if not state:
        return False
    # Drain any Telegram messages queued while the uplink was down —
    # never silently eat a fill announcement again.
    try:
        drained = flush_pending()
        if drained:
            log.info("drained %d queued telegram message(s)", drained)
    except Exception as e:
        log.warning("flush_pending error: %s", e)
    history = fetch_history(state)
    outcomes = fetch_outcomes(state)
    ledger = load_ledger()
    rm = risk()
    if getattr(config, "DYNAMIC_BALANCE", True):
        try:
            ex = get_executor()
            if hasattr(ex, "account_snapshot"):
                snap = ex.account_snapshot()
                live_bal = float(snap.get("balance", 0.0))
                if live_bal and live_bal > 0:
                    rm.balance = live_bal
        except Exception:
            pass

    # Execute Pre-Rollover Guards (Defense 2: Margin Stress-Test, Defense 3: Profit Lock)
    try:
        ex = get_executor()
        handle_prerollover_guards(ledger, ex)
    except Exception as e:
        log.warning("handle_prerollover_guards error: %s", e)

    fired_any = False

    # Defense 1: Portfolio Risk Budgeting & Capacity Guard
    max_trades = int(getattr(config, "MAX_CONCURRENT_TRADES", 4))
    max_trades_per_sym = int(getattr(config, "MAX_TRADES_PER_SYMBOL", 2))
    max_portfolio_risk_pct = float(getattr(config, "MAX_PORTFOLIO_RISK_PCT", 18.0))

    target_broker = getattr(ex, "broker", "mt5")
    open_positions = [
        e for e in ledger.open_entries().values()
        if e.get("broker", "mt5") == target_broker
    ]
    active_open_count = len(open_positions)

    # 1. Execute new setups (oldest first for correct sequence)
    for entry in reversed(history):
        ref_key = _to_ref_key(entry)
        if ledger.has(ref_key):
            continue
        symbol = entry.get("symbol", "")
        direction = _entry_dir(entry)

        # Check 1: Max concurrent open trades across all pairs
        if active_open_count >= max_trades:
            log.info("skip %s ref=%s — max concurrent trades reached (%d/%d active)",
                     symbol, ref_key, active_open_count, max_trades)
            continue

        # Check 2: Max concurrent trades on this specific symbol
        sym_open_count = len([e for e in open_positions if e.get("symbol") == symbol])
        if sym_open_count >= max_trades_per_sym:
            log.info("skip %s ref=%s — symbol concentration limit reached (%d/%d on %s)",
                     symbol, ref_key, sym_open_count, max_trades_per_sym, symbol)
            continue

        # Check 3: Cumulative portfolio unprotected risk (trades at Breakeven have $0 risk!)
        unprotected_risk_usd = sum(
            float(e.get("risk_usd", 0.0)) for e in open_positions
            if e.get("sl_state") != "be"
        )
        current_unprotected_risk_pct = (unprotected_risk_usd / rm.balance * 100.0) if rm.balance > 0 else 0.0
        if current_unprotected_risk_pct + rm.risk_pct > max_portfolio_risk_pct:
            log.info("skip %s ref=%s — portfolio risk limit reached (open risk %.1f%% + new %.1f%% > max %.1f%%)",
                     symbol, ref_key, current_unprotected_risk_pct, rm.risk_pct, max_portfolio_risk_pct)
            continue

        entry_price = float(entry.get("entry", 0))
        sl = float(entry.get("sl", 0))
        tp = float(entry.get("tp", 0))
        rr = float(entry.get("rr", 0))
        signal_ts = str(entry.get("ts", ""))
        if not (symbol and entry_price and sl and tp):
            continue
        if symbol not in SYMBOL_RUNTIME:
            continue
        # Check if the setup has already concluded in delivery bot outcomes
        outcome_key = f"{symbol}:{signal_ts}"
        concluded = outcomes.get(str(ref_key)) or outcomes.get(outcome_key)
        if concluded and concluded.get("hit") in ("tp", "sl"):
            hit_type = concluded.get("hit")
            log.info("skip %s ref=%s — already concluded in outcomes as %s before execution",
                     symbol, ref_key, hit_type)
            ledger.record_outcome(ref_key, status="skipped", hit=f"pre_entry_{hit_type}",
                                  pnl_usd=0.0, classification=f"already_concluded_{hit_type}")
            continue

        # Rollover Blackout Check (e.g. 20:55 - 22:15 UTC)
        if _is_rollover_blackout():
            log.info("skip %s ref=%s — rollover blackout active (%s - %s UTC). New entries paused.",
                     symbol, ref_key, getattr(config, "ROLLOVER_START_UTC", "20:55"), getattr(config, "ROLLOVER_END_UTC", "22:15"))
            continue

        try:
            ex = get_executor()

            # Spread Filter Check
            if hasattr(ex, "get_spread"):
                spread_val = ex.get_spread(symbol)
                c_info = config.CONTRACTS.get(symbol, {})
                digits = c_info.get("digits", 5)
                pip_size = 0.0001 if digits in (4, 5) else (0.01 if digits in (2, 3) else c_info.get("point", 0.00001))
                spread_pips = spread_val / pip_size if pip_size > 0 else 0.0

                max_spread = config.MAX_SPREAD_GOLD if symbol == "XAUUSD" else config.MAX_SPREAD_PIPS
                curr_metric = spread_val if symbol == "XAUUSD" else spread_pips
                metric_unit = "$" if symbol == "XAUUSD" else "pips"

                if curr_metric > max_spread:
                    log.info("skip %s ref=%s — spread too wide (%.2f %s > max %.2f %s). Awaiting normal liquidity.",
                             symbol, ref_key, curr_metric, metric_unit, max_spread, metric_unit)
                    continue

            # Fetch LIVE price first for pre-entry sanity and accurate dynamic sizing
            cur = ex.current_price(symbol) if hasattr(ex, "current_price") else 0.0
            if not cur or cur <= 0:
                cur = entry_price

            # Check A: Never fire a setup whose SL is already breached
            if (direction == 1 and cur <= sl) or (direction == -1 and cur >= sl):
                log.info("skip %s ref=%s — SL already breached (cur %.5f, sl %.5f)",
                         symbol, ref_key, cur, sl)
                ledger.record_outcome(ref_key, status="skipped", hit="pre_entry_sl_breached",
                                      pnl_usd=0.0, classification="sl_already_breached")
                continue

            # Check B: Never fire if price already reached TP
            if (direction == 1 and cur >= tp) or (direction == -1 and cur <= tp):
                log.info("skip %s ref=%s — TP already reached (cur %.5f, tp %.5f)",
                         symbol, ref_key, cur, tp)
                ledger.record_outcome(ref_key, status="skipped", hit="pre_entry_tp_reached",
                                      pnl_usd=0.0, classification="tp_already_reached")
                continue

            orig_sl_dist = abs(entry_price - sl)
            orig_tp_dist = abs(tp - entry_price)

            # Check C: Adverse drift check (price fell too far towards SL)
            max_adverse_pct = float(getattr(config, "MAX_ADVERSE_DRIFT_PCT", 0.35))
            if (direction == 1 and cur < entry_price - max_adverse_pct * orig_sl_dist) or \
               (direction == -1 and cur > entry_price + max_adverse_pct * orig_sl_dist):
                log.info("skip %s ref=%s — price drifted too far adverse from entry zone (cur %.5f, entry %.5f)",
                         symbol, ref_key, cur, entry_price)
                ledger.record_outcome(ref_key, status="skipped", hit="pre_entry_adverse_drift",
                                      pnl_usd=0.0, classification="adverse_drift_exceeded")
                continue

            # Check D: Anti-Chase Guard (price already ran >25% towards TP)
            max_chase_pct = float(getattr(config, "MAX_CHASE_TP_PCT", 0.25))
            if (direction == 1 and cur > entry_price + max_chase_pct * orig_tp_dist) or \
               (direction == -1 and cur < entry_price - max_chase_pct * orig_tp_dist):
                log.info("skip %s ref=%s — anti-chase guard: price already ran >%.0f%% towards TP (cur %.5f, entry %.5f, tp %.5f)",
                         symbol, ref_key, max_chase_pct * 100, cur, entry_price, tp)
                if getattr(config, "RETRACE_ENABLED", True):
                    pending_retrace = _load_pending_retrace()
                    if str(ref_key) not in pending_retrace:
                        tp_75 = entry_price + (tp - entry_price) * getattr(config, "RETRACE_INVAL_TP_PCT", 0.75)
                        pb_thresh = entry_price
                        pending_retrace[str(ref_key)] = {
                            "ref": ref_key,
                            "symbol": symbol,
                            "direction": direction,
                            "entry_delivered": entry_price,
                            "sl_orig": sl,
                            "tp": tp,
                            "orig_risk": orig_sl_dist,
                            "orig_rr": rr,
                            "signal_ts": signal_ts,
                            "first_seen_ts": time.time(),
                            "tp_75": tp_75,
                            "pullback_min_dist": pb_thresh,
                            "had_pullback": False,
                            "profile": entry.get("profile", ""),
                            "label": f"goldfx #{ref_key}" if str(ref_key).isdigit() else f"goldfx {ref_key}",
                        }
                        _save_pending_retrace(pending_retrace)
                        d_str = "LONG" if direction == 1 else "SHORT"
                        digits_fmt = 2 if symbol == "XAUUSD" else config.CONTRACTS.get(symbol, {}).get("digits", 5)
                        msg = (
                            f"🟡 {symbol} — PENDING M5 PULLBACK SNIPER {d_str}\n"
                            f"{'─' * 26}\n"
                            f"Setup #{ref_key} · {d_str} @ {cur:.{digits_fmt}f}\n"
                            f"Price ran >{max_chase_pct*100:.0f}% towards TP ({cur:.{digits_fmt}f} vs {entry_price:.{digits_fmt}f}).\n"
                            f"Anti-Chase active: Bot will wait for M5 pullback to entry zone ({entry_price:.{digits_fmt}f}) + M5 reversal structure to enter safely.\n"
                            f"{'─' * 26}\n"
                            f"🛡️ Capital preservation & entry optimization active."
                        )
                        if CHAT_ID:
                            send(CHAT_ID, msg)
                        log.info("ENQUEUED_RETRACE %s ref=%s — price ran >%.0f%% towards TP; waiting for M5 pullback to entry zone %.5f",
                                 symbol, ref_key, max_chase_pct * 100, pb_thresh)
                else:
                    ledger.record_outcome(ref_key, status="skipped", hit="pre_entry_chase_avoided",
                                          pnl_usd=0.0, classification="chase_avoided")
                continue

            # ---- Gold Quarantine & Small Account Capital Shield ----
            # Standard Gold requires $8.60 margin on 0.01 lot (1:500 leverage).
            # On a Standard account under $50, this creates immediate stop-out risk on minor drawdowns.
            # On Cent accounts (where balance is in cents e.g. 1000 USC = $10), Gold is fully permitted.
            is_cent_account = getattr(config, "IS_CENT_ACCOUNT", False)
            if not is_cent_account and hasattr(ex, "account_snapshot"):
                try:
                    snap = ex.account_snapshot()
                    acc_curr = str(snap.get("currency", "")).upper()
                    if "CENT" in acc_curr or "USC" in acc_curr:
                        is_cent_account = True
                except Exception:
                    pass

            if symbol == "XAUUSD" and not is_cent_account and getattr(config, "GOLD_QUARANTINE_ENABLED", True):
                min_gold_abs = float(getattr(config, "MIN_GOLD_ABSOLUTE_BALANCE", 50.0))
                min_gold_std = float(getattr(config, "MIN_GOLD_BALANCE", 100.0))

                # Tier 1: Total Quarantine for Standard balances < $50
                if rm.balance < min_gold_abs:
                    log.info("🛡️ QUARANTINE XAUUSD ref=%s — balance ($%.2f) below $%.2f minimum for Standard Gold. Forex pairs active.",
                             ref_key, rm.balance, min_gold_abs)
                    ledger.record_outcome(ref_key, status="quarantined", hit="gold_quarantined_low_balance",
                                          pnl_usd=0.0, classification="gold_small_account_quarantine")
                    msg = (
                        f"🛡️ XAUUSD — TRADE QUARANTINED (Small Account Protection)"
                        f"\n{'─' * 26}"
                        f"\nSetup #{ref_key} · Gold entry blocked"
                        f"\nAccount Balance: ${rm.balance:.2f} (Below ${min_gold_abs:.0f} standard minimum)"
                        f"\nReason: Standard Gold requires $8.60 margin on 0.01 lot."
                        f"\nPreserving capital for safer Forex setups (EUR, GBP, AUD, CAD, NZD, CHF)."
                        f"\n💡 Tip: To trade Gold with ${rm.balance:.0f}, switch to a Headway Cent Account!"
                        f"\n{'─' * 26}"
                    )
                    if CHAT_ID:
                        send(CHAT_ID, msg)
                    continue

                # Tier 2: Balances between $50 and $100 must use M5 Retrace Sniper only
                if rm.balance < min_gold_std and not getattr(config, "RETRACE_ENABLED", False):
                    log.info("🛡️ QUARANTINE XAUUSD ref=%s — balance ($%.2f) < $%.2f and retrace sniper disabled. Standard market entry blocked.",
                             ref_key, rm.balance, min_gold_std)
                    ledger.record_outcome(ref_key, status="quarantined", hit="gold_standard_entry_quarantined",
                                          pnl_usd=0.0, classification="gold_requires_sniper")
                    continue


            # Dynamic Sizing based on ACTUAL LIVE MARKET PRICE (cur)
            # This guarantees dollar risk NEVER exceeds the 6% budget!
            actual_sl_dist = abs(cur - sl)
            actual_tp_dist = abs(tp - cur)
            if actual_sl_dist <= 0:
                continue
            actual_rr = actual_tp_dist / actual_sl_dist
            if actual_rr < 0.60:
                log.info("skip %s ref=%s — live RR %.2f too low from current market price (cur %.5f, sl %.5f, tp %.5f)",
                         symbol, ref_key, actual_rr, cur, sl, tp)
                continue

            risk_usd = rm.balance * rm.risk_pct / 100.0
            lots_raw = position_size(symbol, risk_usd, actual_sl_dist)
            lots = floor_lots(symbol, lots_raw)

            if lots < 0.01:
                c = config.CONTRACTS.get(symbol, {})
                min_risk = 0.01 * (actual_sl_dist / c.get("point", 0.01)) * c.get("pip_value_per_lot_usd", 1.0)
                if getattr(config, "RETRACE_ENABLED", True):
                    pending_retrace = _load_pending_retrace()
                    if str(ref_key) not in pending_retrace:
                        tp_75 = entry_price + (tp - entry_price) * getattr(config, "RETRACE_INVAL_TP_PCT", 0.75)
                        pb_thresh = entry_price - direction * (orig_sl_dist * getattr(config, "RETRACE_MIN_PULLBACK_PCT", 0.25))
                        pending_retrace[str(ref_key)] = {
                            "ref": ref_key,
                            "symbol": symbol,
                            "direction": direction,
                            "entry_delivered": entry_price,
                            "sl_orig": sl,
                            "tp": tp,
                            "orig_risk": actual_sl_dist,
                            "orig_rr": rr,
                            "signal_ts": signal_ts,
                            "first_seen_ts": time.time(),
                            "tp_75": tp_75,
                            "pullback_min_dist": pb_thresh,
                            "had_pullback": False,
                            "profile": entry.get("profile", ""),
                            "label": f"goldfx #{ref_key}" if str(ref_key).isdigit() else f"goldfx {ref_key}",
                        }
                        _save_pending_retrace(pending_retrace)
                        log.info("ENQUEUED_RETRACE %s ref=%s — waiting for M5 pullback (live SL dist $%.2f > $%.2f risk budget)",
                                 symbol, ref_key, actual_sl_dist, risk_usd)
                        d_str = "LONG" if direction == 1 else "SHORT"
                        msg = (
                            f"\U0001F7E1 {symbol} \u2014 PENDING M5 PULLBACK SNIPER"
                            f"\n{'\u2500' * 26}"
                            f"\nSetup #{ref_key} \u00b7 {d_str} @ {cur:.2f}"
                            f"\nLive SL distance (${actual_sl_dist:.2f}) exceeds our ${risk_usd:.2f} risk budget ({rm.risk_pct}%)."
                            f"\nBot will wait for 25%–50% pullback + local M5 reversal structure to enter safely."
                            f"\n{'\u2500' * 26}"
                            f"\n\u26A0\ufe0f Capital preservation guard active."
                        )
                        if CHAT_ID:
                            send(CHAT_ID, msg)
                    continue
                log.info("skip %s ref=%s — required lot (%.4f) < broker min (0.01). Min lot would risk $%.2f (%.1f%% of balance), exceeding our %.1f%% budget ($%.2f). Capital preserved.",
                         symbol, ref_key, lots_raw, min_risk, (min_risk / rm.balance) * 100, rm.risk_pct, risk_usd)
                continue

            label = f"goldfx #{ref_key}" if str(ref_key).isdigit() else f"goldfx {ref_key}"
            fill = ex.place_market_order(symbol, direction, lots, cur, sl, tp, label)

        except Exception as e:
            log.error("execution failed for ref=%s: %s", ref_key, e)
            continue
        if not fill.ok:
            log.warning("order rejected ref=%s: %s", ref_key, fill.message[:200])
            continue
        # Record in ledger with full sizing and exit management context
        ledger.data[str(ref_key)] = {
            "ref": ref_key,
            "symbol": symbol,
            "direction": direction,
            "lots": lots,
            "fill_price": fill.fill_price,
            "entry_delivered": entry_price,
            "sl": sl,
            "tp": tp,
            "rr": round(actual_rr, 2),
            "risk_usd": round(risk_usd, 2),
            "order_id": fill.order_id,
            "broker": fill.broker,
            "ts": fill.ts,
            "signal_ts": signal_ts,
            "status": "open",
            "sl_state": "initial",
            "peak_mfe_r": 0.0,
            "profile": entry.get("profile", ""),
            "ltf_confirmed": entry.get("ltf_confirmed", False),
        }
        ledger.save()
        msg = _format_fill_message(fill, entry)
        if CHAT_ID:
            send(CHAT_ID, msg)
        log.info("FILLED ref=%s %s %s @ %.5f %.2f lots",
                 ref_key, symbol, "LONG" if direction == 1 else "SHORT",
                 fill.fill_price, lots)
        fired_any = True
        active_open_count += 1
        open_positions.append(ledger.data[str(ref_key)])


    # 2. Process pending retracements (Option 1: M5 Swing Sniper for Gold)
    retrace_filled = process_pending_retracements(ledger)
    if retrace_filled:
        fired_any = True
        log.info("processed pending retracements, fired %d sniper orders", retrace_filled)

    # 3. Manage open positions (Half B: Breakeven trailing & early exit)
    managed = manage_open_positions(ledger)
    if managed:
        log.info("active exit management processed %d positions", managed)

    # 4. Reconcile outcomes (TP/SL) from the delivery bot
    reconciled = reconcile_outcomes(ledger, outcomes)
    if reconciled:
        log.info("reconciled %d outcomes", reconciled)

    return fired_any


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run_agent():
    interval = int(config.AGENT_POLL_SEC)
    log.info("goldfx-agent starting  env=%s  poll=%ds  state_url=%s",
             config.AUTO_TRADE_ENV, interval, _STATE_CACHE)
    while True:
        try:
            tick()
        except Exception as e:
            log.error("tick error: %s", e, exc_info=True)
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    run_agent()
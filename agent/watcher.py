# -*- coding: utf-8 -*-
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

TOKEN = getattr(config, "AGENT_BOT_TOKEN", config.BOT_TOKEN)
CHAT_ID = getattr(config, "AGENT_CHAT_ID", config.CHAT_ID)


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


PENDING_TURTLE_SOUP_PATH = Path(__file__).resolve().parents[1] / "data" / "pending_turtle_soup.json"


def _load_pending_turtle_soup() -> dict:
    try:
        return json.loads(PENDING_TURTLE_SOUP_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_pending_turtle_soup(pending: dict) -> None:
    try:
        PENDING_TURTLE_SOUP_PATH.parent.mkdir(exist_ok=True)
        PENDING_TURTLE_SOUP_PATH.write_text(json.dumps(pending, indent=2, ensure_ascii=False),
                                            encoding="utf-8")
    except Exception as e:
        log.warning("pending turtle soup save failed: %s", e)


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
    # Deduplicate: do not queue duplicate message text
    if any(m.get("chat_id") == str(chat_id) and m.get("text") == text for m in pending):
        log.debug("telegram _enqueue skipped duplicate pending message")
        return
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
    """GET state.json from configured URL, with automatic fallback to public raw GitHub URL."""
    urls = [_STATE_CACHE]
    raw_github = "https://raw.githubusercontent.com/Glitch-WRLD/goldfx-github/main/gha_state/state.json"
    if raw_github not in urls:
        urls.append(raw_github)

    for u in urls:
        try:
            r = _get_http().get(u)
            if r.status_code == 200:
                return json.loads(r.text)
            log.warning("state fetch (%s) HTTP %d", u, r.status_code)
        except Exception as e:
            log.warning("state fetch (%s) failed: %s", u, e)

    # Local disk fallback if available
    local_state = Path(__file__).resolve().parents[1] / "gha_state" / "state.json"
    if local_state.exists():
        try:
            return json.loads(local_state.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("local state read failed: %s", e)
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
    badge = entry.get("strategy_badge", "⚡ Momentum FVG")
    ltf_line = f"\n🎯 Concept: {badge}"
    if entry.get("ltf_confirmed"):
        ltf_line += f" · [{entry.get('entry_tf', 'M15')} Confirmed]"
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


def _is_us_open_cooldown(symbol: str) -> bool:
    """True if symbol is an equity index and current time is in the 13:25 - 13:45 UTC opening bell window."""
    if not getattr(config, "US_OPEN_BUFFER_ENABLED", True):
        return False
    if symbol not in getattr(config, "INDEX_SYMBOLS", {"NASDAQ-100", "US500", "DJ30"}):
        return False
    now_utc = dt.datetime.now(dt.timezone.utc).time()
    try:
        sh, sm = [int(x) for x in getattr(config, "US_OPEN_START_UTC", "13:25").split(":")]
        eh, em = [int(x) for x in getattr(config, "US_OPEN_END_UTC", "13:45").split(":")]
        return dt.time(sh, sm) <= now_utc <= dt.time(eh, em)
    except Exception:
        return False


def _is_london_open_cooldown(symbol: str) -> bool:
    """True if symbol is Forex or Gold and current time is in the 06:50 - 07:20 UTC London cash open window."""
    if not getattr(config, "LONDON_OPEN_BUFFER_ENABLED", True):
        return False
    london_symbols = getattr(
        config,
        "LONDON_OPEN_SYMBOLS",
        {"XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "NZDUSD", "USDCHF", "GBPAUD"},
    )
    if symbol not in london_symbols:
        return False
    now_utc = dt.datetime.now(dt.timezone.utc).time()
    try:
        sh, sm = [int(x) for x in getattr(config, "LONDON_OPEN_START_UTC", "06:50").split(":")]
        eh, em = [int(x) for x in getattr(config, "LONDON_OPEN_END_UTC", "07:20").split(":")]
        return dt.time(sh, sm) <= now_utc <= dt.time(eh, em)
    except Exception:
        return False


def _is_evening_exhaustion_window() -> bool:
    """True if current time is between 17:00 and 24:00 UTC (Late NY / Rollover exhaustion window)."""
    if not getattr(config, "SESSION_EVENING_FILTER_ENABLED", True):
        return False
    now_utc = dt.datetime.now(dt.timezone.utc)
    sh = getattr(config, "SESSION_EVENING_START_UTC", 17)
    eh = getattr(config, "SESSION_EVENING_END_UTC", 24)
    return sh <= now_utc.hour < eh


def _is_htf_counter_trend(symbol: str, direction: int) -> tuple[bool, str]:
    """Phase 1 Rule 1: Check if trade direction is counter to BOTH H1 and H4 50 EMAs.
    Returns (True, reason) if fighting both H1 and H4 (10.6% historical WR).
    Returns (False, "OK") if aligned with at least one timeframe or if data unavailable.
    """
    if not getattr(config, "HTF_FILTER_ENABLED", True):
        return False, "FILTER_DISABLED"

    try:
        from engine.scanner import check_htf_alignment
        ok, reason = check_htf_alignment(symbol, direction)
        return not ok, reason
    except Exception as e:
        log.warning("_is_htf_counter_trend check error for %s: %s", symbol, e)
        return False, "ERROR_FALLBACK"


def handle_prenew_guards(ledger, ex) -> int:
    """Pre-News Defense: 15 mins before high-impact news, move SL on profitable open trades to BE (+1 pip)."""
    if not getattr(config, "NEWS_PROTECT_PROFITS", True):
        return 0
    from engine.news import get_active_news_blackout
    actions = 0
    for ref, pos in list(ledger.open_entries().items()):
        symbol = pos.get("symbol")
        sl_state = pos.get("sl_state", "initial")
        if sl_state in ("be", "trail_05", "trail_10"):
            continue  # already protected!

        is_bo, reason, ev = get_active_news_blackout(symbol, buffer_before_min=15, buffer_after_min=0)
        if not is_bo:
            continue

        fill_price = float(pos.get("fill_price", 0.0))
        sl = float(pos.get("sl", 0.0))
        direction = int(pos.get("direction", 1))
        risk = abs(fill_price - sl)
        if risk <= 0:
            continue
        try:
            cur = ex.current_price(symbol, direction=direction) if hasattr(ex, "current_price") else 0.0
        except Exception:
            cur = 0.0
        if not cur or cur <= 0:
            continue
        mfe_r = (cur - fill_price) / risk if direction == 1 else (fill_price - cur) / risk
        # If trade is in profit (>= +0.3R), lock to Breakeven (+1 pip) before the news release!
        if mfe_r >= 0.3:
            c_info = config.CONTRACTS.get(symbol, {})
            point = float(c_info.get("point", 0.00001))
            pip_buffer = point * 10 if c_info.get("digits", 5) in (4, 5) else point
            new_sl = fill_price + pip_buffer if direction == 1 else fill_price - pip_buffer
            order_id = pos.get("order_id")
            if hasattr(ex, "modify_sl_tp") and order_id and str(order_id).isdigit():
                mod_res = ex.modify_sl_tp(int(order_id), new_sl, float(pos.get("tp", 0.0)))
                if mod_res and getattr(mod_res, "ok", False):
                    pos["sl"] = new_sl
                    pos["sl_state"] = "be"
                    ledger.save()
                    actions += 1
                    log.info("PRE_NEWS_BE ref=%s %s moved to BE (+1 pip) ahead of %s", ref, symbol, reason)
                    msg = (
                        f"🛡️ {symbol} — PRE-NEWS BREAK-EVEN SHIELD\n"
                        f"{'─' * 26}\n"
                        f"Setup #{ref} · SL moved to BE ({new_sl:.5f})\n"
                        f"Profit secured before High-Impact News: {reason}\n"
                        f"Capital protected against volatility shockwave."
                    )
                    if CHAT_ID:
                        send(CHAT_ID, msg)
    return actions


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
            cur_p = ex.current_price(symbol, direction=direction) if hasattr(ex, "current_price") else 0.0
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
            cur_p = ex.current_price(symbol, direction=direction) if hasattr(ex, "current_price") else 0.0
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
    ex = get_executor()
    active_broker_tickets = None
    if hasattr(ex, "get_open_positions"):
        try:
            bps = ex.get_open_positions()
            active_broker_tickets = {str(p["ticket"]) for p in bps} if bps is not None else None
        except Exception:
            pass

    for key, o in outcomes.items():
        hit = o.get("hit")
        if hit not in ("tp", "sl"):
            continue
        for ref, pos in ledger.open_entries().items():
            # outcome key = "SYMBOL:delivered_ts"; match on the ts stored at fill
            if pos.get("signal_ts") and f"{pos.get('symbol')}:{pos.get('signal_ts')}" != key:
                continue

            order_id = pos.get("order_id")
            # If the trade has an active ticket running on MT5, broker truth takes precedence!
            if active_broker_tickets is not None and order_id and str(order_id) in active_broker_tickets:
                log.debug("RECONCILE_WAIT: ref=%s ticket=%s still active on MT5 broker (state.json hit=%s). Keeping position open & active.",
                          ref, order_id, hit)
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

    # Broker synchronization: if a position with a live MT5 ticket is marked non-open, revive it to open
    if active_broker_tickets is not None:
        revived = False
        for ref_str, pos_item in list(ledger.data.items()):
            if pos_item.get("status") != "open" and pos_item.get("order_id"):
                if str(pos_item["order_id"]) in active_broker_tickets:
                    log.warning("RESYNC_REVIVE: ref=%s ticket=%s still active on MT5 broker! Restoring status to 'open'.",
                                ref_str, pos_item["order_id"])
                    pos_item["status"] = "open"
                    pos_item.pop("closed_ts", None)
                    pos_item.pop("hit", None)
                    pos_item.pop("exit_price", None)
                    pos_item.pop("pnl_usd", None)
                    pos_item.pop("classification", None)
                    revived = True
        if revived:
            ledger.save()

    open_trades = list(ledger.open_entries().items())
    if not open_trades:
        return 0

    for ref, pos in open_trades:
        order_id = pos.get("order_id")
        if active_broker_tickets is not None and order_id and str(order_id).isdigit():
            if str(order_id) not in active_broker_tickets:
                deal = ex.get_closed_deal(order_id) if hasattr(ex, "get_closed_deal") else None
                if deal is not None:
                    # Verified closed by broker history!
                    exit_price = deal.get("price", pos.get("fill_price", 0.0))
                    pnl_raw = deal.get("profit", 0.0)
                    comment = deal.get("comment", "").lower()
                    reason = deal.get("reason", 0)
                    sl_state = pos.get("sl_state", "initial")
                    peak_mfe = float(pos.get("peak_mfe_r", 0.0))

                    if sl_state == "trail_10":
                        hit = "tp"
                        classification = "broker_trailed_profit_10"
                    elif sl_state == "trail_05":
                        hit = "tp"
                        classification = "broker_trailed_profit_05"
                    elif reason == 5 or "[tp" in comment or pnl_raw > 0.5:
                        hit = "tp"
                        classification = "broker_tp"
                    elif sl_state == "be" or "[be" in comment or abs(pnl_raw) < 1.0:
                        hit = "be"
                        classification = "be_avoided_sl"
                    else:
                        hit = "sl"
                        if peak_mfe >= 0.5:
                            classification = "giveback_sl"
                        elif peak_mfe < 0.15:
                            classification = "bad_entry"
                        else:
                            classification = "breach"

                        # Phase 2: Enqueue stopped-out trade for Turtle Soup / Inducement Sweep Re-Entry
                        if getattr(config, "TURTLE_SOUP_REENTRY_ENABLED", True) and not str(ref).startswith("soup_"):
                            try:
                                pending_soup = _load_pending_turtle_soup()
                                ref_str = str(ref)
                                if ref_str not in pending_soup:
                                    fill_price_val = float(pos.get("fill_price", 0.0))
                                    sl_price_val = float(pos.get("sl", 0.0))
                                    tp_price_val = float(pos.get("tp", 0.0))
                                    sl_dist_val = abs(fill_price_val - sl_price_val)
                                    direction_val = int(pos.get("direction", 1))
                                    if sl_dist_val > 0 and tp_price_val > 0:
                                        pending_soup[ref_str] = {
                                            "ref": ref,
                                            "symbol": pos.get("symbol"),
                                            "direction": direction_val,
                                            "original_entry": fill_price_val,
                                            "original_sl": sl_price_val,
                                            "original_tp": tp_price_val,
                                            "sl_dist": sl_dist_val,
                                            "risk_usd": float(pos.get("risk_usd", 100.0)),
                                            "exit_price": float(exit_price),
                                            "sweep_extreme": float(exit_price),
                                            "exit_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                                        }
                                        _save_pending_turtle_soup(pending_soup)
                                        log.info("TURTLE_SOUP_ENQUEUED: ref=%s symbol=%s dir=%d SL=%.5f exit=%.5f (monitoring for inducement sweep re-entry)",
                                                 ref_str, pos.get("symbol"), direction_val, sl_price_val, exit_price)
                            except Exception as e:
                                log.warning("Failed to enqueue turtle soup for ref=%s: %s", ref, e)


                    log.info("RECONCILE_VERIFIED: ref=%s ticket=%s closed in MT5. hit=%s pnl=%.2f exit=%.5f class=%s",
                             ref, order_id, hit, pnl_raw, exit_price, classification)
                    ledger.record_outcome(ref, status="closed", hit=hit, exit_price=exit_price,
                                          pnl_usd=pnl_raw, classification=classification)
                    actions += 1

                    msg = (
                        f"{'🎯' if hit == 'tp' else ('⚪' if hit == 'be' else '🛑')} {pos.get('symbol')} — {hit.upper()} (BROKER CLOSED)"
                        f"\n{'─' * 26}"
                        f"\nSetup #{ref} · Exit price {exit_price:.5f}"
                        f"\nRealized P&L: {pnl_raw:+.2f} ({classification})"
                        f"\nPeak MFE reached: +{peak_mfe:.2f}R"
                        f"\nBroker: {pos.get('broker', 'mt5')}"
                        f"\n{'─' * 26}"
                    )
                    if CHAT_ID:
                        send(CHAT_ID, msg)
                    continue
                else:
                    # Ticket not in active tickets, but no exit deal found yet. Avoid false-closing.
                    miss_count = pos.get("_unconfirmed_missing", 0) + 1
                    pos["_unconfirmed_missing"] = miss_count
                    if miss_count < 10:
                        log.debug("RECONCILE_WAIT: ref=%s ticket=%s not in open positions, but no deal in history yet (%d/10)",
                                  ref, order_id, miss_count)
                        continue
                    else:
                        log.warning("RECONCILE_TIMEOUT: ref=%s ticket=%s missing for 10 checks with no deal. Marking broker_closed.",
                                    ref, order_id)
                        ledger.record_outcome(ref, status="closed", hit="broker_exit",
                                              exit_price=pos.get("fill_price", 0.0), pnl_usd=0.0,
                                              classification="broker_closed")
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
            cur = ex.current_price(symbol, direction=direction) if hasattr(ex, "current_price") else 0.0
        except Exception:
            cur = 0.0
        if not cur:
            continue

        # Current MFE in R and Percentage of TP
        target_dist = abs(tp - fill_price)
        mfe_pts = (cur - fill_price) if direction == 1 else (fill_price - cur)
        mfe_r = mfe_pts / risk if risk > 0 else 0.0
        pct_tp = (mfe_pts / target_dist * 100.0) if target_dist > 0 else 0.0

        prev_peak = float(pos.get("peak_mfe_r", 0.0))
        peak_mfe = max(prev_peak, mfe_r)
        if round(peak_mfe, 2) > prev_peak:
            pos["peak_mfe_r"] = round(peak_mfe, 2)

        prev_peak_pct = float(pos.get("peak_pct_tp", 0.0))
        peak_pct_tp = max(prev_peak_pct, pct_tp)
        if round(peak_pct_tp, 1) > prev_peak_pct:
            pos["peak_pct_tp"] = round(peak_pct_tp, 1)
            ledger.save()

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

        # 2. Progressive Trade Protection & Step-Trailing Stop (Percentage-of-TP Engine)
        trail_mode = getattr(config, "TRAIL_MODE", "PERCENTAGE")
        if symbol == "GBPAUD":
            be_pct = float(getattr(config, "BREAKEVEN_PCT_TP_GBPAUD", 70.0))
            t1_pct = float(getattr(config, "TRAIL_STAGE1_PCT_TP_GBPAUD", 85.0))
        else:
            be_pct = float(getattr(config, "BREAKEVEN_PCT_TP", 50.0))
            t1_pct = float(getattr(config, "TRAIL_STAGE1_PCT_TP", 85.0))
        t1_lock_pct = float(getattr(config, "TRAIL_STAGE1_LOCK_PCT", 70.0)) / 100.0
        t2_pct = float(getattr(config, "TRAIL_STAGE2_PCT_TP", 92.0))
        t2_lock_pct = float(getattr(config, "TRAIL_STAGE2_LOCK_PCT", 80.0)) / 100.0

        # Fallback / Fixed R parameters
        be_threshold = float(getattr(config, "BREAKEVEN_MFE_R", 0.8))
        trail1_threshold = float(getattr(config, "TRAIL_STAGE1_MFE_R", 1.5))
        trail1_lock = float(getattr(config, "TRAIL_STAGE1_LOCK_R", 0.8))
        trail2_threshold = float(getattr(config, "TRAIL_STAGE2_MFE_R", 1.8))
        trail2_lock = float(getattr(config, "TRAIL_STAGE2_LOCK_R", 1.2))

        point = ex.get_point(symbol) if hasattr(ex, "get_point") else (0.01 if symbol == "XAUUSD" or "JPY" in symbol else 0.00001)
        digits = 5 if point < 0.01 else 2
        rr = float(pos.get("rr", 1.0))

        if trail_mode == "PERCENTAGE":
            is_stage3 = (pct_tp >= t2_pct or peak_pct_tp >= t2_pct)
            is_stage2 = (pct_tp >= t1_pct or peak_pct_tp >= t1_pct)
            is_stage1 = (pct_tp >= be_pct or peak_pct_tp >= be_pct)
            trail_stage3_sl = round(fill_price + (t2_lock_pct * target_dist), digits) if direction == 1 else round(fill_price - (t2_lock_pct * target_dist), digits)
            trail_stage2_sl = round(fill_price + (t1_lock_pct * target_dist), digits) if direction == 1 else round(fill_price - (t1_lock_pct * target_dist), digits)
            s3_locked_usd = float(pos.get("risk_usd", 100.0)) * (rr * t2_lock_pct)
            s2_locked_usd = float(pos.get("risk_usd", 100.0)) * (rr * t1_lock_pct)
            s3_label = f"{t2_lock_pct * 100:.0f}% of target ({rr * t2_lock_pct:.2f}R)"
            s2_label = f"{t1_lock_pct * 100:.0f}% of target ({rr * t1_lock_pct:.2f}R)"
        else:
            is_stage3 = (mfe_r >= trail2_threshold or peak_mfe >= trail2_threshold)
            is_stage2 = (mfe_r >= trail1_threshold or peak_mfe >= trail1_threshold)
            is_stage1 = (mfe_r >= be_threshold or peak_mfe >= be_threshold)
            trail_stage3_sl = round(fill_price + (trail2_lock * risk), digits) if direction == 1 else round(fill_price - (trail2_lock * risk), digits)
            trail_stage2_sl = round(fill_price + (trail1_lock * risk), digits) if direction == 1 else round(fill_price - (trail1_lock * risk), digits)
            s3_locked_usd = float(pos.get("risk_usd", 100.0)) * trail2_lock
            s2_locked_usd = float(pos.get("risk_usd", 100.0)) * trail1_lock
            s3_label = f"+{trail2_lock:.1f}R"
            s2_label = f"+{trail1_lock:.1f}R"

        # Check Stage 3 (Lock 75% profit / +1.0R)
        if is_stage3 and sl_state != "trail_10":
            pos["sl_state"] = "trail_10"
            pos["sl_protected"] = trail_stage3_sl
            if hasattr(ex, "modify_position"):
                try:
                    ex.modify_position(symbol, pos.get("order_id"), sl=trail_stage3_sl, tp=tp)
                except Exception as me:
                    log.warning("modify_position ref=%s error: %s", ref, me)
            ledger.save()
            actions += 1
            best_pct = max(pct_tp, peak_pct_tp)
            best_mfe = max(mfe_r, peak_mfe)
            msg = (
                f"\U0001F3AF {symbol} \u2014 SL TRAILED TO LOCK PROFIT (STAGE 3)"
                f"\n{'\u2500' * 26}"
                f"\nSetup #{ref} \u00b7 Target progress: {best_pct:.0f}% (+{best_mfe:.2f}R)"
                f"\nStop-loss locked at {trail_stage3_sl:.5f} ({s3_label})"
                f"\nGuaranteed Profit: ~${s3_locked_usd:.2f} USD"
                f"\nBroker: {pos.get('broker', 'mt5')}"
                f"\n{'\u2500' * 26}"
                f"\n\U0001F3C6 Auto-managed by GoldFX agent."
            )
            if CHAT_ID:
                send(CHAT_ID, msg)
            log.info("TRAILED ref=%s %s SL->Stage3 @ %.5f (progress %.0f%%, peak +%.2fR, locked $%.2f)",
                     ref, symbol, trail_stage3_sl, best_pct, best_mfe, s3_locked_usd)

        # Check Stage 2 (Lock 50% profit / +0.5R)
        elif is_stage2 and sl_state not in ("trail_05", "trail_10"):
            pos["sl_state"] = "trail_05"
            pos["sl_protected"] = trail_stage2_sl
            if hasattr(ex, "modify_position"):
                try:
                    ex.modify_position(symbol, pos.get("order_id"), sl=trail_stage2_sl, tp=tp)
                except Exception as me:
                    log.warning("modify_position ref=%s error: %s", ref, me)
            ledger.save()
            actions += 1
            best_pct = max(pct_tp, peak_pct_tp)
            best_mfe = max(mfe_r, peak_mfe)
            msg = (
                f"\U0001F6E1\uFE0F {symbol} \u2014 SL TRAILED TO LOCK PROFIT (STAGE 2)"
                f"\n{'\u2500' * 26}"
                f"\nSetup #{ref} \u00b7 Target progress: {best_pct:.0f}% (+{best_mfe:.2f}R)"
                f"\nStop-loss locked at {trail_stage2_sl:.5f} ({s2_label})"
                f"\nGuaranteed Profit: ~${s2_locked_usd:.2f} USD"
                f"\nBroker: {pos.get('broker', 'mt5')}"
                f"\n{'\u2500' * 26}"
                f"\n\U0001F3C6 Auto-managed by GoldFX agent."
            )
            if CHAT_ID:
                send(CHAT_ID, msg)
            log.info("TRAILED ref=%s %s SL->Stage2 @ %.5f (progress %.0f%%, peak +%.2fR, locked $%.2f)",
                     ref, symbol, trail_stage2_sl, best_pct, best_mfe, s2_locked_usd)

        # Check Stage 1 (Break-Even +1 pip buffer at 50% TP)
        elif is_stage1 and sl_state not in ("be", "trail_05", "trail_10"):
            is_index = symbol in getattr(config, "INDEX_SYMBOLS", {"NASDAQ-100", "US500", "DJ30"})
            if symbol == "XAUUSD":
                pip_buffer = 0.50
            elif is_index:
                pip_buffer = 1.00
            else:
                pip_buffer = 10 * point

            if direction == 1:
                be_sl = round(fill_price + pip_buffer, digits)
            else:
                be_sl = round(fill_price - pip_buffer, digits)

            mod_ok = True
            if hasattr(ex, "modify_position"):
                try:
                    mod_ok = ex.modify_position(symbol, pos.get("order_id"), sl=be_sl, tp=tp)
                except Exception as me:
                    log.warning("modify_position ref=%s error: %s", ref, me)
                    mod_ok = False

            if mod_ok:
                pos["sl_state"] = "be"
                pos["sl_protected"] = be_sl
                ledger.save()
                actions += 1
                best_mfe = max(mfe_r, peak_mfe)
                best_pct = max(pct_tp, peak_pct_tp)
                msg = (
                    f"\U0001F6E1\uFE0F {symbol} \u2014 SL MOVED TO BREAKEVEN (+1 PIP BUFFER)"
                    f"\n{'\u2500' * 26}"
                    f"\nSetup #{ref} \u00b7 Target progress: {best_pct:.0f}% (+{best_mfe:.2f}R)"
                    f"\nStop-loss adjusted to {be_sl:.5f} (Entry: {fill_price:.5f})"
                    f"\nRisk-Free: spread & broker costs protected!"
                    f"\nBroker: {pos.get('broker', 'mt5')}"
                    f"\n{'\u2500' * 26}"
                    f"\n\u26A0\ufe0f Auto-managed by GoldFX agent."
                )
                if CHAT_ID:
                    send(CHAT_ID, msg)
                log.info("PROTECTED ref=%s %s SL->BE @ %.5f (+1 pip buffer, progress %.0f%%, peak +%.2fR)",
                         ref, symbol, be_sl, best_pct, best_mfe)

        # 3. Protected exit: if price retraces back to protected stop (BE, +0.5R, or +1.0R)
        elif sl_state in ("be", "trail_05", "trail_10"):
            sl_protected = float(pos.get("sl_protected", fill_price))
            hit_protected = (direction == 1 and cur <= sl_protected) or (direction == -1 and cur >= sl_protected)
            if hit_protected:
                order_id = pos.get("order_id")
                try:
                    if hasattr(ex, "close_position_by_ticket") and order_id and str(order_id).isdigit():
                        ex.close_position_by_ticket(int(order_id))
                    else:
                        ex.close_position(symbol, direction, float(pos.get("lots", 0.01)))
                except Exception as ce:
                    log.error("Protected exit close error ref=%s: %s", ref, ce)

                risk_usd = float(pos.get("risk_usd", 100.0))
                if sl_state == "trail_10":
                    hit = "tp"
                    pnl = risk_usd * trail2_lock
                    classification = "trailed_profit_10"
                    icon = "\U0001F3AF"
                    title = f"EXITED AT +{trail2_lock:.1f}R TRAILING PROFIT"
                elif sl_state == "trail_05":
                    hit = "tp"
                    pnl = risk_usd * trail1_lock
                    classification = "trailed_profit_05"
                    icon = "\U0001F6E1\uFE0F"
                    title = f"EXITED AT +{trail1_lock:.1f}R TRAILING PROFIT"
                else:
                    hit = "be"
                    pnl = 0.0
                    classification = "be_avoided_sl"
                    icon = "\u26AA"
                    title = "EXITED AT BREAKEVEN"

                ledger.record_outcome(ref, status="closed", hit=hit,
                                      exit_price=sl_protected, pnl_usd=pnl,
                                      classification=classification)
                actions += 1
                msg = (
                    f"{icon} {symbol} \u2014 {title}"
                    f"\n{'\u2500' * 26}"
                    f"\nSetup #{ref} \u00b7 Exited at {sl_protected:.5f}"
                    f"\nP&L: {pnl:+.2f} USD \u00b7 Protected gain banked!"
                    f"\nBroker: {pos.get('broker', 'mt5')}"
                    f"\n{'\u2500' * 26}"
                    f"\n\u26A0\ufe0f Auto-managed by GoldFX agent."
                )
                if CHAT_ID:
                    send(CHAT_ID, msg)
                log.info("EXITED_PROTECTED ref=%s %s @ %.5f (%s, pnl=$%.2f)",
                         ref, symbol, sl_protected, classification, pnl)

    return actions


def process_pending_retracements(ledger) -> int:
    """Evaluate active pending retracement setups (Option 1: Local M5 Swing Sniper).
    - Checks invalidation: 75% TP reached before entry -> expire
    - Checks invalidation: original SL breached before entry -> save full loss ($0 loss)
    - Checks age timeout: 6 hours
    - Detects 25%-50% pullback into setup range
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
        # Clean up: if ref_key is already filled/active on broker, do NOT re-process or falsely invalidate!
        if ledger.has(ref_key):
            pos_rec = ledger.data.get(str(ref_key), {})
            if pos_rec.get("status") == "open" or pos_rec.get("order_id"):
                log.info("RETRACE_CLEANUP: ref=%s already active on broker (ticket %s); removing from pending queue.",
                         ref_key, pos_rec.get("order_id"))
                continue

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

        # 0. Gold Quarantine Guard for Standard small accounts (<$50)
        is_cent_account = getattr(config, "IS_CENT_ACCOUNT", False)
        if symbol == "XAUUSD" and not is_cent_account and getattr(config, "GOLD_QUARANTINE_ENABLED", True):
            min_gold_abs = float(getattr(config, "MIN_GOLD_ABSOLUTE_BALANCE", 50.0))
            if rm.balance < min_gold_abs:
                log.info("RETRACE_QUARANTINE XAUUSD ref=%s — balance ($%.2f) below $%.2f minimum for Standard Gold. Cancelled.",
                         ref_key, rm.balance, min_gold_abs)
                ledger.record_outcome(ref_key, status="quarantined", hit="gold_quarantined_low_balance",
                                      pnl_usd=0.0, classification="gold_small_account_quarantine")
                continue

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
            cur = ex.current_price(symbol, direction=direction) if hasattr(ex, "current_price") else 0.0
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
            # On small accounts, cap max allowed risk dynamically to max_risk_pct of balance (e.g. 8% of $40 = $3.20)
            max_risk_pct = float(getattr(config, "MAX_RISK_PER_TRADE", 8.0))
            dyn_cap = rm.balance * (max_risk_pct / 100.0) if rm.balance > 0 else 7.50
            max_allowed = min(float(getattr(config, "RETRACE_MAX_RISK_USD", 7.50)), dyn_cap)
            if min_risk > max_allowed:
                log.info("RETRACE_SKIP_BAR ref=%s %s min lot risk $%.2f exceeds small account cap $%.2f (%.1f%% of balance)",
                         ref_key, symbol, min_risk, max_allowed, (min_risk / rm.balance * 100) if rm.balance > 0 else 0)
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


def process_pending_turtle_soup(ledger) -> int:
    """Evaluate active pending Turtle Soup / Inducement Sweep setups (Phase 2).
    - Checks invalidation: age > 45 mins -> expire
    - Checks invalidation: overshoot > 0.6R -> expire (true trend breakdown, not a sweep)
    - Checks invalidation: original TP reached before re-entry -> expire
    - Checks trigger: price rejects and reclaims back inside original SL level
    - Fires sniper market order with tight stop at sweep extreme + buffer
    """
    if not getattr(config, "TURTLE_SOUP_REENTRY_ENABLED", True):
        return 0
    pending = _load_pending_turtle_soup()
    if not pending:
        return 0

    ex = get_executor()
    rm = risk()
    now_utc = dt.datetime.now(dt.timezone.utc)
    still_pending = {}
    reentered_count = 0

    for ref_key, item in list(pending.items()):
        symbol = item["symbol"]
        direction = int(item["direction"])
        orig_entry = float(item["original_entry"])
        orig_sl = float(item["original_sl"])
        orig_tp = float(item["original_tp"])
        sl_dist = float(item["sl_dist"])
        sweep_extreme = float(item.get("sweep_extreme", item["exit_price"]))

        try:
            exit_ts = dt.datetime.fromisoformat(item["exit_ts"])
            age_min = (now_utc - exit_ts).total_seconds() / 60.0
        except Exception:
            age_min = 0.0

        # 1. Invalidation: age > 45 mins
        max_win = getattr(config, "TURTLE_SOUP_MAX_WINDOW_MIN", 45)
        if age_min > max_win:
            log.info("TURTLE_SOUP_EXPIRED: ref=%s %s age=%.1fm > %dm", ref_key, symbol, age_min, max_win)
            continue

        c_info = config.CONTRACTS.get(symbol, {})
        point = c_info.get("point", 0.01 if symbol == "XAUUSD" or "JPY" in symbol else 0.00001)
        pip_val = c_info.get("pip_value_per_lot_usd", 1.0)
        fmt_digits = 2 if point >= 0.01 else 5

        # Query live tick price
        spread_pips, cur_price = ex.get_spread_pips(symbol)
        if cur_price is None or cur_price <= 0:
            still_pending[ref_key] = item
            continue

        max_overshoot_r = getattr(config, "TURTLE_SOUP_MAX_OVERSHOOT_R", 0.6)

        # 2. Update sweep extreme & check overshoot
        triggered = False
        if direction == 1:  # BUY setup stopped below orig_sl
            if cur_price < sweep_extreme:
                sweep_extreme = cur_price
                item["sweep_extreme"] = sweep_extreme
            overshoot_r = (orig_sl - sweep_extreme) / sl_dist if sl_dist > 0 else 0
            if overshoot_r > max_overshoot_r:
                log.info("TURTLE_SOUP_INVALID_DEEP: ref=%s %s overshoot=%.2fR > %.2fR (true breakdown, not sweep)",
                         ref_key, symbol, overshoot_r, max_overshoot_r)
                continue
            if cur_price >= orig_tp:
                log.info("TURTLE_SOUP_MISSED_TP: ref=%s %s price touched original TP before re-entry", ref_key, symbol)
                continue
            # Trigger condition: Market price reclaims above original SL
            if cur_price >= orig_sl:
                triggered = True
        else:  # SELL setup stopped above orig_sl
            if cur_price > sweep_extreme:
                sweep_extreme = cur_price
                item["sweep_extreme"] = sweep_extreme
            overshoot_r = (sweep_extreme - orig_sl) / sl_dist if sl_dist > 0 else 0
            if overshoot_r > max_overshoot_r:
                log.info("TURTLE_SOUP_INVALID_DEEP: ref=%s %s overshoot=%.2fR > %.2fR (true breakdown, not sweep)",
                         ref_key, symbol, overshoot_r, max_overshoot_r)
                continue
            if cur_price <= orig_tp:
                log.info("TURTLE_SOUP_MISSED_TP: ref=%s %s price touched original TP before re-entry", ref_key, symbol)
                continue
            # Trigger condition: Market price reclaims below original SL
            if cur_price <= orig_sl:
                triggered = True

        if not triggered:
            still_pending[ref_key] = item
            continue

        # 3. Triggered! Check portfolio capacity & risk limits
        allowed, reason_cap = rm.can_open_trade(symbol)
        if not allowed:
            log.warning("TURTLE_SOUP_CAP_BLOCKED: ref=%s %s %s — holding in queue", ref_key, symbol, reason_cap)
            still_pending[ref_key] = item
            continue

        soup_label = f"soup_{ref_key}"
        if hasattr(ex, "has_position_with_ref") and ex.has_position_with_ref(symbol, soup_label):
            log.info("TURTLE_SOUP_ALREADY_OPEN: ref=%s %s %s already active in MT5", ref_key, symbol, soup_label)
            continue

        # 4. Compute tight stop loss right behind the sweep wick
        buf_pips = getattr(config, "TURTLE_SOUP_SL_BUFFER_PIPS", 1.5)
        pip_scale = 0.01 if "JPY" in symbol or symbol == "XAUUSD" else (1.0 if any(idx in symbol for idx in ["100", "500", "30"]) else 0.0001)
        buf_price = buf_pips * pip_scale

        fill_entry = cur_price
        if direction == 1:
            candidate_sl = round(sweep_extreme - buf_price, fmt_digits)
            candidate_tp = orig_tp
        else:
            candidate_sl = round(sweep_extreme + buf_price, fmt_digits)
            candidate_tp = orig_tp

        candidate_sl_dist = abs(fill_entry - candidate_sl)
        if candidate_sl_dist <= 0:
            still_pending[ref_key] = item
            continue

        target_dist = abs(candidate_tp - fill_entry)
        target_rr = target_dist / candidate_sl_dist

        # 5. Dynamic Lot Sizing based on portfolio risk budget (6%)
        risk_usd = rm.balance * rm.risk_pct / 100.0
        lots_raw = position_size(symbol, risk_usd, candidate_sl_dist)
        lots = floor_lots(symbol, lots_raw)
        min_lot = float(c_info.get("min_lot", 0.01))

        if lots < min_lot:
            lots = min_lot

        dollar_risk = lots * (candidate_sl_dist / point) * pip_val

        try:
            fill = ex.place_market_order(symbol, direction, lots, fill_entry, candidate_sl, candidate_tp, soup_label)
        except Exception as e:
            log.error("Turtle soup order execution failed ref=%s: %s", ref_key, e)
            still_pending[ref_key] = item
            continue

        if not fill.ok:
            log.warning("Turtle soup order rejected ref=%s: %s", ref_key, fill.message[:200])
            still_pending[ref_key] = item
            continue

        # Record in ledger
        ledger.data[str(soup_label)] = {
            "ref": soup_label,
            "symbol": symbol,
            "direction": direction,
            "lots": lots,
            "fill_price": fill.fill_price,
            "sl": candidate_sl,
            "orig_sl": orig_sl,
            "tp": candidate_tp,
            "rr": round(target_rr, 2),
            "risk_usd": round(dollar_risk, 2),
            "order_id": fill.order_id,
            "broker": fill.broker,
            "ts": fill.ts,
            "signal_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "status": "open",
            "sl_state": "initial",
            "peak_mfe_r": 0.0,
            "profile": item.get("profile", ""),
            "strategy_type": "turtle_soup_reentry",
            "strategy_badge": "🐢 Turtle Soup Inducement Re-Entry",
            "ltf_confirmed": True,
        }
        ledger.save()

        # Format Telegram announcement
        d_str = "LONG" if direction == 1 else "SHORT"
        emoji = "🟢" if direction == 1 else "🔴"
        overshoot_pips = (abs(sweep_extreme - orig_sl) / pip_scale)
        msg = (
            f"🐢 <b>TURTLE SOUP INDUCEMENT RE-ENTRY</b> {d_str} {emoji}\n"
            f"{'─' * 28}\n"
            f"<b>Asset:</b> {symbol}\n"
            f"<b>Original Setup:</b> #{ref_key} (Liquidity Sweep Reclaimed)\n"
            f"<b>Sweep Depth:</b> {overshoot_r:.2f}R ({overshoot_pips:.1f} pips)\n"
            f"<b>Fill:</b> {fill.fill_price:.{fmt_digits}f} · {lots:.2f} lots\n"
            f"<b>Tight SL:</b> {candidate_sl:.{fmt_digits}f} (Behind sweep wick)\n"
            f"<b>Target TP:</b> {candidate_tp:.{fmt_digits}f} · <b>Target RR: 1:{target_rr:.2f}</b>\n"
            f"<b>Ticket:</b> <code>{fill.order_id}</code>\n"
            f"<b>Broker:</b> {fill.broker} · {fill.ts}\n"
            f"{'─' * 28}\n"
            f"🎯 Executed via GoldFX Institutional Liquidity Engine."
        )
        if CHAT_ID:
            send(CHAT_ID, msg)
        log.info("TURTLE_SOUP_FILLED ref=%s %s %s @ %.5f (SL %.5f, Risk $%.2f, RR 1:%.2f)",
                 soup_label, symbol, d_str, fill.fill_price, candidate_sl, dollar_risk, target_rr)
        reentered_count += 1

    _save_pending_turtle_soup(still_pending)
    return reentered_count


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

    # High-Impact Red-Folder News Tactical Playbook Dispatcher (~30 mins before release)
    try:
        from engine.news import check_and_send_pre_news_alerts
        check_and_send_pre_news_alerts(send, CHAT_ID, lookahead_min=getattr(config, "NEWS_PLAYBOOK_AHEAD_MIN", 30))
    except Exception as e:
        log.warning("check_and_send_pre_news_alerts error: %s", e)

    # Pre-News Defense: Lock profits to Break-Even ahead of red-folder releases
    try:
        handle_prenew_guards(ledger, ex)
    except Exception as e:
        log.warning("handle_prenew_guards error: %s", e)

    fired_any = False

    # Defense 1: Portfolio Risk Budgeting & Capacity Guard (Dynamic by Real USD Account Size)
    is_cent = getattr(config, "IS_CENT_ACCOUNT", False)
    if not is_cent and hasattr(ex, "account_snapshot"):
        try:
            snap = ex.account_snapshot()
            acc_curr = str(snap.get("currency", "")).upper()
            if "CENT" in acc_curr or "USC" in acc_curr:
                is_cent = True
        except Exception:
            pass

    if hasattr(config, "dynamic_portfolio_capacity"):
        cap_res = config.dynamic_portfolio_capacity(rm.balance, is_cent)
        if len(cap_res) == 5:
            max_trades, max_at_risk, max_trades_per_sym, max_portfolio_risk_pct, cap_tier = cap_res
        else:
            max_trades, max_trades_per_sym, max_portfolio_risk_pct, cap_tier = cap_res
            max_at_risk = getattr(config, "MAX_AT_RISK_TRADES", 8)
    else:
        max_trades = getattr(config, "MAX_CONCURRENT_TRADES", 14 if is_cent else 8)
        max_at_risk = getattr(config, "MAX_AT_RISK_TRADES", 8 if is_cent else 4)
        max_trades_per_sym = getattr(config, "MAX_TRADES_PER_SYMBOL", 2)
        max_portfolio_risk_pct = getattr(config, "MAX_PORTFOLIO_RISK_PCT", 35.0 if is_cent else 30.0)

    target_broker = getattr(ex, "broker", "mt5")
    open_positions = [
        e for e in ledger.open_entries().values()
        if e.get("broker", "mt5") == target_broker
    ]
    active_open_count = len(open_positions)
    # Positions with active downside risk (< BE). Trades at BE or in trailing profit have $0 risk!
    at_risk_positions = [
        e for e in open_positions
        if e.get("sl_state") not in ("be", "trail_05", "trail_10")
    ]
    at_risk_count = len(at_risk_positions)

    # 1. Execute new setups (oldest first for correct sequence)
    for entry in reversed(history):
        ref_key = _to_ref_key(entry)
        if ledger.has(ref_key):
            continue

        # Broker-level duplicate execution defense
        if hasattr(ex, "has_position_with_ref") and ex.has_position_with_ref(ref_key):
            log.warning("skip %s ref=%s — broker already has an open position for this setup ref! Duplicate blocked.",
                        entry.get("symbol", ""), ref_key)
            continue
        symbol = entry.get("symbol", "")
        direction = _entry_dir(entry)

        # Check 1: Max total open trades across all pairs & indices (including runners)
        if active_open_count >= max_trades:
            log.info("skip %s ref=%s — max total concurrent trades reached (%d/%d active)",
                     symbol, ref_key, active_open_count, max_trades)
            continue

        # Check 1b: Max AT-RISK trades (trades at BE or with trailing profit are risk-free and do not block fresh entries!)
        if at_risk_count >= max_at_risk:
            log.info("skip %s ref=%s — max at-risk trades reached (%d/%d unhedged risk, %d risk-free runners)",
                     symbol, ref_key, at_risk_count, max_at_risk, active_open_count - at_risk_count)
            continue

        # Check 2: Max concurrent trades on this specific symbol (Option 3: Risk-Free Trades Exempt from At-Risk Cap)
        sym_all_positions = [e for e in open_positions if e.get("symbol") == symbol]
        sym_at_risk_positions = [
            e for e in sym_all_positions
            if e.get("sl_state") not in ("be", "trail_05", "trail_10")
        ]
        sym_open_count = len(sym_all_positions)
        sym_at_risk_count = len(sym_at_risk_positions)
        sym_be_count = sym_open_count - sym_at_risk_count

        # Check 2a: Hard ceiling on single symbol (including risk-free runners at BE)
        max_sym_total = getattr(config, "MAX_TOTAL_PER_SYMBOL", 3 if (is_cent or max_trades_per_sym >= 2) else 2)
        if sym_open_count >= max_sym_total:
            log.info("skip %s ref=%s — symbol total concentration ceiling reached (%d/%d on %s, %d risk-free at BE)",
                     symbol, ref_key, sym_open_count, max_sym_total, symbol, sym_be_count)
            continue

        # Check 2b: At-Risk ceiling on single symbol (< BE)
        max_sym_at_risk = getattr(config, "MAX_AT_RISK_PER_SYMBOL", max_trades_per_sym)
        if sym_at_risk_count >= max_sym_at_risk:
            log.info("skip %s ref=%s — symbol at-risk limit reached (%d/%d at-risk on %s; existing %d trades must reach BE first)",
                     symbol, ref_key, sym_at_risk_count, max_sym_at_risk, symbol, sym_at_risk_count)
            continue

        # Check 3: Cumulative portfolio unprotected risk (trades at Breakeven or locked profit have $0 risk!)
        unprotected_risk_usd = sum(
            float(e.get("risk_usd", 0.0)) for e in at_risk_positions
        )
        current_unprotected_risk_pct = (unprotected_risk_usd / rm.balance * 100.0) if rm.balance > 0 else 0.0
        
        # High-Capacity Dynamic Headroom:
        # Instead of skipping a valid trade when open risk is close to the cap,
        # dynamically scale risk down to fit the remaining budget (minimum 2.5% risk).
        remaining_risk_pct = max_portfolio_risk_pct - current_unprotected_risk_pct
        if remaining_risk_pct < 2.5:
            log.info("skip %s ref=%s — portfolio risk limit reached (open risk %.1f%% / max %.1f%%, headroom %.1f%% < 2.5%%)",
                     symbol, ref_key, current_unprotected_risk_pct, max_portfolio_risk_pct, remaining_risk_pct)
            continue

        trade_risk_pct = min(rm.risk_pct, remaining_risk_pct)

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

        # US Open Opening Bell Cooldown Check (13:25 - 13:45 UTC for NASDAQ-100, US500, DJ30)
        if _is_us_open_cooldown(symbol):
            log.info("skip %s ref=%s — US Open opening bell volatility cooldown active (13:25 - 13:45 UTC). New entries paused.",
                     symbol, ref_key)
            continue

        # London Open Opening Bell Cooldown Check (06:50 - 07:20 UTC for Forex & Gold)
        if _is_london_open_cooldown(symbol):
            log.info("skip %s ref=%s — London Open opening bell volatility cooldown active (06:50 - 07:20 UTC). Pausing entries during European cash open purge.",
                     symbol, ref_key)
            continue

        # High-Impact Red-Folder News Blackout (e.g. -15m to +15m around NFP, CPI, FOMC, etc.)
        if getattr(config, "NEWS_BLACKOUT_ENABLED", True):
            from engine.news import get_active_news_blackout
            is_news_bo, bo_reason, _ = get_active_news_blackout(symbol)
            if is_news_bo:
                log.info("skip %s ref=%s — %s", symbol, ref_key, bo_reason)
                continue

        # DXY Macro Momentum Defense (Forex Pairs Only):
        # Double-checks live US Dollar momentum before firing into MT5 broker.
        # Exempts Gold and Equity Indices to preserve independent safe-haven / momentum runs.
        if getattr(config, "DXY_FILTER_ENABLED", True):
            exempt = getattr(config, "DXY_EXEMPT_SYMBOLS", {"XAUUSD", "NASDAQ-100", "US500", "DJ30", "GBPAUD"})
            if symbol not in exempt:
                from engine.scanner import get_dxy_trend, is_dxy_aligned
                tf_dxy = getattr(config, "DXY_FILTER_TF", "M15")
                ema_dxy = getattr(config, "DXY_FILTER_EMA", 21)
                dxy_val, dxy_lbl = get_dxy_trend(tf=tf_dxy, ema_len=ema_dxy)
                aligned, dxy_reason = is_dxy_aligned(symbol, direction, dxy_val)
                if not aligned:
                    log.info("🛡️ DXY_FILTER_BLOCK %s ref=%s — live DXY opposes trade (%s, DXY %s). MT5 execution blocked.",
                             symbol, ref_key, dxy_reason, dxy_lbl)
                    ledger.record_outcome(ref_key, status="skipped", hit="dxy_counter_trend",
                                          pnl_usd=0.0, classification="dxy_momentum_filter")
                    continue

        # Phase 1 Rule 2: Late NY / Rollover Session Exhaustion Check (17:00 - 24:00 UTC)
        # Asian session (00:00 - 06:50 UTC) and London/NY overlap remain 100% active!
        if _is_evening_exhaustion_window():
            log.info("skip %s ref=%s — Late NY/Rollover exhaustion window active (17:00 - 24:00 UTC). New fills paused.",
                     symbol, ref_key)
            continue

        # Phase 1 Rule 1: Higher Timeframe (HTF) Dual-Trend Guard (Prunes 10.6% WR counter-trend setups)
        is_counter_htf, htf_reason = _is_htf_counter_trend(symbol, direction)
        if is_counter_htf:
            log.info("🛡️ HTF_FILTER_BLOCK %s ref=%s — opposes both H1 and H4 trends (%s). MT5 execution blocked.",
                     symbol, ref_key, htf_reason)
            ledger.record_outcome(ref_key, status="skipped", hit="htf_counter_trend",
                                  pnl_usd=0.0, classification="htf_trend_filter")
            continue

        try:
            ex = get_executor()

            # Spread Filter Check
            if hasattr(ex, "get_spread"):
                spread_val = ex.get_spread(symbol)
                c_info = config.CONTRACTS.get(symbol, {})
                digits = c_info.get("digits", 5)
                pip_size = 0.0001 if digits in (4, 5) else (0.01 if digits in (2, 3) else c_info.get("point", 0.00001))

                is_index = symbol in getattr(config, "INDEX_SYMBOLS", {"NASDAQ-100", "US500", "DJ30"})
                if symbol == "XAUUSD":
                    curr_metric = spread_val
                    max_spread = config.MAX_SPREAD_GOLD
                    metric_unit = "$"
                elif is_index:
                    curr_metric = spread_val
                    max_spread = getattr(config, "MAX_SPREAD_INDEX", 8.0)
                    metric_unit = "pts"
                else:
                    curr_metric = spread_val / pip_size if pip_size > 0 else 0.0
                    max_spread = config.MAX_SPREAD_PIPS
                    metric_unit = "pips"

                if curr_metric > max_spread:
                    log.info("skip %s ref=%s — spread too wide (%.2f %s > max %.2f %s). Awaiting normal liquidity.",
                             symbol, ref_key, curr_metric, metric_unit, max_spread, metric_unit)
                    continue

            # Fetch LIVE price first for pre-entry sanity and accurate dynamic sizing
            cur = ex.current_price(symbol, direction=direction) if hasattr(ex, "current_price") else 0.0
            if not cur or cur <= 0:
                cur = entry_price

            # Check A: Never fire a setup whose SL is already breached
            if (direction == 1 and cur <= sl) or (direction == -1 and cur >= sl):
                log.info("skip %s ref=%s — SL already breached (cur %.5f, sl %.5f)",
                         symbol, ref_key, cur, sl)
                ledger.record_outcome(ref_key, status="skipped", hit="pre_entry_sl_breached",
                                      pnl_usd=0.0, classification="sl_already_breached")
                msg = (
                    f"🛑 {symbol} — SETUP #{ref_key} SKIPPED (SL BREACHED)\n"
                    f"{'─' * 26}\n"
                    f"Market price touched or breached SL ({sl:.5f}) before fill.\n"
                    f"Current Price: {cur:.5f}\n"
                    f"Execution safely aborted."
                )
                if CHAT_ID:
                    send(CHAT_ID, msg)
                continue

            # Check B: Never fire if price already reached TP
            if (direction == 1 and cur >= tp) or (direction == -1 and cur <= tp):
                log.info("skip %s ref=%s — TP already reached (cur %.5f, tp %.5f)",
                         symbol, ref_key, cur, tp)
                ledger.record_outcome(ref_key, status="skipped", hit="pre_entry_tp_reached",
                                      pnl_usd=0.0, classification="tp_already_reached")
                msg = (
                    f"🎯 {symbol} — SETUP #{ref_key} SKIPPED (TARGET REACHED)\n"
                    f"{'─' * 26}\n"
                    f"Price reached Take Profit ({tp:.5f}) before fill could occur.\n"
                    f"Current Price: {cur:.5f}\n"
                    f"Anti-Chase: Finished moves are never chased."
                )
                if CHAT_ID:
                    send(CHAT_ID, msg)
                continue

            orig_sl_dist = abs(entry_price - sl)
            orig_tp_dist = abs(tp - entry_price)

            # Check C: Adverse drift check (price fell too far towards SL)
            # Requires adverse drift to exceed 50% of SL AND exceed minimum noise floor (4 pips FX, $0.50 Gold, 15 pts Index)
            max_adverse_pct = float(getattr(config, "MAX_ADVERSE_DRIFT_PCT", 0.50))
            is_index_sym = symbol in getattr(config, "INDEX_SYMBOLS", {"NASDAQ-100", "US500", "DJ30"})
            min_noise_buffer = 0.50 if symbol == "XAUUSD" else (15.0 if is_index_sym else 0.00040)
            adverse_dist = (entry_price - cur) if direction == 1 else (cur - entry_price)
            if adverse_dist > (max_adverse_pct * orig_sl_dist) and adverse_dist > min_noise_buffer:
                log.info("skip %s ref=%s — price drifted too far adverse from entry zone (cur %.5f, entry %.5f, adverse %.5f)",
                         symbol, ref_key, cur, entry_price, adverse_dist)
                ledger.record_outcome(ref_key, status="skipped", hit="pre_entry_adverse_drift",
                                      pnl_usd=0.0, classification="adverse_drift_exceeded")
                msg = (
                    f"⚠️ {symbol} — SETUP #{ref_key} SKIPPED (ADVERSE DRIFT)\n"
                    f"{'─' * 26}\n"
                    f"Price moved too close to Stop Loss before fill.\n"
                    f"Current: {cur:.5f} · Entry: {entry_price:.5f} · SL: {sl:.5f}\n"
                    f"Capital Guard: Entry blocked to avoid buying into adverse momentum."
                )
                if CHAT_ID:
                    send(CHAT_ID, msg)
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

            risk_usd = rm.balance * trade_risk_pct / 100.0
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
                            f"\nLive SL distance (${actual_sl_dist:.2f}) exceeds our ${risk_usd:.2f} risk budget ({trade_risk_pct:.1f}%)."
                            f"\nBot will wait for 25%–50% pullback + local M5 reversal structure to enter safely."
                            f"\n{'\u2500' * 26}"
                            f"\n\u26A0\ufe0f Capital preservation guard active."
                        )
                        if CHAT_ID:
                            send(CHAT_ID, msg)
                    continue
                log.info("skip %s ref=%s — required lot (%.4f) < broker min (0.01). Min lot would risk $%.2f (%.1f%% of balance), exceeding our %.1f%% budget ($%.2f). Capital preserved.",
                         symbol, ref_key, lots_raw, min_risk, (min_risk / rm.balance) * 100, trade_risk_pct, risk_usd)
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
        try:
            pending_retrace = _load_pending_retrace()
            if str(ref_key) in pending_retrace:
                del pending_retrace[str(ref_key)]
                _save_pending_retrace(pending_retrace)
        except Exception:
            pass
        msg = _format_fill_message(fill, entry)
        if CHAT_ID:
            send(CHAT_ID, msg)
        log.info("FILLED ref=%s %s %s @ %.5f %.2f lots",
                 ref_key, symbol, "LONG" if direction == 1 else "SHORT",
                 fill.fill_price, lots)
        fired_any = True
        active_open_count += 1
        at_risk_count += 1
        open_positions.append(ledger.data[str(ref_key)])
        at_risk_positions.append(ledger.data[str(ref_key)])


    # 2. Process pending retracements (Option 1: M5 Swing Sniper for Gold)
    retrace_filled = process_pending_retracements(ledger)
    if retrace_filled:
        fired_any = True
        log.info("processed pending retracements, fired %d sniper orders", retrace_filled)

    # 3. Manage open positions (Half B: Breakeven trailing & early exit)
    managed = manage_open_positions(ledger)
    if managed:
        log.info("active exit management processed %d positions", managed)

    # 3b. Process pending Turtle Soup sweeps (Phase 2 Inducement Re-entry)
    soup_filled = process_pending_turtle_soup(ledger)
    if soup_filled:
        fired_any = True
        log.info("processed pending turtle soup sweeps, fired %d re-entry orders", soup_filled)

    # 4. Reconcile outcomes (TP/SL) from the delivery bot
    reconciled = reconcile_outcomes(ledger, outcomes)
    if reconciled:
        log.info("reconciled %d outcomes", reconciled)

    return fired_any


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run_agent():
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "agent.log"
    root = logging.getLogger()
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        root.addHandler(fh)

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
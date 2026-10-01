"""Economic Calendar & High-Impact Red-Folder News Engine.

Fetches weekly economic calendar events (ForexFactory JSON feed), detects Tier-1
high-impact news windows, enforces pre/post news blackouts to prevent spread-blowout
and slippage losses, and delivers pre-news tactical playbooks to Telegram.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config

log = logging.getLogger("goldfx.news")

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_PATH = config.DATA_CACHE / "news_calendar.json"
SENT_ALERTS_PATH = config.DATA_CACHE / "sent_news_alerts.json"

_MEM_CALENDAR: dict = {"ts": 0.0, "events": []}

# Symbol -> relevant economic currencies
SYMBOL_CURRENCIES: dict[str, set[str]] = {
    "XAUUSD": {"USD"},
    "EURUSD": {"EUR", "USD"},
    "GBPUSD": {"GBP", "USD"},
    "AUDUSD": {"AUD", "USD"},
    "USDCAD": {"CAD", "USD"},
    "NZDUSD": {"NZD", "USD"},
    "USDCHF": {"CHF", "USD"},
    "USDJPY": {"JPY", "USD"},
    "NASDAQ-100": {"USD"},
    "US500": {"USD"},
    "DJ30": {"USD"},
    "GBPAUD": {"GBP", "AUD"},
}


def _load_sent_alerts() -> set[str]:
    try:
        if SENT_ALERTS_PATH.exists():
            data = json.loads(SENT_ALERTS_PATH.read_text(encoding="utf-8"))
            return set(data)
    except Exception:
        pass
    return set()


def _save_sent_alerts(alerts: set[str]) -> None:
    try:
        SENT_ALERTS_PATH.parent.mkdir(exist_ok=True)
        # Keep only the last 200 alert keys
        recent = sorted(alerts)[-200:]
        SENT_ALERTS_PATH.write_text(json.dumps(recent, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("failed to save sent news alerts: %s", e)


def fetch_calendar_events(cache_ttl_sec: float = 1800.0, force_refresh: bool = False) -> list[dict]:
    """Fetch calendar events, caching locally for cache_ttl_sec (default 30 mins)."""
    now = time.time()
    if not force_refresh and _MEM_CALENDAR["events"] and (now - _MEM_CALENDAR["ts"]) < cache_ttl_sec:
        return _MEM_CALENDAR["events"]

    # Try local file cache first if fresh
    if not force_refresh and CACHE_PATH.exists():
        try:
            mtime = CACHE_PATH.stat().st_mtime
            if (now - mtime) < cache_ttl_sec:
                data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                _MEM_CALENDAR["ts"] = mtime
                _MEM_CALENDAR["events"] = data
                return data
        except Exception as e:
            log.debug("error reading disk cache: %s", e)

    # Fetch fresh calendar from ForexFactory feed
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    req = urllib.request.Request(CALENDAR_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            events = json.loads(resp.read().decode("utf-8"))
            if isinstance(events, list) and len(events) > 0:
                CACHE_PATH.parent.mkdir(exist_ok=True)
                CACHE_PATH.write_text(json.dumps(events, indent=2), encoding="utf-8")
                _MEM_CALENDAR["ts"] = now
                _MEM_CALENDAR["events"] = events
                log.info("fetched fresh economic calendar: %d events", len(events))
                return events
    except Exception as e:
        log.warning("calendar fetch failed (%s); using cached/fallback", e)

    # If fetch failed, use whatever cache is available on disk
    if CACHE_PATH.exists():
        try:
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            _MEM_CALENDAR["ts"] = now
            _MEM_CALENDAR["events"] = data
            return data
        except Exception:
            pass

    return _MEM_CALENDAR["events"]


def parse_event_time(event: dict) -> dt.datetime | None:
    """Parse ISO date from event dict to a UTC timezone-aware datetime."""
    t_str = event.get("date")
    if not t_str:
        return None
    try:
        return dt.datetime.fromisoformat(t_str).astimezone(dt.timezone.utc)
    except Exception:
        return None


def is_tier1_event(e: dict) -> bool:
    """True if event is High-Impact (Red folder) OR a market-moving Central Bank / Tier-1 release."""
    impact = str(e.get("impact", "")).capitalize()
    if impact in ("High", "Red"):
        return True
    title = str(e.get("title", "")).lower()
    # Central Bank Speeches & Rate Decisions
    if any(w in title for w in ("speaks", "press conference", "rate decision", "statement", "minutes")):
        if any(k in title for k in ("gov", "president", "chair", "member", "fomc", "boe", "ecb", "snb", "boj", "rba", "powell", "bailey", "lagarde")):
            return True
    # Major Tier-1 Economic Indicators
    if any(k in title for k in ("manufacturing pmi", "services pmi", "unemployment claims", "cpi", "core cpi", "pce", "nfp", "gdp")):
        return True
    return False


def get_active_news_blackout(
    symbol: str,
    buffer_before_min: int | None = None,
    buffer_after_min: int | None = None,
    as_of_utc: dt.datetime | None = None,
) -> tuple[bool, str, dict | None]:
    """Check if symbol is currently in an active high-impact news blackout window.

    Returns:
        (is_blackout: bool, reason: str, event_dict_or_None)
    """
    if not getattr(config, "NEWS_BLACKOUT_ENABLED", True):
        return False, "NEWS_FILTER_DISABLED", None

    currencies = SYMBOL_CURRENCIES.get(symbol, {"USD"})
    b_before = buffer_before_min if buffer_before_min is not None else int(getattr(config, "NEWS_BUFFER_BEFORE_MIN", 15))
    b_after = buffer_after_min if buffer_after_min is not None else int(getattr(config, "NEWS_BUFFER_AFTER_MIN", 15))
    now = as_of_utc or dt.datetime.now(dt.timezone.utc)

    events = fetch_calendar_events()
    for e in events:
        if not is_tier1_event(e):
            continue
        country = str(e.get("country", "")).upper()
        if country not in currencies:
            continue

        ev_dt = parse_event_time(e)
        if not ev_dt:
            continue

        diff_min = (ev_dt - now).total_seconds() / 60.0
        # Active window: between -b_after (past) and +b_before (future)
        if -b_after <= diff_min <= b_before:
            title = e.get("title", "High-Impact News")
            direction_desc = f"in {diff_min:.1f}m" if diff_min > 0 else f"{abs(diff_min):.1f}m ago"
            reason = (
                f"RED_FOLDER_BLACKOUT: [{country}] {title} {direction_desc} "
                f"({ev_dt.strftime('%H:%M')} UTC). New fills paused."
            )
            return True, reason, e

    return False, "NO_BLACKOUT", None


def get_directional_actions(country: str) -> tuple[str, str]:
    """Return (strong_currency_action, weak_currency_action) for our traded portfolio."""
    if country == "USD":
        strong = "SELL EURUSD, GBPUSD, AUDUSD, NZDUSD, Gold | BUY USDCAD, USDCHF, USDJPY"
        weak = "BUY EURUSD, GBPUSD, AUDUSD, NZDUSD, Gold | SELL USDCAD, USDCHF, USDJPY"
    elif country == "EUR":
        strong = "BUY EURUSD"
        weak = "SELL EURUSD"
    elif country == "GBP":
        strong = "BUY GBPUSD, GBPAUD"
        weak = "SELL GBPUSD, GBPAUD"
    elif country == "CAD":
        strong = "SELL USDCAD"
        weak = "BUY USDCAD"
    elif country == "AUD":
        strong = "BUY AUDUSD | SELL GBPAUD"
        weak = "SELL AUDUSD | BUY GBPAUD"
    elif country == "NZD":
        strong = "BUY NZDUSD"
        weak = "SELL NZDUSD"
    elif country == "CHF":
        strong = "SELL USDCHF"
        weak = "BUY USDCHF"
    elif country == "JPY":
        strong = "SELL USDJPY"
        weak = "BUY USDJPY"
    else:
        strong = f"BUY {country} pairs against USD"
        weak = f"SELL {country} pairs against USD"
    return strong, weak


def format_consolidated_playbook(ev_list: list[dict]) -> str:
    """Generate a clean, structured tactical pre-news playbook for Telegram with directional BUY/SELL instructions."""
    first = ev_list[0]
    country = str(first.get("country", "")).upper()
    titles = [str(e.get("title", "High-Impact Event")) for e in ev_list]
    ev_dt = parse_event_time(first)
    t_str = ev_dt.strftime("%a %d %b %H:%M UTC") if ev_dt else "Upcoming"

    forecast = first.get("forecast") or "N/A"
    prev = first.get("previous") or "N/A"
    strong_act, weak_act = get_directional_actions(country)

    # Check event characteristics
    is_speech = any(any(w in t.lower() for w in ("speaks", "speech", "press conference", "testimony", "remarks", "minutes")) for t in titles)
    is_inverse = any(any(w in t.lower() for w in ("unemployment claims", "jobless claims", "claims", "unemployment rate")) for t in titles)

    if is_speech:
        scenarios = (
            f"🎯 Tactical Directional Playbook (Central Bank Guidance):\n"
            f"• Hawkish Tone (Slower cuts / Inflation focus -> Strong {country}):\n"
            f"  💵 {strong_act}\n"
            f"• Dovish Tone (Urgent cuts / Labor softening -> Weak {country}):\n"
            f"  💵 {weak_act}\n"
            f"• Mixed / Neutral Guidance:\n"
            f"  ⚠️ High whipsaw risk. Wait for post-speech M15 candle displacement."
        )
    elif is_inverse:
        scenarios = (
            f"🎯 Tactical Directional Playbook (Labor Data - Inverse Impact):\n"
            f"• Actual < Forecast (Fewer Jobless Claims -> Strong {country}):\n"
            f"  💵 {strong_act}\n"
            f"• Actual > Forecast (More Jobless Claims -> Weak {country}):\n"
            f"  💵 {weak_act}\n"
            f"• Inline with Forecast:\n"
            f"  ⚠️ Initial whipsaw risk. Wait for M15 candle close confirmation."
        )
    else:
        scenarios = (
            f"🎯 Tactical Directional Playbook (Economic Release):\n"
            f"• Actual > Forecast (Economic Beat -> Strong {country}):\n"
            f"  💵 {strong_act}\n"
            f"• Actual < Forecast (Economic Miss -> Weak {country}):\n"
            f"  💵 {weak_act}\n"
            f"• Inline with Forecast:\n"
            f"  ⚠️ High initial whipsaw risk. Wait for M15 candle close confirmation."
        )

    if len(titles) == 1:
        event_header = f"📅 Event: [{country}] {titles[0]}\n🕐 Scheduled: {t_str}\n📊 Consensus: {forecast} · Previous: {prev}"
    else:
        event_lines = "\n".join(f"• {t}" for t in titles)
        event_header = f"📅 Events: [{country}] Scheduled at {t_str}:\n{event_lines}"

    msg = (
        f"🚨 RED-FOLDER NEWS BRIEFING (In ~30 Mins)\n"
        f"{'─' * 28}\n"
        f"{event_header}\n"
        f"{'─' * 28}\n"
        f"{scenarios}\n"
        f"{'─' * 28}\n"
        f"🛡️ Bot Execution Safeguards:\n"
        f"• Automated entries PAUSED (-15m to +15m) on affected [{country}] pairs to avoid spread blowout & slippage.\n"
        f"• Existing winning [{country}] trades protected at Break-Even (+1 pip).\n"
        f"🎯 Manual traders: Never chase the 1st second spike!"
    )
    return msg


def format_news_playbook(e: dict) -> str:
    """Single-event compatibility wrapper."""
    return format_consolidated_playbook([e])


def check_and_send_pre_news_alerts(send_func, chat_id: str | int, lookahead_min: int = 30) -> int:
    """Check for high-impact events ~30 mins ahead and dispatch tactical briefing to Telegram with zero duplicate spam."""
    if not getattr(config, "NEWS_PLAYBOOK_ENABLED", True) or not chat_id:
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    events = fetch_calendar_events()
    sent_alerts = _load_sent_alerts()
    dispatched = 0

    # Group pending events by (country, event_time_minute) so simultaneous speeches/releases
    # are consolidated into a single actionable alert instead of sending multiple spam messages
    time_grouped: dict[tuple[str, str], list[dict]] = {}

    for e in events:
        if not is_tier1_event(e):
            continue
        country = str(e.get("country", "")).upper()
        if country not in {"USD", "EUR", "GBP", "AUD", "CAD", "JPY", "NZD", "CHF"}:
            continue

        ev_dt = parse_event_time(e)
        if not ev_dt:
            continue

        diff_min = (ev_dt - now).total_seconds() / 60.0
        # Trigger window: 20 to 35 minutes before release
        if 20.0 <= diff_min <= 35.0:
            key_slot = (country, ev_dt.strftime("%Y-%m-%d %H:%M"))
            time_grouped.setdefault(key_slot, []).append(e)

    for (country, time_str), ev_list in time_grouped.items():
        slot_key = f"{country}:{time_str}:{','.join(sorted(x.get('title','') for x in ev_list))}"
        if slot_key in sent_alerts:
            continue

        msg = format_consolidated_playbook(ev_list)
        try:
            res = send_func(chat_id, msg)
            # send_func in watcher.py returns boolean True on success; handle dict or bool
            is_ok = bool(res.get("ok")) if isinstance(res, dict) else (res is True or res is None)
            if is_ok:
                sent_alerts.add(slot_key)
                _save_sent_alerts(sent_alerts)
                dispatched += 1
                log.info("dispatched pre-news tactical briefing (exactly once): %s", slot_key)
        except Exception as ex:
            log.warning("failed to send news playbook alert: %s", ex)

    return dispatched


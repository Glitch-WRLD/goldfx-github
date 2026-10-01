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


def format_news_playbook(e: dict) -> str:
    """Generate a clean, structured tactical pre-news playbook for Telegram."""
    country = str(e.get("country", "")).upper()
    title = e.get("title", "Tier-1 High-Impact Event")
    ev_dt = parse_event_time(e)
    t_str = ev_dt.strftime("%a %d %b %H:%M UTC") if ev_dt else "Upcoming"
    forecast = e.get("forecast") or "N/A"
    prev = e.get("previous") or "N/A"

    # Directional scenarios
    lower_title = title.lower()
    if any(k in lower_title for k in ("cpi", "inflation", "pce", "rate", "nfp", "employment", "gdp")):
        if country == "USD":
            strong_usd_action = "SELL EURUSD, GBPUSD, Gold | BUY USDCAD"
            weak_usd_action = "BUY EURUSD, GBPUSD, Gold | SELL USDCAD"
        else:
            strong_usd_action = f"BUY {country} pairs against USD"
            weak_usd_action = f"SELL {country} pairs against USD"

        scenarios = (
            f"\n🎯 Tactical Directional Playbook:"
            f"\n• Actual > Forecast (Strong {country}):"
            f"\n  💵 {strong_usd_action}"
            f"\n• Actual < Forecast (Weak {country}):"
            f"\n  💵 {weak_usd_action}"
            f"\n• Inline with Forecast:"
            f"\n  ⚠️ High risk of initial whipsaw. Wait for M15 candle close."
        )
    else:
        scenarios = (
            f"\n🎯 Volatility Advisory:"
            f"\n• High institutional volume & spread expansion expected."
            f"\n• Wait for post-news M15 displacement before manual entry."
        )

    msg = (
        f"🚨 RED-FOLDER NEWS BRIEFING (In 30 Mins)\n"
        f"{'─' * 26}\n"
        f"📅 Event: [{country}] {title}\n"
        f"🕐 Scheduled: {t_str}\n"
        f"📊 Consensus: {forecast} · Previous: {prev}\n"
        f"{'─' * 26}"
        f"{scenarios}\n"
        f"{'─' * 26}\n"
        f"🛡️ Bot Execution Safeguards:\n"
        f"• Automated entries PAUSED (-15m to +15m) to avoid spread blowout & slippage.\n"
        f"• Existing winning trades protected at Break-Even (+1 pip).\n"
        f"🎯 Manual traders: Never chase the 1st second spike!"
    )
    return msg


def check_and_send_pre_news_alerts(send_func, chat_id: str | int, lookahead_min: int = 30) -> int:
    """Check for high-impact events ~30 mins ahead and dispatch tactical briefing to Telegram."""
    if not getattr(config, "NEWS_PLAYBOOK_ENABLED", True) or not chat_id:
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    events = fetch_calendar_events()
    sent_alerts = _load_sent_alerts()
    dispatched = 0

    for e in events:
        if not is_tier1_event(e):
            continue
        country = str(e.get("country", "")).upper()
        # Only notify for currencies our agent actually trades
        if country not in {"USD", "EUR", "GBP", "AUD", "CAD", "JPY", "NZD", "CHF"}:
            continue

        ev_dt = parse_event_time(e)
        if not ev_dt:
            continue

        diff_min = (ev_dt - now).total_seconds() / 60.0
        # Trigger window: 20 to 35 minutes before release
        if 20.0 <= diff_min <= 35.0:
            key = f"{country}:{e.get('title')}:{ev_dt.isoformat()}"
            if key in sent_alerts:
                continue

            msg = format_news_playbook(e)
            try:
                res = send_func(chat_id, msg)
                if getattr(res, "get", None) and res.get("ok", True):
                    sent_alerts.add(key)
                    _save_sent_alerts(sent_alerts)
                    dispatched += 1
                    log.info("dispatched pre-news tactical briefing: %s", key)
            except Exception as ex:
                log.warning("failed to send news playbook alert: %s", ex)

    return dispatched

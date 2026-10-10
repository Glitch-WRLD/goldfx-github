"""Trusted UTC clock + deterministic broker-time offset.

Why this exists: on 2026-10-09 the PC clock was 69.8 min slow (w32time was dead).
Every wall-clock gate (ISDE killzones, stale-signal age, rollover, ...) and the
MT5 bar-timestamp offset (derived from PC-now vs last tick) silently went wrong,
so ISDE setups were missed or entered late. All time-critical code should use
`utc_now()` / `utc_ts()` from here, and `broker_offset_sec()` for MT5 bar times.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import logging
import subprocess
import threading
import time
import urllib.request

log = logging.getLogger("clock")

_REFERENCE_URLS = (
    "https://www.google.com",
    "https://www.cloudflare.com",
    "https://www.microsoft.com",
)
_REFRESH_SEC = 300.0
_WARN_SKEW_SEC = 30.0
_RESYNC_COOLDOWN_SEC = 600.0

_lock = threading.Lock()
_skew_sec = 0.0            # pc_clock - true_utc  (positive = PC is fast)
_last_refresh = 0.0
_last_resync = 0.0
_measured_once = False


def _measure_skew() -> float | None:
    """PC clock minus true UTC, from an HTTP Date header (midpoint of request)."""
    samples: list[float] = []
    for url in _REFERENCE_URLS:
        try:
            t0 = time.time()
            req = urllib.request.Request(url, method="HEAD",
                                         headers={"User-Agent": "goldfx-clock/1.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                date_hdr = resp.headers.get("Date")
            t1 = time.time()
            if not date_hdr:
                continue
            server = email.utils.parsedate_to_datetime(date_hdr).timestamp()
            # Date header has 1s resolution -> server true time is in [server, server+1)
            samples.append(((t0 + t1) / 2.0) - (server + 0.5))
        except Exception:
            continue
    if not samples:
        return None
    samples.sort()
    return samples[len(samples) // 2]   # median: one CDN with a stale cached Date cannot skew us


def _try_resync() -> None:
    """Best-effort: restart w32time and force an NTP resync (needs admin)."""
    global _last_resync
    now = time.time()
    if now - _last_resync < _RESYNC_COOLDOWN_SEC:
        return
    _last_resync = now
    try:
        subprocess.run(["sc", "start", "w32time"], capture_output=True, timeout=15)
        r = subprocess.run(["w32tm", "/resync", "/force"], capture_output=True,
                           timeout=30, text=True)
        log.warning("clock: attempted w32tm /resync -> rc=%s %s", r.returncode,
                    (r.stdout or r.stderr or "").strip()[:120])
    except Exception as e:
        log.warning("clock: resync attempt failed: %s", e)


def refresh_skew(force: bool = False) -> float:
    """Re-measure PC-vs-true-UTC skew (cached for _REFRESH_SEC). Returns skew in seconds."""
    global _skew_sec, _last_refresh, _measured_once
    now = time.time()
    with _lock:
        if not force and _measured_once and now - _last_refresh < _REFRESH_SEC:
            return _skew_sec
        _last_refresh = now
    s = _measure_skew()
    if s is None:
        return _skew_sec          # keep last known; network down is not a reason to drift
    with _lock:
        prev = _skew_sec
        _skew_sec = s
        first = not _measured_once
        _measured_once = True
    if abs(s) > _WARN_SKEW_SEC:
        log.critical("CLOCK SKEW: PC clock is %+.1fs vs true UTC — using corrected time "
                     "(prev %+.1fs). Fix w32time!", s, prev)
        _try_resync()
    elif first or abs(s - prev) > 5:
        log.info("clock: PC skew vs true UTC = %+.2fs", s)
    return s


def skew_seconds() -> float:
    refresh_skew()
    return _skew_sec


def utc_ts() -> float:
    """Skew-corrected epoch seconds (true UTC)."""
    return time.time() - skew_seconds()


def utc_now() -> dt.datetime:
    """Skew-corrected timezone-aware UTC datetime."""
    return dt.datetime.fromtimestamp(utc_ts(), dt.timezone.utc)


def broker_offset_sec(now: dt.datetime | None = None) -> int:
    """Broker server time minus UTC, in seconds.

    Headway MT5 server time = New-York time + 7h  (UTC+3 during US DST, UTC+2 otherwise).
    Deterministic — does NOT depend on the PC clock nor on how stale the last tick is.
    Override with config.BROKER_UTC_OFFSET_H if the broker ever changes.
    """
    try:
        import config
        override = getattr(config, "BROKER_UTC_OFFSET_H", None)
        if override not in (None, ""):
            return int(float(override) * 3600)
    except Exception:
        pass
    now = now or utc_now()
    try:
        from zoneinfo import ZoneInfo
        ny_off = now.astimezone(ZoneInfo("America/New_York")).utcoffset()
        return int(ny_off.total_seconds()) + 7 * 3600
    except Exception:
        return 3 * 3600

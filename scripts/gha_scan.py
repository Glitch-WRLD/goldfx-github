#!/usr/bin/env python3
"""GoldFX Agent - scheduler (GitHub Actions cron) runner.

Runs once per workflow tick on an ephemeral runner (no persistent disk):
  1. loads gha_state/state.json (delivered signals, update offset, profile)
  2. re-scans a trailing window of the latest closed bars per symbol
     (catch-up: bars between ticks are delivered late, never lost)
  3. sends new signals to the configured Telegram channel
  4. reads pending commands via getUpdates and replies with a short status

Secrets come from GitHub Actions secrets (BOT_TOKEN, CHAT_ID).
State is committed back to the repo by the workflow so the next tick
continues where this one stopped.

Local smoke test (no updates consumed, market can be closed):
    python scripts/gha_scan.py --no-updates
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv  # local runs only
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass

import os

from engine.risk import format_decimal
from engine.scanner import FVGScanner, format_message, format_zone_alert
from strategy.profiles import PROFILES, SYMBOL_RUNTIME

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gha-scan")

BASE_DIR = Path(__file__).resolve().parents[1]
GHA_STATE_DIR = BASE_DIR / "gha_state"
STATE_FILE = GHA_STATE_DIR / "state.json"
LOOKBACK_BARS = int(os.getenv("GHA_LOOKBACK", "40"))
MAX_SENDS_PER_TICK = int(os.getenv("GHA_MAX_SENDS", "8"))

TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")


def log_identity() -> None:
    """Debug the configured bot+chat WITHOUT printing secrets:
    reports the bot username and the resolved chat's id/title so a
    'chat not found' can be traced to a wrong BOT_TOKEN or CHAT_ID."""
    if not TOKEN:
        log.info("identity: no BOT_TOKEN configured")
        return
    me = tg("getMe")
    if me.get("ok"):
        u = me["result"]
        log.info("identity: bot=@%s (id=%s) chat_raw=%r",
                 u.get("username"), u.get("id"), CHAT_ID)
    else:
        log.warning("identity: getMe failed: %s", me.get("description"))
    if CHAT_ID:
        log.info("identity: chat_raw len=%d starts_minus=%s numeric=%s",
                 len(CHAT_ID), CHAT_ID.startswith("-"),
                 CHAT_ID.lstrip("-").isdigit())
        c = tg("getChat", chat_id=CHAT_ID)
        if c.get("ok"):
            log.info("identity: chat resolves to id=%s type=%s title=%r",
                     c["result"].get("id"), c["result"].get("type"),
                     c["result"].get("title"))
        else:
            log.warning("identity: getChat(%r) failed: %s",
                        CHAT_ID, c.get("description"))


# --------------------------------------------------------------------------- telegram
def tg(method: str, **params) -> dict:
    """Direct call to the Bot API; retries transient network errors."""
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    body = json.dumps(params).encode()
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                last = f"HTTP {e.code}: {e.read().decode()[:300]}"
            except Exception:
                last = e
            time.sleep(2 * (attempt + 1))
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    log.warning("tg %s failed: %s", method, last)
    return {"ok": False, "description": str(last)}


def send(chat_id, text: str) -> bool:
    res = tg("sendMessage", chat_id=chat_id, text=text)
    if not res.get("ok"):
        log.warning("send failed: %s", res.get("description"))
    return bool(res.get("ok"))


# --------------------------------------------------------------------------- state
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as e:
            log.warning("state unreadable (%s); starting fresh", e)
    return {"offset": 0, "profile": "balanced", "delivered": [], "zones": [],
            "history": [], "outcomes": {}}


def save_state(s: dict) -> None:
    GHA_STATE_DIR.mkdir(exist_ok=True)
    s["delivered"] = s["delivered"][-500:]
    s["zones"] = s["zones"][-200:]
    s["history"] = s["history"][:200]
    s.setdefault("outcomes", {})
    outstanding = [(k, v) for k, v in s["outcomes"].items()
                   if v.get("status") in ("pending", "posted")]
    s["outcomes"] = dict(outstanding[-500:])
    STATE_FILE.write_text(json.dumps(s, indent=1, sort_keys=True, default=str))


# --------------------------------------------------------------------------- commands
def handle_commands(state: dict, no_updates: bool) -> None:
    if no_updates or not TOKEN or not CHAT_ID:
        return
    res = tg("getUpdates", offset=state.get("offset", 0), timeout=5,
             allowed_updates=["message"])
    if not res.get("ok"):
        return
    for up in res.get("result", []):
        upd_id = up["update_id"]
        state["offset"] = upd_id + 1
        msg = up.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = (msg.get("chat") or {}).get("id")
        if not chat_id or not text.startswith("/"):
            continue
        cmd, _, arg = text.partition(" ")
        arg = arg.strip().lower()
        reply = ["GoldFX agent (scheduler mode - replies may lag).\n/help for commands"]
        try:
            if cmd == "/start" or cmd == "/help":
                reply = ["/status  /bias  /history  /setprofile hi|balanced  /scannow  /help"]
            elif cmd == "/status":
                p = PROFILES[state["profile"]]["label"]
                reply = [f"Profile: {p}\nHistory: {len(state['history'])} signals\n"
                         f"Last tick: {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%MZ}"]
            elif cmd == "/bias":
                sc = FVGScanner(state["profile"])
                reply = []
                for sym, rt in SYMBOL_RUNTIME.items():
                    try:
                        b = sc.current_bias(sym, rt["entry_tf"], rt["bias_htf"])
                    except Exception as e:
                        b = f"err({e})"
                    reply.append(f"{sym}: {b}")
                if not reply:
                    reply = ["no data"]
            elif cmd == "/history":
                if not state["history"]:
                    reply = ["No signals delivered yet."]
                else:
                    reply = [f"{h['symbol']} {h['dir']} @ {h['ts']} "
                             f"entry {h['entry']} sl {h['sl']} tp {h['tp']} rr 1:{h['rr']}"
                             for h in state["history"][:5]]
            elif cmd == "/setprofile":
                if arg in PROFILES:
                    state["profile"] = arg
                    save_state(state)
                    reply = [f"Profile -> {PROFILES[arg]['label']}"]
                else:
                    reply = ["Unknown profile. Use hi or balanced."]
            elif cmd == "/scannow":
                reply = ["Next scheduled scan delivers any new signals. "
                         f"Current history: {len(state['history'])} signals"]
            else:
                reply = [f"Unknown command: {cmd}"]
        except Exception as e:  # never crash the tick on a bad command
            reply = [f"error: {e}"]
        send(chat_id, "\n".join(reply))


# --------------------------------------------------------------------------- scan
def scan_and_deliver(state: dict) -> None:
    if not TOKEN or not CHAT_ID:
        log.error("BOT_TOKEN / CHAT_ID not set")
        return
    log_identity()
    sc = FVGScanner(state.get("profile", "balanced"))
    delivered = set(state.get("delivered", []))
    advised_zones = set(state.get("zones", []))
    sent_this_tick = 0
    for sym, rt in SYMBOL_RUNTIME.items():
        try:
            zones = sc.scan_new_zones(sym, rt["entry_tf"], rt["bias_htf"],
                                      lookback=LOOKBACK_BARS)
        except Exception as e:
            log.warning("zone scan %s failed: %s", sym, e)
            zones = []
        for za in zones:
            zkey = f"{sym}:{za.ts.isoformat()}"
            if zkey in advised_zones or sent_this_tick >= MAX_SENDS_PER_TICK:
                continue
            if sent_this_tick >= MAX_SENDS_PER_TICK:
                log.warning("send cap reached; zone %s queued", zkey)
                continue
            ok = send(CHAT_ID, format_zone_alert(za))
            if ok:
                advised_zones.add(zkey)
                sent_this_tick += 1
                log.info("zone alert %s", zkey)
            else:
                log.warning("zone %s send failed (retry next tick)", zkey)
        try:
            sigs = sc.scan_catchup(sym, rt["entry_tf"], rt["bias_htf"],
                                   lookback=LOOKBACK_BARS)
        except Exception as e:
            log.warning("scan %s failed: %s", sym, e)
            continue
        for sig in sigs:
            key = f"{sym}:{sig.ts.isoformat()}"
            if key in delivered:
                continue
            if sent_this_tick >= MAX_SENDS_PER_TICK:
                log.warning("send cap reached; %s queued for next tick", key)
                continue
            ref = state.get("ref_seq", 0) + 1
            ok = send(CHAT_ID, format_message(sig, ref=ref))
            if ok:
                delivered.add(key)
                sent_this_tick += 1
                state["ref_seq"] = ref
                state["history"].insert(0, {
                    "symbol": sym, "dir": "LONG" if sig.direction == 1 else "SHORT",
                    "ts": sig.ts.isoformat(), "tf": sig.entry_tf,
                    "entry": sig.entry, "sl": sig.stop, "tp": sig.take_profit,
                    "rr": sig.rr, "profile": sig.profile, "ref": ref,
                    "strategy_type": sig.strategy_type, "strategy_badge": sig.strategy_badge})
                log.info("delivered %s as setup #%04d", key, ref)
            else:
                log.warning("failed to deliver %s (will retry next tick)", key)
    state["delivered"] = sorted(delivered)
    state["zones"] = sorted(advised_zones)


# --------------------------------------------------------------------------- outcomes
def format_outcome(h: dict, hit: str, when_ts, exit_price: float) -> str:
    """Follow-up message posted when a delivered signal's TP or SL is touched."""
    d = h.get("dir", "?")
    sym = h.get("symbol", "?")
    emoji = "\U0001F7E2" if d == "LONG" else "\U0001F534"
    res = "\U0001F3AF TP HIT" if hit == "tp" else "\u26D4 SL HIT"
    rr = float(h.get("rr", 0))
    sign = "+" if hit == "tp" else "-"
    e = format_decimal(sym, h.get("entry", 0))
    p = format_decimal(sym, exit_price)
    ref = h.get("ref")
    ref_line = "" if ref is None else f"  \u00b7 setup #{ref:04d}"
    return (
        f"{emoji} {sym} \u2014 {res}{ref_line}"
        f"\n{'\u2500' * 26}"
        f"\n{d} \u00b7 entry {e} \u00b7 exit {p}"
        f"\n{sign}{abs(rr):.2f}R  \u00b7 {str(when_ts)[:16]}"
        f"\n{'\u2500' * 26}\n"
        f"\u26A0\ufe0f Not financial advice."
    )


def check_outcomes(state: dict) -> int:
    """Walk forward bars for every delivered signal whose outcome is unknown and
    post a TP/SL follow-up message once price reaches the level. Returns the
    number of follow-ups posted this tick."""
    from data.tv_data import get_df
    outcomes = state.setdefault("outcomes", {})
    sent = 0
    seen = set()
    frames: dict = {}
    for h in state.get("history", []):
        sym = h.get("symbol"); tf = h.get("tf"); ts = h.get("ts")
        if not sym or not tf or not ts:
            continue
        key = f"{sym}:{ts}"
        if key in seen:
            continue
        seen.add(key)
        if outcomes.get(key, {}).get("status") == "posted":
            continue
        if (sym, tf) not in frames:
            try:
                frames[(sym, tf)] = get_df(sym, tf, refresh=False)
            except Exception as e:
                log.warning("outcome fetch %s: %s", key, e)
                frames[(sym, tf)] = None
        df = frames[(sym, tf)]
        if df is None or len(df) < 5:
            continue
        import pandas as pd
        try:
            idx = int(df.index.get_loc(pd.Timestamp(ts)))
        except (KeyError, TypeError):
            outcomes[key] = {"status": "posted", "hit": "drop", "ts": ts}
            continue
        entry = float(h.get("entry", 0))
        sl = float(h.get("sl", 0))
        tp = float(h.get("tp", 0))
        if not (entry and sl and tp):
            outcomes[key] = {"status": "posted", "hit": "drop", "ts": ts}
            continue
        direction = 1 if h.get("dir") == "LONG" else -1
        hit = None; when = None; ep = None
        for i in range(idx + 1, len(df)):
            hi = float(df["high"].iloc[i]); lo = float(df["low"].iloc[i])
            t = df.index[i]
            if direction == 1:
                if hi >= tp: hit, when, ep = "tp", t, tp; break
                if lo <= sl: hit, when, ep = "sl", t, sl; break
            else:
                if lo <= tp: hit, when, ep = "tp", t, tp; break
                if hi >= sl: hit, when, ep = "sl", t, sl; break
        if hit is None:
            continue  # still running; wait for next tick
        if sent >= MAX_SENDS_PER_TICK:
            outcomes[key] = {"status": "pending", "hit": hit, "when": str(when),
                             "price": ep, "ts": ts}
            continue
        ok = send(CHAT_ID, format_outcome(h, hit, when, ep))
        if ok:
            outcomes[key] = {"status": "posted", "hit": hit, "when": str(when),
                             "price": ep, "ts": ts}
            sent += 1
            log.info("outcome %s: %s", key, hit)
        else:
            outcomes[key] = {"status": "pending", "hit": hit, "when": str(when),
                             "price": ep, "ts": ts}
    return sent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-updates", action="store_true",
                    help="do not consume getUpdates (local smoke tests only)")
    args = ap.parse_args()

    if args.no_updates:
        os.environ["GHA_NO_UPDATES"] = "1"

    state = load_state()
    scan_and_deliver(state)
    check_outcomes(state)
    if not args.no_updates and os.getenv("GHA_NO_UPDATES") != "1":
        handle_commands(state, no_updates=False)
    else:
        log.info("updates skipped")
    save_state(state)
    log.info("tick done: %d delivered, %d history",
             len(state["delivered"]), len(state["history"]))


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""GoldFX Agent - weekly close report.

Posts a stats summary to the configured Telegram channel each time the
market closes for the week (scheduled by .github/workflows/weekly-report.yml).

Groups every delivered setup by the bot-upgrade era active when it was
captured (boundaries are the UTC times the relevant commits were pushed),
then summarizes the trailing 7 days and the all-time totals.

Secrets come from GitHub Actions secrets (BOT_TOKEN, CHAT_ID). The script
only reads gha_state/state.json (committed by the scanner) and sends the
formatted report - it never modifies state.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("weekly-report")

BASE_DIR = Path(__file__).resolve().parents[1]
STATE_FILE = BASE_DIR / "gha_state" / "state.json"
TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# Upgrade eras: (label, UTC boundary). Setups captured before the boundary
# belong to the era; the last one is open-ended (the current config).
# Update this list when a new version-affecting config ships.
ERAS = [
    ("Grand opening (no floor, no clamp)",             datetime.datetime(2026, 9, 15, 6, 3, tzinfo=datetime.timezone.utc)),
    ("RR floor + outcomes (TP unclamped)",             datetime.datetime(2026, 9, 15, 14, 24, tzinfo=datetime.timezone.utc)),
    ("TP clamp to 1.4R",                                datetime.datetime(2026, 9, 16, 9, 56, tzinfo=datetime.timezone.utc)),
    ("Session filter + 3-pair + setup #",               datetime.datetime(2026, 9, 16, 20, 43, tzinfo=datetime.timezone.utc)),
    ("sl_buf 0.25 (current)",                           None),
]


def _parse_ts(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def era_index(ts_str: str) -> int:
    t = _parse_ts(ts_str)
    for i, (_, bound) in enumerate(ERAS):
        if bound is None:
            return i
        if t < bound:
            return i
    return len(ERAS) - 1


def _fmt_pct(x: float) -> str:
    return f"{x:.1%}"


def _stats(rows: list[dict]) -> dict:
    """Aggregate (n, tp, sl, wr, avg_rr, tot_r, pf) from raw history rows of
    the form {rr, outcome} where outcome is 'tp'/'sl' ('' for pending)."""
    tp = sum(1 for r in rows if r["outcome"] == "tp")
    sl = sum(1 for r in rows if r["outcome"] == "sl")
    n = len(rows)
    rr = [r["rr"] for r in rows if r.get("rr") is not None]
    realized = [r["rr"] for r in rows if r["outcome"] == "tp"] + \
        [-1.0] * sl
    tot = sum(realized) if realized else 0.0
    gw = sum(r for r in realized if r > 0)
    gl = -sum(r for r in realized if r < 0)
    pf = gw / gl if gl > 0 else float("inf") if gw > 0 else 0.0
    return {
        "n": n, "tp": tp, "sl": sl,
        "wr": (tp / n) if n else 0.0,
        "avg_rr": (sum(rr) / len(rr)) if rr else 0.0,
        "tot_r": tot, "pf": pf,
        "pending": sum(1 for r in rows if not r["outcome"]),
    }


def load_rows() -> list[dict]:
    if not STATE_FILE.exists():
        log.error("no state at %s", STATE_FILE)
        sys.exit(1)
    state = json.loads(STATE_FILE.read_text())
    outcomes = state.get("outcomes", {})
    rows = []
    for h in state.get("history", []):
        o = outcomes.get(f"{h.get('symbol')}:{h.get('ts')}", {})
        rows.append({
            "ts": h["ts"],
            "kind": "setup",
            "symbol": h.get("symbol", "?"),
            "dir": h.get("dir", "?"),
            "rr": h.get("rr"),
            "outcome": o.get("hit", ""),
            "ref": h.get("ref"),
        })
    return rows


def build_report(rows: list[dict]) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    L: list[str] = []
    L.append("\U0001F4CA Weekly close report")
    L.append(f"Generated {now:%a %d %b %Y %H:%M}Z")
    L.append("\u2500" * 26)

    # trailing 7 days
    week_ago = now - datetime.timedelta(days=7)
    wk = [r for r in rows if _parse_ts(r["ts"]) >= week_ago]
    s = _stats(wk)
    if s["n"]:
        L.append(f"LAST 7 DAYS: {s['n']} setups ({s['tp']} TP / {s['sl']} SL"
                 f"{' / ' + str(s['pending']) + ' open' if s['pending'] else ''})")
        L.append(f"WR {_fmt_pct(s['wr'])}  avgRR {s['avg_rr']:.2f}  "
                 f"totR {s['tot_r']:+.1f}  PF {s['pf']:.2f}")
    else:
        L.append("LAST 7 DAYS: no setups")
    L.append("\u2500" * 26)

    # all-time, per era
    s_all = _stats(rows)
    L.append(f"ALL-TIME: {s_all['n']} setups, WR {_fmt_pct(s_all['wr'])}, "
             f"totR {s_all['tot_r']:+.1f}, PF {s_all['pf']:.2f}")
    L.append("")
    for i, (label, _) in enumerate(ERAS):
        era_rows = [r for r in rows if era_index(r["ts"]) == i]
        s = _stats(era_rows)
        if not s["n"]:
            continue
        L.append(f"\u25CE {label}:")
        L.append(f"    {s['n']} setups ({s['tp']} TP / {s['sl']} SL)"
                 f"  WR {_fmt_pct(s['wr'])}  totR {s['tot_r']:+.1f}  PF {s['pf']:.2f}")

    L.append("\u2500" * 26)
    L.append("\u26A0\ufe0f Not financial advice.")
    return "\n".join(L)


def tg(method: str, **params) -> dict:
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


def main() -> int:
    if not TOKEN or not CHAT_ID:
        log.error("BOT_TOKEN / CHAT_ID not set")
        return 2
    report = build_report(load_rows())
    print(report)
    res = tg("sendMessage", chat_id=CHAT_ID, text=report)
    if not res.get("ok"):
        log.error("send failed: %s", res.get("description"))
        return 1
    log.info("weekly report posted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
#!/usr/bin/env python3
"""GoldFX Agent - weekly close report.

Posts a stats summary to the configured Telegram channel each time the
market closes for the week (scheduled by .github/workflows/weekly-report.yml).

Summarizes:
1. Trailing 7 days (weekly) overall and broken down across all pairs.
2. All-time overall and broken down across all pairs.

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

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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


def _parse_ts(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _fmt_pct(x: float) -> str:
    return f"{x:.1%}"


def _stats(rows: list[dict]) -> dict:
    """Aggregate (n, tp, sl, wr, avg_rr, tot_r, pf, pending) from raw history rows."""
    tp = sum(1 for r in rows if r.get("outcome") == "tp")
    sl = sum(1 for r in rows if r.get("outcome") == "sl")
    resolved = tp + sl
    n = len(rows)
    pending = sum(1 for r in rows if r.get("outcome") not in ("tp", "sl"))

    rr_vals = [float(r["rr"]) for r in rows if r.get("rr") is not None and float(r["rr"]) > 0]
    avg_rr = (sum(rr_vals) / len(rr_vals)) if rr_vals else 0.0

    realized = [float(r["rr"]) for r in rows if r.get("outcome") == "tp" and r.get("rr") is not None] + [-1.0] * sl
    tot_r = sum(realized) if realized else 0.0
    gw = sum(r for r in realized if r > 0)
    gl = -sum(r for r in realized if r < 0)
    pf = (gw / gl) if gl > 0 else (999.0 if gw > 0 else 0.0)

    wr = (tp / resolved) if resolved > 0 else 0.0
    return {
        "n": n,
        "resolved": resolved,
        "tp": tp,
        "sl": sl,
        "pending": pending,
        "wr": wr,
        "avg_rr": avg_rr,
        "tot_r": tot_r,
        "pf": pf,
    }


def _stats_by_pair(rows: list[dict]) -> list[tuple[str, dict]]:
    """Group rows by symbol and sort by total R descending."""
    syms = sorted(set(r["symbol"] for r in rows if r.get("symbol")))
    pair_stats = []
    for sym in syms:
        sub = [r for r in rows if r.get("symbol") == sym]
        st = _stats(sub)
        pair_stats.append((sym, st))
    pair_stats.sort(key=lambda item: item[1]["tot_r"], reverse=True)
    return pair_stats


def load_rows() -> list[dict]:
    if not STATE_FILE.exists():
        log.error("no state at %s", STATE_FILE)
        sys.exit(1)
    state = json.loads(STATE_FILE.read_text())
    outcomes = state.get("outcomes", {})
    rows = []
    for h in state.get("history", []):
        sym = h.get("symbol", "?")
        ts = h.get("ts", "")
        o = outcomes.get(f"{sym}:{ts}", {})
        rows.append({
            "ts": ts,
            "kind": "setup",
            "symbol": sym,
            "dir": h.get("dir", "?"),
            "rr": h.get("rr"),
            "outcome": o.get("hit", ""),
            "ref": h.get("ref"),
        })
    return rows


def build_report(rows: list[dict]) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    L: list[str] = []
    L.append("📊 GOLDFX WEEKLY CLOSE REPORT")
    L.append(f"Generated {now:%a %d %b %Y %H:%M} UTC")
    L.append("─" * 26)

    # 1. Trailing 7 days (Weekly Performance)
    week_ago = now - datetime.timedelta(days=7)
    wk_rows = [r for r in rows if _parse_ts(r["ts"]) >= week_ago]
    s_wk = _stats(wk_rows)

    L.append("🗓 LAST 7 DAYS (WEEKLY PERFORMANCE)")
    if s_wk["n"]:
        pf_str = f"{s_wk['pf']:.2f}" if s_wk["pf"] < 100 else "∞"
        L.append(f"Total: {s_wk['n']} setups ({s_wk['tp']} TP / {s_wk['sl']} SL"
                 f"{' / ' + str(s_wk['pending']) + ' open' if s_wk['pending'] else ''})")
        L.append(f"Win Rate: {_fmt_pct(s_wk['wr'])} · Net R: {s_wk['tot_r']:+.1f}R · PF: {pf_str}")
        L.append(f"Average Planned RR: 1 : {s_wk['avg_rr']:.2f}")
        L.append("")
        L.append("Per-Pair Weekly Breakdown:")
        for sym, st in _stats_by_pair(wk_rows):
            p_pf = f"{st['pf']:.2f}" if st["pf"] < 100 else "∞"
            L.append(f"  • {sym:7s} {st['n']:2d} setups ({st['tp']:2d}W - {st['sl']:2d}L) "
                     f"· WR {_fmt_pct(st['wr'])} · {st['tot_r']:+6.1f}R · PF {p_pf}")
    else:
        L.append("  No setups delivered in the last 7 days.")
    L.append("─" * 26)

    # 2. All-Time Performance across all pairs
    s_all = _stats(rows)
    all_pf = f"{s_all['pf']:.2f}" if s_all["pf"] < 100 else "∞"
    L.append("🌐 ALL-TIME PERFORMANCE (ALL PAIRS)")
    L.append(f"Total: {s_all['n']} setups ({s_all['tp']} TP / {s_all['sl']} SL"
             f"{' / ' + str(s_all['pending']) + ' open' if s_all['pending'] else ''})")
    L.append(f"Win Rate: {_fmt_pct(s_all['wr'])} · Net R: {s_all['tot_r']:+.1f}R · PF: {all_pf}")
    L.append(f"Average Planned RR: 1 : {s_all['avg_rr']:.2f}")
    L.append("")
    L.append("Per-Pair All-Time Breakdown:")
    for sym, st in _stats_by_pair(rows):
        p_pf = f"{st['pf']:.2f}" if st["pf"] < 100 else "∞"
        L.append(f"  • {sym:7s} {st['n']:2d} setups ({st['tp']:2d}W - {st['sl']:2d}L) "
                 f"· WR {_fmt_pct(st['wr'])} · {st['tot_r']:+6.1f}R · PF {p_pf}")

    L.append("─" * 26)
    L.append("⚠️ Not financial advice. Verified outcomes via institutional benchmark.")
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
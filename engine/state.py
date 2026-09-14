"""Small JSON state store for the bot: dedupe, history, runtime profile."""

from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
STATE_FILE = Path(os.getenv("STATE_FILE", BASE_DIR / "bot_state.json"))


def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"profile": "hi", "last_signal": {}, "history": [], "last_scan": None}


def _save(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def get_profile() -> str:
    return _load().get("profile", "hi")


def set_profile(name: str) -> None:
    s = _load()
    s["profile"] = name
    _save(s)


def mark_sent(symbol: str, ts: str, payload: dict) -> bool:
    """Return True if the signal is new (and store it)."""
    s = _load()
    key = f"{symbol}:{ts}"
    if s["last_signal"].get(key):
        return False
    s["last_signal"][key] = True
    # prune old keys, keep the last 400
    keys = list(s["last_signal"])
    if len(keys) > 400:
        for k in keys[:-400]:
            s["last_signal"].pop(k, None)
    s["history"] = ([payload] + s["history"])[:200]
    _save(s)
    return True


def touch_last_scan() -> None:
    import datetime
    s = _load()
    s["last_scan"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _save(s)


def history(limit: int = 10) -> list[dict]:
    return _load().get("history", [])[:limit]
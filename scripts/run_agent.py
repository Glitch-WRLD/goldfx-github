"""Entry point for the GoldFX auto-trading agent (droplet).

Usage:
    python scripts/run_agent.py            # paper mode by default
    AUTO_TRADE_ENV=demo python scripts/run_agent.py

Set BOT_TOKEN, CHAT_ID, and optionally OANDA_TOKEN/OANDA_ACCOUNT_ID in .env.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ctypes
import logging

_MUTEX_HANDLE = None


def acquire_single_instance_mutex(mutex_name: str = "Local\\GoldFX_Agent_SingleInstance_Mutex") -> bool:
    """Ensure strictly ONE instance of goldfx-agent runs on the machine at any time."""
    global _MUTEX_HANDLE
    try:
        kernel32 = ctypes.windll.kernel32
        _MUTEX_HANDLE = kernel32.CreateMutexW(None, True, mutex_name)
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
        return True
    except Exception:
        return True


if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parents[1]
    log_dir = base_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "agent.log"

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8")
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers
    )
    if not acquire_single_instance_mutex():
        logging.getLogger("goldfx.agent").critical(
            "DUPLICATE INSTANCE BLOCKED: Another instance of goldfx-agent is already running on this machine! Exiting immediately to prevent double orders."
        )
        sys.exit(0)

    from agent.watcher import run_agent
    run_agent()
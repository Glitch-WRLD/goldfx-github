"""Entry point for the GoldFX auto-trading agent (droplet).

Usage:
    python scripts/run_agent.py            # paper mode by default
    AUTO_TRADE_ENV=demo python scripts/run_agent.py

Set BOT_TOKEN, CHAT_ID, and optionally OANDA_TOKEN/OANDA_ACCOUNT_ID in .env.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging
from agent.watcher import run_agent

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    run_agent()
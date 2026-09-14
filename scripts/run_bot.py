"""Entry point for the GoldFX Telegram signal bot.

Usage:
    python scripts/run_bot.py
Set BOT_TOKEN (and optionally CHAT_ID) in .env first.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.telegram_bot import main

if __name__ == "__main__":
    main()
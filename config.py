import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ---- TradingView symbols & timeframes ----
SYMBOLS = {
    "XAUUSD": "OANDA:XAUUSD",
    "EURUSD": "OANDA:EURUSD",
    "GBPUSD": "OANDA:GBPUSD",
    "USDJPY": "OANDA:USDJPY",
    "AUDUSD": "OANDA:AUDUSD",
}
# map brand names -> tradingview-sdk Interval
TIMEFRAMES = ["M15", "M30", "H1", "H2"]

DATA_CACHE = BASE_DIR / "data_cache"
REPORTS = BASE_DIR / "reports"
for d in (DATA_CACHE, REPORTS):
    d.mkdir(exist_ok=True)

# ---- Telegram ----
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# ---- Risk / money mgmt (from trader-quality research) ----
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "10000"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "1.0"))   # % of balance at risk per trade
MAX_RISK_PER_TRADE = 2.0                                      # hard cap
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "3.0"))  # % of balance -> stop trading for the day
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "4"))
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))   # scaled kelly cap

# ---- Scanner ----
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "150"))  # ~ every 2.5 min
MIN_TRADES_BEFORE_RESEND_SAME_SETUP = 2

# ---- Gold/EURUSD contract facts (for lot size maths) ----
# point = minimum price step; pip_value_per_lot_usd = value of one "point-move"
# per 1.0 lot. XAU: 1 point = 0.01 $/oz -> $1 per 1.0 lot.
# EUR: 1 point = 0.00001 -> $0.10 per point per 1.0 lot (10 USD/pip).
CONTRACTS = {
    "XAUUSD": {"point": 0.01, "pip_value_per_lot_usd": 100.0 / 100, "digits": 2},
    "EURUSD": {"point": 0.00001, "pip_value_per_lot_usd": 10.0 / 10, "digits": 5},
    "GBPUSD": {"point": 0.00001, "pip_value_per_lot_usd": 10.0 / 10, "digits": 5},
    "AUDUSD": {"point": 0.00001, "pip_value_per_lot_usd": 10.0 / 10, "digits": 5},
    "USDJPY": {"point": 0.0001, "pip_value_per_lot_usd": 10.0 / 10, "digits": 3},
}
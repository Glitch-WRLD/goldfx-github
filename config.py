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
    "USDCAD": "OANDA:USDCAD",
    "NZDUSD": "OANDA:NZDUSD",
    "USDCHF": "OANDA:USDCHF",
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
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "100.0"))
DYNAMIC_BALANCE = os.getenv("DYNAMIC_BALANCE", "1") == "1"   # dynamically read live balance from MT5/broker
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "6.0"))   # % of balance at risk per trade
MAX_RISK_PER_TRADE = float(os.getenv("MAX_RISK_PER_TRADE", "8.0"))  # hard cap
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "12.0"))  # % of balance -> stop trading for the day (2 losses max)
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "2"))
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))   # scaled kelly cap

# ---- Gold Retracement Sniper (M5 Swing Anchor for small accounts) ----
RETRACE_ENABLED = os.getenv("RETRACE_ENABLED", "1") == "1"
RETRACE_MIN_PULLBACK_PCT = float(os.getenv("RETRACE_MIN_PULLBACK_PCT", "0.25"))  # must retrace at least 25% of stop dist
RETRACE_MAX_RISK_USD = float(os.getenv("RETRACE_MAX_RISK_USD", "7.50"))          # max allowed dollar risk on sniper entry
RETRACE_BUFFER_USD = float(os.getenv("RETRACE_BUFFER_USD", "0.50"))              # buffer below swing low / above swing high
RETRACE_MAX_WAIT_HOURS = float(os.getenv("RETRACE_MAX_WAIT_HOURS", "6.0"))       # expire if no entry after 6h
RETRACE_INVAL_TP_PCT = float(os.getenv("RETRACE_INVAL_TP_PCT", "0.75"))          # cancel if 75% of TP reached before pullback

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
    "USDCAD": {"point": 0.00001, "pip_value_per_lot_usd": 7.4 / 10, "digits": 5},
    "NZDUSD": {"point": 0.00001, "pip_value_per_lot_usd": 10.0 / 10, "digits": 5},
    "USDCHF": {"point": 0.00001, "pip_value_per_lot_usd": 11.0 / 10, "digits": 5},
}

# ---- Auto-trading agent (Windows MT5 / OANDA / paper) ----
AUTO_TRADE_ENV = os.getenv("AUTO_TRADE_ENV", "paper")      # paper | mt5 | demo | live
ALLOW_LIVE     = os.getenv("ALLOW_LIVE", "") == "1"         # must be explicit
# MT5 broker (drives a MetaTrader5 terminal; Windows)
MT5_LOGIN      = os.getenv("MT5_LOGIN", "")
MT5_PASSWORD   = os.getenv("MT5_PASSWORD", "")
MT5_SERVER     = os.getenv("MT5_SERVER", "")
MT5_PATH       = os.getenv("MT5_PATH", "")                  # terminal exe path (optional)
MT5_DEVIATION  = int(os.getenv("MT5_DEVIATION", "20"))      # points of max slippage
MT5_MAGIC      = int(os.getenv("MT5_MAGIC", "7710"))        # EA identifier
# OANDA broker (used only when AUTO_TRADE_ENV in demo|live)
OANDA_TOKEN    = os.getenv("OANDA_TOKEN", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
AGENT_POLL_SEC = int(os.getenv("AGENT_POLL_SEC", "20"))     # state.json poll cadence
AGENT_STATE_URL = os.getenv(
    "AGENT_STATE_URL",
    "https://raw.githubusercontent.com/Glitch-WRLD/goldfx-github/main/gha_state/state.json",
)

# ---- Portfolio Exposure & Risk Budgeting ----
MAX_CONCURRENT_TRADES = int(os.getenv("MAX_CONCURRENT_TRADES", "5"))         # max concurrent open trades across all pairs
MAX_TRADES_PER_SYMBOL = int(os.getenv("MAX_TRADES_PER_SYMBOL", "2"))         # max concurrent trades on single symbol
MAX_PORTFOLIO_RISK_PCT = float(os.getenv("MAX_PORTFOLIO_RISK_PCT", "30.0"))  # max cumulative unprotected risk % (user approved 30%)
LOCAL_TP_GUARD_ENABLED = os.getenv("LOCAL_TP_GUARD_ENABLED", "1") == "1"     # close immediately if chart price touches TP
MAX_SPREAD_PIPS = float(os.getenv("MAX_SPREAD_PIPS", "2.5"))                 # max FX spread (pips) for market entry
MAX_SPREAD_GOLD = float(os.getenv("MAX_SPREAD_GOLD", "1.50"))          # max XAUUSD spread ($) for market entry
ROLLOVER_START_UTC = os.getenv("ROLLOVER_START_UTC", "20:55")          # rollover blackout start time (HH:MM UTC)
ROLLOVER_END_UTC = os.getenv("ROLLOVER_END_UTC", "22:15")              # rollover blackout end time (HH:MM UTC)
ROLLOVER_MIN_MARGIN_LEVEL_PCT = float(os.getenv("ROLLOVER_MIN_MARGIN_LEVEL_PCT", "250.0"))  # stress test margin %
CLOSE_IN_PROFIT_BEFORE_ROLLOVER = os.getenv("CLOSE_IN_PROFIT_BEFORE_ROLLOVER", "1") == "1"   # close profitable trades before rollover

# ---- Smart Reversal Early Exit (Asset-Specific: XAUUSD only) ----
GOLD_REVERSAL_EXIT_ENABLED = os.getenv("GOLD_REVERSAL_EXIT_ENABLED", "1") == "1"
GOLD_REVERSAL_MIN_MFE_R    = float(os.getenv("GOLD_REVERSAL_MIN_MFE_R", "0.5"))
GOLD_REVERSAL_TF           = os.getenv("GOLD_REVERSAL_TF", "M15")
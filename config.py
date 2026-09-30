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
    "NASDAQ-100": "PEPPERSTONE:NAS100",
    "US500": "CAPITALCOM:US500",
    "DJ30": "CAPITALCOM:US30",
    "DXY": "TVC:DXY",
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
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "18.0"))  # % of balance -> stop trading for the day (3 losses max)
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))   # scaled kelly cap

# ---- Gold Retracement Sniper (M5 Swing Anchor for small accounts) ----
RETRACE_ENABLED = os.getenv("RETRACE_ENABLED", "1") == "1"
RETRACE_MIN_PULLBACK_PCT = float(os.getenv("RETRACE_MIN_PULLBACK_PCT", "0.25"))  # must retrace at least 25% of stop dist
RETRACE_MAX_RISK_USD = float(os.getenv("RETRACE_MAX_RISK_USD", "7.50"))          # max allowed dollar risk on sniper entry
RETRACE_BUFFER_USD = float(os.getenv("RETRACE_BUFFER_USD", "0.50"))              # buffer below swing low / above swing high
RETRACE_MAX_WAIT_HOURS = float(os.getenv("RETRACE_MAX_WAIT_HOURS", "6.0"))       # expire if no entry after 6h
RETRACE_INVAL_TP_PCT = float(os.getenv("RETRACE_INVAL_TP_PCT", "0.75"))          # cancel if 75% of TP reached before pullback

# ---- Small-Account & Gold Quarantine Guards ($10 - $100 Accounts) ----
GOLD_QUARANTINE_ENABLED = os.getenv("GOLD_QUARANTINE_ENABLED", "1") == "1"
MIN_GOLD_BALANCE = float(os.getenv("MIN_GOLD_BALANCE", "100.0"))        # Below $100: Gold must use tight sniper; below $50: Gold quarantined
MIN_GOLD_ABSOLUTE_BALANCE = float(os.getenv("MIN_GOLD_ABSOLUTE_BALANCE", "50.0")) # Complete quarantine for Gold below $50 (margin risk)
IS_CENT_ACCOUNT = os.getenv("IS_CENT_ACCOUNT", "0") == "1"              # Set 1 if using a Cent/Micro account (bypasses quarantine)
MAX_CHASE_TP_PCT = float(os.getenv("MAX_CHASE_TP_PCT", "0.25"))         # Never chase if price already ran >25% towards TP
MAX_ADVERSE_DRIFT_PCT = float(os.getenv("MAX_ADVERSE_DRIFT_PCT", "0.35"))# Never chase if price ran >35% adverse towards SL

# ---- Daily Broker Rollover Protection (Headway 23:45 - 00:25 UTC) ----
ROLLOVER_START_UTC = os.getenv("ROLLOVER_START_UTC", "23:45")             # Pre-rollover start (when spreads widen)
ROLLOVER_END_UTC = os.getenv("ROLLOVER_END_UTC", "00:25")                 # Post-rollover end (when liquidity normalizes)
CLOSE_IN_PROFIT_BEFORE_ROLLOVER = os.getenv("CLOSE_IN_PROFIT_BEFORE_ROLLOVER", "1") == "1" # Lock profit before spread blowout
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
    "NASDAQ-100": {"point": 0.01, "pip_value_per_lot_usd": 0.10, "digits": 2},
    "US500": {"point": 0.01, "pip_value_per_lot_usd": 0.10, "digits": 2},
    "DJ30": {"point": 0.01, "pip_value_per_lot_usd": 0.10, "digits": 2},
    "DXY": {"point": 0.001, "pip_value_per_lot_usd": 1.0, "digits": 3},
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
MAX_CONCURRENT_TRADES = int(os.getenv("MAX_CONCURRENT_TRADES", "14"))         # max total open trades across all pairs & indices
MAX_AT_RISK_TRADES    = int(os.getenv("MAX_AT_RISK_TRADES", "8"))             # max trades with capital actively at risk (< BE)
MAX_TRADES_PER_SYMBOL = int(os.getenv("MAX_TRADES_PER_SYMBOL", "2"))         # max concurrent trades on single symbol
MAX_PORTFOLIO_RISK_PCT = float(os.getenv("MAX_PORTFOLIO_RISK_PCT", "35.0"))  # max cumulative unprotected risk % (user approved 35%)
LOCAL_TP_GUARD_ENABLED = os.getenv("LOCAL_TP_GUARD_ENABLED", "1") == "1"     # close immediately if chart price touches TP
MAX_SPREAD_PIPS = float(os.getenv("MAX_SPREAD_PIPS", "2.5"))                 # max FX spread (pips) for market entry
MAX_SPREAD_GOLD = float(os.getenv("MAX_SPREAD_GOLD", "1.50"))          # max XAUUSD spread ($) for market entry
ROLLOVER_MIN_MARGIN_LEVEL_PCT = float(os.getenv("ROLLOVER_MIN_MARGIN_LEVEL_PCT", "250.0"))  # stress test margin %

# ---- Account-Size Tiered Trade Capacity ----
def dynamic_portfolio_capacity(balance: float, is_cent: bool = False) -> tuple[int, int, int, float, str]:
    """Return (max_total_trades, max_at_risk_trades, max_trades_per_sym, max_portfolio_risk_pct, tier_name):
    - Cent Accounts: Run on Tier 3 High-Capacity (14 Total Max, 8 At-Risk Max, 2/Pair, 35% max open risk)
    - Standard Accounts < $50:  4 Total Max, 3 At-Risk Max, 1/Pair, 20% max open risk (Tier 1: Small Balance)
    - Standard Accounts $50-$500: 6 Total Max, 4 At-Risk Max, 1/Pair, 25% max open risk (Tier 2: Mid Balance)
    - Standard Accounts $500+:  14 Total Max, 8 At-Risk Max, 2/Pair, 35% max open risk (Tier 3: Institutional Free)
    """
    if is_cent:
        return 14, 8, 2, 35.0, f"Cent Account Tier 3 High-Capacity (Micro-Scale: {balance:.0f} USC · 14 Total / 8 At-Risk Max · 2/Pair)"

    if balance < 50.0:
        return 4, 3, 1, 20.0, f"Tier 1 (<$50 Standard · ${balance:.2f} · 4 Total / 3 At-Risk Max · 1/Pair)"
    elif balance < 500.0:
        return 6, 4, 1, 25.0, f"Tier 2 ($50-$500 Standard · ${balance:.2f} · 6 Total / 4 At-Risk Max · 1/Pair)"
    else:
        return 14, 8, 2, 35.0, f"Tier 3 ($500+ Standard · ${balance:.2f} · 14 Total / 8 At-Risk Max · 2/Pair)"

# ---- Smart Reversal Early Exit (Asset-Specific: XAUUSD only) ----
GOLD_REVERSAL_EXIT_ENABLED = os.getenv("GOLD_REVERSAL_EXIT_ENABLED", "1") == "1"
GOLD_REVERSAL_MIN_MFE_R    = float(os.getenv("GOLD_REVERSAL_MIN_MFE_R", "0.5"))
GOLD_REVERSAL_TF           = os.getenv("GOLD_REVERSAL_TF", "M15")

# ---- Trade Protection & Conservative Trailing (Option B) ----
BREAKEVEN_MFE_R            = float(os.getenv("BREAKEVEN_MFE_R", "0.8"))      # Stage 1: Move SL to BE (+1 pip buffer) at >= 0.8R
TRAIL_STAGE1_MFE_R         = float(os.getenv("TRAIL_STAGE1_MFE_R", "1.3"))   # Stage 2: Trail SL to +0.5R profit at >= 1.3R
TRAIL_STAGE1_LOCK_R        = float(os.getenv("TRAIL_STAGE1_LOCK_R", "0.5"))  # Locked R for Stage 2 (+0.5R)
TRAIL_STAGE2_MFE_R         = float(os.getenv("TRAIL_STAGE2_MFE_R", "1.6"))   # Stage 3: Trail SL to +1.0R profit at >= 1.6R
TRAIL_STAGE2_LOCK_R        = float(os.getenv("TRAIL_STAGE2_LOCK_R", "1.0"))  # Locked R for Stage 3 (+1.0R)

# ---- DXY Macro Momentum Filter (Forex Pairs Only) ----
DXY_FILTER_ENABLED         = os.getenv("DXY_FILTER_ENABLED", "1") == "1"     # 1 = Filter pure Forex by US Dollar index momentum
DXY_FILTER_TF              = os.getenv("DXY_FILTER_TF", "M15")               # M15 momentum baseline (+60.3% win rate in empirical audit)
DXY_FILTER_EMA             = int(os.getenv("DXY_FILTER_EMA", "21"))          # EMA 21 trend filter
DXY_EXEMPT_SYMBOLS         = {"XAUUSD", "NASDAQ-100", "US500", "DJ30"}       # Decoupled / Independent assets exempt from DXY filter

# ---- High-Impact Red-Folder News Blackout & Playbook ----
NEWS_BLACKOUT_ENABLED       = os.getenv("NEWS_BLACKOUT_ENABLED", "1") == "1"     # 1 = Pause new entries during Tier-1 news
NEWS_BUFFER_BEFORE_MIN      = int(os.getenv("NEWS_BUFFER_BEFORE_MIN", "15"))     # Mins before event to pause fills
NEWS_BUFFER_AFTER_MIN       = int(os.getenv("NEWS_BUFFER_AFTER_MIN", "15"))      # Mins after event to resume fills
NEWS_PLAYBOOK_ENABLED       = os.getenv("NEWS_PLAYBOOK_ENABLED", "1") == "1"     # 1 = Send 30-min tactical playbook to Telegram
NEWS_PLAYBOOK_AHEAD_MIN     = int(os.getenv("NEWS_PLAYBOOK_AHEAD_MIN", "30"))    # Mins ahead to send briefing
NEWS_PROTECT_PROFITS        = os.getenv("NEWS_PROTECT_PROFITS", "1") == "1"      # 1 = Move SL to BE (+1 pip) on profitable trades before news

# ---- US Open Index Opening Bell Slippage Buffer (13:25 - 13:45 UTC) ----
US_OPEN_BUFFER_ENABLED      = os.getenv("US_OPEN_BUFFER_ENABLED", "1") == "1"    # 1 = Pause new index fills at NYSE open
US_OPEN_START_UTC           = os.getenv("US_OPEN_START_UTC", "13:25")            # 5 mins before 13:30 opening bell
US_OPEN_END_UTC             = os.getenv("US_OPEN_END_UTC", "13:45")              # 15 mins after opening bell (first M15 candle closes)
INDEX_SYMBOLS               = {"NASDAQ-100", "US500", "DJ30"}                    # Assets subject to US Open cooldown
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
    "GBPAUD": "OANDA:GBPAUD",
    "DXY": "TVC:DXY",
}
# map brand names -> tradingview-sdk Interval
TIMEFRAMES = ["M5", "M15", "M30", "H1", "H2", "H4"]

DATA_CACHE = BASE_DIR / "data_cache"
REPORTS = BASE_DIR / "reports"
for d in (DATA_CACHE, REPORTS):
    d.mkdir(exist_ok=True)

# ---- Telegram ----
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
AGENT_BOT_TOKEN = os.getenv("AGENT_BOT_TOKEN", BOT_TOKEN)
AGENT_CHAT_ID = os.getenv("AGENT_CHAT_ID", CHAT_ID)

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
RETRACE_INVAL_TP_PCT = float(os.getenv("RETRACE_INVAL_TP_PCT", "0.90"))          # cancel if 90% of TP reached before pullback

# ---- Momentum Continuation Fill (Prevents Missed Runaways) ----
MOMENTUM_FILL_ENABLED       = os.getenv("MOMENTUM_FILL_ENABLED", "1") == "1"      # 1 = Fill runaway setups if live RR is favorable
MOMENTUM_FILL_MAX_TP_PCT    = float(os.getenv("MOMENTUM_FILL_MAX_TP_PCT", "0.55"))# Allow momentum entry up to 55% of move to TP
MOMENTUM_FILL_MIN_RR        = float(os.getenv("MOMENTUM_FILL_MIN_RR", "0.90"))   # Require at least 0.90 live RR from current market price

# ---- Small-Account & Gold Quarantine Guards ($10 - $100 Accounts) ----
GOLD_QUARANTINE_ENABLED = os.getenv("GOLD_QUARANTINE_ENABLED", "1") == "1"
MIN_GOLD_BALANCE = float(os.getenv("MIN_GOLD_BALANCE", "100.0"))        # Below $100: Gold must use tight sniper; below $50: Gold quarantined
MIN_GOLD_ABSOLUTE_BALANCE = float(os.getenv("MIN_GOLD_ABSOLUTE_BALANCE", "50.0")) # Complete quarantine for Gold below $50 (margin risk)
IS_CENT_ACCOUNT = os.getenv("IS_CENT_ACCOUNT", "0") == "1"              # Set 1 if using a Cent/Micro account (bypasses quarantine)
MAX_CHASE_TP_PCT = float(os.getenv("MAX_CHASE_TP_PCT", "0.35"))         # Allow market entry up to 35% of move to TP (retains >1.3 RR)
MAX_ADVERSE_DRIFT_PCT = float(os.getenv("MAX_ADVERSE_DRIFT_PCT", "0.50"))# Allow entry unless price drifted >50% adverse towards SL

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
    "USDJPY": {"point": 0.001, "pip_value_per_lot_usd": 6.32 / 10, "digits": 3},
    "USDCAD": {"point": 0.00001, "pip_value_per_lot_usd": 7.4 / 10, "digits": 5},
    "NZDUSD": {"point": 0.00001, "pip_value_per_lot_usd": 10.0 / 10, "digits": 5},
    "USDCHF": {"point": 0.00001, "pip_value_per_lot_usd": 11.0 / 10, "digits": 5},
    "NASDAQ-100": {"point": 0.01, "pip_value_per_lot_usd": 0.10, "digits": 2},
    "US500": {"point": 0.01, "pip_value_per_lot_usd": 0.10, "digits": 2},
    "DJ30": {"point": 0.01, "pip_value_per_lot_usd": 0.10, "digits": 2},
    "GBPAUD": {"point": 0.00001, "pip_value_per_lot_usd": 6.94 / 10, "digits": 5},
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
MAX_CONCURRENT_TRADES   = int(os.getenv("MAX_CONCURRENT_TRADES", "20"))         # max total open trades across all pairs & indices
MAX_AT_RISK_TRADES      = int(os.getenv("MAX_AT_RISK_TRADES", "12"))            # max trades with capital actively at risk (< BE)
MAX_TRADES_PER_SYMBOL   = int(os.getenv("MAX_TRADES_PER_SYMBOL", "3"))          # max concurrent at-risk trades on single symbol
MAX_AT_RISK_PER_SYMBOL  = int(os.getenv("MAX_AT_RISK_PER_SYMBOL", "3"))        # max 3 unhedged trades per symbol (< BE) as designed
MAX_TOTAL_PER_SYMBOL    = int(os.getenv("MAX_TOTAL_PER_SYMBOL", "5"))          # max total trades on single symbol (allows runners if prior are at BE)
MAX_PORTFOLIO_RISK_PCT  = float(os.getenv("MAX_PORTFOLIO_RISK_PCT", "65.0"))   # max cumulative unprotected risk % (accommodates high-capacity)
LOCAL_TP_GUARD_ENABLED = os.getenv("LOCAL_TP_GUARD_ENABLED", "1") == "1"     # close immediately if chart price touches TP
MAX_SPREAD_PIPS = float(os.getenv("MAX_SPREAD_PIPS", "2.5"))                 # max FX spread (pips) for market entry
MAX_SPREAD_GOLD = float(os.getenv("MAX_SPREAD_GOLD", "1.50"))          # max XAUUSD spread ($) for market entry
MAX_SPREAD_INDEX = float(os.getenv("MAX_SPREAD_INDEX", "8.0"))          # max Index spread (pts) for market entry (NAS100 ~3.2, US500 ~2.1, DJ30 ~5.0)
ROLLOVER_MIN_MARGIN_LEVEL_PCT = float(os.getenv("ROLLOVER_MIN_MARGIN_LEVEL_PCT", "250.0"))  # stress test margin %

# ---- Account-Size Tiered Trade Capacity ----
def dynamic_portfolio_capacity(balance: float, is_cent: bool = False) -> tuple[int, int, int, float, str]:
    """Return (max_total_trades, max_at_risk_trades, max_trades_per_sym, max_portfolio_risk_pct, tier_name):
    - Cent Accounts: Run on Tier 3 High-Capacity (20 Total Max, 12 At-Risk Max, 3/Pair At-Risk, 5/Pair Total, 65% max open risk)
    - Standard Accounts < $50:  6 Total Max, 4 At-Risk Max, 1/Pair At-Risk, 2/Pair Total, 35% max open risk (Tier 1: Small Balance)
    - Standard Accounts $50-$500: 10 Total Max, 6 At-Risk Max, 2/Pair At-Risk, 3/Pair Total, 50% max open risk (Tier 2: Mid Balance)
    - Standard Accounts $500+:  20 Total Max, 12 At-Risk Max, 3/Pair At-Risk, 5/Pair Total, 65% max open risk (Tier 3: High Capacity)
    """
    if is_cent:
        return 20, 12, 3, 65.0, f"Cent Account Tier 3 High-Capacity (Micro-Scale: {balance:.0f} USC · 20 Total / 12 At-Risk Max · 3/Pair At-Risk · 5/Pair Total)"

    if balance < 50.0:
        return 6, 4, 1, 35.0, f"Tier 1 (<$50 Standard · ${balance:.2f} · 6 Total / 4 At-Risk Max · 1/Pair)"
    elif balance < 500.0:
        return 10, 6, 2, 50.0, f"Tier 2 ($50-$500 Standard · ${balance:.2f} · 10 Total / 6 At-Risk Max · 2/Pair)"
    else:
        return 20, 12, 3, 65.0, f"Tier 3 ($500+ Standard · ${balance:.2f} · 20 Total / 12 At-Risk Max · 3/Pair At-Risk · 5/Pair Total)"

# ---- Smart Reversal Early Exit (Disabled: Let Winners Run to Full TP) ----
GOLD_REVERSAL_EXIT_ENABLED = os.getenv("GOLD_REVERSAL_EXIT_ENABLED", "0") == "1"
GOLD_REVERSAL_MIN_MFE_R    = float(os.getenv("GOLD_REVERSAL_MIN_MFE_R", "0.5"))
GOLD_REVERSAL_TF           = os.getenv("GOLD_REVERSAL_TF", "M15")

# ---- Trade Protection: Maintain 0.8R Breakeven, Disable Trailing Stops (Let Winners Run to 100% TP) ----
BREAKEVEN_ENABLED          = os.getenv("BREAKEVEN_ENABLED", "1") == "1"       # Stage 1: Move SL to BE (+1 pip) when price reaches >= 50% TP / 0.8R
BREAKEVEN_PCT_TP           = float(os.getenv("BREAKEVEN_PCT_TP", "50.0"))     # Stage 1: Move SL to BE (+1 pip) when price reaches >= 50% of TP
BREAKEVEN_PCT_TP_GBPAUD    = float(os.getenv("BREAKEVEN_PCT_TP_GBPAUD", "70.0")) # Stage 1 GBPAUD: Wider BE buffer (70% TP) for deep SMC inducement retests
BREAKEVEN_MFE_R            = float(os.getenv("BREAKEVEN_MFE_R", "0.8"))      # Stage 1: Move SL to BE (+1 pip buffer) at >= 0.8R
TRAIL_ENABLED              = os.getenv("TRAIL_ENABLED", "0") == "1"          # Disabled: Trailing stops cut winners at 70-80% TP; disabled to allow full 100% TP wins
TRAIL_MODE                 = os.getenv("TRAIL_MODE", "NONE")                 # "NONE" = pure BE + full TP targets (1.3R to 2.5R)
TRAIL_STAGE1_PCT_TP        = float(os.getenv("TRAIL_STAGE1_PCT_TP", "85.0"))
TRAIL_STAGE1_LOCK_PCT      = float(os.getenv("TRAIL_STAGE1_LOCK_PCT", "70.0"))
TRAIL_STAGE2_PCT_TP        = float(os.getenv("TRAIL_STAGE2_PCT_TP", "92.0"))
TRAIL_STAGE2_LOCK_PCT      = float(os.getenv("TRAIL_STAGE2_LOCK_PCT", "80.0"))

# ---- Signal Freshness & Slippage Armor (Prevents Late/Chased Entries) ----
MAX_SIGNAL_AGE_MIN         = float(os.getenv("MAX_SIGNAL_AGE_MIN", "45.0"))  # Abort fills if setup is older than 45 mins
MAX_ENTRY_SLIPPAGE_PIPS    = float(os.getenv("MAX_ENTRY_SLIPPAGE_PIPS", "4.0")) # Abort fills if FX drifted > 4.0 pips from delivered entry
MAX_ENTRY_SLIPPAGE_GOLD    = float(os.getenv("MAX_ENTRY_SLIPPAGE_GOLD", "0.80"))# Abort fills if Gold drifted > $0.80 from delivered entry
LOCAL_SCANNER_ENABLED      = os.getenv("LOCAL_SCANNER_ENABLED", "1") == "1"   # 1 = Run real-time MT5 scanner on local machine (0ms latency)

# ---- DXY Macro Momentum Filter (Forex Pairs Only) ----
DXY_FILTER_ENABLED         = os.getenv("DXY_FILTER_ENABLED", "1") == "1"     # 1 = Filter pure Forex by US Dollar index momentum
DXY_FILTER_TF              = os.getenv("DXY_FILTER_TF", "M15")               # M15 momentum baseline (+60.3% win rate in empirical audit)
DXY_FILTER_EMA             = int(os.getenv("DXY_FILTER_EMA", "21"))          # EMA 21 trend filter
DXY_EXEMPT_SYMBOLS         = {"XAUUSD", "NASDAQ-100", "US500", "DJ30", "GBPAUD"} # Decoupled / Non-USD cross assets exempt from DXY filter

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

# ---- London Open Opening Bell Volatility Buffer (06:50 - 07:20 UTC) ----
LONDON_OPEN_BUFFER_ENABLED  = os.getenv("LONDON_OPEN_BUFFER_ENABLED", "1") == "1" # 1 = Pause new fills during London cash open
LONDON_OPEN_START_UTC       = os.getenv("LONDON_OPEN_START_UTC", "06:50")         # 10 mins before 07:00 London open
LONDON_OPEN_END_UTC         = os.getenv("LONDON_OPEN_END_UTC", "07:20")           # 20 mins after London open (clears Asian range purge)
LONDON_OPEN_SYMBOLS         = {"XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "NZDUSD", "USDCHF", "GBPAUD"}

# ---- Phase 1: Institutional Defense & Signal Quality ----
# Rule 1: Higher Timeframe (HTF) Dual-Trend Alignment (Prunes 10.6% WR counter-trend setups)
HTF_FILTER_ENABLED          = os.getenv("HTF_FILTER_ENABLED", "1") == "1"         # 1 = Filter setups fighting both H1 and H4 50 EMAs
HTF_FILTER_EMA_LEN          = int(os.getenv("HTF_FILTER_EMA_LEN", "50"))          # EMA 50 trend baseline
# Rule 2: Late NY / Rollover Session Exhaustion Guard (Prunes 36.2% WR evening drag, 17:00 - 24:00 UTC)
# Asian Session (00:00 - 06:50 UTC) and London/NY Overlap remain 100% ACTIVE!
SESSION_EVENING_FILTER_ENABLED = os.getenv("SESSION_EVENING_FILTER_ENABLED", "1") == "1" # 1 = Pause brand new fills after 17:00 UTC
SESSION_EVENING_START_UTC      = int(os.getenv("SESSION_EVENING_START_UTC", "17")) # 17:00 UTC (European close / late US chop)
SESSION_EVENING_END_UTC        = int(os.getenv("SESSION_EVENING_END_UTC", "24"))   # 24:00 UTC

# ---- Phase 2: Turtle Soup / Inducement Sweep Re-Entry Engine ----
# Automatically detects when pro-trend setups are stopped by shallow liquidity sweeps (<= 0.6R)
# and triggers an instant high-RR re-entry when price rejects back into market structure!
TURTLE_SOUP_REENTRY_ENABLED = os.getenv("TURTLE_SOUP_REENTRY_ENABLED", "1") == "1" # 1 = Enable Turtle Soup re-entries
TURTLE_SOUP_MAX_OVERSHOOT_R = float(os.getenv("TURTLE_SOUP_MAX_OVERSHOOT_R", "0.6")) # Max sweep overshoot (0.6R) beyond SL
TURTLE_SOUP_MAX_WINDOW_MIN  = int(os.getenv("TURTLE_SOUP_MAX_WINDOW_MIN", "45"))    # Active monitoring window (45 mins)
TURTLE_SOUP_SL_BUFFER_PIPS  = float(os.getenv("TURTLE_SOUP_SL_BUFFER_PIPS", "1.5")) # Pips behind sweep wick for tight stop

# ---- Strategy 3: Institutional Session Delivery Engine (ISDE) ----
# Exploits high-conviction temporal liquidity windows (London Open 07:00-09:00 & NY Silver Bullet 14:00-15:00)
# Tested across 14.3 weeks: 62.2% WR, 3.30 Profit Factor, +11.41 R/wk (+163.0R net profit).
ISDE_ENABLED          = os.getenv("ISDE_ENABLED", "1") == "1"              # 1 = Enable precision session delivery engine
ISDE_TARGET_RR        = float(os.getenv("ISDE_TARGET_RR", "2.0"))          # Fixed 1:2.0 Risk-to-Reward
ISDE_MIN_GAP_FX       = float(os.getenv("ISDE_MIN_GAP_FX", "1.5"))        # Min FVG gap (pips) for FX
ISDE_MIN_GAP_GOLD     = float(os.getenv("ISDE_MIN_GAP_GOLD", "50.0"))     # Min FVG gap ($0.50) for Gold
ISDE_MIN_GAP_INDEX    = float(os.getenv("ISDE_MIN_GAP_INDEX", "2.0"))      # Min FVG gap (2.0 pts) for Indices
ISDE_WINDOWS = {
    "XAUUSD": {"start_utc": 7, "end_utc": 9, "bias_htf": "H1", "min_gap": 50.0},
    "USDJPY": {"start_utc": 7, "end_utc": 9, "bias_htf": "H1", "min_gap": 1.5},
    "DJ30":   {"start_utc": 14, "end_utc": 15, "bias_htf": "H1", "min_gap": 2.0},
    "EURUSD": {"start_utc": 14, "end_utc": 15, "bias_htf": "H1", "min_gap": 1.5},
    "GBPUSD": {"start_utc": 14, "end_utc": 15, "bias_htf": "H1", "min_gap": 1.5},
}
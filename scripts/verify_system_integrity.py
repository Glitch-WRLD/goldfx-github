#!/usr/bin/env python3
"""Deliberate Rigorous System Check for GoldFX Agent.
Verifies all configuration parameters, broker connectivity, contract specifications,
stop floors, risk engine calculations, and Turtle Soup execution rules.
"""
import sys
from pathlib import Path
import json
import logging

BASE_DIR = Path("C:/goldfx-agent")
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding="utf-8")

import config
import MetaTrader5 as mt5
from engine.risk import RiskManager, position_size, floor_lots, risk_amount, get_or_update_anchor_balance
from engine.news import fetch_calendar_events, get_active_news_blackout
from agent.ledger import load_ledger

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sys_check")

def run_checks():
    passes = 0
    failures = 0
    results = []

    def check(name: str, condition: bool, detail: str = ""):
        nonlocal passes, failures
        if condition:
            passes += 1
            results.append((name, "PASS", detail))
            print(f"  [PASS] {name}: {detail}")
        else:
            failures += 1
            results.append((name, "FAIL", detail))
            print(f"  [FAIL] {name}: {detail}")

    print("=" * 80)
    print("GOLDFX RIGOROUS SYSTEM INTEGRITY VERIFICATION")
    print("=" * 80)

    # 1. Config Parameter Verification
    print("\n--- 1. CONFIGURATION & HARDENED BLUEPRINT CHECK ---")
    check("TURTLE_SOUP_REENTRY_ENABLED", getattr(config, "TURTLE_SOUP_REENTRY_ENABLED", False) is True, "Must be True")
    check("TURTLE_SOUP_MAX_OVERSHOOT_R", getattr(config, "TURTLE_SOUP_MAX_OVERSHOOT_R", 0.0) == 0.35, "Must be 0.35R")
    check("TURTLE_SOUP_MAX_WINDOW_MIN", getattr(config, "TURTLE_SOUP_MAX_WINDOW_MIN", 0) == 15, "Must be 15 mins")
    check("TURTLE_SOUP_SL_BUFFER_PIPS", getattr(config, "TURTLE_SOUP_SL_BUFFER_PIPS", 0.0) == 3.5, "Must be 3.5 pips")
    check("TURTLE_SOUP_MIN_WICK_RATIO", getattr(config, "TURTLE_SOUP_MIN_WICK_RATIO", 0.0) == 0.50, "Must be 0.50 (50%)")
    check("TURTLE_SOUP_TARGET_MODE", getattr(config, "TURTLE_SOUP_TARGET_MODE", "") == "ORIGINAL_ENTRY", "Must target Breaker Block")
    check("TURTLE_SOUP_NEWS_BLACKOUT", getattr(config, "TURTLE_SOUP_NEWS_BLACKOUT", False) is True, "Must be True")
    check("LOCAL_TP_GUARD_ENABLED", getattr(config, "LOCAL_TP_GUARD_ENABLED", False) is True, "Must be True")
    check("STEP_COMPOUNDING_ENABLED", getattr(config, "STEP_COMPOUNDING_ENABLED", False) is True, "Must be True")
    check("RISK_PER_TRADE", getattr(config, "RISK_PER_TRADE", 0.0) == 3.5, "Must be 3.5% risk per trade")

    # Weakling Session Safeguards Check
    print("\n--- 2. WEAKLING SESSION SAFEGUARDS CHECK ---")
    check("FILTER_THU_ASIAN_ENABLED", getattr(config, "FILTER_THU_ASIAN_ENABLED", False) is True, "Thu 00:00-07:00 UTC dropped")
    check("FILTER_SUN_OPEN_ENABLED", getattr(config, "FILTER_SUN_OPEN_ENABLED", False) is True, "Sun 18:00-24:00 UTC dropped")
    check("FILTER_EVENING_CHOP_ENABLED", getattr(config, "FILTER_EVENING_CHOP_ENABLED", False) is True, "USD evening chop dropped")
    check("FILTER_FRIDAY_MODE", getattr(config, "FILTER_FRIDAY_MODE", "") == "FULL_DAY", "Friday FULL_DAY drop active")
    check("LONDON_OPEN_BUFFER_ENABLED", getattr(config, "LONDON_OPEN_BUFFER_ENABLED", False) is True, "06:50-07:20 UTC paused")
    check("US_OPEN_BUFFER_ENABLED", getattr(config, "US_OPEN_BUFFER_ENABLED", False) is True, "13:25-13:45 UTC paused")
    check("DXY_FILTER_ENABLED", getattr(config, "DXY_FILTER_ENABLED", False) is True, "M15 EMA21 DXY filter active")
    check("NEWS_BLACKOUT_ENABLED", getattr(config, "NEWS_BLACKOUT_ENABLED", False) is True, "Tier-1 news blackout active")

    # Stop Floors Check
    print("\n--- 3. MINIMUM STOP FLOOR SHIELD CHECK ---")
    floors = getattr(config, "TURTLE_SOUP_MIN_STOP_FLOOR", {})
    symbols = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "NZDUSD", "USDCHF", "GBPAUD", "NASDAQ-100", "US500", "DJ30"]
    all_floors_present = all(s in floors for s in symbols)
    check("STOP_FLOORS_POPULATED", all_floors_present, f"All 12 symbols present (found {len(floors)})")
    check("GOLD_FLOOR_VALUE", floors.get("XAUUSD") == 1.50, "$1.50 / 150 pts Gold floor")
    check("EURUSD_FLOOR_VALUE", floors.get("EURUSD") == 0.00050, "5.0 pips EURUSD floor")
    check("US500_FLOOR_VALUE", floors.get("US500") == 4.0, "4.0 pts US500 floor")
    check("NAS100_FLOOR_VALUE", floors.get("NASDAQ-100") == 8.0, "8.0 pts NAS100 floor")
    check("DJ30_FLOOR_VALUE", floors.get("DJ30") == 15.0, "15.0 pts DJ30 floor")

    # Broker MT5 Connection & Account Check
    print("\n--- 4. BROKER & LIVE MT5 CONNECTIVITY CHECK ---")
    mt5_init = mt5.initialize()
    check("MT5_INITIALIZE", mt5_init, f"Connected to MT5 terminal")
    if mt5_init:
        t_info = mt5.terminal_info()
        check("TERMINAL_TRADE_ALLOWED", bool(t_info and t_info.trade_allowed), "Algo trading enabled in terminal")
        a_info = mt5.account_info()
        check("ACCOUNT_TRADE_ALLOWED", bool(a_info and a_info.trade_allowed), f"Account {a_info.login if a_info else 0} trade allowed")
        check("ACCOUNT_SERVER", bool(a_info and a_info.server == "Headway-Real"), f"Connected to Headway-Real (login={a_info.login})")
        print(f"    Current Account Balance: {a_info.balance:.2f} {a_info.currency} (Equity: {a_info.equity:.2f})")

        # Test symbol contract specs
        print("\n--- 5. SYMBOL CONTRACT SPECS & TICK MATH CHECK ---")
        for sym in symbols:
            s_info = mt5.symbol_info(sym)
            if s_info:
                stops_lvl = s_info.trade_stops_level
                min_lot = s_info.volume_min
                tick_sz = s_info.trade_tick_size
                tick_val = s_info.trade_tick_value
                check(f"SYMBOL_{sym}", s_info.visible and stops_lvl == 0,
                      f"stops_level={stops_lvl}, min_lot={min_lot}, tick_sz={tick_sz}, tick_val={tick_val:.4f}")
            else:
                check(f"SYMBOL_{sym}", False, "Failed to query symbol info")

    # Sizing Math & Risk Manager Check
    print("\n--- 6. RISK MANAGER & SIZING COMPLIANCE CHECK ---")
    rm = RiskManager()
    anchor_bal = get_or_update_anchor_balance()
    check("ANCHOR_BALANCE_ACTIVE", anchor_bal > 0, f"Settled weekly anchor balance: ${anchor_bal:.2f}")
    check("RM_RISK_PCT", rm.risk_pct == 3.5, f"Risk per trade: {rm.risk_pct}%")

    # Test exact lot sizing on sample stop distances
    test_sl_pts = {"XAUUSD": 2.50, "EURUSD": 0.0015, "US500": 6.0}
    for sym, dist in test_sl_pts.items():
        usd_risk = anchor_bal * 0.035
        raw_l = position_size(sym, usd_risk, dist)
        fl_l = floor_lots(sym, raw_l)
        actual_risk = risk_amount(sym, fl_l, dist)
        risk_pct_calc = (actual_risk / anchor_bal) * 100.0 if anchor_bal > 0 else 0
        check(f"SIZING_{sym}", abs(risk_pct_calc - 3.5) < 0.6,
              f"Dist={dist} -> Lots={fl_l:.2f} -> Actual Risk=${actual_risk:.2f} ({risk_pct_calc:.2f}%)")

    # Economic Calendar Feed Check
    print("\n--- 7. ECONOMIC CALENDAR NEWS ENGINE CHECK ---")
    try:
        events = fetch_calendar_events()
        check("NEWS_CALENDAR_FEED", len(events) > 0, f"Loaded {len(events)} economic events")
        is_bo, reason, _ = get_active_news_blackout("EURUSD")
        check("NEWS_BLACKOUT_QUERY", isinstance(is_bo, bool), f"Active blackout check operational ({reason})")
    except Exception as e:
        check("NEWS_CALENDAR_FEED", False, f"Calendar fetch failed: {e}")

    # Summary
    print("\n" + "=" * 80)
    print(f"VERIFICATION SUMMARY: {passes} PASSED / {failures} FAILED")
    print("=" * 80)
    return failures == 0

if __name__ == "__main__":
    success = run_checks()
    sys.exit(0 if success else 1)

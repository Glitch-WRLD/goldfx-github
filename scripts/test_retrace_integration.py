import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from agent.ledger import Ledger
from agent.oanda import PaperBroker
import agent.watcher as watcher

def run_tests():
    print("============================================================")
    print("RUNNING GOLD RETRACEMENT SNIPER INTEGRATION TESTS")
    print("============================================================")

    # 1. Clean test environment
    test_dir = Path("data")
    test_dir.mkdir(exist_ok=True)
    test_pending = test_dir / "test_pending_retrace.json"
    test_ledger_path = test_dir / "test_ledger.json"
    
    # Temporarily redirect paths
    orig_pending_path = watcher.PENDING_RETRACE_PATH
    watcher.PENDING_RETRACE_PATH = test_pending
    
    if test_pending.exists():
        test_pending.unlink()
    if test_ledger_path.exists():
        test_ledger_path.unlink()
        
    ledger = Ledger(test_ledger_path)
    
    # Test A: Helper functions
    assert watcher._load_pending_retrace() == {}
    watcher._save_pending_retrace({"test": 123})
    assert watcher._load_pending_retrace() == {"test": 123}
    test_pending.unlink()
    print("Test A Passed: _load_pending_retrace and _save_pending_retrace verified.")

    # Test B: Enqueueing wide Gold setup
    xau_entry = {
        "ref": 9901,
        "symbol": "XAUUSD",
        "dir": "LONG",
        "entry": 4390.0,
        "sl": 4370.0, # $20 stop -> 0.01 lot would risk $20 (20% on $100) -> lots < 0.01
        "tp": 4410.0,
        "rr": 1.0,
        "ts": "2026-09-20T20:00:00Z",
        "profile": "balanced"
    }
    
    # Check lot size calculation
    rm = watcher.risk()
    risk_usd = rm.balance * rm.risk_pct / 100.0 # $6.00
    sl_dist = abs(xau_entry["entry"] - xau_entry["sl"]) # $20.00
    lots_raw = watcher.position_size("XAUUSD", risk_usd, sl_dist) # 0.003
    lots = watcher.floor_lots("XAUUSD", lots_raw) # 0.0
    assert lots < 0.01
    print(f"Test B.1 Passed: Gold setup with $20 stop calculates lots={lots} < 0.01 (budget ${risk_usd:.2f}).")
    
    # Mock enqueuing
    tp_75 = xau_entry["entry"] + (xau_entry["tp"] - xau_entry["entry"]) * config.RETRACE_INVAL_TP_PCT
    pb_thresh = xau_entry["entry"] - 1 * (sl_dist * config.RETRACE_MIN_PULLBACK_PCT) # 4390 - 5 = 4385
    
    item = {
        "ref": 9901,
        "symbol": "XAUUSD",
        "direction": 1,
        "entry_delivered": 4390.0,
        "sl_orig": 4370.0,
        "tp": 4410.0,
        "orig_risk": 20.0,
        "orig_rr": 1.0,
        "signal_ts": xau_entry["ts"],
        "first_seen_ts": time.time(),
        "tp_75": tp_75, # 4405.0
        "pullback_min_dist": pb_thresh, # 4385.0
        "had_pullback": False,
        "profile": "balanced",
        "label": "goldfx #9901",
    }
    watcher._save_pending_retrace({"9901": item})
    assert "9901" in watcher._load_pending_retrace()
    print("Test B.2 Passed: Gold setup enqueued into pending_retrace.")

    # Test C: Invalidation when 75% TP reached
    # Create mock executor that reports price = 4406 (above tp_75)
    class MockExTP:
        def current_price(self, symbol):
            return 4406.0
            
    old_ex = watcher._ex
    watcher._ex = MockExTP()
    
    filled = watcher.process_pending_retracements(ledger)
    assert filled == 0
    pending_now = watcher._load_pending_retrace()
    assert "9901" not in pending_now
    assert ledger.data["9901"]["status"] == "skipped"
    assert ledger.data["9901"]["classification"] == "retrace_inval_tp75"
    print("Test C Passed: Invalidation on 75% TP target touch verified.")

    # Test D: Invalidation when original SL breached before entry
    ledger.data.clear()
    item["first_seen_ts"] = time.time()
    watcher._save_pending_retrace({"9902": dict(item, ref=9902)})
    
    class MockExSL:
        def current_price(self, symbol):
            return 4369.0 # below sl_orig (4370.0)
            
    watcher._ex = MockExSL()
    filled = watcher.process_pending_retracements(ledger)
    assert filled == 0
    assert "9902" not in watcher._load_pending_retrace()
    assert ledger.data["9902"]["status"] == "skipped"
    assert ledger.data["9902"]["classification"] == "retrace_avoided_sl"
    print("Test D Passed: Original SL breach avoided with $0 loss.")

    # Test E: Successful Pullback + Local M5 Reversal Entry
    ledger.data.clear()
    item["first_seen_ts"] = time.time()
    item["ref"] = 9903
    watcher._save_pending_retrace({"9903": item})
    
    # Mock executor that provides pullback price and M5 candles
    class MockExReversal:
        def __init__(self):
            self.orders = []
        def current_price(self, symbol):
            return 4383.0 # pulled back below 4385 threshold
        def get_candles(self, symbol, tf="M5", count=4):
            # Candle -2: red candle, high=4383.0, low=4380.0, close=4380.5
            # Candle -1: green reversal candle, open=4380.5, low=4380.0, close=4384.0, high=4384.5
            # Local swing low = min(4380, 4380) = 4380.0
            # candidate_sl = 4380.0 - 0.50 = 4379.50
            # candidate_risk = 4384.0 - 4379.50 = $4.50 (well below $7.50 cap!)
            return [
                {"time": 100, "open": 4385.0, "high": 4386.0, "low": 4382.0, "close": 4383.0},
                {"time": 200, "open": 4383.0, "high": 4383.0, "low": 4380.0, "close": 4380.5},
                {"time": 300, "open": 4380.5, "high": 4384.5, "low": 4380.0, "close": 4384.0},
            ]
        def place_market_order(self, symbol, direction, lots, entry, sl, tp, label):
            self.orders.append({"symbol": symbol, "dir": direction, "lots": lots, "entry": entry, "sl": sl, "tp": tp})
            from agent.oanda import Fill
            return Fill(ok=True, symbol=symbol, direction=direction, lots=lots, fill_price=entry,
                        order_id="SNIPER-MOCK-1", broker="paper", ts="2026-09-20T21:00:00Z")

    mock_rev = MockExReversal()
    watcher._ex = mock_rev
    
    filled = watcher.process_pending_retracements(ledger)
    assert filled == 1
    assert "9903" not in watcher._load_pending_retrace()
    assert len(mock_rev.orders) == 1
    ord_ = mock_rev.orders[0]
    assert ord_["lots"] == 0.01
    assert ord_["entry"] == 4384.0
    assert ord_["sl"] == 4379.50
    assert ord_["tp"] == 4410.0
    
    # Check ledger record
    rec = ledger.data["9903"]
    assert rec["status"] == "open"
    assert rec["sniper_entry"] is True
    assert rec["risk_usd"] == 4.50
    assert rec["rr"] == round((4410.0 - 4384.0) / 4.50, 2) # 26 / 4.5 = 5.78 R:R!
    print(f"Test E Passed: M5 Swing Sniper filled! Risk=${rec['risk_usd']:.2f}, R:R=1:{rec['rr']:.2f}, SL={rec['sl']:.2f} (orig was {rec['orig_sl']:.2f}).")

    # Clean up test files
    watcher._ex = old_ex
    watcher.PENDING_RETRACE_PATH = orig_pending_path
    if test_pending.exists():
        test_pending.unlink()
    if test_ledger_path.exists():
        test_ledger_path.unlink()
        
    print("============================================================")
    print("ALL INTEGRATION TESTS PASSED (5/5)!")
    print("============================================================")

if __name__ == "__main__":
    run_tests()

import sys
from pathlib import Path
import MetaTrader5 as mt5

print("Step 1: mt5.initialize(path=...)", flush=True)
res = mt5.initialize(path=r"C:\Program Files\MetaTrader 5\terminal64.exe", timeout=10000)
print(f"Initialize result: {res}", flush=True)
if not res:
    print(f"Error: {mt5.last_error()}", flush=True)
    sys.exit(0)

print("Step 2: Checking version...", flush=True)
print("Version:", mt5.version(), flush=True)

print("Step 3: Checking terminal_info...", flush=True)
print("Terminal info:", mt5.terminal_info(), flush=True)

print("Step 4: Attempting login with server IP...", flush=True)
login_res = mt5.login(5962339, password="Test1234!", server="mt5.demo.trade-hw.online:443", timeout=10000)
print(f"Login result: {login_res}", flush=True)
if not login_res:
    print(f"Login error: {mt5.last_error()}", flush=True)

acc = mt5.account_info()
print("Account info:", acc, flush=True)

mt5.shutdown()
print("Done", flush=True)

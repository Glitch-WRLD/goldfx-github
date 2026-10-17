import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import MetaTrader5 as mt5

print("Testing MT5 initialize...")
ok = mt5.initialize()
print(f"mt5.initialize() result: {ok}")
if not ok:
    print(f"Error: {mt5.last_error()}")
    sys.exit(1)

# Check account info before login
acc_before = mt5.account_info()
print("Account info before login:", acc_before.login if acc_before else "None")

# Try login with Headway-Demo
login = 5962339
pwd = "Test1234!"
server = "Headway-Demo"

print(f"Attempting login to {login} on {server}...")
logged_in = mt5.login(login=login, password=pwd, server=server)
print(f"Login result: {logged_in}")
if not logged_in:
    print(f"Login error: {mt5.last_error()}")
    # Try server IP directly
    server_ip = "mt5.demo.trade-hw.online"
    print(f"Attempting login with server IP: {server_ip}...")
    logged_in_ip = mt5.login(login=login, password=pwd, server=server_ip)
    print(f"Login with IP result: {logged_in_ip}, error: {mt5.last_error()}")
else:
    acc = mt5.account_info()
    if acc:
        print(f"SUCCESSFULLY LOGGED IN!")
        print(f"Account: {acc.login}")
        print(f"Name: {acc.name}")
        print(f"Server: {acc.server}")
        print(f"Balance: ${acc.balance:.2f}")
        print(f"Equity: ${acc.equity:.2f}")
        print(f"Currency: {acc.currency}")
        print(f"Leverage: 1:{acc.leverage}")

mt5.shutdown()

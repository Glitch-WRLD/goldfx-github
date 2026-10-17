import MetaTrader5 as m
import sys, os
LOGIN=5962339
PASS='Test1234!'
SERVER='Headway-Demo'
PATH=r'C:\Program Files\MetaTrader 5\terminal64.exe'
t=m.initialize(PATH, login=LOGIN, password=PASS, server=SERVER, timeout=120000)
print("IPC=",t,"ERR=",m.last_error())
if t:
    print("TERMINAL_PRODUCT=",m.terminal_info().name if m.terminal_info() else None)
    print("ACCOUNT=",m.account_info().login if m.account_info() else None)
    m.shutdown()
else:
    m.shutdown()

import sys
import MetaTrader5 as m
print("PKG_VERSION=" + getattr(m, "__version__", "N/A"))
print("PY=" + sys.version.split()[0])

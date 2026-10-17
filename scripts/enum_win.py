import ctypes

user32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)

def callback(hwnd, extra):
    length = user32.GetWindowTextLengthW(hwnd)
    buff = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buff, length + 1)
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title = buff.value
    if pid.value == 2844 or "MetaTrader" in title or "Headway" in title or "5962339" in title:
        vis = user32.IsWindowVisible(hwnd)
        print(f"HWND: {hwnd} | PID: {pid.value} | Visible: {vis} | Title: '{title}'")
    return True

user32.EnumWindows(WNDENUMPROC(callback), 0)

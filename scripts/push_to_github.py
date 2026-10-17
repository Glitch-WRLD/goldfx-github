import ctypes
from ctypes import wintypes
import subprocess
import sys

advapi32 = ctypes.windll.advapi32

class CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ('Flags', wintypes.DWORD),
        ('Type', wintypes.DWORD),
        ('TargetName', wintypes.LPWSTR),
        ('Comment', wintypes.LPWSTR),
        ('LastWritten', wintypes.FILETIME),
        ('CredentialBlobSize', wintypes.DWORD),
        ('CredentialBlob', ctypes.POINTER(ctypes.c_byte)),
        ('Persist', wintypes.DWORD),
        ('AttributeCount', wintypes.DWORD),
        ('Attributes', ctypes.c_void_p),
        ('TargetAlias', wintypes.LPWSTR),
        ('UserName', wintypes.LPWSTR),
    ]

PCREDENTIAL = ctypes.POINTER(CREDENTIAL)
CredReadW = advapi32.CredReadW
CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIAL)]
CredReadW.restype = wintypes.BOOL

target = 'GitHub - https://api.github.com/Glitch-WRLD'
pcred = PCREDENTIAL()
if not CredReadW(target, 1, 0, ctypes.byref(pcred)):
    print(f"Failed to read credential: {ctypes.GetLastError()}")
    sys.exit(1)

cred = pcred.contents
raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
token = raw.decode('utf-16le', errors='ignore') if b'\x00' in raw else raw.decode('utf-8', errors='ignore')
username = cred.UserName
advapi32.CredFree(pcred)

print(f"Read credentials for {username}. Pushing to GitHub...")

# First pull with rebase to avoid non-fast-forward if GitHub Actions committed state.json
push_url = f"https://{username}:{token}@github.com/Glitch-WRLD/goldfx-github.git"
pull_res = subprocess.run(["git", "pull", "--rebase", push_url, "main"], capture_output=True, text=True)
print("PULL OUTPUT:")
print(pull_res.stdout)
if pull_res.stderr:
    print("PULL STDERR:", pull_res.stderr)

push_res = subprocess.run(["git", "push", push_url, "main"], capture_output=True, text=True)
print("PUSH OUTPUT:")
print(push_res.stdout)
if push_res.stderr:
    print("PUSH STDERR:", push_res.stderr)

if push_res.returncode == 0:
    print("\nSUCCESS! Successfully pushed all commits to GitHub.")
else:
    print(f"\nFAILED with return code {push_res.returncode}")
    sys.exit(push_res.returncode)

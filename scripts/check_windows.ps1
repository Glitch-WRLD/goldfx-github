Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class User32 {
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
}
"@

[User32]::EnumWindows({
    param($hWnd, $lParam)
    $procId = 0
    [User32]::GetWindowThreadProcessId($hWnd, [ref]$procId) | Out-Null
    if ($procId -eq 2844) {
        $sb = New-Object System.Text.StringBuilder 256
        $len = [User32]::GetWindowText($hWnd, $sb, 256)
        $vis = [User32]::IsWindowVisible($hWnd)
        Write-Output "hWnd: $hWnd | Visible: $vis | Title: '$($sb.ToString())'"
    }
    return $true
}, [IntPtr]::Zero) | Out-Null

"""Before each step: is this process on the interactive desktop the user sees? A service in session 0,
a locked screen (the input desktop is Winlogon) or a private desktop cannot reach GUI applications."""
from __future__ import annotations

from ..platform import powershell

SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices; using System.Text;
public static class TaskdDesk {
  [DllImport("user32.dll", SetLastError=true)] static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
  [DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Unicode)] static extern bool GetUserObjectInformation(IntPtr h, int index, StringBuilder info, int len, out int needed);
  [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr h);
  [DllImport("user32.dll")] static extern IntPtr GetProcessWindowStation();
  static string Name(IntPtr h) { var sb = new StringBuilder(256); int n; return GetUserObjectInformation(h, 2, sb, sb.Capacity * 2, out n) ? sb.ToString() : null; }
  public static string Input() { IntPtr h = OpenInputDesktop(0, false, 0x0001); if (h == IntPtr.Zero) return null; try { return Name(h); } finally { CloseDesktop(h); } }
  public static string Station() { return Name(GetProcessWindowStation()); }
}
"@
@{session = [System.Diagnostics.Process]::GetCurrentProcess().SessionId; station = [TaskdDesk]::Station(); input = [TaskdDesk]::Input()} | ConvertTo-Json -Compress
'''


class WindowsDesktopProbe:
    name = 'windows_desktop'
    platforms = ('win32',)

    def probe(self):
        try:
            d = powershell.run_json(SCRIPT, {}, timeout=60)
        except Exception as e:
            return False, 'desktop check failed: %s' % str(e)[:200], None
        if not d.get('session'):
            return False, 'running in session 0 (a service); GUI applications are unreachable', d
        if d.get('station') != 'WinSta0':
            return False, 'not on the interactive window station', d
        if d.get('input') != 'Default':
            return False, 'the screen is locked or another desktop has input', d
        return True, 'interactive desktop', d

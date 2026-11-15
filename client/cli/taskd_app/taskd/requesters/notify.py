"""Best-effort desktop notifications with fixed scripts; the text is an argument, never script code."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from ..platform import powershell

WINDOWS_SCRIPT = r'''
$p = $env:TASKD_PS_PARAMS | ConvertFrom-Json
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.Visible = $true
$n.ShowBalloonTip(10000, $p.title, $p.text, [System.Windows.Forms.ToolTipIcon]::Info)
Start-Sleep -Seconds 10
$n.Dispose()
@{ok = $true} | ConvertTo-Json -Compress
'''


class Notifier:
    def notify(self, title, text):
        title, text = title[:120], text[:400]
        if sys.platform == 'darwin':
            script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
            subprocess.run(['/usr/bin/osascript', '-e', script, title, text], capture_output=True, timeout=10)
        elif sys.platform == 'win32':
            powershell.run_json(WINDOWS_SCRIPT, {'title': title, 'text': text}, timeout=30)
        elif shutil.which('notify-send') and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            subprocess.run(['notify-send', title, text], capture_output=True, timeout=10)

"""Start taskd at login: a LaunchAgent on macOS, a systemd user service on Linux, a Startup-folder
script on Windows (current user, no administrator rights, no stored password). Same approach as
agent-portal/connector.py install."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import sys


def label_for(root):
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:8]


def paths(root):
    h = label_for(root)
    if sys.platform == 'darwin':
        return Path.home() / 'Library/LaunchAgents' / ('org.taskd.%s.plist' % h)
    if sys.platform.startswith('linux'):
        return Path.home() / '.config/systemd/user' / ('taskd-%s.service' % h)
    if sys.platform == 'win32':
        return Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Startup' / ('taskd-%s.vbs' % h)
    raise RuntimeError('autostart is not supported on this system; run `taskd serve` yourself')


def install(root, argv, log_path, start=True):
    f = paths(root)
    f.parent.mkdir(parents=True, exist_ok=True)
    argv = [str(a) for a in argv]
    if sys.platform == 'darwin':
        label = f.stem
        doc = {'Label': label, 'ProgramArguments': argv, 'RunAtLoad': True, 'KeepAlive': True,
               'StandardOutPath': str(log_path), 'StandardErrorPath': str(log_path)}
        f.write_bytes(plistlib.dumps(doc))
        f.chmod(0o600)
        if start:
            subprocess.run(['launchctl', 'bootout', 'gui/%d' % os.getuid(), str(f)], capture_output=True)
            subprocess.run(['launchctl', 'bootstrap', 'gui/%d' % os.getuid(), str(f)], check=True)
    elif sys.platform.startswith('linux'):
        quote = lambda x: '"' + x.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
        f.write_text('[Unit]\nDescription=taskd\nAfter=network-online.target\n[Service]\nExecStart=%s\n'
                     'Restart=on-failure\nRestartSec=10\n[Install]\nWantedBy=default.target\n' % ' '.join(map(quote, argv)))
        if start:
            subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
            subprocess.run(['systemctl', '--user', 'enable', '--now', f.name], check=True)
    else:
        pythonw = Path(argv[0]).with_name('pythonw.exe')
        if pythonw.exists():
            argv[0] = str(pythonw)
        if any('\r' in a or '\n' in a for a in argv):
            raise RuntimeError('paths must not contain line breaks')
        line = subprocess.list2cmdline(argv).replace('"', '""')
        f.write_text('Set sh = CreateObject("WScript.Shell")\r\nsh.Run "%s", 0, False\r\n' % line, encoding='utf-16')
        if start:
            subprocess.Popen(['wscript.exe', str(f)], close_fds=True)
    return f


def uninstall(root):
    f = paths(root)
    if not f.exists():
        return None
    if sys.platform == 'darwin':
        subprocess.run(['launchctl', 'bootout', 'gui/%d' % os.getuid(), str(f)], capture_output=True)
    elif sys.platform.startswith('linux'):
        subprocess.run(['systemctl', '--user', 'disable', '--now', f.name], capture_output=True)
    f.unlink()
    return f

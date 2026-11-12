#!/usr/bin/env python3
import os, sqlite3, time
from pathlib import Path
os.umask(0o077)
root=Path('/var/lib/agent-portal');dest=root/'backups';dest.mkdir(exist_ok=True,mode=0o700)
stamp=time.strftime('%Y%m%d-%H%M%S')
for source,suffix in [(root/'portal.sqlite3',''),(root/'workbuddy/oauth.sqlite3','-workbuddy')]:
    if not source.is_file():continue
    with sqlite3.connect(source) as src, sqlite3.connect(dest/(stamp+suffix+'.sqlite3')) as target:src.backup(target)
    (dest/(stamp+suffix+'.sqlite3')).chmod(0o600)
for workbuddy in (False,True):
    files=[p for p in dest.glob('*.sqlite3') if ('-workbuddy' in p.name)==workbuddy]
    for p in sorted(files,reverse=True)[7:]:p.unlink()

#!/usr/bin/env python3
"""Stands in for the codex CLI: records how it was called and answers from FAKE_CODEX_REPLY."""
import json
import os
import sys
import time
import uuid

argv = sys.argv[1:]
stdin = sys.stdin.read()
log = os.environ.get('FAKE_CODEX_LOG')
if log:
    with open(log, 'a') as f:
        f.write(json.dumps({'argv': argv, 'cwd': os.getcwd(), 'stdin': stdin,
                            'codex_home': os.environ.get('CODEX_HOME')}) + '\n')
if os.environ.get('FAKE_CODEX_SLEEP'):
    time.sleep(float(os.environ['FAKE_CODEX_SLEEP']))
sid = argv[argv.index('resume') + 1] if 'resume' in argv else str(uuid.uuid4())
print(json.dumps({'type': 'thread.started', 'thread_id': sid}))
out = argv[argv.index('-o') + 1]
reply = os.environ.get('FAKE_CODEX_REPLY', '{"ok": true}')
if reply != '__none__':
    with open(out, 'w') as f:
        f.write(reply)
sys.exit(int(os.environ.get('FAKE_CODEX_EXIT', '0')))

"""Running fixed PowerShell scripts on Windows. Scripts are constants of this package; everything a
task supplies travels as JSON in an environment variable, never inside the script text."""
from __future__ import annotations

import base64
import json
import os

from . import proc

PARAMS_VAR = 'TASKD_PS_PARAMS'


def encoded(script):
    return base64.b64encode(script.encode('utf-16-le')).decode('ascii')


def run_json(script, params, timeout=60, shell='powershell.exe'):
    """Run script with params in $env:TASKD_PS_PARAMS and parse the JSON it prints."""
    env = dict(os.environ, **{PARAMS_VAR: json.dumps(params, ensure_ascii=False)})
    r = proc.run([shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                  '-EncodedCommand', encoded(script)], env=env, timeout=timeout)
    if not r.ok:
        raise RuntimeError('PowerShell failed (%s): %s' % (
            'timed out' if r.timed_out else 'exit %s' % r.code, r.stderr.strip()[:300]))
    text = r.stdout.strip().splitlines()
    if not text:
        raise RuntimeError('PowerShell printed nothing')
    return json.loads(text[-1])

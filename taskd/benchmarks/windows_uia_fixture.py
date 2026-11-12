#!/usr/bin/env python3
"""Real Windows UIA batch/freshness smoke test, using ONLY a new disposable WinForms window.

No EPS, keyboard, mouse, existing window, service or business task is operated.
--app is the installed taskd application directory; --out is a new evidence JSON.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

FIXTURE = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$cfg = $env:TASKD_FIXTURE_PARAMS | ConvertFrom-Json
$form = New-Object System.Windows.Forms.Form
$form.Text = $cfg.title
$form.Width = 360; $form.Height = 160; $form.ShowInTaskbar = $false
$runButton = New-Object System.Windows.Forms.Button
$runButton.Name = 'TaskdFixtureRun'; $runButton.Text = 'TaskdFixtureRun'
$runButton.Left = 10; $runButton.Top = 10; $runButton.Width = 200
$valueBox = New-Object System.Windows.Forms.TextBox
$valueBox.Name = 'TaskdFixtureValue'; $valueBox.AccessibleName = 'TaskdFixtureValue'
$valueBox.Text = '7'; $valueBox.Left = 10; $valueBox.Top = 50
$form.Controls.Add($runButton); $form.Controls.Add($valueBox)
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 100
$started = [DateTime]::UtcNow
$timer.Add_Tick({
  if ((Test-Path -LiteralPath $cfg.stop) -or ([DateTime]::UtcNow - $started).TotalSeconds -gt 90) {
    $timer.Stop(); $form.Close(); return
  }
  if (Test-Path -LiteralPath $cfg.change) {
    $runButton.Enabled = $false; $valueBox.Text = '9'
    [IO.File]::WriteAllText($cfg.changed, 'changed')
  }
})
$form.Add_Shown({ [IO.File]::WriteAllText($cfg.ready, 'ready'); $timer.Start() })
[System.Windows.Forms.Application]::Run($form)
$timer.Dispose(); $form.Dispose()
'''


def wait_file(path, child, timeout=15):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if child.poll() is not None:
            raise RuntimeError('owned fixture exited: %s' % child.returncode)
        if time.monotonic() >= deadline:
            raise TimeoutError('fixture state transition timed out')
        time.sleep(.05)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--app', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    if sys.platform != 'win32':
        raise SystemExit('This smoke test requires a Windows interactive desktop; no test was run.')
    if args.out.exists():
        raise SystemExit('Evidence already exists; choose a new output path.')
    sys.path.insert(0, str(args.app.resolve()))
    from taskd.checkers import windows_uia
    from taskd.checkers.base import CheckContext
    from taskd.service import VERSION
    title = 'Taskd-UIA-Fixture-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='taskd-uia-owned-') as tmp:
        root = Path(tmp)
        cfg = {'title': title, **{k: str(root / k) for k in ('ready', 'change', 'changed', 'stop')}}
        env = dict(os.environ, TASKD_FIXTURE_PARAMS=json.dumps(cfg))
        encoded = base64.b64encode(FIXTURE.encode('utf-16le')).decode()
        with (root / 'fixture.stderr').open('w', encoding='utf-8') as err:
            child = subprocess.Popen(['powershell.exe', '-STA', '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded], env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                creationflags=0x08000200)
            result = {'scope': 'new disposable fixture only; no EPS or physical operation', 'version': VERSION}
            try:
                wait_file(root/'ready', child)
                checker = windows_uia.UIAPropertyChecker()
                common = {'window': title, 'process_id': child.pid}
                checks = [{'id': 'enabled', 'type': 'uia_property', 'params': dict(common,
                    element='TaskdFixtureRun', property='IsEnabled', expect=True)},
                    {'id': 'value', 'type': 'uia_property', 'params': dict(common,
                    element='TaskdFixtureValue', property='Value', expect='7')},
                    {'id': 'disabled', 'type': 'uia_property', 'params': dict(common,
                    element='TaskdFixtureRun', property='IsEnabled', expect=False)}]
                ctx = CheckContext(root, time.time())
                original = windows_uia.powershell.run_json
                calls = []
                def measured(script, params, **kwargs):
                    start = time.monotonic()
                    try:
                        return original(script, params, **kwargs)
                    finally:
                        calls.append({'seconds': time.monotonic()-start, 'queries': len(params.get('queries', []))})
                windows_uia.powershell.run_json = measured
                try:
                    start = time.monotonic()
                    serial = {c['id']: checker.check(c['params'], ctx).to_dict() for c in checks}
                    result['serial'] = {'seconds': time.monotonic()-start, 'calls': list(calls)}
                    calls.clear(); start = time.monotonic()
                    batch = checker.check_many(checks, ctx)
                    result['batch'] = {'seconds': time.monotonic()-start, 'calls': list(calls)}
                    assert [serial[c['id']]['outcome'] for c in checks] == ['pass', 'pass', 'fail'], serial
                    assert batch == serial, {'batch': batch, 'serial': serial}
                    assert len(calls) == 1 and calls[0]['queries'] == 2
                    (root/'change').write_text('change only our own fixture')
                    wait_file(root/'changed', child)
                    calls.clear()
                    fresh = checker.check_many(checks, ctx)
                    assert [fresh[c['id']]['outcome'] for c in checks] == ['fail', 'fail', 'pass'], fresh
                    result.update(passed=True, equivalent_results=True, fresh_after_change=True,
                                  serial_results=serial, batch_results=batch, changed_results=fresh)
                finally:
                    windows_uia.powershell.run_json = original
            except Exception as exc:
                result.update(passed=False, error=type(exc).__name__ + ': ' + str(exc)[:2000])
                raise
            finally:
                (root/'stop').write_text('close only our own fixture')
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.terminate()  # exclusively this child created by this invocation
                    child.wait(timeout=5)
                result['fixture_exit_code'] = child.returncode
                result['script_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'passed': result['passed'], 'evidence': str(args.out)}))


if __name__ == '__main__':
    main()

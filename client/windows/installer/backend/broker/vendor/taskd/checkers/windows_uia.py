"""Windows UI checkers through UI Automation (UIAutomationClient, as deployments/anet-codex-output-fix
desktop_probe.ps1 already uses). They must run in the same logged-in session and desktop as the
application, at the same integrity level."""
from __future__ import annotations

from pathlib import Path

from ..platform import powershell
from .base import Checker, CheckResult, only

PROPERTIES = ('IsEnabled', 'Name', 'Value', 'ToggleState', 'IsSelected', 'IsOffscreen', 'HelpText')

COMMON = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$p = $env:TASKD_PS_PARAMS | ConvertFrom-Json
$AE = [Windows.Automation.AutomationElement]
function Find-Window($pattern) {
  $all = $AE::RootElement.FindAll([Windows.Automation.TreeScope]::Children, [Windows.Automation.Condition]::TrueCondition)
  $candidateWindows = @($all | Where-Object { $_.Current.Name -like $pattern -and ((-not $p.process_id) -or $_.Current.ProcessId -eq $p.process_id) })
  if ($candidateWindows.Count -eq 1) { return $candidateWindows[0] }
  if ($candidateWindows.Count -gt 1) { throw 'window selector is ambiguous; specify process_id or an exact title' }
  return $null
}
function Find-Element($win, $name) {
  $c = New-Object Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $name)
  $e = $win.FindFirst([Windows.Automation.TreeScope]::Descendants, $c)
  if (-not $e) {
    $c = New-Object Windows.Automation.PropertyCondition($AE::NameProperty, $name)
    $e = $win.FindFirst([Windows.Automation.TreeScope]::Descendants, $c)
  }
  return $e
}
function Read-Property($e, $prop) {
  switch ($prop) {
    'IsEnabled' { return $e.Current.IsEnabled }
    'Name' { return $e.Current.Name }
    'IsOffscreen' { return $e.Current.IsOffscreen }
    'HelpText' { return $e.Current.HelpText }
    'Value' { $x = $null; if ($e.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern, [ref]$x)) { return $x.Current.Value }; return $null }
    'ToggleState' { $x = $null; if ($e.TryGetCurrentPattern([Windows.Automation.TogglePattern]::Pattern, [ref]$x)) { return [string]$x.Current.ToggleState }; return $null }
    'IsSelected' { $x = $null; if ($e.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$x)) { return $x.Current.IsSelected }; return $null }
  }
  return $null
}
'''

READ_SCRIPT = COMMON + r'''
$win = Find-Window $p.window
if (-not $win) { @{found_window = $false} | ConvertTo-Json -Compress; exit 0 }
$items = @()
$elements = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([System.StringComparer]::Ordinal)
foreach ($q in $p.queries) {
  # Invocation-local only: every check batch obtains fresh controls and values.
  if (-not $elements.ContainsKey($q.element)) { $elements[$q.element] = Find-Element $win $q.element }
  $e = $elements[$q.element]
  if (-not $e) { $items += ,@{element = $q.element; found = $false} }
  else { $items += ,@{element = $q.element; found = $true; property = $q.property; value = (Read-Property $e $q.property)} }
}
@{found_window = $true; window = $win.Current.Name; items = $items} | ConvertTo-Json -Compress -Depth 5
'''

SCREENSHOT_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public static class TaskdDpi { [DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); }'
[void][TaskdDpi]::SetProcessDPIAware()
''' + COMMON + r'''
Add-Type -AssemblyName System.Drawing
$win = Find-Window $p.window
if (-not $win) { @{saved = $false; found_window = $false} | ConvertTo-Json -Compress; exit 0 }
$r = $win.Current.BoundingRectangle
$w = [int][Math]::Ceiling($r.Width); $h = [int][Math]::Ceiling($r.Height)
if ($w -le 0 -or $h -le 0) { @{saved = $false; found_window = $true; reason = 'window has no area'} | ConvertTo-Json -Compress; exit 0 }
$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen([int]$r.X, [int]$r.Y, 0, 0, $bmp.Size)
$bmp.Save($p.out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
@{saved = $true; found_window = $true; window = $win.Current.Name; width = $w; height = $h; path = $p.out} | ConvertTo-Json -Compress
'''


def _window(p):
    if not isinstance(p.get('window'), str) or not p['window'].strip():
        raise ValueError('window is a title pattern such as "EPS*"')
    if 'process_id' in p and (type(p['process_id']) is not int or p['process_id'] <= 0):
        raise ValueError('process_id is a positive integer')


class UIABatch:
    """One PowerShell process/window lookup; never reuse a previous UI observation."""
    def batch_key(self, params):
        return ('windows_uia_read', params['window'], params.get('process_id'))

    def check_many(self, checks, ctx):
        prepared, queries, results = {}, {}, {}
        for c in checks:
            try:
                p = c['params']
                if c['type'] == 'uia_property':
                    expect = {p['element']: p['expect']}
                else:
                    expect = _expect_values(p, ctx)
                    if not isinstance(expect, dict) or not expect or any(not isinstance(k, str) or not k for k in expect):
                        raise ValueError('the expected values are not an element mapping')
                prepared[c['id']] = (p, expect)
                for element in expect:
                    key = (element, p['property'])
                    queries.setdefault(key, {'element': element, 'property': p['property']})
            except Exception as e:
                results[c['id']] = CheckResult('cannot_check', None, self.layer, str(e)[:300]).to_dict()
        if not prepared:
            return results
        first = next(iter(prepared.values()))[0]
        params = {'window': first['window'], 'queries': list(queries.values())}
        if first.get('process_id'):
            params['process_id'] = first['process_id']
        try:
            out = powershell.run_json(READ_SCRIPT, params)
            got = {(i['element'], i.get('property')): i for i in out.get('items', [])}
            for cid, (p, expect) in prepared.items():
                if not out.get('found_window'):
                    result = CheckResult('cannot_check', out, self.layer, 'window not found')
                elif len(expect) == 1 and next(c for c in checks if c['id'] == cid)['type'] == 'uia_property':
                    item = got.get((p['element'], p['property']), {})
                    observation = {'found_window': True, 'window': out.get('window'), 'items': [item]}
                    result = CheckResult('pass' if item.get('found') and matches(item.get('value'), p['expect']) else 'fail',
                                         observation, self.layer, None if item.get('found') else 'element not found')
                else:
                    values = {k: got[(k, p['property'])].get('value') for k in expect
                              if got.get((k, p['property']), {}).get('found')}
                    mismatched = {k: {'expect': v, 'value': values.get(k)} for k, v in expect.items()
                                  if k not in values or not matches(values[k], v)}
                    result = CheckResult('pass' if not mismatched else 'fail',
                                         {'mismatched': mismatched, 'read': len(values)}, self.layer)
                results[cid] = result.to_dict()
        except Exception as e:
            for cid in prepared:
                results[cid] = CheckResult('cannot_check', None, self.layer, str(e)[:300]).to_dict()
        return results


def _expect_values(p, ctx):
    if 'expect' in p:
        return p['expect']
    path = ctx.resolve(p['expect_file'], allow_read_paths=False)
    import json
    return json.loads(path.read_text(encoding='utf-8'))


def matches(value, expect):
    if isinstance(expect, bool):
        return value is expect or str(value).lower() == str(expect).lower()
    if isinstance(expect, (int, float)) and not isinstance(expect, bool):
        try:
            return abs(float(value) - float(expect)) <= 1e-9
        except (TypeError, ValueError):
            return False
    return str(value).strip() == str(expect).strip()


class UIAPropertyChecker(UIABatch, Checker):
    version = 2
    name = 'uia_property'
    platforms = ('win32',)
    doc = ('uia_property: {"window": title pattern, "element": AutomationId or Name, "property": one of %s, '
           '"expect": value, optional "process_id": positive integer}' % ', '.join(PROPERTIES))

    def validate(self, params):
        p = only(params, ('window', 'element', 'property', 'expect', 'process_id'), ('window', 'element', 'property', 'expect'))
        _window(p)
        if not isinstance(p['element'], str) or not p['element']:
            raise ValueError('element is an AutomationId or a Name')
        if p['property'] not in PROPERTIES:
            raise ValueError('property is one of ' + ', '.join(PROPERTIES))
        return p

    def check(self, params, ctx, baseline=None):
        return CheckResult(**self.check_many([{'id': 'one', 'type': self.name, 'params': params}], ctx)['one'])


class UIAValuesChecker(UIABatch, Checker):
    version = 2
    name = 'uia_values'
    platforms = ('win32',)
    doc = ('uia_values: {"window": title pattern, "expect": {element: value} or "expect_file": JSON file in '
           'the work dir with that mapping, "property": property to read (default Value), optional "process_id": positive integer}')

    def validate(self, params):
        p = only(params, ('window', 'expect', 'expect_file', 'property', 'process_id'), ('window',))
        _window(p)
        if ('expect' in p) == ('expect_file' in p):
            raise ValueError('give exactly one of expect and expect_file')
        if 'expect' in p and (not isinstance(p['expect'], dict) or not p['expect']):
            raise ValueError('expect maps element names to values')
        if 'expect_file' in p and (not isinstance(p['expect_file'], str) or Path(p['expect_file']).is_absolute()):
            raise ValueError('expect_file is relative to the work directory')
        p.setdefault('property', 'Value')
        if p['property'] not in PROPERTIES:
            raise ValueError('property is one of ' + ', '.join(PROPERTIES))
        return p

    def check(self, params, ctx, baseline=None):
        return CheckResult(**self.check_many([{'id': 'one', 'type': self.name, 'params': params}], ctx)['one'])


class WindowScreenshotChecker(Checker):
    """Collects evidence: a screenshot of exactly the window's bounds, taken by taskd itself."""
    name = 'window_screenshot'
    platforms = ('win32',)
    layer = 'evidence'
    doc = 'window_screenshot: {"window": title pattern}; saved into the task\'s evidence directory'

    def validate(self, params):
        p = only(params, ('window',), ('window',))
        _window(p)
        return p

    def check(self, params, ctx, baseline=None):
        if ctx.evidence_dir is None:
            return CheckResult('cannot_check', None, self.layer, 'no evidence directory')
        ctx.evidence_dir.mkdir(parents=True, exist_ok=True)
        out = ctx.evidence_dir / ('window-%d.png' % len(list(ctx.evidence_dir.glob('window-*.png'))))
        try:
            r = powershell.run_json(SCREENSHOT_SCRIPT, {'window': params['window'], 'out': str(out)})
        except Exception as e:
            return CheckResult('cannot_check', None, self.layer, str(e)[:300])
        if not r.get('saved'):
            return CheckResult('cannot_check', r, self.layer, r.get('reason') or 'window not found')
        return CheckResult('pass', dict(r, path=str(out)), self.layer)


WINDOWS = (UIAPropertyChecker, UIAValuesChecker, WindowScreenshotChecker)

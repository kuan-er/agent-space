import json
import os
from pathlib import Path
import time
import unittest
from unittest import mock

from helpers import builtin, tmpdir
from taskd.checkers.base import CheckContext
from taskd.checkers import windows_uia


class GenericCheckerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tmpdir())
        self.work = self.tmp / 'work'
        self.work.mkdir()
        self.outside = self.tmp / 'install'
        self.outside.mkdir()
        (self.outside / 'app.dll').write_bytes(b'binary')
        self.c = builtin.make_checkers()
        self.ctx = CheckContext(self.work, time.time() - 1, [self.outside])

    def test_resolve_refuses_outside_paths(self):
        for bad in ('../x', str(self.tmp / 'elsewhere'), '/etc/passwd'):
            with self.assertRaises(ValueError):
                self.ctx.resolve(bad)
        self.assertEqual(self.ctx.resolve(str(self.outside)), self.outside.resolve())
        with self.assertRaises(ValueError):
            self.ctx.resolve(str(self.outside), allow_read_paths=False)

    def test_files(self):
        f = self.c['files']
        params = f.validate({'pattern': 'out/*.md', 'count': 2, 'newer_than': 'task_start'})
        self.assertEqual(f.check(params, self.ctx).outcome, 'fail')
        (self.work / 'out').mkdir()
        (self.work / 'out' / 'a.md').write_text('a')
        (self.work / 'out' / 'b.md').write_text('b')
        r = f.check(params, self.ctx)
        self.assertEqual(r.outcome, 'pass')
        self.assertEqual(r.observed['count'], 2)
        self.assertEqual(len(r.observed['matches'][0]['sha256']), 64)
        old = self.work / 'out' / 'old.md'
        old.write_text('old')
        os.utime(old, (1, 1))
        self.assertEqual(f.check(f.validate({'pattern': 'out/old.md', 'newer_than': 'task_start'}), self.ctx).outcome, 'fail')
        for bad in ({'pattern': '/abs/*'}, {'pattern': '../*'}, {}, {'pattern': 'x', 'count': 0}, {'pattern': 'x', 'mystery': 1}):
            with self.assertRaises(ValueError):
                f.validate(bad)

    def test_value_json_and_csv(self):
        v = self.c['value']
        (self.work / 'summary.json').write_text(json.dumps({'total': {'rows': [1, 12840]}, 'name': 'abc'}))
        self.assertEqual(v.check(v.validate({'file': 'summary.json', 'path': 'total.rows.1', 'expect': 12840}), self.ctx).outcome, 'pass')
        self.assertEqual(v.check(v.validate({'file': 'summary.json', 'path': 'total.rows.1', 'op': '~=', 'expect': 12839, 'tolerance': 2}), self.ctx).outcome, 'pass')
        self.assertEqual(v.check(v.validate({'file': 'summary.json', 'path': 'name', 'op': 'contains', 'expect': 'b'}), self.ctx).outcome, 'pass')
        self.assertEqual(v.check(v.validate({'file': 'summary.json', 'path': 'missing', 'expect': 1}), self.ctx).outcome, 'fail')
        self.assertEqual(v.check(v.validate({'file': 'nofile.json', 'path': 'x', 'expect': 1}), self.ctx).outcome, 'fail')
        (self.work / 'data.csv').write_text('month,amount\n1,10\n2,32.5\n')
        self.assertEqual(v.check(v.validate({'file': 'data.csv', 'path': 'rows', 'expect': 2}), self.ctx).outcome, 'pass')
        self.assertEqual(v.check(v.validate({'file': 'data.csv', 'path': 'amount:sum', 'op': '>=', 'expect': 42}), self.ctx).outcome, 'pass')
        self.assertEqual(v.check(v.validate({'file': 'data.csv', 'path': 'amount:max', 'expect': 32.5}), self.ctx).outcome, 'pass')

    def test_dir_unchanged(self):
        d = self.c['dir_unchanged']
        params = d.validate({'path': str(self.outside)})
        base = d.baseline(params, self.ctx)
        self.assertEqual(d.check(params, self.ctx, base).outcome, 'pass')
        os.utime(self.outside / 'app.dll', None)  # only the time changes
        self.assertEqual(d.check(params, self.ctx, base).outcome, 'pass')
        (self.outside / 'app.dll').write_bytes(b'patched')
        r = d.check(params, self.ctx, base)
        self.assertEqual(r.outcome, 'fail')
        self.assertEqual(r.observed['changed'], ['app.dll'])
        self.assertEqual(d.check(params, self.ctx, None).outcome, 'cannot_check')
        with self.assertRaises(ValueError):
            d.baseline(d.validate({'path': str(self.tmp / 'not-allowed')}), self.ctx)

    def test_llm_review_is_left_to_the_engine(self):
        r = self.c['llm_review']
        self.assertEqual(r.check(r.validate({'question': 'ok?'}), self.ctx).outcome, 'needs_review')
        with self.assertRaises(ValueError):
            r.validate({'question': 'q', 'evidence': ['/abs']})

    def test_platform_filter(self):
        names = set(builtin.make_checkers())
        self.assertTrue({'files', 'value', 'dir_unchanged', 'llm_review'} <= names)
        with mock.patch('sys.platform', 'win32'):
            self.assertIn('uia_property', builtin.make_checkers())


class WindowsCheckerTests(unittest.TestCase):
    """The PowerShell side runs on Windows only; here the parameters and the interpretation are checked."""

    def setUp(self):
        self.tmp = Path(tmpdir())
        self.ctx = CheckContext(self.tmp, 0, (), self.tmp / 'evidence')

    def test_property(self):
        c = windows_uia.UIAPropertyChecker()
        params = c.validate({'window': 'EPS*', 'element': 'paintSimulationItem', 'property': 'IsEnabled', 'expect': True})
        calls = []

        def fake(script, p, timeout=60):
            calls.append(p)
            return {'found_window': True, 'window': 'EPS 3', 'items': [dict(p['queries'][0], found=True, value=False)]}
        with mock.patch.object(windows_uia.powershell, 'run_json', fake):
            r = c.check(params, self.ctx)
        self.assertEqual(r.outcome, 'fail')
        self.assertEqual(calls[0], {'window': 'EPS*', 'queries': [{'element': 'paintSimulationItem', 'property': 'IsEnabled'}]})
        with mock.patch.object(windows_uia.powershell, 'run_json', lambda s, p, **k: {'found_window': True, 'items': [dict(p['queries'][0], found=True, value=True)]}):
            self.assertEqual(c.check(params, self.ctx).outcome, 'pass')
        with mock.patch.object(windows_uia.powershell, 'run_json', lambda *a, **k: {'found_window': False}):
            self.assertEqual(c.check(params, self.ctx).outcome, 'cannot_check')

        def boom(*a, **k):
            raise RuntimeError('PowerShell failed')
        with mock.patch.object(windows_uia.powershell, 'run_json', boom):
            self.assertEqual(c.check(params, self.ctx).outcome, 'cannot_check')
        with self.assertRaises(ValueError):
            c.validate({'window': 'x', 'element': 'y', 'property': 'Secret', 'expect': 1})

    def test_values(self):
        c = windows_uia.UIAValuesChecker()
        (self.tmp / 'expected.json').write_text(json.dumps({'SprayLength': '200', 'Cone': 'Off'}))
        params = c.validate({'window': 'Params*', 'expect_file': 'expected.json'})
        out = {'found_window': True, 'items': [{'element': 'SprayLength', 'property': 'Value', 'found': True, 'value': '200'},
                                               {'element': 'Cone', 'property': 'Value', 'found': True, 'value': 'On'}]}
        with mock.patch.object(windows_uia.powershell, 'run_json', lambda *a, **k: out):
            r = c.check(params, self.ctx)
        self.assertEqual(r.outcome, 'fail')
        self.assertEqual(list(r.observed['mismatched']), ['Cone'])
        with self.assertRaises(ValueError):
            c.validate({'window': 'x'})

    def test_screenshot_is_taken_by_taskd(self):
        c = windows_uia.WindowScreenshotChecker()
        seen = {}

        def fake(script, p, timeout=60):
            seen.update(p)
            return {'saved': True, 'found_window': True, 'width': 800, 'height': 600}
        with mock.patch.object(windows_uia.powershell, 'run_json', fake):
            r = c.check(c.validate({'window': 'EPS*'}), self.ctx)
        self.assertEqual(r.outcome, 'pass')
        self.assertTrue(seen['out'].startswith(str(self.tmp / 'evidence')))

    def test_scripts_keep_task_text_out(self):
        """Task-supplied values reach PowerShell only through $env:TASKD_PS_PARAMS."""
        for script in (windows_uia.READ_SCRIPT, windows_uia.SCREENSHOT_SCRIPT):
            self.assertIn('$env:TASKD_PS_PARAMS', script)
            self.assertNotIn('{window}', script)


if __name__ == '__main__':
    unittest.main()

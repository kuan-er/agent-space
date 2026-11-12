import json
import os
from pathlib import Path
import stat
import sys
import time
import unittest
from unittest import mock

from helpers import ROOT, tmpdir
from taskd.executors.codex import CodexExecutor, thread_id
from taskd.executors.command import CommandExecutor

FAKE = Path(__file__).with_name('fake_codex.py')


def launcher(tmp):
    """A codex stand-in that is a real executable on PATH-less invocation."""
    p = Path(tmp) / 'codex'
    p.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE))
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tmpdir())
        self.log = self.tmp / 'calls.jsonl'
        self.work = self.tmp / 'work'
        self.work.mkdir()
        self.env = mock.patch.dict(os.environ, {'FAKE_CODEX_LOG': str(self.log)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()]

    def test_new_session(self):
        ex = CodexExecutor(command=launcher(self.tmp), sandbox='danger-full-access', codex_home=str(self.tmp / 'home'))
        with mock.patch.dict(os.environ, {'FAKE_CODEX_REPLY': '{"x": 1}'}):
            r = ex.run('do it', work_dir=self.work, timeout=30, schema={'type': 'object'})
        self.assertTrue(r.ok)
        self.assertEqual(r.text, '{"x": 1}')
        self.assertRegex(r.session_id, r'^[0-9a-f-]{36}$')
        c = self.calls()[0]
        a = c['argv']
        self.assertEqual(a[:5], ['exec', '-s', 'danger-full-access', '-C', str(self.work)])
        self.assertIn('--output-schema', a)
        self.assertIn('--json', a)
        self.assertEqual(a[-1], '-')
        self.assertEqual(c['stdin'], 'do it')  # the prompt never goes on the command line
        self.assertNotIn('do it', a)
        self.assertEqual(c['codex_home'], str(self.tmp / 'home'))

    def test_resume_passes_sandbox_as_config_and_cwd(self):
        ex = CodexExecutor(command=launcher(self.tmp), sandbox='workspace-write')
        r = ex.run('continue', work_dir=self.work, timeout=30, session='0000aaaa-0000-0000-0000-000000000001')
        self.assertEqual(r.session_id, '0000aaaa-0000-0000-0000-000000000001')
        c = self.calls()[0]
        self.assertEqual(c['argv'][:5], ['exec', 'resume', '0000aaaa-0000-0000-0000-000000000001', '-c', 'sandbox_mode=workspace-write'])
        self.assertNotIn('-s', c['argv'])
        self.assertNotIn('-C', c['argv'])
        self.assertEqual(Path(c['cwd']).resolve(), self.work.resolve())

    def test_review_mode_is_read_only_and_ephemeral(self):
        ex = CodexExecutor(command=launcher(self.tmp), sandbox='danger-full-access')
        img = self.work / 'shot.png'
        img.write_bytes(b'png')
        ex.run('review', work_dir=self.work, timeout=30, mode='review', session='ignored', images=[img])
        a = self.calls()[0]['argv']
        self.assertEqual(a[:3], ['exec', '-s', 'read-only'])
        self.assertIn('--ephemeral', a)
        self.assertNotIn('resume', a)
        self.assertEqual(a[a.index('-i') + 1], str(img))

    def test_failure_and_timeout(self):
        ex = CodexExecutor(command=launcher(self.tmp))
        with mock.patch.dict(os.environ, {'FAKE_CODEX_EXIT': '3', 'FAKE_CODEX_REPLY': '__none__'}):
            r = ex.run('x', work_dir=self.work, timeout=30)
        self.assertFalse(r.ok)
        self.assertEqual(r.error, 'exit status 3')
        with mock.patch.dict(os.environ, {'FAKE_CODEX_SLEEP': '30'}):
            start = time.time()
            r = ex.run('x', work_dir=self.work, timeout=1)
        self.assertTrue(r.timed_out)
        self.assertLess(time.time() - start, 15)

    def test_thread_id_parsing(self):
        self.assertEqual(thread_id('noise\n{"type":"thread.started","thread_id":"abc"}\n'), 'abc')
        self.assertIsNone(thread_id('{"type":"turn.started"}'))

    def test_selftest(self):
        ex = CodexExecutor(command=launcher(self.tmp))
        self.assertEqual(ex.selftest(self.work)[0], True)
        with mock.patch.dict(os.environ, {'FAKE_CODEX_EXIT': '1', 'FAKE_CODEX_REPLY': '__none__'}):
            ok, why = ex.selftest(self.work)
        self.assertFalse(ok)
        self.assertIn('logged in', why)


class CommandTests(unittest.TestCase):
    def test_stdin_stdout(self):
        tmp = Path(tmpdir())
        script = tmp / 'agent.py'
        script.write_text('import sys,os\ndata=sys.stdin.read()\nprint("got:"+str(len(data))+":"+os.environ["TASKD_MODE"])\n')
        ex = CommandExecutor(sys.executable, [str(script)])
        r = ex.run('hello', work_dir=tmp, timeout=30)
        self.assertTrue(r.ok)
        self.assertEqual(r.text, 'got:5:task')
        self.assertIsNone(r.session_id)
        bad = CommandExecutor(sys.executable, ['-c', 'import sys; sys.exit(2)'])
        self.assertEqual(bad.run('x', work_dir=tmp, timeout=30).error, 'exit status 2')


if __name__ == '__main__':
    unittest.main()

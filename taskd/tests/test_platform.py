import json
import os
from pathlib import Path
import plistlib
import sys
import unittest
from unittest import mock

from helpers import ROOT, tmpdir
from taskd.platform import autostart
from taskd.platform import proc
from taskd.platform.locks import SingleInstance
from taskd.service import Guard
from taskd.transports.anet import AnetTransport, norm_state

HERE = Path(__file__).resolve().parent


class BackgroundProcessTests(unittest.TestCase):
    def test_current_transport_reply_is_not_lost_or_duplicated(self):
        reply = {'messageId': 'progress-1', 'role': 'ROLE_AGENT', 'parts': [{'text': 'phase one'}]}
        raw = {'id': 'job', 'history': [], 'status': {'state': 'TASK_STATE_WORKING', 'message': reply},
               'metadata': {'anet.delivery': 'confirmed', 'anet.delivery_since': '2026-11-07T01:00:00Z'}}
        transport = AnetTransport('anet')
        with mock.patch.object(transport, 'cli', return_value=raw):
            full = transport.get('job')
            self.assertEqual([m['text'] for m in full['messages']], ['phase one'])
            self.assertEqual(full['delivery']['state'], 'confirmed')
            raw['history'] = [reply]
            self.assertEqual(len(transport.get('job')['messages']), 1)

    def test_windows_executor_preserves_io_without_console(self):
        child = mock.Mock(returncode=0)
        child.poll.return_value = 0
        with mock.patch('sys.platform', 'win32'), mock.patch.object(proc.subprocess, 'Popen', return_value=child) as spawn:
            result = proc.run(['codex.exe', 'exec'], stdin_text='request')
        self.assertTrue(result.ok)
        self.assertEqual(spawn.call_args.kwargs['creationflags'], 0x08000200)
        child.stdin.write.assert_called_once_with(b'request')

    def test_windows_transport_cli_does_not_open_window(self):
        completed = mock.Mock(returncode=0, stdout='{}')
        with mock.patch('sys.platform', 'win32'), mock.patch('taskd.transports.anet.subprocess.run', return_value=completed) as run:
            self.assertEqual(AnetTransport('anet.exe').cli('status'), {})
        self.assertEqual(run.call_args.kwargs['creationflags'], 0x08000000)
        self.assertTrue(run.call_args.kwargs['capture_output'])

    def test_windows_background_flags_not_canceled_by_detached_flag(self):
        with mock.patch('sys.platform', 'win32'), mock.patch.object(proc.subprocess, 'Popen') as spawn:
            proc.start_detached(['pythonw.exe', 'service.py'])
        self.assertEqual(spawn.call_args.kwargs['creationflags'], 0x08000200)
        with mock.patch('sys.platform', 'darwin'):
            self.assertEqual(proc.background_options(), {})


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tmpdir())
        self.patch = mock.patch.dict(os.environ, {'HOME': str(self.home), 'APPDATA': str(self.home / 'AppData')})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.root = self.home / '.anet' / 'taskd'
        self.argv = ['/usr/bin/python3', '/app/run_taskd.py', 'serve', '--data-dir', '/x y/.anet']

    def test_macos(self):
        with mock.patch('sys.platform', 'darwin'):
            f = autostart.install(self.root, self.argv, '/tmp/log', start=False)
        doc = plistlib.loads(f.read_bytes())
        self.assertEqual(doc['ProgramArguments'], self.argv)
        self.assertTrue(doc['KeepAlive'])
        self.assertTrue(f.name.startswith('org.taskd.'))

    def test_linux(self):
        with mock.patch('sys.platform', 'linux'):
            f = autostart.install(self.root, self.argv, '/tmp/log', start=False)
        text = f.read_text()
        self.assertIn('ExecStart="/usr/bin/python3" "/app/run_taskd.py" "serve" "--data-dir" "/x y/.anet"', text)
        self.assertIn('Restart=on-failure', text)

    def test_windows(self):
        argv = ['C:\\Python\\python.exe', 'C:\\app\\run_taskd.py', 'supervise', '--data-dir', 'C:\\Users\\a b\\.anet']
        with mock.patch('sys.platform', 'win32'):
            f = autostart.install(self.root, argv, 'C:\\log', start=False)
        vbs = f.read_text(encoding='utf-16')
        self.assertIn('sh.Run', vbs)
        self.assertIn('""C:\\Users\\a b\\.anet""', vbs)
        self.assertIn(', 0, False', vbs)
        with mock.patch('sys.platform', 'win32'):
            with self.assertRaises(RuntimeError):
                autostart.install(self.root, argv[:-1] + ['bad\npath'], 'C:\\log', start=False)

    def test_labels_differ_per_node(self):
        self.assertNotEqual(autostart.label_for('/a'), autostart.label_for('/b'))


class LockAndGuardTests(unittest.TestCase):
    def test_single_instance(self):
        p = Path(tmpdir()) / 'x.lock'
        a, b = SingleInstance(p), SingleInstance(p)
        self.assertTrue(a.acquire())
        self.assertTrue(b.held_elsewhere())
        a.release()
        self.assertFalse(b.held_elsewhere())

    def test_guard_restarts_a_stopped_companion(self):
        tmp = Path(tmpdir())
        lock = tmp / 'heartbeat.lock'
        started = []

        class T:
            def ensure_up(self):
                started.append('node')
        g = Guard(T(), [{'name': 'connector', 'argv': ['x'], 'lock': str(lock)}])
        with mock.patch('taskd.service.start_detached', lambda argv, log=None: started.append(argv)):
            g.tick()
            holder = SingleInstance(lock)
            holder.acquire()
            g.tick()
            holder.release()
        self.assertEqual(started, ['node', ['x'], 'node'])


@unittest.skipIf(sys.platform == 'win32', 'the simulated network uses fcntl')
class AnetTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tmpdir())
        launcher = self.tmp / 'anet'
        launcher.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, HERE / 'fake_anet.py'))
        launcher.chmod(0o755)
        self.patch = mock.patch.dict(os.environ, {'FAKE_ANET_NET': str(self.tmp / 'net.json')})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        for n in ('a', 'b'):
            (self.tmp / n).mkdir()
        self.a = AnetTransport(str(launcher), self.tmp / 'a')
        self.b = AnetTransport(str(launcher), self.tmp / 'b')
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)

    def test_round_trip(self):
        b_aid = self.b.status()['aid']
        tid = self.a.send_new(b_aid, '{"taskd":"submit","goal":"g"}')
        inbound = self.b.list_inbound()
        self.assertEqual(inbound[0]['id'], tid)
        self.assertEqual(inbound[0]['latest']['role'], 'requester')
        self.assertEqual(inbound[0]['peer'], self.a.status()['aid'])
        self.b.reply(tid, '', 'working')
        self.assertEqual(self.a.get(tid)['state'], 'working')
        f = self.tmp / 'report.json'
        f.write_text('{"ok": true}')
        self.b.reply(tid, 'done', 'completed', files=[f])
        full = self.a.get(tid)
        self.assertEqual(full['state'], 'completed')
        self.assertEqual(full['messages'][-1]['files'], ['report.json'])
        self.a.pull(tid, self.tmp / 'got')
        self.assertEqual(json.loads((self.tmp / 'got' / 'report.json').read_text()), {'ok': True})
        with self.assertRaises(Exception):
            self.b.reply(tid, 'late', 'input-required')

    def test_states(self):
        self.assertEqual(norm_state('TASK_STATE_INPUT_REQUIRED'), 'input-required')
        self.assertEqual(norm_state('completed'), 'completed')

    def test_autoreply_detection(self):
        self.assertFalse(self.a.autoreply_on())
        (self.tmp / 'a' / 'config.json').write_text(json.dumps({'auto_reply': {'backend': 'exec'}}))
        self.assertTrue(self.a.autoreply_on())


if __name__ == '__main__':
    unittest.main()

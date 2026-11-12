"""Two nodes on a simulated ANet network, each with its own taskd daemon: install, submit, plan,
execute, check, complete, and the result added back to the requester's original Codex session."""
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import ROOT, tmpdir, wait_for
from taskd.transports.anet import AnetTransport
from taskd.protocol import parse_status
from fake_appserver import FakeAppServer

HERE = Path(__file__).resolve().parent
SID = '01a1ab01-d7ca-7f83-ae0e-2d2256c41b3d'


def script(path, target):
    path.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, target))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@unittest.skipIf(sys.platform == 'win32', 'the simulated network uses fcntl')
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tmpdir())
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir()
        self.anet = script(bin_dir / 'anet', HERE / 'fake_anet.py')
        script(bin_dir / 'codex', HERE / 'fake_codex.py')
        self.agent = script(bin_dir / 'agent', HERE / 'fake_agent.py')
        self.req, self.exe = self.tmp / 'mac-node', self.tmp / 'vm-node'
        for d in (self.req, self.exe):
            d.mkdir()
        # The executor node had auto-reply on, as the Windows VM has today.
        (self.exe / 'config.json').write_text(json.dumps({'auto_reply': {'backend': 'exec', 'agent': 'codex', 'admitted': 'direct'}}))
        self.work = self.tmp / 'work'
        self.work.mkdir()
        self.codex_home = self.tmp / 'codex-home'
        day = self.codex_home / 'sessions' / '2026' / '11' / '05'
        day.mkdir(parents=True)
        self.session_cwd = self.tmp / 'project'
        self.session_cwd.mkdir()
        self.rollout = day / ('rollout-2026-11-05T10-00-00-%s.jsonl' % SID)
        self.rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': SID, 'cwd': str(self.session_cwd)}}) + '\n')
        old = time.time() - 600
        os.utime(self.rollout, (old, old))
        self.codex_log = self.tmp / 'codex-calls.jsonl'
        self.env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
                        FAKE_ANET_NET=str(self.tmp / 'net.json'), CODEX_HOME=str(self.codex_home),
                        FAKE_CODEX_LOG=str(self.codex_log), FAKE_CODEX_REPLY='已为你核对结果。', CODEX_THREAD_ID='')
        self.appserver = FakeAppServer(SID, self.session_cwd)
        self.procs = []

    def tearDown(self):
        self.appserver.close()
        for p in self.procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()

    def taskd(self, node, *args, env=None, check=True):
        cfg_path = node / 'taskd/config.json'
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            if cfg.get('requester'):
                cfg['requester']['codex_endpoint'] = self.appserver.endpoint
                cfg['requester']['downloads_dir'] = str(self.tmp / 'downloads')
                cfg_path.write_text(json.dumps(cfg))
        r = subprocess.run([sys.executable, str(ROOT / 'run_taskd.py'), '--data-dir', str(node), '--anet', str(self.anet), *args],
                           env=dict(self.env, ANET_DATA_DIR=str(node), **(env or {})), capture_output=True, text=True, timeout=120, cwd=self.session_cwd)
        if check and r.returncode:
            self.fail('taskd %s failed: %s %s' % (args[0], r.stdout, r.stderr))
        return r

    def serve(self, node):
        cfg_path = node / 'taskd' / 'config.json'
        cfg = json.loads(cfg_path.read_text())
        if cfg.get('requester'):
            cfg['requester']['codex_endpoint'] = self.appserver.endpoint
            cfg['requester']['downloads_dir'] = str(self.tmp / 'downloads')
        cfg['intervals'] = {'inbound': 0.5, 'relay': 0.5, 'status': 0.5, 'schedule': 1, 'guard': 3600}
        cfg_path.write_text(json.dumps(cfg))
        p = subprocess.Popen([sys.executable, str(node / 'taskd' / 'app' / 'run_taskd.py'), '--data-dir', str(node),
                              '--anet', str(self.anet), 'serve'],
                             env=dict(self.env, ANET_DATA_DIR=str(node)), stdout=subprocess.DEVNULL,
                             stderr=open(self.tmp / (node.name + '.stderr'), 'w'))
        self.procs.append(p)
        self.assertTrue(wait_for(lambda: (node/'taskd/status.json').exists() and
                                json.loads((node/'taskd/status.json').read_text()).get('pid')==p.pid,10))
        return p

    def test_quick_mode_is_answered_without_planning(self):
        self.taskd(self.exe, 'install', '--accept-tasks', '--work-dir', str(self.work), '--executor', 'command',
                   '--executor-command', str(self.agent), '--reviewer', 'none', '--no-requester', '--no-autostart')
        self.taskd(self.req, 'install', '--no-autostart')
        exe_aid = json.loads((self.exe / 'fake_identity.json').read_text())['aid']
        self.serve(self.exe)
        self.serve(self.req)
        spec = self.tmp / 'quick.json'
        spec.write_text(json.dumps({'goal': 'What is 6 times 7?', 'mode': 'quick'}))
        tid = json.loads(self.taskd(self.req, 'submit', '--to', exe_aid, '--spec', str(spec),'--no-callback').stdout)['task_id']
        self.assertTrue(wait_for(lambda: json.loads((self.tmp / 'net.json').read_text())['tasks'][tid]['state'] == 'completed', 30),
                        'the quick task did not complete')
        self.assertFalse((self.exe / 'taskd' / 'state' / 'tasks' / tid).exists(), 'a quick message must not become an engine task')
        self.assertTrue((self.exe / 'taskd' / 'state' / 'quick' / (tid + '.json')).exists())
        history = json.loads((self.tmp / 'net.json').read_text())['tasks'][tid]['history']
        self.assertTrue(any(m for m in history if 'ROLE_AGENT' in json.dumps(m)), 'no answer was sent')
        for p in self.procs:
            p.send_signal(signal.SIGTERM)
        for p in self.procs:
            p.wait(10)

    def test_standalone_capabilities_round_trip(self):
        self.taskd(self.exe, 'install', '--accept-tasks', '--work-dir', str(self.work), '--executor', 'command',
                   '--executor-command', str(self.agent), '--reviewer', 'none', '--no-requester', '--no-autostart')
        self.taskd(self.req, 'install', '--no-autostart')
        guide = (self.codex_home / 'AGENTS.md').read_text()
        self.assertIn('"taskd":"capabilities"', guide)
        self.assertIn('"taskd":"submit"', guide)
        exe_aid = json.loads((self.exe / 'fake_identity.json').read_text())['aid']
        self.serve(self.exe)
        with mock.patch.dict(os.environ, self.env):
            transport = AnetTransport(str(self.anet), self.req)
            try:
                tid = transport.send_new(exe_aid, '{"taskd":"capabilities","protocol":"taskd/1"}')
                self.assertTrue(wait_for(lambda: transport.get(tid)['state'] == 'completed', 30))
                full = transport.get(tid)
            finally:
                transport.close()
        answer = next(m for m in full['messages'] if m['role'] == 'provider')
        env = parse_status(answer['text'])
        self.assertEqual(env['capabilities']['protocol'], 'taskd/1')
        self.assertEqual(env['kind'], 'capabilities')
        self.assertFalse((self.exe/'taskd/state/tasks'/tid/'task.json').exists())
        self.assertFalse((self.exe/'taskd/state/quick'/(tid+'.json')).exists())

    def test_long_task_round_trip(self):
        out = self.taskd(self.exe, 'install', '--accept-tasks', '--work-dir', str(self.work), '--executor', 'command',
                         '--executor-command', str(self.agent), '--reviewer', 'none', '--no-requester', '--no-autostart').stdout
        self.assertIn('auto-reply was on; it is now off', out)
        self.assertIn('self-test: passed', out)
        exe_cfg = json.loads((self.exe / 'taskd' / 'config.json').read_text())
        self.assertEqual(exe_cfg['autoreply_backup']['agent'], 'codex')
        self.assertNotIn('auto_reply', json.loads((self.exe / 'config.json').read_text()))
        self.taskd(self.req, 'install', '--no-autostart')
        exe_aid = json.loads((self.exe / 'fake_identity.json').read_text())['aid']

        self.serve(self.exe)
        self.serve(self.req)
        spec = self.tmp / 'spec.json'
        spec.write_text(json.dumps({'goal': '计算结果并写到 out/result.txt', 'acceptance': [
            {'id': 'result', 'type': 'files', 'pattern': 'out/result.txt'}]}, ensure_ascii=False))
        sub = json.loads(self.taskd(self.req, 'submit', '--to', exe_aid, '--spec', str(spec),
                                    env={'CODEX_THREAD_ID': SID}).stdout)
        tid = sub['task_id']
        self.assertEqual(sub['session'], SID)

        exe_task = self.exe / 'taskd' / 'state' / 'tasks' / tid / 'task.json'
        self.assertTrue(wait_for(lambda: exe_task.exists() and json.loads(exe_task.read_text())['state'] == 'completed', 30),
                        exe_task.read_text() if exe_task.exists() else 'no task')
        net = json.loads((self.tmp / 'net.json').read_text())
        self.assertEqual(net['tasks'][tid]['state'], 'completed')

        tracked = self.req / 'taskd' / 'state' / 'tracked' / (tid + '.json')
        self.assertTrue(wait_for(lambda: tracked.exists() and json.loads(tracked.read_text())['state'] == 'done', 30),
                        tracked.read_text() if tracked.exists() else 'not tracked')
        rec = json.loads(tracked.read_text())
        self.assertTrue(rec['last_delivery']['to_session'])
        deliver = [c for c in self.appserver.calls if c['method'] == 'turn/start']
        self.assertEqual(len(deliver), 1)
        self.assertEqual(deliver[0]['params']['threadId'], SID)
        self.assertIn('只读', deliver[0]['params']['input'][0]['text'])
        self.assertIn('任务已完成', deliver[0]['params']['input'][0]['text'])
        self.assertEqual(rec['stages']['files']['state'], 'verified')
        self.assertEqual(rec['stages']['session']['state'], 'completed')
        files = rec['stages']['files']['files']
        report_path = next(Path(f['path']) for f in files if f['name'] == 'report.json')
        self.assertTrue(report_path.resolve().is_relative_to((self.tmp / 'downloads').resolve()))
        report = json.loads(report_path.read_text())
        self.assertEqual(report['checks'][0]['outcome'], 'pass')
        self.assertEqual((self.work / 'taskd-work' / 'long' / tid / 'out' / 'result.txt').read_text(), '42\n')

        show = json.loads(self.taskd(self.req, 'show', tid).stdout)
        self.assertEqual(show['remote']['state'], 'completed')
        status = json.loads((self.exe / 'taskd' / 'status.json').read_text())
        self.assertTrue(status['executor_ready'])
        self.assertEqual(status['capabilities']['protocol'], 'taskd/1')

        for p in self.procs:
            p.send_signal(signal.SIGTERM)
        for p in self.procs:
            p.wait(10)
        self.taskd(self.exe, 'uninstall')
        self.assertEqual(json.loads((self.exe / 'config.json').read_text())['auto_reply']['agent'], 'codex')

    def test_blocked_task_delivers_partial_artifacts_and_failure_details(self):
        custom = self.tmp / 'blocked_agent.py'
        source = (HERE / 'fake_agent.py').read_text()
        source = source.replace("'checks': ['result']", "'checks': ['result'], 'final_checks': ['result', 'condition'], 'produces': ['out/result.txt', 'out/condition.json']")
        source = source.replace("(out / 'result.txt').write_text('42\\n')", "(out / 'result.txt').write_text('42\\n')\n    (out / 'condition.json').write_text('{\"ok\": false}')")
        custom.write_text(source)
        script(self.agent, custom)
        self.taskd(self.exe, 'install', '--accept-tasks', '--work-dir', str(self.work), '--executor', 'command',
                   '--executor-command', str(self.agent), '--reviewer', 'none', '--no-requester', '--no-autostart')
        self.taskd(self.req, 'install', '--no-autostart')
        cfg_path = self.exe / 'taskd/config.json'
        cfg = json.loads(cfg_path.read_text());cfg['policy'] = {'max_verify_rounds': 1};cfg_path.write_text(json.dumps(cfg))
        aid = json.loads((self.exe / 'fake_identity.json').read_text())['aid']
        self.serve(self.exe);self.serve(self.req)
        spec_path = self.tmp / 'blocked.json'
        spec_path.write_text(json.dumps({'taskd':'submit','goal':'交付阶段文件并如实报告失败', 'acceptance':[
            {'id':'result','type':'files','pattern':'out/result.txt'},
            {'id':'condition','type':'value','file':'out/condition.json','path':'ok','expect':True}]}))
        tid = json.loads(self.taskd(self.req, 'submit', '--to', aid, '--spec', str(spec_path), env={'CODEX_THREAD_ID': SID}).stdout)['task_id']
        task_path = self.exe / 'taskd/state/tasks' / tid / 'task.json'
        self.assertTrue(wait_for(lambda:task_path.exists() and json.loads(task_path.read_text())['state']=='blocked',30))
        tracked = self.req / 'taskd/state/tracked' / (tid+'.json')
        def received():
            if not tracked.exists():return False
            rec = json.loads(tracked.read_text());return rec.get('stages',{}).get('files',{}).get('state')=='verified'
        self.assertTrue(wait_for(received,30))
        rec = json.loads(tracked.read_text());files=rec['stages']['files']['files']
        names=[f['name'] for f in files]
        self.assertIn('report.json',names);self.assertIn('verification-failures.json',names)
        self.assertTrue(any(n.endswith('-result.txt') for n in names))
        report_path = next(Path(f['path']) for f in files if f['name']=='report.json')
        rep = json.loads(report_path.read_text());self.assertEqual(rep['state'],'blocked');self.assertTrue(rep['partial'])
        failure = next(c for c in rep['checks'] if c['id']=='condition')
        self.assertEqual(failure['outcome'],'fail');self.assertIsNotNone(failure['observed'])
        self.assertGreaterEqual(json.loads(task_path.read_text())['used']['steps'],1)
        self.assertLessEqual(json.loads(task_path.read_text())['used']['steps'],3)

    def test_rejected_task_and_restart(self):
        self.taskd(self.exe, 'install', '--accept-tasks', '--work-dir', str(self.work), '--executor', 'command',
                   '--executor-command', str(self.agent), '--reviewer', 'none', '--no-requester', '--no-autostart')
        self.taskd(self.req, 'install', '--no-autostart')
        exe_aid = json.loads((self.exe / 'fake_identity.json').read_text())['aid']
        p = self.serve(self.exe)
        self.serve(self.req)
        spec = self.tmp / 'bad.json'
        spec.write_text(json.dumps({'goal': 'x', 'acceptance': [{'id': 'a', 'type': 'telepathy'}]}))
        tid = json.loads(self.taskd(self.req, 'submit', '--to', exe_aid, '--spec', str(spec),'--no-callback').stdout)['task_id']
        net = self.tmp / 'net.json'
        self.assertTrue(wait_for(lambda: json.loads(net.read_text())['tasks'][tid]['state'] == 'rejected', 20))
        # A second daemon for the same node refuses to start.
        second = subprocess.run([sys.executable, str(self.exe / 'taskd' / 'app' / 'run_taskd.py'), '--data-dir', str(self.exe),
                                 '--anet', str(self.anet), 'serve'], env=dict(self.env, ANET_DATA_DIR=str(self.exe)),
                                capture_output=True, text=True, timeout=30)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('already running', second.stderr)
        p.send_signal(signal.SIGTERM)
        p.wait(10)


if __name__ == '__main__':
    unittest.main()

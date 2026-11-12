import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class PiPortalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.m = load('pi_portal', ROOT / 'server.py'); self.m.DATA = Path(self.tmp.name)
        self.m.BASE = 'http://127.0.0.1'; self.m.init()
        self.m.hub_action = lambda *args: None
        self.server = self.m.ThreadingHTTPServer(('127.0.0.1', 0), self.m.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.cookie = ''; self.csrf = ''
        token = (self.m.DATA / 'admin-setup-link.txt').read_text().split('=')[-1].strip()
        self.req('/api/auth/register', 'POST', {'token':token,'username':'tester','name':'tester','password':'only-test-password'})
        self.req('/api/auth/login', 'POST', {'username':'tester','password':'only-test-password'})
        self.csrf = self.req('/api/me')[1]['csrf']

    def req(self, path, method='GET', data=None, node=None):
        headers = {'Content-Type':'application/json','Cookie':self.cookie,'X-CSRF-Token':self.csrf}
        if node: headers['Authorization'] = 'Bearer ' + node
        c = HTTPConnection('127.0.0.1', self.server.server_port)
        c.request(method, path, json.dumps(data or {}) if method != 'GET' else None, headers)
        r = c.getresponse(); code = r.status
        if r.getheader('Set-Cookie'): self.cookie = r.getheader('Set-Cookie').split(';')[0]
        value = json.loads(r.read()); c.close(); return code, value

    def test_pi_type_survives_pairing_and_old_codex_heartbeat_cannot_claim_pi_ready(self):
        code, d = self.req('/api/agents', 'POST', {'name':'Pi test','executor':'pi'})
        self.assertEqual(code, 201); record = d['id']
        _, pair = self.req('/api/agents/' + record + '/pair', 'POST')
        self.assertEqual(pair['executor'], 'pi'); self.assertIn('configure-pi', pair['instructions'])
        t = pair['url'].split('=')[-1]; aid = 'b' + 'a' * 58
        self.assertEqual(self.req('/api/pair/info', 'POST', {'token':t})[1]['executor'], 'pi')
        self.req('/api/pair/prepare', 'POST', {'token':t,'aid':aid})
        with self.m.db() as c: challenge = self.m.one(c, 'SELECT challenge FROM agents WHERE id=?', (record,))['challenge']
        self.m.hub_agent = lambda *args: {'summary':challenge}
        code, complete = self.req('/api/pair/complete', 'POST', {'token':t,'aid':aid})
        self.assertEqual(code, 200); self.assertEqual(complete['executor'], 'pi'); nt = complete['node_token']
        self.assertEqual(self.req('/api/node/info', 'POST', node=nt)[1]['executor'], 'pi')
        heartbeat = {'connected':True,'policy_ok':True,'executor_ready':True}
        self.req('/api/node/heartbeat', 'POST', heartbeat, nt)
        self.assertEqual(self.req('/api/agents')[1]['agents'][0]['status'], 'executor_pending')
        self.req('/api/node/heartbeat', 'POST', {**heartbeat,'executor':'pi'}, nt)
        self.assertEqual(self.req('/api/agents')[1]['agents'][0]['status'], 'ready')
        self.assertEqual(self.req('/api/agents/' + record, 'PATCH', {'executor':'codex'})[0], 409)

    def test_executor_change_invalidates_previous_instructions_and_devices_reject_pi(self):
        _, d = self.req('/api/agents', 'POST', {'name':'default Codex'})
        record = d['id']; self.assertEqual(self.req('/api/agents')[1]['agents'][0]['executor'], 'codex')
        _, pair = self.req('/api/agents/' + record + '/pair', 'POST')
        self.assertEqual(self.req('/api/agents/' + record, 'PATCH', {'executor':'pi'})[0], 200)
        self.assertEqual(self.req('/api/pair/info', 'POST', {'token':pair['url'].split('=')[-1]})[0], 404)
        self.assertEqual(self.req('/api/devices', 'POST', {'name':'device','executor':'pi'})[0], 400)
        self.assertEqual(self.req('/api/agents', 'POST', {'name':'invalid','executor':'shell'})[0], 400)


@unittest.skipIf(sys.platform == 'win32', 'Real subprocess checks use a Unix fixture, Windows is simulated separately')
class PiBridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.work = self.root / 'work'; self.work.mkdir()
        shutil.copy2(ROOT / 'pi_bridge.py', self.root / 'pi_bridge.py')
        self.fake = self.root / 'fake-pi'
        self.fake.write_text('#!' + sys.executable + '\nimport sys,json,pathlib,time\n'
            "pathlib.Path('args.json').write_text(json.dumps(sys.argv[1:]))\n"
            "prompt=sys.stdin.read()\n"
            "if 'FAIL' in prompt:\n print('Bearer should-not-leak',file=sys.stderr);sys.exit(3)\n"
            "if 'TIMEOUT' in prompt:time.sleep(30)\n"
            "if 'EMPTY' not in prompt:print('Pi result: '+prompt)\n")
        self.fake.chmod(0o700)
        self.settings = {'mode':'direct','pi_command':str(self.fake),'work_dir':str(self.work),'timeout':2}
        self.save_settings()

    def save_settings(self):
        (self.root / 'pi-executor.json').write_text(json.dumps(self.settings))

    def invoke(self, prompt='task', *extra):
        return subprocess.run([sys.executable, str(self.root / 'pi_bridge.py'), 'exec', '--ephemeral',
            '--skip-git-repo-check', '-s', 'workspace-write', '-o', str(self.work / 'reply'), '-C', str(self.work),
            *extra, '-'], input=prompt, text=True, capture_output=True, timeout=8)

    def test_real_bridge_protocol_keeps_task_off_argv_and_writes_private_reply(self):
        r = self.invoke('private-task', '-c', 'developer_instructions="follow instructions"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.work / 'reply').read_text(), 'Pi result: follow instructions\n\nprivate-task')
        self.assertNotIn('private-task', (self.work / 'args.json').read_text())
        self.assertEqual((self.work / 'reply').stat().st_mode & 0o777, 0o600)

    def test_errors_and_empty_output_never_become_success_or_leak_provider_stderr(self):
        for prompt in ['FAIL','EMPTY']:
            r = self.invoke(prompt); self.assertNotEqual(r.returncode, 0)
            self.assertNotIn('should-not-leak', r.stderr); self.assertFalse((self.work / 'reply').exists())

    def test_timeout_is_failed_and_sandbox_cannot_be_misrepresented(self):
        self.settings['timeout'] = 0.2; self.save_settings()
        r = self.invoke('TIMEOUT'); self.assertNotEqual(r.returncode, 0); self.assertIn('超时', r.stderr)
        self.settings['mode'] = 'sandbox'; self.save_settings()
        r = self.invoke(); self.assertNotEqual(r.returncode, 0); self.assertIn('direct', r.stderr)

    def test_output_symlink_cannot_overwrite_another_file(self):
        protected = self.root / 'protected'; protected.write_text('keep')
        (self.work / 'reply').symlink_to(protected)
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertEqual(protected.read_text(), 'keep')


class PiConnectorTest(unittest.TestCase):
    def setUp(self):
        self.m = load('pi_connector', ROOT / 'connector.py')
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.folder = self.root / 'portal'; self.folder.mkdir()
        (self.folder / 'connection.json').write_text(json.dumps({'portal':'https://example.test','node_token':'test'}))
        self.work = self.root / 'work'; self.work.mkdir()
        self.ar = {'backend':'exec','agent':'codex','command':str(self.m.pi_launcher(self.folder)),
            'work_dir':str(self.work),'admitted':'direct','untrusted':'off'}
        (self.root / 'config.json').write_text(json.dumps({'auto_reply':self.ar}))
        (self.folder / 'pi-executor.json').write_text(json.dumps({'mode':'direct','pi_command':sys.executable,'work_dir':str(self.work)}))
        (self.folder / 'pi_bridge.py').write_text('bridge')
        self.m.pi_launcher(self.folder).write_text('launcher')

    def test_ready_requires_successful_test_of_current_configuration(self):
        self.assertFalse(self.m.pi_ready(self.root, self.ar))
        (self.folder / 'pi-check.json').write_text(json.dumps({'ok':True,'fingerprint':self.m.pi_fingerprint(self.root)}))
        self.assertTrue(self.m.pi_ready(self.root, self.ar))
        (self.folder / 'pi_bridge.py').write_text('modified bridge')
        self.assertFalse(self.m.pi_ready(self.root, self.ar))

    def test_device_cannot_be_turned_into_pi_executor(self):
        with patch.object(self.m.sys, 'argv', ['connector.py','configure-pi','--data-dir',str(self.root),'--anet',sys.executable]), patch.object(self.m, 'remote', return_value={'kind':'device'}):
            with self.assertRaisesRegex(RuntimeError, '调用设备'): self.m.main()
        self.assertEqual(json.loads((self.root / 'config.json').read_text())['auto_reply'], self.ar)

    def test_native_pass_summary_marks_the_current_configuration_checked(self):
        def fake_run(cmd, **kwargs):
            data=json.dumps({'allow':[],'trust':[],'public_capabilities':[]}) if cmd[1:3]==['peers','list'] else 'direct: passed (17-character reply).'
            return subprocess.CompletedProcess(cmd,0,data,'')
        args=['connector.py','test-pi','--data-dir',str(self.root),'--anet',sys.executable]
        with patch.object(self.m.sys,'argv',args),patch.object(self.m,'remote',return_value={'kind':'agent','executor':'pi'}),patch.object(self.m.subprocess,'run',side_effect=fake_run):self.m.main()
        self.assertTrue(self.m.pi_ready(self.root,self.ar))

    def test_refused_native_authorization_restores_working_adapter(self):
        original = (self.folder / 'pi_bridge.py').read_text()
        def fake_run(cmd, **kwargs):
            if cmd[1:3] == ['peers','list']:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({'allow':[],'trust':[],'public_capabilities':[]}), '')
            return subprocess.CompletedProcess(cmd, 1, '', 'refused')
        args = ['connector.py','configure-pi','--data-dir',str(self.root),'--anet',sys.executable,
            '--work-dir',str(self.work),'--mode','direct','--pi-command',sys.executable]
        with patch.object(self.m.sys, 'argv', args), patch.object(self.m, 'remote', return_value={'kind':'agent','executor':'pi'}), patch.object(self.m.sys.stdin, 'isatty', return_value=True), patch.object(self.m.subprocess, 'run', side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, '授权未完成'): self.m.main()
        self.assertEqual((self.folder / 'pi_bridge.py').read_text(), original)


if __name__ == '__main__': unittest.main(verbosity=2)

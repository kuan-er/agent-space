import importlib.util
import json
import os
from pathlib import Path
import re
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
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class OpenPortalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.m = load('open_portal', ROOT / 'server.py');self.m.DATA = Path(self.tmp.name)
        self.m.BASE = 'http://127.0.0.1';self.m.init()
        self.server = self.m.ThreadingHTTPServer(('127.0.0.1', 0), self.m.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.cookie = '';self.csrf = ''

    def req(self, path, method='GET', data=None):
        c = HTTPConnection('127.0.0.1', self.server.server_port)
        c.request(method, path, json.dumps(data or {}) if method != 'GET' else None,
            {'Content-Type':'application/json','Cookie':self.cookie,'X-CSRF-Token':self.csrf})
        r = c.getresponse();code = r.status
        if r.getheader('Set-Cookie'):self.cookie = r.getheader('Set-Cookie').split(';')[0]
        value = json.loads(r.read());c.close();return code, value

    def signup(self, username='new-user'):
        return self.req('/api/auth/register', 'POST', {'username':username,'name':'Test member',
            'password':'test-only-password','password_confirm':'test-only-password','role':'admin','enabled':False})

    def test_public_signup_is_immediate_member_and_cannot_claim_admin(self):
        code, d = self.signup();self.assertEqual(code, 201);self.assertTrue(d['signed_in'])
        _, me = self.req('/api/me');self.assertEqual(me['role'], 'member');self.csrf = me['csrf']
        self.assertEqual(self.req('/api/admin/overview')[0], 403)
        self.assertEqual(self.req('/api/devices', 'POST', {'name':'My caller'})[0], 201)
        self.assertEqual(self.req('/api/agents', 'POST', {'name':'My private agent'})[0], 201)
        self.assertEqual(self.req('/api/agents')[1]['agents'][0]['visibility'], 'private')
        self.assertEqual(self.req('/api/agents', 'POST', {'name':'missing csrf'})[0], 201)
        self.csrf = '';self.assertEqual(self.req('/api/agents', 'POST', {'name':'missing csrf'})[0], 403)
        self.assertEqual(self.signup('NEW-USER')[0], 409)

    def test_invalid_invite_and_mismatched_password_do_not_create_accounts(self):
        fields = {'username':'new-user','name':'User','password':'test-password'}
        self.assertEqual(self.req('/api/auth/register', 'POST', {**fields,'token':'expired'})[0], 404)
        self.assertEqual(self.req('/api/auth/register', 'POST', {**fields,'password_confirm':'different'})[0], 400)
        self.assertEqual(self.req('/api/auth/register', 'POST', {**fields,'username':'bad name'})[0], 400)
        with self.m.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM users').fetchone()[0], 0)

    def test_public_signup_does_not_consume_admin_setup(self):
        self.signup()
        token = (self.m.DATA / 'admin-setup-link.txt').read_text().split('=')[-1].strip()
        self.assertEqual(self.req('/api/auth/register', 'POST', {'token':token,'username':'admin','name':'Admin','password':'test-password'})[0], 200)
        self.m.init()
        with self.m.db() as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0], 1)

    def test_all_harnesses_have_real_onboarding_and_devices_stay_call_only(self):
        self.signup();self.csrf = self.req('/api/me')[1]['csrf']
        config = self.req('/api/config')[1];self.assertTrue(config['registration_open'])
        self.assertEqual({h['id'] for h in config['harnesses']}, {'codex','pi','workbuddy','deepseek','custom'})
        for kind in ['workbuddy','deepseek','custom']:
            code, d = self.req('/api/agents', 'POST', {'name':kind,'executor':kind});self.assertEqual(code, 201)
            record = d['id'];_, pair = self.req('/api/agents/' + record + '/pair', 'POST')
            self.assertIn('configure-harness', pair['instructions']);self.assertIn('--harness ' + kind, pair['instructions'])
            if kind == 'workbuddy':self.assertIn('--credential-file', pair['instructions'])
            old_token = pair['url'].split('=')[-1]
            self.assertEqual(self.req('/api/agents/' + record, 'PATCH', {'executor':'pi'})[0], 200)
            self.assertEqual(self.req('/api/pair/info', 'POST', {'token':old_token})[0], 404)
            self.assertEqual(self.req('/api/devices', 'POST', {'name':'device','executor':kind})[0], 400)

    def test_legacy_check_migration_preserves_every_existing_value(self):
        self.signup();self.csrf = self.req('/api/me')[1]['csrf']
        self.req('/api/agents', 'POST', {'name':'Existing Pi','executor':'pi'})
        with self.m.db() as c:
            c.execute("UPDATE agents SET aid='existing-aid',node_hash='credential-hash',pair_hash='pair-hash',last_seen=123,policy_ok=1,executor_ready=1")
            schema = c.execute("SELECT sql FROM sqlite_master WHERE name='agents'").fetchone()[0]
            old = schema.rstrip()[:-1] + ",CHECK(executor IN ('codex','pi')))"
            c.execute(re.sub(r'CREATE TABLE agents', 'CREATE TABLE agents_old_check', old, count=1))
            c.execute('INSERT INTO agents_old_check SELECT * FROM agents');c.execute('DROP TABLE agents')
            c.execute('ALTER TABLE agents_old_check RENAME TO agents')
            before = [dict(r) for r in c.execute('SELECT * FROM agents')]
        self.m.init();self.m.init()
        with self.m.db() as c:
            self.assertEqual([dict(r) for r in c.execute('SELECT * FROM agents')], before)
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(self.req('/api/agents', 'POST', {'name':'New DeepSeek','executor':'deepseek'})[0], 201)


@unittest.skipIf(sys.platform == 'win32', 'Subprocess fixtures use Unix executable scripts')
class HarnessBridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name);self.work = self.root / 'work';self.work.mkdir()
        for name in ['harness_bridge.py','pi_bridge.py']:shutil.copy2(ROOT / name, self.root / name)
        self.fake = self.root / 'fake-harness'
        self.fake.write_text('#!' + sys.executable + '\nimport sys,json,pathlib,time\n'
            "pathlib.Path('args.json').write_text(json.dumps(sys.argv[1:]))\n"
            "task=sys.stdin.read()\n"
            "if 'FAIL' in task:print('Bearer secret-must-not-leak',file=sys.stderr);sys.exit(2)\n"
            "if 'TIMEOUT' in task:time.sleep(10)\n"
            "if '--json' in sys.argv:\n"
            " print(json.dumps({'type':'text','text':'intermediate'}))\n"
            " if 'NO_FINAL' not in task:print(json.dumps({'type':'final','reason':{'kind':'failed' if 'BAD_FINAL' in task else 'completed'},'text':'Final: '+task}))\n"
            "elif 'EMPTY' not in task:print('Final: '+task)\n")
        self.fake.chmod(0o700)
        self.settings = {'harness':'deepseek','mode':'direct','command':str(self.fake),'work_dir':str(self.work),'timeout':1}

    def invoke(self, prompt, *extra):
        (self.root / 'harness-executor.json').write_text(json.dumps(self.settings))
        return subprocess.run([sys.executable,str(self.root / 'harness_bridge.py'),'exec','--ephemeral',
            '--skip-git-repo-check','-s','workspace-write','-C',str(self.work),'-o',str(self.work / 'reply'),*extra,'-'],
            input=prompt,text=True,capture_output=True,timeout=5)

    def test_deepseek_requires_terminal_completion_and_keeps_task_off_argv(self):
        r = self.invoke('private-task','-c','developer_instructions="fixed"');self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.work / 'reply').read_text(), 'Final: fixed\n\nprivate-task')
        args = json.loads((self.work / 'args.json').read_text());self.assertNotIn('private-task', args)
        self.assertIn('--json', args);self.assertIn('never', args)
        self.assertEqual((self.work / 'reply').stat().st_mode & 0o777, 0o600)

    def test_intermediate_failed_and_missing_final_are_not_success(self):
        for prompt in ['BAD_FINAL','NO_FINAL','FAIL']:
            r = self.invoke(prompt);self.assertNotEqual(r.returncode, 0)
            self.assertNotIn('secret-must-not-leak', r.stderr);self.assertFalse((self.work / 'reply').exists())

    def test_custom_stdin_contract_errors_timeout_and_private_output(self):
        self.settings.update(harness='custom',args=['--fixed'],timeout=0.1)
        for prompt in ['EMPTY','FAIL','TIMEOUT']:
            self.assertNotEqual(self.invoke(prompt).returncode, 0);self.assertFalse((self.work / 'reply').exists())
        self.settings['timeout'] = 2
        self.assertEqual(self.invoke('private-task').returncode, 0)
        self.assertEqual(json.loads((self.work / 'args.json').read_text()), ['--fixed'])
        self.assertEqual((self.work / 'reply').read_text(), 'Final: private-task')

    def test_reply_symlink_and_directory_mismatch_cannot_overwrite(self):
        external = self.root / 'external';external.write_text('keep')
        (self.work / 'reply').symlink_to(external)
        self.assertNotEqual(self.invoke('task').returncode, 0);self.assertEqual(external.read_text(), 'keep')
        self.settings['work_dir'] = str(self.root)
        self.assertNotEqual(self.invoke('task').returncode, 0)


class WorkBuddyAdapterTest(unittest.TestCase):
    def setUp(self):
        pi = load('pi_bridge_test_dependency', ROOT / 'pi_bridge.py')
        with patch.dict(sys.modules, {'pi_bridge':pi}):self.m = load('workbuddy_adapter', ROOT / 'harness_bridge.py')
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name);self.credential = self.root / 'authorization.json'
        self.credential.write_text(json.dumps({'access_token':'only-test-token'}))
        self.settings = {'credential_file':str(self.credential),'timeout':0.05}
        self.calls = [];self.task = ''

    def fake(self, token, path, data=None, timeout=15):
        self.assertEqual(token, 'only-test-token');self.calls.append((path,data))
        if path == '/localassistant':return {'online':True}
        if data is not None:self.task = data['content'];return {'message_id':'message-1'}
        marker = re.search(r'AGENT_SPACE_[a-f0-9]+_BEGIN', self.task)[0]
        return {'messages':[{'role':'assistant','content':['old unrelated reply']},
            {'role':'assistant','content':[marker + '\nActual result\n' + marker.replace('_BEGIN','_END')]}]}

    def test_workbuddy_returns_only_correlated_final_and_clears_pending(self):
        with patch.object(self.m, 'workbuddy_request', side_effect=self.fake):
            self.assertEqual(self.m.run_workbuddy(self.settings,str(self.root),'task',self.root), 'Actual result')
        self.assertFalse((self.root / 'workbuddy-pending.json').exists())
        self.assertEqual(self.calls[1][1]['msg_type'], 'text')
        self.assertIn('message_id=message-1', self.calls[-1][0])

    def test_timeout_retains_pending_and_refuses_duplicate_send(self):
        def no_final(*args, **kwargs):
            value = self.fake(*args, **kwargs)
            if 'messages' in value:value['messages'] = [{'role':'assistant','content':['still working']}]
            return value
        with patch.object(self.m, 'workbuddy_request', side_effect=no_final):
            with self.assertRaisesRegex(RuntimeError, '超时'):self.m.run_workbuddy(self.settings,str(self.root),'task',self.root)
            count = len(self.calls)
            with self.assertRaisesRegex(RuntimeError, '未确认'):self.m.run_workbuddy(self.settings,str(self.root),'task',self.root)
            self.assertEqual(len(self.calls), count)
        self.assertTrue((self.root / 'workbuddy-pending.json').exists())


class HarnessConnectorTest(unittest.TestCase):
    def setUp(self):
        self.m = load('harness_connector', ROOT / 'connector.py')
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve();self.folder = self.root / 'portal';self.folder.mkdir()
        self.work = self.root / 'work';self.work.mkdir()
        self.ar = {'backend':'exec','agent':'codex','command':str(self.m.harness_launcher(self.folder)),
            'work_dir':str(self.work),'admitted':'direct','untrusted':'off','model':''}
        (self.root / 'config.json').write_text(json.dumps({'auto_reply':self.ar}))
        (self.folder / 'connection.json').write_text(json.dumps({'portal':'https://example.test','node_token':'test'}))
        (self.folder / 'harness-executor.json').write_text(json.dumps({'harness':'deepseek','mode':'direct','command':sys.executable,'work_dir':str(self.work)}))
        for p in ['harness_bridge.py','pi_bridge.py',self.m.harness_launcher(self.folder).name]:(self.folder / p).write_text('original')

    def test_ready_requires_correct_type_current_fingerprint_and_selftest(self):
        self.assertFalse(self.m.harness_ready(self.root,self.ar,'deepseek'))
        (self.folder / 'harness-check.json').write_text(json.dumps({'ok':True,'harness':'deepseek','fingerprint':self.m.harness_fingerprint(self.root)}))
        self.assertTrue(self.m.harness_ready(self.root,self.ar,'deepseek'))
        self.assertFalse(self.m.harness_ready(self.root,self.ar,'custom'))
        (self.folder / 'pi_bridge.py').write_text('changed')
        self.assertFalse(self.m.harness_ready(self.root,self.ar,'deepseek'))

    def test_device_rejected_and_native_refusal_restores_all_adapter_files(self):
        args = ['connector.py','configure-harness','--data-dir',str(self.root),'--anet',sys.executable,
            '--harness','deepseek','--work-dir',str(self.work),'--mode','direct','--command',sys.executable]
        with patch.object(self.m.sys,'argv',args),patch.object(self.m,'remote',return_value={'kind':'device'}):
            with self.assertRaisesRegex(RuntimeError, '调用设备'):self.m.main()
        def fake(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0 if cmd[1:3]==['peers','list'] else 1,
                json.dumps({'allow':[],'trust':[],'public_capabilities':[]}), '')
        with patch.object(self.m.sys,'argv',args),patch.object(self.m,'remote',return_value={'kind':'agent','executor':'deepseek'}),patch.object(self.m.sys.stdin,'isatty',return_value=True),patch.object(self.m.subprocess,'run',side_effect=fake):
            with self.assertRaisesRegex(RuntimeError, '授权未完成'):self.m.main()
        self.assertEqual((self.folder / 'harness_bridge.py').read_text(), 'original')
        self.assertEqual((self.folder / 'pi_bridge.py').read_text(), 'original')
        self.assertEqual(json.loads((self.root / 'config.json').read_text())['auto_reply'], self.ar)


if __name__ == '__main__':unittest.main(verbosity=2)

"""Installation telemetry privacy, owner isolation, promotion and real setup orchestration."""
import contextlib,importlib.util,io,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
ROOT=Path(__file__).parents[1]
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
fixture=load('install_fixture',ROOT/'tests/test_pair_recovery.py');s=fixture.s;m=fixture.m

def sample():return {'schema':1,'run_id':'a'*24,'os':'Windows','arch':'AMD64','connector_version':'0.7.0','python_version':'3.14.0','events':[{'stage':'pair','result':'failed','error':'acl','at':s.now(),'elapsed_ms':122}]}

class InstallationAPI(unittest.TestCase):
 def setUp(self):
  self.t=fixture.RecoveryTest();self.t.setUp();self.addCleanup(self.t.doCleanups)
 def reports(self):
  req=Request(self.t.base+'/api/connections/'+self.t.record+'/diagnostics',headers={'Cookie':'agent_portal_session='+self.t.owner})
  with urlopen(req) as response:return json.load(response)['reports']
 def test_prebinding_upload_preserves_token_and_filters_secrets(self):
  raw=sample();raw.update(token='secret',argv=['Bearer bad'],stdout='private content',path='C:/private');raw['events'][0].update(message='sk-do-not-store',checks={'mcp':'unverified','token':'secret'})
  code,reply=self.t.request('/api/pair/diagnostics',{'token':self.t.token,'report':raw});self.assertEqual(code,200)
  self.assertIsNotNone(self.t.row()['pair_hash']);self.assertIsNone(self.t.row()['aid'])
  found=self.reports()
  value=found[0]['report'];self.assertNotIn('secret',json.dumps(value));self.assertNotIn('private',json.dumps(value));self.assertEqual(value['events'][0]['checks'],{'mcp':'unverified'})
  self.assertEqual(self.t.request('/api/connections/'+self.t.record+'/diagnostics',owner=self.t.other)[0],404)
  self.assertEqual(self.t.request('/api/connections/'+self.t.record+'/diagnostics',{'report':raw},owner=self.t.other)[0],404)
 def test_postbinding_report_same_run_upserts_and_is_private(self):
  state=self.t.connect();raw=sample()
  for _ in range(2):self.assertTrue(m.remote(self.t.base,'/api/node/diagnostics',{'report':raw},state['node_token'])['ok'])
  self.assertEqual(len(self.reports()),1)
  with self.assertRaises(m.RemoteError):m.remote(self.t.base,'/api/node/diagnostics',{'report':raw},'wrong')
  self.assertEqual(self.t.request('/api/pair/diagnostics',{'token':self.t.token,'report':raw})[0],404)
 def test_limit_retention_and_invalid_schema(self):
  state=self.t.connect()
  for i in range(12):
   raw=sample();raw['run_id']=format(i,'024x');m.remote(self.t.base,'/api/node/diagnostics',{'report':raw},state['node_token'])
  self.assertEqual(len(self.reports()),10)
  bad=sample();bad['events'][0]['stage']='private-file-content';self.assertEqual(self.t.request('/api/connections/'+self.t.record+'/diagnostics',{'report':bad},owner=self.t.owner)[0],400)
  with s.db() as c:c.execute('UPDATE install_reports SET updated_at=?',(s.now()-31*86400,))
  s.cleanup_once();self.assertEqual(self.reports(),[])
 def test_enable_executor_requires_owner_explicit_choice_preserves_identity(self):
  state=self.t.connect();before=self.t.row()
  path='/api/connections/'+self.t.record+'/enable-executor'
  self.assertEqual(self.t.request(path,{'executor':'deepseek'},owner=self.t.owner)[0],400)
  self.assertEqual(self.t.request(path,{'executor':'deepseek','confirm':True},owner=self.t.other)[0],404)
  self.assertEqual(self.t.request(path,{'executor':'deepseek','confirm':True},owner=self.t.owner)[0],200)
  after=self.t.row()
  for key in ['id','owner_id','aid','node_hash','created_at']:self.assertEqual(before[key],after[key])
  self.assertEqual(after['kind'],'agent');self.assertEqual(after['executor'],'deepseek');self.assertEqual(after['executor_ready'],0)
  self.assertEqual(m.remote(self.t.base,'/api/node/info',{},state['node_token'])['executor'],'deepseek')
  with s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM agents').fetchone()[0],1)

class InstallationClient(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.env=patch.dict(os.environ,{'AGENT_SPACE_INSTALL_RUN_ID':'b'*24});self.env.start();self.addCleanup(self.env.stop)
 def test_local_log_and_server_filter_do_not_copy_error_or_command_text(self):
  secret=RuntimeError('Bearer private-secret token=do-not-store')
  m.diagnostic_event(self.root,'pair','failed',m.diagnostic_error(secret),checks={'mcp':'unverified','secret':'value'})
  raw=m.diagnostic_report(self.root);text=json.dumps(raw);self.assertNotIn('private-secret',text);self.assertNotIn('value',text)
  self.assertEqual(raw['events'][0]['error'],'unknown');self.assertEqual((self.root/'portal/install-logs'/('b'*24+'.json')).stat().st_mode&0o777,0o600)
 def test_no_report_flag_keeps_all_network_calls_off(self):
  m.write_private(self.root/'portal/diagnostics-off','1');m.diagnostic_event(self.root,'pair','failed')
  with patch.object(m,'remote',side_effect=AssertionError('must not send')):self.assertIsNone(m.upload_diagnostics(SimpleNamespace(data_dir=self.root,portal='https://example.test')))
 def test_setup_device_does_not_configure_executor(self):
  folder=self.root/'portal';folder.mkdir();(folder/'connection.json').write_text(json.dumps({'portal':'https://example.test','node_token':'secret'}))
  args=SimpleNamespace(data_dir=self.root,anet='/anet',work_dir=None,mode=None)
  commands=[]
  def invoke(cmd):commands.append(cmd[2]);return 0
  info={'kind':'device','node_connected':True,'last_seen':s.now()}
  with patch.object(m,'pair_node'),patch.object(m,'remote',return_value=info),patch.object(m.subprocess,'call',side_effect=invoke),contextlib.redirect_stdout(io.StringIO()):
   m.setup_connection(args,lambda *a,**k:{},lambda:{'hub':{'state':'registered'}},lambda:None)
  self.assertEqual(commands,['install']);self.assertEqual(m.diagnostic_report(self.root)['events'][-1]['checks']['mcp'],'unverified')
 def test_native_exe_is_preserved_and_binary_rollback_supported(self):
  launcher=self.root/'harness-bridge.exe';launcher.write_bytes(b'MZ\x00\xffexisting')
  with patch.object(m,'fetch_bytes',side_effect=AssertionError('must preserve')):m.install_native_launcher(self.root,self.root/'harness_bridge.py',launcher,'https://example.test')
  self.assertEqual(launcher.read_bytes(),b'MZ\x00\xffexisting');m.write_private(self.root/'backup.exe',launcher.read_bytes());self.assertEqual((self.root/'backup.exe').read_bytes(),launcher.read_bytes())
 def test_native_tamper_is_rejected_before_write(self):
  with patch.object(m,'fetch_bytes',return_value=b'MZtampered'),self.assertRaisesRegex(RuntimeError,'校验失败'):m.install_native_launcher(self.root,self.root/'harness_bridge.py',self.root/'harness-bridge.exe','https://example.test')
  self.assertFalse((self.root/'harness-bridge.exe').exists())
 def test_deepseek_profile_precedes_permission_args_and_key_is_never_collected(self):
  sys.path.insert(0,str(ROOT));self.addCleanup(lambda:sys.path.remove(str(ROOT)));bridge=load('install_bridge',ROOT/'harness_bridge.py')
  command=self.root/'fake';command.write_text('#!'+sys.executable+'\nimport sys,json,pathlib\npathlib.Path("args.json").write_text(json.dumps(sys.argv[1:]))\nsys.stdin.read()\nprint(json.dumps({"type":"final","reason":{"kind":"completed"},"text":"ok"}))\n');command.chmod(0o700)
  settings={'harness':'deepseek','command':str(command),'args':['--permission-mode','workspace-write']}
  self.assertEqual(bridge.run_command(settings,str(self.root),'private-task'),'ok');args=json.loads((self.root/'args.json').read_text());self.assertEqual(args[:4],['--profile','headless','--permission-mode','workspace-write'])
  settings['args']=['--profile','headless']
  with self.assertRaisesRegex(RuntimeError,'不能重复'):bridge.run_command(settings,str(self.root),'task')

if __name__=='__main__':unittest.main(verbosity=2)

class WindowsInstallRegression(unittest.TestCase):
 def test_pinned_native_binary_is_written_as_binary_and_private(self):
  binary=Path(os.environ.get('AGENT_SPACE_NATIVE_TEST_BINARY', str(ROOT/'native-launcher/harness-launcher-windows-amd64.exe')))
  if not binary.exists():self.skipTest('prebuilt operator artifact not present')
  with tempfile.TemporaryDirectory() as d:
   folder=Path(d)
   with patch.object(m.platform,'machine',return_value='AMD64'),patch.object(m,'fetch_bytes',return_value=binary.read_bytes()):m.install_native_launcher(folder,folder/'harness_bridge.py',folder/'harness-bridge.exe','https://example.test')
   self.assertTrue((folder/'harness-bridge.exe').read_bytes().startswith(b'MZ'));self.assertEqual((folder/'harness-bridge.exe').stat().st_mode&0o777,0o600)
   self.assertEqual(json.loads((folder/'harness-launcher.json').read_text())['bridge'],str(folder/'harness_bridge.py'))
 def test_known_screenshot_failures_have_safe_categories(self):
  for text,code in [('DeepSeek headless 模型授权未就绪 token=secret','model_auth'),('原生桥接校验失败','native_launcher'),('DeepSeek 固定参数不能重复 profile','dsh_profile')]:
   self.assertEqual(m.diagnostic_error(RuntimeError(text)),code)
 def test_windows_selects_native_exe(self):
  with patch.object(m.sys,'platform','win32'):
   self.assertEqual(m.harness_launcher(Path('/private')).suffix,'.exe');self.assertEqual(m.pi_launcher(Path('/private')).suffix,'.exe')

class DiagnosticCredentialBoundary(unittest.TestCase):
 def test_wrong_portal_argument_cannot_receive_saved_node_token(self):
  with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'AGENT_SPACE_INSTALL_RUN_ID':'e'*24}):
   root=Path(d);m.write_private(root/'portal/connection.json',json.dumps({'portal':'https://original.example','node_token':'private-node'}));m.diagnostic_event(root,'setup','failed','identity_mismatch')
   calls=[]
   def remote(base,path,data,credential=None):calls.append((base,credential));return {'report_id':'safe'}
   with patch.object(m,'remote',side_effect=remote):m.upload_diagnostics(SimpleNamespace(data_dir=root,portal='https://wrong.example',no_report=False))
   self.assertEqual(calls,[('https://original.example','private-node')])
 def test_explicit_optout_is_honored_even_without_marker(self):
  with tempfile.TemporaryDirectory() as d,patch.object(m,'remote',side_effect=AssertionError('must not upload')):
   self.assertIsNone(m.upload_diagnostics(SimpleNamespace(data_dir=Path(d),portal='https://example.test',no_report=True)))
 def test_redirect_never_forwards_bearer_to_another_endpoint(self):
  from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
  import threading
  calls=[]
  class H(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_POST(self):
    calls.append(self.path);self.send_response(307);self.send_header('Location','/stolen');self.end_headers()
  server=ThreadingHTTPServer(('127.0.0.1',0),H);threading.Thread(target=server.serve_forever,daemon=True).start()
  try:
   with self.assertRaisesRegex(m.RemoteError,'重定向'):m.remote('http://127.0.0.1:'+str(server.server_port),'/api/node/diagnostics',{},'private-node')
   self.assertEqual(calls,['/api/node/diagnostics'])
  finally:server.shutdown();server.server_close()

class PendingDiagnosticRecovery(unittest.TestCase):
 def test_pending_credential_can_report_when_active_file_was_not_saved(self):
  with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'AGENT_SPACE_INSTALL_RUN_ID':'f'*24}):
   root=Path(d)
   for name,key in [('connection.json','old'),('pair-pending.json','new')]:m.write_private(root/'portal'/name,json.dumps({'portal':'https://original.example','node_token':key}))
   m.diagnostic_event(root,'pair','failed','permission');calls=[]
   def remote(base,path,data,credential=None):
    calls.append(credential)
    if credential=='old':raise m.RemoteError('expired',401)
    return {'report_id':'safe'}
   with patch.object(m,'remote',side_effect=remote):self.assertEqual(m.upload_diagnostics(SimpleNamespace(data_dir=root,portal=None))['report_id'],'safe')
   self.assertEqual(calls,['old','new'])

class ReportRetryRegression(unittest.TestCase):
 def test_report_retries_original_run_instead_of_empty_new_report(self):
  with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'AGENT_SPACE_INSTALL_RUN_ID':'a'*24}):
   root=Path(d);m.diagnostic_event(root,'configure-harness','failed','native_launcher');original=m.diagnostic_report(root)
   os.environ.pop('AGENT_SPACE_INSTALL_RUN_ID');args=SimpleNamespace(action='report',data_dir=root,portal=None);sent=[]
   with patch.object(m,'LAST_ARGS',args),patch.object(m,'main'),patch.object(m,'upload_diagnostics',side_effect=lambda a:sent.append(m.diagnostic_report(a.data_dir))):self.assertEqual(m.cli_entry(),0)
   self.assertEqual(sent,[original]);self.assertEqual(len(list((root/'portal/install-logs').glob('*.json'))),1)

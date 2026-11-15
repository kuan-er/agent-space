import hashlib,importlib.util,io,json,tempfile,unittest,ctypes,subprocess,sys,os,time,threading,http.server
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('updater',ROOT/'updater.py');u=importlib.util.module_from_spec(spec);spec.loader.exec_module(u)

class UpdaterTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='anet-updater-unit-');self.root=Path(self.temp.name);self.data=self.root/'data';self.front=self.root/'frontend';self.front.mkdir();self.data.mkdir();self.runtime=self.front/'runtime.json';self.bytes=b'MZ'+b'payload'*200
  self.release={'schema':'anet-desktop-release/1','version':'0.2.0-preview.5','channel':'preview','available':True,'minimum_version':'0.1.0','windows':{'amd64':{'url':u.ORIGIN+'/downloads/ANetSetup-0.2.0-preview.5.exe','bytes':len(self.bytes),'sha256':hashlib.sha256(self.bytes).hexdigest(),'authenticode':'unsigned'}},'signature':{'status':'unsigned'}}
  runtime={'schema':'anet-desktop-runtime/1','persistent_root':str(self.data),'python_exe':'python.exe'};u.atomic(self.runtime,runtime);u.atomic(self.front/'receipt.json',{'schema':'anet-desktop-installer/1','runtime':runtime});u.atomic(self.data/'current.json',{'release':'0.2.0-preview.4'})
 def tearDown(self):self.temp.cleanup()
 def network(self,url):return io.BytesIO(json.dumps(self.release).encode() if url==u.ENDPOINT else self.bytes)
 def test_check_read_only(self):
  with patch.object(u,'network',self.network):r=u.check(self.runtime)
  self.assertTrue(r['available']);self.assertFalse((self.data/'updates').exists())
 def test_download_and_repeat_hash_checked(self):
  with patch.object(u,'network',self.network):
   a=u.download(self.runtime);b=u.download(self.runtime)
  self.assertEqual(a['state'],'staged');self.assertEqual(b['state'],'staged');self.assertEqual(u.read(self.data/'current.json')['release'],'0.2.0-preview.4')
 def test_download_corruption_not_staged(self):
  with patch.object(u,'network',lambda url:io.BytesIO(json.dumps(self.release).encode() if url==u.ENDPOINT else b'x'*len(self.bytes))):
   with self.assertRaisesRegex(ValueError,'download_integrity'):u.download(self.runtime)
  self.assertFalse((self.data/'updates/staged.json').exists());self.assertEqual([p.name for p in (self.data/'updates').iterdir()],['operation.lock'])
 def test_staged_update_remains_available_offline(self):
  with patch.object(u,'network',self.network):u.download(self.runtime)
  with patch.object(u,'network',side_effect=AssertionError('network must not be needed')):
   self.assertEqual(u.check(self.runtime)['state'],'staged');self.assertEqual(u.download(self.runtime)['state'],'staged')
 def test_native_lock_excludes_another_process(self):
  script="import sys;sys.path.insert(0,"+repr(str(ROOT))+");import updater;from pathlib import Path\nwith updater.update_lock(Path("+repr(str(self.data))+")):print('unexpected_second_owner')"
  with u.update_lock(self.data):proc=subprocess.run([sys.executable,'-c',script],capture_output=True,timeout=10)
  self.assertNotEqual(proc.returncode,0);self.assertIn(b'update_operation_already_running',proc.stderr);self.assertNotIn(b'unexpected_second_owner',proc.stdout)
 def test_wait_for_real_owned_process_exit(self):
  proc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.15)'])
  try:u.wait_exit(proc.pid);self.assertEqual(proc.wait(timeout=2),0)
  finally:
   if proc.poll() is None:proc.terminate();proc.wait()
 def test_process_access_denial_does_not_count_as_exit(self):
  kernel=type('Kernel',(),{})();kernel.OpenProcess=unittest.mock.Mock(return_value=0);kernel.WaitForSingleObject=unittest.mock.Mock();kernel.CloseHandle=unittest.mock.Mock()
  with patch.object(ctypes,'WinDLL',return_value=kernel),patch.object(ctypes,'get_last_error',return_value=5):
   with self.assertRaises(OSError):u.wait_exit(12345)
  kernel.WaitForSingleObject.assert_not_called()
 def test_worker_exit_during_image_query_requires_same_handle_signal(self):
  for signals,exited in (([0x102,0],True),([0x102,0x102],False)):
   kernel=type('Kernel',(),{})()
   for name in ('OpenProcess','CloseHandle','GetProcessTimes','QueryFullProcessImageNameW','WaitForSingleObject'):setattr(kernel,name,unittest.mock.Mock())
   kernel.OpenProcess.return_value=123;kernel.GetProcessTimes.return_value=True;kernel.QueryFullProcessImageNameW.return_value=False;kernel.WaitForSingleObject.side_effect=signals
   with patch.object(ctypes,'WinDLL',return_value=kernel),patch.object(ctypes,'get_last_error',return_value=5):
    if exited:self.assertIsNone(u.process_token(99))
    else:
     with self.assertRaises(PermissionError):u.process_token(99)
   self.assertEqual(kernel.WaitForSingleObject.call_args_list,[unittest.mock.call(123,0),unittest.mock.call(123,0)])
 def test_stage_tamper_not_executed(self):
  with patch.object(u,'network',self.network):u.download(self.runtime)
  stage=u.read(self.data/'updates/staged.json');Path(stage['path']).write_bytes(b'bad')
  with patch.object(u.subprocess,'Popen') as launch:
   with self.assertRaisesRegex(ValueError,'integrity'):u.schedule(self.runtime)
   launch.assert_not_called()
 def test_installer_failure_keeps_current_and_records_attention(self):
  with patch.object(u,'network',self.network):u.download(self.runtime)
  failed=type('Process',(),{'returncode':1})()
  with patch.object(u.subprocess,'run',return_value=failed):
   with self.assertRaisesRegex(RuntimeError,'activation_not_confirmed'):u.finish(self.runtime)
  self.assertEqual(u.read(self.data/'current.json')['release'],'0.2.0-preview.4');self.assertEqual(u.read(self.data/'updates/apply.json')['state'],'needs_attention')
 def test_fixture_update_cannot_escape_into_real_user_install(self):
  with patch.object(u,'network',self.network):u.download(self.runtime)
  u.atomic(self.data/'current.json',{'release':'0.2.0-preview.4','fixture':True,'fixture_root':str(self.root/'somewhere-else')})
  with patch.object(u.subprocess,'run') as run:
   with self.assertRaisesRegex(ValueError,'fixture_update_root_mismatch'):u.finish(self.runtime)
   run.assert_not_called()
 def test_downgrade_not_executed(self):
  with patch.object(u,'network',self.network):u.download(self.runtime)
  u.atomic(self.data/'current.json',{'release':'0.2.0-preview.6'})
  with self.assertRaisesRegex(ValueError,'downgrade'):u.schedule(self.runtime)
 def test_cross_host_refused(self):
  self.release['windows']['amd64']['url']='https://example.org/downloads/ANetSetup-0.2.0-preview.5.exe'
  with self.assertRaisesRegex(ValueError,'origin'):u.manifest(self.release,'0.2.0-preview.4')
 def test_http_refused(self):
  with self.assertRaisesRegex(ValueError,'origin'):u.same_origin(u.ORIGIN.replace('https:','http:')+'/anything')
 def test_redirect_cross_host_refused(self):
  with self.assertRaisesRegex(ValueError,'origin'):u.FixedRedirect().redirect_request(None,None,302,'',{},'https://example.org/payload')
 def test_wrong_channel_refused(self):
  self.release['channel']='nightly'
  with self.assertRaisesRegex(ValueError,'channel'):u.manifest(self.release,'0.2.0-preview.4')
 def test_minimum_version(self):
  self.release['minimum_version']='0.3.0'
  with self.assertRaisesRegex(ValueError,'minimum'):u.manifest(self.release,'0.2.0-preview.4')
 def test_release_version_sort(self):
  self.assertGreater(u.version_key('0.2.0'),u.version_key('0.2.0-preview.100'));self.assertGreater(u.version_key('0.2.0-preview.10'),u.version_key('0.2.0-preview.9'))
 def test_missing_sha_refused(self):
  self.release['windows']['amd64']['sha256']=''
  with self.assertRaisesRegex(ValueError,'integrity'):u.manifest(self.release,'0.2.0-preview.4')
 def test_path_query_refused(self):
  self.release['windows']['amd64']['url']+='?x=1'
  with self.assertRaisesRegex(ValueError,'origin'):u.manifest(self.release,'0.2.0-preview.4')
 def test_unpublished_release_current(self):
  self.release['available']=False;self.assertIsNone(u.manifest(self.release,'0.2.0-preview.4'))
 def test_stable_newer_no_downgrade(self):self.assertIsNone(u.manifest(self.release,'0.2.0'))
 def test_product_sources_and_status_reasons_preserve_unicode(self):
  for source in [ROOT/'updater.py',ROOT/'install_engine.py',ROOT/'build.py',ROOT/'src/Setup.cs']:
   text=source.read_text(encoding='utf-8');self.assertNotRegex(text,r'\?{3,}|\ufffd',str(source))
  with patch.object(u,'network',self.network):
   result=u.check(self.runtime);ready=u.download(self.runtime)
  for value in (result,ready):
   self.assertNotRegex(value['reason'],r'\?{3,}|\ufffd');self.assertRegex(value['reason'],r'[\u4e00-\u9fff]')
  info=u.manifest(self.release,'0.2.0-preview.4');u.atomic(self.data/'updates/download.json',{'release':info,'state':'paused'})
  value=u.download_status(self.data,{'release':'0.2.0-preview.4'});self.assertNotRegex(value['reason'],r'\?{3,}|\ufffd');self.assertIn('已保留进度',value['reason'])
 def partial(self,body):
  info=u.manifest(self.release,'0.2.0-preview.4');folder=self.data/'updates';folder.mkdir(exist_ok=True);target=folder/'ANetSetup-0.2.0-preview.5.exe.partial';target.write_bytes(body);u.atomic(target.with_name(target.name+'.json'),{'schema':u.SCHEMA,'release':info,'etag':'"fixture-etag"'});return target
 def response(self,body,status,headers):
  stream=io.BytesIO(body);stream.status=status;stream.headers=headers;return stream
 def test_low_download_space_does_not_start_worker_or_package_get(self):
  calls=[]
  def network(url,*args):
   calls.append(url);self.assertEqual(url,u.ENDPOINT);return self.network(url)
  with patch.object(u,'network',network),patch.object(u.shutil,'disk_usage',return_value=SimpleNamespace(free=u.DOWNLOAD_RESERVE_BYTES+len(self.bytes)-1)),patch.object(u,'launch_download',side_effect=AssertionError('worker must not start')):
   value=u.request_download(self.runtime)
  self.assertEqual(value['state'],'needs_space');self.assertEqual(calls,[u.ENDPOINT]);self.assertEqual(value['required_free_bytes'],len(self.bytes)+u.DOWNLOAD_RESERVE_BYTES)
  self.assertFalse(any((self.data/'updates').glob('*.partial')));self.assertFalse((self.data/'updates/download.json').exists())
 def test_exact_matching_partial_reduces_download_budget_and_retains_bytes_on_low_space(self):
  partial=self.partial(self.bytes[:300]);info=u.manifest(self.release,'0.2.0-preview.4');expected=len(self.bytes)-300+u.DOWNLOAD_RESERVE_BYTES
  with patch.object(u.shutil,'disk_usage',return_value=SimpleNamespace(free=expected-1)),patch.object(u,'network',self.network),patch.object(u,'launch_download',side_effect=AssertionError('worker must not start')):
   budget=u.download_budget(self.data,info);value=u.request_download(self.runtime)
  self.assertEqual(budget['required_free_bytes'],expected);self.assertEqual(value['downloaded_bytes'],300);self.assertEqual(partial.read_bytes(),self.bytes[:300]);self.assertEqual(value['state'],'needs_space')
 def test_foreign_partial_never_reduces_download_budget_or_is_destroyed_on_space_hold(self):
  partial=self.partial(self.bytes[:300]);self.bytes=b'MZ'+b'NEWFILE'*200;self.release['windows']['amd64'].update(bytes=len(self.bytes),sha256=hashlib.sha256(self.bytes).hexdigest());info=u.manifest(self.release,'0.2.0-preview.4')
  previous=partial.read_bytes();metadata=partial.with_name(partial.name+'.json').read_bytes()
  with patch.object(u.shutil,'disk_usage',return_value=SimpleNamespace(free=len(self.bytes)+u.DOWNLOAD_RESERVE_BYTES-1)),patch.object(u,'network',self.network),patch.object(u,'launch_download',side_effect=AssertionError('worker must not start')):value=u.request_download(self.runtime)
  self.assertEqual(value['downloaded_bytes'],0);self.assertEqual(value['required_free_bytes'],len(self.bytes)+u.DOWNLOAD_RESERVE_BYTES);self.assertEqual(partial.read_bytes(),previous);self.assertEqual(partial.with_name(partial.name+'.json').read_bytes(),metadata)
 def test_complete_verified_file_before_stage_needs_no_second_download_budget(self):
  info=u.manifest(self.release,'0.2.0-preview.4');target=self.data/'updates/ANetSetup-0.2.0-preview.5.exe';target.parent.mkdir();target.write_bytes(self.bytes)
  with patch.object(u.shutil,'disk_usage',return_value=SimpleNamespace(free=u.DOWNLOAD_RESERVE_BYTES)):budget=u.download_budget(self.data,info)
  self.assertEqual(budget['required_free_bytes'],u.DOWNLOAD_RESERVE_BYTES);self.assertEqual(budget['downloaded_bytes'],len(self.bytes))
 def test_space_consumed_during_download_keeps_prefix_and_same_manifest_can_resume(self):
  self.bytes=b'MZ'+b'x'*(8*1024*1024);self.release['windows']['amd64'].update(bytes=len(self.bytes),sha256=hashlib.sha256(self.bytes).hexdigest());requests=[]
  def network(url,offset=0,etag=None):
   if url==u.ENDPOINT:return self.network(url)
   requests.append(offset);return self.response(self.bytes[offset:],206,{'Content-Range':f'bytes {offset}-{len(self.bytes)-1}/{len(self.bytes)}','Content-Length':str(len(self.bytes)-offset),'ETag':'"fixture-etag"'})
  ample=SimpleNamespace(free=u.DOWNLOAD_RESERVE_BYTES+len(self.bytes)+1024);low=SimpleNamespace(free=u.DOWNLOAD_RESERVE_BYTES-1)
  with patch.object(u,'network',network),patch.object(u.shutil,'disk_usage',side_effect=[ample,ample,ample,low]):
   with self.assertRaises(u.DownloadSpaceRequired):u.download(self.runtime)
  partial=self.data/'updates/ANetSetup-0.2.0-preview.5.exe.partial';prefix=partial.read_bytes();self.assertGreater(len(prefix),0);self.assertLess(len(prefix),len(self.bytes));self.assertEqual(prefix,self.bytes[:len(prefix)]);self.assertFalse((self.data/'updates/staged.json').exists())
  with patch.object(u,'network',network),patch.object(u.shutil,'disk_usage',return_value=ample):result=u.download(self.runtime)
  self.assertEqual(result['state'],'staged');self.assertEqual(requests,[0,len(prefix)]);self.assertEqual((partial.with_suffix('')).read_bytes(),self.bytes)
 def test_interrupted_bytes_resume_from_exact_range_and_hash(self):
  prefix=self.bytes[:300];partial=self.partial(prefix);calls=[]
  def network(url,offset=0,etag=None):
   if url==u.ENDPOINT:return self.network(url)
   calls.append((offset,etag));return self.response(self.bytes[offset:],206,{'Content-Range':f'bytes {offset}-{len(self.bytes)-1}/{len(self.bytes)}','Content-Length':str(len(self.bytes)-offset),'ETag':'"fixture-etag"'})
  with patch.object(u,'network',network):result=u.download(self.runtime)
  self.assertEqual(result['state'],'staged');self.assertEqual(calls,[(len(prefix),'"fixture-etag"')]);self.assertFalse(partial.exists());self.assertEqual(Path(u.read(self.data/'updates/staged.json')['path']).read_bytes(),self.bytes)
 def test_different_sha_never_appends_old_partial(self):
  self.partial(self.bytes[:300]);self.bytes=b'MZ'+b'NEWFILE'*200;self.release['windows']['amd64'].update(bytes=len(self.bytes),sha256=hashlib.sha256(self.bytes).hexdigest());offsets=[]
  def network(url,offset=0,etag=None):offsets.append(offset);return self.network(url)
  with patch.object(u,'network',network):u.download(self.runtime)
  self.assertEqual(offsets,[0,0]);self.assertEqual(Path(u.read(self.data/'updates/staged.json')['path']).read_bytes(),self.bytes)
 def test_range_ignored_restarts_without_appending(self):
  self.partial(self.bytes[:300])
  def network(url,offset=0,etag=None):return self.network(url)
  with patch.object(u,'network',network):u.download(self.runtime)
  self.assertEqual(Path(u.read(self.data/'updates/staged.json')['path']).read_bytes(),self.bytes)
 def test_wrong_content_range_never_becomes_staged(self):
  partial=self.partial(self.bytes[:300])
  def network(url,offset=0,etag=None):return self.network(url) if url==u.ENDPOINT else self.response(self.bytes[offset:],206,{'Content-Range':f'bytes 0-{len(self.bytes)-1}/{len(self.bytes)}','ETag':'"fixture-etag"'})
  with patch.object(u,'network',network):
   with self.assertRaisesRegex(ValueError,'range_mismatch'):u.download(self.runtime)
  self.assertFalse(partial.exists());self.assertFalse((self.data/'updates/staged.json').exists())
 def test_network_interruption_keeps_bound_partial(self):
  class Broken(io.BytesIO):
   def read(stream,n=-1):
    if stream.tell():raise OSError('fixture disconnect')
    return super(Broken,stream).read(300)
  with patch.object(u,'network',lambda url:self.network(url) if url==u.ENDPOINT else Broken(self.bytes)):
   with self.assertRaisesRegex(OSError,'fixture disconnect'):u.download(self.runtime)
  self.assertEqual((self.data/'updates/ANetSetup-0.2.0-preview.5.exe.partial').read_bytes(),self.bytes[:300]);self.assertFalse((self.data/'updates/staged.json').exists())
 def test_premature_eof_keeps_prefix_without_claiming_integrity(self):
  with patch.object(u,'network',lambda url:self.network(url) if url==u.ENDPOINT else io.BytesIO(self.bytes[:300])):
   with self.assertRaisesRegex(OSError,'download_interrupted'):u.download(self.runtime)
  self.assertEqual((self.data/'updates/ANetSetup-0.2.0-preview.5.exe.partial').read_bytes(),self.bytes[:300]);self.assertFalse((self.data/'updates/staged.json').exists())
 def test_slow_detached_download_survives_parent_exit_then_resumes_after_crash(self):
  if os.name!='nt':self.skipTest('native Windows detached process acceptance')
  payload=b'MZ'+b'x'*(262144-2);first_sent=threading.Event();release_first=threading.Event();ranges=[];starts=[]
  test=self
  class Handler(http.server.BaseHTTPRequestHandler):
   def log_message(*args):pass
   def do_GET(handler):
    if handler.path=='/api/desktop-release':
     raw=json.dumps(test.release).encode();handler.send_response(200);handler.send_header('Content-Length',str(len(raw)));handler.end_headers();handler.wfile.write(raw);return
    offset=int(handler.headers.get('Range','bytes=0-').split('=')[1].split('-')[0]);ranges.append((offset,handler.headers.get('If-Range')));starts.append(time.monotonic());handler.send_response(206 if offset else 200);handler.send_header('Content-Length',str(len(payload)-offset));handler.send_header('ETag','"slow-etag"')
    if offset:handler.send_header('Content-Range',f'bytes {offset}-{len(payload)-1}/{len(payload)}')
    handler.end_headers()
    try:
     if len(ranges)==1:
      handler.wfile.write(payload[:65536]);handler.wfile.flush();first_sent.set();release_first.wait(10);handler.wfile.write(payload[65536:])
     else:handler.wfile.write(payload[offset:])
    except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
  server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True;thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();origin='http://127.0.0.1:'+str(server.server_port)
  self.bytes=payload;self.release['windows']['amd64'].update(url=origin+'/downloads/ANetSetup-0.2.0-preview.5.exe',bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest())
  script=self.root/'fixture-process.py';script.write_text("import sys,json\nsys.path.insert(0,"+repr(str(ROOT))+")\nimport updater as u\nu.ORIGIN="+repr(origin)+";u.ENDPOINT=u.ORIGIN+'/api/desktop-release'\nif sys.argv[1]=='parent':\n old=u.launch_download\n def launch(argv,log):return old([sys.executable,__file__,'worker',sys.argv[2],argv[-1]],log)\n u.launch_download=launch\n print(json.dumps(u.request_download(sys.argv[2])))\nelse:\n print(json.dumps(u.fetch_update(sys.argv[2],sys.argv[3])))\n",encoding='utf-8')
  tokens=[]
  def wait(predicate):
   end=time.monotonic()+8
   while time.monotonic()<end:
    try:value=predicate()
    except OSError as error:
     # A terminating process can deny image-name lookup before its wait
     # handle signals. Unknown remains unknown until a later strong probe.
     if getattr(error,'winerror',None)!=5:raise
     value=None
    if value:return value
    time.sleep(.02)
   self.fail('deterministic fixture progress timed out')
  def terminate_owned(token):
   if u.process_token(token['pid'])!=token:return
   from ctypes import wintypes as W
   k=ctypes.WinDLL('kernel32',use_last_error=True);k.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];k.OpenProcess.restype=W.HANDLE;k.TerminateProcess.argtypes=[W.HANDLE,W.UINT];k.CloseHandle.argtypes=[W.HANDLE];k.GetProcessTimes.argtypes=[W.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p]
   handle=k.OpenProcess(0x1001,False,token['pid'])
   if not handle:raise ctypes.WinError(ctypes.get_last_error())
   try:
    # Recheck creation on the SAME handle used to simulate the fixture crash.
    created=ctypes.c_uint64();a=ctypes.c_uint64();b=ctypes.c_uint64();c=ctypes.c_uint64()
    if not k.GetProcessTimes(handle,ctypes.byref(created),ctypes.byref(a),ctypes.byref(b),ctypes.byref(c)):raise ctypes.WinError(ctypes.get_last_error())
    if created.value!=token['creation_filetime']:return
    if not k.TerminateProcess(handle,91):raise ctypes.WinError(ctypes.get_last_error())
   finally:k.CloseHandle(handle)
  try:
   parent=subprocess.run([sys.executable,str(script),'parent',str(self.runtime)],capture_output=True,timeout=8);self.assertEqual(parent.returncode,0,parent.stderr);self.assertEqual(json.loads(parent.stdout)['state'],'downloading');self.assertTrue(first_sent.wait(5))
   marker=self.data/'updates/download.json';token=u.read(marker)['worker'];tokens.append(token);partial=self.data/'updates/ANetSetup-0.2.0-preview.5.exe.partial';wait(lambda:partial.exists() and partial.stat().st_size==65536)
   with patch.object(u,'ORIGIN',origin),patch.object(u,'ENDPOINT',origin+'/api/desktop-release'):
    current=u.check(self.runtime);self.assertEqual(current['state'],'downloading');self.assertEqual(current['downloaded_bytes'],65536)
    with patch.object(u,'launch_download',side_effect=AssertionError('must reuse existing worker')):self.assertEqual(u.request_download(self.runtime)['state'],'downloading')
   terminate_owned(token);wait(lambda:u.process_token(token['pid']) is None);self.assertEqual(partial.stat().st_size,65536);release_first.set()
   parent2=subprocess.run([sys.executable,str(script),'parent',str(self.runtime)],capture_output=True,timeout=8);self.assertEqual(parent2.returncode,0,parent2.stderr);tokens.append(u.read(marker)['worker']);wait(lambda:(self.data/'updates/staged.json').exists())
   self.assertEqual(ranges,[(0,None),(65536,'"slow-etag"')]);self.assertEqual(Path(u.read(self.data/'updates/staged.json')['path']).read_bytes(),payload)
   with patch.object(u,'ORIGIN',origin),patch.object(u,'ENDPOINT',origin+'/api/desktop-release'),patch.object(u,'network',side_effect=AssertionError('staged offline')):self.assertEqual(u.check(self.runtime)['state'],'staged')
   wait(lambda:u.process_token(tokens[-1]['pid']) is None)
   u.atomic(ROOT/'evidence/update-download-lifecycle.json',{'schema':'anet-download-lifecycle-acceptance/1','transport':'isolated loopback HTTP test only; production fixed HTTPS unchanged','actual_windows_processes':True,'parent_simulates_ui_process':True,'actual_wpf_window_test':False,'first_parent_exit_code':parent.returncode,'first_parent_exited_while_worker_live':True,'first_worker':tokens[0],'first_partial_bytes':65536,'reentry_reused_live_worker':True,'simulated_crash_exact_process_handle_checked':True,'second_parent_exit_code':parent2.returncode,'second_worker':tokens[1],'range_requests':ranges,'resumed_at_byte':65536,'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest(),'final_staged_sha_verified':True,'offline_staged_check':True,'second_worker_normal_exit_confirmed':True,'model_requests':0,'business_tasks_sent':0})
  finally:
   release_first.set()
   for token in tokens:terminate_owned(token)
   server.shutdown();server.server_close();thread.join(timeout=2)

if __name__=='__main__':unittest.main()

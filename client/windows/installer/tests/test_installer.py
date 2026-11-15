import argparse,hashlib,importlib.util,json,os,shutil,sys,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('installer',ROOT/'install_engine.py');engine=importlib.util.module_from_spec(spec);spec.loader.exec_module(engine)

class InstallerTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='anet-installer-unit-');self.root=Path(self.temp.name);self.payload=self.root/'payload';self.payload.mkdir();self.fixture=self.root/'fresh-user';self.self_exe=self.root/'setup.exe';self.self_exe.write_bytes(b'setup')
  for name,body in {'frontend/ANetDesktop.exe':b'desktop','frontend/install/updater.py':b'updater','backend/desktop_controller.py':b'backend','python/python.exe':b'python','harness-runtime/codex/codex.exe':b'codex','node/taskd/app/run_taskd.py':b'taskd'}.items():
   p=self.payload/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(body)
  self.set_version('0.2.0-preview.4');self.args=argparse.Namespace(fixture_root=str(self.fixture),node_root=None,backend_root=None,self_exe=self.self_exe,action='install',fail_before_activate=False,start_services=False)
 def tearDown(self):self.temp.cleanup()
 def set_version(self,version):
  files=[{'path':str(p.relative_to(self.payload)).replace('\\','/'),'bytes':p.stat().st_size,'sha256':engine.sha(p)} for p in sorted(self.payload.rglob('*')) if p.is_file() and p.name!='payload-manifest.json']
  self.manifest={'schema':'anet-desktop-payload/1','release':version,'files':files};engine.atomic(self.payload/'payload-manifest.json',self.manifest)
 def install(self):return engine.install(self.args,self.payload,engine.validate_payload(self.payload))
 def current(self):return engine.read(self.fixture/'Data/ANetDesktop/current.json')
 def test_fresh_and_repeat_preserve_config(self):
  first=self.install();cfg=self.fixture/'Data/ANetDesktop/harness-config.json';cfg.write_text('{"user":"retained"}')
  second=self.install();self.assertEqual(first['runtime'],second['runtime']);self.assertEqual(engine.read(cfg),{'user':'retained'})
 def test_private_payload_links_survive_scratch_removal(self):
  self.args.private_payload=True;self.install();installed=Path(self.current()['frontend_root'])/'ANetDesktop.exe';source=self.payload/'frontend/ANetDesktop.exe'
  self.assertEqual(source.stat().st_ino,installed.stat().st_ino);source.unlink();self.assertEqual(installed.read_bytes(),b'desktop');self.assertEqual(installed.stat().st_nlink,1)
 def test_sdk_filename_near_windows_path_limit_copies_atomically(self):
  destination=self.root/'copy-target';name='runtime-'+('x'*72)+'.dll';component='d'*(240-len(str(destination))-len(name)-2);relative=component+'/'+name;source=self.payload/'long-sdk'/relative;source.parent.mkdir(parents=True);source.write_bytes(b'native sdk bytes')
  manifest={'files':[{'path':'long-sdk/'+relative,'sha256':engine.sha(source),'bytes':source.stat().st_size}]}
  engine.copy_group(self.payload,manifest,'long-sdk',destination);installed=destination/relative
  self.assertEqual(len(str(installed)),240);self.assertEqual(installed.read_bytes(),b'native sdk bytes')
 def test_managed_python_app_upgrade_preserves_node_identity(self):
  self.install();old=self.current();node=Path(old['runtime']['anet_root']);identity=node/'user-identity';identity.write_bytes(b'never replace')
  (self.payload/'node/taskd/app/run_taskd.py').write_bytes(b'new taskd app');self.set_version('0.2.0-preview.5');self.install()
  self.assertEqual((node/'taskd/app/run_taskd.py').read_bytes(),b'new taskd app');self.assertEqual(identity.read_bytes(),b'never replace');self.assertTrue(list((self.fixture/'Data/ANetDesktop/backups').iterdir()))
 def test_failed_managed_service_upgrade_restores_previous_code(self):
  self.install();old=self.current();node=Path(old['runtime']['anet_root']);(self.payload/'node/taskd/app/run_taskd.py').write_bytes(b'new bad app');self.set_version('0.2.0-preview.5');self.args.start_services=True
  with patch.object(engine,'setup_fresh',side_effect=RuntimeError('injected_startup_failure')):out=self.install()
  self.assertEqual(out['state'],'rolled_back');self.assertEqual(self.current()['release'],old['release']);self.assertEqual((node/'taskd/app/run_taskd.py').read_bytes(),b'taskd')
 def test_manually_modified_managed_app_is_not_overwritten(self):
  self.install();old=self.current();node=Path(old['runtime']['anet_root']);(node/'taskd/app/run_taskd.py').write_bytes(b'owner hotfix');(self.payload/'node/taskd/app/run_taskd.py').write_bytes(b'new taskd app');self.set_version('0.2.0-preview.5')
  with self.assertRaisesRegex(ValueError,'installed_code_drift'):self.install()
  self.assertEqual(self.current()['release'],old['release']);self.assertEqual((node/'taskd/app/run_taskd.py').read_bytes(),b'owner hotfix')
 def test_existing_node_bytes_untouched(self):
  node=self.root/'original-node';node.mkdir();(node/'identity.json').write_bytes(b'ORIGINAL-NODE');self.args.node_root=str(node);r=self.install()
  self.assertTrue(r['existing_node_reused']);self.assertEqual(list(node.iterdir()),[node/'identity.json']);self.assertEqual((node/'identity.json').read_bytes(),b'ORIGINAL-NODE')
 def test_failed_upgrade_keeps_current(self):
  self.install();before=self.current();self.set_version('0.2.0-preview.5');self.args.fail_before_activate=True
  with self.assertRaisesRegex(RuntimeError,'fixture_fail'):self.install()
  self.assertEqual(self.current(),before)
 def test_backend_upgrade_preserves_stable_source_state(self):
  self.install();before=self.current();state=Path(before['runtime']['source_state_root']);state.mkdir();(state/'source-proof.json').write_text('original source')
  (self.payload/'backend/desktop_controller.py').write_bytes(b'new backend implementation');self.set_version('0.2.0-preview.5');self.install();after=self.current()
  self.assertNotEqual(before['runtime']['backend_root'],after['runtime']['backend_root']);self.assertEqual(before['runtime']['source_state_root'],after['runtime']['source_state_root']);self.assertEqual((state/'source-proof.json').read_text(),'original source')
 def test_upgrade_and_explicit_rollback(self):
  self.install();before=self.current();self.set_version('0.2.0-preview.5');self.install();self.assertEqual(self.current()['release'],'0.2.0-preview.5')
  # The normal backend-manifest is mandatory for rollback verification.
  front=Path(before['frontend_root']);engine.atomic(front/'backend-manifest.json',{'files':before['backend_files']})
  self.args.action='rollback';engine.maintenance(self.args);self.assertEqual(self.current()['release'],'0.2.0-preview.4')
 def test_frontend_repair_is_explicit(self):
  self.install();p=Path(self.current()['frontend_root'])/'ANetDesktop.exe';p.write_bytes(b'drift')
  with self.assertRaisesRegex(ValueError,'installed_code_drift'):self.install()
  self.args.action='repair';self.install();self.assertEqual(p.read_bytes(),b'desktop')
 def test_uninstall_preserves_unknown_and_node(self):
  self.install();r=self.current();unknown=Path(r['frontend_root'])/'my-result.txt';unknown.write_bytes(b'user result');node=Path(r['runtime']['node_root']);sentinel=node/'identity-secret';sentinel.write_bytes(b'keep')
  self.args.action='uninstall';out=engine.maintenance(self.args);self.assertTrue(out['node_state_preserved']);self.assertEqual(sentinel.read_bytes(),b'keep');self.assertTrue(unknown.exists());self.assertFalse((Path(r['frontend_root'])/'ANetDesktop.exe').exists())
 def test_corrupt_payload_rejected(self):
  (self.payload/'frontend/ANetDesktop.exe').write_bytes(b'bad')
  with self.assertRaisesRegex(ValueError,'payload_hash_mismatch'):engine.validate_payload(self.payload)
 def test_duplicate_case_rejected(self):
  self.manifest['files'].append(dict(self.manifest['files'][0],path=self.manifest['files'][0]['path'].upper()));engine.atomic(self.payload/'payload-manifest.json',self.manifest)
  with self.assertRaises(ValueError):engine.validate_payload(self.payload)
 def test_escape_release_refused(self):
  self.set_version('../foreign')
  with self.assertRaisesRegex(ValueError,'release_name'):self.install()
 def test_foreign_nonempty_program_refused(self):
  p=self.fixture/'Programs/ANetDesktop';p.mkdir(parents=True);(p/'user.txt').write_text('keep')
  with self.assertRaisesRegex(ValueError,'nonempty_unowned'):self.install()
 def test_insufficient_destination_space_fails_before_node_creation(self):
  disk=type('Disk',(),{'free':0})()
  with patch.object(engine.shutil,'disk_usage',return_value=disk):
   with self.assertRaisesRegex(OSError,'insufficient_installation_disk_space'):self.install()
  self.assertFalse((self.fixture/'Data/ANetDesktop/current.json').exists());self.assertFalse((self.fixture/'Data/ANetDesktop/node').exists())
 @unittest.skipUnless(os.name=='nt','Windows path policy')
 def test_long_destination_rejected_before_program_or_identity_copy(self):
  self.args.fixture_root=str(self.root/('long-path-'+('x'*120)))
  with patch('winreg.QueryValueEx',return_value=(0,4)):
   with patch.object(engine,'copy_group',side_effect=AssertionError('must preflight before copying')):
    with self.assertRaisesRegex(ValueError,'安装目录过长'):self.install()
  target=Path(self.args.fixture_root)
  self.assertFalse((target/'Data/ANetDesktop/current.json').exists())
  self.assertFalse((target/'Data/ANetDesktop/node').exists())
 def test_escape_manifest_path_refused(self):
  self.manifest['files'][0]['path']='../setup.exe';engine.atomic(self.payload/'payload-manifest.json',self.manifest)
  with self.assertRaisesRegex(ValueError,'outside_owned_root'):engine.validate_payload(self.payload)
 def test_release_policy_pins_both_fixed_packages(self):
  task=self.root/'closure-task';app=task/'app'
  for name in ('anet_harnesses','anet_provider_harnesses'):
   folder=app/name;folder.mkdir(parents=True);(folder/'__init__.py').write_bytes(b'')
  helper=ROOT.parents[1]/'cli/anet_harnesses/package_release.py';shutil.copyfile(helper,app/'anet_harnesses/package_release.py')
  provider=app/'anet_provider_harnesses/bridge.py';provider.write_text('value = 1\n',encoding='utf-8')
  first=engine.harness_release_policy(task,Path(sys.executable));provider.write_text('value = 2\n',encoding='utf-8');second=engine.harness_release_policy(task,Path(sys.executable))
  self.assertEqual(first['package_schema'],'anet-harness-release-closure/2');self.assertEqual(first['service_exe_sha256'],engine.sha(Path(sys.executable)));self.assertNotEqual(first['package_sha256'],second['package_sha256'])
  shutil.rmtree(app/'anet_provider_harnesses')
  with self.assertRaisesRegex(RuntimeError,'finite_command_failed'):engine.harness_release_policy(task,Path(sys.executable))

if __name__=='__main__':unittest.main()

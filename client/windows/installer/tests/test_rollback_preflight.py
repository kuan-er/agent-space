"""Small independent rollback admission fixtures; no native or model launch."""
import json,unittest
from pathlib import Path
from unittest.mock import patch
import test_installer as base
e=base.engine

class RollbackPreflightTests(unittest.TestCase):
 def setUp(self):
  self.case=base.InstallerTests('test_fresh_and_repeat_preserve_config');self.case.setUp()
 def tearDown(self):self.case.tearDown()
 def snapshot(self):
  return {p.relative_to(self.case.fixture).as_posix():(e.sha(p),p.stat().st_ino) for p in self.case.fixture.rglob('*') if p.is_file()}
 def file(self,relative,body):
  p=self.case.payload/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(body)
 def backend_manifest(self,receipt):e.atomic(Path(receipt['frontend_root'])/'backend-manifest.json',{'files':receipt['backend_files']})
 def pair(self,*,node_change=False,runtime_change=False):
  first=self.case.install();self.old=self.case.current();self.backend_manifest(self.old)
  self.file('frontend/ANetDesktop.exe',b'changed frontend only')
  if node_change:self.file('node/taskd/app/run_taskd.py',b'new incompatible task service')
  if runtime_change:self.file('python/python.exe',b'new interpreter bytes')
  self.case.set_version('0.2.0-preview.5');self.case.install();self.now=self.case.current();return first
 def rollback(self):
  self.case.args.action='rollback'
  with patch.object(e,'execute',side_effect=AssertionError('native command forbidden')),patch.object(e.subprocess,'Popen',side_effect=AssertionError('process launch forbidden')):
   return e.maintenance(self.case.args)
 def mini_sdk(self,legacy_front=False):
  for prefix in ('frontend/harnesses','node/taskd/app'):
   self.file(prefix+'/anet_harnesses/__init__.py',b'# miniature fixture SDK\n')
   if legacy_front and prefix=='frontend/harnesses':continue
   self.file(prefix+'/anet_harnesses/package_release.py',b'# closed-schema fixture marker\n');self.file(prefix+'/anet_provider_harnesses/__init__.py',b'# miniature provider fixture\n')
  self.file('node/taskd/app/taskd/harness_sources.py',b'# miniature registry fixture\n')
  self.case.set_version('0.2.0-preview.4')
 def configured_pair(self,legacy_prior=False):
  self.mini_sdk(legacy_prior);self.case.install();self.old=self.case.current();self.backend_manifest(self.old)
  old=e._harness_code_release(Path(self.old['runtime']['harness_root']))
  if legacy_prior:
   self.file('frontend/harnesses/anet_harnesses/package_release.py',b'# closed-schema fixture marker\n');self.file('frontend/harnesses/anet_provider_harnesses/__init__.py',b'# miniature provider fixture\n')
  self.file('frontend/ANetDesktop.exe',b'new UI fixture');self.case.set_version('0.2.0-preview.5');self.case.install();self.now=self.case.current()
  node=Path(self.now['runtime']['node_root']);current=e._harness_code_release(node/'app');policy=dict(current,service_exe_sha256=e.sha(Path(self.now['runtime']['python_exe'])))
  if legacy_prior:policy['compatible_releases']=[dict(old,service_exe_sha256=policy['service_exe_sha256'],registered_records=['a'*64])]
  e.atomic(node/'config.json',{'requester':{'harness_sources':{'enabled':True,'release_policy':policy}}});e.atomic(node/'harness-sources/sources.json',{'schema':'anet-taskd-harness-sources/1','sources':{}})
 def test_changed_managed_app_holds_all_original_bytes_and_ids(self):
  self.pair(node_change=True);before=self.snapshot();value=self.rollback()
  self.assertEqual(value['state'],'needs_service_rollback_plan');self.assertFalse(value['current_pointer_written']);self.assertEqual(value['preflight']['reason_code'],'managed_service_release_changed');self.assertEqual(self.snapshot(),before);self.assertEqual(self.case.current()['release'],'0.2.0-preview.5')
 def test_changed_runtime_release_holds_without_writes(self):
  self.pair(runtime_change=True);before=self.snapshot();value=self.rollback()
  self.assertEqual(value['state'],'needs_service_rollback_plan');self.assertEqual(value['preflight']['reason_code'],'runtime_release_changed');self.assertEqual(self.snapshot(),before)
 def test_different_prior_source_state_root_is_not_silently_selected(self):
  self.pair();data=Path(self.now['data_root']);prior=e.read(data/'previous.json');prior['runtime']['source_state_root']=str(data/'different-prior-state');e.atomic(data/'previous.json',prior);before=self.snapshot();value=self.rollback()
  self.assertEqual(value['state'],'needs_service_rollback_plan');self.assertEqual(value['preflight']['reason_code'],'source_state_or_harness_configuration_root_changed');self.assertEqual(self.snapshot(),before)
 def test_exact_legacy_record_pin_does_not_authorize_new_old_sdk_host(self):
  self.configured_pair(legacy_prior=True);before=self.snapshot();value=self.rollback()
  self.assertEqual(value['state'],'needs_service_rollback_plan');self.assertEqual(value['preflight']['reason_code'],'prior_sdk_not_compatible_with_current_primary');self.assertEqual(self.snapshot(),before)
 def test_same_closed_service_frontend_rollback_preserves_policy_registry_and_paths(self):
  self.configured_pair();node=Path(self.now['runtime']['node_root']);data=Path(self.now['data_root']);source=Path(self.now['runtime']['source_state_root'])/'source.json';work=data/'workspace/work.txt';source.parent.mkdir(parents=True);work.parent.mkdir();source.write_text('{"source":"preserved fixture"}');work.write_text('preserved fixture workspace');before={str(p):e.sha(p) for p in [node/'config.json',node/'harness-sources/sources.json',data/'previous.json',source,work]};value=self.rollback();after=self.case.current()
  self.assertEqual(value['state'],'rolled_back');self.assertEqual(value['rollback_scope'],'frontend_only');self.assertFalse(value['full_managed_reverse_migration']);self.assertEqual(after['release'],self.old['release'])
  for key in ('node_root','anet_root','python_exe','codex_exe','source_state_root'):self.assertEqual(after['runtime'][key],self.now['runtime'][key])
  self.assertTrue(Path(after['runtime']['python_exe']).is_file());self.assertTrue(Path(after['runtime']['node_root'],'app/run_taskd.py').is_file());self.assertTrue(Path(after['frontend_root'],'ANetDesktop.exe').is_file());self.assertEqual({p:e.sha(Path(p)) for p in before},before)
 def test_prior_owned_broker_whole_tuple_must_be_approved(self):
  self.file('backend/broker/src/broker.py',b'old broker');self.case.set_version('0.2.0-preview.4');self.case.install();self.old=self.case.current();self.backend_manifest(self.old);self.file('backend/broker/src/broker.py',b'new broker');self.case.set_version('0.2.0-preview.5');self.case.install();self.now=self.case.current();node=Path(self.now['runtime']['node_root'])
  def tuple_for(rec):
   rt=rec['runtime'];return {'broker_script_sha256':e.sha(Path(rt['backend_root'],'broker/src/broker.py')),'broker_exe_sha256':e.sha(Path(rt['broker_python_exe'])),'codex_exe_sha256':e.sha(Path(rt['codex_exe']))}
  policy=tuple_for(self.now);e.atomic(node/'config.json',{'requester':{'owned_sources':{'enabled':True,'release_policy':policy}}});before=self.snapshot();held=self.rollback();self.assertEqual(held['preflight']['reason_code'],'prior_owned_broker_tuple_not_approved');self.assertEqual(self.snapshot(),before)
  policy['compatible_releases']=[tuple_for(self.old)];e.atomic(node/'config.json',{'requester':{'owned_sources':{'enabled':True,'release_policy':policy}}});allowed=self.rollback();self.assertEqual(allowed['state'],'rolled_back')
 def test_registry_change_after_preflight_is_held_without_pointer_or_policy_rewrite(self):
  self.configured_pair();node=Path(self.now['runtime']['node_root']);data=Path(self.now['data_root']);registry=node/'harness-sources/sources.json';before={str(p):e.sha(p) for p in [data/'current.json',data/'previous.json',node/'config.json']};original=e.rollback_preflight;external={'schema':'fixture','sources':{},'external_change':'preserve'}
  def race(*args):
   plan=original(*args);e.atomic(registry,external);return plan
  with patch.object(e,'rollback_preflight',side_effect=race):value=self.rollback()
  self.assertEqual(value['state'],'needs_service_rollback_plan');self.assertEqual(value['preflight']['reason_code'],'rollback_inputs_changed');self.assertEqual({p:e.sha(Path(p)) for p in before},before);self.assertEqual(e.read(registry),external)

if __name__=='__main__':unittest.main()

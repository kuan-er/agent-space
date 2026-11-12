"""Windows decision paths are simulated; no claim that Windows ACL/WSH ran here."""
import importlib.util,json,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('connector',Path(__file__).parents[1]/'connector.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class ConnectorTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);(self.root/'portal').mkdir()
  self.config={'inbound':{'policy':'closed'},'auto_reply':{'backend':'exec','agent':'codex','admitted':'direct','work_dir':'C:\\Users\\admin\\EPS','untrusted':'off'}}
  (self.root/'config.json').write_text(json.dumps(self.config));(self.root/'portal/connection.json').write_text(json.dumps({'portal':'https://hub.example.com:8888','node_token':'only-a-test-token','aid':'test-aid'}))
  self.ops=[];self.fail=False
 def fake_run(self,cmd,**kwargs):
  if m.sys.platform=='win32':self.assertEqual(kwargs.get('creationflags'),0x08000000)
  self.ops.append(cmd[1:]);cfg=json.loads((self.root/'config.json').read_text())
  if cmd[1:]==['up'] and self.fail:
   caps=cfg.get('modules',{}).get('service',{}).get('capabilities',[])
   if caps:return subprocess.CompletedProcess(cmd,1,'','simulated failed startup')
  data={'policy':'closed','allow':[],'trust':[],'public_capabilities':[],'admit':cfg.get('inbound',{}).get('admit')}
  return subprocess.CompletedProcess(cmd,0,json.dumps(data),'')
 def configure(self,*extra):
  with patch.object(m.sys,'platform','win32'),patch.object(m.sys,'argv',['connector.py','configure','--data-dir',str(self.root),'--anet','/tmp/anet',*extra]),patch.object(m.subprocess,'run',self.fake_run),patch.object(m,'protect_private'),patch.object(m.shutil,'which',return_value='/tmp/anet'),patch.object(m,'remote',return_value={'ok':True}):m.main()
 def test_windows_defaults_to_remote_https_and_preserves_executor(self):
  self.configure();c=json.loads((self.root/'config.json').read_text());svc=c['modules']['service'];cap=svc['capabilities'][0]
  self.assertEqual(cap['url'],'https://hub.example.com:8888/api/node/inbound');self.assertTrue(svc['allow_tcp']);self.assertTrue(cap['internal'])
  self.assertEqual(c['inbound']['policy'],'closed');self.assertEqual(c['auto_reply'],self.config['auto_reply'])
  self.assertEqual(Path(cap['token_file']).read_text().strip(),'only-a-test-token')
 def test_windows_refuses_unix_before_config_change(self):
  with self.assertRaisesRegex(RuntimeError,'仅支持远程 HTTPS'):self.configure('--transport','unix')
  self.assertEqual(json.loads((self.root/'config.json').read_text()),self.config);self.assertEqual(self.ops,[])
 def test_start_failure_restores_original_config(self):
  self.fail=True
  with self.assertRaisesRegex(RuntimeError,'已恢复原配置'):self.configure()
  self.assertEqual(json.loads((self.root/'config.json').read_text()),self.config)
  self.assertEqual(self.ops[-1],['up'])
 def test_calling_device_skips_execution_configuration(self):
  with patch.object(m.sys,'argv',['connector.py','configure','--data-dir',str(self.root),'--anet','/tmp/anet']),patch.object(m.subprocess,'run',self.fake_run),patch.object(m,'remote',return_value={'kind':'device'}):m.main()
  self.assertEqual(json.loads((self.root/'config.json').read_text()),self.config)
  self.assertEqual(self.ops,[])
 def test_taskd_status_replaces_autoreply_readiness(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);self.assertIsNone(m.taskd_state(root))
   (root/'taskd').mkdir()
   st={'ts':__import__('time').time(),'accept_tasks':True,'executor':'codex','executor_ready':True,'capabilities':{'protocol':'taskd/1'}}
   (root/'taskd'/'status.json').write_text(json.dumps(st));self.assertEqual(m.taskd_state(root)['executor'],'codex')
   st['ts']-=600;(root/'taskd'/'status.json').write_text(json.dumps(st));self.assertIsNone(m.taskd_state(root))
   st['ts']+=600;st['accept_tasks']=False;(root/'taskd'/'status.json').write_text(json.dumps(st));self.assertIsNone(m.taskd_state(root))
 def test_requester_only_taskd_does_not_claim_execution(self):
  folder=self.root/'taskd';folder.mkdir()
  st={'ts':__import__('time').time(),'requester':True,'accept_tasks':False}
  (folder/'status.json').write_text(json.dumps(st))
  (folder/'config.json').write_text(json.dumps({'requester':{'clients':['codex']}}))
  self.assertTrue(m.taskd_requester_ready(self.root));self.assertIsNone(m.taskd_state(self.root))
  (folder/'config.json').write_text(json.dumps({'requester':{'clients':[]}}))
  self.assertFalse(m.taskd_requester_ready(self.root))
  for invalid in ('not a timestamp',None,__import__('time').time()+1000):
   st['ts']=invalid;(folder/'status.json').write_text(json.dumps(st));self.assertIsNone(m.taskd_state(self.root,requester=True))
 def test_shared_requester_health_is_authoritative(self):
  folder=self.root/'taskd';folder.mkdir()
  st={'ts':__import__('time').time(),'requester':True,'requester_ready':False}
  (folder/'status.json').write_text(json.dumps(st))
  (folder/'config.json').write_text(json.dumps({'requester':{'clients':['codex']}}))
  self.assertFalse(m.taskd_requester_ready(self.root))
  st['requester_ready']=True;(folder/'status.json').write_text(json.dumps(st))
  self.assertTrue(m.taskd_requester_ready(self.root))
 def test_work_status_obeys_sync_switch_and_executor_identity(self):
  import time
  folder=self.root/'taskd';folder.mkdir()
  w={'schema':1,'source':'taskd','scope':'anet_tasks','observed_at':time.time(),'counts':{'running':1},'tasks':[{'id':'t1','state':'running','goal':'private title','reason':'private question','blocker':{'code':'configuration_mismatch','action':'repair_configuration','detail':'private path'},'plan_revision':2,'revision_pending':True,'phase':{'index':1,'total':2,'title':'private phase'},'last_event':'executor_started'}]}
  st={'ts':time.time(),'accept_tasks':True,'executor':'codex','work':w}
  (folder/'status.json').write_text(json.dumps(st))
  work=m.work_info(self.root,'codex')
  self.assertFalse(work['content_synced']);self.assertNotIn('goal',work['tasks'][0]);self.assertNotIn('title',work['tasks'][0]['phase'])
  self.assertNotIn('blocker',work['tasks'][0]);self.assertEqual(work['tasks'][0]['plan_revision'],2)
  self.assertIsNone(m.work_info(self.root,'pi'))
  w['tasks'][0]['phase']['milestone']={'done':1,'total':3,'current':'private operation'}
  w['tasks'][0]['timing']={'execution':{'calls':1,'seconds':5}}
  w['tasks'][0]['last_purpose']='execution'
  (folder/'status.json').write_text(json.dumps(st))
  filtered=m.work_info(self.root,'codex')['tasks'][0]
  self.assertEqual(filtered['phase']['milestone'],{'done':1,'total':3})
  self.assertEqual(filtered['last_purpose'],'execution')
  m.sync_flag(self.root).write_text('1')
  self.assertEqual(m.work_info(self.root,'codex')['tasks'][0]['goal'],'private title')
  self.assertEqual(m.work_info(self.root,'codex')['tasks'][0]['blocker']['action'],'repair_configuration')
  self.assertEqual(m.work_info(self.root,'codex')['tasks'][0]['phase']['milestone']['current'],'private operation')
  st['ts']-=500;(folder/'status.json').write_text(json.dumps(st))
  self.assertIsNone(m.work_info(self.root,'codex'))
if __name__=='__main__':unittest.main(verbosity=2)

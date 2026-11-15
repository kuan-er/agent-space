import json, time, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from test_execution_facts import connector, server
from test_portal import PortalTest, m

class ContentUnitTest(unittest.TestCase):
 def test_bounds_redaction_and_unknown_fields(self):
  raw={'schema':1,'fields':{'goal':'目标'*20000,'phase':'阶'*20000,'summary':'must truncate','secret':'do not transport','reason':'password=secret123'}}
  a=connector.clean_work_content(raw);self.assertEqual(a,server.clean_work_content(raw))
  self.assertEqual(sum(map(len,a['fields'].values())),32768)
  self.assertIn('summary',a['truncated']);self.assertNotIn('secret',a['fields'])
  self.assertNotIn('secret123',str(a));self.assertIsNone(server.clean_work_content(dict(raw,schema=2)))
  self.assertEqual(server.clean_work_content(a),a)
 def test_opt_in_and_legacy_status(self):
  task={'id':'t1','goal':'目标'*500,'phase':{'title':'阶段'*100},'state':'running'}
  td={'executor':'codex','work':{'schema':1,'observed_at':time.time(),'tasks':[task]}}
  with tempfile.TemporaryDirectory() as d, patch.object(connector,'taskd_state',return_value=td),patch.object(connector,'sync_enabled',return_value=False):
   self.assertEqual(connector.work_content_records(Path(d),'codex'),[])
  with tempfile.TemporaryDirectory() as d, patch.object(connector,'taskd_state',return_value=td),patch.object(connector,'sync_enabled',return_value=True):
   rows=connector.work_content_records(Path(d),'codex');self.assertEqual(rows[0]['content']['fields']['goal'],task['goal'])
   td['work']['tasks']=[dict(task,id='t'+str(i)) for i in range(51)]
   self.assertEqual(len(connector.work_content_records(Path(d),'codex')),50)
 def test_unicode_wire_size_and_per_task_failure_fairness(self):
  import io
  from unittest.mock import Mock
  raw={'task_id':'t1','content':connector.clean_work_content({'schema':1,'fields':{'goal':'😀'*16384,'phase':'😀'*16384}})}
  opener=Mock();opener.open.return_value=io.StringIO('{}')
  with patch.object(connector,'build_opener',return_value=opener):connector.remote('https://portal.example','/api/node/work-content',raw)
  request=opener.open.call_args.args[0];self.assertLess(len(request.data),262144)
  self.assertEqual(json.loads(request.data),raw)
  rows=[{'task_id':'first'},{'task_id':'second'}]
  with patch.object(connector,'work_content_records',return_value=rows),patch.object(connector,'sync_enabled',return_value=True),patch.object(connector,'remote',side_effect=[connector.RemoteError('race',409),{}]) as send:
   self.assertEqual(connector.push_work_content(Path('.'),'codex',{'portal':'https://portal.example','node_token':'test'}),1)
   self.assertEqual(send.call_count,2)
  with patch.object(connector,'work_content_records',return_value=rows),patch.object(connector,'sync_enabled',return_value=True),patch.object(connector,'remote',side_effect=connector.RemoteError('old server',404)) as send:
   connector.push_work_content(Path('.'),'codex',{'portal':'https://portal.example','node_token':'test'})
   self.assertEqual(send.call_count,1)
 def test_visibility_and_stale_content(self):
  ts=time.time();work={'schema':1,'source':'taskd','scope':'anet_tasks','observed_at':ts,'content_synced':True,'counts':{'running':1},'tasks':[{'id':'t1','state':'running'}]}
  a={'node_connected':True,'work':json.dumps(work),'work_content':json.dumps({'t1':{'observed_at':ts-301,'content':{'schema':1,'fields':{'goal':'private'}}}})}
  self.assertTrue(server.work_view(a,'ready',True)['tasks'][0]['content']['stale'])
  self.assertTrue(server.work_view(a,'ready',True)['tasks'][0]['content']['older_than_status'])
  self.assertEqual(server.work_view(a,'ready',False)['tasks'],[])
  work['content_synced']=False;a['work']=json.dumps(work)
  self.assertNotIn('private',json.dumps(server.work_view(a,'ready',True)))

class ContentEndpointTest(PortalTest):
 def test_complete_flow_and_security(self):
  token=(m.DATA/'admin-setup-link.txt').read_text().strip().split('=')[-1]
  user=self.register(token,'content-admin');aid=self.new_agent(user,'content-test');nt=self.pair(user,aid,'b'+'f'*58)
  ts=time.time();work={'schema':1,'source':'taskd','scope':'anet_tasks','observed_at':ts,'content_synced':True,'counts':{'running':1},'tasks':[{'id':'t1','state':'running'}]}
  self.req('/api/node/heartbeat','POST',{'connected':True,'executor_ready':True,'policy_ok':True,'work':work},node=nt)
  record={'task_id':'t1','observed_at':ts,'content':{'schema':1,'fields':{'goal':'长目标'*5000+' token=secret123','summary':'报告'}}}
  self.assertEqual(self.req('/api/node/work-content','POST',record)[0],401)
  self.assertEqual(self.req('/api/node/work-content','POST',record,node=nt)[0],200)
  self.assertEqual(self.req('/api/node/work-content','POST',record,node=nt)[0],200)
  self.assertEqual(self.req('/api/node/work-content','POST',dict(record,content={'schema':1,'fields':{'goal':'conflict'}}),node=nt)[0],409)
  self.assertEqual(self.req('/api/node/work-content','POST',dict(record,task_id='other'),node=nt)[0],409)
  self.assertEqual(self.req('/api/node/work-content','POST',dict(record,observed_at=ts-1),node=nt)[0],409)
  with m.db() as c: a=m.one(c,'SELECT * FROM agents WHERE id=?',(aid,))
  content=m.work_view(a,'ready',True)['tasks'][0]['content'];self.assertGreater(len(content['fields']['goal']),500);self.assertNotIn('secret123',str(content))
  work['content_synced']=False
  self.req('/api/node/heartbeat','POST',{'connected':True,'work':work},node=nt)
  with m.db() as c:self.assertIsNone(m.one(c,'SELECT work_content FROM agents WHERE id=?',(aid,))['work_content'])
  self.assertEqual(self.req('/api/node/work-content','POST',record,node=nt)[0],403)

import importlib.util,json,time,unittest
from pathlib import Path

spec=importlib.util.spec_from_file_location('readiness_server',Path(__file__).parents[1]/'server.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class ReadinessTests(unittest.TestCase):
    def agent(self,**kw):
        a=dict(kind='agent',executor='codex',id='sample',name='Synthetic pending agent',description='',owner_id='owner',visibility='private',aid='b'+'a'*58,enabled=1,last_seen=int(time.time())-15,node_connected=1,policy_ok=0,executor_ready=0,created_at=1,capabilities=None,runtime=None,work=None)
        a.update(kw);return a
    def view(self,**kw):return m.agent_view(self.agent(**kw),'owner','owner')
    def test_synthetic_agent_shape_does_not_promote_heartbeat_to_execution(self):
        v=self.view()
        self.assertEqual(v['node_connected'],1);self.assertEqual(v['status'],'policy_pending')
        self.assertFalse(v['executor_ready']);self.assertFalse(v['policy_ok']);self.assertIsNone(v['reported_capabilities'])
        self.assertFalse(v['long_task']);self.assertFalse(v['requester_ready'])
        self.assertEqual(v['work']['state'],'unknown')
    def test_fresh_connector_heartbeat_does_not_hide_disconnected_node(self):
        v=self.view(node_connected=0,executor_ready=1,policy_ok=1)
        self.assertEqual(v['status'],'offline');self.assertFalse(v['long_task'])
    def test_capability_declaration_separate_from_dispatch_readiness(self):
        c=json.dumps({'protocol':'taskd/1','native':{'available':True,'harness':'codex','persistent_session':True,'private':'do not expose'}})
        v=self.view(capabilities=c)
        self.assertIsNone(v['capabilities']);self.assertFalse(v['long_task'])
        self.assertEqual(v['reported_capabilities']['native'],{'available':True,'harness':'codex','persistent_session':True})
        self.assertNotIn('do not expose',json.dumps(v))
        self.assertIsNone(self.view(capabilities=c,last_seen=int(time.time())-200)['reported_capabilities'])
    def test_harness_names_and_reasons_are_whitelisted_not_inferred(self):
        for native in [{'available':True,'harness':[]},{'available':False,'harness':'custom','reason':'secret=some-private-string'},{'available':False,'harness':'pi','reason':'adapter_has_no_verified_native_goals'}]:
            c=m.long_task_caps(json.dumps({'protocol':'taskd/1','native':native}))
            self.assertNotIn('some-private-string',json.dumps(c))
            if native['harness']==[]:self.assertNotIn('harness',c['native'])
            self.assertEqual(c['native']['available'],native['available'])
        self.assertIsNone(m.long_task_caps(json.dumps({'protocol':'taskd/1'}))['native'])
    def test_private_task_content_does_not_leak_into_new_capability_fields(self):
        work={'schema':1,'source':'taskd','scope':'anet_tasks','observed_at':time.time(),'counts':{'running':1},'content_synced':True,'tasks':[{'id':'t1','state':'running','goal':'private-goal'}]}
        a=self.agent(policy_ok=1,executor_ready=1,work=json.dumps(work),share_content=0)
        v=m.agent_view(a,'owner','someone-else')
        self.assertEqual(v['work']['tasks'],[]);self.assertNotIn('private-goal',json.dumps(v))

if __name__=='__main__':unittest.main()

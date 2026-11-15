"""Interaction sync: what leaves the node is bounded, masked and opt-in."""
import importlib.util, tempfile, unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('connector',Path(__file__).parents[1]/'connector.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
PEER='b'+'q'*58
def thread(ix,state='working',n=1):
    msgs=[{'msg_id':'m-%07d'%i,'from':'me' if i%2==0 else 'them','kind':'text','body':'hello %d'%i,'created_at':'2026-11-06T12:00:%02dZ'%i} for i in range(n)]
    msgs.insert(1,{'msg_id':'s-0000001','from':'them','kind':'status','body':'','created_at':'2026-11-06T12:00:00Z'})
    return {'interaction_id':ix,'role':'outbound','peer':PEER,'goal':'goal Bearer abcdefghijklmnopqrstuvwxyz','state':state,'created_at':'2099-01-01T00:00:00Z','updated_at':'2099-01-01T00:00:09Z','messages':msgs}
class SyncTest(unittest.TestCase):
    def test_record_masks_secrets_and_drops_status_heartbeats(self):
        t=thread('ix_'+'a'*32,n=2);t['messages'][0]['body']='api_key=abc123456789 and sk-abcdefghijklmnopqrstuvwxyz1234';t['messages'][0]['attachments']=[{'name':'a.txt','size':3,'mime':'text/plain','cid':'private'}]
        r=m.thread_record(t)
        self.assertEqual(len(r['messages']),2);self.assertNotIn('abc123456789',str(r));self.assertNotIn('sk-abcdef',str(r));self.assertNotIn('Bearer abcdefgh',str(r));self.assertNotIn('private',str(r))
        self.assertEqual(r['state'],'working')
        self.assertEqual(m.thread_record({**t,'state':'input_required'})['state'],'input-required')
    def test_scan_sends_only_changes_and_rereads_finished_tasks_once(self):
        a,b='ix_'+'a'*32,'ix_'+'b'*32
        state={'a':('TASK_STATE_WORKING','t1'),'b':('TASK_STATE_COMPLETED','t1')};msgs={'a':1,'b':3};reads=[]
        def run(*cmd,decode=False,timeout=30):
            if cmd[:2]==('task','list'):return {'tasks':[{'id':a,'status':{'state':state['a'][0],'timestamp':state['a'][1]}},{'id':b,'status':{'state':state['b'][0],'timestamp':state['b'][1]}}]}
            reads.append(cmd[1]);k='a' if cmd[1]==a else 'b';return {'thread':thread(cmd[1],state[k][0].split('_')[-1].lower(),msgs[k])}
        sent=[];m.push_interactions=lambda st,recs:sent.append([r['id'] for r in recs]);seen={}
        self.assertEqual(m.run_sync(run,{},seen),2);self.assertEqual(sent,[[a,b]])
        reads.clear();self.assertEqual(m.run_sync(run,{},seen),0);self.assertEqual(reads,[a])  # finished task is not re-read
        msgs['a']=2;self.assertEqual(m.run_sync(run,{},seen),1);self.assertEqual(sent[-1],[a])
    def test_taskd_command_executor_serves_non_codex_harnesses(self):
        self.assertTrue(m.taskd_serves({'executor':'codex'},'codex'))
        self.assertTrue(m.taskd_serves({'executor':'command'},'pi'))
        self.assertTrue(m.taskd_serves({'executor':'command'},'custom'))
        self.assertFalse(m.taskd_serves({'executor':'command'},'codex'))
        self.assertFalse(m.taskd_serves({'executor':'codex'},'pi'))
    def test_runtime_of_a_taskd_node_reports_its_work_dir_from_the_taskd_config(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'taskd').mkdir();import json,time
            (root/'taskd'/'status.json').write_text(json.dumps({'ts':time.time(),'accept_tasks':True,'version':'1.1.0','executor':'command'}))
            (root/'taskd'/'config.json').write_text(json.dumps({'work_dir':'/data/ANet','read_paths':['/data/WorldBridge4D'],'probes':[],'executor':{'type':'command'}}))
            (root/'portal').mkdir()
            r=m.runtime_info({'version':'x'},{},'pi',root)
        self.assertEqual((r['work_dir'],r['read_paths'],r['mode'],r['agent'],r['taskd_version'],r['gui']),('/data/ANet',['/data/WorldBridge4D'],'taskd','command','1.1.0',False))
    def test_flag_is_off_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'portal').mkdir();self.assertFalse(m.sync_enabled(root));m.sync_flag(root).write_text('1');self.assertTrue(m.sync_enabled(root))
if __name__=='__main__':unittest.main()

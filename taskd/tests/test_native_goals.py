import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from helpers import Scripted,make_engine,wait_for,plan
from taskd.executors.base import ExecResult
from taskd.executors.codex import CodexExecutor
from taskd.executors import native_codex
from taskd.resources import Resources

CHECK={'id':'result','type':'value','file':'out/result.json','path':'ok','expect':True}

class NativeDouble(Scripted):
    def __init__(self, fn=None, available=True):
        super().__init__(lambda *a:plan([{'final_checks':['result']}]))
        self.fn_goal=fn;self.goal_calls=[];self.available=available
    def native_capability(self):return {'available':self.available,'reason':'probe_fixture'}
    def run_goal(self,objective,**kw):
        self.goal_calls.append((objective,kw.get('session')))
        kw['checkpoint'](kw.get('session') or 'native-thread')
        if self.fn_goal:return self.fn_goal(objective,**kw)
        out=Path(kw['work_dir'])/'out';out.mkdir(exist_ok=True);(out/'result.json').write_text('{"ok":true}')
        r=ExecResult(True,'done','native-thread',duration=.1);r.native_turns=1;return r

class NativeEngineTests(unittest.TestCase):
    def setup_engine(self,ex=None,**cfg):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        ex=ex or NativeDouble();e,out,store,work=make_engine(tmp.name,ex,**cfg)
        def cleanup():
            e.shutdown()
            for th in list(e.workers.values()):th.join(3)
        self.addCleanup(cleanup)
        return e,out,store,work,ex
    def submit(self,e,**kw):return e.intake_new('t','peer','m',json.dumps(dict(taskd='submit',goal='produce checked output',acceptance=[CHECK],**kw)))
    def settled(self,e,store,state):
        self.assertTrue(wait_for(lambda:store.load('t')['state']==state),store.load('t'))
        self.assertTrue(wait_for(lambda:not e.workers))
    def test_native_observations_clear_stop_and_retry_on_new_turn(self):
        def run(obj, **kw):
            emit = kw['emit']
            emit('native/stopping', {})
            emit('native/stopped', {'confirmed': False})
            emit('native/retry', {'reason': 'model_at_capacity', 'attempt': 2})
            emit('turn/started', {'turn': {'id': 'round-1'}})
            emit('turn/started', {'turn': {'id': 'round-1'}})  # duplicate notification
            emit('turn/completed', {'turn': {'id': 'round-1', 'status': 'completed'}})
            emit('turn/started', {'turn': {'id': 'round-2'}})
            out = Path(kw['work_dir'])/'out'; out.mkdir(exist_ok=True)
            (out/'result.json').write_text('{"ok":true}')
            return ExecResult(True, 'done', 'native-thread', duration=.1)
        e, o, s, w, x = self.setup_engine(NativeDouble(run))
        self.submit(e); self.settled(e, s, 'completed')
        native = s.load('t')['native']
        self.assertEqual(native['iteration'], 2)
        self.assertEqual(native['turn_status'], 'inProgress')
        self.assertFalse(native['stop_requested'])
        self.assertNotIn('stop', native)
        self.assertNotIn('retry', native)
        self.assertGreater(native['observed_at'], 0)

    def test_new_stop_clears_old_confirmation_and_output_clears_retry(self):
        observations = []
        def run(obj, **kw):
            emit = kw['emit']
            emit('native/stopped', {'confirmed': True})
            emit('native/stopping', {})
            observations.append(dict(s.load('t')['native']))
            emit('native/retry', {'reason': 'model_at_capacity', 'attempt': 2, 'at': time.time()})
            emit('item/commandExecution/outputDelta', {'text': 'private output'})
            observations.append(dict(s.load('t')['native']))
            out = Path(kw['work_dir'])/'out'; out.mkdir(exist_ok=True)
            (out/'result.json').write_text('{"ok":true}')
            return ExecResult(True, 'done', 'native-thread', duration=.1)
        e, o, s, w, x = self.setup_engine(NativeDouble(run))
        self.submit(e); self.settled(e, s, 'completed')
        self.assertTrue(observations[0]['stop_requested'])
        self.assertNotIn('stop', observations[0])
        self.assertNotIn('retry', observations[1])

    def test_auto_routes_native_no_planner_and_keeps_independent_acceptance(self):
        e,out,s,w,x=self.setup_engine();self.submit(e,budget={'max_steps':1});self.settled(e,s,'completed')
        self.assertEqual(x.kinds(),[]);self.assertEqual(len(x.goal_calls),1)
        self.assertEqual(s.load('t')['sessions_used'],1);self.assertEqual(s.load('t')['used']['replans'],0)
        rep=json.loads((s.task_dir('t')/'report.json').read_text())
        self.assertEqual(rep['orchestration'],'native');self.assertEqual(rep['checks'][0]['outcome'],'pass')
    def test_native_claim_cannot_override_failed_acceptance_same_thread_repairs(self):
        calls=[]
        def run(obj,**kw):
            calls.append(kw['session'])
            if len(calls)==2:
                out=Path(kw['work_dir'])/'out';out.mkdir();(out/'result.json').write_text('{"ok":true}')
            return ExecResult(True,'claimed done','native-thread',duration=.1)
        e,o,s,w,x=self.setup_engine(NativeDouble(run));self.submit(e);self.settled(e,s,'completed')
        self.assertEqual(calls,[None,'native-thread']);self.assertEqual(s.load('t')['used']['verify_rounds'],2)
        self.assertEqual(x.kinds(),[])
    def test_explicit_resume_after_verification_limit_keeps_same_thread(self):
        calls=[]
        def run(obj,**kw):
            calls.append(kw['session'])
            if len(calls)==2:
                self.assertIn('Independent acceptance failed', (Path(kw['work_dir'])/'.taskd-native-instructions.txt').read_text())
                out=Path(kw['work_dir'])/'out';out.mkdir();(out/'result.json').write_text('{"ok":true}')
            return ExecResult(True,'claimed done','native-thread')
        e,o,s,w,x=self.setup_engine(NativeDouble(run));e.policy['max_verify_rounds']=1
        self.submit(e);self.settled(e,s,'blocked')
        e.intake_message('t','resume','{"taskd":"resume"}');self.settled(e,s,'completed')
        self.assertEqual(calls,[None,'native-thread']);self.assertEqual(x.kinds(),[])
    def test_require_native_rejects_unsupported_without_executor(self):
        e,o,s,w,x=self.setup_engine(NativeDouble(available=False))
        self.assertIsNone(self.submit(e,orchestration='native'));self.assertEqual(x.calls,[])
    def test_planned_opt_in_bypasses_native_probe(self):
        e,o,s,w,x=self.setup_engine()
        x.native_capability=lambda:(_ for _ in ()).throw(AssertionError('must not probe'))
        with patch.object(e,'schedule'):
            t=self.submit(e,orchestration='planned')
        self.assertEqual(t['spec']['orchestration'],'planned')
    def test_unsupported_auto_fallback_explained_before_execution(self):
        e,o,s,w,x=self.setup_engine(NativeDouble(available=False))
        with patch.object(e,'schedule'):t=self.submit(e)
        self.assertEqual(t['spec']['orchestration'],'planned')
        self.assertEqual(t['spec']['routing_reason'],'probe_fixture')
    def test_short_run_does_not_probe_native(self):
        e,o,s,w,x=self.setup_engine();x.native_capability=lambda:(_ for _ in ()).throw(AssertionError('must not probe'))
        with patch.object(e,'schedule'):
            t=e.intake_new('t','peer','m',json.dumps({'taskd':'run','goal':'small','acceptance':[CHECK]}))
        self.assertEqual(t['spec']['orchestration'],'direct')
    def test_unknown_outcome_blocks_no_fallback_and_retains_workspace(self):
        x=NativeDouble(lambda obj,**kw:ExecResult(False,error='native_outcome_unknown: connection lost'))
        e,o,s,w,x=self.setup_engine(x);self.submit(e);self.settled(e,s,'blocked')
        self.assertEqual(len(x.goal_calls),1);self.assertEqual(x.kinds(),[])
        self.assertIn('t',e.resources.quarantine)
        self.assertTrue(e.resources._blockers({'task_id':'another','resource':'background','workspace':str(w.resolve())})[1])
    def test_cancel_unknown_reconciles_cleanup_even_after_budget_exhausted(self):
        calls=[]
        def run(obj,**kw):
            calls.append(kw)
            if len(calls)==1:return ExecResult(False,error='native_outcome_unknown: disconnected')
            return ExecResult(False,error='canceled',canceled=True)
        e,o,s,w,x=self.setup_engine(NativeDouble(run));self.submit(e,budget={'max_steps':1})
        self.settled(e,s,'blocked')
        e.intake_message('t','cancel','{"taskd":"cancel"}');self.settled(e,s,'stopped')
        self.assertEqual(s.load('t')['stop_reason'],'canceled')
        self.assertTrue(calls[1]['stop_only']);self.assertEqual(calls[1]['session'],'native-thread')
        self.assertEqual(e.resources.quarantine,{})
    def test_interrupted_quarantine_survives_second_restart(self):
        e,o,s,w,x=self.setup_engine()
        with patch.object(e,'schedule'):
            self.submit(e)
            t=s.load('t');t['step_in_progress']={'kind':'native'};s.save(t)
            e.recover()
            self.assertTrue(s.load('t')['native_quarantine'])
            e.resources=Resources();e.recover()
        self.assertIn('t',e.resources.quarantine)
    def test_hint_delivered_during_native_execution(self):
        seen=[];started=threading.Event()
        def run(obj,**kw):
            started.set()
            end=time.time()+3
            while time.time()<end:
                msg=kw['messages']()
                if msg:seen.append(msg);break
                time.sleep(.01)
            return ExecResult(False,error='native_paused')
        e,o,s,w,x=self.setup_engine(NativeDouble(run));self.submit(e);self.assertTrue(started.wait(2))
        e.intake_message('t','m2','use new evidence');self.settled(e,s,'blocked')
        self.assertEqual(seen,['use new evidence'])
    def test_pause_and_resume_same_thread(self):
        start=threading.Event();calls=[]
        def run(obj,**kw):
            calls.append(kw['session'])
            if len(calls)==1:
                start.set();kw['cancel'].wait(3)
                return ExecResult(False,error='canceled',canceled=True)
            out=Path(kw['work_dir'])/'out';out.mkdir();(out/'result.json').write_text('{"ok":true}')
            return ExecResult(True,'done','native-thread')
        e,o,s,w,x=self.setup_engine(NativeDouble(run));self.submit(e);self.assertTrue(start.wait(2))
        e.intake_message('t','m2','{"taskd":"pause"}');self.settled(e,s,'blocked')
        self.assertEqual(s.load('t')['block_kind'],'native_paused')
        e.intake_message('t','m3','{"taskd":"resume"}');self.settled(e,s,'completed')
        self.assertEqual(calls,[None,'native-thread'])
    def test_native_natural_acceptance_deferred_without_reviewer(self):
        e,o,s,w,x=self.setup_engine()
        e.intake_new('t','peer','m',json.dumps({'taskd':'submit','goal':'produce checked output','acceptance':['works correctly']}))
        self.settled(e,s,'completed')
        rep=json.loads((s.task_dir('t')/'report.json').read_text());self.assertEqual(rep['deferred'],['a1'])

class RpcDouble:
    calls=[];finish=True;approval=False
    def __init__(self,executor,on_event):self.on_event=on_event;self.goal=None
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def rpc(self,method,params,**kw):
        self.calls.append((method,params))
        if method in ['thread/start','thread/resume','thread/read']:return {'thread':{'id':'thread','status':{'type':'idle'}}}
        if method=='thread/goal/get':return {'goal':self.goal}
        if method=='thread/goal/set':
            self.goal={'status':params['status']}
            if params['status']=='active':
                self.on_event({'method':'turn/started','params':{'turn':{'id':'turn'}}})
                if self.approval:self.on_event({'method':'native/needsApproval','params':{}})
                if self.finish:
                    self.goal={'status':'complete'}
                    self.on_event({'method':'thread/goal/updated','params':{'goal':self.goal}})
                    self.on_event({'method':'item/completed','params':{'item':{'type':'agentMessage','phase':'final_answer','text':'done'}}})
                    self.on_event({'method':'turn/completed','params':{'turn':{'status':'completed'}}})
            return {'goal':self.goal}
        return {}
    def receive(self,*a):time.sleep(.005);raise TimeoutError()
    def dispatch(self,v):self.on_event(v)

class NativeProtocolTests(unittest.TestCase):
    def run_native(self,**kw):
        self.events=[];self.saved=[];RpcDouble.calls=[]
        with patch.object(native_codex,'NativeRPC',RpcDouble),patch.object(native_codex,'retry_state',return_value=None):
            return native_codex.run(CodexExecutor(), 'do it',work_dir='.',session=None,timeout=.025,max_turns=2,
                cancel=threading.Event(),emit=lambda m,p:self.events.append((m,p)),checkpoint=self.saved.append,messages=lambda:'',**kw)
    def tearDown(self):RpcDouble.finish=True;RpcDouble.approval=False
    def test_early_notifications_are_not_lost_and_no_duplicate_turn_start(self):
        r=self.run_native();self.assertTrue(r.ok);self.assertEqual(self.saved,['thread'])
        self.assertNotIn('turn/start',[m for m,p in RpcDouble.calls])
    def test_timeout_pauses_interrupts_and_cleans_before_reporting_stop(self):
        RpcDouble.finish=False;r=self.run_native();self.assertFalse(r.ok)
        methods=[m for m,p in RpcDouble.calls]
        self.assertIn('turn/interrupt',methods);self.assertIn('thread/backgroundTerminals/clean',methods)
        self.assertTrue(self.events[-1][1]['confirmed'])
    def test_approval_need_blocks_without_granting_privilege(self):
        RpcDouble.finish=False;RpcDouble.approval=True;r=self.run_native()
        self.assertEqual(r.error,'native_approval_required')
    def test_quarantined_resource_allows_only_own_reconciliation(self):
        r=Resources();r.retain('t','desktop','/tmp/a')
        self.assertEqual(r._blockers({'task_id':'t','resource':'desktop','workspace':'/tmp/a'}),(None,[]))
        self.assertEqual(r._blockers({'task_id':'other','resource':'desktop','workspace':'/tmp/b'})[0],'waiting_desktop')
        r.release_retained('t');self.assertEqual(r.quarantine,{})
    def test_ephemeral_probe_accepts_only_precise_supported_method_rejection(self):
        class ProbeRPC(RpcDouble):
            def __init__(self,ex):super().__init__(ex,lambda e:None)
            def rpc(self,method,params,**kw):
                if method=='thread/goal/get':raise native_codex.RPCError(self.error)
                return super().rpc(method,params,**kw)
        with patch.object(native_codex,'NativeRPC',ProbeRPC):
            ProbeRPC.error='ephemeral thread does not support goals'
            self.assertTrue(native_codex.probe(CodexExecutor())['available'])
            ProbeRPC.error='unknown method thread/goal/get'
            self.assertFalse(native_codex.probe(CodexExecutor())['available'])
    def test_completed_thread_reconciles_without_activating_or_starting_turn(self):
        class CompleteRPC(RpcDouble):
            def rpc(self,method,params,**kw):
                if method=='thread/goal/get':return {'goal':{'status':'complete'}}
                if method=='thread/read':return {'thread':{'turns':[{'status':'completed','items':[
                    {'type':'agentMessage','phase':'final_answer','text':'already done'}]}]}}
                return super().rpc(method,params,**kw)
        RpcDouble.calls=[]
        with patch.object(native_codex,'NativeRPC',CompleteRPC):
            r=native_codex.run(CodexExecutor(),'do it',work_dir='.',session='thread',timeout=1,max_turns=2,
                cancel=threading.Event(),emit=lambda *a:None,checkpoint=lambda *a:None,messages=lambda:'')
        self.assertTrue(r.ok);self.assertEqual(r.text,'already done');self.assertEqual(r.native_turns,0)
        self.assertNotIn('thread/goal/set',[m for m,p in RpcDouble.calls])
    def test_cleanup_only_never_activates_goal(self):
        r=self.run_native(stop_only=True);self.assertTrue(r.canceled)
        self.assertFalse(any(m=='thread/goal/set' and p.get('status')=='active' for m,p in RpcDouble.calls))

if __name__=='__main__':unittest.main()

import copy
import json
import tempfile
import threading
import unittest

from helpers import Scripted, make_engine, plan, report, wait_for
from taskd.protocol import normalize_control, Rejected
from taskd.recovery import prepare_revision


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.e, self.out, self.store, self.work = make_engine(self.tmp.name, Scripted(lambda *a: report()),
            policy={'max_replans': 0, 'no_progress_steps': 1})
        self.e.stopping.set()
        self.e.intake_new('t1', 'peer', 'm1', json.dumps({'taskd':'submit','goal':'repair', 'acceptance':[
            {'id':'result', 'type':'files', 'pattern':'result.txt'}]}))
        t = self.store.load('t1')
        t['plan'] = self.e._validate_plan(t, plan([{'id':'p1','final_checks':['result']}]))
        t['plan'].pop('new_checks'); t['state']='blocked'; t['used']['replans']=2
        self.store.save(t)
    def request(self, **kw):
        return dict({'taskd':'revise_plan','request_id':'r1','base_revision':0,'from_phase':'p1',
            'reason':'repair configuration before desktop', 'plan':plan([
                {'id':'repair','resource':'background'}, {'id':'verify','final_checks':['result']}])}, **kw)
    def send(self, req, mid='m2'):
        self.e.intake_message('t1',mid,json.dumps(req))
    def test_revision_after_replans_exhausted_keeps_budget_and_acceptance(self):
        before=self.store.load('t1'); self.send(self.request())
        self.assertEqual(self.store.load('t1')['pending_revision']['from_index'],0)
        t=self.store.load('t1'); self.e._apply_revision(t)
        after=self.store.load('t1')
        self.assertEqual(before['spec'],after['spec'])
        for key,value in before['used'].items():self.assertEqual(after['used'][key],value)
        self.assertEqual(after['used']['plan_revisions'],1)
        self.assertEqual(after['plan_revision'],1)
        self.assertEqual(after['plan']['phases'][0]['id'],'repair')
        self.assertEqual(after['id'],'t1')
    def test_duplicate_idempotency_and_stale_conflicting_rejection(self):
        req=self.request();self.send(req);t=self.store.load('t1');self.e._apply_revision(t)
        self.send(req,'m3');self.assertIsNone(self.store.load('t1').get('pending_revision'))
        self.send(self.request(reason='changed'),'m4');self.assertIn('different content',self.out.sent[-1]['text'])
        self.send(self.request(request_id='r2'),'m5');self.assertIn('stale',self.out.sent[-1]['text'])
    def test_no_acceptance_or_budget_overrides(self):
        with self.assertRaises(Rejected):normalize_control(self.request(budget={'max_steps':9999}))
        t=self.store.load('t1');t['used']['plan_revisions']=3;self.store.save(t)
        self.send(self.request());self.assertIn('limit reached',self.out.sent[-1]['text'])
    def test_old_preflight_must_not_be_removed(self):
        t=self.store.load('t1');t['plan']['phases'][0]['preflight']=['result'];self.store.save(t)
        # Retaining a final check only is not retaining a mandatory preflight.
        self.send(self.request());self.assertIsNone(self.store.load('t1').get('pending_revision'))
    def test_plain_continue_schedules_bounded_retry_of_stalled_work(self):
        t=self.store.load('t1');t['block_kind']='stalled';self.store.save(t)
        self.e.intake_message('t1','m2',json.dumps({'taskd':'hint','text':'continue'}))
        self.assertEqual(self.store.load('t1')['state'],'running')
        self.assertEqual(self.e.executor.calls,[])
    def test_revision_wakes_desktop_waiter_without_taking_desktop(self):
        def execute(kind,prompt,call):
            (self.work/'result.txt').write_text('done');return report('claims_done',artifacts=['result.txt'])
        self.e.executor=Scripted(execute)
        t=self.store.load('t1');t['state']='running';t['plan']['phases'][0]['resource']='desktop';self.store.save(t)
        with self.e.resources.lease('owner','desktop',self.work.parent/'other'):
            self.e.stopping.clear();self.e.schedule()
            self.assertTrue(wait_for(lambda:(self.e.resources.status('t1') or {}).get('state')=='waiting_desktop'))
            self.send(self.request())
            self.assertTrue(wait_for(lambda:self.store.load('t1')['state']=='completed'))
            self.assertIn('owner',self.e.resources.owners)
    def test_revision_rechecks_prior_passes_and_preserves_prior_files(self):
        t=self.store.load('t1');t['progress']['check_state']['result']='pass';self.store.save(t)
        self.store.log('t1','checks',results={'result':{'outcome':'pass'}})
        (self.work/'old.txt').write_text('preserved')
        self.send(self.request());self.e._apply_revision(self.store.load('t1'))
        self.assertEqual(self.e._latest_checks('t1')['result']['outcome'],'not_run')
        self.assertEqual((self.work/'old.txt').read_text(),'preserved')
    def test_revision_approval_and_pending_survive_reload(self):
        req=self.request(plan=plan([{'id':'repair','final_checks':['result']}],reasons=['new permission required']))
        self.send(req)
        self.e._apply_revision(self.store.load('t1'))
        self.assertEqual(self.store.load('t1')['state'],'awaiting_approval')
    def test_preflight_blocks_before_executor(self):
        t=self.store.load('t1');t['state']='running';t['plan']['phases'][0]['preflight']=['result'];self.store.save(t)
        self.e._step('t1');self.assertEqual(self.store.load('t1')['state'],'blocked')
        self.assertEqual(self.e.executor.calls,[])
        self.assertEqual(self.store.load('t1')['blocker']['action'],'repair_configuration')
    def test_automatic_replan_retains_completed_dependencies_and_same_phase_gates(self):
        t=self.store.load('t1')
        old=plan([{'id':'setup','preflight':['result'],'produces':['input.txt']},
                  {'id':'p1','requires':['input.txt'],'preflight':['result']}])
        # Seed a persisted plan whose completed producer must remain available.
        old['phases'][0].update(status='done',verified=True)
        old['phases'][1]['status']='pending'
        t['plan']=old;t['phase_index']=1
        replacement=plan([{'id':'p1','requires':['input.txt'],'final_checks':['result']}])
        result=self.e._validate_replan(t,replacement)
        self.assertEqual(result['phases'][0],old['phases'][0])
        self.assertEqual(result['phases'][1]['preflight'],['result'])
        self.assertEqual(replacement['phases'][0]['preflight'],[])
    def test_automatic_replan_refuses_to_guess_gate_placement_for_replaced_phase(self):
        t=self.store.load('t1');t['plan']['phases'][0]['preflight']=['result']
        with self.assertRaisesRegex(ValueError,'previous phase assignments.*p1.*result'):
            self.e._validate_replan(t,plan([{'id':'repair'}]))
    def test_replan_retry_reports_known_ids_and_rejected_plan(self):
        t=self.store.load('t1');t['plan']['phases'][0]['preflight']=['result'];self.store.save(t)
        invalid=plan([{'id':'p1','checks':['some explanatory prose']}])
        valid=plan([{'id':'p1','final_checks':['result']}])
        self.e.executor=Scripted(lambda kind,prompt,call: invalid if call['n']==0 else valid)
        self.e._plan('t1',replan='repair remaining work')
        t=self.store.load('t1')
        self.assertEqual(t['state'],'running')
        self.assertEqual(t['plan']['phases'][0]['preflight'],['result'])
        prompt=self.e.executor.calls[1]['prompt']
        self.assertIn('Existing phase gates',prompt)
        self.assertIn('Rejected plan (data)',prompt)
        self.assertIn('some explanatory prose',prompt)
        self.assertIn('result',prompt)
    def test_plan_failed_returns_diagnostic_attachment(self):
        t=self.store.load('t1')
        self.store.log('t1','plan_invalid',error='missing preflight: result')
        self.e._stop(t,'plan_failed')
        message=next(m for m in self.out.sent if 'planning-diagnostics.json' in m['files'])
        self.assertEqual(message['state'],'working')
        diagnostic=json.loads((self.store.task_dir('t1')/'planning-diagnostics.json').read_text())
        self.assertIn('result',diagnostic['invalid_plans'][0]['error'])
    def test_malformed_replan_gates_are_validation_errors_not_worker_crashes(self):
        for fields in ({'preflight':[{'description':'not an id'}]}, {'id':['not an id']}, {'preflight':None}):
            with self.subTest(fields=fields),self.assertRaisesRegex(ValueError,'plan schema'):
                self.e._validate_replan(self.store.load('t1'),plan([fields]))
    def test_inflight_revision_waits_and_releases_desktop(self):
        entered=threading.Event(); release=threading.Event(); observed=[]
        def execute(kind,prompt,call):
            if not entered.is_set():entered.set();release.wait(4);return report()
            observed.append(self.e.resources.status('t1')['resource'])
            (self.work/'result.txt').write_text('done')
            return report('claims_done',artifacts=['result.txt'])
        self.e.executor=Scripted(execute)
        t=self.store.load('t1');t['state']='running';t['plan']['phases'][0]['resource']='desktop';self.store.save(t)
        self.e.stopping.clear();self.e.schedule();self.assertTrue(entered.wait(3))
        self.send(self.request());self.assertEqual(self.store.load('t1')['plan_revision'] if 'plan_revision' in self.store.load('t1') else 0,0)
        release.set();self.assertTrue(wait_for(lambda:self.store.load('t1')['state']=='completed'))
        self.assertEqual(observed,['background','background'])
        self.assertEqual(self.e.resources.owners,{})

if __name__=='__main__':unittest.main()

"""Reproduce the EPS directory gate, resource mismatch and unrecoverable revision loop."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from helpers import Scripted, make_engine, plan, report
from taskd.checkers.base import Checker
from taskd.protocol import normalize_control, Rejected
from taskd.store import Store


class BrokenChecker(Checker):
    name = 'broken'
    def validate(self, params):
        return dict(params)
    def check(self, params, ctx, baseline=None):
        raise RuntimeError('deterministic checker exception')


class OrchestrationFaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.e,self.out,self.store,self.work=make_engine(self.tmp.name,Scripted(lambda *args:report('claims_done')))
        self.e.stopping.set()
        self.e.intake_new('t','peer','m1',json.dumps({'taskd':'submit','goal':'validate native experiments',
            'acceptance':[{'id':'result','type':'files','pattern':'out/result.txt'}]}))

    def seed(self, phases, checks=()):
        t=self.store.load('t');t['checks']+=copy.deepcopy(list(checks))
        p=self.e._validate_plan(t,plan(phases));t['checks']+=p.pop('new_checks')
        t.update(plan=p,state='running',phase_index=0);self.store.save(t)
        return t

    def legacy_directory_gate(self):
        # The real reported phase/path and its deterministic old generated ID.
        t=self.seed([{'id':'p2','produces':['work/p0-evidence/']}])
        gate=next(c for c in t['checks'] if c.get('generated_for'))
        self.assertEqual(gate['id'],'phase_4818c8fa0b0abc43b9d3')
        gate.pop('generated_for');gate['origin']='drafted';gate['params']={'pattern':'work/p0-evidence/','count':1,'min_size':0}
        self.store.save(t)
        (self.work/'work/p0-evidence').mkdir(parents=True)
        return gate['id']

    def revision(self, cid=None, **changes):
        t=self.store.load('t');phase=t['plan']['phases'][t.get('phase_index',0)]['id'] if t.get('plan') else 'initial'
        proposal=plan([{'id':phase if phase!='initial' else 'p1',
                         'checks':[cid] if cid else [],'final_checks':['result']}])
        request={'taskd':'revise_plan','request_id':'repair-1','base_revision':t.get('plan_revision',0),
                 'from_phase':phase,'reason':'repair the framework gate, preserve final acceptance','plan':proposal}
        if cid:
            request['check_updates']=[{'id':cid,'type':'files','params':{'pattern':'work/p0-evidence/','kind':'directory'},
                                      'reason':'generated file-only gate targets an output directory'}]
        request.update(changes)
        self.e.intake_message('t','m2',json.dumps(request))
        return request

    def test_generated_directory_and_extensionless_file_complete_phase(self):
        t=self.seed([{'produces':['evidence/','plain-output']}])
        (self.work/'evidence').mkdir();(self.work/'plain-output').write_bytes(b'')
        self.e._step('t');after=self.store.load('t')
        self.assertEqual(after['plan']['phases'][0]['status'],'done')
        self.assertEqual(after['state'],'verifying')
        # Directory existence never satisfies the original final file acceptance.
        self.assertFalse((self.work/'out/result.txt').exists())

    def test_ambiguous_legacy_directory_declaration_uses_existence_gate(self):
        t=self.seed([{'produces':['evidence']}]);(self.work/'evidence').mkdir()
        gate=next(c for c in t['checks'] if c.get('generated_for'))
        self.assertEqual(gate['params']['kind'],'any')
        self.e._step('t');self.assertEqual(self.store.load('t')['state'],'verifying')

    def test_explicit_file_acceptance_does_not_accept_a_directory(self):
        t=self.seed([{}]);(self.work/'out/result.txt').mkdir(parents=True)
        result=self.e.run_checks('t',[t['checks'][0]])['result']
        self.assertEqual(result['outcome'],'fail')
        self.assertIsNone(self.e._system_check_problem(t,{'result':result}))

    def test_incompatible_directory_size_and_kind_are_rejected(self):
        checker=self.e.checkers['files']
        for params in ({'pattern':'out','kind':'invalid'},{'pattern':'out','kind':'directory','min_size':1}):
            with self.subTest(params=params),self.assertRaises(ValueError):checker.validate(params)

    def test_wrong_output_type_is_business_failure_when_directory_gate_is_correct(self):
        t=self.seed([{'produces':['evidence/']}]);(self.work/'evidence').write_text('wrong producer output')
        gate=next(c for c in t['checks'] if c.get('generated_for'))
        result=self.e.run_checks('t',[gate])[gate['id']]
        self.assertEqual(result['outcome'],'fail')
        self.assertIsNone(self.e._system_check_problem(t,{gate['id']:result}))

    def test_background_phase_cannot_declare_live_desktop_requirements(self):
        with self.assertRaisesRegex(ValueError,'desktop lease'):
            self.e._validate_plan(self.store.load('t'),plan([{'requires_desktop':True}]))

    def test_background_phase_with_gui_check_is_rejected(self):
        t=self.store.load('t');t['checks'].append({'id':'gui','type':'window_screenshot','params':{},'scope':'phase'})
        with self.assertRaisesRegex(ValueError,'desktop lease'):
            self.e._validate_plan(t,plan([{'checks':['gui']}]))

    def test_desktop_execution_requires_an_actual_lease(self):
        self.seed([{'resource':'desktop','requires_desktop':True}])
        self.e._step('t');t=self.store.load('t')
        self.assertEqual(t['block_kind'],'resource_mismatch')
        self.assertEqual(self.e.executor.calls,[])
        self.assertEqual(t['used']['steps'],0)

    def test_desktop_execution_under_lease_can_advance(self):
        self.seed([{'resource':'desktop','requires_desktop':True,'produces':['proof.txt']}])
        (self.work/'proof.txt').write_text('observed')
        with self.e.resources.lease('t','desktop',self.work):self.e._step('t')
        self.assertEqual(self.store.load('t')['state'],'verifying')

    def test_executor_resource_mismatch_blocks_without_review_or_repeat(self):
        self.seed([{}])
        self.e.executor=Scripted(lambda *args:report('blocked',blocker={
            'code':'resource_mismatch','detail':'native experiment requires the desktop','evidence':[]}))
        self.e._step('t');t=self.store.load('t')
        self.assertEqual(t['block_kind'],'resource_mismatch')
        self.assertEqual(t['used']['steps'],1);self.assertEqual(t['used']['reviews'],0)
        self.e.intake_message('t','hint','{"taskd":"hint","text":"continue"}')
        self.assertEqual(self.store.load('t')['state'],'blocked')
        self.assertEqual(len(self.e.executor.calls),1)

    def test_legacy_bad_directory_gate_blocks_once_with_complete_delivery(self):
        cid=self.legacy_directory_gate()
        (self.work/'partial.txt').write_text('actual partial evidence')
        self.e.executor=Scripted(lambda *args:report('claims_done',artifacts=['partial.txt']))
        self.e._step('t');t=self.store.load('t')
        self.assertEqual(t['state'],'blocked');self.assertEqual(t['block_kind'],'checker_error')
        self.assertEqual(t['used']['steps'],1);self.assertEqual(t['used']['replans'],0)
        reply=self.out.sent[-1];self.assertEqual(reply['state'],'input-required')
        self.assertTrue({'report.json','orchestration-diagnostics.json','delivery-manifest.json'}<=set(reply['files']))
        manifest=json.loads((self.store.task_dir('t')/'delivery-manifest.json').read_text())
        self.assertTrue(manifest['partial']);self.assertEqual(manifest['business_acceptance'],'not_verified')
        for item in manifest['files']:
            candidates=list(self.store.task_dir('t').rglob(item['name']))
            self.assertTrue(any(p.stat().st_size==item['size'] and hashlib.sha256(p.read_bytes()).hexdigest()==item['sha256'] for p in candidates))
        diagnostic=json.loads((self.store.task_dir('t')/'orchestration-diagnostics.json').read_text())
        self.assertEqual(diagnostic['latest_results'][cid]['outcome'],'fail')
        before=copy.deepcopy(t['used'])
        self.e.intake_message('t','hint','{"taskd":"answer","text":"continue"}')
        self.assertEqual(self.store.load('t')['used'],before)

    def test_checker_exception_blocks_as_system_fault_before_replanning(self):
        self.e.checkers['broken']=BrokenChecker()
        self.seed([{'checks':['bad']}],[{'id':'bad','type':'broken','params':{},'origin':'drafted','scope':'phase'}])
        self.e._step('t');t=self.store.load('t')
        self.assertEqual(t['block_kind'],'checker_error')
        self.assertEqual(t['used']['steps'],1);self.assertEqual(t['used']['replans'],0)
        self.assertIn('deterministic checker exception',t['block_reason'])

    def test_reported_checker_error_is_measured_before_blocking_without_review(self):
        self.legacy_directory_gate()
        self.e.executor=Scripted(lambda *args:report('blocked',blocker={
            'code':'checker_error','detail':'directory cannot satisfy generated file gate','evidence':[]}))
        self.e._step('t');t=self.store.load('t')
        self.assertEqual(t['block_kind'],'checker_error')
        self.assertEqual(t['used']['steps'],1);self.assertEqual(t['used']['reviews'],0)

    def test_unsubstantiated_checker_error_does_not_become_system_proof(self):
        self.seed([{}])
        self.e.executor=Scripted(lambda *args:report('blocked',blocker={
            'code':'checker_error','detail':'unsupported claim','evidence':[]}))
        self.e._step('t');t=self.store.load('t')
        self.assertEqual(t['state'],'running')
        self.assertNotEqual(t.get('block_kind'),'checker_error')

    def test_check_update_repairs_same_gate_id_preserving_spec_history_and_budget(self):
        cid=self.legacy_directory_gate();t=self.store.load('t');t['state']='blocked'
        t['used'].update(steps=13,seconds=1234,replans=2);t['progress']['check_state'][cid]='pass'
        self.store.save(t);before=copy.deepcopy(t)
        self.revision(cid)
        pending=self.store.load('t');self.assertIsNotNone(pending.get('pending_revision'))
        # Queuing never changes the live gate underneath an in-flight step.
        self.assertNotIn('kind',next(c for c in pending['checks'] if c['id']==cid)['params'])
        self.e._apply_revision(pending);after=self.store.load('t')
        self.assertEqual(after['spec'],before['spec'])
        for key,value in before['used'].items():self.assertEqual(after['used'][key],value)
        replacement=next(c for c in after['checks'] if c['id']==cid)
        self.assertEqual(replacement['params']['kind'],'directory')
        self.assertEqual(self.e._latest_checks('t')[cid]['outcome'],'not_run')
        measured=self.e.run_checks('t',[replacement])[cid]
        self.assertEqual(measured['outcome'],'pass')
        audit=self.store.read_log('t','check_replaced')[0]
        self.assertNotIn('kind',audit['before']['params']);self.assertEqual(audit['after']['params']['kind'],'directory')

    def test_requester_acceptance_cannot_be_replaced(self):
        self.seed([{}]);t=self.store.load('t');t['state']='blocked';self.store.save(t)
        self.revision(check_updates=[{'id':'result','type':'files','params':{'pattern':'anything','min_size':0},'reason':'weaken'}])
        self.assertIsNone(self.store.load('t').get('pending_revision'))
        self.assertIn('requester acceptance is immutable',self.out.sent[-1]['text'])

    def test_update_used_by_retained_completed_phase_is_rejected(self):
        cid=self.legacy_directory_gate();t=self.store.load('t')
        t['plan']['phases'][0]['status']='done'
        t['plan']['phases'].append(plan([{'id':'p3'}])['phases'][0]);t['phase_index']=1;t['state']='blocked';self.store.save(t)
        self.revision(cid,from_phase='p3')
        self.assertIsNone(self.store.load('t').get('pending_revision'))
        self.assertIn('retained completed phase',self.out.sent[-1]['text'])

    def test_baseline_protection_cannot_be_replaced(self):
        self.seed([{'checks':['guard']}],[{'id':'guard','type':'dir_unchanged','params':{'path':'protected'},'origin':'drafted','scope':'phase'}])
        self.revision(check_updates=[{'id':'guard','type':'files','params':{'pattern':'elsewhere'},'reason':'replace snapshot'}])
        self.assertIsNone(self.store.load('t').get('pending_revision'))
        self.assertIn('baseline protection',self.out.sent[-1]['text'])

    def test_duplicate_updates_and_invalid_parameters_are_rejected(self):
        cid=self.legacy_directory_gate();entry={'id':cid,'type':'files','params':{'pattern':'work/p0-evidence/','kind':'directory'},'reason':'repair'}
        request={'taskd':'revise_plan','request_id':'x','base_revision':0,'from_phase':'p2','reason':'repair',
                 'plan':plan([{'id':'p2','checks':[cid]}]),'check_updates':[entry,entry]}
        with self.assertRaises(Rejected):normalize_control(request)
        self.revision(check_updates=[dict(entry,params={'pattern':'../outside'})])
        self.assertIsNone(self.store.load('t').get('pending_revision'))

    def test_repeated_same_planning_root_blocks_after_two_calls(self):
        self.e.executor=Scripted(lambda kind,prompt,call:plan([{'checks':['invented_prose_'+str(call['n'])]}]))
        self.e._plan('t');t=self.store.load('t')
        self.assertEqual(t['block_kind'],'planning_error')
        self.assertEqual(t['used']['steps'],2);self.assertEqual(t['used']['replans'],0)
        self.assertIn('orchestration-diagnostics.json',self.out.sent[-1]['files'])
        self.assertEqual(len(self.store.read_log('t','plan_invalid')),2)
        self.revision(from_phase='initial')
        self.assertIsNotNone(self.store.load('t').get('pending_revision'))
        self.e._apply_revision(self.store.load('t'));after=self.store.load('t')
        self.assertEqual(after['state'],'running');self.assertEqual(after['used']['steps'],2)

    def test_planning_retries_do_not_overrun_step_budget(self):
        t=self.store.load('t');t['spec']['budget']['max_steps']=1;self.store.save(t)
        self.e.executor=Scripted(lambda *args:plan([{'checks':['unknown']}]))
        self.e._plan('t');t=self.store.load('t')
        self.assertEqual(t['used']['steps'],1);self.assertEqual(t['stop_reason'],'budget_steps')

    def test_legacy_applied_revision_remains_idempotent_after_schema_upgrade(self):
        self.seed([{}])
        request={'taskd':'revise_plan','request_id':'old-applied','base_revision':0,'from_phase':'p1',
                 'reason':'original repair','plan':plan([{'id':'p1'}])}
        old=normalize_control(copy.deepcopy(request));old.pop('check_updates')
        for phase in old['plan']['phases']:
            phase.pop('requires_desktop');phase.pop('milestones', None)
        t=self.store.load('t');t['revision_requests']={'old-applied':{
            'sha256':hashlib.sha256(json.dumps(old,sort_keys=True).encode()).hexdigest(),'state':'applied'}}
        t['plan_revision']=1;self.store.save(t)
        self.e.intake_message('t','retry',json.dumps(request))
        self.assertIsNone(self.store.load('t').get('pending_revision'))
        self.assertIn('不会重复应用',self.out.sent[-1]['text'])


if __name__=='__main__':unittest.main()

"""Development loops, evidence progress and bounded plain-text recovery."""
from copy import deepcopy
import json
import tempfile
import unittest

from helpers import Scripted, make_engine, plan, report, wait_for
from taskd import prompts
from taskd.checkers.base import Checker
from taskd.resources import resource_for


class DevelopmentRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.e, self.out, self.store, self.work = make_engine(tmp.name, Scripted(lambda *a: report()),
            policy={'max_replans': 0})
        self.e.stopping.set()
        self.e.intake_new('t', 'peer', 'new', json.dumps({'taskd': 'submit', 'goal': 'fix DPI and package',
            'acceptance': [{'id': 'package', 'type': 'files', 'pattern': 'package.txt'}]}))
        t = self.store.load('t')
        p = self.e._validate_plan(t, plan([{'produces': ['tested.txt'], 'milestones': [
            {'id': 'tested', 'title': 'tested fix', 'checks': ['tested']}]}], drafted=[
                {'id': 'tested', 'type': 'files', 'params_json': '{"pattern":"tested.txt"}'}]))
        t['checks'] += p.pop('new_checks')
        t.update(plan=p, state='running')
        self.store.save(t)

    def reviewer(self, verdict='supported'):
        self.e.reviewer = Scripted(lambda *a: {'verdict': verdict, 'reasons': 'comparison against observed evidence',
                                              'missing': []})

    def finding(self, filename='dpi.log', content='DPI 144 differs from expected 96', status='working'):
        (self.work / filename).write_text(content)
        result = report(status, summary=content, facts=[{
            'statement': content, 'basis': 'observed', 'evidence': [filename]}])
        self.e.executor = Scripted(lambda *a: result)
        return result

    def block(self, kind):
        t = self.store.load('t')
        t.update(state='blocked', block_kind=kind, block_reason='old framework failure')
        t['used'].update(replans=2, steps=8, seconds=10)
        t['progress']['no_progress'] = 3
        self.store.save(t)
        return deepcopy(t)

    def test_diagnostic_findings_before_packaging_are_progress(self):
        self.reviewer()
        for i in range(4):
            self.finding(content='validated distinct compatibility finding %d' % i)
            self.e._step('t')
            t = self.store.load('t')
            self.assertEqual(t['state'], 'running')
            self.assertEqual(t['progress']['no_progress'], 0)
            self.assertEqual(t['used']['replans'], 0)
            self.assertNotEqual(t['plan']['phases'][0]['status'], 'done')
        self.assertEqual(len(self.store.read_log('t', 'diagnostic_progress')), 4)
        self.assertFalse((self.work / 'package.txt').exists())
        self.assertEqual(self.store.activity.load('t')['progress_event'], 'diagnostic_progress')
        self.assertIn('previous_findings', self.e.reviewer.calls[-1]['prompt'])
        self.assertIn('validated distinct compatibility finding 0', self.e.reviewer.calls[-1]['prompt'])

    def test_same_content_renamed_or_repeated_is_not_new_progress(self):
        self.reviewer()
        self.finding(); self.e._step('t')
        self.finding(filename='copy.txt'); self.e._step('t')
        self.assertEqual(self.store.load('t')['progress']['no_progress'], 1)
        self.assertEqual(len(self.e.reviewer.calls), 1)
        self.e._step('t')
        self.assertEqual(self.store.load('t')['progress']['no_progress'], 2)

    def test_artifact_without_observation_never_earns_credit(self):
        self.reviewer()
        (self.work / 'image.txt').write_text('a new screenshot')
        self.e.executor = Scripted(lambda *a: report(artifacts=['image.txt']))
        self.e._step('t')
        self.assertEqual(self.store.load('t')['progress']['no_progress'], 1)
        self.assertEqual(self.e.reviewer.calls, [])

    def test_rejected_or_unavailable_review_cannot_satisfy_progress(self):
        for verdict in ('unsupported', 'unclear', None, 'exhausted'):
            with self.subTest(verdict=verdict):
                t = self.store.load('t'); t['progress']['no_progress'] = 0
                self.store.save(t)
                self.reviewer('supported' if verdict == 'exhausted' else verdict)
                if verdict is None:
                    self.e.reviewer = None
                if verdict == 'exhausted':
                    self.e.policy['max_reviews'] = 0
                self.finding(content=str(verdict)); self.e._step('t')
                self.assertEqual(self.store.load('t')['progress']['no_progress'], 1)

    def test_missing_or_outside_evidence_never_earns_credit(self):
        self.reviewer()
        self.e.executor = Scripted(lambda *a: report(facts=[{
            'statement': 'diagnosed', 'basis': 'observed', 'evidence': ['missing.log', '../outside']}]))
        self.e._step('t')
        self.assertEqual(self.store.load('t')['progress']['no_progress'], 1)
        self.assertEqual(self.e.reviewer.calls, [])

    def test_failed_completion_claim_can_have_progress_but_not_pass(self):
        self.reviewer(); self.finding(status='claims_done'); self.e._step('t')
        t = self.store.load('t')
        self.assertEqual(t['progress']['no_progress'], 0)
        self.assertNotEqual(t['plan']['phases'][0]['status'], 'done')

    def test_background_mismatch_automatically_reschedules_same_phase(self):
        self.e.config['probes'] = ['windows_desktop']
        t = self.store.load('t'); before = deepcopy(t)
        self.e.executor = Scripted(lambda *a: report('blocked', blocker={
            'code': 'resource_mismatch', 'detail': 'need live GUI retest', 'evidence': []}))
        with self.e.resources.lease('t', 'background', self.work):
            self.e._step('t')
            self.assertEqual(self.e.resources.status('t')['resource'], 'background')
        t = self.store.load('t')
        self.assertEqual(resource_for(t, True), 'desktop')
        self.assertEqual(t['state'], 'running')
        self.assertEqual(t['spec'], before['spec'])
        self.assertEqual(t['checks'], before['checks'])
        self.assertEqual(t['used']['replans'], 0)
        self.assertEqual(t['plan']['phases'][0]['id'], before['plan']['phases'][0]['id'])
        # No second executor call is permitted until the desktop lease is held.
        self.e._step('t')
        self.assertEqual(len(self.e.executor.calls), 1)
        self.assertEqual(self.store.load('t')['block_kind'], 'resource_mismatch')

    def test_desktop_loop_reuses_phase_and_context_for_edit_build_retest(self):
        t = self.store.load('t'); t['plan']['phases'][0].update(resource='desktop', requires_desktop=True)
        self.store.save(t)
        def execute(kind, prompt, call):
            self.assertIn('edit source, compile, process files', prompt)
            self.assertEqual(self.e.resources.status('t')['resource'], 'desktop')
            if call['n'] == 0:
                (self.work / 'source.txt').write_text('DPI fix')
                return report()
            (self.work / 'tested.txt').write_text('rebuilt and retested')
            return report('claims_done')
        self.e.executor = Scripted(execute)
        with self.e.resources.lease('t', 'desktop', self.work):
            self.e._step('t'); self.e._step('t')
        self.assertEqual(self.store.load('t')['state'], 'verifying')
        self.assertEqual(self.e.executor.calls[1]['session'], 's1')
        self.assertEqual(self.store.load('t').get('plan_revision', 0), 0)

    def test_explicit_background_contract_cannot_be_promoted(self):
        self.e.config['probes'] = ['windows_desktop']
        t = self.block('resource_mismatch'); t['spec']['resource'] = 'background'; self.store.save(t)
        self.e.intake_message('t', 'continue', '继续')
        self.assertEqual(self.store.load('t')['state'], 'blocked')
        self.assertEqual(self.store.load('t')['spec']['resource'], 'background')

    def test_plain_continue_recovers_stall_with_exhausted_replans(self):
        before = self.block('stalled')
        self.e.intake_message('t', 'continue', '按这个方向修复')
        t = self.store.load('t')
        self.assertEqual(t['state'], 'running')
        self.assertEqual(t['progress']['no_progress'], 0)
        for field in ('used', 'spec', 'plan', 'checks'):
            self.assertEqual(t[field], before[field])
        self.assertIn('按这个方向修复', t['inbox']['hints'])
        self.e.intake_message('t', 'continue', '按这个方向修复')
        self.assertEqual(len(self.store.read_log('t', 'orchestration_resumed')), 1)
        for _ in range(3):
            self.e._step('t')
        self.assertEqual(self.store.load('t')['state'], 'blocked')
        self.assertEqual(self.store.load('t')['used']['steps'], 11)

    def test_resume_does_not_bypass_total_step_budget(self):
        t = self.block('stalled'); t['spec']['budget']['max_steps'] = t['used']['steps']; self.store.save(t)
        self.e.intake_message('t', 'continue', '继续')
        self.assertFalse(self.e._budget_ok('t'))
        self.assertEqual(self.store.load('t')['stop_reason'], 'budget_steps')
        self.assertEqual(self.e.executor.calls, [])

    def test_planning_retry_is_bounded_without_erasing_history(self):
        self.e.executor = Scripted(lambda *a: plan([{'checks': ['unknown']}]))
        self.e._plan('t')
        before = self.store.load('t')
        self.assertEqual(before['block_kind'], 'planning_error')
        self.e.intake_message('t', 'continue', '修复检查 ID 后继续')
        self.e._plan('t')
        t = self.store.load('t')
        self.assertEqual(t['block_kind'], 'planning_error')
        self.assertEqual(t['used']['steps'], before['used']['steps'] + 2)
        self.assertEqual(list(t['progress']['plan_errors'].values()), [4])

    def test_initial_planning_failure_accepts_plain_guidance(self):
        t = self.block('planning_error'); t['plan'] = None; self.store.save(t)
        self.e.intake_message('t', 'continue', '使用已有检查')
        self.assertEqual(self.store.load('t')['state'], 'planning')
        self.e.executor = Scripted(lambda *a: plan([{}]))
        self.e._plan('t')
        self.assertEqual(self.store.load('t')['state'], 'running')

    def test_checker_retry_rechecks_before_spending_an_execution_step(self):
        class Broken(Checker):
            name = 'broken'
            def check(self, *args, **kwargs):
                raise RuntimeError('checker still broken')
        self.e.checkers['broken'] = Broken()
        t = self.block('checker_error')
        t['checks'].append({'id': 'broken', 'type': 'broken', 'params': {}, 'origin': 'drafted', 'scope': 'phase'})
        t['plan']['phases'][0]['checks'].append('broken'); self.store.save(t)
        before = deepcopy(t['used'])
        self.e.intake_message('t', 'continue', '{"taskd":"answer","text":"continue"}')
        self.assertEqual(self.store.load('t')['state'], 'running')
        self.e._step('t')
        self.assertEqual(self.store.load('t')['block_kind'], 'checker_error')
        self.assertEqual(self.store.load('t')['used'], before)
        self.assertEqual(self.e.executor.calls, [])

    def test_repaired_checker_resumes_original_work(self):
        self.block('checker_error')
        self.e.intake_message('t', 'continue', '检查器已修复，继续')
        self.e._step('t')
        self.assertEqual(len(self.e.executor.calls), 1)
        self.assertEqual(self.store.load('t')['state'], 'running')

    def test_existing_desktop_mismatch_can_resume_without_plan_revision(self):
        self.e.config['probes'] = ['windows_desktop']
        t = self.block('resource_mismatch')
        t['plan']['phases'][0].update(resource='desktop', requires_desktop=True); self.store.save(t)
        self.e.intake_message('t', 'continue', '继续')
        self.assertEqual(self.store.load('t')['state'], 'running')
        self.assertEqual(self.store.load('t').get('plan_revision', 0), 0)
        with self.e.resources.lease('t', 'desktop', self.work):
            self.e._step('t')
        self.assertEqual(len(self.e.executor.calls), 1)

    def test_plan_approval_remains_required_after_recovery(self):
        t = self.block('planning_error'); t['spec']['plan_approval'] = 'always'; self.store.save(t)
        self.e.intake_message('t', 'continue', '修好规划后继续')
        self.e.executor = Scripted(lambda *a: plan([{'id': 'p1', 'produces': ['tested.txt']}]))
        self.e._plan('t')
        self.assertEqual(self.store.load('t')['state'], 'awaiting_approval')

    def test_canceled_tasks_are_not_revived_by_continue(self):
        self.block('stalled')
        self.e.intake_message('t', 'cancel', '{"taskd":"cancel"}')
        self.e.intake_message('t', 'continue', '继续')
        self.assertEqual(self.store.load('t')['state'], 'stopped')
        self.assertEqual(self.store.load('t')['stop_reason'], 'canceled')

    def test_scheduler_reacquires_desktop_after_auto_promotion(self):
        self.e.config['probes'] = ['windows_desktop']
        resources = []
        def execute(kind, prompt, call):
            resources.append(self.e.resources.status('t')['resource'])
            if len(resources) == 1:
                return report('blocked', blocker={'code': 'resource_mismatch', 'detail': 'GUI required', 'evidence': []})
            (self.work / 'tested.txt').write_text('native regression evidence')
            (self.work / 'package.txt').write_text('built package')
            return report('claims_done')
        self.e.executor = Scripted(execute)
        def stop_and_join():
            self.e.shutdown()
            with self.e.guard:
                workers = list(self.e.workers.values())
            for worker in workers:
                worker.join(10)
                self.assertFalse(worker.is_alive(), 'worker must stop before temporary files are removed')
        self.e.stopping.clear(); self.addCleanup(stop_and_join)
        self.e.schedule()
        self.assertTrue(wait_for(lambda: self.store.load('t')['state'] == 'completed'))
        self.assertTrue(wait_for(lambda: not self.e.workers))
        self.assertEqual(resources, ['background', 'desktop'])

    def test_resource_prompts_allow_mixed_loop_but_keep_background_restriction(self):
        self.assertIn('does not restrict non-GUI work', prompts.resource_rules('desktop'))
        self.assertIn('Do not use mouse', prompts.resource_rules('background'))


if __name__ == '__main__':
    unittest.main()

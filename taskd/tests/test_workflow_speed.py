from copy import deepcopy
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from helpers import Scripted, make_engine, plan, report, wait_for
from taskd import milestones
from taskd.checkers.windows_uia import UIAPropertyChecker, UIAValuesChecker
from taskd.checkers import windows_uia
from taskd.activity import snapshot
from taskd.recovery import prepare_revision
from taskd.protocol import normalize_control


class WorkflowSpeedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.engine, self.out, self.store, self.work = make_engine(tmp.name, Scripted(lambda *a: report()),
            policy={'max_replans': 0})
        self.engine.stopping.set()
        self.engine.intake_new('t1', 'peer', 'm1', json.dumps({'taskd': 'submit', 'goal': 'test',
            'acceptance': [{'id': 'result', 'type': 'files', 'pattern': 'result.txt'}]}))

    def workflow(self):
        drafted = [{'id': name, 'type': 'files', 'params_json': json.dumps({'pattern': name + '.txt'})}
                   for name in ('backup', 'loaded', 'ran')]
        return plan([{'id': 'p1', 'checks': ['result'], 'milestones': [
            {'id': name, 'title': name, 'checks': [name]} for name in ('backup', 'loaded', 'ran')]}], drafted=drafted)

    def install(self, obj=None):
        t = self.store.load('t1')
        p = self.engine._validate_plan(t, obj or self.workflow())
        t['checks'].extend(p.pop('new_checks'))
        t['plan'] = p
        t['state'] = 'running'
        self.store.save(t)
        return t

    def test_real_file_progress_reuses_context_without_replanning(self):
        self.install()
        actions = iter(('backup', 'loaded', 'ran', 'result'))
        def execute(kind, prompt, call):
            name = next(actions)
            (self.work / (name + '.txt')).write_text(name)
            return report('claims_done' if name == 'result' else 'working', artifacts=[name + '.txt'])
        self.engine.executor = Scripted(execute)
        for _ in range(3):
            self.engine._step('t1')
            self.assertEqual(self.store.load('t1')['progress']['no_progress'], 0)
        t = self.store.load('t1')
        self.assertEqual(milestones.view(t, t['plan']['phases'][0])['done'], 3)
        self.assertEqual(t['used']['replans'], 0)
        calls = self.engine.executor.calls
        self.assertIsNone(calls[0]['session'])
        self.assertEqual([c['session'] for c in calls[1:]], ['s1', 's1'])
        self.assertIn('"current": "loaded"', calls[1]['prompt'])
        self.assertEqual([list(x['results']) for x in self.store.read_log('t1', 'checks')],
                         [['backup'], ['loaded'], ['ran']])
        self.engine._step('t1')
        t = self.store.load('t1')
        self.assertEqual(t['state'], 'verifying')
        self.assertEqual(set(self.store.read_log('t1', 'checks')[-1]['results']), {'backup', 'loaded', 'ran'})
        self.engine._verify('t1')
        self.assertEqual(self.store.load('t1')['state'], 'completed')
        self.assertEqual(self.store.read_log('t1', 'checks')[-1]['results']['result']['outcome'], 'pass')
        self.assertEqual(self.engine.report(t)['timing']['execution']['calls'], 4)

    def test_new_file_or_claim_alone_is_not_milestone_progress(self):
        self.install()
        (self.work / 'unrelated.txt').write_text('not proof')
        self.engine._step('t1')
        t = self.store.load('t1')
        self.assertEqual(t['progress']['no_progress'], 1)
        self.assertEqual(milestones.view(t, t['plan']['phases'][0])['done'], 0)

    def test_regressed_evidence_fails_final_recheck(self):
        t = self.install()
        for name in ('backup', 'loaded', 'ran', 'result'):
            (self.work / (name + '.txt')).write_text('proof')
        self.engine._phase_claim('t1', t['plan']['phases'][0], report('claims_done'), [], [])
        t = self.store.load('t1')
        t['state'] = 'running'; t['phase_index'] = 0; t['plan']['phases'][0]['status'] = 'pending'
        self.store.save(t)
        (self.work / 'backup.txt').unlink()
        self.engine._phase_claim('t1', t['plan']['phases'][0], report('claims_done'), [], [])
        self.assertNotEqual(self.store.load('t1')['plan']['phases'][0]['status'], 'done')

    def test_no_duplicate_credit_and_changed_definition_needs_new_proof(self):
        t = self.install(); phase = t['plan']['phases'][0]
        passed = {'backup': {'outcome': 'pass'}}
        self.assertEqual(milestones.advance(t, phase, passed, 'now'), ['backup'])
        phase['milestones'][0]['id'] = 'renamed'
        self.assertEqual(milestones.advance(t, phase, passed, 'later'), [])
        next(c for c in t['checks'] if c['id'] == 'backup')['params']['pattern'] = 'different.txt'
        self.assertEqual(milestones.pending(t, phase)[0]['id'], 'renamed')
        self.assertEqual(milestones.advance(t, phase, {}, 'later'), [])

    def test_partial_multi_check_milestone_is_not_progress(self):
        obj = self.workflow()
        obj['phases'][0]['milestones'] = [{'id': 'both', 'title': 'both', 'checks': ['backup', 'loaded']}]
        self.install(obj)
        (self.work / 'backup.txt').write_text('partial')
        self.engine._step('t1')
        self.assertEqual(self.store.load('t1')['progress']['no_progress'], 1)

    def test_replan_clearing_check_state_cannot_recredit_completed_milestones(self):
        t = self.install()
        for name in ('backup', 'loaded', 'ran'):
            (self.work / (name + '.txt')).write_text(name)
        phase = t['plan']['phases'][0]
        milestones.advance(t, phase, {cid: {'outcome': 'pass'} for cid in ('backup', 'loaded', 'ran')}, 'now')
        t['progress']['check_state'] = {}; self.store.save(t)
        self.engine._step('t1')
        self.assertEqual(self.store.load('t1')['progress']['no_progress'], 1)
        self.assertEqual(self.store.read_log('t1', 'checks'), [])

    def test_milestone_validation(self):
        for changes in ({'checks': []}, {'checks': ['unknown']}, {'id': 'Bad ID'}):
            obj = self.workflow(); obj['phases'][0]['milestones'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.engine._validate_plan(self.store.load('t1'), obj)
        obj = self.workflow(); obj['phases'][0]['milestones'][1]['checks'] = ['backup']
        with self.assertRaisesRegex(ValueError, 'reused'):
            self.engine._validate_plan(self.store.load('t1'), obj)
        obj = self.workflow(); obj['phases'][0]['milestones'][0]['checks'] = ['result']
        with self.assertRaisesRegex(ValueError, 'phase-local'):
            self.engine._validate_plan(self.store.load('t1'), obj)

    def test_verified_milestones_omit_periodic_advisory_review(self):
        t = self.install()
        self.engine.reviewer = Scripted(lambda *a: self.fail('unnecessary advisory review'))
        t['used']['steps'] = 5
        self.store.save(t)
        (self.work / 'backup.txt').write_text('verified proof')
        self.store.log('t1', 'step', new=['backup.txt'], report=report())
        self.engine._maybe_relevance_review('t1', t['plan']['phases'][0], report())
        self.assertEqual(self.engine.reviewer.calls, [])

    def test_legacy_plan_and_revision_idempotence(self):
        obj = plan([{'id': 'p1', 'checks': ['result']}]); obj['phases'][0].pop('milestones')
        t = self.install(obj)
        self.assertEqual(t['plan']['phases'][0]['milestones'], [])
        request = normalize_control({'taskd': 'revise_plan', 'request_id': 'r1', 'base_revision': 0,
            'from_phase': 'p1', 'reason': 'legacy', 'plan': obj})
        old = deepcopy(request); old['plan']['phases'][0].pop('milestones', None)
        one = prepare_revision(t, old, self.engine._validate_plan, 3, self.engine.checkers)
        t['revision_requests'] = {'r1': {'sha256': one['sha256']}}
        self.assertIsNone(prepare_revision(t, request, self.engine._validate_plan, 3, self.engine.checkers))

    def test_repair_calls_are_separate_from_execution(self):
        self.install(plan([{'checks': ['result']}]))
        answers = iter(('invalid report', report()))
        self.engine.executor = Scripted(lambda *args: next(answers))
        self.engine._step('t1')
        timing = self.store.load('t1')['timing']
        self.assertEqual(timing['execution']['calls'], 1)
        self.assertEqual(timing['report_repair']['calls'], 1)
        self.assertNotIn('planning', timing)

    def uia_checks(self):
        self.engine.checkers.update(uia_property=UIAPropertyChecker(), uia_values=UIAValuesChecker())
        return [{'id': 'a', 'type': 'uia_property', 'params': {'window': 'EPS*', 'element': 'Run', 'property': 'IsEnabled', 'expect': True}},
                {'id': 'b', 'type': 'uia_property', 'params': {'window': 'EPS*', 'element': 'Run', 'property': 'IsEnabled', 'expect': False}},
                {'id': 'c', 'type': 'uia_values', 'params': {'window': 'EPS*', 'property': 'Value', 'expect': {'X': 1, 'Y': 2}}}]

    def test_mixed_uia_batch_deduplicates_and_reads_fresh(self):
        checks = self.uia_checks(); enabled = [True]
        def read(script, p):
            return {'found_window': True, 'window': 'EPS', 'items': [dict(q, found=True,
                value=enabled[0] if q['element'] == 'Run' else 1 if q['element'] == 'X' else 2) for q in p['queries']]}
        with patch.object(windows_uia.powershell, 'run_json', side_effect=read) as call:
            first = self.engine.run_checks('t1', checks)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(len(call.call_args.args[1]['queries']), 3)
            self.assertEqual([first[c['id']]['outcome'] for c in checks], ['pass', 'fail', 'pass'])
            enabled[0] = False
            second = self.engine.run_checks('t1', checks)
            self.assertEqual(call.call_count, 2)
            self.assertEqual(second['a']['outcome'], 'fail')
            self.assertEqual(second['b']['outcome'], 'pass')

    def test_distinct_windows_or_processes_never_share_batch(self):
        checks = self.uia_checks()[:2]
        checks[1]['params']['process_id'] = 123
        with patch.object(windows_uia.powershell, 'run_json', return_value={'found_window': False}) as call:
            self.engine.run_checks('t1', checks)
            self.assertEqual(call.call_count, 2)
            self.assertEqual(call.call_args.args[1]['process_id'], 123)

    def test_bad_expected_file_does_not_poison_other_checks(self):
        checks = self.uia_checks()
        checks[-1]['params'].pop('expect'); checks[-1]['params']['expect_file'] = '../secret.json'
        with patch.object(windows_uia.powershell, 'run_json', return_value={'found_window': False}) as call:
            results = self.engine.run_checks('t1', checks)
        self.assertEqual(call.call_count, 1)
        self.assertIn('..', results['c']['note'])

    def test_batch_failure_is_not_retried_per_check(self):
        with patch.object(windows_uia.powershell, 'run_json', side_effect=RuntimeError('gone')) as call:
            results = self.engine.run_checks('t1', self.uia_checks())
        self.assertEqual(call.call_count, 1)
        self.assertTrue(all(r['outcome'] == 'cannot_check' for r in results.values()))

    def test_empty_checks_emit_no_misleading_activity(self):
        before = self.store.activity.load('t1')
        self.assertEqual(self.engine.run_checks('t1', []), {})
        self.assertEqual(self.store.activity.load('t1'), before)

    def test_status_includes_milestone_and_content_free_purpose(self):
        self.install()
        self.store.activity.record('t1', 'executor_started', timeout=5, purpose='execution')
        task = snapshot(self.engine, None)['tasks'][0]
        self.assertEqual(task['phase']['milestone']['current'], 'backup')
        self.assertEqual(task['last_purpose'], 'execution')
        self.assertNotIn('prompt', str(task))


if __name__ == '__main__':
    unittest.main()

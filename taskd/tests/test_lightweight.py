"""Real scripts and deterministic checks; model doubles measure orchestration calls."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from helpers import Scripted, make_engine, plan, report, wait_for
from taskd import builtin, lightweight
from taskd.executors.base import ExecResult
from taskd.protocol import parse_status
from taskd.quick import QuickResponder
from taskd.service import ProviderLoop
from test_quick import FakeTransport


CHECK = {'id': 'result', 'type': 'value', 'file': 'result.json', 'path': 'ok', 'op': '==', 'expect': True}


def short(status='claims_done', detail=''):
    return {'status': status, 'summary': 'tested', 'artifacts': ['result.json'], 'detail': detail}


def request(**kw):
    return json.dumps(dict({'taskd': 'run', 'goal': 'install and test', 'acceptance': [CHECK]}, **kw))


class LightweightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / 'work'

    def engine(self, fn=None, **config):
        def success(*args):
            (self.work / 'result.json').write_text('{"ok":true}')
            return short()
        ex = Scripted(fn or success)
        e, out, store, work = make_engine(self.tmp.name, ex, **config)
        def cleanup():
            e.shutdown()
            for thread in list(e.workers.values()):
                thread.join(5)
        self.addCleanup(cleanup)
        return e, ex, out, store

    def settle(self, e, store, state='completed'):
        self.assertTrue(wait_for(lambda: store.load('t')['state'] == state), store.load('t'))
        self.assertTrue(wait_for(lambda: not e.workers))

    def register(self, source='from pathlib import Path\nPath("result.json").write_text(\'{"ok":true}\')', **kw):
        path = Path(self.tmp.name) / 'routine.py'
        path.write_text(source)
        return {'deploy': dict({'argv': [sys.executable, str(path)], 'peers': ['peer'],
                                'resource': 'background', 'acceptance': [CHECK]}, **kw)}

    def test_direct_one_call_no_planner_reviewer_or_extra_plan_message(self):
        e, ex, out, store = self.engine()
        e.intake_new('t', 'peer', 'm', request(budget={'max_steps': 1}))
        self.settle(e, store)
        self.assertEqual(ex.kinds(), ['step'])
        self.assertEqual(out.states(), ['working', 'completed'])
        self.assertEqual(store.load('t')['used']['reviews'], 0)
        self.assertEqual(store.load('t')['used']['verify_rounds'], 1)
        self.assertIn('<<<GOAL', ex.calls[0]['prompt'])
        rep = json.loads((store.task_dir('t') / 'report.json').read_text())
        self.assertEqual(rep['orchestration'], 'direct')
        self.assertEqual(rep['checks'][0]['outcome'], 'pass')
        self.assertEqual(rep['evidence'][0]['sha256'], hashlib.sha256(b'{"ok":true}').hexdigest())

    def test_direct_failed_acceptance_blocks_without_replanning(self):
        e, ex, out, store = self.engine(lambda *a: short())
        e.intake_new('t', 'peer', 'm', request())
        self.settle(e, store, 'blocked')
        self.assertEqual(ex.kinds(), ['step'])
        self.assertEqual(store.load('t')['block_kind'], 'verification')
        self.assertEqual(store.load('t')['used']['replans'], 0)
        self.assertIn('verification-failures.json', out.sent[-1]['files'])

    def test_direct_invalid_report_never_repairs_report_with_another_call(self):
        e, ex, out, store = self.engine(lambda *a: 'not json')
        e.intake_new('t', 'peer', 'm', request())
        self.settle(e, store, 'blocked')
        self.assertEqual(ex.kinds(), ['step'])

    def test_direct_executor_failure_never_retries_automatically(self):
        e, ex, out, store = self.engine(lambda *a: ExecResult(False, '', error='failed'))
        e.intake_new('t', 'peer', 'm', request())
        self.settle(e, store, 'blocked')
        self.assertEqual(ex.kinds(), ['step'])

    def test_guidance_allows_one_more_call_without_resetting_cumulative_budget(self):
        def execute(kind, prompt, call):
            if call['n'] == 0:
                return short('blocked', 'missing config')
            self.assertIn('use the new config', prompt)
            (self.work / 'result.json').write_text('{"ok":true}')
            return short()
        e, ex, out, store = self.engine(execute)
        e.intake_new('t', 'peer', 'm', request())
        self.settle(e, store, 'blocked')
        e.intake_message('t', 'm2', 'use the new config')
        self.settle(e, store)
        self.assertEqual(ex.kinds(), ['step', 'step'])
        self.assertEqual(store.load('t')['used']['steps'], 2)

    def test_approval_prevents_execution_until_approved_without_a_planning_call(self):
        e, ex, out, store = self.engine()
        e.intake_new('t', 'peer', 'm', request(plan_approval='always'))
        self.settle(e, store, 'awaiting_approval')
        self.assertEqual(ex.calls, [])
        e.intake_message('t', 'm2', '{"taskd":"approve_plan"}')
        self.settle(e, store)
        self.assertEqual(ex.kinds(), ['step'])

    def test_needs_user_continues_only_on_answer(self):
        def execute(kind, prompt, call):
            if call['n'] == 0:
                return short('needs_user', 'Which version?')
            (self.work / 'result.json').write_text('{"ok":true}')
            return short()
        e, ex, out, store = self.engine(execute)
        e.intake_new('t', 'peer', 'm', request())
        self.settle(e, store, 'needs_user')
        e.intake_message('t', 'm2', 'version two')
        self.settle(e, store)
        self.assertEqual(ex.kinds(), ['step', 'step'])

    def test_natural_or_missing_acceptance_rejected(self):
        e, ex, out, store = self.engine()
        for i, checks in enumerate(([], ['works'], [{'id': 'r', 'type': 'llm_review', 'question': 'works?'}])):
            self.assertIsNone(e.intake_new('t' + str(i), 'peer', 'm', request(acceptance=checks)))
        self.assertEqual(ex.calls, [])

    def test_routine_zero_model_calls_real_script_and_checks(self):
        e, ex, out, store = self.engine(routines=self.register())
        e.intake_new('t', 'peer', 'm', request(routine='deploy', acceptance=[], budget={'max_steps': 1}))
        self.settle(e, store)
        self.assertEqual(ex.calls, [])
        self.assertEqual(store.load('t')['routine_state'], 'passed')
        self.assertEqual(store.load('t')['used']['steps'], 1)
        self.assertEqual(json.loads((self.work / 'routine-output.json').read_text())['exit_code'], 0)
        self.assertTrue(any(name.endswith('-result.json') for name in out.sent[-1]['files']))

    def test_routine_nonzero_with_passing_files_is_not_success(self):
        source = 'from pathlib import Path\nPath("result.json").write_text(\'{"ok":true}\')\nraise SystemExit(4)'
        e, ex, out, store = self.engine(routines=self.register(source))
        e.intake_new('t', 'peer', 'm', request(routine='deploy', acceptance=[]))
        self.settle(e, store, 'blocked')
        self.assertEqual(ex.calls, [])
        self.assertEqual(store.load('t')['block_kind'], 'routine_failed')

    def test_routine_failure_gets_exactly_one_opted_in_repair(self):
        e, ex, out, store = self.engine(routines=self.register('raise SystemExit(4)', repair=True))
        e.intake_new('t', 'peer', 'm', request(routine='deploy', acceptance=[]))
        self.settle(e, store)
        self.assertEqual(ex.kinds(), ['step'])
        self.assertEqual(store.load('t')['used']['steps'], 2)
        self.assertIn('never blindly repeat', ex.calls[0]['prompt'])

    def test_routine_success_bad_acceptance_also_gets_one_repair(self):
        e, ex, out, store = self.engine(routines=self.register('print("ran")', repair=True))
        e.intake_new('t', 'peer', 'm', request(routine='deploy', acceptance=[]))
        self.settle(e, store)
        self.assertEqual(ex.kinds(), ['step'])
        self.assertEqual(store.load('t')['used']['verify_rounds'], 2)

    def test_failed_repair_stops_after_one_call(self):
        e, ex, out, store = self.engine(lambda *a: short(), routines=self.register('raise SystemExit(4)', repair=True))
        e.intake_new('t', 'peer', 'm', request(routine='deploy', acceptance=[]))
        self.settle(e, store, 'blocked')
        self.assertEqual(ex.kinds(), ['step'])
        self.assertEqual(store.load('t')['used']['steps'], 2)

    def test_routine_parameters_only_registered_values_pass_on_stdin(self):
        source = 'import sys,json\nfrom pathlib import Path\np=json.load(sys.stdin)["params"]\nPath("result.json").write_text(json.dumps({"ok":p["version"]=="stable"}))'
        e, ex, out, store = self.engine(routines=self.register(source, parameters={'version': ['stable', 'beta']}))
        for n, params in enumerate(({}, {'version': '$(touch x)'}, {'version': 'stable', 'argv': ['bad']})):
            self.assertIsNone(e.intake_new('bad' + str(n), 'peer', 'm', request(routine='deploy', params=params, acceptance=[])))
        e.intake_new('t', 'peer', 'm', request(routine='deploy', params={'version': 'stable'}, acceptance=[]))
        self.settle(e, store)
        self.assertEqual(ex.calls, [])

    def test_unregistered_or_wrong_peer_rejected(self):
        e, ex, out, store = self.engine(routines=self.register())
        self.assertIsNone(e.intake_new('x', 'stranger', 'm', request(routine='deploy', acceptance=[])))
        self.assertIsNone(e.intake_new('y', 'peer', 'm', request(routine='unknown', acceptance=[])))
        self.assertEqual(ex.calls, [])

    def test_routine_cannot_ignore_new_requester_constraints(self):
        e, ex, out, store = self.engine(routines=self.register())
        self.assertIsNone(e.intake_new('t', 'peer', 'm', request(
            routine='deploy', acceptance=[], constraints=['Do not change anything; only inspect'])))
        self.assertEqual(ex.calls, [])
        self.assertFalse((self.work / 'result.json').exists())

    def test_requester_cannot_replace_registered_checks(self):
        e, ex, out, store = self.engine(routines=self.register())
        self.assertIsNone(e.intake_new('t', 'peer', 'm', request(routine='deploy')))
        self.assertEqual(ex.calls, [])

    def test_routine_config_change_after_acceptance_blocks_execution(self):
        e, ex, out, store = self.engine(routines=self.register())
        e.intake_new('t', 'peer', 'm', request(routine='deploy', acceptance=[], plan_approval='always'))
        self.settle(e, store, 'awaiting_approval')
        e.config['routines']['deploy']['argv'].append('changed')
        e.intake_message('t', 'm2', '{"taskd":"approve_plan"}')
        self.settle(e, store, 'blocked')
        self.assertEqual(store.load('t')['block_kind'], 'routine_changed')
        self.assertFalse((self.work / 'result.json').exists())

    def test_budget_prevents_exception_model_call(self):
        e, ex, out, store = self.engine(routines=self.register('raise SystemExit(4)', repair=True))
        e.intake_new('t', 'peer', 'm', request(routine='deploy', acceptance=[], budget={'max_steps': 1}))
        self.settle(e, store, 'stopped')
        self.assertEqual(store.load('t')['stop_reason'], 'budget_steps')
        self.assertEqual(ex.calls, [])

    def test_timeout_never_repeats_script_or_calls_exception_model(self):
        e, ex, out, store = self.engine(routines=self.register('import time\ntime.sleep(10)', repair=True))
        e.intake_new('t', 'peer', 'm', request(routine='deploy', acceptance=[], budget={'step_timeout_min': 0.001}))
        self.settle(e, store, 'blocked')
        self.assertEqual(store.load('t')['block_kind'], 'routine_interrupted')
        self.assertEqual(ex.calls, [])

    def test_capabilities_lists_only_peer_authorized_routines_without_command_paths(self):
        e, ex, out, store = self.engine(routines=self.register())
        e.capabilities = builtin.capabilities(e.config, ex, e.checkers, None)
        transport = FakeTransport()
        loop = ProviderLoop(e, transport, store, self.work / 'inputs')
        for tid, peer in (('q', 'peer'), ('other', 'stranger')):
            transport.add(tid, '{"taskd":"capabilities"}', peer=peer)
            transport.list_inbound = lambda: [transport.summary(tid)]
            loop.tick()
        caps = parse_status(transport.replies[0][1])['capabilities']
        self.assertEqual(caps['lightweight']['action'], 'run')
        self.assertIn('deploy', caps['routines'])
        self.assertNotIn('argv', transport.replies[0][1])
        self.assertEqual(parse_status(transport.replies[1][1])['capabilities']['routines'], {})
        self.assertEqual(ex.calls, [])

    def test_recover_unknown_script_outcome_never_repeats_it(self):
        e, ex, out, store = self.engine(routines=self.register())
        with patch.object(e, 'schedule'):
            e.intake_new('t', 'peer', 'm', request(routine='deploy', acceptance=[]))
        task = store.load('t'); lightweight.prepare(e, task)
        task = store.load('t')
        task.update(routine_state='started', step_in_progress={'phase': 'run'})
        store.save(task)
        e.recover()
        self.settle(e, store, 'blocked')
        e.intake_message('t', 'm2', 'continue')
        self.settle(e, store, 'blocked')
        self.assertFalse((self.work / 'result.json').exists())
        self.assertEqual(ex.calls, [])

    def test_recover_interrupted_direct_requires_explicit_guidance(self):
        e, ex, out, store = self.engine()
        with patch.object(e, 'schedule'):
            e.intake_new('t', 'peer', 'm', request())
        task = store.load('t'); lightweight.prepare(e, task)
        task = store.load('t'); task.update(light_model_calls=1, step_in_progress={'phase': 'run'})
        store.save(task); e.recover()
        self.settle(e, store, 'blocked')
        self.assertEqual(ex.calls, [])
        e.intake_message('t', 'm2', 'inspect current state and continue')
        self.settle(e, store)
        self.assertEqual(ex.kinds(), ['step'])

    def test_unknown_routine_can_be_inspected_after_explicit_guidance_without_rerunning(self):
        e, ex, out, store = self.engine(routines=self.register('raise RuntimeError("must not rerun")', repair=True))
        with patch.object(e, 'schedule'):
            e.intake_new('t', 'peer', 'm', request(routine='deploy', acceptance=[]))
        task = store.load('t'); lightweight.prepare(e, task)
        task = store.load('t'); task.update(routine_state='started', step_in_progress={'phase': 'run'})
        store.save(task); e.recover()
        self.settle(e, store, 'blocked')
        self.assertEqual(ex.calls, [])
        e.intake_message('t', 'm2', 'inspect side effects and complete the check')
        self.settle(e, store)
        self.assertEqual(ex.kinds(), ['step'])
        self.assertFalse((self.work / 'routine-output.json').exists())

    def test_desktop_execution_waits_for_existing_lease(self):
        e, ex, out, store = self.engine(probes=['windows_desktop'], isolate_tasks=True)
        gate = threading.Event()
        def execute(*args):
            self.assertEqual(e.resources.status('t')['resource'], 'desktop')
            gate.set()
            work = e.task_work_dir(store.load('t'))
            (work / 'result.json').write_text('{"ok":true}')
            return short()
        ex.fn = execute
        with e.resources.lease('owner', 'desktop', Path(self.tmp.name) / 'other'):
            e.intake_new('t', 'peer', 'm', request(resource='desktop'))
            self.assertTrue(wait_for(lambda: (e.resources.status('t') or {}).get('state') == 'waiting_desktop'))
            self.assertFalse(gate.is_set())
        self.settle(e, store)
        self.assertTrue(gate.is_set())

    def test_cancel_real_script_kills_it_and_never_invokes_repair(self):
        source = 'import time\nfrom pathlib import Path\nPath("started").write_text("yes")\ntime.sleep(10)\nPath("result.json").write_text(\'{"ok":true}\')'
        e, ex, out, store = self.engine(routines=self.register(source, repair=True))
        e.intake_new('t', 'peer', 'm', request(routine='deploy', acceptance=[]))
        self.assertTrue(wait_for(lambda: (self.work / 'started').exists()))
        e.intake_message('t', 'm2', '{"taskd":"cancel"}')
        self.settle(e, store, 'stopped')
        self.assertEqual(store.load('t')['stop_reason'], 'canceled')
        self.assertEqual(ex.calls, [])
        self.assertFalse((self.work / 'result.json').exists())

    def test_desktop_lease_covers_execution_and_final_acceptance_without_handoff(self):
        e, ex, out, store = self.engine(probes=['windows_desktop'])
        original = e.checkers['value'].check
        def check(*args, **kwargs):
            self.assertEqual(e.resources.status('t')['resource'], 'desktop')
            return original(*args, **kwargs)
        with patch.object(e.resources, 'lease', wraps=e.resources.lease) as leases, \
                patch.object(e.checkers['value'], 'check', side_effect=check):
            e.intake_new('t', 'peer', 'm', request(resource='desktop'))
            self.settle(e, store)
            self.assertEqual(leases.call_count, 1, 'execution and acceptance must share one uninterrupted lease')

    def test_logs_are_bounded_peer_scoped_zero_model_queries(self):
        e, ex, out, store = self.engine()
        e.intake_new('t', 'peer', 'm', request()); self.settle(e, store)
        store.log('t', 'private', prompt='do not expose full prompts')
        transport = FakeTransport()
        loop = ProviderLoop(e, transport, store, self.work / 'inputs')
        for tid, peer, limit, expected in (('q', 'peer', 2, 'completed'), ('other', 'stranger', 2, 'rejected'), ('bad', 'peer', 1000, 'rejected')):
            transport.add(tid, json.dumps({'taskd': 'logs', 'task_id': 't', 'limit': limit}), peer=peer)
            transport.list_inbound = lambda: [transport.summary(tid)]
            loop.tick()
            self.assertEqual(transport.replies[-1][2], expected)
        env = parse_status(transport.replies[0][1])
        self.assertEqual(len(env['events']), 2)
        self.assertNotIn('do not expose', transport.replies[0][1])
        self.assertEqual(ex.kinds(), ['step'])
        self.assertIsNone(store.load('q'))

    def test_run_cannot_be_routed_to_unchecked_quick_mode(self):
        e, ex, out, store = self.engine()
        quick = QuickResponder(ex, FakeTransport(), store, self.work, {}, self.work / 'inputs')
        self.assertFalse(quick.wants(request(mode='quick')))
        self.assertIsNone(e.intake_new('t', 'peer', 'm', request(mode='quick')))
        self.assertEqual(ex.calls, [])

    def test_legacy_submit_still_calls_planner_and_executor(self):
        def execute(kind, prompt, call):
            if kind == 'plan':
                return plan([{}])
            (self.work / 'result.json').write_text('{"ok":true}')
            return report('claims_done', artifacts=['result.json'])
        e, ex, out, store = self.engine(execute)
        e.intake_new('t', 'peer', 'm', request(taskd='submit', mode='long')); self.settle(e, store)
        self.assertEqual(ex.kinds(), ['plan', 'step'])


if __name__ == '__main__':
    unittest.main()

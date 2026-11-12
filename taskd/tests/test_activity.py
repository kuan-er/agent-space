import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from helpers import Scripted, make_engine, wait_for
from taskd.activity import execution, snapshot
from taskd.platform.proc import run
from taskd.quick import QuickResponder
from taskd.service import Daemon
from taskd.store import read_json
from test_quick import FakeTransport


class Worker:
    def is_alive(self):
        return True


class WorkStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine, self.out, self.store, self.work = make_engine(self.tmp.name, Scripted(lambda *a: 'ok'))
        self.engine.schedule = lambda: None

    def task(self, tid, state='accepted'):
        t = self.engine.intake_new(tid, 'peer', 'm1', 'read the provided files')
        t['state'] = state
        self.store.save(t)
        return t

    def test_native_execution_facts_are_content_free_and_not_acceptance(self):
        import time
        t = self.task('native', 'running')
        t['spec'].update(orchestration='native', routing_reason='native_capability_verified')
        t['native'] = {'thread_id': 'secret-session', 'goal_status': 'complete',
                       'turn_status': 'completed', 'iteration': 3, 'observed_at': time.time(),
                       'retry': {'reason': 'model_at_capacity', 'attempt': 2, 'turn_id': 'secret-turn'},
                       'stop': {'confirmed': False, 'scope': 'secret-path'}}
        t['native_quarantine'] = True
        self.store.save(t)
        facts = snapshot(self.engine, None)['tasks'][0]['execution_facts']
        self.assertEqual(facts['route'], 'native')
        self.assertEqual(facts['goal_status'], 'complete')
        self.assertEqual(facts['iteration'], 3)
        self.assertEqual(facts['stop'], {'requested': True, 'confirmed': False, 'resource_retained': True})
        self.assertNotIn('secret', str(facts))
        self.assertNotIn('last_progress_at', snapshot(self.engine, None)['tasks'][0])
        with execution(self.store.activity, 'native', 60, purpose='native_goal'):
            pass
        self.assertEqual(self.store.activity.load('native')['last_purpose'], 'native_goal')

    def test_queue_workers_waits_and_blocks_count_separately(self):
        self.task('q')
        t = self.task('run', 'running')
        t['plan'] = {'phases': [{'id':'p1', 'title':'check coverage'}]}
        self.store.save(t)
        self.task('person', 'needs_user')
        self.task('approval', 'awaiting_approval')
        self.task('block', 'blocked')
        self.task('old', 'completed')
        self.engine.workers['run'] = Worker()
        w = snapshot(self.engine, None)
        self.assertEqual(w['counts'], dict(running=1, queued=1, waiting=2, blocked=1))
        self.assertEqual(w['total'], 5)
        self.assertEqual(next(t for t in w['tasks'] if t['id']=='run')['phase']['title'], 'check coverage')

    def test_keepalive_output_and_new_files_are_not_acceptance_progress(self):
        t = self.task('t')
        initial = self.store.activity.load('t')
        self.engine.keepalive()
        self.assertEqual(initial, self.store.activity.load('t'))
        with execution(self.store.activity, 't', 60):
            result = run([sys.executable, '-c', 'import time; print("output", flush=True); time.sleep(.3)'])
        self.assertTrue(result.ok)
        a = self.store.activity.load('t')
        self.assertIn('executor_output', [e['event'] for e in a['events']])
        self.assertNotIn('last_progress_at', a)
        self.store.log('t', 'step', new=['a-new-file.txt'])
        self.assertNotIn('last_progress_at', self.store.activity.load('t'))
        self.store.log('t', 'checks_advanced')
        advanced = self.store.activity.load('t')['last_progress_at']
        self.store.log('t', 'checks', results={})
        self.assertEqual(advanced, self.store.activity.load('t')['last_progress_at'])

    def test_old_records_have_unknown_activity_and_timeout_uses_deadline(self):
        t = self.task('t', 'running')
        self.engine.workers['t'] = Worker()
        (self.store.activity.root/'t.json').unlink()
        self.assertNotIn('last_activity_at', snapshot(self.engine, None)['tasks'][0])
        with patch('taskd.activity.time.time', return_value=100):
            self.store.activity.record('t', 'executor_started', timeout=600)
        self.assertFalse(snapshot(self.engine, None, now=700)['tasks'][0]['attention'])
        self.assertTrue(snapshot(self.engine, None, now=731)['tasks'][0]['attention'])

    def test_quick_semaphore_waiters_are_queued_not_executing(self):
        gate = threading.Event()
        ex = Scripted(lambda *a: (gate.wait(4), 'ok')[1])
        transport = FakeTransport()
        q = QuickResponder(ex, transport, self.store, self.work, {'concurrency':1}, self.work/'inputs')
        self.addCleanup(gate.set)
        for tid in ('a','b'):
            transport.add(tid, 'hello')
            q.offer(transport.summary(tid), None)
        self.assertTrue(wait_for(lambda: len(q.executing)==1))
        w = snapshot(self.engine, q)
        self.assertEqual(w['counts'], dict(running=1, queued=1, waiting=0, blocked=0))
        gate.set()
        self.assertTrue(wait_for(lambda: not q.running))
        self.assertEqual(snapshot(self.engine, q)['state'], 'idle')

    def test_daemon_publishes_work_and_requester_only_does_not_claim_idle(self):
        daemon = Daemon(root=self.store.root, config=self.engine.config, transport=self.out, engine=self.engine)
        daemon.write_status()
        self.assertEqual(read_json(self.store.root/'status.json')['work']['state'], 'idle')
        daemon.engine = None
        daemon.write_status()
        self.assertNotIn('work', read_json(self.store.root/'status.json'))

    def test_observation_io_errors_do_not_break_execution(self):
        with patch('taskd.activity.atomic_json', side_effect=OSError('telemetry disk error')):
            with execution(self.store.activity, 't', 5):
                pass


if __name__ == '__main__':
    unittest.main()

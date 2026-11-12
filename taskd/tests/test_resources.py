"""Concurrency regressions use gates and real worker threads, never model calls."""
import json
from pathlib import Path
import tempfile
import threading
import unittest

from helpers import Scripted, make_engine, plan, report, wait_for
from taskd.activity import snapshot
from taskd.protocol import normalize_spec, Rejected
from taskd.quick import QuickResponder
from taskd.resources import Resources, capacity, resource_for
from taskd.service import ProviderLoop
from test_quick import FakeTransport


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_desktop_waiter_does_not_hold_a_slot_and_background_bypasses_it(self):
        pool = Resources(2)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        def waiter():
            with pool.lease('second', 'desktop', Path(self.tmp.name)/'b') as acquired:
                entered.set()
                release.wait(3)
            finished.set()
        with pool.lease('first', 'desktop', Path(self.tmp.name)/'a'):
            thread = threading.Thread(target=waiter); thread.start()
            self.assertTrue(wait_for(lambda: pool.status('second') is not None))
            self.assertEqual(pool.status('second')['state'], 'waiting_desktop')
            self.assertEqual(pool.status('second')['blocked_by'], ['first'])
            with pool.lease('background', 'background', Path(self.tmp.name)/'c') as acquired:
                self.assertTrue(acquired)
                self.assertFalse(entered.is_set())
        self.assertTrue(entered.wait(3)); release.set(); thread.join(3)
        self.assertTrue(finished.is_set())
        self.assertEqual(pool.owners, {})

    def test_cancel_waiter_and_exception_release_all_resources(self):
        pool = Resources(1); cancel = threading.Event(); result = []
        def waiter():
            with pool.lease('waiter', 'desktop', self.tmp.name, cancel) as acquired:
                result.append(acquired)
        with pool.lease('owner', 'desktop', self.tmp.name):
            thread = threading.Thread(target=waiter); thread.start()
            self.assertTrue(wait_for(lambda: pool.status('waiter') is not None))
            cancel.set(); thread.join(3)
        self.assertEqual(result, [False])
        with self.assertRaises(RuntimeError):
            with pool.lease('broken', 'desktop', self.tmp.name):
                raise RuntimeError('executor failed')
        self.assertEqual(pool.owners, {}); self.assertEqual(pool.waiters, {})

    def test_legacy_workspace_is_exclusive_even_for_background_tasks(self):
        pool = Resources(2); cancel = threading.Event()
        def waiter():
            with pool.lease('b', 'background', Path(self.tmp.name)/'isolated-task', cancel):
                pass
        with pool.lease('a', 'background', self.tmp.name):
            thread = threading.Thread(target=waiter); thread.start()
            self.assertTrue(wait_for(lambda: pool.status('b') is not None))
            self.assertEqual(pool.status('b')['state'], 'waiting_workspace')
            cancel.set(); thread.join(3)

    def test_migration_of_forced_gui_limit_and_explicit_single_slot(self):
        self.assertIsNone(capacity({'concurrency':1,'probes':['windows_desktop']}))
        self.assertIsNone(capacity({'concurrency':0,'scheduler_version':2}))
        self.assertEqual(capacity({'concurrency':1,'scheduler_version':2,'probes':['windows_desktop']}),1)
        self.assertEqual(capacity({'concurrency':1}),1)

    def engine(self, ex, **cfg):
        engine, out, store, work = make_engine(self.tmp.name, ex, concurrency=2, isolate_tasks=True, **cfg)
        def cleanup():
            engine.shutdown()
            for thread in list(engine.workers.values()): thread.join(3)
        self.addCleanup(cleanup)
        return engine, out, store, work

    def test_background_tasks_overlap_and_keep_same_named_artifacts_separate(self):
        barrier = threading.Barrier(2)
        class Writer(Scripted):
            def run(inner, prompt, *, work_dir, **kw):
                inner.work = Path(work_dir)
                # Keep the directory on the thread, since executor instances are shared.
                local.directory = Path(work_dir)
                return super(Writer, inner).run(prompt, work_dir=work_dir, **kw)
        local = threading.local()
        def execute(kind, prompt, call):
            if kind == 'plan': return plan([{'produces':['out/result.txt']}])
            barrier.wait(3)
            p = local.directory/'out/result.txt'; p.parent.mkdir(exist_ok=True)
            p.write_text(local.directory.name)
            return report('claims_done', artifacts=['out/result.txt'])
        ex = Writer(execute); e, out, store, work = self.engine(ex)
        for tid in ('a','b'):
            e.intake_new(tid,'peer','m1',json.dumps({'taskd':'submit','goal':'write a result','resource':'background',
                'acceptance':[{'id':'result','type':'files','pattern':'out/result.txt'}]}))
        self.assertTrue(wait_for(lambda: all(store.load(t)['state']=='completed' for t in ('a','b'))))
        self.assertEqual((work/'taskd-work/long/a/out/result.txt').read_text(),'a')
        self.assertEqual((work/'taskd-work/long/b/out/result.txt').read_text(),'b')
        self.assertFalse((work/'out/result.txt').exists())

    def test_quick_reply_uses_free_background_slot_next_to_desktop_and_status_query(self):
        gate = threading.Event(); started = threading.Event()
        def execute(kind, prompt, call):
            if kind == 'plan': return plan([{'resource':'desktop'}])
            if '## Current phase' not in prompt: return 'quick answer'
            started.set(); gate.wait(4)
            return report('needs_user', question='done for this test')
        ex = Scripted(execute); e, out, store, work = self.engine(ex,probes=['windows_desktop'])
        self.addCleanup(gate.set)
        for tid in ('a','b'): e.intake_new(tid,'peer','m1','inspect GUI')
        self.assertTrue(started.wait(3))
        self.assertTrue(wait_for(lambda: any((e.resources.status(t) or {}).get('state')=='waiting_desktop' for t in ('a','b'))))
        status = snapshot(e,None)
        waiting = next(t for t in status['tasks'] if t['state']=='waiting_desktop')
        self.assertEqual(len(waiting['scheduling']['blocked_by']),1)
        transport = FakeTransport(); transport.add('quick','hello')
        quick = QuickResponder(ex,transport,store,work,e.config,work/'inputs',resources=e.resources)
        quick.offer(transport.summary('quick'),None)
        self.assertTrue(wait_for(lambda: not quick.running))
        self.assertEqual(transport.replies[-1][1], 'quick answer')
        # A standalone status control task runs neither a planner nor an executor.
        transport.list_inbound=lambda:[transport.summary('query')]
        transport.add('query',json.dumps({'taskd':'status','task_id':'a'}),peer='peer')
        loop=ProviderLoop(e,transport,store,work/'inputs')
        count=len(ex.calls);loop.tick()
        self.assertEqual(len(ex.calls),count)
        self.assertEqual(transport.replies[-1][2],'completed')
        self.assertIn('scheduling',transport.replies[-1][1])
        transport.add('other',json.dumps({'taskd':'status','task_id':'a'}),peer='stranger')
        transport.list_inbound=lambda:[transport.summary('other')]
        loop.tick(); self.assertEqual(transport.replies[-1][2],'rejected')
        gate.set()
        self.assertTrue(wait_for(lambda: not e.workers))

    def test_hint_resumes_session_and_heartbeat_preserves_progress(self):
        gate=threading.Event(); started=threading.Event()
        steps=[]
        def execute(kind,prompt,call):
            if kind=='plan': return plan([{}])
            steps.append(call)
            if len(steps)==1:
                started.set();gate.wait(3)
                return report('working',summary='compiled successfully',artifacts=['build.log'])
            return report('needs_user',question='test complete')
        ex=Scripted(execute);e,out,store,work=self.engine(ex)
        self.addCleanup(gate.set)
        e.intake_new('t','peer','m1','compile')
        self.assertTrue(started.wait(3))
        e.intake_message('t','m2','{"taskd":"hint","text":"also save logs"}')
        gate.set();self.assertTrue(wait_for(lambda: store.load('t')['state']=='needs_user'))
        self.assertIsNotNone(steps[1]['session'])
        self.assertIn('also save logs',steps[1]['prompt'])
        self.assertEqual(store.load('t')['sessions_used'],2) # plan + one phase
        t=store.load('t');t['state']='running';t['last_sent_ts']=0;store.save(t)
        note=t['last_note'];last=t['last_step'];e.keepalive()
        env=out.sent[-1]['env'];self.assertTrue(env['heartbeat'])
        self.assertEqual(env['last_note'],note);self.assertEqual(env['last_step'],last)
        self.assertEqual(store.load('t')['last_step'],last)

    def test_background_spec_cannot_smuggle_desktop_checks(self):
        e,out,store,work=self.engine(Scripted(lambda *args:''))
        e.schedule=lambda:None
        t=e.intake_new('t','peer','m1','{"taskd":"submit","goal":"read files","resource":"background"}')
        with self.assertRaises(ValueError):e._validate_plan(t,plan([{'resource':'desktop'}]))
        t['state']='verifying';t['checks']=[{'id':'x','type':'window_screenshot'}]
        self.assertEqual(resource_for(t,True),'desktop')


if __name__=='__main__':unittest.main()

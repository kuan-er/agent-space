"""The quick channel answers plain messages with one executor call; JSON specs stay long tasks."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from taskd.executors.base import ExecResult
from taskd.quick import MAX_REPLIES, QuickResponder, message_text
from taskd.store import Store


class FakeTransport:
    def __init__(self):
        self.tasks, self.replies = {}, []

    def add(self, tid, text, mid='m1', peer='bpeer', state='working'):
        t = self.tasks.setdefault(tid, {'peer': peer, 'state': state, 'messages': []})
        t['messages'].append({'id': mid, 'role': 'requester', 'text': text, 'files': []})

    def summary(self, tid):
        t = self.tasks[tid]
        last = t['messages'][-1]
        return {'id': tid, 'peer': t['peer'], 'state': t['state'], 'latest': {'id': last['id'], 'role': last['role'], 'text': last['text']}}

    def get(self, tid, history=100):
        t = self.tasks[tid]
        return {'id': tid, 'peer': t['peer'], 'state': t['state'], 'messages': list(t['messages'])}

    def reply(self, tid, text, state, files=None):
        self.replies.append((tid, text, state))
        if state in ('completed', 'failed'):
            self.tasks[tid]['messages'].append({'id': 'r%d' % len(self.replies), 'role': 'provider', 'text': text, 'files': []})

    def pull(self, tid, out):
        Path(out).mkdir(parents=True, exist_ok=True)


class FakeExecutor:
    def __init__(self, text='the answer', ok=True):
        self.text, self.ok, self.calls = text, ok, []

    def run(self, prompt, *, work_dir, timeout, schema=None, session=None, mode='task', images=(), cancel=None):
        self.calls.append({'prompt': prompt, 'mode': mode, 'timeout': timeout})
        return ExecResult(self.ok, self.text if self.ok else '', None, None if self.ok else 'boom', 0.1)


class QuickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'state')
        self.transport, self.executor = FakeTransport(), FakeExecutor()

    def make(self, **config):
        return QuickResponder(self.executor, self.transport, self.store, self.tmp.name, config, Path(self.tmp.name) / 'inputs', **{
            k: config.pop(k) for k in ('accepts_peer', 'long_task_active') if k in config})

    def settle(self, q):
        for _ in range(200):
            if not q.running:
                return
            time.sleep(0.02)
        self.fail('quick answer did not finish')

    def test_plain_text_is_answered_once_with_the_node_permissions(self):
        q = self.make(); self.transport.add('t1', '今天几号?')
        self.assertTrue(q.offer(self.transport.summary('t1'), None)); self.settle(q)
        self.assertEqual([(r[2]) for r in self.transport.replies], ['working', 'completed'])
        self.assertEqual(self.transport.replies[-1][1], 'the answer')
        self.assertEqual(self.executor.calls[0]['mode'], 'task'); self.assertIn('今天几号?', self.executor.calls[0]['prompt'])
        # The same message is not answered twice, however often the loop sees it.
        self.assertTrue(q.offer(self.transport.summary('t1'), None)); self.settle(q); self.assertEqual(len(self.executor.calls), 1)

    def test_json_spec_stays_a_long_task(self):
        q = self.make(); self.transport.add('t1', json.dumps({'taskd': 'submit', 'goal': 'do it', 'acceptance': ['x']}))
        self.assertFalse(q.offer(self.transport.summary('t1'), None)); self.assertEqual(self.executor.calls, [])

    def test_a_spec_can_ask_for_quick_mode(self):
        q = self.make(); self.transport.add('t1', json.dumps({'taskd': 'submit', 'mode': 'quick', 'goal': 'What is 2+2?'}))
        self.assertTrue(q.offer(self.transport.summary('t1'), None)); self.settle(q)
        self.assertIn('What is 2+2?', self.executor.calls[0]['prompt']); self.assertNotIn('"taskd"', self.executor.calls[0]['prompt'])
        self.assertEqual(message_text('{"taskd":"cancel"}'), '')

    def test_node_can_send_plain_text_to_the_long_task_engine(self):
        q = self.make(plain_text='long'); self.transport.add('t1', 'hello')
        self.assertFalse(q.offer(self.transport.summary('t1'), None))

    def test_peers_the_node_does_not_take_are_left_to_the_engine(self):
        q = self.make(accepts_peer=lambda peer: False); self.transport.add('t1', 'hello')
        self.assertFalse(q.offer(self.transport.summary('t1'), None))

    def test_tasks_the_engine_owns_or_that_already_ended_are_not_taken(self):
        q = self.make(); self.transport.add('t1', 'hello')
        self.assertFalse(q.offer(self.transport.summary('t1'), {'id': 't1'}))
        self.transport.add('t2', 'hello', state='canceled'); self.assertFalse(q.offer(self.transport.summary('t2'), None))

    def test_follow_up_is_answered_with_the_conversation_as_context(self):
        q = self.make(); self.transport.add('t1', 'first question')
        q.offer(self.transport.summary('t1'), None); self.settle(q)
        self.transport.add('t1', 'and a follow-up', mid='m2', state='completed')
        self.assertTrue(q.offer(self.transport.summary('t1'), None)); self.settle(q)
        self.assertEqual(len(self.executor.calls), 2)
        second = self.executor.calls[1]['prompt']
        self.assertIn('first question', second); self.assertIn('the answer', second); self.assertIn('and a follow-up', second)

    def test_a_failing_executor_is_reported_not_hidden(self):
        self.executor = FakeExecutor(ok=False); q = self.make(); self.transport.add('t1', 'hello')
        q.offer(self.transport.summary('t1'), None); self.settle(q)
        self.assertEqual(self.transport.replies[-1][2], 'failed'); self.assertIn('boom', self.transport.replies[-1][1])

    def test_background_quick_reply_is_not_rejected_by_a_long_task(self):
        q = self.make(probes=['windows_desktop'], long_task_active=lambda: True); self.transport.add('t1', 'hello')
        q.offer(self.transport.summary('t1'), None); self.settle(q)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.transport.replies[-1][2], 'completed')
        self.assertIn('BACKGROUND', self.executor.calls[0]['prompt'])

    def test_a_conversation_has_a_reply_cap(self):
        q = self.make(); self.transport.add('t1', 'hello'); q.offer(self.transport.summary('t1'), None); self.settle(q)
        state = json.loads(q._path('t1').read_text()); state['replies'] = MAX_REPLIES; q._path('t1').write_text(json.dumps(state))
        self.transport.add('t1', 'again', mid='m2'); q.offer(self.transport.summary('t1'), None); self.settle(q)
        self.assertEqual(len(self.executor.calls), 1); self.assertIn('limit', self.transport.replies[-1][1])


if __name__ == '__main__':
    unittest.main()

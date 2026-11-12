import json
import unittest

from helpers import Scripted, make_engine, tmpdir
from taskd.protocol import parse_status
from taskd.quick import QuickResponder
from taskd.service import ProviderLoop
from test_quick import FakeTransport


class Transport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.fail = False
        self.pulls = []

    def list_inbound(self):
        return [self.summary(tid) for tid in self.tasks]

    def reply(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError('temporary delivery failure')
        super().reply(*args, **kwargs)

    def pull(self, tid, out):
        self.pulls.append(tid)


class CapabilityQueryTests(unittest.TestCase):
    def setUp(self):
        self.ex = Scripted(lambda *a: self.fail('capability queries must not call a model'))
        self.engine, self.out, self.store, self.work = make_engine(tmpdir(), self.ex)
        self.t = Transport()
        self.quick = QuickResponder(self.ex, self.t, self.store, self.work, {}, self.work / 'inputs')
        self.loop = self.make_loop()

    def make_loop(self):
        return ProviderLoop(self.engine, self.t, self.store, self.work / 'inputs', quick=self.quick)

    def add(self, text='{"taskd":"capabilities","protocol":"taskd/1"}', peer='friend'):
        self.t.add('query', text, peer=peer)

    def test_new_query_completes_without_work_or_files_and_survives_restart(self):
        self.add()
        self.t.tasks['query']['messages'][0]['files'] = ['unneeded.txt']
        self.loop.tick()
        tid, text, state = self.t.replies[-1]
        self.assertEqual((tid, state), ('query', 'completed'))
        self.assertEqual(parse_status(text)['capabilities'], {'protocol': 'taskd/1'})
        self.assertEqual(parse_status(text)['kind'], 'capabilities')
        self.assertEqual(list(self.store.ids()), [])
        self.assertEqual(self.t.pulls, [])
        self.assertEqual(self.ex.calls, [])
        self.assertFalse(self.quick._path('query').exists())
        self.assertNotIn('query', self.loop.rejected)
        # Simulate a stale transport summary after a restart.
        self.t.tasks['query']['messages'] = self.t.tasks['query']['messages'][:1]
        self.make_loop().tick()
        self.assertEqual(len(self.t.replies), 1)

    def test_original_guide_format_without_protocol_still_works(self):
        self.add('{"taskd":"capabilities"}')
        self.loop.tick()
        self.assertEqual(self.t.replies[-1][2], 'completed')

    def test_failed_delivery_is_retried(self):
        self.add(); self.t.fail = True
        self.loop.tick()
        self.assertNotIn('query', self.loop.queries)
        self.t.fail = False
        self.make_loop().tick()
        self.assertEqual(self.t.replies[-1][2], 'completed')

    def test_unaccepted_peer_cannot_read_capabilities(self):
        self.engine.config['accept_peers'] = ['friend']
        self.add(peer='stranger'); self.loop.tick()
        env = parse_status(self.t.replies[-1][1])
        self.assertEqual(env['reason'], 'peer_not_accepted')
        self.assertNotIn('capabilities', env)

    def test_incompatible_protocol_is_rejected(self):
        self.add('{"taskd":"capabilities","protocol":"taskd/999"}')
        self.loop.tick()
        self.assertEqual(parse_status(self.t.replies[-1][1])['reason'], 'unsupported_protocol')

    def test_other_controls_still_require_a_task(self):
        self.add('{"taskd":"cancel"}'); self.loop.tick()
        self.assertEqual(self.out.sent[-1]['env']['reason'], 'no_task')
        self.assertIn('query', self.loop.rejected)


if __name__ == '__main__':
    unittest.main()

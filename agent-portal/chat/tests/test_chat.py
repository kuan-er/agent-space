import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chat_service import Service, ChatError


class Portal:
    def __init__(self, root):
        self.DATA = root
        with self.db() as c:
            c.executescript('''CREATE TABLE users(id TEXT,name TEXT,username TEXT,enabled INTEGER);
                INSERT INTO users VALUES('u1','First','first',1),('u2','Other','other',1);
                CREATE TABLE agents(id TEXT,name TEXT,description TEXT,executor TEXT,status TEXT,owner_id TEXT,kind TEXT,enabled INTEGER,visibility TEXT,aid TEXT);
                INSERT INTO agents VALUES('a1','One','','codex','ready','u1','agent',1,'private','bpeer1');
                INSERT INTO agents VALUES('a2','Two','','codex','ready','u2','agent',1,'private','bpeer2');
                INSERT INTO agents VALUES('a3','Public','','codex','ready','u2','agent',1,'public','bpeer3');''')

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.DATA / 'portal.db')
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def agent_view(self, row, owner, user):
        return dict(row, receive_ready=row['status'] == 'ready', is_owner=row['owner_id'] == user)


class Node:
    sends = []
    threads = {}
    fail = False

    def __init__(self, portal, user):
        self.user = user

    def ensure(self):
        return 'bsource-' + self.user

    def delegate(self, peer, text):
        task = 'ix_' + uuid.uuid4().hex
        self.sends.append((peer, text, task))
        if self.fail:
            raise TimeoutError('possible delivery')
        self.threads[task] = {'state': 'working', 'messages': []}
        return task

    def message(self, task, text):
        self.sends.append((task, text, task))

    def thread(self, task):
        return self.threads[task]


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.portal = Portal(self.root)
        self.queued = []
        self.service = Service(self.portal, Node, spawn=lambda fn, *args: self.queued.append((fn, args)))
        Node.sends = []
        Node.threads = {}
        Node.fail = False
        self.thread = self.service.create_conversation('u1', {'agent_id': 'a1'})['id']

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, text='hello', key=None):
        return {'text': text, 'idempotency_key': key or str(uuid.uuid4())}

    def run_queued(self):
        fn, args = self.queued.pop(0)
        fn(*args)

    def token(self, scopes=None):
        return self.service.create_token('u1', {'name': 'integration', 'agent_ids': ['a1'], 'scopes': scopes or ['chat:read', 'chat:write'], 'expires_days': 7})

    def test_user_and_agent_isolation(self):
        with self.assertRaises(ChatError):
            self.service.create_conversation('u1', {'agent_id': 'a2'})
        with self.assertRaises(ChatError):
            self.service.conversation('u2', self.thread)

    def test_duplicate_send_exactly_one_invocation(self):
        data = self.request()
        first = self.service.send('u1', self.thread, data)
        second = self.service.send('u1', self.thread, data)
        self.assertEqual(first['id'], second['id'])
        self.assertTrue(second['idempotent_replay'])
        self.assertEqual(len(self.queued), 1)
        self.run_queued()
        self.assertEqual(len(Node.sends), 1)

    def test_same_key_different_body_rejected(self):
        data = self.request()
        self.service.send('u1', self.thread, data)
        with self.assertRaises(ChatError):
            self.service.send('u1', self.thread, dict(data, text='changed'))

    def test_unknown_does_not_resend(self):
        Node.fail = True
        data = self.request()
        self.service.send('u1', self.thread, data)
        self.run_queued()
        self.assertEqual(self.service.conversation('u1', self.thread)['state'], 'unknown')
        self.service.send('u1', self.thread, data)
        self.assertEqual(len(Node.sends), 1)
        self.assertEqual(self.queued, [])
        with self.assertRaises(ChatError):
            self.service.send('u1', self.thread, self.request())

    def test_crash_preserves_uncertainty(self):
        data = self.request()
        self.service.send('u1', self.thread, data)
        recovered = Service(self.portal, Node, spawn=lambda *args: self.fail('must not replay'))
        self.assertEqual(recovered.conversation('u1', self.thread)['state'], 'unknown')
        self.assertEqual(recovered.send('u1', self.thread, data)['state'], 'unknown')

    def test_token_secret_only_on_create_and_revocation(self):
        made = self.token()
        principal = self.service.authenticate(made['token'], 'chat:read')
        self.assertEqual(principal['agent_ids'], ['a1'])
        self.assertNotIn(made['token'], json.dumps(self.service.tokens('u1')))
        self.assertNotIn(made['token'].encode(), self.service.path.read_bytes())
        self.service.revoke_token('u1', made['record']['id'])
        with self.assertRaises(ChatError):
            self.service.authenticate(made['token'], 'chat:read')

    def test_token_scope_and_agent_allowlist(self):
        made = self.token(['chat:read'])
        principal = self.service.authenticate(made['token'], 'chat:read')
        self.assertEqual([a['id'] for a in self.service.agents('u1', principal)['agents']], ['a1'])
        with self.assertRaises(ChatError):
            self.service.authenticate(made['token'], 'chat:write')
        with self.assertRaises(ChatError):
            self.service.create_conversation('u1', {'agent_id': 'a3'}, principal)

    def test_token_revoked_after_enqueue_stops_dispatch(self):
        made = self.token()
        principal = self.service.authenticate(made['token'], 'chat:write')
        self.service.send('u1', self.thread, self.request(), principal)
        self.service.revoke_token('u1', principal['id'])
        self.run_queued()
        self.assertEqual(Node.sends, [])
        self.assertEqual(self.service.conversation('u1', self.thread)['state'], 'failed')

    def test_agent_revoked_after_enqueue_stops_dispatch(self):
        self.service.send('u1', self.thread, self.request())
        with self.portal.db() as c:
            c.execute("UPDATE agents SET enabled=0 WHERE id='a1'")
        self.run_queued()
        self.assertEqual(Node.sends, [])

    def test_remote_reply_and_evidence_preserved(self):
        self.service.send('u1', self.thread, self.request())
        self.run_queued()
        task = Node.sends[0][2]
        Node.threads[task] = {'state': 'completed', 'metadata': {'anet.effect_status': 'UNVERIFIED', 'anet.receipt_verified': 'unverified'}, 'messages': [{'msg_id': 'm1', 'from': 'bpeer1', 'body': 'reply'}, {'msg_id': 'm2', 'from': 'bsource-u1', 'body': 'my outbound'}]}
        view = self.service.conversation('u1', self.thread)
        self.assertEqual(view['state'], 'unverified')
        self.assertEqual([m['text'] for m in view['messages']], ['hello', 'reply'])
        self.assertEqual(view['messages'][-1]['metadata']['anet.receipt_verified'], 'unverified')

    def test_completed_followup_carries_visible_context(self):
        self.service.send('u1', self.thread, self.request('first'))
        self.run_queued()
        task = Node.sends[0][2]
        Node.threads[task] = {'state': 'completed', 'messages': [{'msg_id': 'm1', 'from': 'bpeer1', 'body': 'answer'}]}
        self.service.conversation('u1', self.thread)
        self.service.send('u1', self.thread, self.request('follow up'))
        self.run_queued()
        self.assertEqual(len(Node.sends), 2)
        self.assertIn('answer', Node.sends[-1][1])
        self.assertIn('follow up', Node.sends[-1][1])
        self.assertNotEqual(Node.sends[0][2], Node.sends[1][2])

    def test_input_required_uses_same_task(self):
        self.service.send('u1', self.thread, self.request())
        self.run_queued()
        task = Node.sends[0][2]
        Node.threads[task] = {'state': 'input-required', 'messages': []}
        self.service.conversation('u1', self.thread)
        self.service.send('u1', self.thread, self.request('clarification'))
        self.run_queued()
        self.assertEqual(Node.sends[-1], (task, 'clarification', task))

    def test_verified_native_me_them_envelope(self):
        self.service.send('u1', self.thread, self.request())
        self.run_queued()
        task = Node.sends[0][2]
        Node.threads[task] = {'interaction_id': task, 'role': 'outbound', 'peer': 'bpeer1', 'state': 'completed',
            'metadata': {'anet.receipt_verified': 'verified'}, 'messages': [
                {'msg_id': 'native-self', 'from': 'me', 'body': 'sent prompt'},
                {'msg_id': 'native-other', 'from': 'them', 'body': 'actual reply'}]}
        view = self.service.conversation('u1', self.thread)
        self.assertEqual(view['messages'][-1]['text'], 'actual reply')
        self.assertEqual(view['messages'][-1]['metadata'], {'anet.receipt_verified': 'verified'})
        self.assertNotIn('anet.effect_status', view['messages'][-1]['metadata'])

    def test_unverified_relative_sender_is_not_imported(self):
        self.service.send('u1', self.thread, self.request())
        self.run_queued()
        task = Node.sends[0][2]
        Node.threads[task] = {'state': 'completed', 'messages': [{'msg_id': 'bad', 'from': 'them', 'body': 'unverified sender'}]}
        view = self.service.conversation('u1', self.thread)
        self.assertEqual([m['role'] for m in view['messages']], ['user'])


if __name__ == '__main__':
    unittest.main()

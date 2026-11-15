"""Real temporary SQLite journals, injected transport faults; no network sends."""
import importlib.util
import json
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('dispatch_candidate', Path(__file__).parents[1]/'assistant.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Portal:
    Problem = RuntimeError
    def __init__(self, path):
        self.path = path
        self.LOCK = threading.RLock()
        with self.db() as c:
            c.executescript('''
                CREATE TABLE assistant_actions(id TEXT PRIMARY KEY,user_id TEXT,kind TEXT,agent_id TEXT,args TEXT,status TEXT,result TEXT,created_at INTEGER,decided_at INTEGER);
                CREATE TABLE assistant_messages(id INTEGER PRIMARY KEY,user_id TEXT,role TEXT,content TEXT,data TEXT,created_at INTEGER);
                CREATE TABLE agents(id TEXT,name TEXT);
                CREATE TABLE assistants(user_id TEXT,aid TEXT);
                CREATE TABLE assistant_dispatch_attempts(action_id TEXT PRIMARY KEY,user_id TEXT,kind TEXT,target_aid TEXT,source_aid TEXT,source_data_ref TEXT,payload_sha256 TEXT,started_at INTEGER,updated_at INTEGER,phase TEXT,task_id TEXT);
            ''')

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def one(self, c, sql, params):
        r = c.execute(sql, params).fetchone()
        return dict(r) if r else None

    @staticmethod
    def now(): return int(time.time())
    @staticmethod
    def uid(): return uuid.uuid4().hex


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(__file__).parents[1]/'evidence/test-runtime'
        self.test_root.mkdir(parents=True,exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=self.test_root)
        self.p = Portal(str(Path(self.tmp.name)/'actions.db'))
        self.user = {'id':'fixture-owner','name':'Fixture'}
        self.calls = []
        calls = self.calls
        class Node:
            def __init__(node, *_): pass
            def ensure(node): return 'fixture-local-node'
            def delegate(node, target, text):
                calls.append(('delegate',target,text))
                return 'ix_'+'a'*32
            def message(node, task, text): calls.append(('message',task,text))
            def thread(node, task):
                calls.append(('read',task))
                return {'state':'working','messages':[]}
        self.Node = Node
        self.a = m.Assistant(self.p,llm=object(),node=Node)

    def tearDown(self):
        # The only recursive cleanup is our newly created temporary test tree.
        self.assertTrue(Path(self.tmp.name).resolve().is_relative_to(self.test_root.resolve()))
        self.tmp.cleanup()

    def action(self, kind='delegate', args=None):
        args = args or {'goal':'isolated transport fault fixture','mode':'quick'}
        r = self.a.propose(self.user,kind,'fixture-target',args)
        with self.p.db() as c:
            c.execute("UPDATE assistant_actions SET status='approved' WHERE id=?",(r['action_id'],))
        return self.row(r['action_id'])

    def row(self, aid):
        with self.p.db() as c:
            return self.p.one(c,'SELECT * FROM assistant_actions WHERE id=?',(aid,))

    def send(self, x):
        self.a.send(self.user,x,'fixture-target-aid','Fixture target')

    def test_success_duplicate_worker_calls_at_most_once(self):
        x = self.action()
        threads = [threading.Thread(target=self.send,args=(x,)) for _ in range(6)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.row(x['id'])['status'],'sent')
        with self.p.db() as c:
            ledger = self.p.one(c,'SELECT * FROM assistant_dispatch_attempts WHERE action_id=?',(x['id'],))
        self.assertEqual(ledger['target_aid'],'fixture-target-aid')
        self.assertEqual(ledger['source_aid'],'fixture-local-node')
        self.assertEqual(ledger['payload_sha256'],hashlib.sha256(self.calls[0][2].encode('utf-8')).hexdigest())
        self.assertEqual(ledger['phase'],'local_acknowledged')

    def test_timeout_after_actual_invocation_stays_unknown_and_deduplicated(self):
        x = self.action()
        def lost_ack(node, target, text):
            self.calls.append(('accepted-by-transport',target))
            raise TimeoutError('sensitive transport detail must not leak')
        with patch.object(self.Node,'delegate',lost_ack): self.send(x)
        r = self.row(x['id'])
        self.assertEqual(r['status'],'unknown')
        for _ in range(2):
            self.send(x)
            same = self.a.propose(self.user,'delegate','fixture-target',json.loads(x['args']))
            self.assertEqual(same['action_id'],x['id'])
            self.assertTrue(same['do_not_resubmit'])
        self.assertEqual(len(self.calls),1)
        tasks = self.a.tool_my_tasks(self.user,{})['tasks']
        self.assertTrue(tasks[0]['do_not_resubmit'])
        with self.p.db() as c:
            logs = ' '.join(r[0] for r in c.execute('SELECT content FROM assistant_messages'))
        self.assertNotIn('sensitive transport detail',logs)

    def test_crash_after_send_claim_survives_new_assistant_instance(self):
        x = self.action()
        def crash(node,target,text):
            self.calls.append(('invoked',target))
            raise SystemExit('simulated crash')
        with patch.object(self.Node,'delegate',crash), self.assertRaises(SystemExit): self.send(x)
        self.assertEqual(self.row(x['id'])['result'],'dispatch_started')
        self.a = m.Assistant(self.p,llm=object(),node=self.Node)
        self.send(x)
        self.assertEqual(len(self.calls),1)
        self.assertTrue(self.a.tool_my_tasks(self.user,{})['tasks'][0]['do_not_resubmit'])

    def test_event_log_failure_keeps_committed_task_id_and_does_not_reinvoke(self):
        x = self.action()
        with patch.object(self.a,'add',side_effect=sqlite3.OperationalError('disk full')): self.send(x)
        self.assertEqual(self.row(x['id'])['status'],'sent')
        self.assertEqual(self.row(x['id'])['result'],'ix_'+'a'*32)
        self.send(x)
        self.assertEqual(len(self.calls),1)

    def test_preparation_failure_proves_delegate_was_not_called(self):
        x = self.action()
        with patch.object(self.Node,'ensure',side_effect=RuntimeError('setup fault')): self.send(x)
        r = self.row(x['id'])
        self.assertEqual(r['status'],'failed')
        self.assertEqual(r['result'],'dispatch_not_invoked')
        self.assertEqual(self.calls,[])

    def test_unconfirmed_message_retains_original_task_and_no_replay(self):
        tid = 'ix_'+'c'*32
        x = self.action('message',{'task_id':tid,'text':'isolated revision'})
        def lost_ack(node, task, text):
            self.calls.append(('message',task))
            raise TimeoutError()
        with patch.object(self.Node,'message',lost_ack): self.send(x)
        r = self.row(x['id'])
        self.assertEqual((r['status'],r['result']),('unknown',tid))
        self.send(x)
        self.assertEqual(len(self.calls),1)

    def test_malformed_success_output_is_not_delivered(self):
        x = self.action()
        with patch.object(self.Node,'delegate',return_value='unexpected output'): self.send(x)
        self.assertEqual(self.row(x['id'])['status'],'unknown')

    def test_pending_unapproved_action_cannot_send(self):
        r = self.a.propose(self.user,'delegate','fixture-target',{'goal':'unapproved','mode':'quick'})
        self.send(self.row(r['action_id']))
        self.assertEqual(self.calls,[])

    def test_unresolved_is_visible_after_chat_reset_without_model(self):
        x = self.action()
        with patch.object(self.Node,'delegate',side_effect=TimeoutError()): self.send(x)
        self.a.reset(self.user['id'])
        v = self.a.view(self.user['id'])
        self.assertEqual(v['messages'],[])
        self.assertEqual([a['id'] for a in v['unresolved']],[x['id']])
        self.assertEqual(v['unresolved'][0]['target_aid'],'fixture-target-aid')
        self.assertEqual(self.a.view('other-owner')['unresolved'],[])
        same = self.a.propose(self.user,'delegate','fixture-target',json.loads(x['args']))
        self.assertEqual(same['action_id'],x['id'])

    def test_unknown_known_task_allows_read_but_never_new_message(self):
        x = self.action()
        tid = 'ix_'+'b'*32
        with self.p.db() as c:
            c.execute("UPDATE assistant_actions SET status='unknown',result=? WHERE id=?",(tid,x['id']))
        self.a.tool_check_task(self.user,{'task_id':tid})
        self.assertEqual(self.calls,[('read',tid)])
        with self.assertRaises(RuntimeError):
            self.a.tool_propose_message(self.user,{'task_id':tid,'text':'must not send'})

    def test_journal_insert_failure_proves_zero_external_operations(self):
        x = self.action()
        with self.p.db() as c:
            c.execute("CREATE TRIGGER fail_ledger BEFORE INSERT ON assistant_dispatch_attempts BEGIN SELECT RAISE(FAIL,'disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.send(x)
        self.assertEqual(self.calls,[])
        self.assertIsNone(self.row(x['id'])['result'])


if __name__ == '__main__': unittest.main()

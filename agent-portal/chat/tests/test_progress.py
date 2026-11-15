import json
import unittest
from unittest.mock import patch
import test_chat as fixtures
from chat_service import taskd_status
Node = fixtures.Node


class ProgressTests(unittest.TestCase):
    setUp = fixtures.ChatTests.setUp
    tearDown = fixtures.ChatTests.tearDown
    request = fixtures.ChatTests.request
    run_queued = fixtures.ChatTests.run_queued
    def started(self):
        self.service.send('u1', self.thread, self.request('你是谁'))
        self.run_queued()
        return Node.sends[-1][2]

    def status(self, task, state='running', **fields):
        return 'TASKD ' + json.dumps(dict(task=task, state=state, protocol='taskd/1', **fields))

    def message(self, text, id):
        return dict(msg_id=id, **{'from': 'bpeer1'}, body=text)

    def test_heartbeat_is_progress_not_reply_and_storage_bounded(self):
        task = self.started()
        for i in range(12):
            Node.threads[task] = {'state': 'working', 'messages': [
                self.message('正在处理。\n\n' + self.status(task, 'queued'), 'queue'),
                self.message(self.status(task, heartbeat=True, heartbeat_at=str(i)), 'hb-' + str(i))]}
            view = self.service.conversation('u1', self.thread)
            self.assertEqual([m['role'] for m in view['messages']], ['user'])
            self.assertEqual(view['progress']['label'], '智能体正在回复')
            self.assertFalse(view['response_available'])
        with self.service.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 2)
        timestamp = view['progress']['updated_at']
        with patch('chat_service.time.time', return_value=timestamp + 90):
            self.assertEqual(self.service.conversation('u1', self.thread)['progress']['updated_at'], timestamp)

    def test_real_answer_and_terminal_evidence_survive_progress(self):
        task = self.started()
        Node.threads[task] = {'state': 'completed', 'metadata': {'anet.receipt_verified': 'verified'}, 'messages': [
            self.message('正在处理。\n\n' + self.status(task, 'queued'), 'queue'),
            self.message(self.status(task, heartbeat=True), 'hb'),
            self.message('我是 eps 智能体。', 'final')]}
        view = self.service.conversation('u1', self.thread)
        self.assertEqual([m['text'] for m in view['messages']], ['你是谁', '我是 eps 智能体。'])
        self.assertIsNone(view['progress'])
        self.assertTrue(view['response_available'])
        self.assertEqual(view['messages'][-1]['metadata'], {'anet.receipt_verified': 'verified'})

    def test_legacy_completed_rows_cleaned_in_view_and_context_without_deletion(self):
        task = self.started()
        status = self.status(task)
        with self.service.db() as c:
            for i in range(510):
                c.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                          ('old-' + str(i), self.thread, 'assistant', status, 'completed', None, None, task, 'old-remote-' + str(i), '{}', i))
            c.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      ('answer', self.thread, 'assistant', '真实回答', 'completed', None, None, task, 'answer-remote', '{}', 999))
            c.execute("UPDATE conversations SET state='completed' WHERE id=?", (self.thread,))
        view = self.service.conversation('u1', self.thread)
        self.assertEqual(len(view['messages']), 2)
        self.assertIn('真实回答', [m['text'] for m in view['messages']])
        self.assertTrue(view['response_available'])
        Node.threads[task] = {'state': 'completed', 'messages': []}
        self.service.send('u1', self.thread, self.request('继续'))
        self.run_queued()
        payload = json.loads(Node.sends[-1][1].split('\n', 1)[1])
        self.assertEqual(payload['agent']['name'], 'One')
        self.assertEqual(len(payload['history']), 2)
        self.assertNotIn('TASKD ', json.dumps(payload['history']))
        with self.service.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM messages WHERE id LIKE 'old-%'").fetchone()[0], 510)

    def test_recognizer_does_not_hide_user_text_or_code_or_other_tasks(self):
        task = self.started()
        status = self.status(task)
        self.assertIsNotNone(taskd_status(status, task))
        self.assertIsNotNone(taskd_status('Working on it.\n\n' + status, task))
        for value in ['例子：' + status, '```\n' + status + '\n```', status + '\n后续内容', status.replace('taskd/1', 'taskd/2')]:
            self.assertIsNone(taskd_status(value, task))
        self.assertIsNone(taskd_status(status, 'ix_other'))
        self.assertIsNone(taskd_status(self.status(task, state=[]), task))
        Node.threads[task] = {'state': 'completed', 'messages': [self.message('说明：' + status, 'technical-answer')]}
        view = self.service.conversation('u1', self.thread)
        self.assertIn('说明：TASKD ', view['messages'][-1]['text'])
        with self.service.db() as c:
            c.execute("UPDATE messages SET text=? WHERE role='user'", (status,))
        self.assertEqual(self.service.conversation('u1', self.thread)['messages'][0]['text'], status)

    def test_no_claim_of_answer_when_only_status_received(self):
        task = self.started()
        Node.threads[task] = {'state': 'completed', 'messages': [self.message(self.status(task), 'hb')]}
        view = self.service.conversation('u1', self.thread)
        self.assertFalse(view['response_available'])
        self.assertIsNone(view['progress'])
        self.assertEqual(len(view['messages']), 1)

    def test_only_explicit_blockers_claim_queue_wait(self):
        task = self.started()
        Node.threads[task] = {'state': 'working', 'messages': [self.message(self.status(task, 'queued', scheduling={'state': 'waiting_slot', 'resource': 'background'}), 'q')]}
        self.assertEqual(self.service.conversation('u1', self.thread)['progress']['label'], '智能体正在准备回复')
        Node.threads[task]['messages'].append(self.message(self.status(task, scheduling={'blocked_by': ['occupied']}), 'q2'))
        self.assertEqual(self.service.conversation('u1', self.thread)['progress']['label'], '智能体正忙，正在等待处理')


if __name__ == '__main__':
    unittest.main()

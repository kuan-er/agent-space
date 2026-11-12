import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock
import socket

from helpers import tmpdir
from taskd.attachments import receive, cid_sha256
from taskd.protocol import status_line
from taskd.relay import Relay, commands_for, delivery_prompt
from taskd.requesters.codex import CodexRequester
from taskd.store import Store, atomic_json, read_json

SID = '01a1ab01-d7ca-7f83-ae0e-2d2256c41b3d'


class API:
    def __init__(self, cwd):
        self.cwd, self.state, self.calls = str(cwd), 'idle', []
        self.turns = [{'id': 'old', 'status': 'completed', 'items': []}]
        self.lose_ack = self.race = False

    def __call__(self, endpoint):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def rpc(self, method, params, request_id=None):
        self.calls.append((method, params, request_id))
        if method in ('thread/read', 'thread/resume'):
            return {'thread': {'id': SID, 'status': {'type': self.state}, 'cwd': self.cwd}}
        if method == 'thread/turns/list':
            return {'data': copy.deepcopy(self.turns[:params.get('limit', 100)]), 'nextCursor': None}
        if method == 'turn/start':
            turn = {'id': 'old' if self.race else 'new', 'status': 'inProgress', 'items': [
                {'type': 'userMessage', 'id': 'user', 'clientId': params['clientUserMessageId'], 'content': params['input']}]}
            self.turns.insert(0, turn)
            if self.lose_ack:
                raise EOFError('lost response after accepting turn')
            return {'turn': copy.deepcopy(turn)}
        if method == 'thread/loaded/list':
            return {'data': [SID]}
        raise AssertionError(method)

    def pages(self, method, params):
        yield from copy.deepcopy(self.turns)

    def finish(self, final=True, diagnostic=False, state='completed'):
        self.turns[0]['status'] = state
        if final:
            self.turns[0]['items'].append({'type': 'agentMessage', 'id': 'reply', 'phase': 'final_answer', 'text': '已核对报告和文件。'})
        if diagnostic:
            self.turns[0]['items'].append({'type': 'functionCallOutput', 'id': 'log', 'output': 'TASKD-DELIVERY diagnostic marker'})


class RequesterTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tmpdir())
        self.api = API(self.root)
        self.adapter = CodexRequester(codex_home=self.root, rpc_factory=self.api)
        self.journal = self.root / 'delivery.json'

    def call(self):
        return self.adapter.delivery(SID, 'read the report', str(self.root), self.journal)

    def test_platform_without_unix_socket_reports_not_ready(self):
        with mock.patch.object(socket, 'AF_UNIX', None):
            adapter = CodexRequester(codex_home=self.root)
            r = adapter.readiness()
            self.assertFalse(r['ready'])
            self.assertIn('codex_endpoint', r['error'])

    def test_busy_session_uses_runtime_state_not_old_chat_mtime(self):
        self.api.state = 'active'
        (self.root / 'old-chat.jsonl').write_text('TASKD-DELIVERY anything')
        self.assertEqual(self.call()['state'], 'waiting_idle')
        self.assertFalse(any(x[0] == 'turn/start' for x in self.api.calls))
        self.assertIsNone(self.adapter.find_session('anything', 0))

    def test_request_ack_and_new_final_are_independent(self):
        r = self.call()
        self.assertEqual(r['state'], 'accepted')
        self.assertEqual(r['request']['id'], r['ack']['request_id'])
        self.assertEqual(r['ack']['turn_id'], 'new')
        self.assertNotIn('result', r)
        self.assertEqual(self.call()['state'], 'running')
        self.api.finish()
        r = self.call()
        self.assertEqual(r['state'], 'completed')
        self.assertEqual(r['result']['message_id'], 'reply')
        self.assertEqual(r['result']['sha256'], hashlib.sha256('已核对报告和文件。'.encode()).hexdigest())
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'turn/start']), 1)

    def test_diagnostic_text_and_completed_without_final_are_not_delivery(self):
        self.call()
        self.api.finish(final=False, diagnostic=True)
        self.assertEqual(self.call()['state'], 'failed')
        self.assertNotIn('result', read_json(self.journal))

    def test_failed_turn_is_not_delivery(self):
        self.call()
        self.api.finish(state='failed')
        self.assertEqual(self.call()['state'], 'failed')

    def test_transient_interrupted_history_during_active_turn(self):
        self.call()
        self.api.state = 'active'
        self.api.finish(final=False, state='interrupted')
        self.assertEqual(self.call()['state'], 'running')
        self.api.state = 'idle'
        self.api.finish()
        self.assertEqual(self.call()['state'], 'completed')
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'turn/start']), 1)

    def test_actual_interruption_fails_once_runtime_idle(self):
        self.call()
        self.api.state = 'active'
        self.api.finish(final=False, state='interrupted')
        self.assertEqual(self.call()['state'], 'running')
        self.api.state = 'idle'
        self.assertEqual(self.call()['state'], 'failed')

    def test_lost_ack_reconciles_client_id_without_resending(self):
        self.api.lose_ack = True
        self.assertEqual(self.call()['state'], 'reconciling')
        self.api.finish()
        result = self.call()
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['ack']['source'], 'structured_user_message_client_id')
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'turn/start']), 1)

    def test_unconfirmed_send_is_never_blindly_retried(self):
        self.api.lose_ack = True
        self.call()
        self.api.turns = self.api.turns[1:]
        for _ in range(3):
            self.assertEqual(self.call()['state'], 'reconciling')
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'turn/start']), 1)

    def test_idle_start_race_is_not_a_new_turn(self):
        self.api.race = True
        self.assertEqual(self.call()['state'], 'ambiguous')
        self.api.finish()
        self.assertEqual(self.call()['state'], 'ambiguous')

    def test_cwd_mismatch_refuses_resume(self):
        self.api.cwd = '/different'
        self.assertEqual(self.call()['state'], 'binding_error')
        self.assertFalse(self.journal.exists())

    def test_restart_keeps_result_without_repeating_request(self):
        self.call()
        self.api.finish()
        self.call()
        self.adapter = CodexRequester(codex_home=self.root, rpc_factory=self.api)
        self.assertEqual(self.call()['state'], 'completed')
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'turn/start']), 1)


class Transport:
    def __init__(self):
        self.content = b'{"ok":true}'
        self.fail = self.corrupt = False
        self.tasks = {}

    def pull(self, tid, out):
        if self.fail:
            raise RuntimeError('network unavailable')
        p = Path(out) / 'nested'
        p.mkdir()
        (p / 'report.json').write_bytes(b'bad' if self.corrupt else self.content)

    def message(self, state='completed', files=True):
        manifest = [{'name': 'report.json', 'size': len(self.content), 'sha256': hashlib.sha256(self.content).hexdigest()}] if files else []
        env = {'state': state, 'attachments': manifest, 'report': {'checks': [{'id': 'result', 'outcome': 'pass'}]}}
        return {'id': 'm1', 'role': 'provider', 'text': '完成\n' + status_line(env),
                'files': ['report.json'] if files else [], 'attachments': manifest}

    def get(self, tid, history=100):
        return self.tasks[tid]

    def list_outbound(self):
        return [{'id': x} for x in self.tasks]


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tmpdir())
        self.t = Transport()

    def receive(self, message=None, root=None):
        return receive(self.t, 't1', message or self.t.message(), {}, root or self.root)

    def test_bytes_size_and_hash_verified_in_external_directory(self):
        r = self.receive()
        self.assertEqual(r['state'], 'verified')
        self.assertEqual(r['files'][0]['size'], len(self.t.content))
        self.assertTrue(Path(r['files'][0]['path']).exists())

    def test_download_error_and_corruption_are_failed(self):
        self.t.fail = True
        self.assertEqual(self.receive()['state'], 'failed')
        self.t.fail, self.t.corrupt = False, True
        self.assertEqual(self.receive()['state'], 'failed')

    def test_missing_metadata_is_not_downloaded(self):
        m = self.t.message();m['attachments'] = []
        self.assertEqual(self.receive(m)['state'], 'failed')

    def test_content_deduplication_preserves_verified_alias_before_source(self):
        m = self.t.message()
        alias = dict(m['attachments'][0], name='same-bytes.json')
        m['attachments'].insert(0, alias)
        m['files'].insert(0, alias['name'])
        r = self.receive(m)
        self.assertEqual(r['state'], 'verified')
        self.assertEqual(len(r['files']), 2)
        self.assertEqual(r['files'][0]['aliased_from'], 'report.json')
        self.assertEqual(r['files'][0]['path'], r['files'][1]['path'])

    def test_alias_does_not_hide_corrupt_named_file(self):
        m = self.t.message()
        m['attachments'].append(dict(m['attachments'][0], name='alias.json'))
        m['files'].append('alias.json')
        original_pull = self.t.pull
        def pull(tid, out):
            original_pull(tid, out)
            (Path(out) / 'nested' / 'alias.json').write_bytes(b'corrupt')
        self.t.pull = pull
        self.assertEqual(self.receive(m)['state'], 'failed')

    def test_alias_requires_matching_declared_digest(self):
        m = self.t.message()
        m['attachments'].append(dict(m['attachments'][0], name='alias.json', sha256='0' * 64))
        m['files'].append('alias.json')
        self.assertEqual(self.receive(m)['state'], 'failed')

    def test_alias_does_not_borrow_undeclared_historical_bytes(self):
        m = self.t.message()
        m['attachments'][0]['name'] = 'absent.json'
        m['files'] = ['absent.json']
        self.assertEqual(self.receive(m)['state'], 'failed')

    def test_traversal_and_private_node_directory_rejected(self):
        m = self.t.message();m['attachments'][0]['name'] = '../report.json'
        self.assertEqual(self.receive(m)['state'], 'failed')
        self.assertEqual(self.receive(root=self.root / '.anet' / 'files')['state'], 'failed')

    def test_known_cid_is_sha256(self):
        self.assertEqual(cid_sha256('bafkreicnnjewsf47rbkdheeo73bvtmpnsi4foicg7jnapmibexlz6x4uxi'),
                         '4d6a4969179f885433908efec359b1ed9238572046fa5a07b10125d79f5f94ba')
        self.assertIsNone(cid_sha256('bad'))

    def test_failed_prompt_does_not_claim_download(self):
        prompt = delivery_prompt('t', '完成', {}, {'state': 'failed', 'error': 'bad hash'}, 'nonce', '')
        self.assertIn('文件未收到', prompt)
        self.assertNotIn('附件已下载', prompt)


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tmpdir())
        self.store = Store(self.root / 'state')
        self.api = API(self.root)
        self.adapter = CodexRequester(rpc_factory=self.api)
        self.t = Transport()
        self.t.tasks['t1'] = {'id': 't1', 'peer': 'eps', 'state': 'completed', 'messages': [self.t.message()],
                              'delivery': {'receipt_verified': 'verified'}}
        self.now = 10000
        self.relay = Relay(store=self.store, transport=self.t, adapters={'codex': self.adapter},
                           discover=False, downloads_dir=self.root / 'downloads', clock=lambda: self.now)

    def tick(self):
        self.relay.tick()
        return self.store.load_tracked('t1')

    def test_full_receipts_not_done_at_ack(self):
        self.relay.track('t1', client='codex', session=SID)
        rec = self.tick()
        self.assertEqual(rec['stages']['session']['state'], 'accepted')
        self.assertEqual(rec['stages']['files']['state'], 'verified')
        self.assertNotEqual(rec['state'], 'done')
        self.api.finish()
        rec = self.tick()
        self.assertEqual(rec['state'], 'done')
        self.assertEqual(rec['stages']['acceptance']['state'], 'provider_passed')
        self.assertEqual(rec['stages']['message']['receipt_verified'], 'verified')
        self.assertIsNone(rec['stages']['message']['effect_status'])
        self.tick()
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'turn/start']), 1)

    def test_heartbeat_does_not_erase_last_execution_or_acceptance(self):
        self.relay.track('t1', client='codex', session=SID)
        rec = self.tick()
        execution = copy.deepcopy(rec['stages']['execution'])
        acceptance = copy.deepcopy(rec['stages']['acceptance'])
        latest = copy.deepcopy(rec['last'])
        self.t.tasks['t1']['messages'].append({'id':'heartbeat','role':'provider','files':[],
            'text':status_line({'task':'t1','state':'running','heartbeat':True,'heartbeat_at':'now',
                                'scheduling':{'state':'waiting_desktop','blocked_by':['owner']}})})
        rec = self.tick()
        self.assertEqual(rec['stages']['execution'],execution)
        self.assertEqual(rec['stages']['acceptance'],acceptance)
        self.assertEqual(rec['last'],latest)
        self.assertEqual(rec['heartbeat_at'],'now')
        self.assertEqual(rec['scheduling']['state'],'waiting_desktop')

    def test_identical_blocked_messages_coalesce_but_new_files_do_not(self):
        message=self.t.tasks['t1']['messages'][0]
        env={'task':'t1','state':'blocked','reason':'same blocker'}
        message.update(id='b1',text=status_line(env),files=[])
        self.t.tasks['t1']['state']='input-required'
        self.t.tasks['t1']['messages'].append(dict(message,id='b2'))
        self.relay.track('t1')
        rec=self.tick()
        self.assertEqual(rec['coalesced'][0]['message_id'],'b2')
        self.assertEqual(len(rec['queue']),0)
        self.t.tasks['t1']['messages'].append(dict(message,id='b3',text=status_line(dict(env,attachments=[{'name':'new','sha256':'abc'}]))))
        rec=self.tick();self.assertEqual(len(rec['queue']),1)

    def test_unbound_is_not_done_or_session_delivery(self):
        self.relay.track('t1')
        rec = self.tick()
        self.assertEqual(rec['state'], 'attention')
        self.assertEqual(rec['stages']['session']['state'], 'unbound')
        self.assertFalse(rec['delivered'])

    def test_busy_session_then_idle(self):
        self.relay.track('t1', client='codex', session=SID)
        self.api.state = 'active'
        self.assertEqual(self.tick()['stages']['session']['state'], 'waiting_idle')
        self.api.state = 'idle'
        self.assertEqual(self.tick()['stages']['session']['state'], 'accepted')

    def test_bad_files_reported_separately_even_when_session_replies(self):
        self.relay.track('t1', client='codex', session=SID)
        self.t.corrupt = True
        for _ in range(7):
            self.tick();self.now += 100
        self.api.finish()
        rec = self.tick()
        self.assertEqual(rec['stages']['files']['state'], 'failed')
        self.assertEqual(rec['stages']['session']['state'], 'completed')
        prompt = next(c[1]['input'][0]['text'] for c in self.api.calls if c[0] == 'turn/start')
        self.assertIn('文件未收到', prompt)
        self.assertEqual(rec['state'], 'attention')

    def test_historical_nonce_flags_are_not_verified(self):
        atomic_json(self.store.tracked / 't1.json', {'task_id': 't1', 'state': 'done', 'session': SID,
                    'client': 'codex', 'delivered': ['m1'], 'last_delivery': {'to_session': True}})
        rec = self.tick()
        self.assertEqual(rec['stages']['session']['state'], 'legacy_unverified')
        self.assertNotEqual(rec['state'], 'done')

    def test_only_local_submission_intent_auto_binds(self):
        source = {'request_id': 'req1', 'session': SID, 'cwd': str(self.root), 'client': 'codex'}
        first = {'id': 'u1', 'role': 'requester', 'text': json.dumps({'taskd': 'submit', 'goal': 'g', 'source': source}), 'files': []}
        self.t.tasks['t1']['messages'].insert(0, first)
        self.relay.discover()
        self.assertIsNone(self.store.load_tracked('t1')['session'])
        (self.store.tracked / 't1.json').unlink()
        d = self.store.root / 'submissions';d.mkdir()
        atomic_json(d / 'req1.json', {'source': source, 'peer': 'eps'})
        self.relay.discover()
        self.assertEqual(self.store.load_tracked('t1')['session'], SID)

    def test_commands(self):
        self.assertIn('taskd answer t1', commands_for('t1', 'needs_user'))
        self.assertIn('taskd approve t1', commands_for('t1', 'awaiting_approval'))

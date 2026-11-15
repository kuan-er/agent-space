"""Return results through the same Codex app server that owns the source session.

Delivery is a durable request -> acknowledgement -> completed new turn with a final
assistant message. Chat text, notifications, mtimes and CLI exit codes prove none of
these. Ambiguous sends are reconciled using structured client IDs, never text.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import uuid

from . import guidance
from .appserver import AppServer, RPCError, RemoteRPCError
from ..store import atomic_json, read_json, utcnow


class CodexRequester:
    name = 'codex'

    def __init__(self, command=None, codex_home=None, idle_seconds=None, endpoint=None, rpc_factory=None, token_file=None):
        self.home = Path(codex_home or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        self.endpoint = endpoint or str(self.home / 'app-server-control' / 'app-server-control.sock')
        self.rpc_factory = rpc_factory or AppServer
        # Reference only. The transport owns authenticated use; never copy/log the secret.
        self.token_file = token_file

    @staticmethod
    def current_session():
        return os.environ.get('CODEX_THREAD_ID')

    def wire_guidance(self, data_dir, runner, python, *, remove=False):
        override = self.home / 'AGENTS.override.md'
        path = override if override.is_file() and override.stat().st_size else self.home / 'AGENTS.md'
        return guidance.wire(path, data_dir, runner, python, remove=remove)

    def connection(self):
        if self.token_file:
            return self.rpc_factory(self.endpoint, token_file=self.token_file)
        return self.rpc_factory(self.endpoint)

    @staticmethod
    def loaded_ids(api):
        ids, cursors, cursor = set(), set(), None
        for _ in range(100):
            page = api.rpc('thread/loaded/list', {'cursor': cursor, 'limit': 100})
            if not isinstance(page, dict) or not isinstance(page.get('data'), list) or any(
                    not isinstance(sid, str) or not sid for sid in page['data']):
                raise RPCError('invalid loaded-thread page; no source verification')
            ids.update(page['data'])
            cursor = page.get('nextCursor')
            if cursor is None:
                return ids
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise RPCError('invalid or repeated loaded-thread cursor; no source verification')
            cursors.add(cursor)
        raise RPCError('loaded-thread pagination limit; no source verification')

    def inspect(self, session_id):
        with self.connection() as api:
            loaded = self.loaded_ids(api)
            t = api.rpc('thread/read', {'threadId': session_id, 'includeTurns': False})['thread']
        return {'session': t['id'], 'state': t['status']['type'], 'cwd': t['cwd'], 'at': utcnow(),
                'source': 'codex_app_server', 'loaded_on_endpoint': session_id in loaded and t['status']['type'] != 'notLoaded',
                'loaded_evidence': 'thread/loaded/list+thread/read', 'host_instance_verified': False}

    def idle(self, session_id):
        return self.inspect(session_id)['state'] == 'idle'

    def readiness(self):
        try:
            with self.connection() as api:
                api.rpc('thread/loaded/list', {})
            return {'ready': True, 'source': 'shared_app_server', 'at': utcnow()}
        except (OSError, ValueError, RuntimeError, EOFError) as e:
            return {'ready': False, 'error': str(e)[:300], 'at': utcnow()}

    def cwd(self, session_id):
        return self.inspect(session_id)['cwd']

    def loaded_sources(self):
        """Workspace inbox discovery only. These metadata never infer a task binding."""
        sources=[]
        with self.connection() as api:
            for sid in sorted(self.loaded_ids(api)):
                try:
                    t=api.rpc('thread/read',{'threadId':sid,'includeTurns':False})['thread']
                    sources.append({'session':t['id'],'cwd':t['cwd']})
                except RPCError:
                    continue
        return sources

    def find_session(self, task_id, since_ts):
        # Legacy callers must use explicit source metadata or `taskd track --session`.
        return None

    def delivery(self, session_id, prompt, cwd, journal):
        """Advance one delivery without waiting for model execution. Journal before send."""
        rec = read_json(journal, {})
        if (rec.get('state') == 'failed' and rec.get('request') and not rec.get('ack') and
                rec.get('error_kind') is None):
            # Older versions conflated remote rejection and post-send protocol
            # faults. Preserve that observation, then inspect the SAME request.
            rec['legacy_failure'] = {k: rec.get(k) for k in ('state', 'error', 'at')}
            rec.update(state='reconciling', error_kind='legacy_unclassified', at=utcnow())
            atomic_json(journal, rec)
        if rec.get('state') in ('completed', 'failed', 'ambiguous'):
            return rec
        try:
            with self.connection() as api:
                if rec.get('request'):
                    return self._reconcile(api, rec, journal)
                t = api.rpc('thread/read', {'threadId': session_id, 'includeTurns': False})['thread']
                if Path(t['cwd']).resolve() != Path(cwd).resolve():
                    return {'state': 'binding_error', 'error': 'source working directory does not match Codex thread', 'at': utcnow()}
                if t['id'] != session_id or session_id not in self.loaded_ids(api) or t['status']['type'] == 'notLoaded':
                    return {'state': 'binding_error', 'error': 'source is not loaded on the configured host; open the intended source on its registered host; no resume or send', 'at': utcnow()}
                if t['status']['type'] != 'idle':
                    return {'state': 'waiting_idle', 'observed': t['status'], 'at': utcnow()}
                previous = api.rpc('thread/turns/list', {'threadId': session_id, 'limit': 1,
                                   'sortDirection': 'desc', 'itemsView': 'notLoaded'})['data']
                # Narrow the read/start race. Codex has no atomic start-if-idle operation.
                state = api.rpc('thread/read', {'threadId': session_id, 'includeTurns': False})['thread']['status']
                if state['type'] != 'idle':
                    return {'state': 'waiting_idle', 'observed': state, 'at': utcnow()}
                if session_id not in self.loaded_ids(api):
                    return {'state': 'binding_error', 'error': 'source unloaded before dispatch; no send', 'at': utcnow()}
                rid, client_id = str(uuid.uuid4()), str(uuid.uuid4())
                rec = {'state': 'requested', 'request': {'id': rid, 'method': 'turn/start',
                       'session': session_id, 'cwd': cwd, 'client_user_message_id': client_id,
                       'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(), 'at': utcnow()},
                       'previous_turn_id': previous[0]['id'] if previous else None}
                atomic_json(journal, rec)  # never resend automatically after a crash here
                try:
                    result = api.rpc('turn/start', {'threadId': session_id,
                                      'input': [{'type': 'text', 'text': prompt}],
                                      'clientUserMessageId': client_id, 'turnTrigger': 'taskd'}, request_id=rid)
                except RemoteRPCError as e:
                    rec.update(state='failed', error=str(e), error_kind='rpc_error_response', at=utcnow())
                    atomic_json(journal, rec)
                    return rec
                turn = result['turn']
                rec['ack'] = {'request_id': rid, 'turn_id': turn['id'], 'status': turn['status'], 'at': utcnow()}
                if turn['id'] == rec['previous_turn_id']:
                    rec.update(state='ambiguous', error='request joined an existing turn during idle/start race; no new-turn proof')
                else:
                    rec['state'] = 'accepted'
                atomic_json(journal, rec)
                return rec
        except (OSError, ValueError, RuntimeError, EOFError) as e:
            if rec.get('request'):
                rec.update(state='reconciling', error=str(e)[:500], at=utcnow())
                atomic_json(journal, rec)
                return rec
            return {'state': 'unavailable', 'error': str(e)[:500], 'at': utcnow()}

    def _reconcile(self, api, rec, journal):
        sid = rec['request']['session']
        wanted = (rec.get('ack') or {}).get('turn_id')
        target = None
        for turn in api.pages('thread/turns/list', {'threadId': sid, 'sortDirection': 'desc', 'itemsView': 'full'}):
            if wanted and turn['id'] == wanted:
                target = turn
                break
            if not wanted and any(i.get('type') == 'userMessage' and
                                  i.get('clientId') == rec['request']['client_user_message_id']
                                  for i in self._turn_items(api, sid, turn)):
                target = turn
                rec['ack'] = {'request_id': rec['request']['id'], 'turn_id': turn['id'],
                              'at': utcnow(), 'source': 'structured_user_message_client_id'}
                break
            if turn['id'] == rec.get('previous_turn_id'):
                break
        if not target:
            rec.update(state='reconciling', error='request not yet confirmed by Codex; no resend', at=utcnow())
        elif target['id'] == rec.get('previous_turn_id'):
            rec.update(state='ambiguous', error='request joined an existing turn; no new-turn proof', at=utcnow())
        elif target['status'] in ('failed', 'interrupted'):
            # A live turn can briefly appear interrupted in the persisted turn list
            # before its first event is flushed. Runtime activity wins until idle.
            runtime = api.rpc('thread/read', {'threadId': sid, 'includeTurns': False})['thread']['status']
            if runtime['type'] == 'active':
                rec.update(state='running', observation='terminal history while runtime active; awaiting stable state', at=utcnow())
            else:
                rec.update(state='failed', error=str(target.get('error') or target['status'])[:500], at=utcnow())
        elif target['status'] == 'completed':
            items = self._turn_items(api, sid, target)
            finals = [i for i in items if i.get('type') == 'agentMessage' and i.get('phase') == 'final_answer'
                      and (i.get('text') or '').strip()]
            if finals:
                final = finals[-1]
                rec.update(state='completed', result={'turn_id': target['id'], 'status': target['status'],
                           'message_id': final['id'], 'text': final['text'], 'completed_at': target.get('completedAt'),
                           'sha256': hashlib.sha256(final['text'].encode()).hexdigest(), 'observed_at': utcnow()})
                rec.pop('error', None)
            else:
                rec.update(state='failed', error='turn completed without a final assistant response', at=utcnow())
        else:
            rec.update(state='running', at=utcnow())
        atomic_json(journal, rec)
        return rec

    @staticmethod
    def _turn_items(api, session, turn):
        if turn.get('itemsView') in (None, 'full'):
            return turn.get('items', [])
        # Current app-server returns ThreadItemEntry wrappers, not bare items.
        # Keep the exact turn association; never search chat text to infer binding.
        entries = api.pages('thread/items/list', {'threadId': session, 'turnId': turn['id'], 'sortDirection': 'asc'})
        return [entry['item'] for entry in entries
                if entry.get('turnId') == turn['id'] and isinstance(entry.get('item'), dict)]

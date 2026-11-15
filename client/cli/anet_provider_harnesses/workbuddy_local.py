"""WorkBuddy PC local-assistant message provider, using only documented APIs.

Official contract: https://open.workbuddy.cn/docs/openapi (local assistant).
An online flag and message history expose no native terminal/cancel/session or
workspace-setting API. A correlated reply is never promoted to that evidence.
"""
import datetime
import hashlib
import json
import math
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid

from .store import ProviderStore, fingerprint
from anet_harnesses.network import plain

API = 'https://www.workbuddy.cn/openapi/v2'
MAX_PROMPT = 128 * 1024
MAX_REPLY = 256 * 1024
MAX_RESPONSE = 2 * 1024 * 1024


def shared_local_state_root(node_root):
    """UI and provider MUST share this owner-derived root, never task input."""
    node = plain(node_root)
    if node.name != 'taskd' or not node.is_dir():
        raise ValueError('installed taskd node directory required')
    return node.parent / 'workbuddy-local-assistant'


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class LocalProtocolError(RuntimeError):
    pass


class LocalAuthorizationRequired(LocalProtocolError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise LocalProtocolError('authenticated WorkBuddy redirect refused')


class LocalHTTP:
    """Finite authenticated API. A mutation is never refreshed/retried here."""
    def __init__(self, token_supplier, timeout=15, opener=None):
        self.supplier = token_supplier
        self.timeout = timeout
        self.opener = opener or build_opener(_NoRedirect())

    def prepare(self, method, path, payload=None):
        self.validate(method, path, payload)
        token = self.supplier()
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise LocalAuthorizationRequired('WorkBuddy local-assistant authorization required')
        return lambda: self._send(method, path, payload, token)

    @staticmethod
    def validate(method, path, payload):
        parts = urlsplit(path)
        if parts.scheme or parts.netloc or parts.fragment or '\\' in path or any(c in path for c in '\r\n'):
            raise ValueError('invalid WorkBuddy local API path')
        if method == 'GET' and parts.path == '/localassistant' and not parts.query and payload is None:
            return
        if method == 'POST' and parts.path == '/localassistant/message' and not parts.query:
            if not isinstance(payload, dict) or set(payload) != {'content', 'msg_type'} or payload['msg_type'] != 'text':
                raise ValueError('only explicit ordinary local messages are exposed')
            if not isinstance(payload['content'], str) or not payload['content'].strip():
                raise ValueError('nonempty message required')
            return
        if method == 'GET' and parts.path == '/localassistant/message' and payload is None:
            query = parse_qs(parts.query, strict_parsing=True)
            if set(query) == {'message_id'} and len(query['message_id']) == 1 and query['message_id'][0]:
                return
            if set(query) == {'limit', 'offset'} and len(query['limit']) == len(query['offset']) == 1:
                if 1 <= int(query['limit'][0]) <= 100 and 0 <= int(query['offset'][0]) <= 900:
                    return
        raise ValueError('endpoint outside finite WorkBuddy local provider contract')

    def __call__(self, method, path, payload=None):
        return self.prepare(method, path, payload)()

    def _send(self, method, path, payload, token):
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
        request = Request(API + path, data=data, method=method,
                          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except HTTPError as error:
            code = error.code
            error.close()
            if code in (401, 403):
                raise LocalAuthorizationRequired('WorkBuddy local authorization refused; original mutation was not repeated') from None
            raise LocalProtocolError('WorkBuddy HTTP request refused; native effect requires reconciliation') from None
        except (OSError, TimeoutError):
            raise LocalProtocolError('WorkBuddy transport outcome unverified; no automatic mutation retry') from None
        if len(raw) > MAX_RESPONSE:
            raise LocalProtocolError('WorkBuddy response exceeds bound')
        try:
            envelope = json.loads(raw)
        except (ValueError, UnicodeError):
            raise LocalProtocolError('WorkBuddy response is not JSON') from None
        if (not isinstance(envelope, dict) or type(envelope.get('code')) is not int
                or envelope['code'] != 0 or not isinstance(envelope.get('data'), dict)):
            raise LocalProtocolError('WorkBuddy application acknowledgement unverified')
        request_id = envelope.get('request_id')
        if not isinstance(request_id, str) or not request_id:
            raise LocalProtocolError('WorkBuddy HTTP acknowledgement has no request_id')
        # Credentials stay only in the closure/HTTP request. The journal receives
        # the documented HTTP request identity, separately from native message ID.
        return {'request_id': request_id, 'data': envelope['data']}


def content_text(message):
    content = message.get('content')
    if not isinstance(content, list):
        raise LocalProtocolError('native local message content must be an array')
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and isinstance(part.get('text'), str):
            parts.append(part['text'])
        else:
            raise LocalProtocolError('native local message content item unsupported')
    return '\n'.join(parts)


def messages_from(response):
    values = response.get('data', {}).get('messages')
    if not isinstance(values, list) or len(values) > 1000:
        raise LocalProtocolError('native message history is not a bounded list')
    seen = set()
    result = []
    for message in values:
        if not isinstance(message, dict):
            raise LocalProtocolError('native message history item invalid')
        ident = message.get('message_id')
        if not isinstance(ident, str) or not ident or ident in seen:
            raise LocalProtocolError('native history message identity missing or repeated')
        if message.get('role') not in ('user', 'assistant'):
            raise LocalProtocolError('native history role unsupported')
        text = content_text(message)
        if len(text.encode('utf-8')) > MAX_REPLY:
            raise LocalProtocolError('native history content exceeds reply bound')
        seen.add(ident)
        result.append(message)
    return result


class WorkBuddyLocalRunner:
    """One owned message interval, durable ACK and conservative unknown effects.

    The owner must dedicate the native PC assistant while this provider is
    enabled. OpenAPI cannot enforce workspace selection, independent sessions or
    read-only tools. A reply marker plus unchanged history only correlates the
    assistant's own report; deterministic taskd checks decide business success.
    """
    def __init__(self, state_root, request, *, settle_seconds=2.0, poll_seconds=1.0,
                 clock=time.monotonic, sleep=time.sleep):
        self.store = ProviderStore(state_root)
        self.request = request
        self.settle_seconds = max(0.0, float(settle_seconds))
        self.poll_seconds = max(0.01, float(poll_seconds))
        if not math.isfinite(self.settle_seconds) or not math.isfinite(self.poll_seconds):
            raise ValueError('finite observation intervals required')
        self.clock, self.sleep = clock, sleep

    def readiness(self):
        response = self.request('GET', '/localassistant')
        online = response.get('data', {}).get('online')
        if type(online) is not bool:
            raise LocalProtocolError('native online state is not boolean')
        return {'online': online, 'http_request_id': response['request_id'],
                'native_busy_state_available': False, 'native_terminal_available': False,
                'workspace_binding_available': False, 'native_cancel_available': False,
                'pc_identity_verified': False, 'single_native_assistant_verified': False}

    def status(self, operation_id):
        """Read evidence without dispatch, authorization, or reconciliation."""
        operation = self.store.load(operation_id)
        if operation is None:
            return None
        return self._view(operation)

    def observe(self, operation_id, *, timeout, cancel=None):
        """Read-only native reconciliation. Never creates or first-sends a job.

        A service may call this after restart, including for a reserved operation
        that was never dispatched. Unlike run(), prepared intent cannot send.
        """
        operation = self.store.load(operation_id)
        if operation is None:
            raise LocalProtocolError('provider operation not found')
        request = operation['input']
        return self.run(request['prompt'],work_dir=request['work_dir'],mode=request['mode'],
                        timeout=timeout,cancel=cancel,operation_id=operation_id,_observation_only=True)

    def source(self, work_dir, source_id=None):
        """A delegation context, deliberately not a native WorkBuddy session."""
        work = self._input('source validation only', work_dir, 'task')['work_dir']
        source_id = source_id or str(uuid.uuid4())
        self.store.operation_path(source_id)  # canonical opaque ID validation
        return {'local_id': source_id, 'harness': 'workbuddy', 'surface': 'workbuddy_local_assistant',
                'work_dir': work, 'workspace_scope': 'local_delegation_context', 'native_session_id': None,
                'native_goal_available': False, 'native_terminal_available': False,
                'workspace_binding_verified': False, 'native_cancel_available': False,
                'pc_identity_verified': False, 'single_native_assistant_verified': False}

    def _history(self, message_id=None, offset=0):
        path = '/localassistant/message?' + urlencode({'message_id': message_id} if message_id else {'limit': 100, 'offset': offset})
        return messages_from(self.request('GET', path))

    @staticmethod
    def _input(prompt, work_dir, mode):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode('utf-8')) > MAX_PROMPT:
            raise ValueError('provider prompt must be nonempty and at most 128 KiB')
        work = plain(work_dir)
        if not work.is_dir():
            raise ValueError('explicit existing provider work directory required')
        if mode not in ('task', 'review'):
            raise ValueError('unsupported provider execution mode')
        return {'prompt': prompt, 'work_dir': str(work), 'mode': mode}

    def _new_operation(self, request, operation_id=None):
        operation_id = operation_id or str(uuid.uuid4())
        self.store.operation_path(operation_id)
        begin, end = 'ANET_LOCAL_' + uuid.uuid4().hex + '_BEGIN', 'ANET_LOCAL_' + uuid.uuid4().hex + '_END'
        wrapped = ('ANet provider message: ' + operation_id + '\n'
            'Work only in this explicitly authorized local directory: ' + request['work_dir'] + '\n'
            'First verify the WorkBuddy native workspace and tool permissions. If this directory is not available, report that fact and stop. '
            'This message does not grant additional filesystem, account, network or tool permissions.\n'
            + ('Review only: do not change files or invoke commands. Native permissions remain in effect.\n' if request['mode'] == 'review' else '')
            + '\nRequested task:\n' + request['prompt']
            + '\n\nWhen you consider this response finished, put ONLY the requested final answer (including its required JSON, if any) '
              'between these two unique marker lines. Do not use them in progress updates or quoted examples:\n'
            + begin + '\n<final answer>\n' + end)
        return {'schema': 'anet-workbuddy-local-operation/1', 'operation_id': operation_id,
                'input': request, 'input_sha256': fingerprint(request), 'state': 'prepared',
                'message_content': wrapped, 'message_content_sha256': hashlib.sha256(wrapped.encode()).hexdigest(),
                'begin': begin, 'end': end, 'created_at': utcnow(), 'http_ack': None, 'native_message_id': None,
                'native_terminal_verified': False, 'native_cancel_confirmed': False,
                'business_effect_verified': False, 'workspace_binding_verified': False,
                'pc_identity_verified': False,
                'original_message_must_not_be_repeated': True}

    def _view(self, operation, *, timed_out=None, canceled=None):
        result = {k: operation.get(k) for k in ('operation_id', 'state', 'http_ack', 'native_message_id',
                'reply_evidence', 'native_terminal_verified', 'native_cancel_confirmed',
                'business_effect_verified', 'workspace_binding_verified', 'pc_identity_verified', 'reason_code')}
        result.update(reply=operation.get('reply'),
                      timed_out=operation.get('observation_timed_out', False) if timed_out is None else timed_out,
                      canceled=operation.get('observation_canceled', False) if canceled is None else canceled,
                      evidence_path=str(self.store.operation_path(operation['operation_id'])),
                      original_message_must_not_be_repeated=True)
        return result

    def _outcome(self, operation, state=None, error=None, *, reply=None, timed_out=False, canceled=False):
        if state:
            operation['state'] = state
        if error:
            operation['reason_code'] = error
        elif state == 'reply_observed':
            operation.pop('reason_code', None)
        operation['observation_timed_out'] = timed_out
        operation['observation_canceled'] = canceled
        operation['observed_at'] = utcnow()
        self.store.save(operation)
        return self._view(operation, timed_out=timed_out, canceled=canceled)

    def _recover_ack(self, operation):
        matches = []
        exhausted = False
        for offset in range(0, 1000, 100):
            page = self._history(offset=offset)
            for message in page:
                if message['role'] != 'user':
                    continue
                text = content_text(message)
                if hashlib.sha256(text.encode()).hexdigest() == operation['message_content_sha256']:
                    matches.append(message['message_id'])
            if len(page) < 100:
                exhausted = True
                break
        if not exhausted or len(set(matches)) != 1:
            return False
        operation['native_message_id'] = matches[0]
        operation['state'] = 'ack_reconciled_from_exact_owned_message'
        operation['ack_reconciliation'] = {'native_message_id': matches[0], 'own_content_sha256': operation['message_content_sha256'],
                                           'http_request_id': None, 'at': utcnow()}
        self.store.save(operation)
        return True

    def run(self, prompt, *, work_dir, timeout, mode='task', cancel=None, operation_id=None,_observation_only=False):
        requested = self._input(prompt, work_dir, mode)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('positive observation timeout required')
        deadline = self.clock() + float(timeout)
        with self.store.locked():
            lease = self.store.lease()
            previous = self.store.load(operation_id) if operation_id else None
            if previous and previous['input_sha256'] != fingerprint(requested):
                raise LocalProtocolError('operation ID is already bound to different input')
            if previous and previous['state'] == 'reply_observed':
                # A UI/IPC retry carries the SAME operation ID, including after
                # the lease was released. Returning evidence never repeats POST.
                if lease and lease.get('operation_id') == operation_id:
                    self.store.release_observed_reply(previous)
                return self._view(previous)
            if lease:
                operation = self.store.load(lease['operation_id'])
                if not operation or operation['input_sha256'] != lease['input_sha256']:
                    raise LocalProtocolError('local assistant lease evidence differs')
                if operation['input_sha256'] != fingerprint(requested):
                    return {'operation_id': operation['operation_id'], 'state': 'blocked_by_unresolved_operation',
                            'reply': None, 'native_terminal_verified': False, 'native_cancel_confirmed': False,
                            'business_effect_verified': False, 'workspace_binding_verified': False,
                            'evidence_path': str(self.store.operation_path(operation['operation_id'])),
                            'original_message_must_not_be_repeated': True, 'timed_out': False, 'canceled': False}
                if operation_id and operation['operation_id'] != operation_id:
                    raise LocalProtocolError('another operation owns the local assistant lease')
                if operation['state'] == 'ambiguous':
                    return self._outcome(operation)
            else:
                if previous:
                    raise LocalProtocolError('nonfinal operation lost its durable lease; reconciliation required')
                if _observation_only:
                    raise LocalProtocolError('read-only observation cannot create a provider operation')
                if cancel is not None and cancel.is_set():
                    return {'state': 'not_dispatched', 'reply': None, 'canceled': True,
                            'timed_out': False, 'native_cancel_confirmed': False,
                            'native_terminal_verified': False, 'business_effect_verified': False,
                            'workspace_binding_verified': False, 'original_message_must_not_be_repeated': False}
                ready = self.readiness()
                if not ready['online']:
                    return {'state': 'offline_not_dispatched', 'reply': None, 'timed_out': False, 'canceled': False,
                            'native_terminal_verified': False, 'native_cancel_confirmed': False,
                            'business_effect_verified': False, 'workspace_binding_verified': False,
                            'original_message_must_not_be_repeated': False}
                operation = self._new_operation(requested, operation_id)
                self.store.reserve(operation)
            if operation['state'] == 'prepared':
                if _observation_only:
                    return self._view(operation)
                # Resolve a token before durable dispatch intent if the finite
                # HTTP transport supports preparation. No auth retry follows POST.
                payload = {'content': operation['message_content'], 'msg_type': 'text'}
                prepared = self.request.prepare('POST', '/localassistant/message', payload) if hasattr(self.request, 'prepare') else lambda: self.request('POST', '/localassistant/message', payload)
                if cancel is not None and cancel.is_set() or self.clock() >= deadline:
                    # The durable state stays prepared, proving no mutation was
                    # attempted. A later explicit retry may continue this ID.
                    return self._outcome(operation, error='not_dispatched_before_observation_deadline',
                                         timed_out=self.clock() >= deadline,
                                         canceled=bool(cancel is not None and cancel.is_set()))
                operation['state'] = 'dispatching'
                self.store.save(operation)
                try:
                    ack = prepared()
                    message_id = ack.get('data', {}).get('message_id')
                    request_id = ack.get('request_id')
                    if not isinstance(message_id, str) or not message_id or not isinstance(request_id, str) or not request_id:
                        raise LocalProtocolError('native send acknowledgement incomplete')
                    operation['native_message_id'] = message_id
                    operation['http_ack'] = {'request_id': request_id, 'native_message_id': message_id, 'at': utcnow()}
                    operation['state'] = 'acknowledged'
                    self.store.save(operation)
                except Exception as error:
                    return self._outcome(operation, 'dispatch_unverified', type(error).__name__)
            if not operation.get('native_message_id'):
                try:
                    if not self._recover_ack(operation):
                        return self._outcome(operation, 'dispatch_unverified', 'native_message_identity_not_recovered')
                except (LocalProtocolError, OSError, ValueError) as error:
                    return self._outcome(operation, 'dispatch_unverified', type(error).__name__)
            candidate = None
            candidate_since = None
            while self.clock() < deadline:
                if cancel is not None and cancel.is_set():
                    return self._outcome(operation, 'observation_canceled_native_effect_unverified',
                                         'native_cancel_not_available', canceled=True)
                try:
                    messages = self._history(operation['native_message_id'])
                    signatures = []
                    replies = []
                    for message in messages:
                        text = content_text(message)
                        signatures.append((message['message_id'], message['role'], hashlib.sha256(text.encode()).hexdigest()))
                        if message['role'] == 'user' and message['message_id'] != operation['native_message_id']:
                            return self._outcome(operation, 'ambiguous', 'another_user_message_interleaved')
                        if message['role'] != 'assistant':
                            continue
                        begin, end = operation['begin'], operation['end']
                        if begin in text and end in text.split(begin, 1)[1]:
                            if text.count(begin) != 1 or text.count(end) != 1:
                                return self._outcome(operation, 'ambiguous', 'reply_marker_repeated')
                            reply = text.split(begin, 1)[1].split(end, 1)[0].strip()
                            if reply:
                                replies.append((message['message_id'], reply))
                    if len(replies) > 1:
                        return self._outcome(operation, 'ambiguous', 'multiple_correlated_assistant_replies')
                    if len(replies) == 1:
                        snapshot = fingerprint({'history': signatures, 'reply': replies[0]})
                        if candidate != snapshot:
                            candidate, candidate_since = snapshot, self.clock()
                        elif self.clock() - candidate_since >= self.settle_seconds:
                            message_id, reply = replies[0]
                            operation['reply'] = reply
                            operation['reply_evidence'] = {'assistant_message_id': message_id,
                                'own_user_message_id': operation['native_message_id'],
                                'reply_sha256': hashlib.sha256(reply.encode()).hexdigest(),
                                'correlation': 'unique_owned_nonce_and_uninterleaved_history',
                                'history_sha256': snapshot, 'quiet_observation_seconds': self.settle_seconds,
                                'native_terminal_status': None, 'at': utcnow()}
                            outcome = self._outcome(operation, 'reply_observed', reply=reply)
                            self.store.release_observed_reply(operation)
                            return outcome
                    else:
                        candidate, candidate_since = None, None
                except (LocalProtocolError, OSError, ValueError) as error:
                    return self._outcome(operation, 'observation_unverified', type(error).__name__)
                self.sleep(min(self.poll_seconds, max(0.0, deadline - self.clock())))
            return self._outcome(operation, 'observation_timed_out_native_effect_unverified',
                                 'native_terminal_not_available', timed_out=True)

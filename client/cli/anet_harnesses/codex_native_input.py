"""Ephemeral, once-only owner decisions for the owned Codex 0.166 transport.

The native request ID retains its JSON type and transport generation.  A local
UI UUID never serves as a native ID.  No answer is an execution/terminal ACK.
"""
import copy
import queue
import threading
import time
import uuid

from .ledger import OwnershipError
from .native_input import _bounded, _validation_rejected


APPROVALS = ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval')
QUESTIONS = 'item/tool/requestUserInput'
LIFECYCLE = ('serverRequest/resolved', 'turn/started', 'turn/completed',
             'thread/closed', 'thread/status/changed')
TERMINAL = ('completed', 'failed', 'interrupted')


def typed_request_id(value):
    if type(value) is int and -(2 ** 63) <= value < 2 ** 63:
        return ('integer', value)
    if isinstance(value, str) and len(value) <= 512:
        return ('string', value)
    raise ValueError('native request ID must be an exact int64 or bounded string')


def _identity(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError('native request requires exact thread/turn/item identity')
    return value


class CodexNativeInput:
    def __init__(self, ledger, current_rpc, *, timeout_seconds=600, max_pending=16,
                 clock=time.monotonic, wall_clock=time.time):
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 3600:
            raise ValueError('native input timeout must be bounded')
        self.ledger = ledger
        self.current_rpc = current_rpc
        self.timeout = timeout_seconds
        self.max_pending = max_pending
        self.clock = clock
        self.wall_clock = wall_clock
        self.lock = threading.RLock()
        self.closed = False
        self.pending = {}
        self.seen = set()
        self.resolved = set()
        self.active_turns = {}
        self.finished_turns = set()

    def _owner(self, thread_id):
        sources = [s for s in self.ledger.sessions()
                   if s['harness'] == 'codex' and s['native_id'] == thread_id]
        if len(sources) != 1:
            raise OwnershipError('native request has no unique owned source')
        return sources[0]

    def _current(self, row):
        rpc = self.current_rpc()
        return rpc is row['rpc'] and rpc.generation == row['generation'] and not rpc.closed

    def _binding(self, row):
        source = self._owner(row['thread_id'])
        if source != row['source']:
            raise OwnershipError('native source ownership has changed')
        native_turn = (row['generation'], row['thread_id'], row['turn_id'])
        if native_turn in self.finished_turns:
            raise OwnershipError('original native turn has ended')
        active = self.active_turns.get(native_turn[:2])
        if active is not None and active != row['turn_id']:
            raise OwnershipError('original native turn has been replaced')
        operations = [op for op in self.ledger.operations(source['id'])
                      if op['kind'] == 'prompt' and op['native_id'] == row['turn_id']]
        if operations and all(op['state'] in TERMINAL for op in operations):
            raise OwnershipError('original owned turn is already terminal')
        if active != row['turn_id'] and not any(op['state'] not in TERMINAL for op in operations):
            raise OwnershipError('native request has no observed active owned turn')
        return next((op['id'] for op in operations if op['state'] not in TERMINAL), None)

    @staticmethod
    def _questions(params):
        if type(params.get('isBlocking')) is not bool:
            raise ValueError('native question blocking mode is missing')
        questions = params.get('questions')
        if not isinstance(questions, list) or not 1 <= len(questions) <= 16:
            raise ValueError('native questions must be a bounded nonempty list')
        names = set()
        for question in questions:
            if not isinstance(question, dict):
                raise ValueError('native question must be an object')
            key = _identity(question.get('id'))
            if key in names:
                raise ValueError('native question IDs must be distinct')
            names.add(key)
            if not all(isinstance(question.get(k), str) for k in ('header', 'question')):
                raise ValueError('native question text is missing')
            for flag in ('isOther', 'isSecret'):
                if flag in question and type(question[flag]) is not bool:
                    raise ValueError('native question flag is invalid')
            options = question.get('options')
            if options is not None and (not isinstance(options, list) or len(options) > 32 or
                    not all(isinstance(o, dict) and isinstance(o.get('label'), str) and
                            isinstance(o.get('description'), str) for o in options)):
                raise ValueError('native question options are invalid')
        return names

    @classmethod
    def _validate_params(cls, method, params):
        if not isinstance(params, dict):
            raise ValueError('native request params must be an object')
        for key in ('threadId', 'turnId', 'itemId'):
            _identity(params.get(key))
        if method in APPROVALS:
            if type(params.get('startedAtMs')) is not int or not -(2 ** 63) <= params['startedAtMs'] < 2 ** 63:
                raise ValueError('native approval timestamp is missing')
            decisions = params.get('availableDecisions')
            if decisions is not None and (not isinstance(decisions, list) or len(decisions) > 32):
                raise ValueError('native approval choices are invalid')
        elif method == QUESTIONS:
            cls._questions(params)
        else:
            raise ValueError('unsupported native owner input method')

    @classmethod
    def _validate_result(cls, row, result):
        result = _bounded(result)
        if row['method'] in APPROVALS:
            if not isinstance(result, dict) or set(result) != {'decision'} or result['decision'] not in ('accept', 'decline', 'cancel'):
                raise ValueError('choose one explicit native approval decision')
            params = row['params'] if isinstance(row['params'], dict) else {}
            choices = params.get('availableDecisions')
            if choices is not None and result['decision'] not in choices:
                raise ValueError('native approval decision was not offered')
        else:
            expected = cls._questions(row['params'])
            if not isinstance(result, dict) or set(result) != {'answers'} or not isinstance(result['answers'], dict) or set(result['answers']) != expected:
                raise ValueError('answer every exact native question ID once')
            for answer in result['answers'].values():
                if not isinstance(answer, dict) or set(answer) != {'answers'} or not isinstance(answer['answers'], list) or not 1 <= len(answer['answers']) <= 32 or not all(isinstance(v, str) and v.strip() and len(v) <= 4096 for v in answer['answers']):
                    raise ValueError('native question answers require nonempty string lists')
        return result

    def _retire(self, key):
        return self.pending.pop(key, None)

    def drain(self, rpc):
        # Global local lock order: broker -> transport. The native reader takes
        # ONLY transport.lock when publishing; it never calls this broker.
        with self.lock, rpc.lock:
            ordered = getattr(rpc, 'host_inputs', None)
            if ordered is not None:
                while True:
                    try:generation, message = ordered.get_nowait()
                    except queue.Empty:break
                    self.receive(rpc, generation, message)
            # Retire the compatibility queue, which has no lifecycle ordering.
            while True:
                try:rpc.host_requests.get_nowait()
                except queue.Empty:break

    def _deny(self, row, reason, *, approval_decision=True):
        # Retire BEFORE a transport write. A write can take effect and then fail;
        # neither a duplicate request nor UI retry is allowed to send it again.
        if not self._current(row):
            return
        try:
            params = row['params'] if isinstance(row['params'], dict) else {}
            choices = params.get('availableDecisions')
            decision = next((d for d in ('cancel', 'decline') if choices is None or d in choices), None)
            if approval_decision and row['method'] in APPROVALS and decision is not None:
                row['rpc'].answer(row['native_request_id'], {'decision': decision}, row['generation'])
            else:
                row['rpc'].answer_error(row['native_request_id'],
                    {'code': -32000, 'message': 'Explicit owner input unavailable: ' + reason}, row['generation'])
        except Exception:
            pass  # Unknown write effect remains retired; never retry or grant.

    def receive(self, rpc, generation, message):
        with self.lock:
            if rpc is not self.current_rpc() or generation != rpc.generation:
                return
            if not isinstance(message, dict):
                return
            method = message.get('method')
            params = message.get('params', {})
            if 'id' not in message:
                return self._event(generation, method, params)
            try:
                typed = typed_request_id(message['id'])
            except ValueError:
                return  # There is no schema-valid ID to answer.
            key = (generation, *typed)
            if key in self.seen:
                duplicate = self._retire(key)
                if duplicate is not None:
                    self._deny(duplicate, 'duplicate native request identity')
                return
            self.seen.add(key)
            row = {'rpc': rpc, 'generation': generation,
                   'native_request_id': message['id'], 'method': method,
                   'params': params if isinstance(params, dict) else {}}
            try:
                row['params'] = params = _bounded(params)
                self._validate_params(method, params)
                remaining = self.timeout
                if method in APPROVALS:
                    age = self.wall_clock() - params['startedAtMs'] / 1000
                    if age < -60 or age >= self.timeout:
                        raise ValueError('native approval timestamp is stale or invalid')
                    remaining = min(self.timeout, self.timeout - age)
                row.update(thread_id=params['threadId'], turn_id=params['turnId'],
                           source=self._owner(params['threadId']))
                if (key, row['thread_id']) in self.resolved:
                    return
                operation_id = self._binding(row)
                if self.closed or len(self.pending) >= self.max_pending:
                    self._deny(row, 'owner view closed or pending queue full')
                    return
                row.update(request_id='codex-' + str(uuid.uuid4()),
                           deadline=self.clock() + remaining,
                           expires_at=self.wall_clock() + remaining,
                           operation_id=operation_id)
                self.pending[key] = row
            except (ValueError, TypeError, OwnershipError):
                self._deny(row, 'invalid or unowned native request', approval_decision=False)

    def _event(self, generation, method, params):
        if not isinstance(params, dict):
            return
        thread = params.get('threadId')
        if not isinstance(thread, str):
            return
        if method == 'serverRequest/resolved':
            try:key = (generation, *typed_request_id(params.get('requestId')))
            except ValueError:return
            self.resolved.add((key, thread))
            row = self.pending.get(key)
            if row and row['thread_id'] == thread:
                self._retire(key)
        elif method in ('turn/started', 'turn/completed'):
            turn = params.get('turn')
            if not isinstance(turn, dict) or not isinstance(turn.get('id'), str):
                return
            identity = (generation, thread, turn['id'])
            if method == 'turn/started':
                if identity in self.finished_turns or turn.get('status') != 'inProgress':
                    return
                self.active_turns[identity[:2]] = turn['id']
            elif turn.get('status') in TERMINAL:
                self.finished_turns.add(identity)
                if self.active_turns.get(identity[:2]) == turn['id']:
                    self.active_turns.pop(identity[:2], None)
            else:
                return
            for key, row in list(self.pending.items()):
                if row['generation'] == generation and row['thread_id'] == thread and (
                        (method == 'turn/started' and row['turn_id'] != turn['id']) or
                        (method == 'turn/completed' and row['turn_id'] == turn['id'])):
                    self._retire(key)
        elif method == 'thread/closed' or method == 'thread/status/changed' and isinstance(params.get('status'), dict) and params['status'].get('type') in ('idle', 'notLoaded', 'systemError'):
            active = self.active_turns.pop((generation, thread), None)
            if active is not None:
                self.finished_turns.add((generation, thread, active))
            for key, row in list(self.pending.items()):
                if row['generation'] == generation and row['thread_id'] == thread:
                    self._retire(key)

    def sweep(self):
        with self.lock:
            rpc = self.current_rpc()
            if rpc is None:
                self.pending.clear()
                return
            with rpc.lock:
                self.drain(rpc)
                for key, row in list(self.pending.items()):
                    try:
                        if not self._current(row):
                            raise OwnershipError('original native runtime unavailable')
                        self._binding(row)
                    except OwnershipError:
                        self._retire(key)
                        continue
                    if self.clock() >= row['deadline']:
                        self._retire(key)
                        self._deny(row, 'owner decision timed out')

    def list_requests(self):
        with self.lock:
            result = []
            for row in self.pending.values():
                if self.clock() >= row['deadline'] or not self._current(row):
                    continue
                try:self._binding(row)
                except OwnershipError:continue
                detail = copy.deepcopy(row['params'])
                detail.update(operation_correlation_verified=False,
                              binding_scope='owned_native_thread_exact_active_turn')
                result.append({'request_id': row['request_id'], 'source_id': row['source']['id'],
                    'native_id': row['thread_id'], 'turn_id': row['turn_id'],
                    'operation_id': row['operation_id'], 'method': row['method'],
                    'requires_user_action': True, 'expires_at': row['expires_at'], 'detail': detail})
            return _bounded(result, 2 * 1024 * 1024)

    def answer(self, request_id, source_id, result, user_authorized=False, *, validation_result=False):
        if type(validation_result) is not bool:
            raise ValueError('validation_result must be boolean')
        if user_authorized is not True:
            raise OwnershipError('native response needs explicit originating-user action')
        with self.lock:
            rpc = self.current_rpc()
            if rpc is None:
                raise OwnershipError('original native runtime is no longer current')
            with rpc.lock:
                # No already-published native retirement can enter between this
                # final drain, the exact owner/turn check and the response write.
                self.drain(rpc)
                pair = next(((key, row) for key, row in self.pending.items()
                             if row['request_id'] == request_id), None)
                if pair is None or pair[1]['source']['id'] != source_id:
                    raise OwnershipError('native request is no longer pending in this source')
                key, row = pair
                if self.closed or self.clock() >= row['deadline']:
                    raise OwnershipError('native owner decision is closed or expired')
                if not self._current(row):
                    self._retire(key)
                    raise OwnershipError('original native runtime is no longer current')
                try:self._binding(row)
                except OwnershipError:
                    self._retire(key)
                    raise
                try:
                    result = self._validate_result(row, result)
                except ValueError:
                    if not validation_result:
                        raise
                    if (self.closed or self.clock() >= row['deadline'] or
                            self.pending.get(key) is not row or not self._current(row)):
                        raise OwnershipError('native request is no longer pending in this source') from None
                    try:self._binding(row)
                    except OwnershipError:
                        self._retire(key)
                        raise
                    return _validation_rejected(request_id, source_id)
                self._retire(key)
                # Deliberately propagate write ambiguity while keeping the
                # request retired; flush is not native execution terminal ACK.
                row['rpc'].answer(row['native_request_id'], result, row['generation'])
                return {'response_sent': True, 'request_id': request_id, 'source_id': source_id, 'operation_id': row['operation_id'],
                        'native_acknowledgement_verified': False,
                        'terminal_observation_required': True}

    def close(self):
        with self.lock:
            rpc = self.current_rpc()
            if rpc is None:
                self.closed = True
                self.pending.clear()
                return
            with rpc.lock:
                self.drain(rpc)
                self.closed = True
                for key in list(self.pending):
                    self._deny(self._retire(key), 'owner view closed')

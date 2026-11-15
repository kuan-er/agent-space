"""Ephemeral owner decisions for one locally bound live native SDK activity.

Native tool arguments stay in memory while a decision is pending. A process
restart never restores an approval, and no permission rules are persisted.
"""
import asyncio
import concurrent.futures
import json
import threading
import time
import uuid
from dataclasses import dataclass

from .ledger import OwnershipError


class NativeInputClosed(RuntimeError):
    pass


class NativeInputTimeout(RuntimeError):
    pass


def _bounded(value, limit=65536):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(raw.encode('utf-8')) > limit:
        raise ValueError('native input exceeds the owner decision size limit')
    return json.loads(raw)


def _validation_rejected(request_id, source_id):
    # Only the current owner-answer validation attempt is described here.
    # This is never a global native execution/business no-effect assertion.
    return {'state': 'answer_validation_rejected',
            'request_id': request_id, 'source_id': source_id,
            'answer_intent_recorded': False, 'response_recorded': False,
            'response_sent': False, 'retry_authorized': True,
            'retry_scope': 'same_native_request_after_validation',
            'reason': '回答格式不符合当前请求要求，请修改后提交同一请求。'}


@dataclass
class _Pending:
    row: dict
    future: concurrent.futures.Future
    validator: object


class NativeInputRequests:
    def __init__(self, *, max_pending=16):
        self._lock = threading.RLock()
        self._pending = {}
        self._closed = False
        self._max_pending = max_pending
        self._closed_operations = set()

    def _prepare(self, source_id, operation_id, native_id, method, detail,
                 validator, timeout_seconds):
        if not all(isinstance(value, str) and value and len(value) <= 256
                   for value in (source_id, operation_id, native_id, method)):
            raise ValueError('native input requires exact source and activity identity')
        if not isinstance(detail, dict) or not callable(validator):
            raise ValueError('native input requires a detail object and validator')
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 3600:
            raise ValueError('native input timeout must be bounded')
        row = {'request_id': 'native-' + str(uuid.uuid4()),
               'source_id': source_id, 'operation_id': operation_id,
               'native_id': native_id, 'method': method,
               'requires_user_action': True, 'detail': _bounded(detail),
               'expires_at': time.time() + timeout_seconds}
        pending = _Pending(row, concurrent.futures.Future(), validator)
        with self._lock:
            if self._closed:
                raise NativeInputClosed('native owner decision service has closed')
            if (source_id, operation_id) in self._closed_operations:
                raise NativeInputClosed('original native activity has ended')
            if len(self._pending) >= self._max_pending:
                raise NativeInputClosed('native owner decision queue is full')
            self._pending[row['request_id']] = pending
        return pending

    def _remove(self, pending):
        with self._lock:
            if self._pending.get(pending.row['request_id']) is pending:
                self._pending.pop(pending.row['request_id'])

    def ask_sync(self, source_id, operation_id, native_id, method, detail,
                 validator, *, timeout_seconds=600):
        pending = self._prepare(source_id, operation_id, native_id, method,
                                detail, validator, timeout_seconds)
        try:
            try:
                return pending.future.result(timeout=timeout_seconds)
            except concurrent.futures.TimeoutError as error:
                raise NativeInputTimeout('native owner decision timed out') from error
        finally:
            self._remove(pending)

    async def ask_async(self, source_id, operation_id, native_id, method, detail,
                        validator, *, timeout_seconds=600):
        pending = self._prepare(source_id, operation_id, native_id, method,
                                detail, validator, timeout_seconds)
        try:
            try:
                return await asyncio.wait_for(asyncio.wrap_future(pending.future), timeout_seconds)
            except asyncio.TimeoutError as error:
                raise NativeInputTimeout('native owner decision timed out') from error
        finally:
            self._remove(pending)

    def list_requests(self):
        with self._lock:
            return _bounded([pending.row for pending in self._pending.values()
                             if not pending.future.done()], 2 * 1024 * 1024)

    def is_operation_open(self, source_id, operation_id):
        with self._lock:
            return not self._closed and (source_id, operation_id) not in self._closed_operations

    def answer(self, request_id, source_id, result, user_authorized=False, *, validation_result=False):
        if type(validation_result) is not bool:
            raise ValueError('validation_result must be boolean')
        if user_authorized is not True:
            raise OwnershipError('native response needs explicit originating-user action')
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.future.done() or pending.row['source_id'] != source_id:
                raise OwnershipError('native request is no longer pending in this source')
            if time.time() >= pending.row['expires_at']:
                raise OwnershipError('native request has expired')
            # A rejected value leaves the same request available for correction.
            try:
                validated = pending.validator(_bounded(result))
            except ValueError:
                if not validation_result:
                    raise
                if (self._closed or (source_id, pending.row['operation_id']) in self._closed_operations or
                        self._pending.get(request_id) is not pending or pending.future.done() or
                        time.time() >= pending.row['expires_at']):
                    raise OwnershipError('native request is no longer pending in this source') from None
                return _validation_rejected(request_id, source_id)
            pending.future.set_result(validated)
            self._pending.pop(request_id)
            return {'response_recorded': True,
                    'request_id': request_id, 'source_id': source_id,
                    'operation_id': pending.row['operation_id'],
                    'native_acknowledgement_verified': False,
                    'terminal_observation_required': True}

    def close_operation(self, source_id, operation_id):
        with self._lock:
            self._closed_operations.add((source_id, operation_id))
            for request_id, pending in list(self._pending.items()):
                if (pending.row['source_id'], pending.row['operation_id']) == (source_id, operation_id):
                    self._pending.pop(request_id)
                    if not pending.future.done():
                        pending.future.set_exception(NativeInputClosed('original native activity has ended'))

    def close(self):
        with self._lock:
            self._closed = True
            for pending in self._pending.values():
                if not pending.future.done():
                    pending.future.set_exception(NativeInputClosed('native owner decision service has closed'))
            self._pending.clear()

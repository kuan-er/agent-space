"""One native invocation per quick request, including after process failure.

The quick ledger is persisted before calling the provider. After that boundary
recovery may only observe its fixed operation ID. A missing provider journal is
uncertain, not permission to invoke again. Native completion and reply delivery
are separate checkpoints; neither implies verified business effects.
"""
from pathlib import Path
import threading
import time
import uuid

from anet_provider_harnesses.store import fingerprint
from anet_provider_harnesses.taskd_step import record_result
from .activity import execution
from .prompts import resource_rules
from .protocol import parse_message, status_line
from .store import atomic_json, read_json, utcnow


class DurableQuick:
    def __init__(self, quick):
        self.q = quick
        self.pending = set()
        self.retry_at = {}
        self.guard = threading.RLock()
        for path in quick.root.glob('*.json'):
            state = read_json(path, {})
            op = state.get('operation') or {}
            if op and not op.get('finished'):
                self.pending.add(state['id'])
                if not op.get('native_resolved'):
                    self._retain(state)

    def _save(self, state):
        atomic_json(self.q._path(state['id']), state)

    def _retain(self, state):
        op = state['operation']
        self.q.resources.retain('quick-' + state['id'], op['resource'], op['work_dir'])

    def _binding(self, work_dir):
        return fingerprint({'executor': self.q.config.get('executor'),
                            'adapter': self.q.executor.describe(), 'work_dir': str(work_dir)})

    @staticmethod
    def _inputs(messages):
        return [{k: m.get(k) for k in ('id', 'text', 'files', 'attachments')} for m in messages]

    def pending_for(self, tid):
        with self.guard:
            return tid in self.pending and time.monotonic() >= self.retry_at.get(tid, 0)

    def pending_ids(self):
        with self.guard:
            return set(self.pending)

    def recover(self):
        # This also recovers old operations outside ANet's latest-task page.
        with self.guard:
            tids = list(self.pending)
        for tid in tids:
            if self.pending_for(tid):
                self.q._spawn(tid)

    def _finish(self, state):
        op = state['operation']
        op['finished'] = True
        op['finished_at'] = utcnow()
        state['seen'] = list(dict.fromkeys(state['seen'] + op['message_ids']))[-200:]
        state['replies'] += 1
        self._save(state)
        self.q.resources.release_retained('quick-' + state['id'])
        with self.guard:
            self.pending.discard(state['id'])
            self.retry_at.pop(state['id'], None)

    def _hold(self, state, reason):
        op = state['operation']
        op.update(reason=reason, observed_at=utcnow())
        self._save(state)
        if not op.get('native_resolved'):
            self._retain(state)
        if (reason not in ('peer_unavailable', 'reply_delivery_unverified')
                and not op.get('requester_ended') and op.get('notice_reason') != reason):
            op['notice_reason'] = reason
            self._save(state)
            self.q._reply(state['id'],
                '正在核对同一次调用的结果；不会重复执行。\n\n' + status_line({
                    'task': state['id'], 'state': 'blocked', 'reason': reason,
                    'quick': {'operation_id': op['operation_id'], 'native_terminal_verified': False,
                              'automatic_redispatch': False}}), 'working')

    def _deliver(self, state, full):
        op = state['operation']
        if (full.get('id') != state['id'] or full.get('peer') != state.get('peer')
                or not self.q.accepts_peer(state.get('peer'))):
            self._hold(state, 'peer_unavailable')
            return
        if op.get('requester_ended') or full.get('state') in ('canceled', 'rejected'):
            op['reply_state'] = 'requester_ended'
            self._finish(state)
            return
        reply = op['reply']
        if op.get('reply_state') == 'attempted':
            # A reply may have committed before its ACK was lost. Inspect the
            # same thread; absence or failure never grants permission to resend.
            if ('reply_prior_message_ids' in op and full.get('state') == reply['state'] and any(
                    m.get('role') == 'provider' and m.get('text') == reply['text']
                    and isinstance(m.get('id'), str) and bool(m['id'])
                    and m['id'] not in op.get('reply_prior_message_ids', [])
                    for m in full['messages'])):
                op['reply_state'] = 'observed'
                self._finish(state)
            else:
                self._hold(state, 'reply_delivery_unverified')
            return
        if op.get('reply_state') == 'acknowledged':
            self._finish(state)
            return
        op['reply_state'] = 'attempted'
        op['reply_prior_message_ids'] = [m['id'] for m in full['messages'] if m.get('id')]
        self._save(state)
        try:
            self.q.transport.reply(state['id'], reply['text'], reply['state'])
        except Exception:
            self._hold(state, 'reply_delivery_unverified')
            return
        op['reply_state'] = 'acknowledged'
        self._save(state)
        self._finish(state)

    def answer(self, tid):
        q = self.q
        with self.guard:
            self.retry_at[tid] = time.monotonic() + 15
        state = read_json(q._path(tid), None)
        if state is None:
            return
        full = q.transport.get(tid)
        # A revoked peer cannot start, observe or obtain another result.
        if (full.get('id') != tid or full.get('peer') != state.get('peer')
                or not q.accepts_peer(state.get('peer'))):
            if state.get('operation') and not state['operation'].get('finished'):
                self._hold(state, 'peer_unavailable')
            return
        op = state.get('operation') or {}
        if op.get('finished'):
            op = {}
        new = [m for m in full['messages'] if m['role'] == 'requester' and m['id'] not in state['seen']]
        if not op:
            if not new or full.get('state') in ('completed', 'failed', 'canceled', 'rejected'):
                return
            _, payload = parse_message(new[-1]['text'])
            resource = payload.get('resource', 'background') if isinstance(payload, dict) else 'background'
            if resource not in ('background', 'desktop'):
                return
            work_dir = Path(state.get('work_dir') or q.work_dir)
            prompt, sample = q._prompt(full['messages'], tid)
            if any(m.get('files') for m in new) and not (q.inputs_root / q._path(tid).stem).is_dir():
                prompt += '\n\nFiles from the other side are in ' + str(q.inputs_root / q._path(tid).stem)
            op = {'schema': 'taskd-durable-quick/1', 'operation_id': str(uuid.uuid4()),
                  'message_ids': [m['id'] for m in new], 'input_sha256': fingerprint(self._inputs(new)),
                  'prompt': prompt + '\n\n' + resource_rules(resource), 'sample': sample,
                  'work_dir': str(work_dir), 'resource': resource, 'binding': self._binding(work_dir),
                  'state': 'reserved', 'created_at': utcnow(), 'native_resolved': False}
            state['operation'] = op
            self._save(state)
            with self.guard:
                self.pending.add(tid)
        original = [m for m in full['messages'] if m.get('role') == 'requester' and m.get('id') in op['message_ids']]
        if fingerprint(self._inputs(original)) != op['input_sha256']:
            self._hold(state, 'original_input_differs_or_missing')
            return
        if self._binding(Path(op['work_dir'])) != op['binding']:
            self._hold(state, 'executor_or_workspace_changed')
            return
        cancel = q.cancel_events[tid]
        if full.get('state') in ('failed', 'canceled', 'rejected'):
            op['requester_ended'] = True
            cancel.set()
        if op.get('native_resolved'):
            self._deliver(state, full)
            return
        if op['state'] == 'reserved' and (cancel.is_set() or q.stopping.is_set()):
            if cancel.is_set():
                op.update(native_resolved=True, state='not_dispatched', reply_state='requester_ended')
                self._finish(state)
            return
        work_dir = Path(op['work_dir'])
        # On cancellation we still observe an already dispatched call. A set
        # cancellation event must not prevent acquiring its original lease.
        lease_cancel = cancel if op['state'] == 'reserved' else None
        with q.resources.lease('quick-' + tid, op['resource'], work_dir,
                               cancel=lease_cancel, stopping=q.stopping) as acquired:
            if not acquired:
                return
            if q.stopping.is_set():
                return
            current = q.transport.get(tid)
            if (current.get('id') != tid or current.get('peer') != state.get('peer')
                    or not q.accepts_peer(state.get('peer'))):
                self._hold(state, 'peer_unavailable')
                return
            if current.get('state') in ('failed', 'canceled', 'rejected'):
                op['requester_ended'] = True
                cancel.set()
                if op['state'] == 'reserved':
                    op.update(native_resolved=True, state='not_dispatched', reply_state='requester_ended')
                    self._finish(state)
                    return
            with q.lock:
                q.executing.add(tid)
            initial_call = op['state'] == 'reserved'
            if op['state'] == 'reserved':
                if state['replies'] >= 30:
                    op.update(native_resolved=True, state='not_dispatched',
                              reply={'state': 'failed', 'text': 'Quick reply limit reached; start a new task.'})
                    self._save(state)
                    self._deliver(state, full)
                    return
                if any(m.get('files') for m in original):
                    try:
                        q.transport.pull(tid, q.inputs_root / q._path(tid).stem)
                    except Exception:
                        self._hold(state, 'input_files_unavailable')
                        return
                # File retrieval and slot waits may outlive the original ACL
                # check. Inspect again at the last pre-dispatch boundary.
                current = q.transport.get(tid)
                if (current.get('id') != tid or current.get('peer') != state.get('peer')
                        or not q.accepts_peer(state.get('peer'))):
                    self._hold(state, 'peer_unavailable')
                    return
                current_inputs = [m for m in current['messages'] if m.get('role') == 'requester'
                                  and m.get('id') in op['message_ids']]
                if fingerprint(self._inputs(current_inputs)) != op['input_sha256']:
                    self._hold(state, 'original_input_differs_or_missing')
                    return
                if current.get('state') in ('failed', 'canceled', 'rejected') or cancel.is_set():
                    op.update(native_resolved=True, state='not_dispatched', reply_state='requester_ended')
                    self._finish(state)
                    return
                # Everything after this durable boundary is observation-only on
                # restart, even a crash immediately before the runner call.
                op['state'] = 'dispatching'
                self._save(state)
                self._retain(state)
                invoke = lambda: q.executor.run(op['prompt'], work_dir=work_dir, timeout=q.timeout,
                    mode='task', cancel=cancel, operation_id=op['operation_id'])
            else:
                invoke = lambda: q.executor.observe(op['operation_id'], work_dir=work_dir,
                    timeout=min(q.timeout, 10), cancel=cancel)
            try:
                with execution(q.activity, 'quick-' + tid, q.timeout):
                    result = invoke()
            except Exception:
                op['state'] = 'effect_unverified'
                self._hold(state, 'provider_observation_unavailable')
                return
            evidence = getattr(result, 'harness_evidence', None) or {}
            # Require the exact journal and native terminal evidence. r.ok alone
            # is insufficient (e.g. WorkBuddy's merely correlated local reply).
            verified = (evidence.get('operation_id') == op['operation_id']
                        and evidence.get('native_terminal_verified') is True)
            not_dispatched = (initial_call and evidence.get('operation_id') == op['operation_id']
                and evidence.get('state') == 'not_dispatched' and evidence.get('dispatch_attempted') is False
                and evidence.get('original_message_must_not_be_repeated') is False)
            if not verified and not not_dispatched:
                op['state'] = 'effect_unverified'
                op['last_observation'] = {k: evidence.get(k) for k in
                    ('state', 'timed_out', 'canceled', 'native_terminal_verified', 'native_cancel_confirmed')}
                self._hold(state, 'native_effect_unverified')
                return
            op.update(state='result_checkpointed', native_resolved=True, result=record_result(result))
            text = (result.text or '').strip()
            success = verified and result.ok and bool(text) and evidence.get('state') == 'completed'
            reply_text = text if success else 'This invocation ended without a verified final answer.'
            op['reply'] = {'state': 'completed' if success else 'failed', 'text': reply_text}
            self._save(state)
            q.resources.release_retained('quick-' + tid)
            if cancel.is_set():
                op['requester_ended'] = True
            if q.stopping.is_set():
                return
            self._deliver(state, q.transport.get(tid))

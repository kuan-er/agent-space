"""The task engine: intake, planning, steps, progress, checks, reviews, replies.

It knows nothing of the transport, the executor or the platform it runs on: those are the objects it
is given (design §5, §6). Every decision about progress and completion is made here, by program; the
reviewer only advises (design §13, §14).
"""
from __future__ import annotations

import fnmatch
from copy import deepcopy
import hashlib
import json
import logging
from pathlib import Path
import threading
import time

from . import messages, prompts, lightweight, native
from . import milestones, delivery_receipts, facts
from .maintenance import AdmissionGate
from .checkers.base import CheckContext
from .checkers.generic import sha256_file
from .protocol import (ACTIVE, FINAL, PLAN_SCHEMA, PROTOCOL, REPORT_SCHEMA, REVIEW_SCHEMA, WAITING,
                       Rejected, a2a_state, conforms, extract_json, normalize_control,
                       parse_message, schema_instructions, status_line, CHECK_ID)
from .store import atomic_json, read_json, utcnow
from .activity import execution
from .resources import Resources, capacity, resource_for, workspace, DESKTOP_CHECKS, desktop_enabled
from .recovery import prepare_revision, blocker, plan_error_code
from .transports.anet import ReplyTooLarge

POLICY = {'no_progress_steps': 3, 'max_replans': 2, 'review_every': 5, 'keepalive_seconds': 600,
          'same_plan_error_limit': 2,
          'progress_note_seconds': 1800, 'max_plan_revisions': 3,
          'max_verify_rounds': 3, 'probe_retry_seconds': 60, 'probe_max_wait_minutes': 30,
          'max_exec_errors': 3, 'max_reviews': 40, 'max_phases': 16, 'review_images': 4}
NOISE = ('*.log', '*.tmp', '*.lock', '*.pid', '.DS_Store')
SEEN_LIMIT = 4000


def _hash_obj(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]


class Engine:
    def __init__(self, *, config, store, outbox, executor, checkers, reviewer=None, probes=(),
                 capabilities=None, log=None, clock=time.time, artifacts=None):
        self.config = config
        self.policy = dict(POLICY, **config.get('policy', {}))
        self.store, self.outbox, self.executor, self.reviewer = store, outbox, executor, reviewer
        self.checkers, self.probes = checkers, list(probes)
        self.capabilities = capabilities or {}
        self.artifacts = artifacts
        if artifacts:artifacts.bind_engine(self)
        self.log = log or logging.getLogger('taskd')
        self.clock = clock
        self.work_dir = Path(config['work_dir']).resolve()
        self.read_paths = config.get('read_paths', [])
        self.noise = tuple(config.get('noise_patterns', NOISE))
        self.workers, self.cancel_events = {}, {}
        self.guard = threading.Lock()
        self.admission = AdmissionGate()
        self.stopping = threading.Event()
        self.provider_consumers = set()
        self.resources = Resources(capacity(config))
        self.owner_inputs=None
        owner_config=config.get('provider_owner_input',{'enabled':False})
        if (not isinstance(owner_config,dict) or set(owner_config)-{'enabled'} or type(owner_config.get('enabled',False)) is not bool):
            raise ValueError('invalid provider local owner input configuration')
        if owner_config.get('enabled') is True:
            if not getattr(executor,'owner_input_supported',False):raise ValueError('local owner input requires an SDK provider executor')
            from .owner_input import OwnerInputBroker
            self.owner_inputs=OwnerInputBroker(store.root.parent,self._notify_owner_input)

    def task_work_dir(self, task):
        # Persisted legacy tasks retain their original directory and share a workspace lock.
        return Path(task.get('work_dir') or self.work_dir)

    # ------------------------------------------------------------------ messaging

    def _owner_context(self,task_id,operation_id):
        if self.owner_inputs is None:return None
        with self.store.lock(task_id):
            initial=self.store.load(task_id);peer=initial.get('peer')
        def active():
            if self.stopping.is_set():return False
            with self.store.lock(task_id):
                task=self.store.load(task_id) or {}
                return (task.get('peer')==peer and task.get('state') not in FINAL and not task.get('flags',{}).get('cancel')
                    and (task.get('provider_step_intent') or {}).get('operation_id')==operation_id)
        return self.owner_inputs.context(task_id,operation_id,self.executor.runner.harness,active,requester_aid=peer)

    def _notify_owner_input(self,task_id,operation_id,notice):
        with self.store.lock(task_id):
            task=self.store.load(task_id)
            if not task or (task.get('provider_step_intent') or {}).get('operation_id')!=operation_id:return
            # This is a bounded notice, never the native question/tool parameters.
            self.send(task,'Windows execution reported a request for its LOCAL owner. Remote answer/hint cannot approve it. '
                'The Windows operator can inspect taskd owner-input list/show; current local pending state is authoritative.',
                state='working',owner_input=notice)
            self.store.log(task_id,'provider_local_owner_notice',notice_id=notice['notice_id'],harness=notice['harness'],request_type=notice['request_type'])
            self.store.save(task)

    def _envelope(self, task, **extra):
        plan = task.get('plan') or {'phases': []}
        idx = task.get('phase_index', 0)
        phases = plan['phases']
        env = {'task': task['id'], 'state': task['state'],
               'orchestration': task['spec'].get('orchestration', 'planned'),
               'routing_reason': task['spec'].get('routing_reason'), 'native': facts.native_view(task.get('native')),
               'a2a_state': a2a_state(task['state'], task.get('stop_reason')),
               'phase': ({'index': idx + 1, 'total': len(phases), 'id': phases[idx]['id'],
                          'title': phases[idx]['title']} if phases and idx < len(phases) else None),
               'used': task['used'], 'scheduling': facts.scheduling_view(self.resources.status(task['id']), task['state']),
               'last_note': task.get('last_note'), 'last_step': task.get('last_step'),
               'block_reason': task.get('block_reason'), 'blocker': task.get('blocker'),
               'plan_revision': task.get('plan_revision', 0),
               'revision_pending': bool(task.get('pending_revision')),
               'heartbeat_at': task.get('heartbeat_at')}
        env['delivery'] = facts.delivery_view(task.get('delivery'), task['state'])
        env['provider'] = self._provider_view(task)
        if task.get('external_delivery') is not None:
            env['external_delivery'] = task['external_delivery']
        if task.get('file_delivery') is not None:env['file_delivery']=task['file_delivery']
        env.update(extra)
        return env

    def send(self, task, text, *, state=None, files=None, status_only=False, _delivery_transport=None, **extra):
        """Send text plus the status envelope. state is the A2A state, derived from the task otherwise.
        status_only sends the state alone (working without a message is a status, not an answer)."""
        a2a = state or a2a_state(task['state'], task.get('stop_reason'))
        reply_transport=_delivery_transport or self.outbox
        if files:
            extra['attachments'] = [{'name': Path(p).name, 'size': Path(p).stat().st_size,
                                     'sha256': sha256_file(p)} for p in files]
        elif task.get('undelivered_attachments') and a2a=='completed':
            extra['attachments']=task['undelivered_attachments']
            text='任务执行已完成，但结果文件未交付；文件仍保留在执行端，本条仅为摘要。\n'+(text or '')
        extra['execution'] = {'state': task['state'], 'at': utcnow(), 'error': task.get('block_reason') or task.get('stop_reason')}
        extra['delivery'] = facts.delivery_view(task.get('delivery'), task['state'], sending=True, at=extra['execution']['at'])
        if status_only:
            task['heartbeat_at'] = utcnow()
            extra['heartbeat'] = True
        elif text and text.strip():
            task['last_note'] = {'text': text.strip()[:2000], 'at': utcnow()}
        body = (text.strip() + '\n\n' if text and not status_only else '') + status_line(self._envelope(task, **extra))
        if self.artifacts and a2a == 'completed':
            body = self.artifacts.record_terminal_body(task, body, files, a2a)
        try:
            if self.artifacts and a2a == 'completed':self.artifacts.outbox.before_invoke(task,body)
            reply_result = reply_transport.reply(task['id'], body, a2a, files=files)
        except Exception as e:
            if self.artifacts and a2a == 'completed':
                self.artifacts.outbox.outcome(task,local_refusal=isinstance(e,ReplyTooLarge))
            self.log.warning('reply to %s failed: %s', task['id'], e)
            self.store.log(task['id'], 'reply_failed', error=str(e)[:300], state=a2a)
            task['delivery_state'] = 'unconfirmed'
            delivery_receipts.record(self.store, task, 'unconfirmed', files, reason='reply_failed')
            # A local size refusal sent nothing. A remote size refusal is explicit,
            # but check the task before a second reply in case delivery raced it.
            oversized = isinstance(e, ReplyTooLarge) or 'body: http: request body too large' in str(e)
            if oversized and a2a in ('completed', 'failed', 'canceled', 'rejected'):
                if not isinstance(e, ReplyTooLarge):
                    try:
                        remote_state = reply_transport.get(task['id'], history=0)['state']
                        if remote_state == a2a:
                            task['delivery_state'] = 'confirmed'
                            delivery_receipts.record(self.store, task, 'remote_terminal_observed', files,
                                                     reason='terminal_state_does_not_prove_file_delivery')
                            return True
                        if remote_state in ('completed', 'failed', 'canceled', 'rejected'):
                            task['delivery_state'] = 'terminal_conflict'
                            return False
                    except Exception as query_error:
                        self.store.log(task['id'], 'reply_status_unknown', error=str(query_error)[:300])
                        return False
                brief = ('Task reached local %s state. Large attachments were not transmitted; '
                         'see the task directory delivery-manifest.json and report.json on the executor.' % task['state'])
                if task.get('external_delivery'):
                    brief = ('Task reached local %s state. The summary is sent separately; '
                             'external attachment delivery remains pending verification.' % task['state'])
                compact = status_line({'task': task['id'], 'state': task['state'], 'a2a_state': a2a,
                                       'delivery': 'local_artifacts_not_transmitted',
                                       'external_delivery': task.get('external_delivery')})
                if task.get('undelivered_attachments'):
                    compact=status_line({'task':task['id'],'state':task['state'],'a2a_state':a2a,
                        'file_delivery':task.get('file_delivery'),'attachments':task['undelivered_attachments']})
                try:
                    fallback_body = brief + '\n\n' + compact
                    if self.artifacts and a2a == 'completed':
                        fallback_body = self.artifacts.record_terminal_body(task, fallback_body, None, a2a)
                        self.artifacts.outbox.before_invoke(task,fallback_body)
                    fallback_result = reply_transport.reply(task['id'], fallback_body, a2a, files=None)
                except Exception as fallback_error:
                    if self.artifacts and a2a == 'completed':self.artifacts.outbox.outcome(task)
                    self.store.log(task['id'], 'reply_fallback_failed', error=str(fallback_error)[:300])
                    return False
                task['delivery_state'] = 'confirmed'
                if self.artifacts and a2a == 'completed':self.artifacts.outbox.outcome(task,acknowledged=True)
                delivery_receipts.record(self.store, task, 'acknowledged', [], result=fallback_result,
                                         reason='summary_only_artifacts_retained_locally')
                self.store.log(task['id'], 'reply_fallback', state=a2a)
            else:
                return False
        else:
            if self.artifacts and a2a == 'completed':self.artifacts.outbox.outcome(task,acknowledged=True)
            task['delivery_state'] = 'confirmed'
            delivery_receipts.record(self.store, task, 'acknowledged', files, result=reply_result)
        task['last_sent_ts'] = self.clock()
        if not status_only:
            task['last_note_ts'] = task['last_sent_ts']
            task['messages_sent'] = task.get('messages_sent', 0) + 1
        self.store.log(task['id'], 'reply', state=a2a, text=(text or '')[:2000], status_only=status_only)
        return True

    def _goal(self, task):
        return task['spec']['goal']

    # ------------------------------------------------------------------ intake

    def accepts_peer(self, peer):
        allowed = self.config.get('accept_peers', 'admitted')
        return allowed == 'admitted' or peer in (allowed or [])

    def ended_by_peer(self, task_id, state):
        return self.admission.run(self._ended_by_peer, task_id, state)

    def _ended_by_peer(self, task_id, state):
        """The requester ended the task on its side (canceled it, or it failed there)."""
        with self.store.lock(task_id):
            t = self.store.load(task_id)
            if not t or t['state'] in FINAL:
                return
            t['flags']['cancel'] = True
            ev = self.cancel_events.get(task_id)
            if ev:
                ev.set()
            t['state'], t['stop_reason'] = 'stopped', 'ended_by_requester'
            self.store.log(task_id, 'stopped', reason='ended_by_requester', remote_state=state)
            atomic_json(self.store.task_dir(task_id) / 'report.json', self.report(t))
            self.store.save(t)

    def reconcile_native_stop(self, task_id, peer, request_id):
        return self.admission.run(self._reconcile_native_stop, task_id, peer, request_id)

    def _reconcile_native_stop(self, task_id, peer, request_id):
        """Queue stop-only reconciliation for an already closed transport task."""
        with self.store.lock(task_id):
            task = self.store.load(task_id)
            if not self.accepts_peer(peer) or not task or task.get('peer') != peer:
                return 'task_unavailable'
            if not native.is_native(task) or task.get('stop_reason') != 'ended_by_requester':
                return 'not_eligible'
            if task.get('native_reconcile_request') == request_id or task.get('state') in ACTIVE:
                return 'already_requested'
            if not task.get('native_quarantine'):
                return 'no_quarantine'
            if not task.get('session'):
                return 'missing_native_session'
            if not callable(getattr(self.executor, 'run_goal', None)):
                return 'native_executor_unavailable'
            task['native_reconcile_request'] = request_id
            task['native_cleanup_only'] = True
            task['native_pause'] = False
            task['flags']['cancel'] = False
            task['state'] = 'running'
            self.store.save(task)
            self.store.log(task_id, 'native_stop_reconcile_queued', request_id=request_id)
        self.schedule()
        return 'queued'

    def intake_new(self, task_id, peer, message_id, text, inputs_dir=None):
        return self.admission.run(self._intake_new, task_id, peer, message_id, text, inputs_dir)

    def _intake_new(self, task_id, peer, message_id, text, inputs_dir=None):
        """A task this node has not seen. Returns the record, or None when it was rejected."""
        with self.store.lock(task_id):
            if self.store.load(task_id):
                return self.store.load(task_id)
            kind, payload = parse_message(text)
            try:
                if not self.accepts_peer(peer):
                    raise Rejected('peer_not_accepted', 'this node does not take tasks from this peer')
                if kind == 'control':
                    raise Rejected('no_task', 'the first message of a task must be the task itself')
                spec = lightweight.normalize(payload, self.config, peer, self.checkers)
                if (getattr(self.executor,'durable_step_required',False)
                        and spec.get('orchestration','planned') != 'planned'):
                    raise Rejected('provider_planned_only','this provider supports planned taskd submissions only')
                native.select(spec, self.executor, self.config)
                previous = self._previous_summary(spec.get('continue_from'), peer) if spec.get('continue_from') else None
            except Rejected as r:
                goal = payload.get('goal', '') if isinstance(payload, dict) else payload
                body = messages.say('rejected', goal if isinstance(goal, str) else '', reason=r.message)
                try:
                    self.outbox.reply(task_id, body + '\n\n' + status_line(
                        {'task': task_id, 'state': 'rejected', 'a2a_state': 'rejected', 'reason': r.reason}), 'rejected')
                except Exception as e:
                    self.log.warning('reject reply to %s failed: %s', task_id, e)
                self.log.info('rejected %s from %s: %s', task_id, peer, r.reason)
                return None
            task = {
                'id': task_id, 'protocol': PROTOCOL, 'peer': peer, 'created_at': utcnow(),
                'started_ts': self.clock(), 'state': 'accepted', 'stop_reason': None, 'spec': spec,
                'checks': [c for c in spec['acceptance'] if c['type'] != 'natural'],
                'previous': previous, 'plan': None, 'phase_index': 0, 'session': None,
                'sessions_used': 0, 'used': {'steps': 0, 'seconds': 0.0, 'reviews': 0, 'replans': 0,
                                             'verify_rounds': 0},
                'progress': {'no_progress': 0, 'seen': [], 'check_state': {}, 'exec_errors': 0},
                'inbox': {'hints': [], 'answers': [], 'feedback': []},
                'flags': {'fresh': False, 'cancel': False, 'interrupted': False},
                'question': None, 'block_reason': None, 'seen_messages': [message_id] if message_id else [],
                'last_sent_ts': 0, 'messages_sent': 0, 'step_in_progress': None, 'result': None,
                'approval_reasons': [], 'inputs_dir': str(inputs_dir) if inputs_dir else None,
                'work_dir': str(workspace(self.work_dir, task_id, self.config.get('isolate_tasks', True))),
            }
            self.store.save(task)
            self.store.log(task_id, 'accepted', peer=peer, spec=spec)
            self._baselines(task, task['checks'])
            ahead = len([t for t in self._active_ids() if t != task_id])
            text = (messages.say('queued', spec['goal'], task=task_id, n=ahead) if self._concurrency() is not None and ahead >= self._concurrency()
                    else messages.say('accepted', spec['goal'], task=task_id))
            self.send(task, text)
            self.store.save(task)
        self.schedule()
        return task

    def intake_message(self, task_id, message_id, text):
        return self.admission.run(self._intake_message, task_id, message_id, text)

    def _intake_message(self, task_id, message_id, text):
        """A later message from the requester on a known task."""
        reply_status = False
        with self.store.lock(task_id):
            task = self.store.load(task_id)
            if not task or message_id in task.get('seen_messages', []):
                return
            task['seen_messages'] = (task.get('seen_messages', []) + [message_id])[-500:]
            goal = self._goal(task)
            kind, payload = parse_message(text)
            if kind == 'control':
                try:
                    c = normalize_control(payload)
                except Rejected as r:
                    self.store.save(task)
                    self.send(task, r.message)
                    self.store.save(task)
                    return
            else:
                # Plain text answers a pending question, and is a hint otherwise.
                c = {'kind': 'answer' if task['state'] in WAITING else 'hint', 'text': payload if isinstance(payload, str) else json.dumps(payload)}
            self.store.log(task_id, 'requester_message', kind=c['kind'], text=(c.get('text') or '')[:2000])
            if task['state'] in FINAL:
                self.store.save(task)
                return
            k = c['kind']
            if task.get('provider_step_intent'):
                if k == 'reconcile':
                    if not callable(getattr(self.executor,'observe',None)):
                        self.send(task,'Provider has no read-only observation interface; original intent retained.')
                    else:
                        task['provider_reconcile_requested'] = True
                        task['state'] = 'running'
                        self.store.save(task)
                        self.send(task,'Read-only reconciliation queued for the SAME provider operation; no task was dispatched.')
                    self.store.save(task)
                    self.schedule()
                    return
                if k in ('hint','answer'):
                    task.setdefault('provider_deferred_controls',[]).append({'kind':k,'text':c.get('text','')})
                    self.send(task,'Guidance saved for after the original provider result is consumed; use reconcile to observe it.')
                    self.store.save(task)
                    return
                if k in ('revise_plan','resume'):
                    self.send(task,'Provider intent remains unresolved; reconcile the original operation before changing its context.')
                    self.store.save(task)
                    return
            if native.is_native(task) and task.get('native_quarantine') and k in ('resume', 'hint', 'answer'):
                self.store.save(task)
                self.send(task, 'Stop outcome is unknown; use {"taskd":"reconcile"} to confirm cleanup before resume. No goal was activated.')
                self.store.save(task)
                return
            if k == 'reconcile':
                if (not native.is_native(task) or not task.get('session') or
                        task['state'] not in ('blocked', 'needs_user') or task_id in self.workers):
                    self.send(task, 'Reconcile requires a stopped native task with its existing session and no active worker.')
                else:
                    task['native_cleanup_only'] = True
                    task['native_reconcile_only'] = True
                    task['native_pause'] = True
                    task['flags']['cancel'] = False
                    task['state'] = 'running'
                self.store.save(task)
                self.schedule()
                return
            if k in ('pause', 'resume'):
                if not native.is_native(task):
                    self.send(task, 'pause/resume requires a native goal task; use hint/answer or cancel for this task.')
                elif k == 'pause':
                    task['native_pause'] = True
                    ev = self.cancel_events.get(task_id)
                    if ev and task_id in self.workers:
                        ev.set()
                    else:
                        self._block(task, 'Native goal paused by requester', kind='native_paused')
                elif task['state'] in ('blocked', 'needs_user'):
                    self._resume_verification(task)
                    task['native_pause'] = False
                    task['state'] = 'running' if task.get('plan') else 'accepted'
                    task['block_reason'] = task['blocker'] = task['block_kind'] = None
                self.store.save(task)
                self.schedule()
                return
            if native.is_native(task) and k == 'revise_plan':
                self.send(task, '原生目标不使用阶段计划；请直接 hint/answer，或 pause/resume。')
                self.store.save(task)
                return
            if native.is_native(task) and k in ('hint', 'answer'):
                task['native_pause'] = False
            if lightweight.is_light(task) and k == 'revise_plan':
                self.send(task, '轻量任务不使用阶段修订；用 hint/answer 补充说明，或为复杂工作提交长任务。')
                self.store.save(task)
                return
            if lightweight.is_light(task) and k in ('hint', 'answer') and task['state'] in ('blocked', 'needs_user'):
                # Explicit guidance permits another bounded model call, never an automatic script rerun.
                task['light_model_base'] = task.get('light_model_calls', 0)
                task['light_resume_authorized'] = True
            if k == 'revise_plan':
                try:
                    proposal = prepare_revision(task, c, self._validate_plan, self.policy['max_plan_revisions'], self.checkers)
                    if proposal:
                        task['pending_revision'] = proposal
                        task.setdefault('revision_requests', {})[c['request_id']] = {
                            'sha256': proposal['sha256'], 'state': 'queued'}
                        task['state'] = 'running'
                        self.store.log(task_id, 'revision_queued', request_id=c['request_id'], reason=c['reason'])
                    self.send(task, '计划修订已排队，将在当前步骤结束并释放资源后应用。' if proposal else '该计划修订已接收，不会重复应用。')
                except ValueError as error:
                    self.send(task, '计划修订未接受：' + str(error), revision_error=str(error))
            elif k == 'cancel':
                task.pop('native_reconcile_only', None)
                if native.is_native(task):task['native_pause'] = False
                if native.is_native(task) and task.get('native_quarantine'):
                    # Reconcile and stop the existing thread before claiming cancellation.
                    task['native_cleanup_only'] = True
                    task['native_pause'] = False
                    task['flags']['cancel'] = False
                    task['state'] = 'running'
                    self.store.save(task)
                    self.schedule()
                    return
                task['flags']['cancel'] = True
                ev = self.cancel_events.get(task_id)
                if ev:
                    ev.set()
                if task['state'] not in ACTIVE or task_id not in self.workers:
                    self._stop(task, 'canceled')
            elif k == 'capabilities':
                self.store.save(task)
                self.send(task, json.dumps(self.capabilities, ensure_ascii=False), capabilities=self.capabilities)
            elif k == 'status':
                reply_status = True
            elif k == 'approve_plan':
                if task['state'] == 'awaiting_approval':
                    task['state'] = 'running'
                    self.send(task, messages.say('answer_taken', goal))
                else:
                    reply_status = True
            elif k in ('answer', 'hint') and task['state'] == 'blocked' and task.get('block_kind') in (
                    'stalled', 'checker_error', 'resource_mismatch', 'planning_error'):
                task['inbox']['hints'].append(c['text'])
                self._resume_orchestration(task)
            elif k == 'answer':
                if task['state'] in WAITING:
                    task['inbox']['answers'].append(c['text'])
                    if task['state'] == 'awaiting_approval':
                        task['inbox']['hints'].append(c['text'])
                    self._resume_verification(task)
                    task['state'] = 'running' if task.get('plan') else 'planning'
                    task['question'] = task['block_reason'] = None
                    task['blocker'] = None
                    task['block_kind'] = None
                    task['progress']['no_progress'] = 0
                    self.send(task, messages.say('answer_taken', goal))
                else:
                    task['inbox']['hints'].append(c['text'])
                    self.send(task, messages.say('nothing_to_answer', goal))
            elif k == 'hint':
                task['inbox']['hints'].append(c['text'])
                if task['state'] == 'blocked':
                    self._resume_verification(task)
                    task['state'] = 'running' if task.get('plan') else 'planning'
                    task['block_reason'] = None
                    task['blocker'] = None
                    task['block_kind'] = None
                    task['progress']['no_progress'] = 0
                self.send(task, messages.say('hint_taken', goal))
            self.store.save(task)
            if reply_status:
                if task['state'] == 'blocked':
                    self._send_blocked_report(task)
                else:
                    self.send(task, messages.say('status', goal, state=task['state'], detail=self._detail(task)), plan=task.get('plan'), checks=task['checks'])
                self.store.save(task)
        self.schedule()

    def _resume_orchestration(self, t):
        """An explicit follow-up permits one bounded retry, not a budget reset."""
        kind, reason = t.get('block_kind'), t.get('block_reason')
        if kind == 'resource_mismatch':
            phase = self._phase(t)
            if not phase or t['spec'].get('resource') == 'background' or not desktop_enabled(self.config):
                self.send(t, '继续提示已保存；当前任务资源约定或本机配置不允许桌面访问，不能自动放宽权限。')
                return
            if not self._promote_desktop(t, reason):
                self.send(t, '继续提示已保存；没有可重新调度的未完成阶段，请检查当前阻塞详情。')
                return
        elif kind == 'planning_error':
            t['state'] = 'planning'
            t['replan_context'] = self.checkpoint(t)
            # Preserve cumulative errors, but grant a fresh bounded validation window.
            t['progress']['plan_error_baseline'] = dict(t['progress'].get('plan_errors', {}))
        else:
            t['state'] = 'running' if t.get('plan') else 'planning'
            if kind == 'checker_error':
                t['retry_system_checks'] = True
        t['inbox']['feedback'].append('Retry after %s: %s. Use the new requester guidance; '
                                      'do not repeat a ruled-out approach.' % (kind, reason))
        t['progress']['no_progress'] = 0
        t['question'] = t['block_reason'] = t['blocker'] = t['block_kind'] = None
        t['flags']['fresh'] = True
        self.store.log(t['id'], 'orchestration_resumed', kind=kind, reason=reason, used=deepcopy(t['used']))
        self.send(t, '已按补充说明恢复一次有界执行；累计预算、已完成阶段和原始验收条件保持不变。')

    def _promote_desktop(self, t, reason):
        """Schedule the next step under a real lease; never operate GUI in this step."""
        phase = self._phase(t)
        if (not phase or phase.get('status') == 'done' or t['spec'].get('resource') == 'background'
                or not desktop_enabled(self.config)):
            return False
        before = phase.get('resource')
        phase.update(resource='desktop', requires_desktop=True)
        t['state'] = 'running'
        if before != 'desktop':
            t['plan_revision'] = t.get('plan_revision', 0) + 1
            self.store.log(t['id'], 'resource_rescheduled', phase=phase['id'], before=before,
                           resource='desktop', reason=reason, revision=t['plan_revision'])
        t['inbox']['feedback'].append('Taskd will acquire the desktop lease before your next step. '
                                      'Continue the same phase, including edits, compilation and GUI retesting.')
        self.store.save(t)
        return True

    def _detail(self, task):
        if task['state'] in ('needs_user', 'awaiting_approval'):
            return task.get('question') or ''
        if task['state'] == 'blocked':
            return task.get('block_reason') or ''
        last = self.store.read_log(task['id'], 'step')
        return last[-1]['report'].get('summary', '') if last and last[-1].get('report') else ''

    def _previous_summary(self, prior_id, peer):
        prior = self.store.load(prior_id)
        if not prior or prior.get('peer') != peer:
            raise Rejected('continue_from_unavailable', 'continue_from must name an earlier task of the same requester on this node')
        return self.checkpoint(prior, limit=8000)

    # ------------------------------------------------------------------ scheduling

    def _concurrency(self):
        return self.resources.capacity

    def _active_ids(self):
        out = []
        for tid in self.store.ids():
            t = self.store.load(tid)
            if t and t['state'] in ACTIVE:
                out.append((t['created_at'], tid))
        return [tid for _, tid in sorted(out)]

    def schedule(self):
        return self.admission.run(self._schedule)

    def _schedule(self):
        if self.stopping.is_set():
            return
        with self.guard:
            for tid in list(self.workers):
                if not self.workers[tid].is_alive():
                    del self.workers[tid]
            for tid in self._active_ids():
                if tid in self.workers:
                    continue
                ev = threading.Event()
                self.cancel_events[tid] = ev
                th = threading.Thread(target=self._work, args=(tid,), name='task-' + tid[:16], daemon=True)
                self.workers[tid] = th
                th.start()

    def recover(self):
        return self.admission.run(self._recover)

    def _recover(self):
        """At start: a step that was running when the process ended is marked interrupted."""
        for tid in self.store.ids():
            with self.store.lock(tid):
                t = self.store.load(tid)
                if t and t.get('provider_step_intent'):
                    # A planned provider effect survives independently of the
                    # engine checkpoint. Never let a new planning prompt mint a
                    # replacement operation after restart.
                    t['flags']['interrupted'] = True
                    self._retain_provider(t)
                    self.store.log(tid, 'provider_interrupted', intent=t['provider_step_intent'])
                    t['step_in_progress'] = None
                    self.store.save(t)
                    self._block(t, 'Provider step interrupted; inspect or reconcile the SAME operation before continuing',
                                kind='provider_incomplete')
                    continue
                if t and native.is_native(t) and (t.get('native_quarantine') or t.get('step_in_progress')):
                    t['native_quarantine'] = True
                    self.resources.retain(tid, t['spec']['resource'], self.task_work_dir(t))
                    self.store.save(t)
                if t and t.get('step_in_progress'):
                    t['flags']['interrupted'] = True
                    self.store.log(tid, 'interrupted', step=t['step_in_progress'])
                    t['step_in_progress'] = None
                    self.store.save(t)
                    if lightweight.is_light(t) or native.is_native(t):
                        self._block(t, 'Execution interrupted; inspect effects before explicitly continuing',
                                    kind='native_incomplete' if native.is_native(t) else 'routine_interrupted' if t.get('routine_state') == 'started' else 'direct_incomplete')
        self.schedule()

    def keepalive(self):
        """Requesters fail a task they hear nothing about for 15 minutes; tell them it is being worked on."""
        now = self.clock()
        for tid in self.store.ids():
            with self.store.lock(tid):
                t = self.store.load(tid)
                if (t and t['state'] in ACTIVE and t.get('stop_reason') != 'ended_by_requester'
                        and now - t.get('last_sent_ts', 0) >= self.policy['keepalive_seconds']):
                    self.send(t, '', status_only=True)
                    self.store.save(t)

    def shutdown(self):
        self.stopping.set()
        if self.owner_inputs:self.owner_inputs.close()
        for ev in self.cancel_events.values():
            ev.set()

    def _waiting_note(self, tid, scheduling):
        with self.store.lock(tid):
            t = self.store.load(tid)
            if not t or t['state'] not in ACTIVE:
                return
            label = {'waiting_desktop': '等待桌面', 'waiting_workspace': '等待工作目录',
                     'waiting_slot': '等待执行名额'}[scheduling['state']]
            self.send(t, label + '；占用任务：' + ', '.join(scheduling['blocked_by']))
            self.store.save(t)

    def _work(self, tid):
        try:
            while not self.stopping.is_set():
                reconcile = False
                with self.store.lock(tid):
                    t = self.store.load(tid)
                    if t and t.get('provider_step_intent') and t.get('provider_reconcile_requested'):
                        reconcile = True
                    elif t and t.get('provider_step_intent') and not t['provider_step_intent'].get('replay_ready'):
                        self._retain_provider(t)
                        self._block(t,'Provider intent is unresolved; reconcile the original operation',kind='provider_incomplete')
                        break
                    if not reconcile and t and not t.get('provider_step_intent') and t.get('pending_revision') and not t['flags'].get('cancel') and t['state'] not in FINAL:
                        self._apply_revision(t)
                if reconcile:
                    self._reconcile_provider(tid)
                    continue
                if not t or t['state'] not in ACTIVE:
                    break
                if t['flags'].get('cancel'):
                    with self.store.lock(tid):
                        t = self.store.load(tid)
                        self._stop(t, 'canceled')
                    break
                if t['state'] == 'accepted':
                    with self.store.lock(tid):
                        t = self.store.load(tid)
                        if native.is_native(t):
                            native.prepare(self, t)
                        elif lightweight.is_light(t):
                            lightweight.prepare(self, t)
                        else:
                            t['state'] = 'planning'
                            self.store.save(t)
                    continue
                if not self._budget_ok(tid):
                    break
                resource = resource_for(t, desktop_enabled(self.config))
                wait_started = time.monotonic()
                with self.resources.lease(tid, resource, self.task_work_dir(t), self.cancel_events.get(tid), self.stopping,
                                          on_wait=lambda status: self._waiting_note(tid, status),
                                          abort_wait=lambda: bool((self.store.load(tid) or {}).get('pending_revision'))) as acquired:
                    if not acquired:
                        continue
                    self._timing(tid, 'resource_wait', time.monotonic() - wait_started, resource=resource)
                    # Control messages can change the task while it waits. Re-read before acting.
                    t = self.store.load(tid)
                    if not t or t.get('pending_revision') or t['state'] not in ACTIVE or t['flags'].get('cancel'):
                        continue
                    if resource != resource_for(t, desktop_enabled(self.config)):
                        continue
                    if resource == 'desktop' and t['spec'].get('resource') == 'background':
                        with self.store.lock(tid):
                            self._block(t, 'background task requires a desktop check; revise the task resource contract', kind='resource_mismatch')
                        break
                    if resource == 'desktop' and not t.get('native_cleanup_only') and not self._environment_ok(tid):
                        break
                    if t['state'] == 'planning':
                        self._plan(tid, replan=t.get('replan_context'))
                    elif t['state'] == 'running':
                        if native.is_native(t):
                            native.step(self, tid)
                            t = self.store.load(tid)
                            if t['state'] == 'verifying' and not t['flags'].get('cancel'):
                                self._verify(tid)
                            continue
                        if lightweight.is_light(t):
                            while True:
                                lightweight.step(self, tid)
                                t = self.store.load(tid)
                                if (t['state'] == 'verifying' and not t['flags'].get('cancel')
                                        and not self.stopping.is_set() and self._budget_ok(tid)):
                                    # Keep the lease through acceptance: another GUI task must
                                    # not change the live state between execution and its check.
                                    self._verify(tid)
                                t = self.store.load(tid)
                                if (resource != 'desktop' or t['state'] != 'running' or
                                        self.stopping.is_set() or t['flags'].get('cancel')):
                                    break
                                if not self._budget_ok(tid) or not self._environment_ok(tid):
                                    break
                            continue
                        phase_id = (self._phase(t) or {}).get('id')
                        while True:
                            self._step(tid)
                            self._maybe_replan(tid)
                            t = self.store.load(tid)
                            if (resource != 'desktop' or self.stopping.is_set() or t['flags'].get('cancel') or
                                    t.get('pending_revision') or t['state'] != 'running' or (self._phase(t) or {}).get('id') != phase_id):
                                break
                            # Keep the desktop for this phase so another task cannot change its GUI state
                            # between steps. Background phases release their slot after each step.
                            if not self._budget_ok(tid) or not self._environment_ok(tid):
                                break
                    elif t['state'] == 'verifying':
                        self._verify(tid)
        except Exception:
            self.log.exception('task %s failed inside taskd', tid)
            with self.store.lock(tid):
                t = self.store.load(tid)
                if t and t['state'] in ACTIVE:
                    if t.get('provider_step_intent'):
                        self._retain_provider(t)
                        self._block(t,'Provider interrupted by taskd exception; inspect original operation',kind='provider_incomplete')
                    else:
                        self._block(t, 'internal error in taskd; see the taskd log', kind='executor_error')
        finally:
            with self.store.lock(tid):
                pending = self.store.load(tid)
                if pending and pending.get('provider_step_intent'):
                    self._retain_provider(pending)
            with self.guard:
                self.workers.pop(tid, None)
                self.cancel_events.pop(tid, None)
            if not self.stopping.is_set():
                threading.Thread(target=self.schedule, daemon=True).start()

    # ------------------------------------------------------------------ guards

    def _budget_ok(self, tid):
        with self.store.lock(tid):
            t = self.store.load(tid)
            intent = t.get('provider_step_intent')
            if intent and not intent.get('replay_ready') and not (intent.get('state')=='result_checkpointed' and tid in self.provider_consumers):
                self._retain_provider(t)
                return False
            if native.is_native(t) and t.get('native_cleanup_only'):
                return True  # stopping existing work never starts another goal
            # Legacy cumulative step/time budgets are advisory telemetry.
        return True

    def _environment_ok(self, tid):
        if not self.probes:
            return True
        waited = 0.0
        limit = self.policy['probe_max_wait_minutes'] * 60
        while not self.stopping.is_set():
            failures = []
            for p in self.probes:
                ok, why, _ = p.probe()
                if not ok:
                    failures.append('%s: %s' % (p.name, why))
            if not failures:
                return True
            if waited >= limit:
                with self.store.lock(tid):
                    t = self.store.load(tid)
                    self._block(t, messages.reason('environment', self._goal(t), detail='; '.join(failures)), kind='environment')
                return False
            self.store.log(tid, 'environment_wait', failures=failures)
            ev = self.cancel_events.get(tid)
            if ev and ev.wait(self.policy['probe_retry_seconds']):
                return True  # canceled: the loop handles it
            waited += self.policy['probe_retry_seconds']
        return False

    # ------------------------------------------------------------------ executor calls

    def _retain_provider(self,t):
        intent = t.get('provider_step_intent') or {}
        snapshot = intent.get('resource_snapshot') or {}
        self.resources.retain(t['id'],snapshot.get('resource','desktop'),
                              snapshot.get('workspace',str(self.task_work_dir(t))))

    def _provider_view(self,task):
        intent = task.get('provider_step_intent') or {}
        execution = task.get('provider_execution')
        if not intent and not execution: return None
        return {'operation_id':intent.get('operation_id') or (execution or {}).get('operation_id'),
                'intent_state':intent.get('state'),'caller':intent.get('identity'),
                'input_sha256':intent.get('input_sha256'),
                'reconcile_available':bool(intent and callable(getattr(self.executor,'observe',None))),
                'cancel_requested':bool(task['flags'].get('cancel') or task.get('provider_stop_requested')),
                'original_context_available':bool(intent.get('invocation') and intent.get('caller_context')),
                'execution':execution}

    def _reconcile_provider(self,tid):
        from anet_provider_harnesses.taskd_step import record_result
        with self.store.lock(tid):
            task = self.store.load(tid)
            task.pop('provider_reconcile_requested',None)
            intent = task['provider_step_intent']
            self._retain_provider(task)
            self.store.save(task)
            operation_id = intent['operation_id']
            invocation = intent.get('invocation')
            if not invocation or not intent.get('caller_context'):
                self._block(task,'Legacy provider intent lacks original invocation context; no dispatch attempted',kind='provider_incomplete')
                return
        try:
            extra={'owner_input':self._owner_context(tid,operation_id)} if self.owner_inputs is not None else {}
            result = self.executor.observe(operation_id,work_dir=invocation['work_dir'],
                timeout=min(30,task['spec']['budget']['step_timeout_min']*60),**extra)
        except Exception as error:
            with self.store.lock(tid):
                task = self.store.load(tid)
                task['provider_observation_error'] = type(error).__name__
                self._block(task,'Provider observation failed; original intent retained',kind='provider_incomplete')
            return
        with self.store.lock(tid):
            task = self.store.load(tid)
            intent = task['provider_step_intent']
            evidence = getattr(result,'harness_evidence',None) or {}
            if evidence.get('operation_id') != operation_id:
                self._block(task,'Provider observation identity differs; original intent retained',kind='provider_incomplete')
                return
            intent['execution_result'] = record_result(result)
            task['provider_execution'] = {k:evidence.get(k) for k in ('operation_id','source_id','native_session_id','native_message_id',
                'state','evidence_path','native_terminal_verified','native_cancel_confirmed','business_effect_verified','workspace_binding_verified')}
            canceled = task['flags'].get('cancel') or task.get('provider_stop_requested')
            terminal = evidence.get('native_terminal_verified') is True
            if canceled and terminal:
                task.pop('provider_step_intent',None)
                task['provider_reconciled_terminal'] = evidence
                self.store.save(task)
                self.resources.release_retained(tid)
                self._stop(task,'canceled')
                return
            if not (result.ok or terminal) or canceled:
                intent['state'] = 'effect_unverified'
                self._block(task,'Original provider effect still requires observation; no replacement dispatched',kind='provider_incomplete')
                return
            # Rewind only the engine caller checkpoint, never native effects.
            # Controls arriving after dispatch remain queued for the next call.
            restored = deepcopy(intent['caller_context'])
            for key in ('seen_messages','provider_deferred_controls','last_sent_ts','messages_sent','last_note','delivery','delivery_state'):
                if key in task: restored[key] = deepcopy(task[key])
            restored['provider_step_intent'] = intent
            if restored.get('pending_revision'):
                restored['provider_deferred_revision'] = restored.pop('pending_revision')
            intent['replay_ready'] = True
            intent['state'] = 'result_observed_pending_consumer'
            restored['flags']['cancel'] = False
            restored['flags']['interrupted'] = False
            restored['state'] = 'planning' if invocation.get('caller_kind')=='plan' else 'running'
            restored['block_reason'] = restored['block_kind'] = restored['blocker'] = None
            restored['provider_execution'] = task['provider_execution']
            self.store.save(restored)
            self.store.log(tid,'provider_reconcile_ready',operation_id=operation_id,native_terminal_verified=terminal)

    def _finish_provider_caller(self,tid):
        with self.store.lock(tid):
            task = self.store.load(tid)
            intent = task.get('provider_step_intent')
            if not intent or intent.get('state') != 'result_checkpointed': return
            evidence = (intent.get('execution_result') or {}).get('harness_evidence') or {}
            if (task['flags'].get('cancel') or task.get('provider_stop_requested')) and evidence.get('native_terminal_verified') is not True:
                self._retain_provider(task)
                return
            task.pop('provider_step_intent',None)
            for control in task.pop('provider_deferred_controls',[]):
                task['inbox']['answers' if control['kind']=='answer' else 'hints'].append(control['text'])
            if task.get('provider_deferred_revision'):
                task['pending_revision'] = task.pop('provider_deferred_revision')
            self.store.save(task)
            self.resources.release_retained(tid)

    def _timing(self, tid, purpose, seconds, **extra):
        with self.store.lock(tid):
            t = self.store.load(tid)
            item = t.setdefault('timing', {}).setdefault(purpose, {'calls': 0, 'seconds': 0.0})
            item['calls'] += 1
            item['seconds'] = round(item['seconds'] + max(0, seconds), 6)
            self.store.save(t)
        self.store.log(tid, 'timing', purpose=purpose, duration=round(max(0, seconds), 6), **extra)

    def _run_exec(self, tid, prompt, schema, session, phase_id, purpose=None,caller_metadata=None):
        """One executor call in task mode, accounted for. Returns (parsed object or None, result)."""
        caller_prompt = prompt
        if not self.executor.caps.get('schema'):
            prompt = prompt + '\n\n' + schema_instructions(schema)
        purpose = purpose or ('planning' if schema is PLAN_SCHEMA else 'execution')
        durable = getattr(self.executor, 'durable_step_required', False)
        operation_id = None
        with self.store.lock(tid):
            t = self.store.load(tid)
            replay = t.get('provider_step_intent') or {}
            cached = None
            if durable and not replay and t.get('pending_revision'):
                from .executors.base import ExecResult
                return None,ExecResult(False,error='provider_revision_pending_no_dispatch')
            if durable and replay.get('replay_ready'):
                invocation = replay['invocation']
                if (phase_id != invocation['phase_id'] or purpose != invocation['purpose'] or schema != invocation['schema']):
                    self._block(t,'Original provider caller differs; reconciliation held',kind='provider_incomplete')
                    from .executors.base import ExecResult
                    return None,ExecResult(False,error='provider_caller_conflict')
                prompt,session = invocation['prompt'],invocation['session']
                caller_prompt = invocation['caller_prompt']
                cached = replay['execution_result']
            elif durable and replay.get('state')=='result_checkpointed' and tid in self.provider_consumers:
                # The same caller has consumed the previous result and is now
                # explicitly requesting a schema repair or its next attempt.
                t.pop('provider_step_intent',None)
            resource = (self.resources.status(tid) or {}).get('resource', 'background')
            if not cached:
                prompt += '\n\n' + prompts.resource_rules(resource)
                if self.task_work_dir(t) != self.work_dir:
                    prompt += '\nShared source directory (read only): %s. Copy needed inputs into your task directory before editing or building.' % self.work_dir
            if durable:
                from anet_provider_harnesses.taskd_step import reserve_step, DurableStepConflict
                from .executors.base import ExecResult
                try:
                    operation_id = reserve_step(t,phase_id=phase_id,purpose=purpose,prompt=prompt,
                        work_dir=self.task_work_dir(t),schema=schema,
                        session=session if self.executor.caps.get('resume') else None)
                    t['provider_step_intent'].setdefault('resource_snapshot',
                        {'resource':resource,'workspace':str(self.task_work_dir(t))})
                    intent = t['provider_step_intent']
                    if 'caller_context' not in intent:
                        context = deepcopy(t)
                        context.pop('provider_step_intent',None)
                        intent['caller_context'] = context
                        intent['invocation'] = {'caller_prompt':caller_prompt,'prompt':prompt,'schema':deepcopy(schema),
                            'session':session if self.executor.caps.get('resume') else None,'work_dir':str(self.task_work_dir(t)),
                            'phase_id':phase_id,'purpose':purpose,
                            'caller_kind':'plan' if schema is PLAN_SCHEMA else 'step' if schema is REPORT_SCHEMA else 'unknown',
                            'caller_metadata':deepcopy(caller_metadata or {})}
                except DurableStepConflict:
                    self._retain_provider(t)
                    self.store.log(tid,'provider_intent_conflict',intent=t.get('provider_step_intent'))
                    self._block(t,'Provider input differs from an uncheckpointed operation; inspect original evidence',kind='provider_incomplete')
                    return None, ExecResult(False,error='durable_provider_input_conflict')
            t['step_in_progress'] = {'n': t['used']['steps'] + 1, 'phase': phase_id, 'since': utcnow()}
            if operation_id:
                t['step_in_progress']['operation_id'] = operation_id
            self.store.save(t)
        timeout = t['spec']['budget']['step_timeout_min'] * 60
        started = time.monotonic()
        with execution(self.store.activity, tid, timeout, purpose=purpose):
            extra = {'operation_id':operation_id} if operation_id else {}
            if operation_id and self.owner_inputs is not None:extra['owner_input']=self._owner_context(tid,operation_id)
            if cached:
                from anet_provider_harnesses.taskd_step import restore_result
                r = restore_result(cached)
            else:
                r = self.executor.run(prompt, work_dir=self.task_work_dir(t), timeout=timeout, schema=schema,
                                      session=session if self.executor.caps.get('resume') else None,
                                      mode='task', cancel=self.cancel_events.get(tid), **extra)
        self._timing(tid, 'provider_result_replay' if cached else purpose,
                     time.monotonic() - started, session_reused=bool(session), phase=phase_id)
        obj = extract_json(r.text) if r.ok else None
        with self.store.lock(tid):
            t = self.store.load(tid)
            if durable:
                evidence = getattr(r,'harness_evidence',None) or {}
                t['provider_execution'] = {k:evidence.get(k) for k in ('operation_id','source_id','native_session_id',
                    'native_message_id','state','evidence_path','native_terminal_verified','native_cancel_confirmed',
                    'business_effect_verified','workspace_binding_verified')}
                resolved = r.ok or evidence.get('native_terminal_verified') is True or evidence.get('state') in ('not_dispatched','offline_not_dispatched')
                if not resolved:
                    t['provider_step_intent']['state'] = 'effect_unverified'
                    self._retain_provider(t)
                    self.store.save(t)
                    self._block(t,'Provider outcome unverified; retain and reconcile the SAME operation',kind='provider_incomplete')
                    return obj,r
                from anet_provider_harnesses.taskd_step import record_result
                t['provider_step_intent'].update(state='result_checkpointed',execution_result=record_result(r))
                t['provider_step_intent'].pop('replay_ready',None)
            t['step_in_progress'] = None
            t['used']['steps'] += 1
            t['used']['seconds'] += r.duration
            if r.session_id and r.session_id != (t.get('session') or {}).get('id'):
                t['sessions_used'] = t.get('sessions_used', 0) + 1
            if r.session_id:
                t['session'] = {'id': r.session_id, 'phase': phase_id}
            t['progress']['exec_errors'] = 0 if r.ok else t['progress']['exec_errors'] + 1
            self.store.save(t)
            self.store.log(tid, 'provider_result_replay' if cached else 'exec',
                           ok=r.ok, error=r.error, duration=round(r.duration, 1),
                           session=r.session_id, chars=len(r.text or ''))
        return obj, r

    def _exec_failed(self, tid, r):
        with self.store.lock(tid):
            t = self.store.load(tid)
            if t.get('provider_step_intent') or t.get('block_kind') == 'provider_incomplete':
                if t.get('provider_step_intent'):
                    evidence=((t['provider_step_intent'].get('execution_result') or {}).get('harness_evidence') or {})
                    if (t['flags'].get('cancel') or r.canceled) and evidence.get('native_terminal_verified') is True:
                        self._stop(t,'canceled')
                        return True
                    if t['provider_step_intent'].get('state')=='result_checkpointed' and not r.ok:
                        self._block(t,'Original provider operation ended without a usable successful result',kind='provider_execution_failed')
                    else:
                        self._retain_provider(t)
                return True
            if t['flags'].get('cancel') or r.canceled:
                self._stop(t, 'canceled')
                return True
            if t['progress']['exec_errors'] >= self.policy['max_exec_errors']:
                self._block(t, messages.reason('executor_failed', self._goal(t)) + ' (%s)' % r.error, kind='executor_error')
                return True
        return False

    # ------------------------------------------------------------------ planning

    def _checker_docs(self):
        return [c.doc for c in self.checkers.values() if c.doc]

    def _plan(self, tid, replan=None):
        self.provider_consumers.add(tid)
        try:
            self._plan_body(tid,replan=replan)
        except BaseException:
            raise
        else:
            self._finish_provider_caller(tid)
        finally:
            self.provider_consumers.discard(tid)

    def _plan_body(self, tid, replan=None):
        with self.store.lock(tid):
            t = self.store.load(tid)
            intent = t.get('provider_step_intent') or {}
            replay = intent.get('replay_ready')
            if replay:
                t = deepcopy(intent['caller_context'])
        hints = list(t['inbox']['hints'])
        if replay:
            hints = list(intent['invocation'].get('caller_metadata',{}).get('plan_hints',hints))
        prompt = prompts.plan_prompt(t['spec'], self.task_work_dir(t), t['checks'], self._checker_docs(),
                                     previous=t.get('previous'), replan=replan, hints=hints,
                                     inputs_dir=t.get('inputs_dir'), existing_plan=t.get('plan'))
        if replay:
            prompt = intent['invocation']['caller_prompt']
        plan = None
        plan_error = 'Repair the plan schema and artifact dependencies.'
        for attempt in range(2):
            if not self._budget_ok(tid):
                return
            obj, r = self._run_exec(tid, prompt, PLAN_SCHEMA, None, 'plan',caller_metadata={'plan_hints':hints})
            if not r.ok:
                if self._exec_failed(tid, r):
                    return
                continue
            try:
                plan = self._validate_replan(t, obj) if t.get('plan') else self._validate_plan(t, obj)
                break
            except ValueError as e:
                plan_error = str(e)
                self.store.log(tid, 'plan_invalid', error=str(e)[:500])
                with self.store.lock(tid):
                    current = self.store.load(tid)
                    errors = current['progress'].setdefault('plan_errors', {})
                    key = _hash_obj(plan_error_code(plan_error))
                    errors[key] = errors.get(key, 0) + 1
                    self.store.save(current)
                    baseline = current['progress'].get('plan_error_baseline', {}).get(key, 0)
                    if errors[key] - baseline >= self.policy['same_plan_error_limit']:
                        self._block(current, 'Repeated plan validation error: ' + plan_error, kind='planning_error')
                        return
                    hints = list(current['inbox']['hints'])
                prompt = prompts.plan_prompt(t['spec'], self.task_work_dir(t), t['checks'], self._checker_docs(),
                                             previous=t.get('previous'), replan=replan, hints=hints,
                                             inputs_dir=t.get('inputs_dir'), existing_plan=t.get('plan')) + \
                    '\n\nYour previous plan was not usable: %s\nRejected plan (data):\n%s' % (
                        e, json.dumps(obj, ensure_ascii=False)[:12000])
        with self.store.lock(tid):
            t = self.store.load(tid)
            if t['flags'].get('cancel'):
                self._stop(t, 'canceled')
                return
            if plan is None:
                # Invalid plans are internal orchestration failures, not decisions
                # the user must repair. Retry with feedback within the replan budget.
                if t['used']['replans'] < self.policy['max_replans']:
                    t['used']['replans'] += 1
                    t['state'] = 'planning'
                    t['inbox']['hints'].append('Repair the invalid plan: ' + plan_error)
                    self.store.save(t)
                else:
                    self._block(t, 'No valid plan within the replan budget: ' + plan_error, kind='planning_error')
                return
            done = {p['id'] for p in (t.get('plan') or {}).get('phases', []) if p.get('status') == 'done'}
            if t.get('plan'):
                t['phase_index'] = len(done)
            else:
                t['phase_index'] = 0
            new_checks = [c for c in plan.pop('new_checks') if c['id'] not in {x['id'] for x in t['checks']}]
            t['checks'] += new_checks
            t['plan'] = plan
            t.pop('replan_context', None)
            t['inbox']['hints'] = t['inbox']['hints'][len(hints):]
            t['flags']['fresh'] = True
            t['session'] = None
            self._baselines(t, new_checks)
            self.store.log(tid, 'plan', plan=plan, drafted=[c['id'] for c in new_checks], replan=bool(replan))
            phases_text = '\n'.join('%d. %s — %s' % (i + 1, p['title'], p['done_when']) for i, p in enumerate(plan['phases']))
            reasons = plan.get('approval_reasons') or []
            t['plan_revision'] = t.get('plan_revision', 0) + 1
            if reasons or t['spec']['plan_approval'] == 'always':
                t['state'] = 'awaiting_approval'
                t['question'] = 'approve_plan'
                t['approval_reasons'] = reasons
                self.send(t, messages.say('plan_approval', self._goal(t), n=len(plan['phases']), phases=phases_text,
                                          reasons='\n'.join('- ' + x for x in reasons) or '-'), plan=plan)
            else:
                t['state'] = 'running'
                self.send(t, messages.say('plan_ready', self._goal(t), n=len(plan['phases']), phases=phases_text), plan=plan)
            self.store.save(t)

    def _validate_replan(self, t, obj):
        """Keep completed work and same-phase gates before dependency validation.

        A changed phase ID needs an explicit gate assignment: guessing a phase could
        run preflight before the very repair it is supposed to verify.
        """
        if not isinstance(obj, dict) or not isinstance(obj.get('phases'), list):
            return self._validate_plan(t, obj)
        if any(not isinstance(p, dict) or not isinstance(p.get('id'), str)
               or not isinstance(p.get('preflight', []), list)
               or any(not isinstance(c, str) for c in p.get('preflight', []))
               for p in obj['phases']):
            return self._validate_plan(t, obj)  # malformed JSON remains a plan error, never a worker crash
        proposed = deepcopy(obj)
        old = (t.get('plan') or {}).get('phases', [])
        kept = [p for p in old if p.get('status') == 'done']
        completed_ids = {p['id'] for p in kept}
        if any(isinstance(p, dict) and p.get('id') in completed_ids for p in proposed['phases']):
            raise ValueError('return remaining phases only; completed phase IDs are retained: ' +
                             ', '.join(sorted(completed_ids)))
        old_by_id = {p['id']: p for p in old}
        for p in proposed['phases']:
            if not isinstance(p, dict):
                continue
            prior = old_by_id.get(p.get('id'), {})
            if isinstance(p.get('preflight', []), list):
                p['preflight'] = list(dict.fromkeys(p.get('preflight', []) + prior.get('preflight', [])))
        fields = ('id', 'title', 'done_when', 'checks', 'resource', 'requires_desktop', 'produces', 'requires', 'final_checks', 'preflight', 'milestones')
        prefix = [{k: p.get(k, 'background' if k == 'resource' else False if k == 'requires_desktop' else []) for k in fields} for p in kept]
        proposed['phases'] = prefix + proposed['phases']
        result = self._validate_plan(t, proposed)
        for i, p in enumerate(kept):
            # Restore exact completed work, including its verification record.
            result['phases'][i] = deepcopy(p)
        return result

    def _validate_plan(self, t, obj):
        # Accept old persisted/scripted plans, but normalize their gates safely.
        if isinstance(obj, dict) and isinstance(obj.get('phases'), list):
            for p in obj['phases']:
                if isinstance(p, dict):
                    for name in ('produces', 'requires', 'final_checks', 'preflight', 'milestones'):
                        p.setdefault(name, [])
                    p.setdefault('resource', 'background' if t['spec'].get('resource') == 'background' or
                                 not desktop_enabled(self.config) else 'desktop')
                    p.setdefault('requires_desktop', False)
        if obj is None or not conforms(obj, PLAN_SCHEMA):
            raise ValueError('the answer does not match the plan schema')
        phases = obj['phases']
        if not phases or len(phases) > self.policy['max_phases']:
            raise ValueError('a plan has 1 to %d phases' % self.policy['max_phases'])
        existing = {c['id']: c for c in t['checks']}
        drafted = []
        for d in obj['drafted_checks']:
            if d['type'] not in self.checkers:
                raise ValueError('drafted check %s uses unknown type %s' % (d['id'], d['type']))
            if not CHECK_ID.fullmatch(d['id']) or d['id'] in existing or d['id'] in {x['id'] for x in drafted}:
                raise ValueError('drafted check id %r is invalid or taken' % d['id'])
            try:
                params = json.loads(d['params_json'] or '{}')
                params = self.checkers[d['type']].validate(params)
            except (ValueError, TypeError) as e:
                raise ValueError('drafted check %s: %s' % (d['id'], e)) from None
            drafted.append({'id': d['id'], 'type': d['type'], 'params': params, 'origin': 'drafted',
                            'covers': d['covers'], 'scope': 'final' if d['covers'] else 'phase'})
        covered = {d['covers'] for d in drafted}
        for a in t['spec']['acceptance']:
            if a['type'] == 'natural' and a['id'] not in covered and not any(c.get('covers') == a['id'] for c in existing.values()):
                cid = 'r_' + a['id']
                drafted.append({'id': cid, 'type': 'llm_review', 'origin': 'drafted', 'covers': a['id'], 'scope': 'final',
                                'params': self.checkers['llm_review'].validate({'question': a['text']})})
        known = set(existing) | {d['id'] for d in drafted}
        definitions = dict(existing, **{d['id']: d for d in drafted})
        seen_ids = set()
        clean = []
        for p in phases:
            pid = p['id'].strip() or 'p%d' % (len(clean) + 1)
            if pid in seen_ids or not CHECK_ID.fullmatch(pid):
                pid = 'p%d' % (len(clean) + 1)
            seen_ids.add(pid)
            milestone_ids, milestone_checks = set(), set()
            if len(p['milestones']) > 32:
                raise ValueError('a phase has at most 32 milestones')
            for m in p['milestones']:
                if not CHECK_ID.fullmatch(m['id']) or m['id'] in milestone_ids or not m['title'].strip() or not m['checks']:
                    raise ValueError('milestones require unique IDs, a title and nonempty deterministic checks')
                if len(set(m['checks'])) != len(m['checks']) or milestone_checks.intersection(m['checks']):
                    raise ValueError('milestone checks may not be reused within a phase')
                for cid in m['checks']:
                    if cid not in known or definitions[cid]['type'] == 'llm_review':
                        raise ValueError('milestone requires a known deterministic check: ' + cid)
                    if definitions[cid].get('scope', 'final') != 'phase' or definitions[cid].get('covers'):
                        raise ValueError('milestones require phase-local checks; final acceptance remains separate: ' + cid)
                milestone_ids.add(m['id'])
                milestone_checks.update(m['checks'])
            unknown = [c for c in p['checks'] + p['final_checks'] + p['preflight'] if c not in known]
            if unknown:
                raise ValueError('phase %s names unknown checks %s; use only check IDs from %s; '
                                 'put prose in done_when/notes or define a drafted_check' %
                                 (pid, unknown, sorted(known)))
            clean.append({'id': pid, 'title': p['title'], 'done_when': p['done_when'],
                          'resource': p['resource'], 'requires_desktop': p['requires_desktop'],
                          'checks': list(dict.fromkeys(p['checks'] + [cid for m in p['milestones'] for cid in m['checks']])),
                          'milestones': deepcopy(p['milestones']), 'status': 'pending',
                          'produces': list(dict.fromkeys(p['produces'])), 'requires': p['requires'],
                          'final_checks': list(dict.fromkeys(p['final_checks'])), 'preflight': list(dict.fromkeys(p['preflight']))})
        old_preflight = {cid for p in (t.get('plan') or {}).get('phases', []) for cid in p.get('preflight', [])}
        new_preflight = {cid for p in clean for cid in p['preflight']}
        if old_preflight - new_preflight:
            missing = old_preflight - new_preflight
            owners = {p['id']: sorted(set(p.get('preflight', [])) & missing)
                      for p in (t.get('plan') or {}).get('phases', []) if set(p.get('preflight', [])) & missing}
            raise ValueError('plan drops mandatory preflight checks: %s; previous phase assignments: %s' %
                             (sorted(missing), json.dumps(owners, sort_keys=True)))
        all_checks = dict(existing, **{d['id']: d for d in drafted})
        for p in clean:
            if any(all_checks[c]['type'] == 'llm_review' for c in p['preflight']):
                raise ValueError('preflight requires deterministic checks')
        final_ids = {cid for cid, c in all_checks.items() if c.get('scope', 'final') == 'final'}
        declared_owners = {cid: i for i, p in enumerate(clean) for cid in p['final_checks'] + p['checks'] if cid in final_ids}
        producers = {}
        for i, p in enumerate(clean):
            for artifact in p['produces']:
                if Path(artifact).is_absolute() or '..' in Path(artifact).parts:
                    raise ValueError('phase artifacts must be paths relative to the work directory')
                producers[artifact] = i
                cid = 'phase_' + hashlib.sha256((p['id'] + ':' + artifact).encode()).hexdigest()[:20]
                if cid not in all_checks:
                    c = {'id': cid, 'type': 'files', 'origin': 'generated', 'scope': 'phase', 'covers': '',
                         'generated_for': {'phase': p['id'], 'path': artifact},
                         'params': self.checkers['files'].validate({'pattern': artifact,
                                   'kind': 'directory' if artifact.endswith(('/', '\\')) else 'any', 'min_size': 0})}
                    drafted.append(c)
                    all_checks[cid] = c
                if cid not in p['checks']:
                    p['checks'].append(cid)
        for i, p in enumerate(clean):
            desktop_gates = [cid for cid in p['checks'] + p['preflight'] + p['final_checks']
                             if all_checks[cid]['type'] in DESKTOP_CHECKS]
            if p['resource'] != 'desktop' and (p['requires_desktop'] or desktop_gates):
                raise ValueError('phase %s requires a desktop lease; use resource=desktop (desktop checks: %s)' %
                                 (p['id'], desktop_gates))
            for artifact in p['requires']:
                if producers.get(artifact, -1) >= i:
                    raise ValueError('phase %s requires %s before its producer phase; reorder or split the plan' % (p['id'], artifact))
            # Final acceptance NEVER gates an earlier phase. Local drafted checks do.
            p['checks'] = [cid for cid in p['checks'] if cid not in final_ids]
            p['final_checks'] = []
            for cid in p['checks'] + p['preflight']:
                params = all_checks[cid]['params']
                paths = [params[k] for k in ('file', 'path', 'pattern') if isinstance(params.get(k), str)]
                for path in paths:
                    if any((j >= i if cid in p['preflight'] else j > i) and (fnmatch.fnmatch(a, path) or fnmatch.fnmatch(path, a)) for a, j in producers.items()):
                        raise ValueError('phase %s check %s requires a later phase artifact %s' % (p['id'], cid, path))
        for cid in sorted(final_ids):
            params = all_checks[cid]['params']
            paths = [params[k] for k in ('file', 'path', 'pattern') if isinstance(params.get(k), str)]
            owners = [i for artifact, i in producers.items() if any(fnmatch.fnmatch(artifact, p) or fnmatch.fnmatch(p, artifact) for p in paths)]
            clean[max(owners) if owners else declared_owners.get(cid, len(clean) - 1)]['final_checks'].append(cid)
        if t['spec'].get('resource') == 'background' and (any(p['resource'] == 'desktop' for p in clean) or
                any(c['type'] in DESKTOP_CHECKS for c in all_checks.values())):
            raise ValueError('a background task cannot use desktop phases or desktop checks')
        return {'phases': clean, 'approval_reasons': [r for r in obj['approval_reasons'] if r.strip()],
                'notes': obj.get('notes', ''), 'new_checks': drafted}

    # ------------------------------------------------------------------ steps

    def _phase(self, t):
        phases = (t.get('plan') or {}).get('phases', [])
        i = t.get('phase_index', 0)
        return phases[i] if i < len(phases) else None

    def _since(self, t):
        items = []
        if t['flags'].get('interrupted'):
            items.append('The previous step was interrupted (the machine or taskd restarted). Check the '
                         'current state of the environment before acting; do not assume what it left.')
        for f in t['inbox']['feedback']:
            items.append(f)
        for h in t['inbox']['hints']:
            items.append('Hint from the requester: ' + prompts.fence('HINT', h))
        for a in t['inbox']['answers']:
            items.append('Answer from the requester: ' + prompts.fence('ANSWER', a))
        return items

    def _prepare_step(self, tid):
        with self.store.lock(tid):
            t = self.store.load(tid)
            phase = self._phase(t)
            if phase is None:
                t['state'] = 'verifying'
                self.store.save(t)
                return
            phase_checks = [c for c in t['checks'] if c['id'] in phase['checks'] + phase.get('preflight', [])]
            if (phase.get('requires_desktop') or phase.get('resource') == 'desktop'
                    or any(c['type'] in DESKTOP_CHECKS for c in phase_checks)):
                lease = self.resources.status(tid) or {}
                if lease.get('state') != 'executing' or lease.get('resource') != 'desktop':
                    self._block(t, 'Phase requires desktop access but no desktop lease is held', kind='resource_mismatch')
                    return
            if t.pop('retry_system_checks', False):
                self.store.save(t)
                results = self.run_checks(tid, [c for c in phase_checks if c['type'] != 'llm_review'])
                problem = self._system_check_problem(t, results)
                if problem:
                    self._block(t, problem, kind='checker_error', results=results)
                    return
            preflight = [c for c in t['checks'] if c['id'] in phase.get('preflight', [])]
            if preflight:
                results = self.run_checks(tid, preflight)
                if self._system_check_problem(t, results):
                    self._block(t, self._system_check_problem(t, results), kind='checker_error', results=results)
                    return
                failed = {k:v for k,v in results.items() if v['outcome'] != 'pass'}
                if failed:
                    detail = next(iter(failed.values())).get('observed') or {}
                    code = detail.get('code', 'configuration_mismatch') if isinstance(detail, dict) else 'configuration_mismatch'
                    self._block(t, 'Preflight failed: ' + json.dumps(failed, ensure_ascii=False)[:2000], kind=code, results=results)
                    return
            sess = t.get('session') or {}
            resume = (self.executor.caps.get('resume') and sess.get('id') and sess.get('phase') == phase['id']
                      and not t['flags'].get('fresh'))
            since = self._since(t)
            step_no = t['used']['steps'] + 1
            prompt = prompts.step_prompt(t['spec'], self.task_work_dir(t), t['checks'], t['plan'], phase, step_no,
                                         None if resume else self.checkpoint(t), since,
                                         t['spec']['budget']['step_timeout_min'], fresh=not resume,
                                         inputs_dir=t.get('inputs_dir'))
            if phase.get('milestones'):
                prompt += '\nTaskd-verified milestone position: ' + json.dumps(milestones.view(t, phase), ensure_ascii=False)
            t['inbox'] = {'hints': [], 'answers': [], 'feedback': []}
            t['flags']['interrupted'] = False
            t['flags']['fresh'] = False
            self.store.save(t)
        return phase,sess,resume,step_no,prompt,None

    def _step(self,tid):
        self.provider_consumers.add(tid)
        try:
            self._step_body(tid)
        except BaseException:
            raise
        else:
            self._finish_provider_caller(tid)
        finally:
            self.provider_consumers.discard(tid)

    def _step_body(self,tid):
        with self.store.lock(tid):
            t = self.store.load(tid)
            intent = t.get('provider_step_intent') or {}
        if intent.get('replay_ready'):
            context = intent['caller_context']
            invocation = intent['invocation']
            phase = self._phase(context)
            sess = {'id':invocation['session']}
            resume = bool(invocation['session'])
            step_no = intent['identity']['step_number']
            prompt = invocation['caller_prompt']
            purpose = invocation['purpose']
        else:
            prepared = self._prepare_step(tid)
            if prepared is None: return
            phase,sess,resume,step_no,prompt,purpose = prepared
        report, r = self._run_exec(tid, prompt, REPORT_SCHEMA, sess.get('id') if resume else None, phase['id'],purpose=purpose)
        if not r.ok:
            self.store.log(tid, 'step', n=step_no, phase=phase['id'], ok=False, error=r.error)
            self._exec_failed(tid, r)
            return
        problem = None if (report is not None and conforms(report, REPORT_SCHEMA)) else 'it is not the JSON report'
        if problem and self.executor.caps.get('resume') and r.session_id:
            report, r2 = self._run_exec(tid, prompts.repair_prompt(problem), REPORT_SCHEMA, r.session_id, phase['id'], purpose='report_repair')
            problem = None if (r2.ok and report is not None and conforms(report, REPORT_SCHEMA)) else problem
        with self.store.lock(tid):
            t = self.store.load(tid)
            if problem:
                self.store.log(tid, 'step', n=step_no, phase=phase['id'], ok=False, error='invalid report')
                self._no_progress(t, 'the last answer was not a usable report')
                self.store.save(t)
                return
            if isinstance(report.get('blocker'), dict):
                report['blocker_detail'] = report['blocker']
                report['blocker'] = report['blocker_detail']['detail']
            evidence = self._evidence(report, self.task_work_dir(t))
            t['last_step'] = {'summary': report['summary'][:2000], 'artifacts': report['artifacts'][:50],
                              'next': report['next'][:1000], 'blocker': report.get('blocker'), 'at': utcnow()}
            self.store.save(t)
            novelty = self._novelty(t, evidence, report)
            self.store.save(t)
            self.store.log(tid, 'step', n=step_no, phase=phase['id'], ok=True, report=report,
                           evidence=evidence, new=novelty)
            status = report['status']
            if t['flags'].get('cancel'):
                self._stop(t, 'canceled')
                return
            if status == 'needs_user' and (report.get('question') or '').strip():
                t['state'] = 'needs_user'
                t['question'] = report['question']
                self.send(t, messages.say('needs_user', self._goal(t), question=report['question']),
                          question=report['question'])
                self.store.save(t)
                return
            if status == 'blocked':
                self.store.save(t)
        if status == 'blocked':
            if (report.get('blocker_detail') or {}).get('code') in ('resource_mismatch', 'requires_desktop'):
                with self.store.lock(tid):
                    current = self.store.load(tid)
                    lease = self.resources.status(tid) or {}
                    if lease.get('resource') != 'desktop':
                        if self._promote_desktop(current, report.get('blocker') or report['summary']):
                            return
                        self._block(current, 'Executor needs GUI access in a background phase: ' +
                                    (report.get('blocker') or report['summary']), kind='resource_mismatch')
                        return
            if (report.get('blocker_detail') or {}).get('code') == 'checker_error':
                checks = [c for c in t['checks'] if c['id'] in phase['checks'] + phase.get('preflight', [])]
                results = self.run_checks(tid, checks)
                with self.store.lock(tid):
                    current = self.store.load(tid)
                    problem = self._system_check_problem(current, results)
                    if problem:
                        self._block(current, problem, kind='checker_error', results=results)
                        return
            self._claimed_block(tid, report, evidence)
            return
        if status == 'claims_done':
            self._phase_claim(tid, phase, report, evidence, novelty)
            return
        # Passing gates and independently supported diagnostic findings are distinct
        # forms of progress. Only the former can satisfy phase/final acceptance.
        remaining = milestones.pending(t, phase)
        milestone_ids = {cid for m in phase.get('milestones', []) for cid in m['checks']}
        progress_ids = remaining[0]['checks'] if remaining else [cid for cid in phase['checks'] if cid not in milestone_ids]
        checks = [c for c in t['checks'] if c['id'] in progress_ids and c['type'] != 'llm_review']
        progress_checks = self.run_checks(tid, checks) if checks else {}
        with self.store.lock(tid):
            t = self.store.load(tid)
            problem = self._system_check_problem(t, progress_checks)
            if problem:
                self._block(t, problem, kind='checker_error', results=progress_checks)
                return
            newly = [cid for cid, r in progress_checks.items() if r['outcome'] == 'pass'
                     and t['progress']['check_state'].get(cid) != 'pass']
            advanced = milestones.advance(t, phase, progress_checks, utcnow())
            if remaining:
                newly = advanced
            if advanced:
                self.store.log(tid, 'milestone_done', phase=phase['id'], milestones=advanced)
            t['progress']['check_state'].update({cid: r['outcome'] for cid, r in progress_checks.items()})
            if newly:
                self.store.log(tid, 'checks_advanced')
                t['progress']['no_progress'] = 0
            self.store.save(t)
        if not newly:
            self._diagnostic_progress(tid, phase, report, evidence, novelty)
        else:
            self._maybe_relevance_review(tid, phase, report)
        self._maybe_replan(tid)
        self._progress_note(tid, report)

    def _progress_note(self, tid, report):
        with self.store.lock(tid):
            t = self.store.load(tid)
            if t['state'] != 'running':
                return
            if self.clock() - t.get('last_note_ts', 0) >= self.policy['progress_note_seconds']:
                phase = self._phase(t)
                if phase:
                    self.send(t, messages.say('progress', self._goal(t), index=t['phase_index'] + 1,
                                              total=len(t['plan']['phases']), title=phase['title'],
                                              steps=t['used']['steps'], summary=report.get('summary', '')[:500]))
                    self.store.save(t)

    def _evidence(self, report, work_dir=None):
        """The files a report cites that really exist in the work directory, with taskd's own hashes."""
        names = set(report.get('artifacts', []))
        names.update((report.get('blocker_detail') or {}).get('evidence', []))
        for a in report.get('attempts', []):
            names.update(a.get('evidence', []))
        for f in report.get('facts', []):
            names.update(f.get('evidence', []))
        out = []
        work_dir = Path(work_dir or self.work_dir)
        ctx = CheckContext(work_dir, 0, ())
        for name in sorted(n for n in names if isinstance(n, str) and n.strip()):
            try:
                p = ctx.resolve(name, allow_read_paths=False)
            except ValueError:
                out.append({'path': name, 'exists': False})
                continue
            if not p.is_file():
                out.append({'path': name, 'exists': False})
                continue
            out.append({'path': str(p.relative_to(work_dir)), 'exists': True,
                        'size': p.stat().st_size, 'sha256': sha256_file(p, 512 * 1024 * 1024)})
        return out

    def _novelty(self, t, evidence, report=None):
        seen = set(t['progress']['seen'])
        new = []
        cited = {p for f in (report or {}).get('facts', []) if f.get('basis') == 'observed'
                 for p in f.get('evidence', [])}
        cited.update(p for a in (report or {}).get('attempts', []) if a.get('observed')
                     for p in a.get('evidence', []))
        for e in evidence:
            if not e.get('exists') or not e.get('sha256'):
                continue
            if e['path'] not in cited and any(fnmatch.fnmatch(Path(e['path']).name, pat) for pat in self.noise):
                continue
            fp = 'sha:' + e['sha256']
            if fp not in seen:
                new.append(e['path'])
                seen.add(fp)
        t['progress']['seen'] = list(seen)[-SEEN_LIMIT:]
        return new

    def _diagnostic_progress(self, tid, phase, report, evidence, novelty):
        """Review novel, explicitly cited observations, never mere artifact churn."""
        observations = [f for f in report.get('facts', []) if f.get('basis') == 'observed']
        observations += [a for a in report.get('attempts', []) if a.get('observed') and a.get('outcome')]
        cited = {p for item in observations for p in item.get('evidence', [])}
        files = sorted(cited.intersection(novelty))
        verdict = None
        if files and self.reviewer:
            recent = self.store.read_log(tid, 'diagnostic_progress')[-8:]
            claim = json.dumps({'report': report, 'previous_findings': recent}, ensure_ascii=False)
            verdict = self._review(tid, 'Does the new evidence establish a substantive new finding, ruled-out '
                'hypothesis or validated fix towards: %s? Compare prior findings. Repeated symptoms, new '
                'timestamps, renamed files, unsupported claims and screenshots alone are not progress. '
                'A supported diagnostic finding need not pass the phase acceptance yet.' % phase['done_when'],
                claim, files, None)
        supported = bool(verdict and verdict['verdict'] == 'supported')
        with self.store.lock(tid):
            t = self.store.load(tid)
            if t['state'] != 'running' or (self._phase(t) or {}).get('id') != phase['id']:
                return
            if supported:
                t['progress']['no_progress'] = 0
                self.store.log(tid, 'diagnostic_progress', phase=phase['id'], summary=report['summary'],
                               evidence=[e for e in evidence if e['path'] in files], reasons=verdict['reasons'])
            else:
                self._no_progress(t, 'No acceptance gate or independently supported diagnostic finding advanced. '
                                      'Use new evidence to test a different hypothesis; new files alone are not progress.' +
                                      (' Review: ' + verdict['reasons'][:1000] if verdict else ''))
            self.store.save(t)

    def _no_progress(self, t, feedback):
        t['progress']['no_progress'] += 1
        if feedback:
            t['inbox']['feedback'].append(feedback)

    def _maybe_relevance_review(self, tid, phase, report):
        """Every review_every steps the reviewer judges whether the recent new evidence serves the phase.
        New files that do not are not progress (design §13)."""
        # Explicit deterministic milestone gates already establish phase relevance.
        # Keep independent phase/final acceptance reviews; omit only this periodic advisory call.
        if not self.reviewer or phase.get('milestones'):
            return
        with self.store.lock(tid):
            t = self.store.load(tid)
            n = t['used']['steps']
            if n % self.policy['review_every'] or t['progress']['no_progress']:
                return
        steps = self.store.read_log(tid, 'step')[-self.policy['review_every']:]
        files = sorted({p for s in steps for p in s.get('new', [])})
        if not files:
            return
        claims = '\n'.join('- ' + (s.get('report') or {}).get('summary', '') for s in steps)
        verdict = self._review(tid, 'Does this recent work and its new evidence make real progress towards: %s?' % phase['done_when'],
                               claims, files, None)
        if verdict and verdict['verdict'] == 'unsupported':
            with self.store.lock(tid):
                t = self.store.load(tid)
                t['progress']['no_progress'] = self.policy['no_progress_steps']
                t['inbox']['feedback'].append('An independent review found the recent work does not move the '
                                              'phase forward: ' + verdict['reasons'][:1000])
                self.store.save(t)

    def _maybe_replan(self, tid):
        with self.store.lock(tid):
            t = self.store.load(tid)
            if t.get('provider_step_intent'):
                self._retain_provider(t)
                return
            if lightweight.is_light(t):
                return
            if t.get('pending_revision') or t['state'] != 'running' or t['progress']['no_progress'] < self.policy['no_progress_steps']:
                return
            if t['used']['replans'] >= self.policy['max_replans']:
                self._block(t, messages.reason('no_progress', self._goal(t)), kind='stalled')
                return
            t['used']['replans'] += 1
            t['progress']['no_progress'] = 0
            self.store.save(t)
            t['replan_context'] = self.checkpoint(t)
            t['state'] = 'planning'
            self.store.save(t)
        self.store.log(tid, 'replan')

    def _claimed_block(self, tid, report, evidence):
        observed = [f for f in report.get('facts', []) if f.get('basis') == 'observed']
        has_evidence = any(e.get('exists') for e in evidence)
        with self.store.lock(tid):
            t = self.store.load(tid)
            if not observed or not has_evidence:
                self._no_progress(t, 'You reported blocked without an observed fact backed by an evidence file. '
                                     'Either save the evidence of the blocker (an error message, a screenshot) '
                                     'and cite it, or keep working.')
                self.store.save(t)
                return
        verdict = None
        if self.reviewer:
            verdict = self._review(tid, 'Is the work genuinely blocked by an external condition the executor '
                                        'cannot change, as claimed?',
                                   (report.get('blocker') or '') + '\n' + report.get('summary', ''),
                                   [e['path'] for e in evidence if e.get('exists')], None)
        with self.store.lock(tid):
            t = self.store.load(tid)
            if t['state'] in FINAL:
                return
            if verdict and verdict['verdict'] != 'supported':
                self._no_progress(t, 'An independent review does not accept the blocker: ' + verdict['reasons'][:1000])
                self.store.save(t)
            else:
                self._block(t, messages.reason('executor_blocked', self._goal(t), detail=report.get('blocker') or report.get('summary', '')),
                            kind=(report.get('blocker_detail') or {}).get('code'))
                return
        self._maybe_replan(tid)

    def _phase_claim(self, tid, phase, report, evidence, novelty):
        with self.store.lock(tid):
            t = self.store.load(tid)
            checks = [c for c in t['checks'] if c['id'] in phase['checks']]
        results = self.run_checks(tid, checks, claim=report.get('summary', ''),
                                  step_evidence=[e['path'] for e in evidence if e.get('exists')])
        with self.store.lock(tid):
            t = self.store.load(tid)
            problem = self._system_check_problem(t, results)
            if problem:
                self._block(t, problem, kind='checker_error', results=results)
                return
        failed = {cid: r for cid, r in results.items() if r['outcome'] not in ('pass', 'deferred')}
        verdict = None
        if not checks and self.reviewer:
            verdict = self._review(tid, 'Is this phase done? Done when: ' + phase['done_when'],
                                   report.get('summary', ''), [e['path'] for e in evidence if e.get('exists')], None,
                                   judge_claim=True)
        with self.store.lock(tid):
            t = self.store.load(tid)
            if t['state'] in FINAL:
                return
            advanced = milestones.advance(t, phase, results, utcnow())
            if advanced:
                self.store.log(tid, 'milestone_done', phase=phase['id'], milestones=advanced)
            newly = [cid for cid, r in results.items() if r['outcome'] == 'pass'
                     and t['progress']['check_state'].get(cid) != 'pass'
                     and cid not in {c for m in phase.get('milestones', []) for c in m['checks']}]
            newly += advanced
            for cid, r in results.items():
                t['progress']['check_state'][cid] = r['outcome']
            if failed or (verdict and verdict['verdict'] != 'supported'):
                lines = ['%s: %s — observed %s%s' % (cid, r['outcome'], json.dumps(r.get('observed'), ensure_ascii=False)[:600],
                                                    (' (' + r['note'] + ')') if r.get('note') else '')
                         for cid, r in failed.items()]
                if verdict and verdict['verdict'] != 'supported':
                    lines.append('review: ' + verdict['reasons'][:800])
                t['inbox']['feedback'].append('You claimed phase %s done, but the checks disagree:\n' % phase['id'] + '\n'.join(lines))
                if newly:
                    self.store.log(tid, 'checks_advanced')
                    t['progress']['no_progress'] = 0
                self.store.save(t)
            else:
                t['plan']['phases'][t['phase_index']]['status'] = 'done'
                t['plan']['phases'][t['phase_index']]['verified'] = bool(checks) or bool(verdict)
                t['phase_index'] += 1
                t['progress']['no_progress'] = 0
                t['flags']['fresh'] = True
                self.store.log(tid, 'phase_done', phase=phase['id'], checks=results)
                if t['phase_index'] >= len(t['plan']['phases']):
                    t['state'] = 'verifying'
                else:
                    self.send(t, messages.say('phase_done', self._goal(t), phase=phase['title']))
                self.store.save(t)
                return
        if not newly:
            self._diagnostic_progress(tid, phase, report, evidence, novelty)
        self._maybe_replan(tid)

    # ------------------------------------------------------------------ checks and reviews

    def _ctx(self, tid, t):
        return CheckContext(self.task_work_dir(t), t['started_ts'], self.read_paths,
                            self.store.task_dir(tid) / 'evidence')

    def _baselines(self, t, checks):
        path = self.store.task_dir(t['id']) / 'baselines.json'
        data = read_json(path, {}) or {}
        ctx = self._ctx(t['id'], t)
        for c in checks:
            checker = self.checkers.get(c['type'])
            if checker and c['id'] not in data:
                try:
                    b = checker.baseline(c['params'], ctx)
                except Exception as e:
                    b = {'error': str(e)[:300]}
                if b is not None:
                    data[c['id']] = b
        if data:
            atomic_json(path, data)

    def _system_check_problem(self, task, results):
        """Only deterministic framework faults, never ordinary missing business output."""
        checks = {c['id']: c for c in task['checks']}
        for cid, result in results.items():
            if result.get('system_error'):
                return 'Checker %s cannot execute: %s' % (cid, result.get('note'))
            check = checks.get(cid, {})
            legacy_directory_gate = False
            if check.get('origin') == 'drafted' and check.get('scope') == 'phase':
                # Recognize legacy auto-generated gates without guessing from an ID prefix.
                legacy_directory_gate = any(
                    a.endswith(('/', '\\'))
                    and cid == 'phase_' + hashlib.sha256((p['id'] + ':' + a).encode()).hexdigest()[:20]
                    and check.get('params', {}).get('pattern') == a
                    and check.get('params', {}).get('kind', 'file') == 'file'
                    for p in (task.get('plan') or {}).get('phases', []) for a in p.get('produces', []))
            mismatches = (result.get('observed') or {}).get('kind_mismatches', []) if isinstance(result.get('observed'), dict) else []
            if legacy_directory_gate and result['outcome'] != 'pass' and any(
                    m.get('actual') == 'directory' and m.get('expected') == 'file' for m in mismatches):
                return 'Generated artifact check %s has an incompatible path kind: %s; repair with check_updates' % (
                    cid, json.dumps(mismatches, ensure_ascii=False))
        return None

    def run_checks(self, tid, checks, claim='', step_evidence=()):
        if not checks:
            return {}
        self.store.activity.record(tid, 'checks_started')
        started = time.monotonic()
        with self.store.lock(tid):
            t = self.store.load(tid)
        baselines = read_json(self.store.task_dir(tid) / 'baselines.json', {}) or {}
        ctx = self._ctx(tid, t)
        results, program = {}, {}
        batched = {}
        deterministic = [c for c in checks if c['type'] != 'llm_review']
        # Only adjacent compatible checks are batched: plugin ordering is preserved.
        index = 0
        while index < len(deterministic):
            first = deterministic[index]
            checker = self.checkers.get(first['type'])
            key = checker.batch_key(first['params']) if checker and hasattr(checker, 'batch_key') else None
            group = [first]
            index += 1
            while key is not None and index < len(deterministic):
                other = deterministic[index]
                candidate = self.checkers.get(other['type'])
                if not candidate or not hasattr(candidate, 'batch_key') or candidate.batch_key(other['params']) != key:
                    break
                group.append(other)
                index += 1
            if key is not None and len(group) > 1:
                try:
                    batched.update(checker.check_many(group, ctx))
                except Exception as e:
                    # Do not retry a failed batch N times or hide a checker framework error.
                    for c in group:
                        batched[c['id']] = {'outcome': 'cannot_check', 'observed': None, 'layer': checker.layer,
                                            'note': str(e)[:300], 'system_error': True}
        for c in checks:
            if c['type'] == 'llm_review':
                continue
            checker = self.checkers.get(c['type'])
            try:
                if checker is None:
                    raise ValueError('checker is unavailable: ' + c['type'])
                r = batched[c['id']] if c['id'] in batched else checker.check(c['params'], ctx, baselines.get(c['id'])).to_dict()
            except Exception as e:
                r = {'outcome': 'cannot_check', 'observed': None, 'layer': checker.layer if checker else 'file',
                     'note': str(e)[:300], 'system_error': True}
            r.update(type=c['type'], origin=c.get('origin', 'requester'), observed_by='executor_node',
                     version=checker.version if checker else None)
            results[c['id']] = program[c['id']] = r
        self._timing(tid, 'checks', time.monotonic() - started, checks=len(program))
        for c in checks:
            if c['type'] != 'llm_review':
                continue
            files = self._review_files(c['params'].get('evidence') or [], self.task_work_dir(t)) or list(step_evidence)
            verdict = (self._review(tid, c['params']['question'], claim, files, program, judge_claim=False)
                       if self.reviewer else None)
            if verdict is None:
                r = {'outcome': 'deferred', 'observed': None, 'layer': 'model',
                     'note': 'no reviewer on this node; needs the requester\'s confirmation'}
            else:
                r = {'outcome': 'pass' if verdict['verdict'] == 'supported' else 'fail',
                     'observed': verdict, 'layer': 'model', 'note': None}
            r.update(type='llm_review', origin=c.get('origin', 'requester'), observed_by='executor_node', version=1)
            results[c['id']] = r
        self.store.log(tid, 'checks', results=results)
        return results

    def _review_files(self, globs, work_dir=None):
        work_dir = Path(work_dir or self.work_dir)
        out = []
        for g in globs:
            for p in sorted(work_dir.glob(g)):
                if p.is_file():
                    out.append(str(p.relative_to(work_dir)))
        return out[:50]

    def _evidence_key(self, question, files, judge_claim, claim, work_dir=None):
        work_dir = Path(work_dir or self.work_dir)
        h = hashlib.sha256((question + '\x00' + (claim if judge_claim else '')).encode('utf-8'))
        for f in sorted(files):
            p = work_dir / f
            h.update(f.encode('utf-8'))
            h.update((sha256_file(p, 512 * 1024 * 1024) or 'large') .encode() if p.is_file() else b'missing')
        return h.hexdigest()[:32]

    def _review(self, tid, question, claim, files, program, judge_claim=True):
        """A verdict from the reviewer. The same question on the same evidence (by content) is answered once:
        reviewers are not deterministic, and one fact should not get two judgements."""
        work_dir = self.task_work_dir(self.store.load(tid))
        key = self._evidence_key(question, files, judge_claim, claim, work_dir)
        with self.store.lock(tid):
            t = self.store.load(tid)
            cached = (t.get('review_cache') or {}).get(key)
            if cached:
                return cached
            if t['used']['reviews'] >= self.policy['max_reviews']:
                return None
            t['used']['reviews'] += 1
            self.store.save(t)
        images = [work_dir / f for f in files if f.lower().endswith(('.png', '.jpg', '.jpeg'))][:self.policy['review_images']]
        prompt = prompts.review_prompt(question, claim, files, program, judge_claim=judge_claim)
        if not self.reviewer.caps.get('schema'):
            prompt += '\n\n' + schema_instructions(REVIEW_SCHEMA)
        started = time.monotonic()
        with execution(self.store.activity, tid, 900, purpose='review'):
            r = self.reviewer.run(prompt + '\n\n' + prompts.resource_rules('background'), work_dir=work_dir, timeout=900, schema=REVIEW_SCHEMA, mode='review',
                                  images=images if self.reviewer.caps.get('images') else ())
        self._timing(tid, 'review', time.monotonic() - started)
        obj = extract_json(r.text) if r.ok else None
        verdict = obj if obj is not None and conforms(obj, REVIEW_SCHEMA) else None
        with self.store.lock(tid):
            t = self.store.load(tid)
            t['used']['seconds'] += r.duration
            if verdict is not None and verdict['verdict'] != 'unclear':
                cache = t.setdefault('review_cache', {})
                cache[key] = verdict
                if len(cache) > 200:
                    cache.pop(next(iter(cache)))
            self.store.save(t)
        self.store.log(tid, 'review', question=question[:500], files=files[:20], verdict=verdict,
                       ok=r.ok, error=r.error, judge_claim=judge_claim)
        if verdict is None:
            return {'verdict': 'unclear', 'reasons': 'the reviewer gave no usable answer (%s)' % (r.error or 'invalid'), 'missing': []}
        return verdict

    def _latest_checks(self, tid):
        results = {}
        for entry in self.store.read_log(tid, 'checks'):
            results.update(entry.get('results') or {})
        return results

    def _rewind_verification(self, t, failed):
        """Keep proven earlier phases; repair the first phase owning a failed final check."""
        phases = (t.get('plan') or {}).get('phases') or []
        if not phases or not failed:
            return False
        target = len(phases) - 1
        for i, phase in enumerate(phases):
            if set(phase.get('final_checks', phase.get('checks', []))) & set(failed):
                target = i
                break
        for phase in phases[target:]:
            phase['status'] = 'pending'
            phase['verified'] = False
        t['phase_index'] = target
        t['flags']['fresh'] = True
        feedback = 'Final verification failed:\n' + '\n'.join(
            '%s: %s — observed %s%s' % (cid, result['outcome'],
                json.dumps(result.get('observed'), ensure_ascii=False)[:600],
                (' (' + str(result['note']) + ')') if result.get('note') else '')
            for cid, result in failed.items())
        if feedback not in t['inbox']['feedback']:
            t['inbox']['feedback'].append(feedback)
        return True

    def _resume_verification(self, t):
        """An explicit requester continuation grants a bounded new attempt, not a history reset.

        Legacy 1.2.5 blocked records have no block_kind and point beyond the last phase.
        Their existing check journal is sufficient to find the repair phase safely.
        """
        if t['state'] != 'blocked':
            return
        if native.is_native(t):
            if t.get('block_kind') == 'verification':
                t['verification_retry_base'] = t['used']['verify_rounds']
            return
        phases = (t.get('plan') or {}).get('phases') or []
        legacy = bool(phases and t.get('phase_index', 0) >= len(phases))
        if t.get('block_kind') != 'verification' and not legacy:
            return
        latest = self._latest_checks(t['id'])
        failed = {}
        for check in t['checks']:
            if check.get('scope', 'final') != 'final':
                continue
            result = latest.get(check['id']) or {'outcome': t['progress']['check_state'].get(check['id'], 'not_run')}
            if result['outcome'] not in ('pass', 'deferred'):
                failed[check['id']] = result
        if self._rewind_verification(t, failed):
            t['verification_retry_base'] = t['used']['verify_rounds']
            self.store.log(t['id'], 'verification_resumed', total_rounds=t['used']['verify_rounds'],
                           repair_phase=phases[t['phase_index']]['id'], failed=list(failed))

    def _verify(self, tid):
        with self.store.lock(tid):
            t = self.store.load(tid)
            checks = [c for c in t['checks'] if c.get('scope', 'final') == 'final']
            steps = self.store.read_log(tid, 'step')
            step_evidence = sorted({e['path'] for s in steps for e in s.get('evidence', []) if e.get('exists')})[-30:]
            owner = {}
            for p in t['plan']['phases']:
                for cid in p.get('final_checks', p['checks']):
                    owner.setdefault(cid, p['id'])
        notes, phase_evidence = {}, {}
        for s in steps:
            if (s.get('report') or {}).get('status') == 'claims_done':
                notes[s.get('phase')] = s['report'].get('summary', '')
                # The evidence of the claim that finished the phase, as the phase review saw it.
                phase_evidence[s.get('phase')] = [e['path'] for e in s.get('evidence', []) if e.get('exists')]
        results = {}
        for c in checks:
            ph = owner.get(c['id'])
            results.update(self.run_checks(tid, [c], claim=notes.get(ph, ''),
                                           step_evidence=phase_evidence.get(ph) or step_evidence))
        failed = {cid: r for cid, r in results.items() if r['outcome'] not in ('pass', 'deferred')}
        with self.store.lock(tid):
            t = self.store.load(tid)
            if t['state'] in FINAL:
                return
            problem = self._system_check_problem(t, results)
            if problem:
                self._block(t, problem, kind='checker_error', results=results)
                return
            t['used']['verify_rounds'] += 1
            for cid, r in results.items():
                t['progress']['check_state'][cid] = r['outcome']
            if not failed:
                if lightweight.is_light(t) or native.is_native(t):
                    t['plan']['phases'][0].update(status='done', verified=True)
                self._complete(t, results)
                return
            if native.is_native(t):
                native.verification_failed(self, t, results)
                return
            if lightweight.is_light(t):
                lightweight.verification_failed(self, t, results)
                return
            # Rewind BEFORE blocking so a continuation executes a repair instead of
            # falling directly back into _verify with the identical files.
            self._rewind_verification(t, failed)
            attempts = t['used']['verify_rounds'] - t.get('verification_retry_base', 0)
            if attempts >= self.policy['max_verify_rounds']:
                self._block(t, messages.reason('verification_failed', self._goal(t)),
                            kind='verification', results=results)
                return
            t['state'] = 'running'
            self.store.save(t)

    # ------------------------------------------------------------------ endings

    def report(self, t, results=None):
        steps = self.store.read_log(t['id'], 'step')
        evidence = {}
        for s in steps:
            for e in s.get('evidence', []):
                if e.get('exists'):
                    evidence[e['path']] = {'path': e['path'], 'sha256': e.get('sha256'), 'size': e.get('size')}
        last = steps[-1].get('report') if steps and steps[-1].get('report') else {}
        latest = self._latest_checks(t['id'])
        latest.update(results or {})
        checks = []
        for c in t['checks']:
            r = latest.get(c['id'], {'outcome': t['progress']['check_state'].get(c['id'], 'not_run')})
            checks.append({'id': c['id'], 'type': c['type'], 'origin': c.get('origin', 'requester'),
                           'scope': c.get('scope', 'final'),
                           'covers': c.get('covers'), 'outcome': r.get('outcome'), 'layer': r.get('layer'),
                           'observed_by': 'executor_node', 'observed': r.get('observed'), 'note': r.get('note')})
        return {'protocol': PROTOCOL, 'task': t['id'], 'state': t['state'], 'stop_reason': t.get('stop_reason'),
                'block_reason': t.get('block_reason'), 'block_kind': t.get('block_kind'),
                'blocker': t.get('blocker'), 'plan_revision': t.get('plan_revision', 0),
                'partial': t['state'] != 'completed',
                'verification_retry_base': t.get('verification_retry_base', 0),
                'goal': t['spec']['goal'], 'summary': last.get('summary', ''),
                'orchestration': t['spec'].get('orchestration', 'planned'),
                'routing_reason': t['spec'].get('routing_reason'), 'native': facts.native_view(t.get('native')),
                'provider':self._provider_view(t),
                'timing': t.get('timing', {}), 'milestone_receipts': t['progress'].get('milestones', {}),
                'phases': [{'id': p['id'], 'title': p['title'], 'status': p.get('status'),
                            'verified': p.get('verified', False), 'checks': p['checks'],
                            'milestones': p.get('milestones', []),
                            'final_checks': p.get('final_checks', []), 'produces': p.get('produces', []),
                            'requires': p.get('requires', [])} for p in (t.get('plan') or {}).get('phases', [])],
                'checks': checks, 'drafted_checks': [c['id'] for c in t['checks'] if c.get('origin') == 'drafted'],
                'deferred': [c['id'] for c in checks if c['outcome'] == 'deferred'],
                'evidence': sorted(evidence.values(), key=lambda e: e['path'])[:200],
                'used': t['used'], 'sessions_used': t.get('sessions_used', 0),
                'started_at': t['created_at'], 'finished_at': utcnow(),
                'trust_note': 'Check results were observed by the executor node and are self-reported from the '
                              'requester\'s point of view; verify what matters on your side.'}

    def _attach(self, t, rep):
        path = self.store.task_dir(t['id']) / 'report.json'
        atomic_json(path, rep)
        if self.artifacts and t['state'] == 'completed':
            return self.artifacts.prepare(self, t, rep, path)
        return self._attach_local(t, rep, path)

    def _attach_local(self, t, rep, path):
        # Snapshot actual result files, not just report.json. Unique wire names keep
        # different result directories from overwriting each other at the receiver.
        import shutil
        files = [path]
        failed = [c for c in rep['checks'] if c.get('scope', 'final') == 'final' and c['outcome'] not in ('pass', 'deferred')]
        if failed:
            diagnostics = self.store.task_dir(t['id']) / 'verification-failures.json'
            atomic_json(diagnostics, {'task': t['id'], 'state': t['state'], 'checks': failed,
                        'phases': rep['phases'], 'used': t['used'], 'observed_by': 'executor_node'})
            files.append(diagnostics)
        names = {e['path'] for e in rep.get('evidence', [])}
        for c in rep.get('checks', []):
            observed = c.get('observed') or {}
            if isinstance(observed, dict):
                names.update(m['path'] for m in observed.get('matches', []) if 'path' in m)
        ctx = CheckContext(self.task_work_dir(t), 0, ())
        dest = self.store.task_dir(t['id']) / 'delivery'
        dest.mkdir(exist_ok=True)
        remaining = 1_000_000 - sum(p.stat().st_size for p in files)
        omitted = []
        for name in sorted(names):
            try:
                source = ctx.resolve(name, allow_read_paths=False)
                if not source.is_file():
                    continue
                size = source.stat().st_size
                if size > remaining:
                    omitted.append({'path': name, 'size': size, 'sha256': sha256_file(source)})
                    continue
                target = dest / (hashlib.sha256(name.encode()).hexdigest()[:12] + '-' + source.name)
                shutil.copyfile(source, target)
                if sha256_file(source) != sha256_file(target):
                    raise OSError('result changed while preparing delivery: ' + name)
                files.append(target)
                remaining -= size
            except ValueError:
                continue  # never attach paths outside the authorized work directory
            except OSError as error:
                # A missing/changing result must not suppress the diagnostic report.
                # Its failure is visible in the report; it was not delivered.
                rep.setdefault('attachment_errors', []).append({'path': name, 'error': str(error)[:300]})
                self.store.log(t['id'], 'attachment_failed', path=name, error=str(error)[:300])
        if omitted:
            rep['attachment_omitted'] = omitted
        if rep.get('attachment_errors') or omitted:
            atomic_json(path, rep)
        manifest = self.store.task_dir(t['id']) / 'delivery-manifest.json'
        atomic_json(manifest, {'schema': 2, 'task': t['id'], 'execution_state': t['state'],
                    'message_state': 'not_sent', 'prepared':
                    [{'name': p.name, 'size': p.stat().st_size, 'sha256': sha256_file(p)} for p in files],
                    'not_transmitted': omitted,
                    'receipt_file': 'delivery-receipt.json',
                    'note': 'Prepared is not transmitted. Provider RPC acknowledgement is not requester file verification.'})
        files.append(manifest)
        return files

    def _complete(self, t, results):
        if t['state'] in FINAL:
            return
        t['state'] = 'completed'
        rep = self.report(t, results)
        t['result'] = {k: rep[k] for k in ('summary', 'deferred', 'drafted_checks')}
        goal = self._goal(t)
        if rep['deferred']:
            text = messages.say('completed_deferred', goal, n=len(rep['deferred']), items=', '.join(rep['deferred']),
                                summary=rep['summary'][:1500])
        else:
            text = messages.say('completed', goal, summary=rep['summary'][:1500])
        self.store.log(t['id'], 'completed', report=rep)
        # Persist business completion before attachment preparation or network
        # delivery. A crash now can recover delivery, never execute the Goal again.
        if self.artifacts:self.artifacts.outbox.enroll(t,rep,text)
        try:
            self.store.save(t)
            self.send(t, text, files=self._attach(t, rep), report=self._short(rep))
            self.store.save(t)
        finally:
            if self.artifacts:self.artifacts.outbox.release(t['id'])

    def _short(self, rep):
        return {'summary': rep['summary'][:1000], 'checks': [{k: c[k] for k in ('id', 'outcome', 'layer', 'origin')}
                                                             for c in rep['checks'] if c.get('scope', 'final') == 'final'],
                'deferred': rep['deferred'], 'drafted_checks': rep['drafted_checks']}

    def _stop(self, t, why):
        if t.get('provider_step_intent'):
            evidence = ((t['provider_step_intent'].get('execution_result') or {}).get('harness_evidence') or {})
            if evidence.get('native_terminal_verified') is True:
                t.pop('provider_step_intent',None)
                self.store.save(t)
                self.resources.release_retained(t['id'])
            else:
                t['provider_stop_requested'] = why
                self._retain_provider(t)
                self._block(t,'Stop requested; provider native effect remains unverified',kind='provider_incomplete')
                return
        if t['state'] in FINAL:
            return
        t['state'] = 'stopped'
        t['stop_reason'] = why
        goal = self._goal(t)
        rep = self.report(t)
        self.store.log(t['id'], 'stopped', reason=why, report=rep)
        files = self._attach(t, rep)
        if why == 'plan_failed':
            diagnostics = self.store.task_dir(t['id']) / 'planning-diagnostics.json'
            atomic_json(diagnostics, {'task': t['id'], 'reason': why,
                        'invalid_plans': self.store.read_log(t['id'], 'plan_invalid')[-10:],
                        'existing_plan': t.get('plan'), 'known_check_ids': [c['id'] for c in t['checks']],
                        'observed_by': 'executor_node'})
            files.append(diagnostics)
        if why == 'canceled':
            # A cancellation carries no text or files on the wire; the report stays on this node.
            self.send(t, messages.say('canceled', goal), reason=why)
        else:
            done = len([p for p in rep['phases'] if p['status'] == 'done'])
            text = messages.say('stopped_budget', goal, reason=messages.reason(why, goal), done=done,
                                total=len(rep['phases']))
            # failed carries text only, so the report goes first in a progress message.
            self.send(t, text, state='working', files=files, reason=why, report=self._short(rep))
            self.send(t, text, reason=why)
        self.store.save(t)

    def _send_blocked_report(self, t, results=None):
        text = messages.say('blocked', self._goal(t), reason=t.get('block_reason') or '')
        rep = self.report(t, results)
        failed = [c for c in rep['checks'] if c.get('scope', 'final') == 'final' and c['outcome'] not in ('pass', 'deferred')]
        if failed:
            text += '\n\nFailed checks: ' + '; '.join('%s: %s%s' % (
                c['id'], c['outcome'], ' (' + str(c['note'])[:200] + ')' if c.get('note') else '') for c in failed[:20])
        try:
            files = self._attach(t, rep)
            manifest = self.store.task_dir(t['id']) / 'delivery-manifest.json'
            files = [p for p in files if Path(p).resolve() != manifest.resolve()]
            diagnostics = self.store.task_dir(t['id']) / 'orchestration-diagnostics.json'
            atomic_json(diagnostics, {'task': t['id'], 'blocker': t.get('blocker'), 'used': t['used'],
                        'plan_revision': t.get('plan_revision', 0), 'plan': t.get('plan'), 'checks': t['checks'],
                        'latest_results': results or self._latest_checks(t['id']),
                        'plan_errors': self.store.read_log(t['id'], 'plan_invalid')[-10:],
                        'observed_by': 'executor_node', 'business_acceptance': 'not_verified'})
            files.append(diagnostics)
            manifest = self.store.task_dir(t['id']) / 'delivery-manifest.json'
            atomic_json(manifest, {'task': t['id'], 'state': 'blocked', 'partial': True,
                        'files': [{'name': p.name, 'size': p.stat().st_size, 'sha256': sha256_file(p)} for p in files],
                        'observed_by': 'executor_node', 'business_acceptance': 'not_verified'})
            files.append(manifest)
        except (OSError, ValueError) as error:
            self.store.log(t['id'], 'blocked_report_failed', error=str(error)[:300])
            files = []
            text += '\nDiagnostic files could not be prepared; they are not delivered.'
        # input-required can carry attachments. Keep the actual blocked execution state
        # and put report+partial artifacts on this SAME reply, not a lost prior status.
        return self.send(t, text, files=files, reason=t.get('block_reason'), report=self._short(rep), plan=t.get('plan'), checks=t['checks'])

    def _block(self, t, reason, kind=None, results=None):
        if t['state'] in FINAL:
            return
        t['state'] = 'blocked'
        t['block_reason'] = reason
        t['block_kind'] = kind
        t['blocker'] = blocker(kind, reason)
        t['blocker']['evidence'] = [e['path'] for e in self.report(t, results).get('evidence', [])][:20]
        self.store.log(t['id'], 'blocked', reason=reason)
        self.store.save(t)
        self._send_blocked_report(t, results)
        self.store.save(t)

    def _apply_revision(self, t):
        """Called outside resource leases, after the current step has fully returned."""
        proposal = t.pop('pending_revision')
        plan = proposal['plan']
        index = proposal['from_index']
        new_checks = plan.pop('new_checks')
        self.store.log(t['id'], 'plan_superseded', plan=t.get('plan'), revision=t.get('plan_revision', 0))
        invalidated = {c for p in (t.get('plan') or {}).get('phases', [])[index:]
                       for c in p['checks'] + p.get('final_checks', []) + p.get('preflight', [])}
        invalidated.update(c for p in plan['phases'][index:]
                           for c in p['checks'] + p.get('final_checks', []) + p.get('preflight', []))
        for change in proposal.get('check_updates', []):
            t['checks'] = [deepcopy(change['after']) if c['id'] == change['id'] else c for c in t['checks']]
            self.store.log(t['id'], 'check_replaced', request_id=proposal['request_id'], **change)
        for cid in invalidated:
            t['progress']['check_state'].pop(cid, None)
        self.store.log(t['id'], 'checks', results={cid: {'outcome': 'not_run', 'note': 'invalidated by plan revision'} for cid in invalidated})
        t['checks'] += [c for c in new_checks if c['id'] not in {x['id'] for x in t['checks']}]
        self._baselines(t, new_checks)
        t.update(plan=plan, phase_index=index, session=None, block_reason=None, block_kind=None, blocker=None,
                 question=None, plan_revision=t.get('plan_revision', 0) + 1)
        t['used']['plan_revisions'] = t['used'].get('plan_revisions', 0) + 1
        t['progress']['no_progress'] = 0
        t['progress'].pop('plan_errors', None)
        t['progress'].pop('plan_error_baseline', None)
        t['flags']['fresh'] = True
        t['inbox']['feedback'].append('Plan revised: ' + proposal['reason'] + '. Recheck all affected outputs.')
        reasons = plan.get('approval_reasons') or []
        t['state'] = 'awaiting_approval' if reasons or t['spec']['plan_approval'] == 'always' else 'running'
        if t['state'] == 'awaiting_approval':
            t['question'] = 'approve_plan'
            t['approval_reasons'] = reasons
        t['revision_requests'][proposal['request_id']].update(state='applied', revision=t['plan_revision'])
        self.store.log(t['id'], 'plan_revised', request_id=proposal['request_id'], revision=t['plan_revision'],
                       invalidated=sorted(invalidated), plan=plan)
        self.store.save(t)
        self.send(t, '计划修订已应用；历史、累计预算和原验收要求保留。', plan=plan)
        self.store.save(t)

    # ------------------------------------------------------------------ checkpoint

    def checkpoint(self, t, limit=6000):
        """What is known so far, built from the records alone (no model): phase states, observed facts,
        ruled-out attempts, the latest check results."""
        lines = []
        for p in (t.get('plan') or {}).get('phases', []):
            lines.append('Phase %s %s: %s' % (p['id'], p.get('status', 'pending'), p['title']))
        facts, ruled, latest = [], [], ''
        for s in self.store.read_log(t['id'], 'step'):
            rep = s.get('report')
            if not rep:
                continue
            latest = rep.get('summary', '')
            for f in rep.get('facts', []):
                if f.get('basis') == 'observed':
                    facts.append(f['statement'])
            for a in rep.get('attempts', []):
                if a.get('outcome') == 'failed':
                    ruled.append('%s → %s' % (a['what'], a.get('observed', '')))
        if facts:
            lines.append('Observed facts:')
            lines += ['- ' + f for f in list(dict.fromkeys(facts))[-20:]]
        if ruled:
            lines.append('Ruled out (do not repeat):')
            lines += ['- ' + r for r in list(dict.fromkeys(ruled))[-20:]]
        checks = t['progress'].get('check_state', {})
        if checks:
            lines.append('Latest check outcomes: ' + ', '.join('%s=%s' % kv for kv in sorted(checks.items())))
        if latest:
            lines.append('Latest step summary: ' + latest)
        text = '\n'.join(lines)
        return text if len(text) <= limit else text[-limit:]

"""Content-free execution observations. Heartbeats never count as activity or progress."""
from __future__ import annotations

from contextlib import contextmanager
import threading
import time

from .execution_facts import clean_execution_facts
from .store import atomic_json, read_json, safe_name

_local = threading.local()
EVENTS = frozenset({'accepted', 'planning', 'running', 'verifying', 'awaiting_approval',
    'needs_user', 'blocked', 'completed', 'stopped', 'executor_started', 'executor_output',
    'executor_finished', 'checks_started', 'checks', 'checks_advanced', 'phase_done',
    'plan', 'step', 'review', 'replan', 'environment_wait', 'interrupted', 'milestone_done', 'diagnostic_progress'})
PROGRESS = frozenset({'checks_advanced', 'phase_done', 'milestone_done', 'diagnostic_progress'})


def emit(event):
    callback = getattr(_local, 'callback', None)
    if callback:
        callback(event)


@contextmanager
def execution(activity, task_id, timeout, purpose='execution'):
    previous = getattr(_local, 'callback', None)
    activity.record(task_id, 'executor_started', timeout=timeout, purpose=purpose)
    _local.callback = lambda event: activity.record(task_id, event)
    try:
        yield
    finally:
        _local.callback = previous
        activity.record(task_id, 'executor_finished', purpose=purpose)


class Activity:
    def __init__(self, root):
        self.root = root / 'activity'
        self.root.mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()

    def load(self, tid):
        try:
            data = read_json(self.root / (safe_name(tid) + '.json'), {})
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def state(self, tid, state):
        with self.lock:
            if self.load(tid).get('state') != state:
                self.record(tid, state, state=state)

    def record(self, tid, event, timeout=None, state=None, purpose=None):
        try:
            self._record(tid, event, timeout, state, purpose)
        except (OSError, ValueError):
            # Observability must not turn a successful business operation into a failed task.
            pass

    def _record(self, tid, event, timeout=None, state=None, purpose=None):
        if event not in EVENTS:
            return
        with self.lock:
            data = self.load(tid)
            now = time.time()
            # Output is activity, never acceptance progress. Bound disk writes and timeline size.
            if event == 'executor_output' and data.get('last_event') == event and now - data.get('last_activity_at', 0) < 5:
                return
            data.update(last_activity_at=now, last_event=event)
            if purpose in ('planning', 'execution', 'routine', 'report_repair', 'review', 'native_goal'):
                data['last_purpose'] = purpose
            if state:
                data['state'] = state
            if event in PROGRESS:
                data.update(last_progress_at=now, progress_event=event)
            if event == 'executor_started':
                data['execution'] = {'started_at': now, 'deadline_at': now + timeout, 'purpose': purpose}
            elif event == 'executor_finished':
                data.pop('execution', None)
            history = data.get('events', [])
            if history and history[-1]['event'] == event and history[-1].get('purpose') == purpose:
                history[-1]['at'] = now
            else:
                history.append({'event': event, 'at': now, 'purpose': purpose})
            data['events'] = history[-20:]
            atomic_json(self.root / (safe_name(tid) + '.json'), data)


def snapshot(engine, quick, now=None):
    """All counts cover ANet work only; detailed text is filtered by the connector's sync switch."""
    now = time.time() if now is None else now
    counts = dict(running=0, queued=0, waiting=0, blocked=0)
    tasks = []
    # A copy avoids iterating a worker dictionary while a worker removes itself.
    with engine.guard:
        workers = dict(engine.workers)
    for tid in engine.store.ids():
        t = engine.store.load(tid)
        if not t or t['state'] in ('completed', 'stopped'):
            continue
        state = t['state']
        scheduling = engine.resources.status(tid)
        bucket = ('waiting' if state in ('awaiting_approval', 'needs_user') else
                  'blocked' if state == 'blocked' else
                  'queued' if scheduling and scheduling['state'].startswith('waiting_') else
                  'running' if tid in workers and workers[tid].is_alive() else 'queued')
        counts[bucket] += 1
        a = engine.store.activity.load(tid)
        phases = (t.get('plan') or {}).get('phases', [])
        idx = t.get('phase_index', 0)
        phase = {'index': min(idx + 1, len(phases)), 'total': len(phases)} if phases else None
        if phase and idx < len(phases):
            phase['title'] = phases[idx]['title']
            from .milestones import view
            phase['milestone'] = view(t, phases[idx])
        task = dict(a, id=tid, mode='long', state=state if bucket != 'queued' else 'queued',
                    goal=t['spec']['goal'], phase=phase, started_at=t.get('started_ts'),
                    reason=t.get('question') or t.get('block_reason') or '',
                    no_progress_steps=t.get('progress', {}).get('no_progress', 0))
        native = t.get('native') or {}
        route = t['spec'].get('orchestration')
        reason = t['spec'].get('routing_reason')
        if isinstance(reason, str) and reason.startswith('native_probe_failed'):
            reason = 'native_probe_failed'
        stop = native.get('stop') or {}
        facts = {'schema': 1, 'route': route, 'routing_reason': reason,
                 'goal_status': native.get('goal_status'), 'turn_status': native.get('turn_status'),
                 'last_event': native.get('last_event'), 'iteration': native.get('iteration'),
                 'updated_at': native.get('observed_at'), 'retry': native.get('retry')}
        if route == 'native':
            facts['stop'] = {'requested': bool(t.get('flags', {}).get('cancel') or t.get('native_pause') or native.get('stop_requested') or stop),
                             'resource_retained': bool(t.get('native_quarantine'))}
            if type(stop.get('confirmed')) is bool:
                facts['stop']['confirmed'] = stop['confirmed']
        task['execution_facts'] = clean_execution_facts(facts, now)
        task['scheduling'] = scheduling
        task['last_step'] = t.get('last_step')
        task['blocker'] = t.get('blocker')
        task['plan_revision'] = t.get('plan_revision', 0)
        task['revision_pending'] = bool(t.get('pending_revision'))
        task['timing'] = t.get('timing', {})
        if scheduling and scheduling['state'].startswith('waiting_'):
            task['state'] = scheduling['state']
        # Old records have no observations: do not substitute updated_at (also touched by keepalive).
        task['attention'] = bool(bucket == 'running' and (
            task['no_progress_steps'] >= engine.policy['no_progress_steps'] or
            now > (a.get('execution') or {}).get('deadline_at', float('inf')) + 30))
        tasks.append(task)
    if quick:
        with quick.lock:
            pending, executing = set(quick.running), set(quick.executing)
        for tid in pending:
            bucket = 'running' if tid in executing else 'queued'
            counts[bucket] += 1
            a = engine.store.activity.load('quick-' + tid)
            tasks.append(dict(a, id=tid, mode='quick', state=bucket,
                              execution_facts={'schema': 1, 'route': 'quick'},
                              scheduling=quick.resources.status('quick-' + tid),
                              attention=bool(now > (a.get('execution') or {}).get('deadline_at', float('inf')) + 30)))
            scheduling = tasks[-1]['scheduling']
            if scheduling and scheduling['state'].startswith('waiting_'):
                tasks[-1]['state'] = scheduling['state']
    tasks.sort(key=lambda t: (t['state'] not in ('needs_user', 'awaiting_approval', 'blocked'),
                              -t.get('last_activity_at', 0)))
    state = next((k for k in ('running', 'waiting', 'blocked', 'queued') if counts[k]), 'idle')
    return {'schema': 1, 'source': 'taskd', 'scope': 'anet_tasks', 'observed_at': now,
            'state': state, 'counts': counts, 'total': sum(counts.values()),
            'tasks': tasks[:50], 'truncated': len(tasks) > 50}

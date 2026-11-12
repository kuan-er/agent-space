"""One scheduler for quick replies and long-task phases. Waiters hold no execution slots.

Desktop access is cooperative: executor prompts must obey their phase resource declaration.
The lease covers the executor AND its checks/reviews, including screenshots.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
import time

from .store import private_dir, safe_name

DESKTOP_CHECKS = frozenset({'uia_property', 'uia_values', 'window_screenshot'})


def desktop_enabled(config):
    return 'windows_desktop' in config.get('probes', [])


def capacity(config):
    slots = int(config.get('concurrency', 0))
    if slots <= 0:
        return None
    # Before phase scheduling, --gui unconditionally forced concurrency=1. Migrate that
    # generated value; new installs stamp scheduler_version and can explicitly retain one slot.
    if desktop_enabled(config) and config.get('scheduler_version', 1) < 2 and slots == 1:
        return None
    return slots


def workspace(root, tid, isolate=True, mode='long'):
    root = Path(root).resolve()
    if not isolate:
        return root
    path = root / 'taskd-work' / mode / safe_name(tid)
    # Do not follow a pre-existing task-directory symlink outside the selected root.
    if not path.resolve().is_relative_to(root):
        raise ValueError('task workspace escapes the configured work directory')
    return private_dir(path)


def resource_for(task, desktop_enabled):
    spec = task['spec']
    requested = spec.get('resource', 'auto')
    state = task['state']
    phases = (task.get('plan') or {}).get('phases', [])
    index = task.get('phase_index', 0)
    phase = phases[index] if index < len(phases) else None
    checks = task.get('checks', [])
    if state == 'running' and phase:
        checks = [c for c in checks if c['id'] in phase.get('checks', []) + phase.get('preflight', [])]
    if any(c['type'] in DESKTOP_CHECKS for c in checks) and state != 'planning':
        return 'desktop'
    if state in ('accepted', 'planning'):
        return 'background'  # planning is file inspection only; never inspect the desktop
    if state == 'running' and phase:
        if phase.get('requires_desktop'):
            return 'desktop'
        return 'background' if requested == 'background' else phase.get('resource', 'desktop' if desktop_enabled else 'background')
    return 'desktop' if requested == 'desktop' else 'background'


class Resources:
    def __init__(self, slots=None):
        self.capacity = max(1, int(slots)) if slots is not None else None
        self.condition = threading.Condition(threading.RLock())
        self.owners = {}
        self.waiters = {}
        self.quarantine = {}

    def retain(self, tid, resource, work_dir):
        """An uncertain native stop must not release a shared desktop/workspace."""
        with self.condition:
            self.quarantine[tid] = dict(resource=resource, workspace=str(Path(work_dir).resolve()))

    def release_retained(self, tid):
        with self.condition:
            self.quarantine.pop(tid, None)
            self.condition.notify_all()

    def _blockers(self, request):
        # Desktop/workspace contention takes precedence over generic capacity.
        for resource, predicate in (
            ('waiting_desktop', lambda other: request['resource'] == other['resource'] == 'desktop'),
            ('waiting_workspace', lambda other: self._workspace_conflict(request['workspace'], other['workspace'])),
        ):
            owners = [tid for tid, other in dict(self.quarantine, **self.owners).items()
                      if tid != request.get('task_id') and predicate(other)]
            if owners:
                return resource, owners
        if self.capacity is not None and len(self.owners) >= self.capacity:
            return 'waiting_slot', list(self.owners)
        return None, []

    @staticmethod
    def _workspace_conflict(first, second):
        first, second = Path(first), Path(second)
        return first == second or first in second.parents or second in first.parents

    def status(self, tid):
        with self.condition:
            if tid in self.owners:
                return dict(state='executing', resource=self.owners[tid]['resource'], blocked_by=[])
            if tid not in self.waiters:
                return None
            request = self.waiters[tid]
            state, owners = self._blockers(request)
            return dict(state=state or 'waiting_slot', resource=request['resource'], blocked_by=owners,
                        position=list(self.waiters).index(tid) + 1, since=request['since'])

    @contextmanager
    def lease(self, tid, resource, work_dir, cancel=None, stopping=None, on_wait=None, abort_wait=None):
        acquired = False
        with self.condition:
            self.waiters[tid] = dict(task_id=tid, resource=resource, workspace=str(Path(work_dir).resolve()), since=time.time())
        try:
            if on_wait and self.status(tid).get('blocked_by'):
                on_wait(self.status(tid))
            with self.condition:
                while True:
                    if abort_wait and abort_wait():
                        break
                    if any(event and event.is_set() for event in (cancel, stopping)):
                        break
                    # FIFO among eligible requests; a desktop waiter cannot block background work.
                    eligible = next((key for key, req in self.waiters.items() if not self._blockers(req)[0]), None)
                    if eligible == tid:
                        self.owners[tid] = self.waiters.pop(tid)
                        acquired = True
                        break
                    self.condition.wait(.1)
            yield acquired
        finally:
            with self.condition:
                self.waiters.pop(tid, None)
                self.owners.pop(tid, None)
                self.condition.notify_all()

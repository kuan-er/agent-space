"""Shared admission barrier and conservative durable update-safety checks.

Lock order: admission gate -> engine scheduling guard / quick lock / task store locks.
An updater closes the gate, draining already-entered admission callbacks, before it
checks for live workers. Closing never waits for a business task to finish: a live
worker makes the updater defer. Ordinary work never holds this gate for execution.
"""
from __future__ import annotations

import threading
import json
from .protocol import ACTIVE, STATES


class AdmissionGate:
    def __init__(self):
        self.lock = threading.RLock()
        self.held = threading.Event()

    def set_hold(self, on):
        with self.lock:
            (self.held.set if on else self.held.clear)()

    def run(self, fn, *args, **kwargs):
        with self.lock:
            if self.held.is_set():
                return None
            return fn(*args, **kwargs)


def task_update_safe(task):
    """Waiting is durable, but an unconfirmed external execution is not idle."""
    if not isinstance(task, dict) or task.get('state') not in STATES:
        return False
    if task['state'] in ACTIVE or task.get('step_in_progress') or task.get('native_quarantine'):
        return False
    n = task.get('native') or {}
    if not isinstance(n, dict):
        return False
    spec = task.get('spec') or {}
    if not isinstance(spec, dict):
        return False
    is_native = spec.get('orchestration') == 'native' or bool(n)
    if not is_native:
        return True
    if not task.get('session'):
        return not n  # no session is safe only before ANY external execution observation
    stop = n.get('stop') or {}
    observed = stop.get('observed') or {} if isinstance(stop, dict) else {}
    if (isinstance(stop, dict) and isinstance(observed, dict)
            and n.get('last_event') == 'native/stopped' and n.get('stop_requested') is True
            and stop.get('confirmed') is True and observed.get('thread_status') == 'idle'
            and observed.get('active_turn_ids') == []):
        return True
    # A completed native Goal/turn is terminal observation, unlike a blocked or
    # canceled task with an old completed turn or a bare stop ACK.
    return (task['state'] == 'completed' and n.get('goal_status') == 'complete'
            and n.get('turn_status') == 'completed' and not n.get('stop_requested'))


def store_update_safe(store):
    return all(task_update_safe(store.load(tid)) or _verified_legacy_stop(store, tid)
               for tid in store.ids())


def _verified_legacy_stop(store, tid):
    """Narrow compatibility for two pre-1.9.4 stops; never add inferred state."""
    if tid not in {'ix_7f6d5f6005da8c3f50ab2473bde0a583',
                   'ix_98437028f23d1f525567ae1529887a4c'}:
        return False
    t = store.load(tid)
    n = t.get('native') or {}
    stop = n.get('stop') or {}
    if not (t.get('state') == 'stopped' and not t.get('step_in_progress')
            and not t.get('native_quarantine') and n.get('last_event') == 'native/stopped'
            and n.get('goal_status') == 'paused' and n.get('turn_status') == 'failed'
            and n.get('stop_requested') is None and stop.get('confirmed') is True
            and stop.get('reason') == 'canceled' and 'observed' not in stop):
        return False
    try:
        events = [json.loads(line) for line in
                  (store.task_dir(tid) / 'log.jsonl').read_text(encoding='utf-8').splitlines()]
    except (OSError, ValueError):
        return False
    matches = [e for e in events if e.get('event') == 'native_event'
               and e.get('method') == 'native/stopped'
               and (e.get('data') or {}).get('confirmed') is True
               and (e.get('data') or {}).get('reason') == 'canceled']
    return bool(matches and matches[-1].get('ts') == n.get('updated_at')
                and matches[-1]['ts'] < '2026-11-13T00:00:00Z')

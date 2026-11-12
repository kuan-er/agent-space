import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from taskd import builtin  # noqa: E402
from taskd.engine import Engine  # noqa: E402
from taskd.executors.base import ExecResult, Executor  # noqa: E402
from taskd.protocol import PLAN_SCHEMA, REPORT_SCHEMA, REVIEW_SCHEMA, parse_status  # noqa: E402
from taskd.store import Store  # noqa: E402


class Outbox:
    def __init__(self):
        self.sent = []
        self.fail = False

    def reply(self, task_id, text, state, files=None):
        if self.fail:
            raise RuntimeError('network down')
        self.sent.append({'task': task_id, 'text': text, 'state': state,
                          'files': [Path(f).name for f in files or []],
                          'env': parse_status(text) if text else None})

    def states(self, task_id=None):
        return [s['state'] for s in self.sent if task_id is None or s['task'] == task_id]

    def envs(self):
        return [s['env'] for s in self.sent if s['env']]


def plan(phases, drafted=(), reasons=()):
    return {'phases': [dict({'id': 'p%d' % (i + 1), 'title': 'phase %d' % (i + 1), 'done_when': 'done %d' % (i + 1),
                             'checks': [], 'preflight': [], 'produces': [], 'requires': [], 'final_checks': [],
                             'resource': 'background', 'requires_desktop': False, 'milestones': []}, **p) for i, p in enumerate(phases)],
            'drafted_checks': [dict({'covers': ''}, **d) for d in drafted], 'approval_reasons': list(reasons), 'notes': ''}


def report(status='working', evidence=(), summary='did a thing', question=None, blocker=None,
           facts=(), artifacts=(), outcome='worked'):
    return {'phase': 'p1', 'status': status, 'summary': summary,
            'attempts': [{'what': 'tried', 'outcome': outcome, 'observed': 'saw', 'evidence': list(evidence)}],
            'facts': list(facts), 'artifacts': list(artifacts), 'next': 'more', 'blocker': blocker,
            'question': question}


class Scripted(Executor):
    """An executor driven by a function of (kind, prompt, call) -> dict or str or ExecResult."""
    name = 'scripted'

    def __init__(self, fn, resume=True, schema=True):
        self.fn = fn
        self.caps = {'resume': resume, 'assign_session': False, 'schema': schema, 'images': False}
        self.calls = []
        self.lock = threading.Lock()
        self.sessions = 0

    def run(self, prompt, *, work_dir, timeout, schema=None, session=None, mode='task', images=(), cancel=None):
        kind = 'plan' if schema is PLAN_SCHEMA else 'review' if schema is REVIEW_SCHEMA else 'step'
        with self.lock:
            call = {'kind': kind, 'prompt': prompt, 'session': session, 'mode': mode, 'n': len(self.calls)}
            self.calls.append(call)
            if session is None and mode == 'task':
                self.sessions += 1
                sid = 's%d' % self.sessions
            else:
                sid = session
        out = self.fn(kind, prompt, call)
        if isinstance(out, ExecResult):
            return out
        text = out if isinstance(out, str) else json.dumps(out)
        return ExecResult(True, text, sid if mode == 'task' else None, duration=1.0)

    def kinds(self):
        return [c['kind'] for c in self.calls]


def make_engine(tmp, executor, reviewer=None, policy=None, **config):
    work = Path(tmp) / 'work'
    work.mkdir(exist_ok=True)
    store = Store(Path(tmp) / 'state')
    out = Outbox()
    cfg = dict({'work_dir': str(work), 'isolate_tasks': False,
                'policy': dict({'keepalive_seconds': 600}, **(policy or {}))}, **config)
    checkers = builtin.make_checkers()
    e = Engine(config=cfg, store=store, outbox=out, executor=executor, reviewer=reviewer,
               checkers=checkers, capabilities={'protocol': 'taskd/1'})
    return e, out, store, work


def wait_for(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def tmpdir():
    return tempfile.mkdtemp(prefix='taskd-test-')

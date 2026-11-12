"""The checker interface. A checker observes something itself; it never takes the executor's word."""
from __future__ import annotations

from pathlib import Path


class CheckContext:
    """What a checker may touch: the task's work directory, extra read-only directories the node's
    owner allowed, the time the task started, and a private directory for evidence it collects."""

    def __init__(self, work_dir, task_start, read_paths=(), evidence_dir=None):
        self.work_dir = Path(work_dir).resolve()
        self.task_start = task_start
        self.read_paths = [Path(p).resolve() for p in read_paths]
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None

    def resolve(self, value, *, allow_read_paths=True):
        """A path named by a task: relative to the work directory, or absolute inside it or inside a
        directory the owner allowed. Anything else is refused."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError('a path is required')
        p = Path(value)
        if any(part == '..' for part in p.parts):
            raise ValueError('paths may not contain ..')
        p = (p if p.is_absolute() else self.work_dir / p).resolve()
        roots = [self.work_dir] + (self.read_paths if allow_read_paths else [])
        for root in roots:
            if p == root or root in p.parents:
                return p
        raise ValueError('%s is outside the work directory and the allowed read paths' % value)


class CheckResult:
    def __init__(self, outcome, observed=None, layer='observed', note=None):
        if outcome not in ('pass', 'fail', 'cannot_check', 'needs_review'):
            raise ValueError(outcome)
        self.outcome, self.observed, self.layer, self.note = outcome, observed, layer, note

    def to_dict(self):
        return {'outcome': self.outcome, 'observed': self.observed, 'layer': self.layer, 'note': self.note}


class Checker:
    name = 'base'
    version = 1
    platforms = ('any',)  # sys.platform prefixes this checker runs on
    layer = 'observed'    # 'file', 'observed' or 'model'
    doc = ''

    def validate(self, params):
        """The params a task may give, normalized; ValueError for anything else."""
        raise NotImplementedError

    def baseline(self, params, ctx):
        """State to record when the task starts, or None."""
        return None

    def check(self, params, ctx, baseline=None):
        raise NotImplementedError


def only(params, allowed, required=()):
    extra = set(params) - set(allowed)
    if extra:
        raise ValueError('unknown parameter(s): %s' % ', '.join(sorted(extra)))
    missing = [k for k in required if k not in params]
    if missing:
        raise ValueError('missing parameter(s): %s' % ', '.join(missing))
    return dict(params)

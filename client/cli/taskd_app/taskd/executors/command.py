"""The lowest-capability adapter: any local program that reads the whole task on standard input and
writes its final answer on standard output (exit status 0). No sessions, no schema enforcement."""
from __future__ import annotations

import os
from pathlib import Path

from ..platform import proc
from .base import ExecResult, Executor


class CommandExecutor(Executor):
    name = 'command'
    caps = {'resume': False, 'assign_session': False, 'schema': False, 'images': False}

    def __init__(self, command, args=(), env=None):
        if not command:
            raise ValueError('command executor needs a program path')
        self.argv = [str(command)] + [str(a) for a in args]
        self.extra_env = dict(env or {})

    def run(self, prompt, *, work_dir, timeout, schema=None, session=None, mode='task',
            images=(), cancel=None):
        env = dict(os.environ, **self.extra_env)
        env['TASKD_MODE'] = mode
        r = proc.run(self.argv, stdin_text=prompt, cwd=str(work_dir), env=env, timeout=timeout, cancel=cancel)
        text = r.stdout.strip()
        if r.ok and text:
            return ExecResult(True, text, None, duration=r.duration)
        error = 'timed out' if r.timed_out else 'canceled' if r.canceled else (
            'exit status %s' % r.code if r.code else 'empty answer')
        return ExecResult(False, text, None, error, r.duration, r.timed_out, r.canceled)

    def selftest(self, work_dir):
        if not Path(self.argv[0]).exists() and os.sep in self.argv[0]:
            return False, 'program not found: ' + self.argv[0]
        r = self.run('Reply with exactly: OK', work_dir=work_dir, timeout=120, mode='review')
        return (r.ok, r.text[:200] if r.ok else r.error)

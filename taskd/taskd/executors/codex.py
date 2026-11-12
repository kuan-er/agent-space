"""Codex CLI adapter (`codex exec`, `codex exec resume`).

Verified against codex-cli 0.166.0: the first --json event is {"type":"thread.started","thread_id":…};
`exec resume <id>` continues that session and takes --output-schema and -o, but not -s or -C, so the
sandbox mode is passed as `-c sandbox_mode=…` and the working directory as the process's cwd.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

from ..platform import proc
from .base import ExecResult, Executor

SANDBOXES = ('read-only', 'workspace-write', 'danger-full-access')


def thread_id(events):
    for line in events.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get('type') == 'thread.started' and isinstance(e.get('thread_id'), str):
            return e['thread_id']
    return None


class CodexExecutor(Executor):
    name = 'codex'
    caps = {'resume': True, 'assign_session': False, 'schema': True, 'images': True}

    def __init__(self, command=None, sandbox='workspace-write', model=None, codex_home=None, config=(),
                 native_goals=True, native_endpoint=None, native_token_file=None):
        self.command = command or shutil.which('codex') or 'codex'
        if sandbox not in SANDBOXES:
            raise ValueError('codex sandbox must be one of %s' % ', '.join(SANDBOXES))
        self.sandbox, self.model = sandbox, model
        self.codex_home = str(codex_home) if codex_home else None
        self.config = list(config)
        self.native_goals, self.native_endpoint = native_goals, native_endpoint
        self.native_token_file = native_token_file
        self._native_capability = None

    def native_capability(self):
        if self._native_capability is None:
            from .native_codex import probe
            self._native_capability = probe(self)
        return dict(self._native_capability)

    def run_goal(self, objective, **kwargs):
        from .native_codex import run
        return run(self, objective, **kwargs)

    def argv(self, *, work_dir, out_file, schema_file=None, session=None, mode='task', images=()):
        sandbox = 'read-only' if mode == 'review' else self.sandbox
        argv = [self.command, 'exec']
        if session:
            # Unquoted: a value that is not TOML is taken as a literal string (codex --help), and no quotes
            # have to survive the cmd.exe hop of an npm codex.cmd on Windows.
            argv += ['resume', session, '-c', 'sandbox_mode=' + sandbox]
        else:
            argv += ['-s', sandbox, '-C', str(work_dir)]
        argv += ['--json', '--skip-git-repo-check', '-o', str(out_file)]
        if mode == 'review':
            argv.append('--ephemeral')
        if schema_file:
            argv += ['--output-schema', str(schema_file)]
        if self.model:
            argv += ['-m', self.model]
        for item in self.config:
            argv += ['-c', item]
        for image in images:
            argv += ['-i', str(image)]
        return argv + ['-']

    def env(self):
        env = dict(os.environ)
        if self.codex_home:
            env['CODEX_HOME'] = self.codex_home
        return env

    def run(self, prompt, *, work_dir, timeout, schema=None, session=None, mode='task',
            images=(), cancel=None):
        with tempfile.TemporaryDirectory(prefix='taskd-codex-') as tmp:
            tmp = Path(tmp)
            out_file = tmp / 'last-message.txt'
            schema_file = None
            if schema is not None:
                schema_file = tmp / 'schema.json'
                schema_file.write_text(json.dumps(schema), encoding='utf-8')
            argv = self.argv(work_dir=work_dir, out_file=out_file, schema_file=schema_file,
                             session=session if mode == 'task' else None, mode=mode, images=images)
            r = proc.run(argv, stdin_text=prompt, cwd=str(work_dir), env=self.env(), timeout=timeout, cancel=cancel)
            sid = thread_id(r.stdout) or (session if mode == 'task' else None)
            text = out_file.read_text(encoding='utf-8', errors='replace').strip() if out_file.exists() else ''
        if r.ok and text:
            return ExecResult(True, text, sid, duration=r.duration)
        if r.timed_out:
            error = 'timed out'
        elif r.canceled:
            error = 'canceled'
        elif r.code:
            error = 'exit status %s' % r.code
        else:
            error = 'empty answer'
        return ExecResult(False, text, sid, error, r.duration, r.timed_out, r.canceled)

    def selftest(self, work_dir):
        schema = {'type': 'object', 'additionalProperties': False, 'required': ['ok'],
                  'properties': {'ok': {'type': 'boolean'}}}
        r = self.run('Answer with ok set to true. Do not run any commands.', work_dir=work_dir,
                     timeout=300, schema=schema, mode='review')
        if not r.ok:
            return False, 'codex did not answer (%s); check that it is installed and logged in%s' % (
                r.error, ' with CODEX_HOME=' + self.codex_home if self.codex_home else '')
        try:
            return json.loads(r.text).get('ok') is True, r.text[:200]
        except ValueError:
            return False, 'codex answered without the requested JSON'

    def describe(self):
        return {'name': self.name, 'caps': dict(self.caps), 'sandbox': self.sandbox,
                'separate_home': bool(self.codex_home)}

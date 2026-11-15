"""Checkers that work on every platform: files, value, dir_unchanged, llm_review."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from pathlib import Path

from .base import Checker, CheckResult, only

HASH_LIMIT = 64 * 1024 * 1024


def sha256_file(path, limit=None):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        read = 0
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            read += len(chunk)
            if limit is not None and read > limit:
                return None
    return h.hexdigest()


def _rel_pattern(pattern):
    p = Path(pattern)
    if any(part == '..' for part in p.parts):
        raise ValueError('patterns may not contain ..')
    return pattern


class FilesChecker(Checker):
    name = 'files'
    version = 2
    layer = 'file'
    doc = ('files: {"pattern": glob relative to the work dir, "count": minimum number of matches (default 1), '
           '"newer_than": "task_start" or omitted, "min_size": bytes (default 1 for files), '
           '"kind": "file" (default), "directory", or "any". Directories prove existence only, not file contents.}')

    def validate(self, params):
        p = only(params, ('pattern', 'count', 'newer_than', 'min_size', 'kind'), ('pattern',))
        if not isinstance(p['pattern'], str) or not p['pattern'].strip() or Path(p['pattern']).is_absolute():
            raise ValueError('pattern is a glob relative to the work directory')
        _rel_pattern(p['pattern'])
        if p.get('kind', 'file') not in ('file', 'directory', 'any'):
            raise ValueError('kind is file, directory or any')
        p.setdefault('count', 1)
        p.setdefault('min_size', 0 if p.get('kind') in ('directory', 'any') else 1)
        if p.get('kind') == 'directory' and p['min_size'] != 0:
            raise ValueError('directory checks require min_size=0; check contents with a file pattern')
        if not isinstance(p['count'], int) or p['count'] < 1:
            raise ValueError('count is a positive integer')
        if not isinstance(p['min_size'], int) or p['min_size'] < 0:
            raise ValueError('min_size is a non-negative integer')
        if p.get('newer_than') not in (None, 'task_start'):
            raise ValueError('newer_than is "task_start" or omitted')
        return p

    def check(self, params, ctx, baseline=None):
        matches = []
        mismatches = []
        kind = params.get('kind', 'file')
        for path in sorted(ctx.work_dir.glob(params['pattern'])):
            try:
                ctx.resolve(str(path), allow_read_paths=False)
            except ValueError:
                continue
            actual = 'directory' if path.is_dir() else 'file' if path.is_file() else None
            if actual is None:
                continue
            if kind != 'any' and kind != actual:
                mismatches.append({'path': str(path.relative_to(ctx.work_dir)), 'expected': kind, 'actual': actual})
                continue
            st = path.stat()
            if actual == 'file' and st.st_size < params['min_size']:
                continue
            if params.get('newer_than') == 'task_start' and st.st_mtime < ctx.task_start:
                continue
            matches.append({'path': str(path.relative_to(ctx.work_dir)), 'kind': actual,
                            'size': st.st_size if actual == 'file' else None,
                            'sha256': sha256_file(path, HASH_LIMIT) if actual == 'file' else None})
        outcome = 'pass' if len(matches) >= params['count'] else 'fail'
        return CheckResult(outcome, {'matches': matches[:20], 'count': len(matches),
                                     'kind_mismatches': mismatches[:20]}, self.layer)


OPS = ('==', '!=', '<', '<=', '>', '>=', '~=', 'contains')


def json_path(obj, path):
    for part in path.split('.') if path else []:
        if isinstance(obj, list):
            obj = obj[int(part)]
        elif isinstance(obj, dict):
            obj = obj[part]
        else:
            raise KeyError(part)
    return obj


def csv_value(path, expr):
    with open(path, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    if expr == 'rows':
        return len(rows)
    column, _, agg = expr.partition(':')
    values = [r[column] for r in rows if column in r]
    if agg == 'count':
        return len(values)
    if agg in ('first', 'last'):
        if not values:
            raise KeyError(column)
        return values[0] if agg == 'first' else values[-1]
    numbers = [float(v) for v in values if v not in ('', None)]
    if agg == 'sum':
        return sum(numbers)
    if agg in ('min', 'max'):
        if not numbers:
            raise KeyError(column)
        return min(numbers) if agg == 'min' else max(numbers)
    raise ValueError('unknown aggregate: ' + agg)


def compare(value, op, expect, tolerance):
    if op == 'contains':
        return isinstance(value, (str, list)) and expect in value
    if op in ('==', '!='):
        same = value == expect
        if not same and isinstance(value, (int, float)) and isinstance(expect, (int, float)):
            same = math.isclose(value, expect, rel_tol=0, abs_tol=1e-9)
        return same if op == '==' else not same
    value, expect = float(value), float(expect)
    return {'<': value < expect, '<=': value <= expect, '>': value > expect, '>=': value >= expect,
            '~=': abs(value - expect) <= tolerance}[op]


class ValueChecker(Checker):
    name = 'value'
    layer = 'file'
    doc = ('value: {"file": JSON or CSV file in the work dir, "path": JSON dot path (a.b.0.c) or for CSV '
           '"rows" or "<column>:<sum|min|max|count|first|last>", "op": one of %s, "expect": value, '
           '"tolerance": number for ~=}' % ', '.join(OPS))

    def validate(self, params):
        p = only(params, ('file', 'path', 'op', 'expect', 'tolerance'), ('file', 'path', 'expect'))
        if not isinstance(p['file'], str) or Path(p['file']).is_absolute():
            raise ValueError('file is relative to the work directory')
        _rel_pattern(p['file'])
        if not isinstance(p['path'], str):
            raise ValueError('path is a string')
        p.setdefault('op', '==')
        if p['op'] not in OPS:
            raise ValueError('op is one of ' + ', '.join(OPS))
        p.setdefault('tolerance', 0)
        if not isinstance(p['tolerance'], (int, float)) or p['tolerance'] < 0:
            raise ValueError('tolerance is a non-negative number')
        return p

    def check(self, params, ctx, baseline=None):
        try:
            path = ctx.resolve(params['file'], allow_read_paths=False)
        except ValueError as e:
            return CheckResult('cannot_check', None, self.layer, str(e))
        if not path.is_file():
            return CheckResult('fail', {'file': params['file'], 'exists': False}, self.layer)
        try:
            if path.suffix.lower() == '.csv':
                value = csv_value(path, params['path'])
            else:
                value = json_path(json.loads(path.read_text(encoding='utf-8')), params['path'])
        except (KeyError, IndexError, ValueError, TypeError) as e:
            return CheckResult('fail', {'file': params['file'], 'error': type(e).__name__}, self.layer)
        try:
            ok = compare(value, params['op'], params['expect'], params['tolerance'])
        except (TypeError, ValueError):
            ok = False
        return CheckResult('pass' if ok else 'fail', {'value': value, 'op': params['op'],
                           'expect': params['expect']}, self.layer)


class DirUnchangedChecker(Checker):
    name = 'dir_unchanged'
    layer = 'observed'
    doc = ('dir_unchanged: {"path": a directory inside the work dir or an allowed read path}; snapshot '
           'taken when the task starts, compared at verification')
    max_files = 200000

    def validate(self, params):
        p = only(params, ('path',), ('path',))
        if not isinstance(p['path'], str) or not p['path'].strip():
            raise ValueError('path is required')
        return p

    def manifest(self, root):
        out = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                full = Path(dirpath) / name
                try:
                    st = full.stat()
                except OSError:
                    continue
                digest = sha256_file(full, HASH_LIMIT) if st.st_size <= HASH_LIMIT else None
                out[str(full.relative_to(root))] = [st.st_size, st.st_mtime_ns, digest]
                if len(out) > self.max_files:
                    raise ValueError('more than %d files' % self.max_files)
        return out

    def baseline(self, params, ctx):
        root = ctx.resolve(params['path'])
        if not root.is_dir():
            return {'missing': True}
        return {'files': self.manifest(root)}

    def check(self, params, ctx, baseline=None):
        if not baseline:
            return CheckResult('cannot_check', None, self.layer, 'no snapshot was taken at the start')
        try:
            root = ctx.resolve(params['path'])
        except ValueError as e:
            return CheckResult('cannot_check', None, self.layer, str(e))
        if baseline.get('missing'):
            return CheckResult('pass' if not root.exists() else 'fail', {'created': root.exists()}, self.layer)
        if not root.is_dir():
            return CheckResult('fail', {'removed': True}, self.layer)
        now, before = self.manifest(root), baseline['files']
        changed = sorted(k for k in now.keys() & before.keys() if now[k] != before[k]
                         and not (now[k][2] and now[k][2] == before[k][2] and now[k][0] == before[k][0]))
        added = sorted(now.keys() - before.keys())
        removed = sorted(before.keys() - now.keys())
        ok = not (changed or added or removed)
        return CheckResult('pass' if ok else 'fail', {'changed': changed[:20], 'added': added[:20],
                           'removed': removed[:20], 'files': len(now)}, self.layer)


class LLMReviewChecker(Checker):
    """Asks the node's reviewer, in a separate read-only session; the engine runs it."""
    name = 'llm_review'
    layer = 'model'
    doc = ('llm_review: {"question": what the reviewer must decide, "evidence": [globs in the work dir] '
           '(optional; the step evidence is used otherwise)}')

    def validate(self, params):
        p = only(params, ('question', 'evidence'), ('question',))
        if not isinstance(p['question'], str) or not p['question'].strip():
            raise ValueError('question is required')
        p.setdefault('evidence', [])
        if not isinstance(p['evidence'], list) or not all(isinstance(x, str) for x in p['evidence']):
            raise ValueError('evidence is a list of globs')
        for g in p['evidence']:
            if Path(g).is_absolute():
                raise ValueError('evidence globs are relative to the work directory')
            _rel_pattern(g)
        return p

    def check(self, params, ctx, baseline=None):
        return CheckResult('needs_review', None, self.layer)


GENERIC = (FilesChecker, ValueChecker, DirUnchangedChecker, LLMReviewChecker)

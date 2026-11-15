"""Durable state: one directory per task holding task.json (replaced atomically) and log.jsonl
(append only), and one file per task a requester is tracking."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time

SAFE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$')


def utcnow():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)  # where there are no POSIX modes this only clears read-only
    except OSError:
        pass
    return path


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_name('.%s.%d.tmp' % (path.name, os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Some systems refuse to replace a file another process has open for a moment.
            if attempt == 19:
                raise
            time.sleep(0.05)


def read_json(path, default=None):
    for attempt in range(20):
        try:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        except FileNotFoundError:
            return default
        except PermissionError:
            # Some systems deny an open while another thread/process replaces the
            # state file. Retry just as the writer does; permanent denial still fails.
            if attempt == 19:
                raise
            time.sleep(0.05)


def safe_name(task_id):
    if SAFE.fullmatch(task_id) and '..' not in task_id:
        return task_id
    return 'h_' + hashlib.sha256(task_id.encode('utf-8')).hexdigest()[:32]


class Store:
    def __init__(self, root):
        self.root = private_dir(root)
        self.tasks = private_dir(self.root / 'tasks')
        self.tracked = private_dir(self.root / 'tracked')
        self._locks = {}
        self._guard = threading.Lock()
        from .activity import Activity
        self.activity = Activity(self.root)

    def lock(self, task_id):
        with self._guard:
            return self._locks.setdefault(task_id, threading.RLock())

    def task_dir(self, task_id):
        return self.tasks / safe_name(task_id)

    def load(self, task_id):
        return read_json(self.task_dir(task_id) / 'task.json')

    def save(self, task):
        task['updated_at'] = utcnow()
        private_dir(self.task_dir(task['id']))
        atomic_json(self.task_dir(task['id']) / 'task.json', task)
        self.activity.state(task['id'], task['state'])

    def log(self, task_id, event, **fields):
        self.activity.record(task_id, event)
        record = dict(fields, ts=utcnow(), event=event)
        path = private_dir(self.task_dir(task_id)) / 'log.jsonl'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        return record

    def read_log(self, task_id, event=None):
        path = self.task_dir(task_id) / 'log.jsonl'
        out = []
        try:
            with open(path, encoding='utf-8') as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue  # a line cut by a crash is skipped, never repaired
                    if event is None or r.get('event') == event:
                        out.append(r)
        except FileNotFoundError:
            pass
        return out

    def ids(self):
        out = []
        for d in self.tasks.iterdir():
            t = read_json(d / 'task.json')
            if t:
                out.append(t['id'])
        return out

    def tail_log(self, task_id, limit=20, max_bytes=128 * 1024):
        """Bounded diagnostic query; never scan an entire long experiment's log."""
        try:
            with open(self.task_dir(task_id) / 'log.jsonl', 'rb') as f:
                size = f.seek(0, os.SEEK_END)
                f.seek(max(0, size - max_bytes))
                data = f.read(max_bytes)
        except FileNotFoundError:
            return []
        lines = data.splitlines()
        if size > max_bytes:
            lines = lines[1:]
        out = []
        for line in lines:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    out.append(value)
            except (ValueError, UnicodeDecodeError):
                pass
        return out[-limit:]

    # --- requester side ---

    def load_tracked(self, task_id):
        return read_json(self.tracked / (safe_name(task_id) + '.json'))

    def save_tracked(self, record):
        record['updated_at'] = utcnow()
        atomic_json(self.tracked / (safe_name(record['task_id']) + '.json'), record)

    def tracked_ids(self):
        return [r['task_id'] for r in (read_json(p) for p in self.tracked.glob('*.json')) if r]

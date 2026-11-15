"""ANet transport (ANet 0.3.9): the node's own MCP server (`anet mcp`) and CLI.

taskd answers inbound tasks with MCP reply_task, whose state is working (a status, or a progress
message), input-required, completed (signs the receipt), failed, rejected or canceled
(internal/daemon/tasks_api.go replyTask). It does not use auto-reply: auto-reply must be off on an
executor node, or both would answer. A requester that hears nothing for 15 minutes fails a task as
no_response, which the engine's keepalive prevents.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import mimetypes
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

from ..attachments import descriptors
from ..platform.proc import background_options

TERMINAL = {'completed', 'failed', 'canceled', 'rejected'}
MAX_REPLY_BODY_BYTES = 1_500_000


class ReplyTooLarge(ValueError):
    """The reply was rejected locally before any ANet call was made."""


def norm_state(value):
    s = (value or '').lower()
    if s.startswith('task_state_'):
        s = s[len('task_state_'):]
    return s.replace('_', '-')


def iso_ts(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def message_text(m):
    return '\n'.join(p['text'] for p in (m or {}).get('parts', []) if isinstance(p.get('text'), str))


def message_files(m):
    return [p.get('filename') or 'file' for p in (m or {}).get('parts', []) if 'raw' in p or 'url' in p]


def role_of(m):
    r = (m or {}).get('role', '')
    return 'requester' if r == 'ROLE_USER' else 'provider' if r == 'ROLE_AGENT' else r


class MCPError(RuntimeError):
    pass


class MCP:
    """One `anet mcp` process: newline-delimited JSON-RPC over stdio, one call at a time."""

    def __init__(self, argv, env, client='taskd'):
        self.proc = subprocess.Popen(argv + ['mcp'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env, text=True, encoding='utf-8', bufsize=1,
                                     **background_options())
        self.responses = queue.Queue()
        self.serial = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        self.rpc('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                               'clientInfo': {'name': client, 'version': '1'}})
        self._write({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.tools = {t['name'] for t in self.rpc('tools/list', {})['tools']}

    def _read(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if line:
                    try:
                        self.responses.put(json.loads(line))
                    except ValueError:
                        continue
        finally:
            self.responses.put(EOFError('anet mcp exited'))

    def _write(self, payload):
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + '\n')
        self.proc.stdin.flush()

    def rpc(self, method, params, timeout=60):
        with self.lock:
            self.serial += 1
            serial = self.serial
            self._write({'jsonrpc': '2.0', 'id': serial, 'method': method, 'params': params})
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError('anet mcp did not answer %s' % method)
                try:
                    value = self.responses.get(timeout=left)
                except queue.Empty:
                    raise TimeoutError('anet mcp did not answer %s' % method) from None
                if isinstance(value, Exception):
                    raise MCPError(str(value))
                if value.get('id') != serial:
                    continue
                if 'error' in value:
                    raise MCPError('%s: %s' % (method, value['error'].get('message', value['error'])))
                return value['result']

    def call(self, name, args, timeout=60):
        r = self.rpc('tools/call', {'name': name, 'arguments': args}, timeout=timeout)
        text = ' '.join(c.get('text', '') for c in r.get('content', []) if c.get('type') == 'text')
        if r.get('isError'):
            raise MCPError('%s failed: %s' % (name, text[:500]))
        if isinstance(r.get('structuredContent'), dict):
            return r['structuredContent']
        try:
            return json.loads(text)
        except ValueError:
            raise MCPError('%s returned no JSON' % name) from None

    def alive(self):
        return self.proc.poll() is None

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class AnetTransport:
    name = 'anet'

    def __init__(self, anet=None, data_dir=None, log=None):
        self.anet = anet or shutil.which('anet') or 'anet'
        self.env = dict(os.environ)
        if data_dir:
            self.env['ANET_DATA_DIR'] = str(data_dir)
        self.log = log
        self._mcp = None
        self._lock = threading.Lock()

    # --- plumbing ---

    def cli(self, *args, timeout=60):
        r = subprocess.run([self.anet, *args], env=self.env, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout, **background_options())
        if r.returncode:
            raise RuntimeError('anet %s failed: %s' % (args[0], r.stderr.strip()[:500]))
        out = r.stdout.strip()
        return json.loads(out) if out.startswith(('{', '[')) else out

    def mcp(self):
        with self._lock:
            if self._mcp is None or not self._mcp.alive():
                self._mcp = MCP([self.anet], self.env)
            return self._mcp

    def call(self, name, args, timeout=60):
        try:
            return self.mcp().call(name, args, timeout=timeout)
        except (MCPError, TimeoutError, OSError, BrokenPipeError):
            with self._lock:
                if self._mcp:
                    self._mcp.close()
                self._mcp = None
            raise

    def waiter(self):
        """A separate MCP connection for long waits, so they do not hold up replies."""
        return MCP([self.anet], self.env, client='taskd-wait')

    def close(self):
        with self._lock:
            if self._mcp:
                self._mcp.close()
                self._mcp = None

    # --- node ---

    def status(self):
        return self.cli('status', '--json')

    def ensure_up(self):
        """Start the node when it is not running (after a reboot, or a crash)."""
        try:
            s = self.status()
            if s.get('aid'):
                return s
        except Exception:
            pass
        self.cli('up', timeout=120)
        return self.status()

    def autoreply_config(self):
        try:
            return json.loads(self.config_path().read_text(encoding='utf-8')).get('auto_reply') or {}
        except (OSError, ValueError):
            return {}

    def autoreply_on(self):
        """Whether auto-reply is configured; it would answer tasks too (`autoreply off` removes the block,
        module/autoreply/autoreply_control.go)."""
        return bool(self.autoreply_config())

    def config_path(self):
        return Path(self.env.get('ANET_DATA_DIR') or Path.home() / '.anet') / 'config.json'

    # --- the provider side ---

    def list_inbound(self, page_size=50):
        out = self.call('list_tasks', {'role': 'provider', 'page_size': page_size, 'history_length': 1})
        return [self.summary(t) for t in out.get('tasks', out.get('items', []))]

    def summary(self, t):
        m = t.get('metadata', {})
        hist = t.get('history') or []
        latest = (t.get('status') or {}).get('message') or (hist[-1] if hist else None)
        return {'id': t['id'], 'peer': m.get('anet.peer_aid'), 'state': norm_state((t.get('status') or {}).get('state')),
                'ts': iso_ts((t.get('status') or {}).get('timestamp')),
                'latest': {'id': (latest or {}).get('messageId'), 'role': role_of(latest),
                           'text': message_text(latest)} if latest else None,
                'seq': m.get('anet.state_seq')}

    def get(self, task_id, history=100):
        """One task with its history (the CLI reads it whole, without the MCP size bound)."""
        t = self.cli('task', 'get', task_id, '--full', '--history', str(history), timeout=60)
        m = t.get('metadata', {})
        msgs = []
        history = list(t.get('history') or [])
        current = (t.get('status') or {}).get('message')
        # ANet keeps the current working/status reply outside history until it is
        # superseded. It is real received data too; do not hide live progress.
        if current and not any(x.get('messageId') == current.get('messageId') for x in history):
            history.append(current)
        for x in history:
            msgs.append({'id': x.get('messageId'), 'role': role_of(x), 'text': message_text(x),
                         'files': message_files(x), 'attachments': descriptors(x)})
        return {'id': t['id'], 'peer': m.get('anet.peer_aid'), 'role': m.get('anet.role'),
                'state': norm_state((t.get('status') or {}).get('state')), 'messages': msgs,
                'delivery': {'state': m.get('anet.delivery'), 'detail': m.get('anet.delivery_detail'),
                             'since': m.get('anet.delivery_since'), 'wait': m.get('anet.delivery_wait'),
                             'effect_status': m.get('anet.effect_status'), 'receipt_verified': m.get('anet.receipt_verified')},
                'seq': m.get('anet.state_seq'), 'raw': t}

    def pull(self, task_id, out_dir):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        return self.cli('pull', task_id, '--out', str(out_dir), timeout=300)

    def reply(self, task_id, text, state, files=None):
        args = {'task_id': task_id, 'state': state}
        if text:
            args['text'] = text
        if files:
            estimated = len((text or '').encode('utf-8')) + 4096
            estimated += sum(4 * ((Path(f).stat().st_size + 2) // 3) + len(Path(f).name.encode('utf-8')) + 256
                             for f in files)
            if estimated > MAX_REPLY_BODY_BYTES:
                raise ReplyTooLarge('ANet reply exceeds local encoded-body budget')
            args['files'] = [{'name': Path(f).name, 'media_type': mimetypes.guess_type(str(f))[0] or 'application/octet-stream',
                              'content_base64': base64.b64encode(Path(f).read_bytes()).decode('ascii')} for f in files]
        if len(json.dumps(args, ensure_ascii=False).encode('utf-8')) > MAX_REPLY_BODY_BYTES:
            raise ReplyTooLarge('ANet reply exceeds local encoded-body budget')
        return self.call('reply_task', args, timeout=90)

    # --- the requester side ---

    def list_outbound(self, page_size=50):
        out = self.call('list_tasks', {'role': 'requester', 'page_size': page_size, 'history_length': 1})
        return [self.summary(t) for t in out.get('tasks', out.get('items', []))]

    def send_new(self, to, text):
        t = self.call('send_message', {'to': to, 'text': text, 'return_immediately': True, 'history_length': 0}, timeout=90)
        return t['id']

    def send(self, task_id, text):
        return self.call('send_message', {'task_id': task_id, 'text': text, 'return_immediately': True,
                                          'history_length': 0}, timeout=90)

    def node_aid(self):
        return self.call('node_status', {}).get('aid')

"""The quick channel: an ordinary (non-JSON) message is answered with one executor call, the way ANet's
auto-reply answers it, so a node that runs taskd still answers a plain question in seconds. A message
that is a taskd/1 JSON spec is a long task and goes to the engine. A requester can ask for either:

  plain text                              -> quick (node setting plain_text: 'quick', the default)
  {"taskd":"submit","mode":"quick",...}   -> quick
  {"taskd":"submit", ...}                 -> long task
"""
from __future__ import annotations

import logging
from pathlib import Path
import threading
import time

from .messages import CJK
from .maintenance import AdmissionGate
from .protocol import parse_message
from .store import atomic_json, read_json, safe_name, utcnow
from .activity import execution
from .resources import Resources, capacity, workspace
from .prompts import resource_rules
from .protocol import status_line

TERMINAL = {'completed', 'failed', 'canceled', 'rejected'}
MAX_REPLIES = 30          # the same bound ANet puts on one interaction
HISTORY = 20
DEFAULT_TIMEOUT = 300

SAY = {
    'working': ('正在处理。', 'Working on it.'),
    'failed': ('这次没能完成:{reason}', 'Could not finish this time: {reason}'),
    'cap': ('这个会话的快速回复已达上限({n} 条),请新开一个任务。', 'This conversation reached its quick-reply limit ({n}); start a new task.'),
}


def _say(key, sample, **values):
    return SAY[key][0 if CJK.search(sample or '') else 1].format(**values)


def message_text(text):
    """The words to answer: the goal of a quick-mode spec, otherwise the text itself."""
    kind, payload = parse_message(text)
    if kind == 'submit' and isinstance(payload.get('goal'), str):
        return payload['goal']
    return payload if kind == 'text' else ''


class QuickResponder:
    def __init__(self, executor, transport, store, work_dir, config, inputs_root, log=None, accepts_peer=None,
                 long_task_active=None, resources=None, stopping=None, admission=None):
        self.executor, self.transport, self.work_dir = executor, transport, work_dir
        self.config, self.inputs_root = config, Path(inputs_root)
        self.root = Path(store.root) / 'quick'
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = log or logging.getLogger('taskd')
        self.accepts_peer = accepts_peer or (lambda peer: True)
        self.running, self.lock = set(), threading.Lock()
        self.admission = admission or AdmissionGate()
        self.cancel_events = {}
        self.executing = set()
        self.activity = store.activity
        self.resources = resources or Resources(capacity(config))
        self.stopping = stopping or threading.Event()
        self.timeout = int(config.get('quick_timeout_s') or DEFAULT_TIMEOUT)
        from .quick_durable import DurableQuick
        self.durable = DurableQuick(self)

    # -- routing --------------------------------------------------------------------------------

    def _path(self, tid):
        return self.root / (safe_name(tid) + '.json')

    def wants(self, text):
        kind, payload = parse_message(text)
        if kind == 'text':
            return self.config.get('plain_text', 'quick') == 'quick'
        return kind == 'submit' and payload.get('taskd') == 'submit' and payload.get('mode') == 'quick'

    def offer(self, summary, record):
        """One inbound task summary. Returns True when this task is a quick one (this loop handles it),
        False when the long-task engine should. record is the engine's record of the task, if any."""
        tid = summary['id']
        if record is not None:
            return False
        state = read_json(self._path(tid), None)
        if state is not None and summary.get('state') in ('canceled', 'failed', 'rejected'):
            with self.lock:
                event = self.cancel_events.get(tid)
                if event:
                    event.set()
            if self.durable.pending_for(tid):
                self._spawn(tid)
            return True
        latest = summary.get('latest') or {}
        if state is None:
            if latest.get('role') != 'requester' or summary.get('state') in TERMINAL:
                return False
            full = self.transport.get(tid)
            mine = [m for m in full['messages'] if m['role'] == 'requester']
            if not mine or not self.wants(mine[0]['text']):
                return False
            peer = full.get('peer') or summary.get('peer')
            if not self.accepts_peer(peer):
                return False  # the engine rejects it in its own words
            state = {'id': tid, 'peer': peer, 'seen': [], 'replies': 0, 'created_at': utcnow(),
                     'work_dir': str(workspace(self.work_dir, tid, self.config.get('isolate_tasks', True), 'quick'))}
            atomic_json(self._path(tid), state)
        if self.durable.pending_for(tid) or (latest.get('role') == 'requester' and latest.get('id') not in state['seen']):
            self._spawn(tid)
        return True

    def _spawn(self, tid):
        return self.admission.run(self._spawn_admitted, tid)

    def _spawn_admitted(self, tid):
        with self.lock:
            if tid in self.running:
                return
            self.running.add(tid)
            self.cancel_events[tid] = threading.Event()
        threading.Thread(target=self._answer, args=(tid,), daemon=True, name='quick-' + tid[-6:]).start()

    # -- answering ------------------------------------------------------------------------------

    def _prompt(self, messages, tid):
        turns = [(m['role'] == 'requester', message_text(m['text']) if m['role'] == 'requester' else (m['text'] or ''))
                 for m in messages[-HISTORY:]]
        turns = [(theirs, text) for theirs, text in turns if text.strip()]
        sample = next((text for theirs, text in reversed(turns) if theirs), '')
        zh = bool(CJK.search(sample))
        names = ('对方', '我') if zh else ('them', 'me')
        head = ('以下是你和对方的对话,最新一条在最后。直接给出对最后一条消息的最终答复,不要复述对话,不要提到这些说明。'
                if zh else 'Below is your conversation with the other side, newest last. Reply with the final answer to the '
                           'last message only; do not repeat the conversation or mention these instructions.')
        body = '\n'.join('[%s] %s' % (names[0 if theirs else 1], text) for theirs, text in turns)
        inputs = self.inputs_root / safe_name(tid)
        tail = (('\n\n(对方附带的文件在 %s)' if zh else '\n\n(files from the other side are in %s)') % inputs) if inputs.is_dir() else ''
        return head + '\n\n' + body + tail, sample

    def _reply(self, tid, text, state):
        try:
            self.transport.reply(tid, text, state)
            return True
        except Exception as e:
            self.log.warning('quick reply to %s failed: %s', tid, e)
            return False

    def _answer(self, tid):
        try:
            state = read_json(self._path(tid), None)
            if state is None:
                return
            if ((state.get('operation') and not state['operation'].get('finished'))
                    or getattr(self.executor, 'durable_quick_supported', False)):
                self.durable.answer(tid)
                return
            full = self.transport.get(tid)
            new = [m for m in full['messages'] if m['role'] == 'requester' and m['id'] not in state['seen']]
            if not new:
                return
            if full.get('peer') != state.get('peer') or not self.accepts_peer(state.get('peer')):
                return
            if getattr(self.executor, 'durable_step_required', False):
                self._reply(tid, 'This receiver cannot verify a native final answer for ordinary chat. '
                    'Choose a supported receiving harness in ANet settings.', 'failed')
                state['seen'] = (state['seen'] + [m['id'] for m in new])[-200:]
                atomic_json(self._path(tid), state)
                return
            _, payload = parse_message(new[-1]['text'])
            resource = payload.get('resource', 'background') if isinstance(payload, dict) else 'background'
            if resource not in ('background', 'desktop'):
                self._reply(tid, 'resource must be background or desktop', 'failed')
                state['seen'] = (state['seen'] + [m['id'] for m in new])[-200:]
                atomic_json(self._path(tid), state)
                return
            prompt, sample = self._prompt(full['messages'], tid)
            self._reply(tid, _say('working', sample) + '\n\n' + status_line({
                'task': tid, 'state': 'queued', 'scheduling': {'state': 'waiting_slot', 'resource': resource}}), 'working')
            work_dir = Path(state.get('work_dir') or self.work_dir)
            cancel = self.cancel_events[tid]
            with self.resources.lease('quick-' + tid, resource, work_dir, cancel=cancel, stopping=self.stopping) as acquired:
                if not acquired:
                    return
                state = read_json(self._path(tid), None)
                if state is None:
                    return
                full = self.transport.get(tid)
                new = [m for m in full['messages'] if m['role'] == 'requester' and m['id'] not in state['seen']]
                if not new:
                    return
                if any(m.get('files') for m in new):
                    try:
                        self.transport.pull(tid, self.inputs_root / safe_name(tid))
                    except Exception as e:
                        self.log.warning('could not fetch the files of %s: %s', tid, e)
                prompt, sample = self._prompt(full['messages'], tid)
                ids = [m['id'] for m in new]
                if state['replies'] >= MAX_REPLIES:
                    self._reply(tid, _say('cap', sample, n=MAX_REPLIES), 'failed')
                else:
                    with self.lock:
                        self.executing.add(tid)
                    with execution(self.activity, 'quick-' + tid, self.timeout):
                        r = self.executor.run(prompt + '\n\n' + resource_rules(resource), work_dir=work_dir,
                                              timeout=self.timeout, mode='task', cancel=cancel)
                    if cancel.is_set() or self.stopping.is_set():
                        return
                    text = (r.text or '').strip()
                    if r.ok and text:
                        self._reply(tid, text, 'completed')
                    else:
                        reason = 'timed out' if r.timed_out else (r.error or 'empty answer')
                        self._reply(tid, _say('failed', sample, reason=reason), 'failed')
                    state['replies'] += 1
                state['seen'] = (state['seen'] + ids)[-200:]
                atomic_json(self._path(tid), state)
        except Exception as e:
            self.log.exception('quick answer for %s failed: %s', tid, e)
        finally:
            with self.lock:
                self.running.discard(tid)
                self.executing.discard(tid)
                self.cancel_events.pop(tid, None)

    def keepalive(self):
        with self.lock:
            pending = set(self.running)
        for tid in pending:
            state = read_json(self._path(tid), {})
            if (state.get('operation') or {}).get('requester_ended'):
                continue
            self._reply(tid, status_line({'task': tid, 'state': 'running', 'heartbeat': True,
                'heartbeat_at': utcnow(), 'scheduling': self.resources.status('quick-' + tid)}), 'working')

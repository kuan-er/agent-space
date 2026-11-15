"""The daemon's loops: take tasks from the transport into the engine, keep requesters informed, keep
the node's own processes running, and publish this node's status."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import socket
import threading
import time

from .protocol import ACTIVE, FINAL, PROTOCOL, parse_message, status_line
from .maintenance import AdmissionGate, store_update_safe
from .quick import QuickResponder
from . import attachments
from .activity import snapshot
from .binding import BindingService, PROTOCOL as BINDING_PROTOCOL
from .platform.locks import SingleInstance
from .platform.proc import start_detached
from .store import atomic_json, read_json, safe_name, utcnow

VERSION = '1.9.4'
TERMINAL = {'completed', 'failed', 'canceled', 'rejected'}


class ProviderLoop:
    """Turns the transport's inbound tasks into engine calls. A task is known by its id; a message by
    its id, so nothing is handled twice after a restart."""

    def __init__(self, engine, transport, store, inputs_root, log=None, quick=None):
        self.engine, self.transport, self.store = engine, transport, store
        self.quick = quick  # answers plain messages in one executor call; None when only long tasks are taken
        self.inputs_root = Path(inputs_root)
        self.log = log or logging.getLogger('taskd')
        self.rejected_path = store.root / 'rejected.json'
        self.rejected = set(read_json(self.rejected_path, []) or [])
        self.queries_path = store.root / 'queries.json'
        self.queries = dict(read_json(self.queries_path, {}) or {})

    def _capabilities(self, task_id, peer, message):
        """A node query needs no work record, attachments, planner or executor."""
        _, payload = parse_message(message['text'])
        env = {'task': task_id, 'kind': 'capabilities', 'state': 'completed', 'a2a_state': 'completed'}
        if not self.engine.accepts_peer(peer):
            env.update(state='rejected', a2a_state='rejected', reason='peer_not_accepted')
        elif payload.get('protocol', PROTOCOL) != PROTOCOL:
            env.update(state='rejected', a2a_state='rejected', reason='unsupported_protocol')
        else:
            from .lightweight import routine
            from .protocol import Rejected
            caps = dict(self.engine.capabilities)
            if self.engine.config.get('routines'):
                caps['routines'] = {}
            for name in self.engine.config.get('routines', {}):
                try:
                    item = routine(self.engine.config, name, peer)
                except Rejected:
                    continue
                caps['routines'][name] = {k: item.get(k, default) for k, default in (
                    ('parameters', {}), ('resource', 'background'), ('repair', False))}
            env['capabilities'] = caps
        body = json.dumps(env.get('capabilities', {'reason': env.get('reason')}), ensure_ascii=False)
        try:
            self.transport.reply(task_id, body + '\n\n' + status_line(env), env['a2a_state'])
        except Exception as e:
            self.log.warning('capabilities reply to %s failed: %s', task_id, e)
            return  # keep it eligible for retry; a failed delivery is not a handled query
        self.queries[task_id] = message['id']
        self.queries = dict(list(self.queries.items())[-2000:])
        atomic_json(self.queries_path, self.queries)

    def _inputs(self, task_id, messages):
        if not any(m.get('files') for m in messages if m['role'] == 'requester'):
            return None
        incoming = [m for m in messages if m['role'] == 'requester' and m.get('files')]
        # One fresh directory contains all files pulled for this interaction. Verify
        # each incoming message's declarations before exposing any inputs to execution.
        last_dir = None
        for message in incoming:
            key = safe_name(task_id) + '-' + safe_name(message['id'])
            receipt_path = self.store.root / ('input-' + key + '.json')
            receipt = read_json(receipt_path, {})
            if receipt.get('state') != 'verified':
                receipt = attachments.receive(self.transport, task_id, message, {}, self.inputs_root)
                atomic_json(receipt_path, receipt)
            if receipt['state'] != 'verified':
                raise ValueError('文件未收到: ' + receipt.get('error', 'integrity check failed'))
            last_dir = receipt['directory']
        return last_dir

    def _status_query(self, task_id, peer, message, payload):
        """A separate control task reads status without consuming a model/desktop slot."""
        target = payload.get('task_id')
        record = self.store.load(target) if isinstance(target, str) else None
        limit = payload.get('limit', 20)
        if (payload.get('protocol', PROTOCOL) != PROTOCOL or
                type(limit) is not int or not 1 <= limit <= 50):
            env = {'task': task_id, 'state': 'rejected', 'reason': 'invalid_query'}
            state = 'rejected'
        elif not self.engine.accepts_peer(peer) or not record or record.get('peer') != peer:
            env = {'task': task_id, 'state': 'rejected', 'reason': 'task_unavailable'}
            state = 'rejected'
        elif payload.get('taskd') == 'reconcile_stop':
            result = self.engine.reconcile_native_stop(target, peer, task_id)
            state = 'completed' if result in ('queued', 'already_requested', 'no_quarantine') else 'rejected'
            env = {'task': target, 'state': state, 'control_query': task_id, 'reconciliation': result}
        else:
            env = self.engine._envelope(record, plan=record.get('plan'), checks=record['checks'],
                                        native_quarantine=bool(record.get('native_quarantine')))
            env['control_query'] = task_id
            if payload.get('taskd') == 'logs':
                # Only this peer's task events, never arbitrary paths or full prompts/configuration.
                events = []
                for row in self.store.tail_log(target, limit):
                    event = {k: row[k] for k in ('ts', 'event', 'n', 'phase', 'ok', 'duration') if k in row}
                    if isinstance(row.get('report'), dict):
                        event['summary'] = str(row['report'].get('summary', ''))[:1000]
                    if row.get('event') == 'native_event':
                        event['method'] = row.get('method')
                        event['detail'] = row.get('data')
                    events.append(event)
                env = {'task': target, 'state': record['state'], 'control_query': task_id,
                       'events': events, 'limit': limit}
            state = 'completed'
        self.transport.reply(task_id, status_line(env), state)
        self.queries[task_id] = message['id']
        self.queries = dict(list(self.queries.items())[-2000:])
        atomic_json(self.queries_path, self.queries)

    def tick(self):
        for s in self.transport.list_inbound():
            tid = s['id']
            if tid in self.rejected or tid in self.queries:
                continue
            rec = self.store.load(tid)
            if self.quick and self.quick.offer(s, rec):
                continue
            if s['state'] in TERMINAL:
                if rec and rec['state'] not in FINAL and rec.get('stop_reason') != 'ended_by_requester':
                    self.engine.ended_by_peer(tid, s['state'])
                continue
            latest = s.get('latest') or {}
            if latest.get('role') != 'requester':
                continue
            if rec and latest.get('id') in rec.get('seen_messages', []):
                continue
            full = self.transport.get(tid)
            mine = [m for m in full['messages'] if m['role'] == 'requester']
            if not mine:
                continue
            kind, payload = parse_message(mine[0]['text'])
            if rec is None and kind == 'control' and payload.get('taskd') == 'capabilities':
                self._capabilities(tid, full.get('peer') or s.get('peer'), mine[0])
                continue
            if rec is None and kind == 'control' and payload.get('taskd') in ('status', 'logs', 'reconcile_stop'):
                self._status_query(tid, full.get('peer') or s.get('peer'), mine[0], payload)
                continue
            try:
                inputs = self._inputs(tid, full['messages'])
            except ValueError as e:
                self.log.warning('input delivery %s: %s', tid, e)
                if not self.store.read_log(tid, 'input_delivery_failed'):
                    self.transport.reply(tid, str(e) + '\n\n' + status_line({'task': tid, 'state': 'blocked',
                                         'reason': str(e), 'files': {'state': 'failed', 'error': str(e)}}), 'working')
                    self.store.log(tid, 'input_delivery_failed', error=str(e))
                continue
            if rec is None:
                first = mine[0]
                rec = self.engine.intake_new(tid, full.get('peer') or s.get('peer'), first['id'], first['text'],
                                             inputs_dir=inputs)
                if rec is None:
                    self.rejected.add(tid)
                    atomic_json(self.rejected_path, sorted(self.rejected)[-2000:])
                    continue
                mine = mine[1:]
            for m in mine:
                text = m['text']
                if m.get('files') and inputs:
                    text += '\n\n(files from the requester are in %s)' % inputs
                self.engine.intake_message(tid, m['id'], text)


class Guard:
    """Keeps the node's companions running: the transport's node, and processes named in the config
    by a lock file they hold or a socket they listen on."""

    def __init__(self, transport, entries, log=None):
        self.transport, self.entries = transport, list(entries or [])
        self.log = log or logging.getLogger('taskd')

    def alive(self, e):
        if e.get('lock'):
            return SingleInstance(e['lock']).held_elsewhere()
        if e.get('socket') and hasattr(socket, 'AF_UNIX'):
            s = socket.socket(socket.AF_UNIX)
            try:
                s.settimeout(2)
                s.connect(e['socket'])
                return True
            except OSError:
                return False
            finally:
                s.close()
        return True

    def tick(self):
        try:
            self.transport.ensure_up()
        except Exception as e:
            self.log.warning('the node is not running and could not be started: %s', e)
        for e in self.entries:
            try:
                if not self.alive(e):
                    self.log.info('starting %s again', e.get('name'))
                    start_detached(e['argv'], e.get('log'))
            except Exception as err:
                self.log.warning('guard %s: %s', e.get('name'), err)


class Daemon:
    def __init__(self, *, root, config, transport, engine=None, relay=None, guard=None, capabilities=None,
                 selftest=None, log=None, poll_seconds=5, updater=None, lock_wait=0):
        self.root, self.config, self.transport = Path(root), config, transport
        self.engine, self.relay, self.guard = engine, relay, guard
        self.capabilities = capabilities or {}
        self.selftest = selftest or {}
        self.log = log or logging.getLogger('taskd')
        self.poll_seconds = poll_seconds
        self.stop = threading.Event()
        self.updater, self.lock_wait = updater, lock_wait
        self.admission = AdmissionGate()
        self.hold = self.admission.held
        if self.engine:
            self.engine.admission = self.admission
        self.restart_requested = False
        inputs = Path(config['work_dir']) / 'taskd-inputs'
        self.quick = QuickResponder(engine.executor, transport, engine.store, engine.work_dir, config, inputs, self.log,
                                    accepts_peer=engine.accepts_peer, resources=engine.resources, stopping=self.stop, admission=self.admission) if engine else None
        self.provider = ProviderLoop(engine, transport, engine.store, inputs, self.log, self.quick) if engine else None
        self.bindings = BindingService(self.root, relay) if relay else None

    def is_idle(self):
        """No owned work or uncertain native execution; admission must be held for swap."""
        with self.admission.lock:
            if not self.engine:
                return True
            if not store_update_safe(self.engine.store):
                return False
            if self.quick:
                with self.quick.lock:
                    if self.quick.running:
                        return False
            with getattr(self.engine, 'guard', self.admission.lock):
                return not any(th.is_alive() for th in getattr(self.engine, 'workers', {}).values())

    def set_hold(self, on):
        self.admission.set_hold(on)

    def request_restart(self):
        self.restart_requested = True
        self.stop.set()

    def _unless_held(self, fn):
        return lambda: self.admission.run(fn)

    def write_status(self):
        st = {'ts': time.time(), 'at': utcnow(), 'version': VERSION, 'pid': os.getpid(),
              'accept_tasks': self.engine is not None and not self.hold.is_set(), 'requester': self.relay is not None,
              'maintenance': self.hold.is_set(),
              'executor': (self.config.get('executor') or {}).get('type'),
              'executor_ready': bool(self.selftest.get('ok')) and self.engine is not None and not self.hold.is_set(),
              'capabilities': self.capabilities if self.engine else None}
        if self.relay:
            st['requester_binding'] = BINDING_PROTOCOL
            st['requester_clients'] = {}
            for name, adapter in self.relay.adapters.items():
                try:
                    st['requester_clients'][name] = adapter.readiness()
                except Exception as e:
                    st['requester_clients'][name] = {'ready': False, 'error': str(e)[:300], 'at': utcnow()}
            st['requester_ready'] = any(r.get('ready') for r in st['requester_clients'].values())
        if self.updater:
            st.update(self.updater.public_state())
        if self.engine:
            counts = {}
            for tid in self.engine.store.ids():
                t = self.engine.store.load(tid)
                counts[t['state']] = counts.get(t['state'], 0) + 1
            st['tasks'] = counts
            st['work'] = snapshot(self.engine, self.quick)
        atomic_json(self.root / 'status.json', st)

    def _every(self, seconds, fn, name):
        def loop():
            while not self.stop.is_set():
                try:
                    fn()
                except Exception as e:
                    self.log.warning('%s: %s', name, e)
                self.stop.wait(seconds)
        th = threading.Thread(target=loop, name=name, daemon=True)
        th.start()
        return th

    def run(self):
        lock = SingleInstance(self.root / 'daemon.lock')
        waited = 0
        while not lock.acquire():
            waited += 1
            if waited > self.lock_wait:
                raise RuntimeError('taskd is already running for this data directory')
            time.sleep(1)
        self.log.info('taskd %s started (accept tasks: %s, requester: %s)', VERSION, bool(self.engine), bool(self.relay))
        every = dict({'guard': 60, 'inbound': self.poll_seconds, 'keepalive': 60, 'schedule': 30,
                      'relay': 30, 'status': 10}, **self.config.get('intervals', {}))
        try:
            # Status remains observable during validation, but NO guard, recovery,
            # provider, quick, binding, or relay admission starts before it finishes.
            if self.updater and self.updater.needs_validation():
                self.set_hold(True)
            self._every(every['status'], self.write_status, 'status')
            ready = True
            if self.updater:
                ready = self.updater.startup(self.stop)
            if ready and not self.stop.is_set():
                if self.guard:
                    self._unless_held(self.guard.tick)()
                    self._every(every['guard'], self._unless_held(self.guard.tick), 'guard')
                if self.engine:
                    self._unless_held(self.engine.recover)()
                    self._every(every['inbound'], self._unless_held(self.provider.tick), 'inbound')
                    self._every(every['keepalive'], self._unless_held(self.engine.keepalive), 'keepalive')
                    self._every(60, self._unless_held(self.quick.keepalive), 'quick-keepalive')
                    self._every(every['schedule'], self._unless_held(self.engine.schedule), 'schedule')
                if self.relay:
                    self._every(.25, self._unless_held(self.bindings.tick), 'requester-bindings')
                    self._every(every['relay'], self._unless_held(self.relay.tick), 'relay')
                if self.updater:
                    threading.Thread(target=self.updater.run, args=(self.stop,), name='update', daemon=True).start()
            else:
                self.log.warning('update startup validation did not release admission; service remains in maintenance')
            while not self.stop.wait(1):
                if (self.root / 'stop').exists():
                    break
        finally:
            self.set_hold(True)
            if self.quick:
                with self.quick.lock:
                    for event in self.quick.cancel_events.values():
                        event.set()
            if self.engine:
                self.engine.shutdown()
            self.transport.close()
            lock.release()
            self.log.info('taskd stopped')

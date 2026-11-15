"""Planned provider steps through the existing durable harness SDK service.

One dedicated native source per provider/workspace. This never adopts UI history,
uses native Goals, changes permissions, or launches arbitrary configured commands.
"""
import math
import threading
import time
import uuid
from contextlib import nullcontext

from .store import ProviderStore, fingerprint
from .workbuddy_local import WorkBuddyLocalRunner, utcnow, LocalProtocolError
from anet_harnesses.network import private_folder, plain

HARNESSES = {'codex': 'codex_turn', 'claude_code': 'claude_result',
             'deepseek_harness': 'deepseek_inbox_interval'}
TERMINAL = ('completed', 'failed', 'interrupted')


class SDKHarnessRunner:
    def __init__(self, state_root, harness, *, config=None, service_factory=None,
                 clock=time.monotonic, sleep=time.sleep, poll_seconds=0.5):
        if harness not in HARNESSES:
            raise ValueError('only verified local SDK harnesses belong in this provider')
        config = config or {'harnesses': {}}
        if (not isinstance(config,dict) or set(config)-{'schema','harnesses'}
                or config.get('schema','anet-harness-config/1') != 'anet-harness-config/1'
                or not isinstance(config.get('harnesses',{}),dict)
                or set(config.get('harnesses',{}))-set(HARNESSES)):
            raise ValueError('invalid native provider configuration')
        def reject_credentials(value):
            if isinstance(value,dict):
                if any(str(k).lower() in ('token','api_key','client_secret','access_token','refresh_token','password') for k in value):
                    raise ValueError('credentials belong in native login, never provider configuration')
                for child in value.values(): reject_credentials(child)
            elif isinstance(value,list):
                for child in value: reject_credentials(child)
        reject_credentials(config)
        self.root = plain(state_root)
        private_folder(self.root)
        self.harness, self.config = harness, config
        self.service_factory = service_factory
        self.services, self.lock = {}, threading.RLock()
        self.clock, self.sleep = clock, sleep
        self.poll_seconds = float(poll_seconds)
        if not math.isfinite(self.poll_seconds) or self.poll_seconds <= 0:
            raise ValueError('positive finite poll interval required')

    def _service(self, store, work_dir):
        with self.lock:
            if str(store.root) not in self.services:
                factory = self.service_factory
                if factory is None:
                    from anet_harnesses.service import HarnessService
                    factory = HarnessService
                self.services[str(store.root)] = factory(store.root/'sdk', work_dir, config=self.config)
            return self.services[str(store.root)]

    @staticmethod
    def _view(operation, *, timed_out=False, canceled=False):
        return dict(operation, reply=operation.get('reply'), timed_out=timed_out,
                    canceled=canceled, business_effect_verified=False,
                    original_message_must_not_be_repeated=operation.get('state') != 'not_dispatched')

    def observe(self,operation_id,*,work_dir,timeout,cancel=None,owner_input=None):
        work = WorkBuddyLocalRunner._input('observation validation',work_dir,'task')['work_dir']
        key = fingerprint({'harness':self.harness,'work_dir':work})
        if not (self.root/key).is_dir():
            raise LocalProtocolError('provider operation not found')
        store = ProviderStore(self.root/key)
        operation = store.load(operation_id)
        if not operation or operation.get('harness') != self.harness or operation['input']['work_dir'] != work:
            raise LocalProtocolError('owned provider operation not found')
        return self.run(operation['input']['prompt'],work_dir=work,timeout=timeout,
            mode=operation['input']['mode'],cancel=cancel,operation_id=operation_id,_observation_only=True,owner_input=owner_input)

    def run(self, prompt, *, work_dir, timeout, mode='task', cancel=None, operation_id=None,_observation_only=False,owner_input=None):
        if mode != 'task':
            raise ValueError('this SDK wrapper does not enforce read-only review permissions')
        requested = WorkBuddyLocalRunner._input(prompt, work_dir, mode)
        if not isinstance(timeout,(int,float)) or isinstance(timeout,bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('positive finite observation timeout required')
        deadline = self.clock() + timeout
        key = fingerprint({'harness':self.harness,'work_dir':requested['work_dir']})
        store = ProviderStore(self.root/key)
        pending_path = store.root/'sdk-pending.json'
        with store.locked(), owner_input if owner_input is not None else nullcontext():
            pending = store.read(pending_path)
            previous = store.load(operation_id) if operation_id else None
            if previous and previous['input_sha256'] != fingerprint(requested):
                raise LocalProtocolError('operation input differs')
            if previous and previous.get('native_terminal_verified'):
                if pending and pending.get('operation_id') == previous['operation_id']:
                    pending_path.unlink()
                return self._view(previous)
            if pending:
                operation = store.load(pending['operation_id'])
                if not operation or operation['input_sha256'] != pending['input_sha256']:
                    raise LocalProtocolError('provider pending evidence differs')
                if operation['input_sha256'] != fingerprint(requested) or operation_id and operation_id != operation['operation_id']:
                    return self._view(dict(operation,state='blocked_by_unresolved_operation'))
                if operation.get('native_terminal_verified'):
                    pending_path.unlink()
                    return self._view(operation)
            else:
                if previous:
                    raise LocalProtocolError('unknown operation lost its pending evidence')
                if _observation_only:
                    raise LocalProtocolError('read-only observation cannot create a native source or operation')
                if cancel is not None and cancel.is_set():
                    return {'state':'not_dispatched','canceled':True,'timed_out':False,'reply':None,
                            'operation_id':operation_id,'dispatch_attempted':False,
                            'native_terminal_verified':False,'native_cancel_confirmed':False,
                            'business_effect_verified':False,'original_message_must_not_be_repeated':False}
                source_path = store.root/'sdk-source.json'
                source = store.read(source_path)
                if source is None:
                    source = {'local_id':str(uuid.uuid4()),'harness':self.harness,
                              'workspace':requested['work_dir'],'state':'creation_reserved'}
                    store.write(source_path,source)
                if source['harness'] != self.harness or source['workspace'] != requested['work_dir']:
                    raise LocalProtocolError('provider source identity/workspace differs')
                service = self._service(store,requested['work_dir'])
                # Source creation uses the SAME durable ID after any restart.
                # The SDK service rejects an uncertain creation instead of
                # adopting another native source or creating a replacement.
                binding = service.create(source['local_id'],self.harness)
                if not binding.get('backend_id') or binding.get('workspace') != requested['work_dir']:
                    raise LocalProtocolError('native source binding incomplete')
                source.update(state='bound',native_session_id=binding['backend_id'])
                store.write(source_path,source)
                operation_id = operation_id or str(uuid.uuid4())
                store.operation_path(operation_id)
                operation = {'schema':'anet-sdk-provider-operation/1','operation_id':operation_id,
                             'source_id':source['local_id'],'native_session_id':source['native_session_id'],
                             'harness':self.harness,'input':requested,'input_sha256':fingerprint(requested),
                             'state':'prepared','native_terminal_verified':False,'native_cancel_confirmed':False,
                             'business_effect_verified':False,'created_at':utcnow()}
                operation['evidence_path'] = str(store.operation_path(operation_id))
                store.save(operation)
                store.write(pending_path,{'operation_id':operation_id,'input_sha256':operation['input_sha256']})
            service = self._service(store,requested['work_dir'])
            operation_id = operation['operation_id']
            if operation['state'] == 'prepared':
                if _observation_only:
                    return self._view(operation)
                if cancel is not None and cancel.is_set() or self.clock() >= deadline:
                    return self._view(operation,timed_out=self.clock() >= deadline,
                                      canceled=bool(cancel is not None and cancel.is_set()))
                operation['state'] = 'dispatching'
                store.save(operation)
                try:
                    service.submit(operation['source_id'],operation_id,prompt,goal=False,user_authorized=False)
                except Exception as error:
                    operation.update(state='dispatch_unverified',reason_code=type(error).__name__)
                    store.save(operation)
                    return self._view(operation)
            while self.clock() < deadline:
                status = service.status(operation_id,refresh=True)
                if owner_input is not None:owner_input.observe(service,operation)
                native = status.get('operation') or {}
                native_state = native.get('state')
                result = native.get('result') or {}
                evidence = result.get('native_evidence') or {}
                same_session = evidence.get('session_id') == operation['native_session_id']
                identity_verified = (same_session and evidence.get('kind') == HARNESSES[self.harness]
                    and isinstance(evidence.get('activity_id'),str) and bool(evidence['activity_id'].strip()))
                if self.harness == 'codex':
                    identity_verified = identity_verified and evidence.get('activity_id') == native.get('backend_id')
                else:
                    identity_verified = identity_verified and native.get('backend_id') == operation['native_session_id']
                native_end = evidence.get('terminal_status')
                # A terminal label is insufficient when it contradicts the
                # SDK operation state. Never turn an interruption/error into
                # successful caller continuation, or an ordinary result into
                # confirmed native cancellation.
                if self.harness == 'codex':
                    terminal_status_verified = native_state in TERMINAL and native_end == native_state
                elif self.harness == 'deepseek_harness':
                    terminal_status_verified = (native_state == 'completed' and native_end == 'completed'
                        or native_state == 'interrupted' and native_end in ('aborted','cancelled')
                        or native_state == 'failed' and native_end == 'error')
                else:
                    terminal_status_verified = (native_state == 'completed' and (native_end == 'success' or native_end == 'completed' and result.get('native_subtype') == 'success')
                        or native_state == 'interrupted' and native_end in ('aborted_streaming','aborted_tools')
                        or native_state == 'failed' and isinstance(native_end,str) and bool(native_end)
                            and native_end not in ('success','completed','aborted_streaming','aborted_tools','running','queued','pending'))
                if (native_state in TERMINAL and native.get('operation_id') == operation_id
                        and native.get('source_id') == operation['source_id'] and identity_verified and terminal_status_verified):
                    text = native.get('result_text') or result.get('text') or ''
                    final_verified = (native_state != 'completed' or (evidence.get('final_assistant') is True
                        and isinstance(evidence.get('final_message_id'),str) and bool(evidence['final_message_id'].strip())
                        and isinstance(text,str) and bool(text.strip())
                        and native.get('result_text',text) == result.get('text',text)))
                    if final_verified:
                        operation.update(state=native_state,reply=text,native_evidence=evidence,
                            native_terminal_verified=True,native_cancel_confirmed=native_state=='interrupted',observed_at=utcnow())
                        store.save(operation)
                        pending_path.unlink()
                        return self._view(operation,canceled=bool(cancel is not None and cancel.is_set()))
                if cancel is not None and cancel.is_set():
                    if not operation.get('cancel_attempted'):
                        # Persist control intent before invoking it once. An ACK
                        # is not a native interruption observation.
                        operation['cancel_attempted'] = True
                        store.save(operation)
                        try:
                            service.control('cancel',operation['source_id'],operation_id)
                            operation['cancel_control_acknowledged'] = True
                        except Exception as error:
                            operation['cancel_error'] = type(error).__name__
                        store.save(operation)
                    operation.update(state='observation_canceled_native_effect_unverified')
                    store.save(operation)
                    return self._view(operation,canceled=True)
                operation['last_native_state'] = native_state or status.get('state','unverified')
                if status.get('local_error'):
                    operation.update(state='observation_unverified',reason_code='sdk_worker_error')
                    store.save(operation)
                    return self._view(operation)
                self.sleep(min(self.poll_seconds,max(0,deadline-self.clock())))
            operation.update(state='observation_timed_out_native_effect_unverified')
            store.save(operation)
            return self._view(operation,timed_out=True)

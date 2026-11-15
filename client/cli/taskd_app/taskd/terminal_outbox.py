"""Opt-in completion recovery, never a business executor or unknown-send retry."""
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid

from .store import atomic_json,utcnow
from . import private_artifact_dir as private

SCHEMA='taskd-terminal-delivery/2'
READY={'not_invoked','preparing','prepared'}
UNCERTAIN={'invoking','unknown'}
MAX_REPORT_BYTES=16*1024*1024

def report_bytes(path):
    path=private.plain(path)
    if not path.is_file() or not 0<path.stat().st_size<=MAX_REPORT_BYTES:raise ValueError('completion_report_unavailable')
    with path.open('rb') as stream:
        before=os.fstat(stream.fileno());data=stream.read(MAX_REPORT_BYTES+1);after=os.fstat(stream.fileno())
    if (len(data)>MAX_REPORT_BYTES or len(data)!=before.st_size or
            (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=
            (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)):
        raise ValueError('completion_report_changed')
    return data


class TerminalOutbox:
    def __init__(self,provider):
        self.p=provider;self.engine=None;self.instance=uuid.uuid4().hex
        self.active=set();self.guard=threading.RLock();self.cursor=0
        self.last_step_progress=False

    def bind(self,engine):self.engine=engine

    def valid(self,task):
        marker=task.get('terminal_outbox') or {}
        return (task.get('state')=='completed' and marker.get('schema')==SCHEMA
            and marker.get('task_id')==task.get('id') and marker.get('provider')==self.p.node_aid
            and marker.get('requester')==task.get('peer')
            and marker.get('queue_identity')==self.p.root_identity)

    def enroll(self,task,report,text):
        # This marker is committed atomically WITH the new completed state. Old
        # completed records have no marker and are never adopted or modified.
        path=self.p.store.task_dir(task['id'])/'report.json';atomic_json(path,report)
        pins=[];pin_error=None
        try:
            from .provider_artifacts import source_key
            pins=[{'label':label,'key':source_key(source.stat())} for label,source in
                self.p._sources(self.engine,task,report,path) if source!=path]
        except (OSError,ValueError,TypeError,KeyError):pin_error='completion_sources_unavailable'
        # A recovery parser limit must not prevent the current completed state
        # and bounded summary from being sent. Such a report is explicitly held
        # only if a later restart actually needs to reconstruct that send.
        try:report_hash=hashlib.sha256(report_bytes(path)).hexdigest();report_error=None
        except ValueError:report_hash=None;report_error='completion_report_unavailable'
        task['terminal_outbox']={'schema':SCHEMA,'task_id':task['id'],'provider':self.p.node_aid,
            'requester':task['peer'],'queue_identity':self.p.root_identity,'enrollment_id':uuid.uuid4().hex,
            'owner_instance':self.instance,'phase':'not_invoked','report_sha256':report_hash,'report_restore_error':report_error,
            'text':text,'source_pins':pins,'source_pin_error':pin_error,'created_at':utcnow(),
            'terminal_transport_ack':'not_verified','business_resume':False}
        with self.guard:self.active.add(task['id'])

    def release(self,task_id):
        with self.guard:self.active.discard(task_id)

    def before_invoke(self,task,body):
        if not self.valid(task):return
        with self.p.store.lock(task['id']):
            current=self.p.store.load(task['id']);marker=(current or {}).get('terminal_outbox') or {}
            if not self.valid(current or {}) or marker.get('enrollment_id')!=task['terminal_outbox']['enrollment_id']:
                raise ValueError('terminal_enrollment_changed')
            if marker.get('phase') not in READY:raise ValueError('terminal_invocation_not_known_unsent')
            marker.update(phase='invoking',owner_instance=self.instance,
                body_sha256=hashlib.sha256(body.encode('utf-8')).hexdigest(),invocation_at=utcnow())
            task['terminal_outbox']=marker;self.p.store.save(task)

    def outcome(self,task,*,acknowledged=False,local_refusal=False):
        if not self.valid(task):return
        marker=task['terminal_outbox']
        if marker.get('phase')!='invoking':return
        marker.update(phase='rpc_acknowledged' if acknowledged else 'not_invoked' if local_refusal else 'unknown',
            local_rpc_acknowledged=acknowledged,terminal_transport_ack='not_verified',updated_at=utcnow())
        if local_refusal:
            marker['local_refusals']=(marker.get('local_refusals',[])+[marker['body_sha256']])[-4:]
        self.p.store.save(task)

    def report(self,task):
        marker=task['terminal_outbox'];path=private.plain(self.p.store.task_dir(task['id'])/'report.json')
        data=report_bytes(path)
        if hashlib.sha256(data).hexdigest()!=marker['report_sha256']:raise ValueError('completion_report_changed')
        report=json.loads(data)
        if report.get('task')!=task['id'] or report.get('state')!='completed':raise ValueError('completion_report_identity')
        return report

    def check_sources(self,task,sources):
        if not self.valid(task):return
        marker=task['terminal_outbox']
        if marker.get('source_pin_error'):raise ValueError('completion_sources_unavailable')
        from .provider_artifacts import source_key
        actual=[{'label':label,'key':source_key(source.stat())} for label,source in sources
            if source!=self.p.store.task_dir(task['id'])/'report.json']
        if actual!=marker.get('source_pins'):raise ValueError('completion_sources_changed')

    def _hold(self,task,reason):
        task['terminal_outbox'].update(phase='held',reason=reason,terminal_transport_ack='not_verified',updated_at=utcnow())
        self.p.store.save(task)

    def public_state(self):
        phases={};held=[]
        for tid in self.p.store.ids():
            task=self.p.store.load(tid)
            if not task or not self.valid(task):continue
            marker=task['terminal_outbox'];phase=marker.get('phase','invalid')
            phases[phase]=phases.get(phase,0)+1
            if phase=='held':held.append({'task_id':tid,'reason':marker.get('reason')})
        return {'phases':phases,'held':held[-16:],'terminal_transport_ack':'not_verified'}

    def tick(self):
        self.last_step_progress=False
        candidates=[]
        for tid in self.p.store.ids():
            task=self.p.store.load(tid)
            if not task or not self.valid(task):continue
            with self.guard:active=tid in self.active
            marker=task['terminal_outbox'];phase=marker.get('phase')
            eligible=(phase in READY and self.engine is not None) or (
                phase in UNCERTAIN and self.p.clock()>=marker.get('next_probe_at',0))
            if not active and eligible:candidates.append(tid)
        if not candidates:return False
        tid=sorted(candidates)[self.cursor%len(candidates)];self.cursor+=1
        with self.p.store.lock(tid):
            task=self.p.store.load(tid)
            with self.guard:
                if tid in self.active:return False
                self.active.add(tid)
        try:
            marker=task['terminal_outbox']
            if marker['phase'] in UNCERTAIN:
                # Unknown invocation is read-only reconciliation forever; absence
                # of a receipt is never treated as evidence that nothing happened.
                marker['next_probe_at']=self.p.clock()+30
                if not self.p.proof_source or not self.p.proof_verifier:
                    marker.update(recovery_state='query_original_result_only',reason='proof_capability_unavailable')
                    self.p.store.save(task);return True
                try:
                    full,record=self.p.proof_source.fetch(tid)
                    proof=self.p.proof_verifier(full,record,local_aid=self.p.node_aid,role='provider',require_descriptor=False)
                    if (proof.get('task_id')!=tid or proof.get('provider_aid')!=self.p.node_aid
                            or proof.get('requester_aid')!=task['peer'] or proof.get('signed_terminal_verified') is not True
                            or proof.get('terminal_local_commit_verified') is not True
                            or proof.get('terminal_body_sha256')!=marker.get('body_sha256')):
                        raise ValueError('terminal_reconcile_mismatch')
                    marker.update(phase='local_commit_verified',terminal_transport_ack='not_verified',
                        result_cid=proof.get('result_cid'),receipt_cid=proof.get('receipt_cid'),updated_at=utcnow())
                    self.p.store.save(task)
                    self.last_step_progress=True
                except Exception:
                    marker.update(recovery_state='query_original_result_only',reason='original_signed_commit_unconfirmed')
                    self.p.store.save(task)
                return True
            report=self.report(task)
            self.check_sources(task,self.p._sources(self.engine,task,report,self.p.store.task_dir(tid)/'report.json'))
            marker.update(phase='preparing',owner_instance=self.instance,recovery_started_at=utcnow())
            self.p.store.save(task)
            files=self.engine._attach(task,report)
            # This is the only recovery send: the durable marker still proves no
            # invocation has ever begun. Engine.send writes invoking first.
            self.engine.send(task,marker['text'],files=files,report=self.engine._short(report),
                _delivery_transport=self.p.recovery_transport)
            self.p.store.save(task)
            self.last_step_progress=task['terminal_outbox'].get('phase')=='rpc_acknowledged'
            return True
        except (OSError,ValueError,TypeError,KeyError) as error:
            reason=str(error) if str(error) in ('completion_sources_changed','completion_sources_unavailable',
                'completion_report_changed','completion_report_unavailable','completion_report_identity') else 'delivery_recovery_requires_local_review'
            self._hold(task,reason);return True
        finally:self.release(tid)

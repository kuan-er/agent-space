"""Provider-only automatic artifacts, independent of business execution.

Engine prepares snapshots and announces a fixed descriptor. This worker never
calls Engine/recover/executor/reply. Verified terminal proof is mandatory before
opening the portal session; missing proof capability is explicit, fail-closed.
"""
from contextlib import contextmanager
from functools import partial
import hashlib
import errno
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import threading
import time

from .artifact_core import auto_artifacts as core
from . import private_artifact_dir as private
from .checkers.base import CheckContext
from .store import utcnow
from .protocol import ARTIFACT_DELIVERY

SCHEMA='taskd-provider-artifacts/1'
MAX_JOBS=32
MAX_BYTES=2*1024*1024*1024
PREPARATION_REASONS={
    'completed_provider_task_required','artifact_count_limit','artifact_size_limit',
    'artifact_queue_quota','artifact_disk_headroom','artifact_disk_full',
    'queued_peer_mismatch','unknown_job_directory','interrupted_preparation_preserved',
    'source_changed_during_selection','source_changed_during_snapshot','source_grew',
    'source_changed_type','snapshot_integrity','spool_capacity','portal_manifest_32k_limit',
    'artifact_source_unavailable_or_outside_root','queue_validation_failed',
    'local_artifact_preparation_failed','completion_sources_changed','completion_sources_unavailable',
    'preparation_retry_requires_review','preparation_retry_after_announcement_refused'}


def source_key(info):
    return [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,stat.S_IFMT(info.st_mode)]


def bounded_snapshot(source,target,expected):
    """Never read or write more than the size already admitted to the queue."""
    private.plain(source);limit=expected[2];path_before=source.stat()
    with source.open('rb') as inp:
        before=os.fstat(inp.fileno())
        if not stat.S_ISREG(before.st_mode) or source_key(before)!=expected:
            raise ValueError('source_changed_during_selection')
        copied=0;digest=hashlib.sha256()
        from contextlib import nullcontext
        with (private.open_new(target) if target is not None else nullcontext(None)) as out:
            while True:
                block=inp.read(min(1024*1024,limit-copied+1))
                if not block:break
                if copied+len(block)>limit:raise ValueError('source_grew')
                if out is not None:out.write(block)
                digest.update(block);copied+=len(block)
            if out is not None:out.flush();os.fsync(out.fileno())
        after=os.fstat(inp.fileno())
    private.plain(source)
    path_after=source.stat()
    if (copied!=limit or source_key(after)!=expected or source_key(path_after)!=expected
            or before.st_ctime_ns!=after.st_ctime_ns or path_before.st_ctime_ns!=path_after.st_ctime_ns):
        raise ValueError('source_changed_during_selection')
    return {'size':copied,'sha256':digest.hexdigest()}


class ProofUnavailable(ValueError):
    pass


def create_json(path,value):
    path=private.plain(path)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as out:
        out.write(core.canonical(value));out.flush();os.fsync(out.fileno())


def update_json(path,value):
    path=private.plain(path)
    temporary=path.with_name(path.name+'.new-'+os.urandom(12).hex())
    create_json(temporary,value);os.replace(temporary,path)


def finite_json(path):
    path=private.plain(path)
    if not path.is_file() or path.stat().st_size>1024*1024:raise ValueError('queue_metadata_unavailable')
    return json.loads(path.read_text(encoding='utf-8'))


class ProviderArtifacts:
    def __init__(self, *, directory, node_aid, binding_loader, transport, store,
                 proof_source=None, proof_verifier=None, session_factory=core.PortalSession,
                 max_jobs=MAX_JOBS, max_bytes=MAX_BYTES, inline_bytes=700000, clock=time.time):
        self.root=private.plain(directory);self.node_aid=node_aid;self.transport=transport;self.store=store
        self.binding_loader=binding_loader;self.proof_source=proof_source;self.proof_verifier=proof_verifier
        self.session_factory=session_factory;self.clock=clock;self.guard=threading.RLock()
        self.max_jobs=max_jobs;self.max_bytes=max_bytes;self.inline_bytes=inline_bytes
        if not isinstance(node_aid,str) or not node_aid:raise ValueError('provider_identity_required')
        if type(max_jobs)is not int or not 1<=max_jobs<=128 or type(max_bytes)is not int or not 1<=max_bytes<=MAX_BYTES:
            raise ValueError('bounded_queue_policy_required')
        if type(inline_bytes)is not int or not 0<=inline_bytes<=core.INLINE_BUDGET:raise ValueError('inline_budget')
        if not self.root.exists():
            private.create_new(self.root)
            private.create_new(self.root/'jobs')
            private.write_new(self.root/'queue.lock',b'0')
            create_json(self.root/'owner.json',{'schema':SCHEMA,'node_aid':node_aid,'operator':private.owner_id(),
                'root_identity':core.identity(self.root,True),'jobs_identity':core.identity(self.root/'jobs',True),
                'lock_identity':core.identity(self.root/'queue.lock')})
            create_json(self.root/'index.json',{'schema':SCHEMA,'node_aid':node_aid,'jobs':{},'cursor':0})
        self.validate_root()
        self.root_identity=core.identity(self.root,True)
        self.tick_lane=0
        self.last_step_progress=False;self.progress_tokens={};self.job_due={}
        self.portal_session=None;self.portal_session_key=None;self.portal_session_until=0
        self.recovery_transport=None
        from .terminal_outbox import TerminalOutbox
        self.outbox=TerminalOutbox(self)

    def bind_engine(self,engine):self.outbox.bind(engine)

    def close(self):
        self._close_portal_session()
        if self.recovery_transport is not None:self.recovery_transport.close()

    def _close_portal_session(self):
        session=self.portal_session
        self.portal_session=None;self.portal_session_key=None;self.portal_session_until=0
        if session is not None:session.close()

    def _portal_session(self,binding):
        # Reuse only this worker's authenticated connection, in RAM. Every
        # request still authorizes at the server; changed binding or a short
        # lifetime forces the normal node identity/role check again.
        key=hashlib.sha256(core.canonical(binding)).digest()
        if self.portal_session is not None and (key!=self.portal_session_key or self.clock()>=self.portal_session_until):
            self._close_portal_session()
        if self.portal_session is None:
            self.portal_session=self.session_factory(binding,self.node_aid,binding.get('portal',''),'provider')
            self.portal_session_key=key;self.portal_session_until=self.clock()+60
        return self.portal_session

    def validate_root(self):
        private.verify(self.root,root=True)
        owner=finite_json(self.root/'owner.json')
        if owner.get('schema')!=SCHEMA or owner.get('node_aid')!=self.node_aid or owner.get('operator')!=private.owner_id():
            raise ValueError('queue_owner_mismatch')
        core.require_identity(self.root,owner.get('root_identity'),True)
        core.require_identity(self.root/'jobs',owner.get('jobs_identity'),True)
        core.require_identity(self.root/'queue.lock',owner.get('lock_identity'))
        private.verify(self.root/'jobs');private.verify(self.root/'queue.lock')
        return owner

    @contextmanager
    def lock(self):
        with self.guard:
            self.validate_root()
            with core.Lock(self.root/'queue.lock'):yield

    def index(self):
        value=finite_json(self.root/'index.json')
        if value.get('schema')!=SCHEMA or value.get('node_aid')!=self.node_aid or not isinstance(value.get('jobs'),dict):
            raise ValueError('queue_index_identity')
        return value

    def container(self,task_id):
        if not core.TASK_ID.fullmatch(task_id):raise ValueError('task_id_invalid')
        path=private.plain(self.root/'jobs'/task_id)
        if path.parent!=self.root/'jobs':raise ValueError('queue_path_escape')
        return path

    def status(self,task_id,**fields):
        root=self.container(task_id);previous=finite_json(root/'status.json')
        if previous.get('schema')!=SCHEMA or previous.get('task_id')!=task_id:raise ValueError('queue_status_identity')
        previous.update(fields,updated_at=utcnow());update_json(root/'status.json',previous)
        return previous

    def attempt_root(self,root):
        value=finite_json(root/'status.json');attempt=value.get('active_attempt',1)
        if type(attempt)is not int or attempt not in (1,2):raise ValueError('preparation_retry_requires_review')
        return root if attempt==1 else private.plain(root/'attempt-2')

    def job_path(self,root):return self.attempt_root(root)/'job'

    def legacy_not_transmitted(self,task,sources,pins,reason='requester_not_opted_in'):
        declarations=[]
        for index,((label,path),pin) in enumerate(zip(sources,pins)):
            measured=bounded_snapshot(path,None,pin)
            declarations.append({'name':'%03d-'%index+path.name[:120],**measured})
        task.pop('external_delivery',None);task.pop('artifact_job',None)
        task['undelivered_attachments']=declarations
        task['file_delivery']={'state':'not_transmitted','reason':reason,'files_verified':False,
            'declared_files':len(declarations),'total_bytes':sum(f['size'] for f in declarations)}
        return []

    def validate_container(self,root,task):
        private.verify(root,root=True);owner=finite_json(root/'owner.json')
        if (owner.get('schema')!=SCHEMA or owner.get('task_id')!=task['id']
                or owner.get('peer')!=task['peer'] or owner.get('provider')!=self.node_aid
                or owner.get('enrollment_id')!=(task.get('terminal_outbox') or {}).get('enrollment_id')):
            raise ValueError('unknown_job_directory')
        core.require_identity(root,owner.get('identity'),True)
        return owner

    def _sources(self,engine,task,report,report_path):
        # report_path is the exact file this Engine just wrote, not a supplied
        # arbitrary path. Every other source passes existing CheckContext gates.
        report_path=private.plain(report_path)
        expected=private.plain(self.store.task_dir(task['id'])/'report.json')
        if report_path!=expected:raise ValueError('report_path_mismatch')
        sources=[('report.json',report_path)]
        names={e['path'] for e in report.get('evidence',[]) if isinstance(e,dict) and isinstance(e.get('path'),str)}
        for check in report.get('checks',[]):
            observed=check.get('observed') or {}
            if isinstance(observed,dict):names.update(m['path'] for m in observed.get('matches',[]) if isinstance(m,dict) and isinstance(m.get('path'),str))
        context=CheckContext(engine.task_work_dir(task),0,())
        for name in sorted(names):
            # Inspect the spelling before CheckContext.resolve follows links.
            spelled=Path(name) if Path(name).is_absolute() else Path(engine.task_work_dir(task))/name
            private.plain(spelled)
            source=context.resolve(name,allow_read_paths=False)
            source=private.plain(source)
            if source.is_file() and source!=report_path:sources.append((name,source))
        if not 1<=len(sources)<=64:raise ValueError('artifact_count_limit')
        return sources

    def prepare(self,engine,task,report,report_path):
        """Called by actual Engine._attach only for a newly completed task."""
        stage='queue_validation_failed'
        try:
            if task['state']!='completed' or task.get('peer')==self.node_aid:raise ValueError('completed_provider_task_required')
            stage='artifact_source_unavailable_or_outside_root'
            sources=self._sources(engine,task,report,report_path)
            self.outbox.check_sources(task,sources)
            source_pins=[source_key(p.stat()) for _,p in sources]
            sizes=[info[2] for info in source_pins]
            if any(not 0<n<=core.MAX_TOTAL for n in sizes):raise ValueError('artifact_size_limit')
            if sum(sizes)<=self.inline_bytes:
                # Ordinary small results do not consume external queue slots or
                # reservations. Keep the Engine's established signed route.
                task.pop('external_delivery',None);task.pop('artifact_job',None)
                return engine._attach_local(task,report,report_path)
            if task.get('spec',{}).get('artifact_delivery')!=ARTIFACT_DELIVERY:
                # Actual required bytes are declared but NOT attached. Old
                # receivers measure a missing-file failure rather than claiming
                # that an empty/partial attachment set completed delivery.
                return self.legacy_not_transmitted(task,sources,source_pins)
            stage='local_artifact_preparation_failed'
            # Keep the reviewed core unchanged: a private selection workspace is
            # copied once from authorized roots, then core makes its own snapshot.
            reserved=2*sum(sizes)+1048576
            root=self.container(task['id'])
            with self.lock():
                index=self.index()
                entry=index['jobs'].get(task['id'])
                if entry is None and root.exists():
                    # Only a complete, matching owner registry can recover the
                    # directory-created/index-not-written crash window.
                    owner=self.validate_container(root,task)
                    finite_json(root/'announcements.json');self.attempt_root(root)
                    if owner.get('reservation')!=reserved:raise ValueError('unknown_job_directory')
                    if len(index['jobs'])>=self.max_jobs or sum(x['reserved_bytes'] for x in index['jobs'].values())+reserved>self.max_bytes:
                        raise ValueError('artifact_queue_quota')
                    entry={'peer':task['peer'],'reserved_bytes':reserved,'created_at':utcnow()}
                    index['jobs'][task['id']]=entry;update_json(self.root/'index.json',index)
                if entry:
                    self.validate_container(root,task)
                    if entry['peer']!=task['peer']:raise ValueError('queued_peer_mismatch')
                    if (self.job_path(root)/'plan.json').is_file() and (self.job_path(root)/'queue.json').is_file():
                        return self._prepared(task,root)
                    if finite_json(root/'announcements.json').get('attempts'):
                        raise ValueError('preparation_retry_after_announcement_refused')
                    if (task.get('terminal_outbox') or {}).get('phase') not in ('not_invoked','preparing','prepared'):
                        raise ValueError('preparation_retry_requires_review')
                    if self.attempt_root(root)!=root or (root/'attempt-2').exists():raise ValueError('preparation_retry_requires_review')
                    if sum(x['reserved_bytes'] for x in index['jobs'].values())+reserved>self.max_bytes:
                        raise ValueError('artifact_queue_quota')
                    if shutil.disk_usage(self.root).free<reserved:raise ValueError('artifact_disk_headroom')
                    attempt=root/'attempt-2';private.create_new(attempt);private.create_new(attempt/'selection')
                    entry['reserved_bytes']+=reserved;update_json(self.root/'index.json',index)
                    self.status(task['id'],state='preparing',active_attempt=2,previous_attempt_preserved=True)
                else:
                    if len(index['jobs'])>=self.max_jobs or sum(x['reserved_bytes'] for x in index['jobs'].values())+reserved>self.max_bytes:
                        raise ValueError('artifact_queue_quota')
                    if shutil.disk_usage(self.root).free<reserved:raise ValueError('artifact_disk_headroom')
                    private.create_new(root);private.create_new(root/'selection')
                    create_json(root/'owner.json',{'schema':SCHEMA,'task_id':task['id'],'peer':task['peer'],
                        'provider':self.node_aid,'identity':core.identity(root,True),'reservation':reserved,
                        'enrollment_id':(task.get('terminal_outbox') or {}).get('enrollment_id')})
                    private.write_new(root/'announce.lock',b'0')
                    create_json(root/'announcements.json',{'schema':SCHEMA,'task_id':task['id'],'attempts':[]})
                    create_json(root/'status.json',{'schema':SCHEMA,'task_id':task['id'],'state':'preparing',
                        'active_attempt':1,'execution_state':'completed','execution_modified':False})
                    index['jobs'][task['id']]={'peer':task['peer'],'reserved_bytes':reserved,'created_at':utcnow()}
                    update_json(self.root/'index.json',index);attempt=root
            selected=[];mapping=[]
            for n,(label,source) in enumerate(sources):
                suffix=source.suffix[:12]
                if not suffix.isascii() or any(c not in '.abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789' for c in suffix):suffix='.bin'
                name='item-%04d'%n+suffix;target=attempt/'selection'/name
                before=bounded_snapshot(source,target,source_pins[n])
                selected.append(name);mapping.append({'original':label,'selected':name,'size':before['size'],'sha256':before['sha256']})
            create_json(attempt/'selection-map.json',mapping)
            core.prepare_completed(attempt/'job',attempt/'selection',{'id':task['id'],'state':'completed',
                'provider':self.node_aid,'requester':task['peer'],'taskd_version':'1.9.4'},selected,self.inline_bytes,
                make_directory=private.create_new)
            for path in [root,*root.rglob('*')]:
                if os.name=='nt':private.verify(path)
            return self._prepared(task,root)
        except (OSError,ValueError,TypeError,KeyError,core.KnownFailure) as error:
            reason=str(error) if str(error) in PREPARATION_REASONS else stage
            if isinstance(error,OSError) and error.errno==errno.ENOSPC:reason='artifact_disk_full'
            if task.get('spec',{}).get('artifact_delivery')!=ARTIFACT_DELIVERY:
                # report.json is a real required result file, never a fabricated
                # sentinel. Preserve an honest failed-file check even when the
                # remaining source set cannot safely be measured.
                path=private.plain(report_path)
                return self.legacy_not_transmitted(task,[('report.json',path)],[source_key(path.stat())],reason)
            task['external_delivery']={'schema':core.SCHEMA,'state':'preparation_failed','reason':reason}
            self.store.log(task['id'],'external_artifacts_preparation_failed',reason=reason)
            try:
                with self.lock():
                    index=self.index();counts=index.setdefault('failure_counts',{})
                    counts[reason]=counts.get(reason,0)+1
                    index['recent_failures']=(index.get('recent_failures',[])+[{'task_id':task['id'],'reason':reason,'at':utcnow()}])[-16:]
                    update_json(self.root/'index.json',index)
            except Exception:pass
            # The terminal summary remains sendable and cannot be made dependent
            # on a large/invalid report or a broken external storage connection.
            path=Path(report_path)
            return [path] if path.is_file() and path.stat().st_size<=self.inline_bytes else []

    def _prepared(self,task,root):
        job=core.Job(self.job_path(root))
        if job.plan['task_id']!=task['id'] or job.plan['provider']!=self.node_aid or job.plan['requester']!=task['peer']:
            raise ValueError('prepared_job_binding_mismatch')
        payload=job.terminal_payload();task['external_delivery']=payload['external_delivery']
        task['artifact_job']={'schema':SCHEMA,'task_id':task['id'],'manifest_sha256':job.mid}
        self.status(task['id'],state='waiting_terminal_proof' if job.mid else 'inline_only',manifest_sha256=job.mid)
        return [Path(p) for p in payload['inline_files']]

    def before_reply(self,task,body,files,state):
        """Durable intent for every actual terminal reply, including size fallback."""
        d=task.get('external_delivery')
        if state!='completed' or not isinstance(d,dict) or d.get('state')!='prepared_not_transmitted':return
        root=self.container(task['id']);job=core.Job(self.job_path(root))
        if job.plan['external_delivery']!=d:raise ValueError('terminal_descriptor_changed')
        with core.Lock(root/'announce.lock'):
            value=finite_json(root/'announcements.json')
            if value.get('schema')!=SCHEMA or value.get('task_id')!=task['id']:raise ValueError('announcement_identity')
            intent={'body_sha256':hashlib.sha256(body.encode('utf-8')).hexdigest(),'body_bytes':len(body.encode('utf-8')),
                'manifest_sha256':job.mid,'files':[{'name':Path(p).name,'size':Path(p).stat().st_size,'sha256':core.sha_file(p)} for p in files or []]}
            if intent not in value['attempts']:
                if len(value['attempts'])>=8:raise ValueError('terminal_intent_limit')
                value['attempts'].append(intent);update_json(root/'announcements.json',value)

    def record_terminal_body(self,task,body,files,state):
        try:self.before_reply(task,body,files,state);return body
        except Exception:
            # External queue damage must not suppress the already-completed
            # business summary. This replacement descriptor cannot pass the
            # worker's fixed-manifest comparison, so uploads remain closed.
            from .protocol import status_line
            task['external_delivery']={'schema':core.SCHEMA,'state':'announcement_failed',
                'reason':'durable_announcement_unavailable','files_verified':False}
            return ('Task reached local '+task['state']+'; external artifact delivery needs attention.\n\n'+
                status_line({'task':task['id'],'state':task['state'],'a2a_state':state,
                    'external_delivery':task['external_delivery'],
                    'attachments':[{'name':Path(p).name,'size':Path(p).stat().st_size,'sha256':core.sha_file(p)} for p in files or []]}))

    def _proof(self,task_id,job,announcements):
        pinned=self.container(task_id)/'terminal-proof.json'
        if pinned.exists():
            private.verify(pinned);value=finite_json(pinned)
            if value.get('schema')!=SCHEMA or value.get('manifest_sha256')!=job.mid:
                raise ProofUnavailable('pinned_proof_identity_mismatch')
            proof=value['proof']
        else:
            if self.proof_source is None or self.proof_verifier is None:raise ProofUnavailable('provider_exact_result_proof_unavailable')
            full,record=self.proof_source.fetch(task_id)
            proof=self.proof_verifier(full,record,local_aid=self.node_aid,role='provider')
        if proof.get('task_id')!=task_id or proof.get('external_delivery')!=job.plan['external_delivery']:
            raise ProofUnavailable('proof_descriptor_or_task_mismatch')
        if proof.get('provider_aid')!=self.node_aid or proof.get('requester_aid')!=job.plan['requester']:
            raise ProofUnavailable('proof_party_mismatch')
        if (proof.get('receipt_verified')!='verified' or proof.get('signed_terminal_verified') is not True
                or proof.get('terminal_local_commit_verified') is not True or not proof.get('message_id')):
            raise ProofUnavailable('terminal_signature_or_local_commit_unverified')
        if not any(x['body_sha256']==proof.get('terminal_body_sha256') and x['manifest_sha256']==job.mid for x in announcements['attempts']):
            raise ProofUnavailable('proof_not_a_recorded_terminal_intent')
        if not pinned.exists():
            public={key:proof.get(key) for key in ('task_id','provider_aid','requester_aid','external_delivery',
                'receipt_verified','transport_delivery','terminal_body_sha256','request_cid','result_cid',
                'receipt_cid','message_id','effect_status','verifier_sha256','terminal_ack_verified',
                'signed_terminal_verified','terminal_local_commit_verified')}
            public['verifier_sha256']=(proof.get('verification') or {}).get('verifier_sha256',public['verifier_sha256'])
            create_json(pinned,{'schema':SCHEMA,'manifest_sha256':job.mid,'proof':public,'verified_at':utcnow()})
        return proof

    def delivery_delay(self):
        # A waiting job must not back off another ready job in this worker.
        with self.lock():
            ids=[tid for tid in self.index()['jobs']
                if finite_json(self.container(tid)/'status.json').get('state')!='recipient_reported']
            return min((max(0.0,self.job_due.get(tid,0)-self.clock()) for tid in ids),default=5.0)

    def tick(self):
        """Daemon-owned separate loop, one job and finite transfer step per tick."""
        self.last_step_progress=False
        lane=self.tick_lane;self.tick_lane=(lane+1)%2
        if lane==0 and self.outbox.tick():
            self.last_step_progress=self.outbox.last_step_progress
            if self.last_step_progress:return
        with self.lock():
            index=self.index();ids=[]
            for candidate in sorted(index['jobs']):
                state=finite_json(self.container(candidate)/'status.json')
                if state.get('state')=='recipient_reported' or self.job_due.get(candidate,0)>self.clock():continue
                ids.append(candidate)
            if not ids:return
            cursor=index.get('cursor',0)%len(ids);task_id=ids[cursor]
            index['cursor']=(cursor+1)%len(ids);update_json(self.root/'index.json',index)
        try:self._advance_task(task_id)
        finally:
            # Waiting/finished jobs never impose their backoff on an active job.
            self.job_due[task_id]=self.clock()+(0 if self.last_step_progress else 5)

    def _advance_task(self,task_id):
        root=self.container(task_id)
        if not (self.job_path(root)/'plan.json').exists():return
        job=core.Job(self.job_path(root))
        if not job.mid:return
        task=self.store.load(task_id)
        if not task or task.get('state')!='completed' or task.get('peer')!=job.plan['requester']:
            self.status(task_id,state='blocked_local_execution_or_peer');return
        if self.outbox.valid(task) and task['terminal_outbox'].get('phase') not in ('rpc_acknowledged','local_commit_verified'):
            self.status(task_id,state='waiting_original_commit_reconciliation',terminal_transport_ack='not_verified');return
        announcements=finite_json(root/'announcements.json')
        if not announcements.get('attempts'):
            self.status(task_id,state='waiting_terminal_intent');return
        try:
            proof=self._proof(task_id,job,announcements)
        except ProofUnavailable as error:
            self.status(task_id,state='proof_unavailable',reason=str(error));return
        except Exception:
            self.status(task_id,state='proof_unconfirmed',reason='terminal_proof_validation_failed');return
        # These are public hashes/evidence identifiers, never receipt bytes/KEL,
        # credentials or an invented assertion that a local ACK was delivery.
        self.status(task_id,state='terminal_local_commit_verified',terminal_transport_ack='not_verified',
            recipient_files_verified=False,proof={k:proof.get(k) for k in
            ('task_id','request_cid','result_cid','receipt_cid','terminal_body_sha256','message_id','receipt_verified',
                'signed_terminal_verified','terminal_local_commit_verified')})
        job.observe_terminal({'task_id':task_id,'state':'completed','peer':job.plan['requester'],
            'receipt_verified':'verified','effect_status':proof.get('effect_status')})
        session=None
        try:
            binding=self.binding_loader()  # Existing standard binding, memory only.
            session=self._portal_session(binding)
            previous_state=job.state.get('state');previous_pending=job.state.get('pending')
            observations=[]
            class ObservedSession:
                aid,role=session.aid,session.role
                def json(self,value):
                    answer=session.json(value)
                    if value.get('op')=='part_status':observations.append(('part_status',value,answer))
                    return answer
                def request(self,*args,**kwargs):
                    answer=session.request(*args,**kwargs)
                    observations.append(('part',args[1],answer));return answer
            value=job.advance(ObservedSession())
            event=(value.get('events') or [{}])[-1].get('event')
            if event in ('begin_acknowledged','part_acknowledged','immutable_part_replay_acknowledged','pending_reconciled','chunk_completion_observed',
                    'server_storage_acknowledged','server_storage_observed'):
                normalized=[]
                for kind,key,answer in observations:
                    normalized.append((kind,key,{k:answer.get(k) for k in ('chunk_verified','sha256','parts')}))
                token=hashlib.sha256(core.canonical([job.mid,event,previous_pending,normalized,value.get('state')])).hexdigest()
                self.last_step_progress=self.progress_tokens.get(task_id)!=token and (
                    event!='server_storage_observed' or previous_state not in ('server_stored','recipient_reported'))
                self.progress_tokens[task_id]=token
            self.status(task_id,state=value['state'],server_files_verified=value.get('server_files_verified',False),
                recipient_files_verified=False,terminal_transport_ack='not_verified',
                execution_state='completed',execution_modified=False,
                last_delivery_event=(value.get('events') or [{}])[-1].get('event'))
        except Exception:
            self._close_portal_session()
            self.status(task_id,state='delivery_unconfirmed',reason='portal_session_or_delivery_failed')

    def public_state(self):
        try:
            with self.lock():index=self.index()
            counts={}
            for task_id in index['jobs']:
                state=finite_json(self.container(task_id)/'status.json').get('state','unknown')
                counts[state]=counts.get(state,0)+1
            return {'enabled':True,'ready':bool(self.proof_source and self.proof_verifier and self.outbox.engine),
                'readiness_scope':'local_queue_and_proof_adapter_only','portal_access':'not_verified',
                'jobs':len(index['jobs']),'states':counts,
                'capacity':{'reserved_bytes':sum(x['reserved_bytes'] for x in index['jobs'].values()),
                    'max_bytes':self.max_bytes,'max_jobs':self.max_jobs,'free_bytes':shutil.disk_usage(self.root).free},
                'failure_counts':index.get('failure_counts',{}),'recent_failures':index.get('recent_failures',[]),
                'retention':'owned_external_jobs_preserved_no_automatic_deletion',
                'terminal_outbox':self.outbox.public_state(),
                'proof_capability':self.proof_source is not None and self.proof_verifier is not None,
                'scope':'independent_provider_artifact_delivery'}
        except Exception:return {'enabled':True,'state':'queue_validation_failed','ready':False}


def from_node(node,config,transport,store,**kwargs):
    settings=config.get('provider_artifacts') or {}
    if settings.get('enabled') is not True:return None
    directory=private.plain(settings['queue_dir'])
    for excluded in (Path(node.data_dir),Path(config['work_dir']),Path(store.root)):
        excluded=private.plain(excluded)
        if directory==excluded or directory in excluded.parents or excluded in directory.parents:
            raise ValueError('independent_artifact_queue_required')
    # Uses exactly this node's existing connection location, as directory.py and
    # the connector already do. It is not copied, modified, printed or exported.
    connection=Path(node.data_dir)/'portal/connection.json'
    def load_binding():return finite_json(connection)
    binding=load_binding();aid=transport.node_aid()
    if not aid or binding.get('aid')!=aid:raise ValueError('existing_provider_identity_mismatch')
    worker=ProviderArtifacts(directory=directory,node_aid=aid,binding_loader=load_binding,
        transport=transport,store=store,max_jobs=settings.get('max_jobs',MAX_JOBS),
        max_bytes=settings.get('max_bytes',MAX_BYTES),inline_bytes=settings.get('inline_bytes',700000),**kwargs)
    from .transports.anet import AnetTransport
    worker.recovery_transport=AnetTransport(anet=transport.anet,data_dir=node.data_dir)
    if not kwargs.get('proof_source') and not kwargs.get('proof_verifier'):
        from .terminal_source import SingleTaskSource
        from .terminal_proof import LocalSelfVerifier,verify_terminal_result
        executable=Path(shutil.which(str(transport.anet)) or transport.anet).resolve()
        binary_hash=core.sha_file(executable)
        proof_dir=worker.root/'proof-verifier'
        if not proof_dir.exists():private.create_new(proof_dir)
        private.verify(proof_dir,root=True)
        def owns(task_id):
            with worker.lock():entry=worker.index()['jobs'].get(task_id)
            record=store.load(task_id)
            enrolled=record and worker.outbox.valid(record) and record['terminal_outbox'].get('phase') in (
                'invoking','unknown','rpc_acknowledged','local_commit_verified')
            return bool(record and (enrolled or (entry and
                record.get('state')=='completed' and record.get('peer')==entry['peer'])))
        worker.proof_source=SingleTaskSource(executable,node.data_dir,local_aid=aid,role='provider',
            allowed_task=owns,binary_sha256=binary_hash)
        verifier=LocalSelfVerifier(executable,proof_dir,private.verify,local_aid=aid,
            data_dir=node.data_dir,binary_sha256=binary_hash)
        worker.proof_verifier=partial(verify_terminal_result,verifier=verifier)
    return worker

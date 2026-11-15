"""Independent requester file worker. Relay offer/result never performs network I/O.

Only receipt-covered terminal bytes authorize download. Existing source-session
delivery stays in Relay and waits for this worker's measured, immutable receipts.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time

from . import attachments, private_artifact_dir as private
from .artifact_core import auto_artifacts as core
from .terminal_proof import verify_terminal_result, ProofUnavailable, strict_json
from .terminal_source import SingleTaskSource, bounded_cli

SCHEMA='taskd-requester-artifacts/1'
MAX_JOBS=32
MAX_BYTES=2*1024*1024*1024
PULL_BUDGET=2*1024*1024
SAFE_REASONS={
    'requester_queue_budget','requester_disk_space','candidate_changed','unknown_requester_job',
    'initial_owner_missing_preserved','initial_owner_mismatch','initial_unknown_file_preserved',
    'initial_record_conflict_preserved','initial_lock_conflict_preserved','inline_source_size_changed',
    'inline_source_open_changed','inline_source_grew_before_write','inline_source_changed',
    'inline_existing_file_differs','received_bytes_changed','persisted_proof_pin_mismatch',
    'inline_loader_unavailable','inline_all_history_pull_budget_unavailable','proof_source_unavailable',
    'public_task_nonce_store_unavailable','interaction_schema_not_3','task_not_owned_by_artifact_queue',
    'proof_cli_timeout','proof_cli_output_limit','requester_public_kel_unavailable',
    'receipt_not_verified','receipt_cid_binding','local_node_identity_changed',
    'offline_signature_or_content_rejected','receipt_covered_descriptor_missing'}

def safe_reason(error):
    reason=str(error)
    return reason if reason in SAFE_REASONS else 'requester_artifact_step_unavailable'

def read(path):
    path=private.plain(path);private.verify(path)
    if not path.is_file() or path.stat().st_size>1048576:raise ValueError('queue_record_size')
    return strict_json(path.read_text(encoding='utf-8'))

def create(path,value):
    path=private.plain(path)
    with private.open_new(path) as stream:
        stream.write(core.canonical(value));stream.flush();os.fsync(stream.fileno())
    private.verify(path)

def update(path,value):
    path=private.plain(path);private.verify(path)
    temp=path.with_name(path.name+'.new-'+os.urandom(12).hex())
    create(temp,value);os.replace(temp,path)

def fingerprint(path):
    private.verify(path);s=path.stat()
    return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]

def bounded_copy(source,target,expected_size,expected_sha):
    """Never write an extra source byte, including growth after the first stat."""
    source=private.plain(source);target=private.plain(target)
    before=source.stat()
    # Windows Python may expose birth-time as stat.ctime and change-time as
    # fstat.ctime. Compare ctime only within the same API, never across them.
    key=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
    same_api=lambda s:(*key(s),s.st_ctime_ns)
    if before.st_size!=expected_size:raise ValueError('inline_source_size_changed')
    with source.open('rb') as inp:
        opened=os.fstat(inp.fileno())
        if key(before)!=key(opened):raise ValueError('inline_source_open_changed')
        with private.open_new(target) as out:
            left=expected_size;digest=hashlib.sha256()
            while True:
                block=inp.read(min(65536,left+1))
                if len(block)>left:raise ValueError('inline_source_grew_before_write')
                if not block:break
                out.write(block);digest.update(block);left-=len(block)
            out.flush();os.fsync(out.fileno())
            after=os.fstat(inp.fileno())
        current=source.stat()
        if left or digest.hexdigest()!=expected_sha or same_api(opened)!=same_api(after) or same_api(before)!=same_api(current) or key(after)!=key(current):
            raise ValueError('inline_source_changed')
    private.verify(target)

def bounded_measure(path,expected_size,expected_sha):
    path=private.plain(path);before=path.stat()
    key=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
    same_api=lambda s:(*key(s),s.st_ctime_ns)
    if before.st_size!=expected_size:return False
    with path.open('rb') as stream:
        opened=os.fstat(stream.fileno())
        if key(before)!=key(opened):return False
        left=expected_size;digest=hashlib.sha256()
        while True:
            block=stream.read(min(65536,left+1))
            if len(block)>left:return False
            if not block:break
            digest.update(block);left-=len(block)
        after=os.fstat(stream.fileno())
    current=path.stat()
    return not left and digest.hexdigest()==expected_sha and same_api(opened)==same_api(after) and same_api(before)==same_api(current) and key(after)==key(current)

class RequesterArtifacts:
    def __init__(self, *, directory,node_aid,binding_loader,proof_source,proof_verifier,
                 inline_loader=None,session_factory=core.PortalSession,clock=time.time,stop_requested=lambda:False):
        self.root=private.plain(directory);self.node_aid=node_aid;self.binding_loader=binding_loader
        self.proof_source=proof_source;self.proof_verifier=proof_verifier;self.inline_loader=inline_loader
        self.session_factory=session_factory;self.clock=clock;self.stop_requested=stop_requested
        self.guard=threading.RLock();self.busy=threading.Lock()
        self.last_step_progress=False;self.batch_should_yield=lambda:False
        if any(p.name.lower().startswith('.anet') for p in (self.root,*self.root.parents)):
            raise ValueError('requester_files_must_be_outside_anet')
        if not self.root.exists():
            private.create_new(self.root)
            for child in ('jobs','downloads','verification'):private.create_new(self.root/child)
            create(self.root/'owner.json',{'schema':SCHEMA,'aid':node_aid,'operator':private.owner_id(),
                'root':core.identity(self.root,True),'jobs':core.identity(self.root/'jobs',True),
                'downloads':core.identity(self.root/'downloads',True)})
            private.write_new(self.root/'queue.lock',b'0')
            create(self.root/'index.json',{'schema':SCHEMA,'jobs':{},'cursor':0})
        self.validate()

    def validate(self):
        private.verify(self.root,root=True);owner=read(self.root/'owner.json')
        if owner.get('schema')!=SCHEMA or owner.get('aid')!=self.node_aid or owner.get('operator')!=private.owner_id():
            raise ValueError('requester_queue_owner')
        for name in ('root','jobs','downloads'):
            path=self.root if name=='root' else self.root/name
            private.verify(path,root=True);core.require_identity(path,owner[name],True)
        private.verify(self.root/'queue.lock')

    @contextmanager
    def lock(self):
        with self.guard:
            self.validate()
            with core.Lock(self.root/'queue.lock'):yield

    def owned_task(self,tid):
        with self.lock():return any(v['task_id']==tid for v in read(self.root/'index.json')['jobs'].values())

    failure_reason=staticmethod(safe_reason)

    def offer(self,task_id,message,env,peer):
        try:return self._offer(task_id,message,env,peer)
        except (ValueError,OSError,core.KnownFailure) as error:
            code=safe_reason(error)
            with self.lock():
                index=read(self.root/'index.json');counts=index.setdefault('failure_counts',{})
                counts[code]=counts.get(code,0)+1
                index['last_failure']={'reason':code,'task_id':task_id if core.TASK_ID.fullmatch(str(task_id)) else None}
                update(self.root/'index.json',index)
            raise ValueError(code) from None

    def _offer(self,task_id,message,env,peer):
        """Untrusted candidate only. No CLI, proof, portal or callback here."""
        if not core.TASK_ID.fullmatch(str(task_id)) or not isinstance(message.get('id'),str):raise ValueError('terminal_candidate_id')
        if env.get('state')!='completed':raise ValueError('terminal_candidate_state')
        descriptor=env.get('external_delivery')
        if descriptor is not None:core.validate_descriptor(descriptor)
        key=hashlib.sha256((task_id+'\0'+message['id']).encode()).hexdigest()
        record={'schema':SCHEMA,'task_id':task_id,'peer':peer,'message_id':message['id'],
            'body_sha256':hashlib.sha256(message['text'].encode()).hexdigest(),'descriptor':descriptor,
            'local_aid':self.node_aid}
        reserve=(descriptor or {}).get('total_bytes',0)+PULL_BUDGET
        with self.lock():
            index=read(self.root/'index.json');root=self.root/'jobs'/key
            if key in index['jobs']:
                entry=index['jobs'][key]
                if entry.get('phase')=='creating':
                    if entry.get('record')!=record:raise ValueError('candidate_changed')
                    self._ensure_job(root,record)
                    entry.update(phase='active');update(self.root/'index.json',index)
                if read(root/'record.json')!=record:raise ValueError('candidate_changed')
                return key
            if len(index['jobs'])>=MAX_JOBS or sum(v['reserve'] for v in index['jobs'].values())+reserve>MAX_BYTES:
                raise ValueError('requester_queue_budget')
            if root.exists():raise ValueError('unknown_requester_job')
            if shutil.disk_usage(self.root).free<reserve:raise ValueError('requester_disk_space')
            # Persist allocation before any directory creation. Restart may only
            # complete the same owned initial files; it cannot create a new task.
            index['jobs'][key]={'task_id':task_id,'reserve':reserve,'phase':'creating','record':record}
            update(self.root/'index.json',index)
            self._ensure_job(root,record)
            index['jobs'][key]['phase']='active';update(self.root/'index.json',index)
        return key

    def _ensure_job(self,root,record):
        expected_owner={'schema':SCHEMA,'record_sha256':core.digest(core.canonical(record))}
        if not root.exists():
            private.create_new(root)
            create(root/'initial-owner.json',dict(expected_owner,identity=core.identity(root,True)))
        private.verify(root,root=True)
        owner_path=root/'initial-owner.json'
        if not owner_path.exists():raise ValueError('initial_owner_missing_preserved')
        owner=read(owner_path)
        if any(owner.get(k)!=v for k,v in expected_owner.items()):raise ValueError('initial_owner_mismatch')
        core.require_identity(root,owner.get('identity'),True)
        allowed={'initial-owner.json','record.json','status.json','worker.lock'}
        if any(p.name not in allowed for p in root.iterdir()):raise ValueError('initial_unknown_file_preserved')
        initial={'state':'proof_pending','files_verified':False,'next_at':0}
        for name,value in (('record.json',record),('status.json',initial)):
            path=root/name
            if path.exists():
                if read(path)!=value:raise ValueError('initial_record_conflict_preserved')
            else:create(path,value)
        lock=root/'worker.lock'
        if lock.exists():
            private.verify(lock)
            if lock.read_bytes()!=b'0':raise ValueError('initial_lock_conflict_preserved')
        else:
            private.write_new(lock,b'0')

    def result(self,key):
        """Read local outcome. Stat-only freshness check keeps Relay short."""
        if not isinstance(key,str) or len(key)!=64 or any(c not in '0123456789abcdef' for c in key):raise ValueError('queue_key')
        root=private.plain(self.root/'jobs'/key);state=read(root/'status.json')
        if state.get('files_verified'):
            for file in state.get('files',[]):
                path=private.plain(file['path'])
                if not path.is_relative_to(self.root/'downloads') or fingerprint(path)!=file['fingerprint']:
                    return {'state':'local_files_changed','files_verified':False}
        return state

    def public_state(self):
        with self.lock():index=read(self.root/'index.json')
        states={}
        for key,entry in index['jobs'].items():
            value='initial_allocation_pending' if entry.get('phase')=='creating' else self.result(key).get('state','unknown')
            states[value]=states.get(value,0)+1
        return {'enabled':True,'ready':bool(self.proof_source and self.proof_verifier),
            'readiness_scope':'local_queue_and_proof_adapter_only','portal_access':'not_verified',
            'jobs':len(index['jobs']),'states':states,'network_in_relay':False,
            'capacity':{'max_jobs':MAX_JOBS,'max_bytes':MAX_BYTES,
                        'reserved_bytes':sum(v['reserve'] for v in index['jobs'].values()),
                        'free_bytes':shutil.disk_usage(self.root).free},
            'failure_counts':index.get('failure_counts',{}),'last_failure':index.get('last_failure')}

    def _status(self,root,**fields):
        value=read(root/'status.json');value.update(fields,at=self.clock(),execution_modified=False)
        update(root/'status.json',value);return value

    def delivery_delay(self):
        with self.lock():
            states=[self.result(key) for key,entry in read(self.root/'index.json')['jobs'].items()
                if entry.get('phase','active')=='active']
            return min((max(0.0,state.get('next_at',0)-self.clock()) for state in states
                if not state.get('files_verified') and state.get('state')!='rejected_uncovered_message'),default=5.0)

    def tick(self):
        self.last_step_progress=False
        if self.stop_requested() or not self.busy.acquire(False):return
        try:
            with self.lock():
                index=read(self.root/'index.json');keys=[]
                for candidate,entry in index['jobs'].items():
                    if entry.get('phase','active')!='active':continue
                    state=self.result(candidate)
                    if state.get('files_verified') or state.get('state')=='rejected_uncovered_message' or state.get('next_at',0)>self.clock():continue
                    keys.append(candidate)
                if not keys:return
                cursor=index.get('cursor',0)%len(keys);key=keys[cursor]
                index['cursor']=cursor+1;update(self.root/'index.json',index)
            root=self.root/'jobs'/key;state=self.result(key)
            if state.get('state')=='rejected_uncovered_message' or state.get('next_at',0)>self.clock():return
            if state.get('files_verified'):return
            try:
                with core.Lock(root/'worker.lock'):self._advance(root,key)
            except ProofUnavailable as error:self._status(root,state='proof_not_ready',files_verified=False,reason=safe_reason(error),next_at=self.clock()+30)
            except (ValueError,OSError,core.KnownFailure,core.UnknownOutcome,KeyError,TypeError) as error:
                self._status(root,state='waiting_files_or_access',files_verified=False,reason=safe_reason(error),next_at=self.clock()+30)
        finally:self.busy.release()

    def _proof(self,root,record):
        pin=root/'proof.json'
        if pin.exists():
            value=read(pin)
            if (value.get('task_id')!=record['task_id'] or value.get('message_id')!=record['message_id']
                    or value.get('terminal_body_sha256')!=record['body_sha256'] or value.get('external_delivery')!=record['descriptor']
                    or value.get('provider_aid')!=record['peer'] or value.get('requester_aid')!=self.node_aid
                    or value.get('signed_terminal_verified') is not True or value.get('receipt_verified')!='verified'):
                raise ValueError('persisted_proof_pin_mismatch')
            return value
        if not self.proof_source or not self.proof_verifier:raise ProofUnavailable('proof_source_unavailable')
        full,result=self.proof_source.fetch(record['task_id'])
        proof=verify_terminal_result(full,result,local_aid=self.node_aid,role='requester',verifier=self.proof_verifier,require_descriptor=False)
        if (proof['message_id']!=record['message_id'] or proof['terminal_body_sha256']!=record['body_sha256']
                or proof['external_delivery']!=record['descriptor'] or proof['provider_aid']!=record['peer']):
            self._status(root,state='rejected_uncovered_message',files_verified=False,reason='message_not_receipt_covered')
            return None
        transcript=strict_json(result['result']);entries=transcript['messages'] if isinstance(transcript,dict) else transcript
        last=[m for m in entries if m.get('from')=='provider'][-1]
        inline=[]
        for f in last.get('attachments',[]):
            core.validate_name(f['name']);sha=attachments.cid_sha256(f['cid'])
            if sha is None or type(f['size']) is not int or not 0<=f['size']<=core.INLINE_BUDGET:raise ValueError('inline_cid_or_size')
            inline.append({'name':f['name'],'size':f['size'],'sha256':sha,'cid':f['cid']})
        if sum(f['size'] for f in inline)>core.INLINE_BUDGET or len(inline)>64:raise ValueError('inline_budget')
        if inline and (result.get('local_pull_bytes',PULL_BUDGET+1)>PULL_BUDGET or result.get('local_pull_count',65)>64):
            raise ProofUnavailable('inline_all_history_pull_budget_unavailable')
        pinned={k:proof[k] for k in ('task_id','provider_aid','requester_aid','peer','role','state','message_role','message_id',
            'request_cid','result_cid','receipt_cid','receipt_sha256','receipt_verified','terminal_body_sha256','external_delivery',
            'verification','signed_terminal_verified','terminal_local_commit_verified','terminal_ack_verified')}
        pinned['inline']=inline;create(pin,pinned);return pinned

    def _inline(self,root,key,pin):
        files=pin['inline']
        if not files:return []
        path=root/'inline-receipt.json'
        if path.exists():return read(path)['files']
        if self.inline_loader is None:raise ProofUnavailable('inline_loader_unavailable')
        destination=self.root/'downloads'/('inline-'+key)
        if not destination.exists():private.create_new(destination)
        # Existing supported ANet pull is idempotent into its same owned output;
        # a failed call is reconciled from bytes, never a business-task replay.
        result=self.inline_loader(pin['task_id'],destination,files)
        measured=self._measure(result)
        if [{k:f[k] for k in ('name','size','sha256')} for f in measured]!=[{k:f[k] for k in ('name','size','sha256')} for f in files]:
            raise ValueError('inline_file_set')
        create(path,{'files':measured});return measured

    def _measure(self,files):
        measured=[]
        for f in files:
            path=private.plain(f['path'])
            if not path.is_relative_to(self.root/'downloads'):raise ValueError('received_path_escape')
            before=fingerprint(path)
            if not bounded_measure(path,f['size'],f['sha256']) or fingerprint(path)!=before:raise ValueError('received_bytes_changed')
            measured.append(dict(f,path=str(path),fingerprint=before))
        return measured

    def _advance(self,root,key):
        record=read(root/'record.json');pin=self._proof(root,record)
        if pin is None:return
        inline=self._inline(root,key,pin)
        descriptor=pin['external_delivery']
        if descriptor is None:
            self._status(root,state='verified' if inline else 'not_required',files_verified=True,
                         files=self._measure(inline),next_at=0);self.last_step_progress=True;return
        job=core.RecipientJob.create(root/'recipient',descriptor,pin,self.node_aid,
            self.root/'downloads'/('external-'+key),make_directory=private.create_new)
        private.verify(job.root,root=True)
        binding=self.binding_loader()
        if not isinstance(binding,dict) or not isinstance(binding.get('portal'),str):raise ValueError('existing_binding_unavailable')
        session=self.session_factory(binding,self.node_aid,binding['portal'],'requester')
        owner=self
        destination=self.root/'downloads'/('external-'+key)
        def measured_extent():
            total=0
            if destination.exists():
                for path in destination.iterdir():
                    private.plain(path)
                    if path.is_file():total+=path.stat().st_size
                partials=destination/'.delivery/parts'
                if partials.exists():
                    for path in partials.glob('*.partial'):
                        private.plain(path);total+=path.stat().st_size
            return total
        before_extent=measured_extent()
        class BoundedSession:
            aid,role=session.aid,session.role
            remaining=64
            yielded=False
            def permit(self):
                if owner.stop_requested() or owner.batch_should_yield() or self.remaining<=0:
                    self.yielded=True;raise core.UnknownOutcome('cooperative_worker_yield')
                self.remaining-=1
            def request(self,*a,**k):self.permit();return session.request(*a,**k)
            def json(self,v):
                # RecipientJob already persisted send intent. Never manufacture
                # an unknown send by yielding locally between intent and POST.
                if v.get('op')!='received':self.permit()
                return session.json(v)
        bounded=BoundedSession()
        try:state=job.advance(bounded,make_directory=private.create_new)
        finally:session.close()
        if not state.get('files_verified'):
            progressed=bounded.yielded and measured_extent()>before_extent
            self.last_step_progress=progressed
            self._status(root,state=state['state'],files_verified=False,next_at=0 if progressed else self.clock()+5);return
        files=[dict(f,path=str(Path(state['directory'])/f['name'])) for f in state['files']]
        self._status(root,state='verified',files_verified=True,files=self._measure(inline+files),next_at=0,
                     recipient_confirmation='reported_verified',requester_acceptance='not_verified')
        self.last_step_progress=True

class ExistingInlinePull:
    """Bounded supported CLI, no token reader or unverified URL fetch."""
    def __init__(self,executable,data_dir):self.exe=executable;self.env=dict(os.environ,ANET_DATA_DIR=str(data_dir))
    def __call__(self,task_id,destination,files):
        pulled=destination/'pulled';published=destination/'files'
        for path in (pulled,published):
            if not path.exists():private.create_new(path)
            private.verify(path,root=True)
        def find():
            paths=list(pulled.rglob('*'))
            if len(paths)>256:raise ValueError('inline_pull_file_limit')
            found=[]
            for f in files:
                matches=[]
                for path in paths:
                    private.plain(path)
                    if path.is_file() and bounded_measure(path,f['size'],f['sha256']):matches.append(path)
                if not matches:return None
                target=published/('file-%04d'%len(found))
                if target.exists():
                    private.verify(target)
                    if not bounded_measure(target,f['size'],f['sha256']):raise ValueError('inline_existing_file_differs')
                else:
                    bounded_copy(matches[0],target,f['size'],f['sha256'])
                found.append(dict(f,path=str(target)))
            return found
        result=find()
        if result is not None:return result
        bounded_cli(self.exe,['pull',task_id,'--out',str(pulled)],self.env,limit=1048576,timeout=30)
        result=find()
        if result is None:raise ValueError('inline_pull_missing_verified_bytes')
        return result

def from_node(node,config,transport,store,*,binding_loader=None,**kwargs):
    """Explicit candidate opt-in only; does not edit existing configuration."""
    settings=config.get('requester_artifacts') or {}
    if settings.get('enabled') is not True:return None
    directory=private.plain(settings['queue_dir'])
    for excluded in (Path(node.data_dir),Path(store.root),Path(config.get('work_dir') or store.root)):
        excluded=private.plain(excluded)
        if directory==excluded or directory in excluded.parents or excluded in directory.parents:
            raise ValueError('independent_requester_artifact_queue_required')
    if binding_loader is None:
        connection=Path(node.data_dir)/'portal/connection.json'
        def binding_loader():
            path=private.plain(connection)
            if not path.is_file() or path.stat().st_size>1048576:raise ValueError('existing_binding_unavailable')
            return strict_json(path.read_text(encoding='utf-8'))  # normal existing binding in RAM only
    binding=binding_loader();aid=transport.node_aid()
    if not aid or binding.get('aid')!=aid:raise ValueError('existing_requester_identity_mismatch')
    executable=Path(shutil.which(str(transport.anet)) or transport.anet).resolve()
    worker=RequesterArtifacts(directory=directory,node_aid=aid,binding_loader=binding_loader,
        proof_source=None,proof_verifier=None,inline_loader=ExistingInlinePull(executable,node.data_dir),**kwargs)
    def owns(task_id):
        record=store.load_tracked(task_id)
        return bool(record and worker.owned_task(task_id))
    binary_hash=core.sha_file(executable)
    worker.proof_source=SingleTaskSource(executable,node.data_dir,local_aid=aid,role='requester',
        allowed_task=owns,binary_sha256=binary_hash)
    from .terminal_proof import OfflineVerifier
    worker.proof_verifier=OfflineVerifier(executable,worker.root/'verification',private.verify,binary_sha256=binary_hash)
    return worker

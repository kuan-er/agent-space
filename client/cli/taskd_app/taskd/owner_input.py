"""Provider-local owner decisions, with redacted original-requester notices.

Answers and native details stay in RAM. Disk records contain only opaque
identities and phases. No remote message can invoke the answer path.
"""
from __future__ import annotations
import copy,hashlib,json,os,threading,time,uuid
from pathlib import Path

from owned_source_product import private_directory as private
from owned_source_product.records import atomic_json,create_json
from owned_source_product.registry import identity

SCHEMA='taskd-provider-owner-input/1'
METHODS={
 'codex':{'item/commandExecution/requestApproval':'permission','item/fileChange/requestApproval':'permission','item/tool/requestUserInput':'question'},
 'claude_code':{'claude/tool/requestApproval':'permission','claude/tool/requestUserInput':'question'},
 'deepseek_harness':{'deepseek/approval/request':'permission','deepseek/user/requestInput':'question'}}
IDENTITY=('broker_instance','challenge_id','task_id','operation_id','source_id','native_id','request_id')
NO_RESEND=('answer_intent','answer_effect_unverified','response_recorded','response_sent')
class OwnerInputError(RuntimeError):pass

def bounded(value,limit=65536):
    raw=json.dumps(value,ensure_ascii=False,allow_nan=False)
    if len(raw.encode('utf-8'))>limit:raise OwnerInputError('owner input size limit')
    return json.loads(raw)
def uuid_text(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value:raise OwnerInputError('canonical UUID required')
    return value
def text(value):
    if not isinstance(value,str) or not value or len(value)>512 or any(ord(c)<32 for c in value):raise OwnerInputError('bounded identity required')
    return value
def read_private(path,limit=262144):
    private.verify(path);info=path.stat()
    if info.st_nlink!=1 or info.st_size>limit:raise OwnerInputError('ordinary bounded owner metadata required')
    with path.open('rb') as stream:
        before=os.fstat(stream.fileno());raw=stream.read(limit+1);after=os.fstat(stream.fileno())
    now=path.stat();token=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_nlink)
    if len(raw)>limit or token(before)!=token(after) or token(before)!=token(now):raise OwnerInputError('owner metadata changed')
    value=json.loads(raw)
    if not isinstance(value,dict):raise OwnerInputError('owner metadata object required')
    return value
def ensure_root(node_root):
    node=private.plain(node_root);root=node/'owner-input'
    if not root.exists():private.create_new(root)
    private.verify(root,root=True)
    owner=root/'owner.json';expected={'schema':SCHEMA,'operator':private.owner_id(),'node':identity(node),'root':identity(root)}
    if not owner.exists():create_json(owner,expected)
    if read_private(owner)!=expected:raise OwnerInputError('owner input root identity differs')
    for name in ('requests','client-attempts'):
        path=root/name
        if not path.exists():private.create_new(path)
        private.verify(path,root=True)
    return root

class OwnerInputBroker:
    def __init__(self,node_root,notify,*,clock=time.time,max_pending=16):
        self.root=ensure_root(node_root);self.instance=str(uuid.uuid4());self.notify=notify;self.clock=clock
        self.max_pending=max_pending;self.lock=threading.RLock();self.pending={};self.by_native={};self.closed=False
    def context(self,task_id,operation_id,harness,active,requester_aid=None):
        text(task_id);uuid_text(operation_id)
        if harness not in METHODS or not callable(active):raise OwnerInputError('unsupported owner input context')
        return _Context(self,task_id,operation_id,harness,active,requester_aid)
    def _path(self,challenge):return self.root/'requests'/(uuid_text(challenge)+'.json')
    def _save(self,record):atomic_json(self._path(record['challenge_id']),record)
    def _historical(self,challenge):
        path=self._path(challenge)
        return read_private(path) if path.exists() else None
    @staticmethod
    def _key(identity):return tuple(identity[k] for k in ('task_id','operation_id','source_id','native_id','request_id'))
    def offer(self,context,service,operation,row):
        row=bounded(row);kind=METHODS[context.harness].get(row.get('method'))
        if (not kind or not isinstance(row.get('detail'),dict) or row.get('operation_id')!=context.operation_id or row.get('source_id')!=operation['source_id']
            or row.get('native_id')!=operation['native_session_id'] or row.get('requires_user_action') is not True):return
        rid=row.get('request_id');text(rid)
        if not (rid.startswith('native-') or rid.startswith('codex-')):return
        uuid_text(rid.split('-',1)[1]);expiry=row.get('expires_at')
        if type(expiry) not in (int,float) or not self.clock()<expiry<=self.clock()+3601:return
        ident={'task_id':context.task_id,'operation_id':context.operation_id,'source_id':row['source_id'],
               'native_id':row['native_id'],'request_id':rid}
        key=self._key(ident);fingerprint=json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        def fresh():
            if not context.active():return 'closed'
            status=service.status(context.operation_id,refresh=False).get('operation') or {}
            if (status.get('operation_id')!=context.operation_id or status.get('source_id')!=operation['source_id']
                or status.get('state') in ('completed','failed','interrupted')):return 'closed'
            if context.harness=='codex' and row.get('turn_id')!=status.get('backend_id'):return 'closed'
            matches=[r for r in service.requests().get('requests',[]) if r.get('request_id')==rid]
            if len(matches)!=1:return 'closed'
            return 'current' if json.dumps(matches[0],ensure_ascii=False,sort_keys=True,separators=(',',':'))==fingerprint else 'changed'
        with self.lock:
            if self.closed or not context.active():return
            old=self.by_native.get(key)
            if old:
                pending=self.pending.get(old)
                if pending and pending['fingerprint']!=fingerprint:self._retire(old,'native_request_changed')
                return
            # Persisted unknown attempts cannot be retried by making a new challenge.
            for path in (self.root/'requests').glob('*.json'):
                record=read_private(path)
                if self._key(record)==key and (record.get('state') in NO_RESEND or record.get('reason')=='native_request_changed'):
                    self.by_native[key]=record['challenge_id'];return
            if fresh()!='current' or len(self.pending)>=self.max_pending:return
            if len(list((self.root/'requests').glob('*.json')))>=4096:raise OwnerInputError('owner input history limit')
            challenge=str(uuid.uuid4());ident.update(challenge_id=challenge,broker_instance=self.instance)
            record=dict(ident,schema=SCHEMA,harness=context.harness,request_type=kind,state='pending',expires_at=expiry,requester_aid=context.requester_aid,
                        attempts={},notice_id=str(uuid.uuid4()),notice_state='send_intent')
            self._save(record)
            self.pending[challenge]={'record':record,'row':row,'fresh':fresh,'fingerprint':fingerprint,'context':context,
                'answer':lambda result:service.answer(rid,row['source_id'],result,user_authorized=True)}
            self.by_native[key]=challenge
        # Only the safe notice leaves the provider; never row/detail/answers.
        notice={'schema':SCHEMA,'notice_id':record['notice_id'],'state':'awaiting_provider_local_owner',
                'harness':context.harness,'request_type':kind,'local_only':True}
        try:self.notify(context.task_id,context.operation_id,notice)
        except Exception:
            with self.lock:record['notice_state']='delivery_unverified';self._save(record)
        else:
            with self.lock:record['notice_state']='queued_to_original_requester';self._save(record)
    def _retire(self,challenge,reason):
        pending=self.pending.pop(challenge,None)
        if not pending:return
        record=pending['record']
        if record['state'] not in NO_RESEND:
            record.update(state='retired',reason=reason);self._save(record)
            if reason!='native_request_changed':self.by_native.pop(self._key(record),None)
    def _current(self,challenge):
        pending=self.pending.get(uuid_text(challenge))
        if not pending:return None
        freshness=pending['fresh']()
        if self.closed or pending['record']['expires_at']<=self.clock() or freshness!='current':
            self._retire(challenge,'native_request_changed' if freshness=='changed' else 'native_request_closed_or_context_ended');return None
        return pending
    def list(self,task_id=None):
        with self.lock:
            result=[]
            for challenge in list(self.pending):
                current=self._current(challenge)
                if current and (task_id is None or current['record']['task_id']==task_id):result.append(copy.deepcopy(current['record']))
            return {'requests':result,'local_only':True,'remote_approval_supported':False}
    def show(self,challenge,task_id):
        with self.lock:
            pending=self._current(challenge)
            if not pending or pending['record']['task_id']!=task_id:raise OwnerInputError('no current owner request for this task')
            return dict(copy.deepcopy(pending['record']),detail=copy.deepcopy(pending['row']['detail']),
                operation_correlation_verified=pending['row']['detail'].get('operation_correlation_verified',False))
    def status(self,challenge,task_id):
        with self.lock:
            record=self._historical(challenge)
            if not record or record['task_id']!=task_id:raise OwnerInputError('owner request unavailable')
            result=copy.deepcopy(record)
            if result['broker_instance']!=self.instance and result['state'] not in NO_RESEND:
                result['state']='retired_after_restart'
            result['do_not_resubmit']=result['state'] in NO_RESEND
            return result
    def answer(self,identity,result,attempt_id,*,user_authorized=False):
        if user_authorized is not True:raise OwnerInputError('explicit local owner action required')
        uuid_text(attempt_id);result=bounded(result)
        if not isinstance(identity,dict) or set(identity)!=set(IDENTITY):raise OwnerInputError('exact owner request identity required')
        with self.lock:
            record=self._historical(identity['challenge_id'])
            if not record or any(record.get(k)!=identity.get(k) for k in IDENTITY):raise OwnerInputError('owner request identity differs')
            if attempt_id in record['attempts'] or record['state'] in NO_RESEND:
                return self.status(identity['challenge_id'],identity['task_id'])
            pending=self._current(identity['challenge_id'])
            if not pending or pending['record']!=record or record['state'] not in ('pending','answer_validation_rejected'):raise OwnerInputError('owner request is not current')
            record=pending['record']
            if len(record['attempts'])>=16:raise OwnerInputError('owner answer correction limit')
            record['attempts'][attempt_id]={'state':'answer_intent'};record.update(state='answer_intent',attempt_id=attempt_id)
            pending['record']=record;self._save(record)
        try:
            reply=pending['answer'](result)
        except Exception:
            state='answer_effect_unverified'
        else:
            from anet_harnesses.native_input import _validation_rejected
            expected=_validation_rejected(identity['request_id'],identity['source_id'])
            proof=(type(reply) is dict and set(reply)==set(expected)
                and all(type(reply[k]) is type(value) and reply[k]==value for k,value in expected.items()))
            state='answer_validation_rejected' if proof else 'answer_effect_unverified'
            for sent in ('response_recorded','response_sent'):
                shape={sent,'request_id','source_id','operation_id','native_acknowledgement_verified','terminal_observation_required'}
                if (type(reply) is dict and set(reply)==shape and reply.get(sent) is True
                    and reply.get('request_id')==identity['request_id'] and reply.get('source_id')==identity['source_id']
                    and reply.get('operation_id')==identity['operation_id'] and reply.get('native_acknowledgement_verified') is False
                    and reply.get('terminal_observation_required') is True):state=sent
        with self.lock:
            record['state']=state;record['attempts'][attempt_id]['state']=state;self._save(record)
            if state!='answer_validation_rejected':self.pending.pop(identity['challenge_id'],None)
            # No native answer value or hash is persisted or echoed.
            return dict(self.status(identity['challenge_id'],identity['task_id']),native_acknowledgement_verified=False,
                        native_terminal_observation_required=True,business_effect_verified=False)
    def close_context(self,context):
        with self.lock:
            for challenge,pending in list(self.pending.items()):
                if pending['context'] is context:self._retire(challenge,'provider_observation_context_ended')
    def close(self):
        with self.lock:
            self.closed=True
            for challenge in list(self.pending):self._retire(challenge,'provider_owner_service_closed')

class _Context:
    def __init__(self,broker,task_id,operation_id,harness,active,requester_aid):
        self.broker,self.task_id,self.operation_id,self.harness,self.active=broker,task_id,operation_id,harness,active
        self.requester_aid=requester_aid
    def __enter__(self):return self
    def __exit__(self,*_):self.broker.close_context(self)
    def observe(self,service,operation):
        if operation['operation_id']!=self.operation_id:raise OwnerInputError('provider operation differs')
        for row in service.requests().get('requests',[]):self.broker.offer(self,service,operation,row)

class OwnerInputEndpoint:
    """Local Windows pipe; answers never pass through a persistent file inbox."""
    def __init__(self,broker):self.broker=broker;self.stop=threading.Event();self.thread=None;self.record=None
    def start(self):
        if os.name!='nt':raise OwnerInputError('owner input endpoint currently requires Windows')
        from anet_harnesses import ipc_windows as w
        path=self.broker.root/'endpoint.json'
        if path.exists():
            previous=read_private(path)
            if previous.get('state') in ('running','stopping'):
                from owned_source_product.recovery import process_gone
                if process_gone(previous['broker']) is not True:raise OwnerInputError('previous owner endpoint still live or unverified')
        pipe='\\\\.\\pipe\\anet-source-owner-input-'+uuid.uuid4().hex;handle=w.make_pipe(pipe)
        self.record={'schema':SCHEMA,'instance':self.broker.instance,'pipe':pipe,'broker':w.process_identity(os.getpid()),'state':'running'}
        atomic_json(path,self.record)
        def listen():
            try:
                while not self.stop.is_set():
                    try:w.operation(handle,w.ConnectNamedPipe,time.monotonic()+1,connecting=True)
                    except TimeoutError:continue
                    try:
                        request=w.recv_json(handle,10)
                        if self.stop.is_set():raise OwnerInputError('local owner endpoint is closing')
                        if request.get('schema')!=SCHEMA or request.get('instance')!=self.record['instance']:raise OwnerInputError('owner endpoint instance differs')
                        action=request.get('action')
                        if action=='list':result=self.broker.list(request.get('task_id'))
                        elif action=='show':result=self.broker.show(request['challenge_id'],request['task_id'])
                        elif action=='status':result=self.broker.status(request['challenge_id'],request['task_id'])
                        elif action=='answer':result=self.broker.answer(request['identity'],request['result'],request['attempt_id'],user_authorized=request.get('user_authorized'))
                        else:raise OwnerInputError('operation outside finite local owner endpoint')
                        reply={'ok':True,'result':result}
                    except Exception as error:reply={'ok':False,'error':type(error).__name__}
                    try:w.send_json(handle,dict(reply,instance=self.record['instance']),10);w.recv_json(handle,3)
                    except (OSError,TimeoutError,EOFError,ValueError):pass
                    finally:w.DisconnectNamedPipe(handle)
            finally:
                w.CloseHandle(handle)
                if read_private(path).get('instance')==self.record['instance']:atomic_json(path,dict(self.record,state='closed'))
        self.thread=threading.Thread(target=listen,daemon=True,name='taskd-local-owner-input');self.thread.start()
        return self.record
    def close(self):
        # Revoke owner-answer authority BEFORE waiting for an idle/partial frame.
        self.broker.close()
        self.stop.set()
        if self.thread:self.thread.join(2)
        if self.record:
            path=self.broker.root/'endpoint.json'
            if read_private(path).get('instance')==self.record['instance']:atomic_json(path,dict(self.record,state='stopping' if self.thread and self.thread.is_alive() else 'closed'))

class OwnerInputClient:
    def __init__(self,node_root):
        node=private.plain(node_root);self.root=node/'owner-input';private.verify(self.root,root=True)
        expected={'schema':SCHEMA,'operator':private.owner_id(),'node':identity(node),'root':identity(self.root)}
        if read_private(self.root/'owner.json')!=expected:raise OwnerInputError('owner input root identity differs')
        self.endpoint=read_private(self.root/'endpoint.json')
        if self.endpoint.get('schema')!=SCHEMA or self.endpoint.get('state')!='running':raise OwnerInputError('local owner endpoint unavailable')
        uuid_text(self.endpoint['instance'])
        if not self.endpoint['pipe'].startswith('\\\\.\\pipe\\anet-source-owner-input-'):raise OwnerInputError('fixed local owner pipe required')
    def call(self,action,**payload):
        if os.name!='nt':raise OwnerInputError('local owner CLI currently requires Windows')
        from anet_harnesses import ipc_windows as w
        handle=w.connect_checked(self.endpoint,5)
        try:
            w.send_json(handle,dict(schema=SCHEMA,instance=self.endpoint['instance'],action=action,**payload),10)
            reply=w.recv_json(handle,30);w.send_json(handle,{'received':True},3)
            if reply.get('instance')!=self.endpoint['instance'] or reply.get('ok') is not True:raise OwnerInputError('local owner response unavailable')
            return reply['result']
        finally:w.CloseHandle(handle)
    def answer(self,task_id,challenge,result,attempt_id,*,user_authorized=False):
        if user_authorized is not True:raise OwnerInputError('explicit local owner action required')
        path=self.root/'client-attempts'/(uuid_text(attempt_id)+'.json')
        if path.exists():
            prior=read_private(path)
            if prior['task_id']!=task_id or prior['challenge_id']!=challenge:raise OwnerInputError('owner answer attempt identity differs')
            return self.call('status',task_id=task_id,challenge_id=challenge)
        result=bounded(result)
        current=self.call('show',task_id=task_id,challenge_id=challenge)
        ident={key:current[key] for key in IDENTITY}
        create_json(path,dict(ident,attempt_id=attempt_id,state='send_intent'))
        # An unknown write has a durable marker, and can only be inspected later.
        return self.call('answer',identity=ident,result=result,attempt_id=attempt_id,user_authorized=True)

"""Finite taskd requester bridge; no taskd imports or production mutation.

The desktop-owned harness service is the source endpoint. Windows pipe ACL and
actual server PID/creation/executable hash are checked before sending a summary.
The adapter never opens SQLite to fabricate backend ACKs, searches chat, starts
a replacement session, or replays an uncertain result-delivery request.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from .ledger import OwnershipError,EffectUncertain


def utcnow():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temporary,path)


class BridgeEndpoint:
    def __init__(self,service):
        self.service=service;self.lock=threading.RLock()
        private=service.root/'bridge'
        if os.name=='nt':
            from . import ipc_windows as win
            win.ensure_private_runtime_root(private)
        else:private.mkdir(mode=0o700,exist_ok=True)
        self.root=private
        identity=private/'identity.json'
        if identity.is_file():self.identity=json.loads(identity.read_text(encoding='utf-8'))
        else:
            self.identity={'schema':'anet-harness-endpoint/1','service_id':str(uuid.uuid4()),'state_root':str(service.root)}
            atomic_json(identity,self.identity)
        if self.identity.get('schema')!='anet-harness-endpoint/1' or self.identity.get('state_root')!=str(service.root):
            raise OwnershipError('harness endpoint identity/state root differs')
        self.registrations=private/'registrations';self.registrations.mkdir(exist_ok=True)
        self.deliveries=private/'deliveries';self.deliveries.mkdir(exist_ok=True)
        self.started=None;self.listener=None;self.stop=threading.Event()

    def validate_source(self,source_id):
        s=self.service.ledger.session(source_id)
        if s['harness']=='workbuddy' and s['transport']=='workbuddy-local-assistant/1':
            raise OwnershipError('WorkBuddy local context has no verifiable native session/terminal for automatic source callback')
        adapter=self.service._source_adapter(s)
        if s['workspace']!=str(self.service.workspace):raise OwnershipError('source working directory differs')
        live=any(not job.done() and self.service.job_sources.get(op)==source_id for op,job in list(self.service.jobs.items()))
        state='active' if live else 'idle';h=s['harness'];evidence=''
        if h=='codex':
            native=adapter.rpc.request('thread/read',{'threadId':s['native_id'],'includeTurns':False})['thread']
            if native.get('id')!=s['native_id'] or str(Path(native.get('cwd','')).resolve())!=s['workspace']:
                raise OwnershipError('native Codex source/cwd differs')
            state=native.get('status',{}).get('type')
            if state not in ('active','idle'):raise OwnershipError('native Codex source is not loaded')
            evidence='owned app-server thread/read on verified pipe-host process'
        elif h=='claude_code':
            from claude_agent_sdk import get_session_info
            native=get_session_info(s['native_id'],directory=s['workspace'])
            if not native or native.session_id!=s['native_id'] or not native.cwd or str(Path(native.cwd).resolve())!=s['workspace']:
                raise OwnershipError('exact Claude SDK native source has not been established')
            if not any(row['state']=='completed' for row in self.service.ledger.operations(source_id)):
                raise OwnershipError('complete an authorized native Claude source query before delegation binding')
            evidence='official SDK exact-session metadata + completed owned native query; service owns resumed-query endpoint'
        elif h=='deepseek_harness':
            if source_id not in adapter.sessions:self.service.restore(source_id)
            cwd=adapter.sessions[source_id].get_working_directory()
            if str(Path(cwd).resolve())!=s['workspace']:raise OwnershipError('native DeepSeek cwd differs')
            evidence='official SDK session/working-directory/get on exact owned session/home'
        elif h=='workbuddy':
            if s['native_id'].startswith('pending-'):raise OwnershipError('create the real WorkBuddy cloud source before delegation binding')
            native=adapter.status(source_id)
            if native.get('task_id')!=s['native_id']:raise OwnershipError('native cloud task differs')
            if native.get('native_status') in ('CREATING','working','planning'):state='active'
            elif native.get('native_status') in ('deleted','archived','failed'):raise OwnershipError('cloud source is unavailable')
            evidence='official WorkBuddy GET exact cloud task ID; ACP resumes that task'
        else:raise OwnershipError('unknown harness source')
        return {'session':source_id,'backend_session':s['native_id'],'harness':h,'cwd':s['workspace'],'state':state,
                'cwd_scope':'local_delegation_context' if h=='workbuddy' else 'native_workspace',
                'native_workspace_verified':h!='workbuddy','cloud_workspace_verified':False if h=='workbuddy' else None,
                'source':'anet_owned_harness_service','loaded_on_endpoint':True,'loaded_evidence':evidence,
                'host_instance_verified':self.started is not None,
                'broker_instance':self.started.get('instance') if self.started else None,
                'broker':self.started.get('broker') if self.started else None,
                'package_sha256':self.service.package_sha256,'package_schema':self.service.package_schema,
                'source_purpose':'delegation_requester','real_submission_eligible':True,
                'service_id':self.identity['service_id'],'state_root':self.identity['state_root'],'at':utcnow()}

    def register(self,source_id):
        observed=self.validate_source(source_id)
        pin={k:observed[k] for k in ('session','backend_session','harness','cwd','service_id','state_root')}
        key=fingerprint(pin)
        binding=dict(pin,protocol='anet-harness-binding/1',registration_id=key,
                     client='harness_owned_'+key[:24],source_purpose='delegation_requester',real_submission_eligible=True)
        path=self.registrations/(key+'.json')
        if path.exists() and json.loads(path.read_text(encoding='utf-8'))!=binding:raise OwnershipError('registration record conflict')
        if not path.exists():atomic_json(path,binding)
        return {'binding':binding,'startup':self.started,'binding_state':'verified','observed':observed}

    def inspect(self,binding):
        expected=self._binding(binding)
        observed=self.validate_source(expected['session'])
        if any(observed.get(k)!=expected[k] for k in ('session','backend_session','harness','cwd','service_id','state_root')):
            raise OwnershipError('native source no longer matches registration')
        return observed

    def _binding(self,binding):
        if not isinstance(binding,dict) or binding.get('protocol')!='anet-harness-binding/1':raise OwnershipError('explicit harness binding required')
        key=binding.get('registration_id','')
        if not isinstance(key,str) or len(key)!=64 or any(c not in '0123456789abcdef' for c in key):raise OwnershipError('invalid registration identity')
        path=self.registrations/(key+'.json')
        if not path.is_file() or json.loads(path.read_text(encoding='utf-8'))!=binding:
            raise OwnershipError('unregistered or changed source binding')
        return binding

    def verify_turn(self,binding,operation_id,previous_activity=None):
        binding=self._binding(binding)
        value=self.service.status(operation_id)
        operation=value.get('operation')
        if not operation:return {'state':'reconciling','do_not_resend':True,'at':utcnow()}
        if operation['source_id']!=binding['session']:raise OwnershipError('callback operation belongs to another source')
        result=operation.get('result') or {};evidence=result.get('native_evidence') or {}
        if operation['state'] in ('failed','interrupted'):
            return {'state':'failed','error':'native callback activity did not complete','operation_id':operation_id,'at':utcnow()}
        if operation['state']!='completed':return {'state':'running' if operation.get('backend_id') else 'reconciling','do_not_resend':True,'at':utcnow()}
        activity=evidence.get('activity_id');text=result.get('text','')
        kinds={'codex':'codex_turn','claude_code':'claude_result','deepseek_harness':'deepseek_inbox_interval','workbuddy':'workbuddy_acp_round'}
        terminals={'codex':('completed',),'claude_code':('completed','success'),'deepseek_harness':('completed',),'workbuddy':('end_turn',)}
        prior=previous_activity if isinstance(previous_activity,list) else [previous_activity]
        valid=(evidence.get('kind')==kinds[binding['harness']] and evidence.get('terminal_status') in terminals[binding['harness']]
               and evidence.get('session_id')==binding['backend_session'] and isinstance(activity,str) and activity
               and activity not in prior and evidence.get('final_assistant') is True and isinstance(text,str) and text.strip()
               and (evidence.get('response_correlated_to_request') is True if binding['harness']=='workbuddy'
                    else bool(evidence.get('final_message_id'))))
        if not valid:return {'state':'ambiguous','error':'native final response/new activity identity is incomplete; no delivery claim','do_not_resend':True,'at':utcnow()}
        return {'state':'completed','ack':{'turn_id':activity,'activity_identity_kind':evidence['kind'],
                   'session':binding['session'],'backend_session':binding['backend_session'],'operation_id':operation_id,'at':utcnow()},
                'result':{'turn_id':activity,'status':'completed','text':text,'message_id':evidence.get('final_message_id'),
                          'sha256':hashlib.sha256(text.encode()).hexdigest(),'native_evidence':evidence,'observed_at':utcnow()},'at':utcnow()}

    def deliver(self,binding,delivery_id,prompt):
        binding=self._binding(binding);uuid.UUID(delivery_id)
        if not isinstance(prompt,str) or not prompt.strip() or len(prompt.encode())>16384:raise ValueError('bounded nonempty 16 KiB summary required')
        path=self.deliveries/(delivery_id+'.json')
        pin={'binding':fingerprint(binding),'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest()}
        with self.lock:
            if path.exists():
                record=json.loads(path.read_text(encoding='utf-8'))
                if record.get('pin')!=pin:raise OwnershipError('delivery identity/prompt changed; original evidence retained')
                if record.get('state') in ('completed','failed','ambiguous'):return record
            else:
                observed=self.inspect(binding)
                if observed['state']!='idle':return {'state':'waiting_idle','at':utcnow()}
                previous=[]
                for row in self.service.ledger.operations(binding['session']):
                    result=json.loads(row['result']) if row.get('result') else {}
                    if isinstance(result,dict) and result.get('native_evidence',{}).get('activity_id'):
                        previous.append(result['native_evidence']['activity_id'])
                operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL,binding['registration_id']+':'+delivery_id))
                record={'state':'requested','pin':pin,'operation_id':operation_id,'previous_activity':previous,
                        'request':{'id':delivery_id,'operation_id':operation_id,'session':binding['session'],
                            'backend_session':binding['backend_session'],'cwd':binding['cwd'],'prompt_sha256':pin['prompt_sha256'],'at':utcnow()},'at':utcnow()}
                atomic_json(path,record)
                try:self.service.submit(binding['session'],operation_id,prompt)
                except BaseException as exc:
                    record.update(state='reconciling',error=type(exc).__name__,do_not_resend=True);atomic_json(path,record)
                    return record
            verified=self.verify_turn(binding,record['operation_id'],record.get('previous_activity'))
            record.update(verified)
            if record.get('ack'):record['ack']['request_id']=record['request']['id']
            atomic_json(path,record)
            return record

    def start(self):
        if os.name!='nt':return None
        from . import ipc_windows as win
        pipe='\\\\.\\pipe\\anet-source-harness-'+uuid.uuid4().hex
        handle=win.make_pipe(pipe)
        self.started={'protocol':'anet-harness-endpoint/1','instance':str(uuid.uuid4()),'codex':self.identity['service_id'],
                      'package_sha256':self.service.package_sha256,'package_schema':self.service.package_schema,
                      'service_id':self.identity['service_id'],'state_root':self.identity['state_root'],
                      'pipe':pipe,'broker':win.process_identity(os.getpid()),'at':utcnow()}
        atomic_json(self.root/'startup.json',self.started)
        def listen():
            try:
                while not self.stop.is_set():
                    try:win.operation(handle,win.ConnectNamedPipe,time.monotonic()+1,connecting=True)
                    except TimeoutError:continue
                    try:
                        request=win.recv_json(handle,10)
                        if request.get('protocol')!='anet-source/1' or request.get('instance')!=self.started['instance']:
                            raise OwnershipError('endpoint instance differs')
                        op=request.get('op');binding=request.get('binding')
                        if op=='inspect_source':result=self.inspect(binding)
                        elif op=='deliver':result=self.deliver(binding,request.get('delivery_id'),request.get('prompt'))
                        elif op=='verify_turn':result=self.verify_turn(binding,request.get('operation_id'),request.get('previous_activity'))
                        else:raise OwnershipError('operation outside finite requester bridge')
                        reply={'ok':True,'result':result}
                    except BaseException as exc:reply={'ok':False,'error':type(exc).__name__}
                    try:
                        win.send_json(handle,dict(reply,instance=self.started['instance'],codex=self.started['codex']),10)
                        win.recv_json(handle,3)
                    except (OSError,TimeoutError,EOFError,ValueError):pass
                    finally:win.DisconnectNamedPipe(handle)
            finally:win.CloseHandle(handle)
        self.listener=threading.Thread(target=listen,daemon=True,name='anet-harness-taskd-pipe');self.listener.start()
        return self.started

    def close(self):
        self.stop.set()
        if self.listener:self.listener.join(timeout=2)


class TaskdHarnessRequester:
    """Drop-in taskd Relay requester; instantiate from explicit local registration."""
    def __init__(self,startup,binding,call=None):
        self.startup=dict(startup);self.binding=dict(binding);self.name=binding['client']
        if startup.get('service_id')!=binding.get('service_id') or startup.get('state_root')!=binding.get('state_root'):
            raise OwnershipError('startup and source registration differ')
        self._call=call
    def connect(self,action,**payload):
        if action not in ('inspect_source','deliver','verify_turn'):raise OwnershipError('operation outside requester bridge')
        if self._call:return self._call(action,binding=self.binding,**payload)
        from .ipc_windows import request
        return request(self.startup,action,binding=self.binding,**payload)
    def current_session(self):return self.binding['session']
    def recover(self,startup):
        """Explicitly repin a restarted host only after it proves the same source."""
        if startup.get('service_id')!=self.binding['service_id'] or startup.get('state_root')!=self.binding['state_root']:
            raise OwnershipError('replacement endpoint belongs to another persistent service')
        old=self.startup;self.startup=dict(startup)
        try:self.inspect(self.binding['session'])
        except BaseException:
            self.startup=old;raise
        return {'state':'verified','source':'explicit_harness_endpoint_recovery','at':utcnow()}
    def find_session(self,task_id,since_ts):return None
    def inspect(self,session_id):
        if session_id!=self.binding['session']:raise OwnershipError('source mismatch; zero IPC requests')
        return self.connect('inspect_source')
    def cwd(self,session_id):return self.inspect(session_id)['cwd']
    def idle(self,session_id):return self.inspect(session_id)['state']=='idle'
    def readiness(self):
        try:
            source=self.inspect(self.binding['session'])
            return {'ready':source.get('loaded_on_endpoint') is True,'source':'owned_harness_service','at':utcnow()}
        except Exception as exc:return {'ready':False,'error':type(exc).__name__,'at':utcnow()}
    def loaded_sources(self):
        try:
            source=self.inspect(self.binding['session']);return [{'session':source['session'],'cwd':source['cwd']}]
        except Exception:return []
    def delivery(self,session_id,prompt,cwd,journal):
        if session_id!=self.binding['session'] or str(Path(cwd).resolve())!=self.binding['cwd']:
            return {'state':'binding_error','error':'source/cwd differs; zero IPC requests','at':utcnow()}
        path=Path(journal);pin={'registration':fingerprint(self.binding),'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest()}
        if path.exists():
            record=json.loads(path.read_text(encoding='utf-8'))
            if record.get('_harness_bridge',{}).get('pin')!=pin:
                return {'state':'binding_error','error':'callback journal identity differs; no resend','at':utcnow()}
            if record.get('state') in ('completed','failed','ambiguous'):return record
        else:
            record={'state':'requested','_harness_bridge':{'pin':pin,'delivery_id':str(uuid.uuid4())},'at':utcnow()}
            atomic_json(path,record)
        identity=record['_harness_bridge']
        try:record=dict(self.connect('deliver',delivery_id=identity['delivery_id'],prompt=prompt),_harness_bridge=identity)
        except Exception as exc:record.update(state='reconciling',error=type(exc).__name__,do_not_resend=True,at=utcnow())
        atomic_json(path,record);return record

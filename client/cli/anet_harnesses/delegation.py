"""Desktop client for taskd's private binding inbox, with durable request identity.

Prepare is local and sends nothing. Submit queues one exact command once. Status
only verifies the daemon's matching receipt and reads its pinned tracked state.
"""
import hashlib,json,os,re,stat
from pathlib import Path
import threading,time,uuid
from .taskd_bridge import atomic_json,utcnow
from .ledger import OwnershipError,EffectUncertain,ConflictingOperation

PROTOCOL='taskd-binding/1'
REGISTRY_SCHEMA='anet-taskd-harness-sources/1'
# 32 sources * (current + 32 lineage) * (16 KiB normalized source + small
# record framing) is below 18 MiB. Do not reuse the 256 KiB inbox limit.
REGISTRY_LIMIT=24*1024*1024
HASH=re.compile('[0-9a-f]{64}')

def record_digest(value):
    """Exact taskd records.canonical encoding; inbox wire_digest is different."""
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()

def _hold(message):raise OwnershipError('registration predecessor proof unavailable: '+message)

def _plain(path):
    path=Path(os.path.abspath(path))
    for item in (path,*path.parents):
        if item.is_symlink() or item.exists() and getattr(item.lstat(),'st_file_attributes',0)&0x400:_hold('reparse metadata path')
    return path

def _private(path,root=False):
    """Verify existing metadata only; never create, repair or adopt a root."""
    path=_plain(path)
    if not path.exists() or root and not path.is_dir() or not root and not path.is_file():_hold('private metadata missing')
    if os.name!='nt':
        info=path.stat()
        if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077:_hold('private metadata owner or mode')
        return str(os.geteuid())
    from . import ipc_windows as win
    if root:
        win.verify_private_runtime_root(path)
        return win.owner_sid()
    class ACL_INFO(win.C.Structure):_fields_=[('count',win.W.DWORD),('used',win.W.DWORD),('free',win.W.DWORD)]
    def sid_text(pointer):
        value=win.W.LPWSTR();win.check(win.ConvertSidToStringSidW(pointer,win.C.byref(value)))
        try:return value.value
        finally:win.LocalFree(win.C.cast(value,win.C.c_void_p))
    owner=win.C.c_void_p();acl=win.C.c_void_p();sd=win.C.c_void_p()
    error=win.GetNamedSecurityInfoW(win.filesystem_path(path),1,5,win.C.byref(owner),None,win.C.byref(acl),None,win.C.byref(sd))
    if error:_hold('private metadata security unavailable')
    try:
        sid=win.owner_sid()
        if not owner or not acl:_hold('private metadata security missing')
        control=win.W.WORD();revision=win.W.DWORD();win.check(win.GetSecurityDescriptorControl(sd,win.C.byref(control),win.C.byref(revision)))
        if root and (sid_text(owner)!=sid or not control.value&0x1000):_hold('private root owner or protection')
        if not root and sid_text(owner) not in (sid,'S-1-5-18','S-1-5-32-544'):_hold('private metadata owner')
        info=ACL_INFO();win.check(win.GetAclInformation(acl,win.C.byref(info),win.C.sizeof(info),2));principals=[]
        for i in range(info.count):
            ace=win.C.c_void_p();win.check(win.GetAce(acl,i,win.C.byref(ace)));header=win.C.string_at(ace,8)
            if header[0]!=0 or header[1]&~0x13 or root and header[1]&3!=3 or int.from_bytes(header[4:8],'little')!=0x1f01ff:_hold('private metadata rights')
            principals.append(sid_text(ace.value+8))
        if len(principals)!=2 or set(principals)!={sid,'S-1-5-18'}:_hold('private metadata principals')
        return sid
    finally:win.LocalFree(sd)

def _file_id(path):
    info=path.stat();return [info.st_dev,info.st_ino]

def _receipt_acl_snapshot(path):
    """Existing Windows ACL facts for this fixed receipt role, never an ACL edit."""
    from . import ipc_windows as win
    path=_plain(path);info=path.stat()
    class ACL_INFO(win.C.Structure):_fields_=[('count',win.W.DWORD),('used',win.W.DWORD),('free',win.W.DWORD)]
    def text(pointer):
        value=win.W.LPWSTR();win.check(win.ConvertSidToStringSidW(pointer,win.C.byref(value)))
        try:return value.value
        finally:win.LocalFree(win.C.cast(value,win.C.c_void_p))
    owner=win.C.c_void_p();acl=win.C.c_void_p();sd=win.C.c_void_p()
    error=win.GetNamedSecurityInfoW(win.filesystem_path(path),1,5,win.C.byref(owner),None,win.C.byref(acl),None,win.C.byref(sd))
    if error:_hold('receipt security unavailable')
    try:
        if not owner or not acl:_hold('receipt security missing')
        control=win.W.WORD();revision=win.W.DWORD();win.check(win.GetSecurityDescriptorControl(sd,win.C.byref(control),win.C.byref(revision)))
        count=ACL_INFO();win.check(win.GetAclInformation(acl,win.C.byref(count),win.C.sizeof(count),2));entries=[]
        for i in range(count.count):
            ace=win.C.c_void_p();win.check(win.GetAce(acl,i,win.C.byref(ace)));head=win.C.string_at(ace,8)
            if head[0]!=0:_hold('receipt non-allow ACE')
            entries.append((text(ace.value+8),int.from_bytes(head[4:8],'little'),head[1]))
        return {'owner':text(owner),'control':control.value,'entries':entries,
                'identity':[info.st_dev,info.st_ino],'directory':path.is_dir(),'nlink':info.st_nlink,
                'attributes':getattr(info,'st_file_attributes',0),'file_mtime_ns':None if path.is_dir() else info.st_mtime_ns}
    finally:win.LocalFree(sd)

def _receipt_permissions(node_root,path):
    """Fixed queue receipt role: strict private, or the observed legacy node ACL.

    Registry, state and local intent/journal readers keep _private unchanged.
    Admin is accepted only as one member of this complete protected-node queue
    layout, never as a global metadata principal or independent business proof.
    """
    node=_plain(node_root);path=_plain(path)
    if (node.name!='taskd' or not node.is_dir() or path.parent!=node/'requester-commands/receipts'
        or path.suffix!='.json' or not path.is_file()):_hold('fixed receipt queue required')
    try:
        if str(uuid.UUID(path.stem))!=path.stem:_hold('canonical receipt request UUID required')
    except (ValueError,TypeError,AttributeError):_hold('canonical receipt request UUID required')
    try:
        _private(path)
        if os.name!='nt':
            info=path.stat();return ('strict_posix',info.st_uid,info.st_mode,info.st_dev,info.st_ino,info.st_mtime_ns)
        proof=_receipt_acl_snapshot(path);_private(path)
        if proof!=_receipt_acl_snapshot(path):_hold('receipt ACL changed during validation')
        return ('strict_private',proof)
    except OwnershipError:
        if os.name!='nt':raise
    from . import ipc_windows as win
    sid=win.owner_sid();allowed={sid,'S-1-5-18','S-1-5-32-544'}
    chain=(node.parent,node,node/'requester-commands',node/'requester-commands/receipts',path);proof=[]
    for index,member in enumerate(chain):
        facts=_receipt_acl_snapshot(member)
        directory=index<len(chain)-1
        if facts['directory']!=directory:_hold('legacy receipt path kind')
        if index==0 and not facts['control']&0x1000:_hold('legacy receipt protected node anchor required')
        if facts['owner'] not in allowed:_hold('legacy receipt owner')
        entries=facts['entries'];flags=3 if index==0 else 19 if directory else 16
        if (len(entries)!=3 or {e[0] for e in entries}!=allowed
            or any(mask!=0x1f01ff or ace_flags!=flags for _,mask,ace_flags in entries)):_hold('legacy receipt exact inherited node ACL required')
        if not directory and facts['nlink']!=1:_hold('legacy receipt ordinary single-link file required')
        proof.append(facts)
    return ('legacy_protected_node_queue',proof)

def _private_bytes(path,limit,receipt_role=None):
    path=_plain(path)
    proof=_receipt_permissions(receipt_role,path) if receipt_role is not None else _private(path)
    with path.open('rb') as stream:
        before=os.fstat(stream.fileno())
        if before.st_nlink!=1 or before.st_size>limit:_hold('bounded ordinary metadata required')
        raw=stream.read(limit+1);after=os.fstat(stream.fileno())
    current=path.stat()
    token=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_nlink)
    if len(raw)>limit or token(before)!=token(after) or token(before)!=token(current):_hold('metadata changed during read')
    if receipt_role is not None:
        if proof!=_receipt_permissions(receipt_role,path):_hold('receipt ACL or queue identity changed during read')
        # Permission/queue proof itself performs filesystem reads. A same-tick
        # append during that final proof can leave mtime unchanged on Windows.
        # Recheck the complete ordinary-file identity/size token AFTER it.
        if token(before)!=token(path.stat()):_hold('receipt metadata changed during final permission proof')
    return raw

def _private_json(path,limit=262144,receipt_role=None):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:_hold('duplicate metadata key')
            result[key]=value
        return result
    value=json.loads(_private_bytes(path,limit,receipt_role=receipt_role),object_pairs_hook=pairs)
    if not isinstance(value,dict):_hold('metadata object required')
    return value
def wire_digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def read_json(path):
    p=Path(path)
    if not p.is_file():return None
    if p.is_symlink() or p.stat().st_size>262144:raise OwnershipError('requester evidence file is invalid')
    return json.loads(p.read_text(encoding='utf-8'))


class Delegations:
    def __init__(self,service,node_root):
        self.service=service;self.node_root=Path(node_root).resolve()
        if not Path(node_root).is_absolute():raise ValueError('explicit absolute taskd node root required')
        self.root=service.root/'delegations';self.root.mkdir(exist_ok=True)
        self.lock=threading.RLock()
    def _path(self,operation_id):
        canonical=str(uuid.UUID(operation_id))
        if canonical!=operation_id:raise ValueError('delegation operation must be a canonical UUID')
        return self.root/(canonical+'.json')
    def _ready(self):
        status=read_json(self.node_root/'status.json') or {}
        if status.get('requester_binding')!=PROTOCOL or status.get('requester') is not True or not 0<=time.time()-status.get('ts',0)<=90:
            raise OwnershipError('taskd requester binding service is unavailable; nothing was queued')
        for directory in ('requester-commands/inbox','requester-commands/receipts'):
            path=self.node_root/directory
            if not path.is_dir() or path.is_symlink():raise OwnershipError('taskd private requester directory unavailable')
    def _receipt(self,record):
        request=record.get('request')
        if not request:return None
        receipt=read_json(self.node_root/'requester-commands/receipts'/(request['request_id']+'.json'))
        if receipt and (receipt.get('protocol')!=PROTOCOL or receipt.get('request_id')!=request['request_id'] or receipt.get('request_sha256')!=wire_digest(request)):
            raise OwnershipError('daemon receipt does not match this exact prepared request')
        return receipt
    def _queue(self,record,path,operation,payload):
        if record.get('request'):
            # An earlier write may have reached the daemon even if its ACK was
            # lost. Never recreate its inbox file or refresh its expiry.
            return self.status(record['operation_id'])
        if (record.get('node_root')!=str(self.node_root) or path!=self._path(record.get('operation_id'))):_hold('prepared request operation or node differs')
        try:
            if str(uuid.UUID(record.get('request_id')))!=record.get('request_id'):_hold('prepared request identity')
        except (ValueError,TypeError,AttributeError):_hold('prepared request identity')
        self._ready()
        request={'protocol':PROTOCOL,'request_id':record['request_id'],'operation':operation,'payload':payload,
                 'created_at':utcnow(),'expires_at':time.time()+90}
        if len(json.dumps(request,ensure_ascii=False).encode())>262144:raise ValueError('requester command too large')
        record.update(request=request,state='queue_intent',do_not_resubmit=True,at=utcnow())
        atomic_json(path,record) # even an uncertain inbox write can never be repeated
        try:atomic_json(self.node_root/'requester-commands/inbox'/(request['request_id']+'.json'),request)
        except OSError:
            record.update(state='queue_unconfirmed',at=utcnow());atomic_json(path,record);raise
        record.update(state='queued',at=utcnow());atomic_json(path,record)
        return self.status(record['operation_id'])
    def _registry(self):
        root=_plain(self.node_root/'harness-sources')
        if not root.exists():return None
        operator=_private(root,root=True)
        owner=_private_json(root/'owner.json');lock=root/'registry.lock'
        if (owner.get('schema')!=REGISTRY_SCHEMA or owner.get('operator')!=operator
            or owner.get('root')!=_file_id(root) or owner.get('lock')!=_file_id(lock)
            or _private_bytes(lock,1)!=b'0'):_hold('registry owner or filesystem identity')
        raw=_private_bytes(root/'sources.json',REGISTRY_LIMIT)
        def pairs(items):
            result={}
            for key,value in items:
                if key in result:_hold('duplicate registry key')
                result[key]=value
            return result
        table=json.loads(raw,object_pairs_hook=pairs)
        if (not isinstance(table,dict) or set(table)!={'schema','sources'} or table['schema']!=REGISTRY_SCHEMA
            or not isinstance(table['sources'],dict) or len(table['sources'])>32):_hold('registry schema')
        if owner.get('root')!=_file_id(root) or owner.get('lock')!=_file_id(lock):_hold('registry filesystem changed')
        return table,operator,hashlib.sha256(raw).hexdigest()
    def _record(self,key,value,binding,operator):
        fields={'source_key','client','startup','binding','state_root_identity'}
        if (not isinstance(value,dict) or set(value)!=fields or value['source_key']!=key
            or value['client']!=binding['client'] or value['binding']!=binding
            or value['state_root_identity']!=_file_id(self.service.root)):_hold('record source binding or state-root identity')
        startup=value['startup'];base={'protocol','instance','codex','service_id','state_root','pipe','broker','at'}
        if not isinstance(startup,dict) or set(startup) not in (base,base|{'package_schema','package_sha256'}):_hold('record startup schema')
        for name in ('instance','service_id'):
            try:
                if str(uuid.UUID(startup[name]))!=startup[name]:_hold('record startup UUID')
            except (ValueError,TypeError,KeyError):_hold('record startup UUID')
        if (startup['protocol']!='anet-harness-endpoint/1' or startup['codex']!=startup['service_id']
            or startup['service_id']!=binding['service_id'] or startup['state_root']!=binding['state_root']
            or not isinstance(startup['pipe'],str) or not re.fullmatch(r'\\\\\.\\pipe\\anet-source-harness-[0-9a-f]{32}',startup['pipe'])):_hold('record endpoint mismatch')
        if 'package_schema' in startup and (startup['package_schema']!='anet-harness-release-closure/2' or not isinstance(startup['package_sha256'],str) or not HASH.fullmatch(startup['package_sha256'])):_hold('record package schema')
        process=startup['broker']
        if (not isinstance(process,dict) or set(process)!={'pid','creation_filetime','exe','exe_sha256','owner_sid'}
            or type(process['pid']) is not int or process['pid']<=0 or type(process['creation_filetime']) is not int or process['creation_filetime']<=0
            or process['owner_sid']!=operator or not isinstance(process['exe'],str) or not Path(process['exe']).is_absolute()
            or not isinstance(process['exe_sha256'],str) or not HASH.fullmatch(process['exe_sha256'])):_hold('record process identity')
        if len(json.dumps({'startup':startup,'binding':binding},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())>16384:_hold('record size')
        return record_digest(value)
    def _predecessor(self,registered,source_id,operation_id):
        binding=registered['binding'];key=binding['registration_id']
        histories=[]
        for path in self.root.glob('*.json'):
            if path==self._path(operation_id):continue
            prior=read_json(path)
            if prior and prior.get('kind')=='register' and prior.get('source_id')==source_id:histories.append((path,prior))
        snapshot=self._registry()
        recorded=[prior for _,prior in histories if prior.get('request')]
        if snapshot is None:
            if recorded:_hold('past registration exists but registry is missing')
            return 'register_harness_source',None,{'kind':'new_source_no_prior_registration'}
        table,operator,registry_sha=snapshot;entry=table['sources'].get(key)
        for other,other_entry in table['sources'].items():
            if not isinstance(other_entry,dict) or not isinstance(other_entry.get('record'),dict):_hold('invalid registry entry')
            other_binding=(other_entry.get('record') or {}).get('binding') or {}
            if other!=key and (other_binding.get('session')==source_id or other_binding.get('client')==binding['client']):_hold('ambiguous registry source')
        if entry is None:
            if recorded:_hold('past source is absent from registry')
            return 'register_harness_source',None,{'kind':'new_source_no_prior_registration'}
        expected_fields={'protocol','registration_id','session','backend_session','harness','cwd','service_id','state_root','client','source_purpose','real_submission_eligible'}
        if (set(binding)!=expected_fields or binding['protocol']!='anet-harness-binding/1' or binding['session']!=source_id
            or binding['client']!='harness_owned_'+key[:24] or binding['source_purpose']!='delegation_requester'
            or binding['real_submission_eligible'] is not True or binding['harness'] not in ('codex','claude_code','workbuddy','deepseek_harness')):_hold('local source binding')
        try:
            if str(uuid.UUID(binding['session']))!=binding['session'] or str(uuid.UUID(binding['service_id']))!=binding['service_id']:_hold('local source UUID')
        except (ValueError,TypeError,AttributeError):_hold('local source UUID')
        if (not isinstance(binding['backend_session'],str) or not 1<=len(binding['backend_session'])<=200
            or any(ord(c)<32 for c in binding['backend_session'])):_hold('local native session')
        pin={name:binding[name] for name in ('session','backend_session','harness','cwd','service_id','state_root')}
        if not isinstance(key,str) or not HASH.fullmatch(key) or record_digest(pin)!=key:_hold('source key digest')
        if str(_plain(binding['state_root']))!=binding['state_root'] or Path(binding['state_root'])!=self.service.root.resolve():_hold('local state-root path')
        if not Path(binding['cwd']).is_absolute() or not Path(binding['cwd']).is_dir() or str(_plain(binding['cwd']))!=binding['cwd']:_hold('local cwd')
        _private(self.service.root,root=True)
        if (not isinstance(entry,dict) or entry.get('active') is not True
            or any(not isinstance(entry.get(name),str) or not entry[name] for name in ('registered_at','last_verified_at'))
            or not isinstance(entry.get('lineage',[]),list) or len(entry.get('lineage',[]))>32):_hold('inactive or invalid current registry entry')
        current=entry['record'];current_sha=self._record(key,current,binding,operator)
        if entry.get('record_sha256')!=current_sha:_hold('current record digest')
        records={current_sha:current};observed={current_sha};parents={};verified=set();intents={};uncertain=[]
        previous=None
        for old in entry.get('lineage',[]):
            digest=self._record(key,old,binding,operator)
            if digest in observed:_hold('duplicate registry lineage record')
            records[digest]=old;observed.add(digest)
            if previous is not None:parents.setdefault(digest,set()).add(previous)
            previous=digest
        if previous is not None:
            if entry.get('recovery_origin_record_sha256')!=previous:_hold('current recovery origin differs from lineage')
            parents.setdefault(current_sha,set()).add(previous)
        elif entry.get('recovery_origin_record_sha256') is not None:_hold('recovery origin without lineage')
        for path,prior in histories:
            if _private_json(path)!=prior:_hold('local registration changed during read')
            if prior.get('node_root')!=str(self.node_root) or prior.get('operation_id')!=path.stem or prior.get('registration',{}).get('binding')!=binding:_hold('local registration binding conflict')
            if not prior.get('request'):continue
            request=prior['request'];rid=request.get('request_id')
            try:
                if str(uuid.UUID(rid))!=rid or prior.get('request_id')!=rid:_hold('request identity')
            except (ValueError,TypeError,AttributeError):_hold('request identity')
            action=request.get('operation');payload=request.get('payload')
            required={'source_key':key,'startup':prior['registration']['startup'],'binding':binding}
            if action=='recover_harness_source':
                parent=(payload or {}).get('expected_record_sha256')
                if not isinstance(parent,str) or not HASH.fullmatch(parent):_hold('recorded recovery predecessor')
                required['expected_record_sha256']=parent
            elif action!='register_harness_source':_hold('registration request operation')
            if request.get('protocol')!=PROTOCOL or payload!=required or prior.get('registration_action',action)!=action or prior.get('registration_payload',payload)!=payload:_hold('recorded registration request differs')
            record={'source_key':key,'client':binding['client'],'startup':prior['registration']['startup'],'binding':binding,'state_root_identity':current['state_root_identity']}
            target=self._record(key,record,binding,operator);records.setdefault(target,record)
            receipt_path=self.node_root/'requester-commands/receipts'/(rid+'.json')
            receipt=_private_json(receipt_path,receipt_role=self.node_root) if receipt_path.exists() else None
            if receipt and (receipt.get('protocol')!=PROTOCOL or receipt.get('request_id')!=rid or receipt.get('request_sha256')!=wire_digest(request)):_hold('registration receipt request mismatch')
            if receipt and receipt.get('state')=='verified':
                result=receipt.get('result') or {};proof=result.get('binding') or {}
                if (result.get('client')!=binding['client'] or result.get('session')!=binding['session'] or result.get('cwd')!=binding['cwd']
                    or result.get('source_key')!=key or result.get('source_purpose')!='delegation_requester' or result.get('real_submission_eligible') is not True
                    or proof.get('state')!='verified' or proof.get('source')!='harness_source_registry'
                    or proof.get('instance')!=record['startup']['instance'] or proof.get('record_sha256')!=target):_hold('verified receipt source or exact startup conflict')
                verified.add(target)
            else:uncertain.append((target,action,required.get('expected_record_sha256')))
            if action=='recover_harness_source':
                parents.setdefault(target,set()).add(required['expected_record_sha256'])
                intents.setdefault(target,set()).add(required['expected_record_sha256'])
        if any(len(values)>1 for values in parents.values()):_hold('ambiguous recovery predecessors')
        def prove(target,seen):
            if target in verified:return target
            if target in seen or target not in observed or not intents.get(target):return None
            seen=seen|{target};choices=parents.get(target,set())
            if len(choices)!=1:return None
            parent=next(iter(choices))
            if parent not in intents[target] or parent not in records:return None
            return prove(parent,seen)
        ancestor=prove(current_sha,set())
        if ancestor is None:_hold('no verified local ancestor for current record')
        ancestors={current_sha};cursor=current_sha
        while len(parents.get(cursor,set()))==1:
            cursor=next(iter(parents[cursor]))
            if cursor in ancestors:_hold('cyclic recovery evidence')
            ancestors.add(cursor)
        for target,action,parent in uncertain:
            if target in ancestors:continue
            # An earlier CAS against an already-retired proven ancestor cannot
            # move the current record. Keep that old operation unknown, never
            # resend it; the daemon still performs the new exact CAS itself.
            if action=='recover_harness_source' and parent in ancestors and parent!=current_sha:continue
            _hold('unresolved registration may still change current source')
        proof={'kind':'local_verified_registration' if current_sha in verified else 'owner_registry_current_record',
               'record_sha256':current_sha,'verified_ancestor_record_sha256':ancestor,'registry_sha256':registry_sha}
        return ('register_harness_source' if current['startup']==registered['startup'] else 'recover_harness_source'),current_sha,proof
    def register(self,source_id,operation_id):
        path=self._path(operation_id)
        with self.lock:
            if path.is_file():
                record=read_json(path)
                if record.get('kind')!='register' or record.get('source_id')!=source_id:raise ConflictingOperation('registration operation identity differs')
                if not record.get('request'):
                    if _private_json(path)!=record or record.get('node_root')!=str(self.node_root) or record.get('operation_id')!=operation_id:_hold('prepared registration identity or node differs')
                    try:
                        if str(uuid.UUID(record.get('request_id')))!=record.get('request_id'):_hold('prepared request identity')
                    except (ValueError,TypeError,AttributeError):_hold('prepared request identity')
                    registered=record['registration']
                    if self.service.bridge is None or self.service.bridge.started!=registered['startup']:_hold('prepared registration host changed; retain original operation')
                    self.service.bridge.inspect(registered['binding'])
                    rid=record.get('request_id')
                    if any((self.node_root/'requester-commands'/group/(str(rid)+'.json')).exists() for group in ('inbox','receipts')):_hold('unsent record has existing request evidence')
                    action,expected,proof=self._predecessor(registered,source_id,operation_id)
                    payload={'source_key':registered['binding']['registration_id'],'startup':registered['startup'],'binding':registered['binding']}
                    if action=='recover_harness_source':payload['expected_record_sha256']=expected
                    record.update(registration_action=action,registration_payload=payload,predecessor_proof=proof);atomic_json(path,record)
                    return self._queue(record,path,action,payload)
                return self.status(operation_id)
            if self.service.bridge is None:
                from .taskd_bridge import BridgeEndpoint
                self.service.bridge=BridgeEndpoint(self.service);self.service.bridge.start()
            registered=self.service.bridge.register(source_id)
            if not registered['startup']:raise OwnershipError('OS-verified harness requester endpoint is unavailable')
            operation,expected,proof=self._predecessor(registered,source_id,operation_id)
            payload={'source_key':registered['binding']['registration_id'],'startup':registered['startup'],'binding':registered['binding']}
            if operation=='recover_harness_source':payload['expected_record_sha256']=expected
            record={'operation_id':operation_id,'kind':'register','source_id':source_id,'request_id':str(uuid.uuid4()),
                    'registration':registered,'state':'prepared','node_root':str(self.node_root),'at':utcnow(),
                    'registration_action':operation,'registration_payload':payload,'predecessor_proof':proof}
            atomic_json(path,record)
            return self._queue(record,path,operation,payload)
    def _registration(self,source_id):
        for path in self.root.glob('*.json'):
            record=read_json(path)
            if record.get('kind')!='register' or record.get('source_id')!=source_id:continue
            receipt=self._receipt(record)
            if receipt and receipt.get('state')=='verified':
                registration=record['registration'];binding=registration['binding']
                result=receipt.get('result',{})
                if (result.get('client')!=binding['client'] or result.get('session')!=binding['session'] or result.get('cwd')!=binding['cwd']
                        or result.get('binding',{}).get('state')!='verified' or result.get('real_submission_eligible') is not True):
                    raise OwnershipError('registered requester source/cwd/client differs')
                if self.service.bridge is None:raise OwnershipError('registered source endpoint must be reopened explicitly')
                if registration['startup'].get('instance')!=(self.service.bridge.started or {}).get('instance'):continue
                self.service.bridge.inspect(binding)
                return registration
        raise OwnershipError('register this harness source with taskd and verify its receipt before preparing a delegation')
    def prepare(self,source_id,operation_id,peer,spec):
        path=self._path(operation_id)
        if not isinstance(peer,str) or not peer.strip() or len(peer)>512:raise ValueError('explicit peer AID required')
        if not isinstance(spec,dict) or spec.get('taskd') not in ('submit','run') or not isinstance(spec.get('goal'),str) or not spec['goal'].strip():
            raise ValueError('explicit taskd submit/run goal specification required')
        if not isinstance(spec.get('acceptance'),list) or not spec['acceptance']:raise ValueError('explicit task acceptance required')
        digest=wire_digest({'source_id':source_id,'peer':peer,'spec':spec})
        with self.lock:
            if path.is_file():
                record=read_json(path)
                if record.get('input_sha256')!=digest:raise ConflictingOperation('delegation operation id input conflict')
                return self.status(operation_id)
            registration=self._registration(source_id);binding=registration['binding']
            source={'session':binding['session'],'cwd':binding['cwd'],'client':binding['client']}
            record={'operation_id':operation_id,'kind':'delegation','source_id':source_id,'request_id':str(uuid.uuid4()),
                    'node_root':str(self.node_root),'source':source,'peer':peer,'spec':spec,
                    'registration_id':binding['registration_id'],'input_sha256':digest,'state':'prepared_not_sent','at':utcnow()}
            atomic_json(path,record);return self.status(operation_id)
    def submit(self,operation_id):
        path=self._path(operation_id)
        with self.lock:
            record=read_json(path)
            if not record or record.get('kind')!='delegation':raise OwnershipError('prepare this exact delegation before submitting')
            if record.get('request'):return self.status(operation_id)
            self._registration(record['source_id'])
            return self._queue(record,path,'submit_task',{'source':record['source'],'peer':record['peer'],'spec':record['spec'],'no_callback':False})
    def status(self,operation_id):
        record=read_json(self._path(operation_id))
        if not record:return {'operation_id':operation_id,'state':'not_found','sent':False}
        if record.get('node_root')!=str(self.node_root):raise OwnershipError('delegation belongs to another node root')
        value={k:record.get(k) for k in ('operation_id','kind','source_id','request_id','state','peer','source','spec','at')}
        value.update(sent=False if not record.get('request') else None,do_not_resubmit=bool(record.get('request')),
                     auto_return=False,binding={'state':'not_verified'})
        receipt=self._receipt(record)
        if not receipt:return value
        value['receipt']=receipt
        result=receipt.get('result') or {}
        if record['kind']=='register':
            binding=record['registration']['binding']
            verified=(receipt.get('state')=='verified' and result.get('client')==binding['client']
                      and result.get('session')==binding['session'] and result.get('cwd')==binding['cwd']
                      and result.get('binding',{}).get('state')=='verified' and result.get('real_submission_eligible') is True)
            value.update(state='registered' if verified else 'registration_failed',binding={'state':'verified' if verified else 'not_verified'},registration=record['registration'])
            return value
        source=record['source'];task_id=result.get('task_id')
        valid=(receipt.get('state')=='verified' and result.get('session')==source['session'] and result.get('cwd')==source['cwd']
               and result.get('binding',{}).get('state')=='verified' and result.get('sent') is True and result.get('tracked') is True
               and result.get('auto_return') is True and isinstance(task_id,str) and task_id and all(c.isalnum() or c in '_-' for c in task_id))
        value.update(state='submitted' if valid else 'submission_unverified',task_id=task_id,
                     sent=result.get('sent'),binding=result.get('binding') or {'state':'not_verified'},auto_return=valid)
        if valid:
            tracked=read_json(self.node_root/'state/tracked'/(task_id+'.json'))
            if tracked:
                if any(tracked.get(k)!=expected for k,expected in [('task_id',task_id),('client',source['client']),('session',source['session']),('cwd',source['cwd']),('peer',record['peer'])]):
                    raise OwnershipError('tracked remote task/source does not match verified submission')
                value.update(remote_state=tracked.get('state'),stages=tracked.get('stages'),last_delivery=tracked.get('last_delivery'))
        return value

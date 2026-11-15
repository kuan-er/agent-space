"""Exact owned-source registration for a single existing taskd Relay.

This registry cannot submit an ANet task, spawn a host, adopt/resume a thread,
change node identities/policies, or read arbitrary app-server endpoints. Its
trusted caller supplies an installed-release hash policy and a NEW private root.
"""
import copy
import hashlib
from pathlib import Path
import re
import threading
import uuid
from . import private_directory as private
from .records import atomic_json,canonical,create_json,file_lock,read_json,utcnow
from .profiles import SYNTHETIC,PRODUCT,source_facts

SCHEMA='anet-owned-sources/1'
MAX_SOURCES=32
HASH=re.compile('[0-9a-f]{64}')
PROCESS_KEYS={'pid','creation_filetime','exe','exe_sha256','owner_sid'}
STARTUP_KEYS={'protocol','instance','pipe','broker','codex','launcher','script_sha256','created_at',
              'owner_sid','state_dir','owned_job_kill_on_broker_exit'}
BINDING_KEYS={'schema','instance','session','cwd','codex','broker'}

class SourceUnavailable(ValueError):pass

def identity(path):
    info=private.plain(path).stat();return [info.st_dev,info.st_ino]
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def uuid_text(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value:raise ValueError('source_key_invalid')
    return value
def client_name(key):return 'codex_owned_'+uuid.UUID(key).hex
def production_factory(startup,binding):
    # Importing the safe IPC implementation does not open a pipe or start a host.
    from .adapter import OwnedSourceRequester
    return OwnedSourceRequester(startup,binding)

class SourceContract:
    """Read-only verifier; construction never creates registry files or a host."""
    def __init__(self,release_policy,adapter_factory=production_factory):
        expected={'broker_script_sha256','broker_exe_sha256','codex_exe_sha256'}
        if not isinstance(release_policy,dict) or set(release_policy) not in (expected,expected|{'compatible_releases'}):
            raise ValueError('explicit_installed_release_hash_policy_required')
        primary={k:release_policy[k] for k in expected}
        compatible=release_policy.get('compatible_releases',[])
        if not isinstance(compatible,list) or len(compatible)>8:
            raise ValueError('explicit_installed_release_hash_policy_required')
        policies=[primary]+compatible
        if any(not isinstance(p,dict) or set(p)!=expected or any(not isinstance(v,str) or not HASH.fullmatch(v) for v in p.values()) for p in policies):
            raise ValueError('explicit_installed_release_hash_policy_required')
        # Whole reviewed release tuples are allowed. Never combine one build's
        # script with another build's interpreter or native executable.
        self.policy=primary;self.policies=copy.deepcopy(policies);self.factory=adapter_factory

    def preflight(self,key,startup,binding):
        record=self._normalize(key,startup,binding)
        if self.factory is production_factory:
            from . import ipc_windows
            # Refuse a closed/reused host before opening its pipe or transmitting
            # even an inspect request. These checks only read OS process identity.
            ipc_windows.verify_identity(record['startup']['broker'])
            ipc_windows.verify_identity(record['startup']['codex'])
        _,observed=self._observe(record)
        return dict(self._receipt(record,observed),operation='read_only_source_preflight',registry_written=False)

    def _normalize(self,key,startup,binding):
        key=uuid_text(key)
        if not isinstance(startup,dict) or set(startup)!=STARTUP_KEYS:raise ValueError('startup_fields_invalid')
        if not isinstance(binding,dict):raise ValueError('binding_fields_invalid')
        expected_binding=BINDING_KEYS|({'purpose'} if binding.get('schema')==2 else set())
        if set(binding)!=expected_binding:raise ValueError('binding_fields_invalid')
        if len(canonical({'startup':startup,'binding':binding}))>16384:raise ValueError('source_registration_too_large')
        instance=uuid_text(startup.get('instance'))
        if startup.get('protocol')!='anet-source/1' or startup.get('pipe')!='\\\\.\\pipe\\anet-source-'+instance:
            raise ValueError('exact_local_source_pipe_required')
        if startup.get('owned_job_kill_on_broker_exit') is not True:raise ValueError('owned_host_lifetime_unverified')
        if startup.get('owner_sid')!=private.owner_id():raise ValueError('source_owner_mismatch')
        for name in ('broker','codex','launcher'):
            process=startup.get(name)
            if not isinstance(process,dict) or set(process)!=PROCESS_KEYS:raise ValueError('process_identity_fields_invalid')
            if type(process['pid']) is not int or process['pid']<=0 or type(process['creation_filetime']) is not int or process['creation_filetime']<=0:
                raise ValueError('process_identity_invalid')
            if process['owner_sid']!=startup['owner_sid'] or not isinstance(process['exe_sha256'],str) or not HASH.fullmatch(process['exe_sha256']):
                raise ValueError('process_identity_owner_or_hash_invalid')
            if not isinstance(process['exe'],str) or not Path(process['exe']).is_absolute():raise ValueError('process_executable_path_invalid')
        if not any(startup['script_sha256']==policy['broker_script_sha256']
            and startup['broker']['exe_sha256']==policy['broker_exe_sha256']
            and startup['codex']['exe_sha256']==policy['codex_exe_sha256'] for policy in self.policies):
            raise ValueError('source_release_policy_mismatch')
        if type(binding.get('schema')) is not int or binding.get('schema') not in (1,2) or binding.get('instance')!=instance or any(binding.get(k)!=startup[k] for k in ('broker','codex')):
            raise ValueError('explicit_source_startup_mismatch')
        if binding['schema']==2 and binding.get('purpose') not in (SYNTHETIC,PRODUCT):raise ValueError('source_purpose_invalid')
        sid=binding.get('session')
        if not isinstance(sid,str) or not sid or len(sid)>100 or any(ord(c)<32 for c in sid):raise ValueError('session_id_invalid')
        cwd=binding.get('cwd')
        if not isinstance(cwd,str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():raise ValueError('source_cwd_invalid')
        canonical_cwd=str(private.plain(cwd))
        if Path(canonical_cwd)!=Path(cwd):raise ValueError('source_cwd_not_canonical')
        # Copy only the strict public record. Never copy a token/config/private key.
        return {'source_key':key,'client':client_name(key),'startup':copy.deepcopy(startup),
                'binding':copy.deepcopy(binding)}

    def _observe(self,record):
        normalized=self._normalize(record['source_key'],record['startup'],record['binding'])
        if normalized!=record:raise ValueError('stored_source_registration_changed')
        adapter=self.factory(copy.deepcopy(record['startup']),copy.deepcopy(record['binding']))
        observed=adapter.inspect(record['binding']['session'])
        if (not isinstance(observed,dict) or observed.get('session')!=record['binding']['session']
            or Path(observed.get('cwd','')).resolve()!=Path(record['binding']['cwd'])
            or observed.get('loaded_on_endpoint') is not True or observed.get('state') not in ('idle','active')
            or observed.get('host_instance_verified') is not True
            or observed.get('broker_instance')!=record['startup']['instance']
            or observed.get('process_identity')!=record['startup']['codex']):
            raise SourceUnavailable('owned_source_observation_mismatch')
        expected_purpose=record['binding'].get('purpose',SYNTHETIC)
        if observed.get('source_purpose',SYNTHETIC)!=expected_purpose:
            raise SourceUnavailable('owned_source_purpose_mismatch')
        return adapter,observed

    def _receipt(self,record,observed):
        return {'client':record['client'],'source_key':record['source_key'],'session':record['binding']['session'],
            'cwd':record['binding']['cwd'],'binding':{'state':'verified','source':'owned_source_registry',
                'instance':record['startup']['instance'],'record_sha256':digest(record),'at':utcnow()},
            'source_state':observed['state'],'remote_execution_verified':False,'a2a_task_sent':False,
            **source_facts(record['binding'])}

class OwnedSourceRegistry(SourceContract):
    def __init__(self,directory,*,release_policy,adapter_factory=production_factory):
        super().__init__(release_policy,adapter_factory)
        self.root=private.plain(directory)
        self.guard=threading.RLock()
        if not self.root.exists():
            private.create_new(self.root)
            with (self.root/'registry.lock').open('xb') as f:f.write(b'0');f.flush()
            create_json(self.root/'owner.json',{'schema':SCHEMA,'operator':private.owner_id(),
                'root':identity(self.root),'lock':identity(self.root/'registry.lock')})
            create_json(self.root/'sources.json',{'schema':SCHEMA,'sources':{}})
        self.validate_root()

    def validate_root(self):
        private.verify(self.root,root=True)
        owner=read_json(self.root/'owner.json')
        if not isinstance(owner,dict) or owner.get('schema')!=SCHEMA or owner.get('operator')!=private.owner_id():
            raise ValueError('registry_owner_invalid')
        if owner.get('root')!=identity(self.root) or owner.get('lock')!=identity(self.root/'registry.lock'):
            raise ValueError('registry_filesystem_identity_changed')
        for name in ('owner.json','registry.lock','sources.json'):private.verify(self.root/name)
        # Windows byte locks also deny reads through a second handle. Content is
        # checked by file_lock on its own handle before acquisition.
        if (self.root/'registry.lock').stat().st_size!=1:raise ValueError('registry_lock_changed')

    def _read(self):
        self.validate_root();value=read_json(self.root/'sources.json')
        if not isinstance(value,dict) or value.get('schema')!=SCHEMA or not isinstance(value.get('sources'),dict):
            raise ValueError('registry_record_invalid')
        if len(value['sources'])>MAX_SOURCES:raise ValueError('registry_capacity_invalid')
        return value

    def register(self,key,startup,binding):
        record=self._normalize(key,startup,binding)
        with self.guard,file_lock(self.root/'registry.lock'):
            table=self._read();entries=table['sources'];old=entries.get(key)
            if old is not None and (old.get('record')!=record or old.get('record_sha256')!=digest(record)):
                raise ValueError('source_key_reuse_mismatch')
            for other,entry in entries.items():
                if other!=key and entry['record']['binding']['session']==binding['session']:
                    raise ValueError('session_already_registered_under_another_key')
            if old is None and len(entries)>=MAX_SOURCES:raise ValueError('owned_source_capacity')
            _,observed=self._observe(record)
            entries[key]={'record':record,'record_sha256':digest(record),'active':True,
                'registered_at':old['registered_at'] if old else utcnow(),'last_verified_at':utcnow()}
            atomic_json(self.root/'sources.json',table)
        return self._receipt(record,observed)

    def recover(self,key,expected_record_sha256,startup,binding):
        from .recovery import process_gone
        record=self._normalize(key,startup,binding)
        with self.guard,file_lock(self.root/'registry.lock'):
            table=self._read();entry=table['sources'].get(uuid_text(key))
            if entry is None:raise SourceUnavailable('source_recovery_unknown_key_hold')
            old=entry['record']
            if entry.get('record_sha256')==digest(record):
                # Reconcile a registration ACK lost after a committed swap.
                if entry.get('recovery_origin_record_sha256')!=expected_record_sha256:
                    raise SourceUnavailable('source_recovery_origin_mismatch')
                _,observed=self._observe(record);return self._receipt(record,observed)
            if entry.get('record_sha256')!=expected_record_sha256 or digest(old)!=expected_record_sha256:
                raise SourceUnavailable('source_recovery_compare_and_swap_mismatch')
            if (binding.get('session')!=old['binding'].get('session') or binding.get('cwd')!=old['binding'].get('cwd')
                or binding.get('purpose')!=old['binding'].get('purpose') or binding.get('instance')==old['binding'].get('instance')):
                raise SourceUnavailable('source_recovery_identity_mismatch')
            if not process_gone(old['startup']['broker']) or not process_gone(old['startup']['codex']):
                raise SourceUnavailable('old_owned_host_still_live_hold')
            _,observed=self._observe(record)
            if observed.get('recovery_origin_record_sha256')!=expected_record_sha256:
                raise SourceUnavailable('source_recovery_broker_provenance_missing')
            lineage=entry.get('lineage',[])+[old]
            if len(lineage)>32:raise SourceUnavailable('source_recovery_lineage_capacity_hold')
            entry.update(record=record,record_sha256=digest(record),active=True,lineage=lineage,
                recovery_origin_record_sha256=expected_record_sha256,last_verified_at=utcnow())
            atomic_json(self.root/'sources.json',table)
        return self._receipt(record,observed)

    def _entry(self,key):
        uuid_text(key)
        with self.guard,file_lock(self.root/'registry.lock'):
            value=self._read()['sources'].get(key)
            if value is None:raise SourceUnavailable('source_key_unknown')
            record=value.get('record')
            if not isinstance(record,dict) or value.get('record_sha256')!=digest(record):raise ValueError('stored_source_pin_mismatch')
            return copy.deepcopy(value)

    def inspect(self,key):
        entry=self._entry(key)
        if entry.get('active') is not True:raise SourceUnavailable('source_registration_inactive')
        _,observed=self._observe(entry['record'])
        return self._receipt(entry['record'],observed)

    def deactivate(self,key):
        with self.guard,file_lock(self.root/'registry.lock'):
            table=self._read();entry=table['sources'].get(uuid_text(key))
            if entry is None:raise SourceUnavailable('source_key_unknown')
            entry.update(active=False,deactivated_at=utcnow());atomic_json(self.root/'sources.json',table)
        return {'source_key':key,'active':False,'host_stopped':False,'tasks_canceled':False}

    def adapter(self,key):
        self._entry(key)
        return RegisteredRequester(self,key)

    def adapters(self):
        # All registered adapters remain addressable for historical receipts;
        # readiness/inspect revalidate liveness and never trust saved ready=true.
        with self.guard,file_lock(self.root/'registry.lock'):keys=list(self._read()['sources'])
        return {client_name(key):RegisteredRequester(self,key) for key in keys}

class RegisteredRequester:
    def __init__(self,registry,key):self.registry,self.key=registry,key;self.name=client_name(key)
    def prepare_callback(self,rec,item,files,journal):
        from .summaries import prepare_summary
        measured=[{'name':Path(v['path']).name,'path':v['path'],'size':v['size'],'sha256':v['sha256'],'verified':True}
                  for v in files.get('files',[])] if files.get('state')=='verified' else []
        # Source facts come from the explicit tracked record; remote state is
        # retained as provider-reported evidence, not verified acceptance.
        observations={'execution':'provider_reported:'+str(item.get('state','not_provided')),
            'acceptance':'not_verified','files_received':files.get('state','not_provided'),
            'source_return':'pending','remote_envelope':item.get('env')}
        summary=prepare_summary(Path(journal).with_suffix('.summary'),task_id=rec['task_id'],
            requester_context=rec.get('requester_context','Original requester context not provided.'),
            remote_text=item['text'],observations=observations,files=measured)
        return summary['prompt']
    def inspect(self,session):
        entry=self.registry._entry(self.key);record=entry['record']
        if session!=record['binding']['session']:raise SourceUnavailable('adapter_source_id_mismatch')
        if entry.get('active') is not True:raise SourceUnavailable('source_registration_inactive')
        return self.registry._observe(record)[1]
    def current_session(self):return self.registry._entry(self.key)['record']['binding']['session']
    def find_session(self,task_id,since_ts):return None
    def cwd(self,session):return self.inspect(session)['cwd']
    def idle(self,session):return self.inspect(session)['state']=='idle'
    def readiness(self):
        try:
            value=self.inspect(self.current_session())
            return {'ready':True,'source':'explicit_owned_registry','source_key':self.key,
                    'session':value['session'],'at':utcnow()}
        except (OSError,ValueError,RuntimeError,EOFError):
            return {'ready':False,'source_key':self.key,'reason':'owned_source_not_currently_verified','at':utcnow()}
    def overview(self):
        entry=self.registry._entry(self.key)
        return {'ready':False,'source_key':self.key,'registered':True,'active':entry.get('active') is True,
            'reason':'per_source_live_preflight_required','at':utcnow()}
    def loaded_sources(self):
        try:value=self.inspect(self.current_session());return [{'session':value['session'],'cwd':value['cwd']}]
        except (OSError,ValueError,RuntimeError,EOFError):return []
    def delivery(self,session,prompt,cwd,journal):
        entry=self.registry._entry(self.key);record=entry['record']
        if session!=record['binding']['session'] or Path(cwd).resolve()!=Path(record['binding']['cwd']):
            return {'state':'binding_error','error':'explicit_owned_source_mismatch','do_not_resend':True,'at':utcnow()}
        # The underlying adapter preserves matching historical terminal receipts
        # before inspecting a now-offline host. New/unknown sends stay source-bound.
        existing=read_json(journal,{})
        historical_terminal=existing.get('state') in ('completed','failed','ambiguous') and existing.get('_source_broker')
        if entry.get('active') is not True and not historical_terminal:
            return {'state':'reconciling' if existing else 'binding_error','error':'source_registration_inactive',
                    'do_not_resend':True,'historical_receipt_preserved':bool(existing),'at':utcnow()}
        adapter=self.registry.factory(copy.deepcopy(record['startup']),copy.deepcopy(record['binding']))
        adapter.recovery_lineage=copy.deepcopy(entry.get('lineage',[]))
        return adapter.delivery(session,prompt,cwd,journal)

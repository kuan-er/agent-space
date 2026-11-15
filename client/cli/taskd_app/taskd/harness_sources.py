"""Explicit same-owner harness registrations. Never discover, import payload paths or spawn."""
import copy
from contextlib import contextmanager
import hashlib
import importlib
from pathlib import Path
import re
import sys
import threading
import uuid

from owned_source_product import private_directory as private
from owned_source_product.records import atomic_json, canonical, create_json, file_lock, read_json, utcnow
from owned_source_product.registry import identity

SCHEMA='anet-taskd-harness-sources/1'
HASH=re.compile('[0-9a-f]{64}')
PROCESS_KEYS={'pid','creation_filetime','exe','exe_sha256','owner_sid'}
STARTUP_KEYS={'protocol','instance','codex','service_id','state_root','pipe','broker','at'}
LEGACY_PACKAGE_SCHEMA='anet-harness-package/1'
PACKAGE_SCHEMA='anet-harness-release-closure/2'
BINDING_KEYS={'protocol','registration_id','session','backend_session','harness','cwd','service_id',
              'state_root','client','source_purpose','real_submission_eligible'}
HARNESSES={'codex','claude_code','workbuddy','deepseek_harness'}


def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def uuid_text(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value:raise ValueError('canonical_uuid_required')
    return value
def key_text(value):
    if not isinstance(value,str) or not HASH.fullmatch(value):raise ValueError('harness_source_key_invalid')
    return value


def package_digest():
    """Fixed two-package release closure; no payload or caller module paths."""
    app=private.plain(Path(__file__).resolve().parents[1]);hashes={}
    for name in ('anet_harnesses','anet_provider_harnesses'):
        root=private.plain(app/name)
        if not root.is_dir() or not (root/'__init__.py').is_file():raise ValueError('bundled_harness_package_missing')
        for file in sorted(root.rglob('*.py')):
            private.plain(file)
            hashes[name+'/'+file.relative_to(root).as_posix()]=hashlib.sha256(file.read_bytes()).hexdigest()
    return digest(hashes)

def _legacy_package_digest():
    root=private.plain(Path(__file__).resolve().parents[1]/'anet_harnesses')
    hashes={}
    for file in sorted(root.rglob('*.py')):
        private.plain(file)
        hashes[file.relative_to(root).as_posix()]=hashlib.sha256(file.read_bytes()).hexdigest()
    if not {'__init__.py','taskd_bridge.py','ipc_windows.py'}.issubset(hashes):raise ValueError('bundled_harness_package_incomplete')
    return digest(hashes)


def _trusted_module(policy):
    root=private.plain(Path(__file__).resolve().parents[1]/'anet_harnesses')
    schema=policy.get('package_schema',LEGACY_PACKAGE_SCHEMA)
    actual=package_digest() if schema==PACKAGE_SCHEMA else _legacy_package_digest()
    if actual!=policy['package_sha256']:raise ValueError('harness_code_release_policy_mismatch')
    # Refuse an already loaded namespace from a different environment. The normal
    # application root is established by run_taskd.py; no sys.path is added here.
    for name,module in list(sys.modules.items()):
        if name=='anet_harnesses' or name.startswith('anet_harnesses.') or name=='anet_provider_harnesses' or name.startswith('anet_provider_harnesses.'):
            file=getattr(module,'__file__',None)
            expected=root if name.startswith('anet_harnesses') else root.parent/'anet_provider_harnesses'
            if not file or not Path(file).resolve().is_relative_to(expected):raise ValueError('harness_import_origin_mismatch')
    module=importlib.import_module('anet_harnesses.taskd_bridge')
    if Path(module.__file__).resolve()!=root/'taskd_bridge.py':raise ValueError('harness_import_origin_mismatch')
    return module


def _upgrade_registry_path(node_root):
    node=private.plain(node_root)
    if node.name.lower()!='taskd' or not node.is_dir():raise ValueError('explicit_installed_taskd_root_required')
    path=node/'harness-sources'
    try:info=path.lstat()
    except FileNotFoundError:return path
    if path.is_symlink() or getattr(info,'st_file_attributes',0)&0x400:raise ValueError('harness_registry_reparse_refused')
    return path


def _validated_release_policy(release_policy):
    if (not isinstance(release_policy,dict) or set(release_policy)-{'package_sha256','service_exe_sha256','package_schema','compatible_releases'}
        or any(not isinstance(release_policy.get(k),str) or not HASH.fullmatch(release_policy[k]) for k in ('package_sha256','service_exe_sha256'))
        or release_policy.get('package_schema',LEGACY_PACKAGE_SCHEMA) not in (LEGACY_PACKAGE_SCHEMA,PACKAGE_SCHEMA)):
        raise ValueError('explicit_harness_release_policy_required')
    policy={k:release_policy[k] for k in ('package_sha256','service_exe_sha256')}
    policy['package_schema']=release_policy.get('package_schema',LEGACY_PACKAGE_SCHEMA)
    compatible=copy.deepcopy(release_policy.get('compatible_releases',[]))
    if not isinstance(compatible,list) or len(compatible)>8:raise ValueError('bounded_harness_compatibility_required')
    pins=set()
    for release in compatible:
        if (not isinstance(release,dict) or set(release)!={'package_schema','package_sha256','service_exe_sha256','registered_records'}
            or release['package_schema'] not in (LEGACY_PACKAGE_SCHEMA,PACKAGE_SCHEMA)
            or any(not isinstance(release[k],str) or not HASH.fullmatch(release[k]) for k in ('package_sha256','service_exe_sha256'))
            or not isinstance(release['registered_records'],list) or not 1<=len(release['registered_records'])<=32):
            raise ValueError('harness_compatible_release_invalid')
        for value in release['registered_records']:
            if not isinstance(value,str) or not HASH.fullmatch(value) or value in pins:raise ValueError('harness_compatible_record_pin_invalid')
            pins.add(value)
    return policy,compatible


def prepare_release_upgrade(node_root,previous_policy,service_exe_sha256):
    """Read-only policy migration after installer verifies its previous receipt.

    Past private verified records are approved individually under their complete
    old tuple. No new registration, service discovery, IPC, import from an old
    path, ACL repair, registry write or process start occurs here. The caller must
    verify_upgrade_snapshot immediately before its atomic configuration commit.
    """
    if not isinstance(service_exe_sha256,str) or not HASH.fullmatch(service_exe_sha256):raise ValueError('explicit_service_executable_digest_required')
    primary={'package_schema':PACKAGE_SCHEMA,'package_sha256':package_digest(),'service_exe_sha256':service_exe_sha256}
    prior_parts=_validated_release_policy(previous_policy) if previous_policy is not None else None
    groups={}
    if prior_parts is not None:
        for approved in prior_parts[1]:
            signature=tuple(approved[name] for name in ('package_schema','package_sha256','service_exe_sha256'))
            groups.setdefault(signature,set()).update(approved['registered_records'])
    def retain_approved_groups():
        if len(groups)>8:raise ValueError('bounded_harness_compatibility_required')
        if any(len(pins)>32 for pins in groups.values()):raise ValueError('harness_compatible_release_invalid')
        if groups:
            primary['compatible_releases']=[dict(zip(('package_schema','package_sha256','service_exe_sha256'),group),registered_records=sorted(pins)) for group,pins in sorted(groups.items())]
        _validated_release_policy(primary)
    path=_upgrade_registry_path(node_root)
    snapshot={'schema':'anet-harness-release-upgrade/1','release_policy':primary,'registry_present':path.exists(),
              'before_registry_sha256':None,'before_root_identity':None,'registered_record_count':0}
    if not path.exists():
        retain_approved_groups()
        return snapshot
    # A dummy factory is deliberately never called: validation uses existing
    # private owner/filesystem identities and the already-approved prior policy,
    # without trying to import code belonging to a different release.
    old=HarnessSourceRegistry(path,release_policy=previous_policy,adapter_factory=lambda *_:None)
    with old.guard,file_lock(path/'registry.lock'):
        table=old._read()
        snapshot['before_root_identity']=identity(path)
        snapshot['before_registry_sha256']=hashlib.sha256((path/'sources.json').read_bytes()).hexdigest()
        for key,entry in table['sources'].items():
            if (not isinstance(entry,dict) or not isinstance(entry.get('record'),dict)
                or entry.get('record_sha256')!=digest(entry['record'])
                or type(entry.get('active')) is not bool or not isinstance(entry.get('registered_at'),str)
                or not entry['registered_at'] or not isinstance(entry.get('last_verified_at'),str) or not entry['last_verified_at']):
                raise ValueError('past_verified_harness_record_required')
            record=entry['record'];release=old.policy
            for approved in old.compatible:
                if entry['record_sha256'] in approved['registered_records']:release=approved;break
            if old._normalize(key,record['startup'],record['binding'],release)!=record:raise ValueError('past_harness_record_policy_or_identity_changed')
            signature=tuple(release[name] for name in ('package_schema','package_sha256','service_exe_sha256'))
            if signature!=tuple(primary[name] for name in ('package_schema','package_sha256','service_exe_sha256')):
                groups.setdefault(signature,set()).add(entry['record_sha256'])
        retain_approved_groups()
        snapshot['registered_record_count']=len(table['sources'])
    return snapshot


def verify_upgrade_snapshot(node_root,snapshot):
    """Fail closed if any registry changes between preparation and config commit."""
    if not isinstance(snapshot,dict) or snapshot.get('schema')!='anet-harness-release-upgrade/1':raise ValueError('harness_upgrade_snapshot_required')
    path=_upgrade_registry_path(node_root)
    if path.exists() is not snapshot['registry_present']:raise ValueError('harness_upgrade_registry_changed')
    if not path.exists():return True
    registry=HarnessSourceRegistry(path,release_policy=snapshot['release_policy'],adapter_factory=lambda *_:None)
    with registry.guard,file_lock(path/'registry.lock'):
        registry._read()
        if identity(path)!=snapshot['before_root_identity'] or hashlib.sha256((path/'sources.json').read_bytes()).hexdigest()!=snapshot['before_registry_sha256']:
            raise ValueError('harness_upgrade_registry_changed')
    return True


@contextmanager
def release_upgrade_guard(node_root,snapshot):
    """Hold the existing registration lock across caller's config commit.

    The installer also holds its node/update lifecycle lock. On a fresh node
    without a registry it must keep the service stopped until setup commits.
    This function never creates/rewrites a registry or starts/stops a process.
    """
    path=_upgrade_registry_path(node_root)
    if not isinstance(snapshot,dict) or snapshot.get('schema')!='anet-harness-release-upgrade/1':raise ValueError('harness_upgrade_snapshot_required')
    if path.exists() is not snapshot['registry_present']:raise ValueError('harness_upgrade_registry_changed')
    if not path.exists():
        yield snapshot['release_policy'];return
    registry=HarnessSourceRegistry(path,release_policy=snapshot['release_policy'],adapter_factory=lambda *_:None)
    with registry.guard,file_lock(path/'registry.lock'):
        registry._read()
        if identity(path)!=snapshot['before_root_identity'] or hashlib.sha256((path/'sources.json').read_bytes()).hexdigest()!=snapshot['before_registry_sha256']:
            raise ValueError('harness_upgrade_registry_changed')
        yield snapshot['release_policy']


def preserve_owned_release_policy(previous_policy,new_release):
    """Keep the old primary and whole approved tuples when installer moves code."""
    from owned_source_product.registry import SourceContract
    SourceContract(new_release)  # Pure validation: no registry, process or IPC.
    if previous_policy is None:return copy.deepcopy(new_release)
    approved=SourceContract(previous_policy)
    result=copy.deepcopy(previous_policy)
    if new_release not in approved.policies:
        compatible=result.setdefault('compatible_releases',[])
        if len(compatible)>=8:raise ValueError('owned_release_compatibility_capacity')
        compatible.append(copy.deepcopy(new_release))
    SourceContract(result)
    return result


class HarnessSourceRegistry:
    def __init__(self,directory,*,release_policy,adapter_factory=None,process_verifier=None,gone=None):
        self.policy,self.compatible=_validated_release_policy(release_policy)
        self.factory=adapter_factory;self.process_verifier=process_verifier;self.gone=gone
        if adapter_factory is None:_trusted_module(self.policy)
        self.root=private.plain(directory);self.guard=threading.RLock()
        if not self.root.exists():
            private.create_new(self.root)
            with (self.root/'registry.lock').open('xb') as stream:stream.write(b'0')
            create_json(self.root/'owner.json',{'schema':SCHEMA,'operator':private.owner_id(),
                'root':identity(self.root),'lock':identity(self.root/'registry.lock')})
            create_json(self.root/'sources.json',{'schema':SCHEMA,'sources':{}})
        self.validate_root()

    def validate_root(self):
        private.verify(self.root,root=True)
        owner=read_json(self.root/'owner.json')
        if (not isinstance(owner,dict) or owner.get('schema')!=SCHEMA or owner.get('operator')!=private.owner_id()
            or owner.get('root')!=identity(self.root) or owner.get('lock')!=identity(self.root/'registry.lock')):
            raise ValueError('harness_registry_owner_or_filesystem_mismatch')
        for name in ('owner.json','sources.json','registry.lock'):private.verify(self.root/name)
        if (self.root/'registry.lock').stat().st_size!=1:raise ValueError('harness_registry_lock_invalid')

    def _read(self):
        self.validate_root();value=read_json(self.root/'sources.json')
        if (not isinstance(value,dict) or value.get('schema')!=SCHEMA or not isinstance(value.get('sources'),dict)
            or len(value['sources'])>32):raise ValueError('harness_registry_record_invalid')
        return value

    def _normalize(self,key,startup,binding,release=None):
        key=key_text(key)
        release=release or self.policy
        fields=STARTUP_KEYS|{'package_sha256','package_schema'} if release['package_schema']==PACKAGE_SCHEMA else STARTUP_KEYS
        if not isinstance(startup,dict) or set(startup)!=fields:raise ValueError('harness_startup_fields_invalid')
        if release['package_schema']==PACKAGE_SCHEMA and (startup['package_schema']!=release['package_schema'] or startup['package_sha256']!=release['package_sha256']):
            raise ValueError('harness_host_package_release_mismatch')
        if not isinstance(binding,dict) or set(binding)!=BINDING_KEYS:raise ValueError('harness_binding_fields_invalid')
        if len(canonical({'startup':startup,'binding':binding}))>16384:raise ValueError('harness_registration_too_large')
        uuid_text(startup['instance']);uuid_text(startup['service_id']);uuid_text(binding['session'])
        if (startup['protocol']!='anet-harness-endpoint/1' or startup['codex']!=startup['service_id']
            or not isinstance(startup['pipe'],str) or not re.fullmatch(r'\\\\\.\\pipe\\anet-source-harness-[0-9a-f]{32}',startup['pipe'])):
            raise ValueError('exact_local_harness_pipe_required')
        process=startup['broker']
        if (not isinstance(process,dict) or set(process)!=PROCESS_KEYS or type(process['pid']) is not int or process['pid']<=0
            or type(process['creation_filetime']) is not int or process['creation_filetime']<=0
            or process['owner_sid']!=private.owner_id() or process['exe_sha256']!=release['service_exe_sha256']
            or not isinstance(process['exe'],str) or not Path(process['exe']).is_absolute()):
            raise ValueError('harness_process_identity_or_release_invalid')
        if (binding['protocol']!='anet-harness-binding/1' or binding['registration_id']!=key
            or binding['client']!='harness_owned_'+key[:24] or binding['harness'] not in HARNESSES
            or binding['source_purpose']!='delegation_requester' or binding['real_submission_eligible'] is not True
            or binding['service_id']!=startup['service_id'] or binding['state_root']!=startup['state_root']):
            raise ValueError('harness_source_binding_mismatch')
        pin={name:binding[name] for name in ('session','backend_session','harness','cwd','service_id','state_root')}
        if digest(pin)!=key:raise ValueError('harness_registration_digest_mismatch')
        if not isinstance(binding['backend_session'],str) or not 1<=len(binding['backend_session'])<=200 or any(ord(c)<32 for c in binding['backend_session']):
            raise ValueError('harness_backend_session_invalid')
        for name in ('cwd','state_root'):
            value=binding[name]
            if not isinstance(value,str) or not Path(value).is_absolute() or not Path(value).is_dir() or str(private.plain(value))!=value:
                raise ValueError('canonical_harness_source_directory_required')
        private.verify(Path(binding['state_root']),root=True)
        return {'source_key':key,'client':binding['client'],'startup':copy.deepcopy(startup),'binding':copy.deepcopy(binding),
                'state_root_identity':identity(Path(binding['state_root']))}

    def _factory(self,startup,binding):
        if self.factory is not None:return self.factory(copy.deepcopy(startup),copy.deepcopy(binding))
        module=_trusted_module(self.policy)
        return module.TaskdHarnessRequester(copy.deepcopy(startup),copy.deepcopy(binding))

    def _observe(self,record):
        release=self.policy
        for approved in self.compatible:
            if digest(record) in approved['registered_records']:release=approved;break
        if self._normalize(record['source_key'],record['startup'],record['binding'],release)!=record:raise ValueError('harness_stored_record_changed')
        if self.process_verifier is not None:self.process_verifier(record['startup']['broker'])
        elif self.factory is None:
            _trusted_module(self.policy)
            from anet_harnesses.ipc_windows import verify_identity
            verify_identity(record['startup']['broker'])
        adapter=self._factory(record['startup'],record['binding']);observed=adapter.inspect(record['binding']['session'])
        b=record['binding']
        if (not isinstance(observed,dict) or any(observed.get(k)!=b[k] for k in
                ('session','backend_session','harness','cwd','service_id','state_root','source_purpose','real_submission_eligible'))
            or observed.get('loaded_on_endpoint') is not True or observed.get('state') not in ('idle','active')
            or observed.get('host_instance_verified') is not True
            or observed.get('broker_instance')!=record['startup']['instance'] or observed.get('broker')!=record['startup']['broker']):
            raise ValueError('harness_live_source_observation_mismatch')
        if release['package_schema']==PACKAGE_SCHEMA and (observed.get('package_sha256')!=release['package_sha256'] or observed.get('package_schema')!=release['package_schema']):
            raise ValueError('harness_live_host_package_mismatch')
        return adapter,observed

    def _receipt(self,record,observed):
        return {'client':record['client'],'source_key':record['source_key'],'session':record['binding']['session'],
            'cwd':record['binding']['cwd'],'binding':{'state':'verified','source':'harness_source_registry',
                'instance':record['startup']['instance'],'record_sha256':digest(record),'at':utcnow()},
            'source_state':observed['state'],'source_purpose':'delegation_requester','real_submission_eligible':True,
            'remote_execution_verified':False,'a2a_task_sent':False}

    def register(self,key,startup,binding):
        with self.guard,file_lock(self.root/'registry.lock'):
            table=self._read();entries=table['sources'];prior=entries.get(key)
            release=self.policy
            if prior and prior['record']['startup']==startup and prior['record']['binding']==binding:
                for approved in self.compatible:
                    if prior.get('record_sha256') in approved['registered_records']:release=approved;break
            record=self._normalize(key,startup,binding,release)
            if prior and (prior.get('record')!=record or prior.get('record_sha256')!=digest(record)):
                raise ValueError('harness_source_key_reuse_requires_explicit_recovery')
            for other,entry in entries.items():
                if other!=key and (entry['record']['binding']['session']==binding['session']
                    or entry['record']['client']==binding['client']):raise ValueError('harness_source_already_registered')
            if prior is None and len(entries)>=32:raise ValueError('harness_source_capacity')
            _,observed=self._observe(record)
            entries[key]=dict(prior or {},record=record,record_sha256=digest(record),active=True,
                registered_at=prior['registered_at'] if prior else utcnow(),last_verified_at=utcnow())
            atomic_json(self.root/'sources.json',table)
        return self._receipt(record,observed)

    def recover(self,key,expected_record_sha256,startup,binding):
        record=self._normalize(key,startup,binding)
        with self.guard,file_lock(self.root/'registry.lock'):
            table=self._read();entry=table['sources'].get(key)
            if entry is None:raise ValueError('harness_recovery_unknown_source')
            old=entry['record']
            if entry.get('record_sha256')==digest(record):
                if entry.get('recovery_origin_record_sha256')!=expected_record_sha256:raise ValueError('harness_recovery_origin_mismatch')
                _,observed=self._observe(record);return self._receipt(record,observed)
            if entry.get('record_sha256')!=expected_record_sha256 or digest(old)!=expected_record_sha256:
                raise ValueError('harness_recovery_compare_and_swap_mismatch')
            if (binding!=old['binding'] or record['state_root_identity']!=old.get('state_root_identity')
                or startup['instance']==old['startup']['instance']):
                raise ValueError('harness_recovery_source_identity_mismatch')
            if self.gone is not None:gone=self.gone(old['startup']['broker'])
            else:
                from owned_source_product.recovery import process_gone
                gone=process_gone(old['startup']['broker'])
            if gone is not True:raise ValueError('old_harness_host_still_live_or_exit_unverified')
            _,observed=self._observe(record)
            lineage=entry.get('lineage',[])+[old]
            if len(lineage)>32:raise ValueError('harness_recovery_lineage_capacity')
            entry.update(record=record,record_sha256=digest(record),active=True,lineage=lineage,
                recovery_origin_record_sha256=expected_record_sha256,last_verified_at=utcnow())
            atomic_json(self.root/'sources.json',table)
        return self._receipt(record,observed)

    def _entry(self,key):
        key_text(key)
        with self.guard,file_lock(self.root/'registry.lock'):
            entry=self._read()['sources'].get(key)
            if not entry or not isinstance(entry.get('record'),dict) or digest(entry['record'])!=entry.get('record_sha256'):
                raise ValueError('harness_source_missing_or_record_pin_mismatch')
            return copy.deepcopy(entry)

    def adapter(self,key):self._entry(key);return RegisteredHarnessRequester(self,key)
    def adapters(self):
        with self.guard,file_lock(self.root/'registry.lock'):keys=list(self._read()['sources'])
        return {'harness_owned_'+key[:24]:RegisteredHarnessRequester(self,key) for key in keys}


class RegisteredHarnessRequester:
    def __init__(self,registry,key):self.registry,self.key=registry,key;self.name='harness_owned_'+key[:24]
    def inspect(self,session):
        entry=self.registry._entry(self.key)
        if session!=entry['record']['binding']['session'] or entry.get('active') is not True:raise ValueError('harness_registered_source_mismatch')
        return self.registry._observe(entry['record'])[1]
    def current_session(self):return self.registry._entry(self.key)['record']['binding']['session']
    def find_session(self,task_id,since_ts):return None
    def cwd(self,session):return self.inspect(session)['cwd']
    def idle(self,session):return self.inspect(session)['state']=='idle'
    def readiness(self):
        try:self.inspect(self.current_session());return {'ready':True,'source':'explicit_harness_registry','at':utcnow()}
        except Exception:return {'ready':False,'reason':'harness_source_live_preflight_required','at':utcnow()}
    def overview(self):
        entry=self.registry._entry(self.key)
        return {'ready':False,'registered':True,'active':entry.get('active') is True,
                'source':'explicit_harness_registry','reason':'per_source_live_preflight_required','at':utcnow()}
    def loaded_sources(self):
        try:s=self.inspect(self.current_session());return [{'session':s['session'],'cwd':s['cwd']}]
        except Exception:return []
    def prepare_callback(self,rec,item,files,journal):
        from owned_source_product.summaries import prepare_summary
        measured=[{'name':Path(v['path']).name,'path':v['path'],'size':v['size'],'sha256':v['sha256'],'verified':True}
                  for v in files.get('files',[])] if files.get('state')=='verified' else []
        summary=prepare_summary(Path(journal).with_suffix('.summary'),task_id=rec['task_id'],
            requester_context=rec.get('requester_context','Original requester context not provided.'),
            remote_text=item['text'],observations={'execution':'provider_reported:'+str(item.get('state','not_provided')),
                'acceptance':'not_verified','files_received':files.get('state','not_provided'),
                'source_return':'pending','remote_envelope':item.get('env')},files=measured)
        return summary['prompt']
    def delivery(self,session,prompt,cwd,journal):
        entry=self.registry._entry(self.key);record=entry['record']
        if session!=record['binding']['session'] or str(Path(cwd).resolve())!=record['binding']['cwd']:
            return {'state':'binding_error','error':'registered_harness_source_mismatch','do_not_resend':True,'at':utcnow()}
        # The bridge preserves exact terminal journals before any new live send.
        adapter=self.registry._factory(record['startup'],record['binding'])
        return adapter.delivery(session,prompt,cwd,journal)

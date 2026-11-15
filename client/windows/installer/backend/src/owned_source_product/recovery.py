"""Read-only provenance checks for explicitly selected package-owned sources."""
import hashlib
from pathlib import Path
import uuid
from . import private_directory as private
from .records import canonical,read_json

def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def proof_name(session):return 'source-'+hashlib.sha256(session.encode()).hexdigest()[:24]+'.proof.json'
def process_gone(expected,identity=None):
    if identity is None:
        from .ipc_windows import process_identity
        identity=process_identity
    try:return identity(expected['pid'])!=expected
    except OSError as error:
        # Access-denied, timeout and other observation failures are not exit.
        if getattr(error,'winerror',None) in (87,1168):return True
        raise ValueError('old_host_exit_unverified') from error

def record_for(key,startup,binding):
    if str(uuid.UUID(key))!=key:raise ValueError('canonical_source_key_required')
    return {'source_key':key,'client':'codex_owned_'+uuid.UUID(key).hex,'startup':startup,'binding':binding}

def load_owned_proof(broker_root,prior_state,session,key,expected_record,*,current_startup,
                     spec_hash,gone=process_gone):
    root=private.plain(broker_root);state=private.plain(prior_state)
    if state.parent!=root/'state' or str(uuid.UUID(state.name))!=state.name:
        raise ValueError('prior_owned_instance_directory_required')
    private.verify(state,root=True)
    for name in ('startup.json',proof_name(session)):private.verify(state/name)
    startup=read_json(state/'startup.json');proof=read_json(state/proof_name(session))
    if (not isinstance(startup,dict) or startup.get('instance')!=state.name or startup.get('state_dir')!=str(state)
        or startup.get('owner_sid')!=private.owner_id() or startup.get('owned_job_kill_on_broker_exit') is not True):
        raise ValueError('prior_startup_provenance_invalid')
    if (startup.get('script_sha256')!=current_startup.get('script_sha256') or
        startup.get('broker',{}).get('exe_sha256')!=current_startup.get('broker',{}).get('exe_sha256') or
        startup.get('codex',{}).get('exe_sha256')!=current_startup.get('codex',{}).get('exe_sha256')):
        raise ValueError('source_recovery_release_mismatch_hold')
    binding=(proof or {}).get('binding') or {}
    if (proof.get('schema')!=1 or proof.get('source_key')!=key or proof.get('startup_sha256')!=digest(startup)
        or proof.get('thread_spec_sha256')!=spec_hash(binding.get('cwd'),binding.get('purpose'))
        or binding.get('session')!=session or binding.get('instance')!=startup['instance']
        or any(binding.get(k)!=startup.get(k) for k in ('broker','codex'))
        or digest(record_for(key,startup,binding))!=expected_record):
        raise ValueError('owned_source_provenance_mismatch')
    workspace=private.plain(binding.get('cwd',''))
    if workspace.parent!=root/'workspaces':raise ValueError('original_owned_workspace_required')
    private.verify(workspace,root=True)
    origin=private.plain(proof.get('origin_delivery_root',''))
    if origin.parent!=root/'state':raise ValueError('original_owned_delivery_directory_required')
    private.verify(origin,root=True)
    creation_id=proof.get('creation_id')
    if not isinstance(creation_id,str) or str(uuid.UUID(creation_id))!=creation_id:
        raise ValueError('original_owned_creation_identity_missing')
    creation_path=origin/('creation-'+creation_id+'.json');private.verify(creation_path)
    creation=read_json(creation_path) or {}
    lineage=proof.get('lineage_bindings',[])
    original=lineage[0] if lineage else binding
    if (creation.get('state')!='accepted' or creation.get('source_request_id')!=creation_id
        or creation.get('binding')!=original or creation.get('cwd')!=binding['cwd']
        or creation.get('purpose')!=binding['purpose']):
        raise ValueError('original_owned_thread_creation_not_proven')
    if not gone(startup['broker']) or not gone(startup['codex']):raise ValueError('old_owned_host_still_live_hold')
    return startup,proof

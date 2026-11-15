"""Client-owned source sessions with explicit, proof-bound cold recovery.

Prototype only: no UI or auto-start install, no A2A transport, no production edits.
"""
from __future__ import annotations
import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

CODE_ROOT = Path(__file__).resolve().parents[1]
_data_root = Path(os.environ.get('ANET_SOURCE_BROKER_ROOT',str(CODE_ROOT)))
if not _data_root.is_absolute():raise ValueError('absolute_owned_broker_data_root_required')
ROOT = _data_root.resolve()
sys.path.insert(0, str(CODE_ROOT / 'vendor'))
sys.path.insert(0, str(CODE_ROOT.parent / 'src'))
from owned_source_product.profiles import SYNTHETIC,PRODUCT,PURPOSES,thread_start_params,source_facts
from owned_source_product.recovery import digest,proof_name,load_owned_proof
from taskd.executors.codex import CodexExecutor
from taskd.executors.native_codex import NativeRPC
from taskd.requesters.codex import CodexRequester
from taskd.requesters.appserver import RPCError, RemoteRPCError, rpc_result
from taskd.store import atomic_json, read_json, utcnow
import winlocal

def valid_uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value: raise ValueError('canonical UUID required')
    return value

def canonical_workspace(path):
    candidate = Path(path)
    root = ROOT / 'workspaces'
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()) or resolved == root.resolve():
        raise ValueError('new broker-owned workspace required')
    for part in (resolved, *resolved.parents):
        if part.is_symlink() or part.is_junction(): raise ValueError('reparse workspace rejected')
        if part == root: break
    return str(resolved)

class OwnedRPC(NativeRPC):
    def __init__(self, executor, audit):
        super().__init__(executor)
        self.audit = audit

    def rpc(self, method, params, timeout=20, request_id=None):
        rid = request_id or str(uuid.uuid4())
        self.audit.append({'method': method, 'thread_id': params.get('threadId'), 'at': utcnow()})
        self.send({'id': rid, 'method': method, 'params': params})
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try: value = self.receive(min(1, max(.01, end-time.monotonic())))
            except TimeoutError: continue
            if not isinstance(value, dict): raise RPCError('invalid RPC message')
            if value.get('id') == rid:
                try:return rpc_result(value, rid)
                except RemoteRPCError as error:
                    # Preserve structured native rejection fields for narrow
                    # compatibility checks; never infer a protocol condition
                    # by searching a formatted or truncated error string.
                    error.code=value['error']['code']
                    error.native_message=value['error']['message']
                    raise
            self.dispatch(value)
        raise TimeoutError('native RPC acknowledgement unknown: ' + method)

    def pages(self, method, params):
        cursor, seen = None, set()
        for _ in range(1000):
            page = self.rpc(method, dict(params, cursor=cursor, limit=100))
            if not isinstance(page, dict) or not isinstance(page.get('data'), list): raise RPCError('invalid history page')
            yield from page['data']
            cursor = page.get('nextCursor')
            if cursor is None: return
            if not isinstance(cursor, str) or not cursor or cursor in seen: raise RPCError('non-progressing history cursor')
            seen.add(cursor)
        raise RPCError('history pages exceed operation budget')

class OwnedRequester(CodexRequester):
    def __init__(self, api): self.api = api
    def connection(self): return nullcontext(self.api)

class Broker:
    def __init__(self, api, record, state_dir):
        self.api, self.record, self.state_dir = api, record, Path(state_dir)
        self.requester = OwnedRequester(api)
        self.sources = {}
        self.creations = {}
        self.started_sources = set()
        self.shutdown = False
        self.rejections = 0
        self.proofs={};self.recoveries={}

    def verify_host(self):
        if self.api.process.poll() is not None: raise RuntimeError('owned_codex_exited: open a NEW source; old source is not resumed')
        winlocal.verify_identity(self.record['codex'])

    def source(self, binding):
        if not isinstance(binding, dict) or binding.get('instance') != self.record['instance']:
            raise ValueError('source_instance_mismatch: source belongs to a different startup; no resume or send')
        sid = binding.get('session')
        known = self.sources.get(sid)
        if known is None or known != binding: raise ValueError('source_not_owned: explicit source was not created by this broker instance')
        self.verify_host()
        return known

    def inspect_source(self, binding):
        self.source(binding)
        observed = self.requester.inspect(binding['session'])
        if observed['session'] != binding['session'] or Path(observed['cwd']).resolve() != Path(binding['cwd']).resolve():
            raise ValueError('source_metadata_mismatch: no send')
        if not observed['loaded_on_endpoint'] or observed['state'] not in ('idle', 'active'):
            raise ValueError('owned_source_not_loaded: no automatic resume')
        observed.update(host_instance_verified=True, broker_instance=self.record['instance'],
                        ownership='created_by_this_broker', process_identity=self.record['codex'],
                        **source_facts(binding))
        proof=self.proofs.get(binding['session']) or {}
        if proof.get('recovery_origin_record_sha256'):
            observed.update(ownership='recovered_explicit_owned_source',
                recovery_origin_record_sha256=proof['recovery_origin_record_sha256'])
        return observed

    def deliver(self, binding, delivery_id, prompt):
        self.source(binding)
        valid_uuid(delivery_id)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode('utf-8')) > 16384:
            raise ValueError('bounded nonempty delivery text required (16 KiB)')
        proof=self.proofs.get(binding['session']) or {}
        delivery_root=Path(proof.get('origin_delivery_root',self.state_dir))
        path = delivery_root / ('delivery-' + delivery_id + '.json')
        pin_path = delivery_root / ('delivery-' + delivery_id + '.binding.json')
        pin = {'binding': binding, 'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest()}
        existing = read_json(pin_path, None)
        allowed=[pin]+[dict(pin,binding=old) for old in proof.get('lineage_bindings',[])]
        if existing is not None and existing not in allowed: raise ValueError('delivery_id_reuse_mismatch: no send')
        if existing is None:
            self.inspect_source(binding)
            atomic_json(pin_path, pin)
        # Existing request is reconciled, never replayed. Verify host/source even
        # for completed records, so restart cannot masquerade as live return.
        journals=list(delivery_root.glob('delivery-*.json'))
        if len(journals)>2048:raise ValueError('source_delivery_provenance_capacity_hold')
        if any((read_json(p,{}) or {}).get('request',{}).get('session')==binding['session'] for p in journals):
            self.started_sources.add(binding['session'])
        if binding['session'] not in self.started_sources and not read_json(path, {}).get('request'):
            # A truly new owned thread is not materialized: turns/list is rejected
            # before its first user message. This branch is available only for a
            # thread created by THIS stdio host, never for an adopted/stored ID.
            rid, client_id = str(uuid.uuid4()), str(uuid.uuid4())
            result = {'state': 'requested', 'request': {'id': rid, 'method': 'turn/start',
                'session': binding['session'], 'cwd': binding['cwd'], 'client_user_message_id': client_id,
                'prompt_sha256': pin['prompt_sha256'], 'at': utcnow()},
                'previous_turn_id': None, 'source_initial_input': True}
            atomic_json(path, result)
            self.started_sources.add(binding['session'])  # never a second bootstrap
            try:
                turn = self.api.rpc('turn/start', {'threadId': binding['session'],
                    'input': [{'type': 'text', 'text': prompt}], 'clientUserMessageId': client_id,
                    'turnTrigger': 'taskd'}, request_id=rid)['turn']
                result['ack'] = {'request_id': rid, 'turn_id': turn['id'], 'status': turn['status'], 'at': utcnow()}
                result['state'] = 'accepted'
            except RemoteRPCError as error:
                result.update(state='failed', error_kind='rpc_error_response', error=str(error)[:500], at=utcnow())
            except Exception as error:
                # Even an exception may follow an accepted write; reconcile only.
                result.update(state='reconciling', error=str(error)[:500], at=utcnow())
            atomic_json(path, result)
        else:
            result = self.requester.delivery(binding['session'], prompt, binding['cwd'], path)
        if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > 128 * 1024:
            raise ValueError('result exceeds prototype summary limit; full journal remains on disk')
        return result

    def dispatch(self, request):
        if request.get('protocol') != 'anet-source/1' or request.get('instance') != self.record['instance']:
            raise ValueError('startup_instance_mismatch: zero Codex operations')
        op = request.get('op')
        self.verify_host()
        if op == 'describe':
            return {'harness': 'codex', 'transport': 'owned_stdio', 'existing_gui_attach': False,
                    'source_count': len(self.sources), 'a2a_tasks_sent': 0, 'other_harnesses': 'unsupported',
                    'source_purposes':sorted(PURPOSES),'local_goal_execution':False,'remote_goal_verified':False,
                    'rpc_counts': {m: sum(v['method'] == m for v in self.api.audit) for m in sorted({v['method'] for v in self.api.audit})},
                    'rejections': self.rejections}
        if op == 'create_source':
            # Caller cannot supply an arbitrary existing thread or workspace.
            if set(request) - {'protocol', 'instance', 'op', 'source_request_id','purpose','source_key'}: raise ValueError('create_source accepts no adoption parameters')
            purpose=request.get('purpose',SYNTHETIC)
            if purpose not in PURPOSES:raise ValueError('explicit_source_purpose_required')
            creation_id = valid_uuid(request.get('source_request_id') or str(uuid.uuid4()))
            prior = self.creations.get(creation_id)
            if prior:
                if prior.get('purpose',SYNTHETIC)!=purpose:raise ValueError('source_creation_purpose_mismatch_no_replay')
                if prior.get('state')=='accepted' and prior.get('binding'):
                    return {'binding': prior['binding'], 'inspection': self.inspect_source(prior['binding']), 'replayed': True}
                raise RuntimeError('source_creation_ack_unknown: original creation may have taken effect; no repeated thread/start')
            if len(self.sources) >= 8: raise ValueError('prototype source capacity reached')
            workspace_root = winlocal.ensure_private_runtime_root(ROOT / 'workspaces')
            workspace = winlocal.create_private_directory(workspace_root / str(uuid.uuid4()))
            cwd = canonical_workspace(workspace)
            creation = {'source_request_id': creation_id, 'state': 'requested', 'cwd': cwd, 'at': utcnow(),'purpose':purpose}
            creation_path = self.state_dir / ('creation-' + creation_id + '.json')
            atomic_json(creation_path, creation)
            self.creations[creation_id] = creation
            result = self.api.rpc('thread/start',thread_start_params(cwd,purpose),request_id=creation_id)
            thread = result['thread']
            if not isinstance(thread.get('id'), str) or Path(thread['cwd']).resolve() != Path(cwd).resolve():
                raise RuntimeError('thread/start returned unexpected identity')
            binding = {'schema': 2, 'purpose':purpose,'instance': self.record['instance'], 'session': thread['id'], 'cwd': cwd,
                       'codex': self.record['codex'], 'broker': self.record['broker']}
            # A fresh thread/start can remain an unmaterialized native session
            # until its first stored event. Naming the actual native thread is
            # a documented metadata operation that persists it without a model
            # request or an invented rollout/history. Keep its intent durable
            # before sending and never repeat create/name on an unknown ACK.
            name_request_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'anet-source-name:'+creation_id))
            creation.update(state='materialization_requested',binding=binding,
                materialization={'method':'thread/name/set','request_id':name_request_id,'name':'ANet 来源会话','acknowledged':False})
            atomic_json(creation_path,creation)
            self.api.rpc('thread/name/set',{'threadId':binding['session'],'name':'ANet 来源会话'},request_id=name_request_id)
            creation['materialization']['acknowledged']=True
            self.sources[binding['session']] = binding
            proof={'schema':1,'source_key':valid_uuid(request.get('source_key') or creation_id),'binding':binding,
                'startup_sha256':digest(self.record),'creation_id':creation_id,
                'thread_spec_sha256':digest(thread_start_params(cwd,purpose)),
                'origin_delivery_root':str(self.state_dir),'lineage_bindings':[]}
            self.proofs[binding['session']]=proof
            atomic_json(self.state_dir/proof_name(binding['session']),proof)
            creation.update(state='accepted', binding=binding)
            atomic_json(creation_path, creation)
            atomic_json(self.state_dir / ('source-' + str(uuid.uuid4()) + '.json'), binding)
            return {'binding': binding, 'inspection': self.inspect_source(binding)}
        if op == 'inspect_creation':
            creation_id = valid_uuid(request.get('source_request_id'))
            prior = self.creations.get(creation_id)
            if not prior:
                return {'state': 'not_started', 'thread_start_performed': False}
            if prior.get('state')=='accepted' and prior.get('binding'):
                return {'state': 'accepted', 'binding': prior['binding'],
                        'inspection': self.inspect_source(prior['binding'])}
            return {'state': 'ack_unknown', 'thread_start_performed': 'unknown'}
        if op == 'inspect_source': return self.inspect_source(request.get('binding'))
        if op == 'recover_source':return self.recover_source(request)
        if op == 'deliver': return self.deliver(request.get('binding'), request.get('delivery_id'), request.get('prompt'))
        if op == 'shutdown':
            if any(self.requester.inspect(sid)['state'] == 'active' for sid in self.sources):
                raise ValueError('owned source active; graceful shutdown refused')
            self.shutdown = True
            return {'stopping': True, 'only_owned_process': self.record['codex']['pid']}
        raise ValueError('unsupported_operation')

    def recover_source(self,request):
        if set(request)!={'protocol','instance','op','recovery_id','prior_state_dir','session','source_key','expected_record_sha256'}:
            raise ValueError('exact_owned_recovery_fields_required')
        rid=valid_uuid(request['recovery_id']);sid=request['session']
        if not isinstance(sid,str) or not sid:raise ValueError('recovery_session_required')
        prior,proof=load_owned_proof(ROOT,request['prior_state_dir'],sid,request['source_key'],
            request['expected_record_sha256'],current_startup=self.record,
            spec_hash=lambda cwd,purpose:digest(thread_start_params(cwd,purpose)))
        pin={k:request[k] for k in ('prior_state_dir','session','source_key','expected_record_sha256')}
        path=self.state_dir/('recovery-'+rid+'.json');saved=read_json(path)
        if saved is not None and saved.get('pin')!=pin:raise ValueError('recovery_id_reuse_mismatch')
        if sid in self.sources:
            if self.proofs[sid].get('recovery_origin_record_sha256')!=request['expected_record_sha256']:
                raise ValueError('recovered_source_origin_mismatch')
            return {'binding':self.sources[sid],'inspection':self.inspect_source(self.sources[sid]),'replayed':True}
        if saved is None:
            saved={'state':'prepared','pin':pin,'request_id':rid,'resume_requested':False,'at':utcnow()}
            atomic_json(path,saved)
        elif saved.get('state')=='rejected':raise ValueError('owned_source_restore_explicitly_rejected')
        if saved.get('resume_requested',saved.get('state') not in ('prepared','preflight_failed')) is not True:
            try:
                # Some app-server stores expose cold metadata, while current
                # private stores reject that read until explicit restoration.
                # Only the exact structured "not loaded" response below can
                # omit this optional read. The private owned proof above pins
                # ID/cwd/original thread-start contract and proves old exit.
                try:thread=self.api.rpc('thread/read',{'threadId':sid,'includeTurns':False})['thread']
                except RemoteRPCError as error:
                    message=getattr(error,'native_message',None)
                    cold=(getattr(error,'code',None)==-32600 and isinstance(message,str)
                          and message.startswith('thread not loaded:') and message[len('thread not loaded:'):].strip()==sid)
                    if not cold:raise
                    saved['cold_metadata_unavailable']=True
                else:
                    if thread['id']!=sid or Path(thread['cwd']).resolve()!=Path(proof['binding']['cwd']):
                        raise ValueError('stored_owned_thread_metadata_mismatch')
                if sid in self.requester.loaded_ids(self.api):raise ValueError('source_already_loaded_unexpectedly_hold')
            except Exception:
                saved.update(state='preflight_failed',resume_requested=False);atomic_json(path,saved);raise
            # A failed loaded-set check above remains read-only. Only this record
            # marks a possible resume write, and it is durable before dispatch.
            saved.update(state='resume_requested',resume_requested=True);atomic_json(path,saved)
            try:
                resumed=self.api.rpc('thread/resume',{'threadId':sid,'excludeTurns':True},request_id=rid)['thread']
                if resumed.get('id')!=sid or Path(resumed.get('cwd','')).resolve()!=Path(proof['binding']['cwd']):
                    raise ValueError('resume_ack_identity_mismatch_hold')
                saved.update(state='resume_acknowledged',resume_ack={'session':sid,'cwd':resumed['cwd'],'at':utcnow()})
                atomic_json(path,saved)
            except RemoteRPCError as error:
                saved.update(state='rejected',error_kind='rpc_error_response');atomic_json(path,saved);raise
            except Exception:
                saved.update(state='resume_unconfirmed');atomic_json(path,saved);raise
        # Lost resume ACK: observe the SAME thread; never repeat resume/create/turn.
        if sid not in self.requester.loaded_ids(self.api):raise ValueError('owned_source_restore_unconfirmed_hold')
        thread=self.api.rpc('thread/read',{'threadId':sid,'includeTurns':False})['thread']
        if thread['id']!=sid or Path(thread['cwd']).resolve()!=Path(proof['binding']['cwd']):
            raise ValueError('restored_thread_identity_mismatch')
        old=proof['binding'];binding=dict(old,instance=self.record['instance'],broker=self.record['broker'],codex=self.record['codex'])
        recovered=dict(proof,binding=binding,startup_sha256=digest(self.record),
            lineage_bindings=proof.get('lineage_bindings',[])+[old],
            recovery_origin_record_sha256=request['expected_record_sha256'])
        if len(recovered['lineage_bindings'])>32:raise ValueError('recovery_lineage_capacity_hold')
        self.sources[sid]=binding;self.proofs[sid]=recovered
        atomic_json(self.state_dir/proof_name(sid),recovered)
        saved.update(state='accepted',binding=binding);atomic_json(path,saved)
        return {'binding':binding,'inspection':self.inspect_source(binding),'recovery_id':rid}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--instance', required=True)
    parser.add_argument('--pipe', required=True)
    parser.add_argument('--record', required=True)
    parser.add_argument('--codex', required=True)
    args = parser.parse_args()
    valid_uuid(args.instance)
    # State/workspaces are independent from immutable installed code. Existing
    # data roots must already have the operator's protected private ACL.
    winlocal.ensure_private_runtime_root(ROOT)
    lifetime_job = winlocal.own_lifetime_job()
    record_path = Path(args.record).resolve()
    if not record_path.is_relative_to(ROOT / 'state'): raise ValueError('isolated startup record required')
    # Launcher already created this unique private instance root. Avoid nesting
    # the same UUID twice, which pushes atomic journal paths past MAX_PATH in
    # ordinary extracted Windows packages without long-path policy changes.
    state_dir = winlocal.verify_private_runtime_root(record_path.parent)
    audit = []
    pipe = winlocal.make_pipe(args.pipe)
    try:
        with OwnedRPC(CodexExecutor(command=args.codex, sandbox='read-only'), audit) as api:
            if api.ws or api.process is None: raise RuntimeError('owned stdio process required')
            record = {'protocol': 'anet-source/1', 'instance': args.instance, 'pipe': args.pipe,
                      'broker': winlocal.process_identity(os.getpid()), 'codex': winlocal.process_identity(api.process.pid),
                      'launcher': winlocal.process_identity(os.getppid()), 'script_sha256': winlocal.sha_file(__file__),
                      'created_at': utcnow(), 'owner_sid': winlocal.owner_sid(), 'state_dir': str(state_dir)}
            record['owned_job_kill_on_broker_exit'] = bool(lifetime_job)
            atomic_json(record_path, record)
            broker = Broker(api, record, state_dir)
            while not broker.shutdown:
                try:
                    winlocal.operation(pipe, winlocal.ConnectNamedPipe, time.monotonic() + 30, connecting=True)
                    request = winlocal.recv_json(pipe, 10)
                    try:
                        result = broker.dispatch(request)
                        reply = {'ok': True, 'result': result}
                    except Exception as error:
                        broker.rejections += 1
                        reply = {'ok': False, 'error': str(error)[:600]}
                    reply.update(instance=record['instance'], codex=record['codex'])
                    winlocal.send_json(pipe, reply, 5)
                    if winlocal.recv_json(pipe, 3) != {'received': True}: raise ValueError('invalid receipt acknowledgement')
                except (OSError, ValueError, TimeoutError, EOFError):
                    pass  # Abandoned local client cannot kill or re-send a source turn.
                finally:
                    winlocal.DisconnectNamedPipe(pipe)
                    atomic_json(state_dir / 'rpc-audit.json', audit)
    finally:
        winlocal.CloseHandle(pipe)

if __name__ == '__main__': main()

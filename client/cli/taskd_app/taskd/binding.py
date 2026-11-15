"""Narrow local requester service: verify a source, submit, and bind an outbound task.

Clients exchange private files with the already-running daemon. This exposes neither
arbitrary app-server RPC nor local command execution, and works when a client cannot open its socket.
"""
from __future__ import annotations

import hashlib
import errno
import json
import os
from pathlib import Path
import time
import uuid

from .store import atomic_json, private_dir, read_json, safe_name, utcnow
from .protocol import ARTIFACT_DELIVERY

PROTOCOL = 'taskd-binding/1'
LIMIT = 262144


class BindingError(RuntimeError):
    def __init__(self, message, request_id=None, result=None):
        super().__init__(message)
        self.request_id = request_id
        self.result = result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def submission_evidence(path):
    """Read durable dispatch truth before checking current source availability."""
    missing={'task_id':None,'sent':False,'do_not_resubmit':False,'submission_intent':None}
    try:
        intent=json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None,missing
    except (OSError,ValueError):
        return None,dict(missing,sent=None,do_not_resubmit=True,submission_intent=str(path))
    if not isinstance(intent,dict):
        return None,dict(missing,sent=None,do_not_resubmit=True,submission_intent=str(path))
    tid=intent.get('task_id')
    tid=tid if isinstance(tid,str) and tid and safe_name(tid)==tid else None
    sent=True if tid else False if intent.get('state')=='prepared' and not intent.get('task_id') else None
    return intent,{'task_id':tid,'sent':sent,'do_not_resubmit':sent is not False,'submission_intent':str(path)}


def workspace_queue(root,cwd):
    key=hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
    return Path(cwd).resolve()/'.taskd-requester'/key


def source_metadata(session, cwd, client='codex'):
    if not isinstance(session, str) or not session.strip() or len(session)>100:
        raise BindingError('explicit source session is required; use the source session or --session')
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise BindingError('explicit existing source working directory is required')
    return {'session':session, 'cwd':str(Path(cwd).resolve()), 'client':client}


def verify_source(relay, source):
    if not isinstance(source, dict):
        raise BindingError('explicit source metadata is required')
    source = source_metadata(source.get('session'), source.get('cwd'), source.get('client'))
    adapter = relay.adapters.get(source['client'])
    if not adapter:
        raise BindingError('requester adapter is unavailable')
    observed = adapter.inspect(source['session'])
    if observed.get('session') != source['session']:
        raise BindingError('app-server returned a different source session')
    if Path(observed['cwd']).resolve() != Path(source['cwd']):
        raise BindingError('source session working directory mismatch')
    if observed.get('loaded_on_endpoint') is not True or observed.get('state') not in ('active', 'idle'):
        raise BindingError('source session is not loaded and usable on the configured host; open the intended source on its registered host; no task sent')
    result=dict(source,binding={'state':'verified','source':'requester_daemon','at':utcnow()})
    if str(source['client']).startswith(('codex_owned_','harness_owned_')):
        if observed.get('source_purpose')!='delegation_requester' or observed.get('real_submission_eligible') is not True:
            raise BindingError('registered owned source is synthetic; real task submission is disabled')
        result.update(source_purpose='delegation_requester',real_submission_eligible=True)
    return result


class BindingService:
    def __init__(self, root, relay, project_queue=None, owned_sources=None,harness_sources=None):
        self.relay = relay
        self.owned_sources=owned_sources
        self.harness_sources=harness_sources
        if harness_sources is not None:
            for name,adapter in harness_sources.adapters().items():
                if name in relay.adapters:raise BindingError('harness adapter name already in use')
                relay.adapters[name]=adapter
        if owned_sources is not None:
            for name,adapter in owned_sources.adapters().items():
                if name in relay.adapters:raise BindingError('owned adapter name already in use')
                relay.adapters[name]=adapter
        self.node_root=Path(root)
        if project_queue:
            self.root=Path(project_queue)
            self.inbox=self.root/'inbox'
            if any(p.is_symlink() or not p.is_dir() for p in (self.root.parent,self.root,self.inbox)):
                raise BindingError('invalid workspace inbox')
            # Never write a daemon result into an untrusted workspace. The sandbox
            # can read this node-owned outbox but cannot fabricate its receipts.
            self.receipts=Path(root)/'requester-commands/receipts'
        else:
            self.root=private_dir(Path(root)/'requester-commands')
            self.inbox=private_dir(self.root/'inbox')
            self.receipts=private_dir(self.root/'receipts')
        self.project_queues={}
        self.last_scan=0
        self.project_queue=project_queue

    def handle(self, request):
        if request.get('protocol') != PROTOCOL or request.get('operation') not in ('verify_source','bind_task','submit_task','register_owned_source','recover_owned_source','deactivate_owned_source','register_harness_source','recover_harness_source'):
            raise BindingError('unsupported requester command')
        expires = request.get('expires_at')
        if type(expires) not in (int,float) or not time.time()<expires<=time.time()+120:
            raise BindingError('requester command expired or invalid')
        payload = request.get('payload')
        if not isinstance(payload, dict):
            raise BindingError('invalid requester payload')
        if request['operation'] in ('register_harness_source','recover_harness_source'):
            if self.project_queue or self.harness_sources is None:
                raise BindingError('harness source registration is unavailable in this inbox')
            fields={'source_key','startup','binding'}
            if request['operation']=='recover_harness_source':fields.add('expected_record_sha256')
            if set(payload)!=fields:raise BindingError('exact public harness source registration fields required')
            if request['operation']=='recover_harness_source':
                result=self.harness_sources.recover(payload['source_key'],payload['expected_record_sha256'],payload['startup'],payload['binding'])
            else:result=self.harness_sources.register(payload['source_key'],payload['startup'],payload['binding'])
            self.relay.adapters[result['client']]=self.harness_sources.adapter(payload['source_key'])
            return result
        if request['operation'] in ('register_owned_source','recover_owned_source','deactivate_owned_source'):
            if self.project_queue or self.owned_sources is None:
                raise BindingError('owned source registration is unavailable in this inbox')
            if request['operation']=='deactivate_owned_source':
                return self.owned_sources.deactivate(payload.get('source_key'))
            if request['operation']=='recover_owned_source':
                if set(payload)!={'source_key','expected_record_sha256','startup','binding'}:
                    raise BindingError('exact owned recovery registration fields required')
                result=self.owned_sources.recover(payload['source_key'],payload['expected_record_sha256'],payload['startup'],payload['binding'])
                self.relay.adapters[result['client']]=self.owned_sources.adapter(payload['source_key'])
                return result
            if set(payload)!={'source_key','startup','binding'}:
                raise BindingError('exact public owned source registration fields required')
            result=self.owned_sources.register(payload['source_key'],payload['startup'],payload['binding'])
            self.relay.adapters[result['client']]=self.owned_sources.adapter(payload['source_key'])
            return result
        if self.project_queue:
            cwd=(payload.get('source') or {}).get('cwd')
            if not isinstance(cwd,str) or workspace_queue(self.node_root,cwd).resolve()!=self.root.resolve():
                raise BindingError('workspace inbox cannot bind another working directory')
        if request['operation']=='submit_task':
            return self.submit(request,payload)
        source = verify_source(self.relay, payload.get('source'))
        if request['operation']=='verify_source':
            return source
        tid = payload.get('task_id')
        if not isinstance(tid,str) or safe_name(tid)!=tid:
            raise BindingError('invalid task ID')
        # A command may only bind a task actually sent by this node.
        full = self.relay.transport.get(tid, history=0)
        if full.get('role') != 'requester' or full.get('id') != tid:
            raise BindingError('only this node\'s outbound tasks can be bound')
        if payload.get('peer') and payload['peer'] != full.get('peer'):
            raise BindingError('task peer does not match submission')
        rec = self.relay.track(tid, client=source['client'], session=source['session'], cwd=source['cwd'],
                               peer=full.get('peer'), source='requester_daemon')
        return {k:rec.get(k) for k in ('task_id','session','cwd','client','binding')}

    def artifact_spec(self,spec):
        declared=spec.get('artifact_delivery')
        if declared is not None and declared!=ARTIFACT_DELIVERY:
            raise BindingError('unsupported artifact delivery declaration')
        worker=getattr(self.relay,'artifacts',None);ready=False
        if worker is not None:
            try:
                ready=worker.public_state().get('ready') is True and worker.node_aid==self.relay.transport.node_aid()
            except Exception:ready=False
        if declared is not None and not ready:
            raise BindingError('receipt-bound requester worker is not ready; no task was sent')
        return dict(spec,artifact_delivery=dict(ARTIFACT_DELIVERY)) if ready else spec

    def submit(self,request,payload):
        tid,attempted,intent=None,False,None
        dispatched_now=False
        payload_verification='not_checked'
        intent_path=self.relay.store.root/'submissions'/(request['request_id']+'.json')
        try:
            intent,known=submission_evidence(intent_path)
            tid,attempted=known['task_id'],known['do_not_resubmit']
            if intent is None and attempted:
                raise BindingError('prior submission record is unreadable; dispatch outcome is uncertain; do not resubmit')
            if attempted and not tid:
                raise BindingError('prior dispatch outcome is uncertain; inspect the original task instead of resending')
            no_callback=payload.get('no_callback') is True
            if no_callback:
                cwd=(payload.get('source') or {}).get('cwd')
                if not isinstance(cwd,str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
                    raise BindingError('explicit working directory required')
                source={'session':None,'client':None,'cwd':str(Path(cwd).resolve()),'callback':False}
            else:
                raw=payload.get('source') or {}
                source=source_metadata(raw.get('session'),raw.get('cwd'),raw.get('client'))
            source.update(request_id=request['request_id'],at=request['created_at'])
            peer,spec=payload.get('peer'),payload.get('spec')
            if not isinstance(peer,str) or not peer or not isinstance(spec,dict):
                raise BindingError('explicit peer and JSON task spec required')
            spec=dict(spec,source=source)
            spec_digest=fingerprint(spec)
            if intent:
                if intent.get('source')!=source or intent.get('peer')!=peer:
                    raise BindingError('submission intent does not match request')
                recorded_digest=intent.get('spec_sha256')
                if recorded_digest is not None:
                    payload_verification='verified' if recorded_digest==spec_digest else 'mismatch'
                    if payload_verification=='mismatch':
                        raise BindingError('submission intent payload differs; original task evidence retained; changed requirements were not submitted')
                else:
                    payload_verification='legacy_unverified'
                    if not tid:
                        raise BindingError('legacy prepared intent has no payload digest; original payload cannot be verified; no task sent')
                if not tid and intent.get('state')!='prepared':
                    attempted=True
                    raise BindingError('prior dispatch outcome is uncertain; inspect the original task instead of resending')
            if tid:
                prior=self.relay.store.load_tracked(tid)
                if prior and prior.get('peer')==peer and (
                        (no_callback and prior.get('callback_disabled') is True) or
                        (not no_callback and prior.get('client')==source['client'] and prior.get('session')==source['session'] and
                         prior.get('cwd')==source['cwd'] and prior.get('binding',{}).get('state')=='verified')):
                    # Idempotent recovery returns the original binding evidence. It
                    # does not claim that the source is currently loaded or idle.
                    return {'task_id':tid,'sent':True,'tracked':True,'session':prior.get('session'),'cwd':prior.get('cwd'),
                            'binding':prior.get('binding') if not no_callback else {'state':'disabled'},
                            'auto_return':not no_callback,'submission_intent':str(intent_path),'replayed':True,
                            'payload_verification':payload_verification}
            if not no_callback:
                verify_source(self.relay,payload.get('source'))
            if not intent:
                wire_spec=self.artifact_spec(spec)
                intent={'source':source,'peer':peer,'state':'prepared','at':utcnow(),'spec_sha256':spec_digest,
                        'artifact_delivery':wire_spec.get('artifact_delivery')}
                payload_verification='verified'
                private_dir(intent_path.parent)
                atomic_json(intent_path,intent)
            if not tid:
                # Pin the original caller payload independently of the local
                # negotiated capability. Recheck support only before a first
                # send; later source/capability loss cannot erase sent truth.
                if intent.get('artifact_delivery') is not None:
                    spec=self.artifact_spec(dict(spec,artifact_delivery=intent['artifact_delivery']))
                elif spec.get('artifact_delivery') is not None:
                    spec=self.artifact_spec(spec)
                intent.update(state='sending');atomic_json(intent_path,intent)
                attempted=True
                dispatched_now=True
                tid=self.relay.transport.send_new(peer,json.dumps(spec,ensure_ascii=False))
                intent.update(state='sent',task_id=tid);atomic_json(intent_path,intent)
            if no_callback:
                rec=self.relay.track(tid,peer=peer)
                rec['callback_disabled']=True;self.relay.store.save_tracked(rec)
            else:
                # Check the actual outbound task, then bind it; no caller-side socket or state writes.
                full=self.relay.transport.get(tid,history=0)
                if full.get('role')!='requester' or full.get('peer')!=peer or full.get('id')!=tid:
                    raise BindingError('submitted task identity/peer could not be verified')
                rec=self.relay.track(tid,client=source['client'],session=source['session'],cwd=source['cwd'],
                                     peer=peer,source='requester_daemon')
            if str(source.get('client','')).startswith(('codex_owned_','harness_owned_')) and 'requester_context' not in rec:
                rec['requester_context']=spec.get('goal','not_provided')
                self.relay.store.save_tracked(rec)
            result={'task_id':tid,'sent':True,'tracked':True,'session':rec.get('session'),'cwd':rec.get('cwd'),
                    'binding':rec.get('binding') if not no_callback else {'state':'disabled'},
                    'auto_return':not no_callback,'submission_intent':str(intent_path),
                    'payload_verification':payload_verification}
            intent.update(state='tracked',binding=result['binding']);atomic_json(intent_path,intent)
            return result
        except Exception as e:
            if intent is not None:
                # Preserve prior sent/uncertain/prepared facts when only a new
                # source check failed. No availability check rewrites history.
                if dispatched_now:intent['state']='binding_failed' if tid else 'dispatch_unconfirmed'
                intent.update(error='%s: %s'%(type(e).__name__,str(e)[:500]))
                try:atomic_json(intent_path,intent)
                except OSError:pass  # preserve the uncertain/sent outcome even if journaling fails
            raise BindingError(str(e),request['request_id'],{
                'task_id':tid,'sent':True if tid else None if attempted else False,'tracked':False,
                'auto_return':False,'do_not_resubmit':bool(tid or attempted),
                'submission_intent':str(intent_path) if intent is not None or attempted else None,
                'payload_verification':payload_verification}) from e

    def tick(self):
        self.process()
        if self.project_queue:
            return
        if time.monotonic()-self.last_scan>=5:
            self.last_scan=time.monotonic()
            for adapter in self.relay.adapters.values():
                if not hasattr(adapter,'loaded_sources'):
                    continue
                try:
                    sources=adapter.loaded_sources()
                except (OSError,RuntimeError,ValueError,EOFError):
                    continue
                for source in sources:
                    cwd=source.get('cwd')
                    if not isinstance(cwd,str) or not Path(cwd).is_absolute():continue
                    path=workspace_queue(self.node_root,cwd)
                    # Discover queues only in loaded source workspaces, never from remote text.
                    if path.is_dir() and not path.is_symlink() and not path.parent.is_symlink():
                        if path not in self.project_queues and len(self.project_queues)<100:
                            try:self.project_queues[path]=BindingService(self.node_root,self.relay,project_queue=path)
                            except BindingError:continue
        for service in list(self.project_queues.values()):
            service.process()

    def process(self):
        processed=0
        for path in sorted(self.inbox.glob('*.json')):
            if processed>=32:break
            if path.is_symlink() or path.stat().st_size>LIMIT:
                continue
            try:
                rid = str(uuid.UUID(path.stem))
                if rid != path.stem:
                    continue
                # Refuse a request-file symlink even if it changed since the listing.
                fd=os.open(str(path),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
                with os.fdopen(fd,'r',encoding='utf-8') as stream:
                    raw=stream.read(LIMIT+1)
                if len(raw)>LIMIT:continue
                request=json.loads(raw)
                if not isinstance(request,dict) or request.get('request_id')!=rid:
                    continue
                receipt_path = self.receipts/(rid+'.json')
                digest = fingerprint(request)
                prior = read_json(receipt_path)
                if prior:
                    if not self.project_queue:path.unlink(missing_ok=True)
                    continue  # durable response: never execute the same request twice
                processed+=1
                receipt = {'protocol':PROTOCOL, 'request_id':rid, 'request_sha256':digest, 'at':utcnow(),
                           'inbox_scope':'workspace' if self.project_queue else 'node'}
                try:
                    receipt.update(state='verified', result=self.handle(request))
                except Exception as e:
                    receipt.update(state='failed', error='%s: %s' % (type(e).__name__,str(e)[:500]))
                    if getattr(e,'result',None):receipt['result']=e.result
                    elif request.get('operation')=='submit_task':
                        # Expiry/schema failure can precede submit(). A recovered
                        # command may nevertheless already have dispatched work.
                        _,known=submission_evidence(self.relay.store.root/'submissions'/(rid+'.json'))
                        receipt['result']=dict(known,tracked=False,auto_return=False)
                atomic_json(receipt_path, receipt)
                if not self.project_queue:path.unlink(missing_ok=True)
            except (OSError,ValueError,TypeError):
                continue  # a malformed file cannot stop other commands


class BindingClient:
    def __init__(self, root, timeout=40):
        self.root, self.timeout = Path(root), timeout

    def call(self, operation, payload):
        status = read_json(self.root/'status.json', {}) or {}
        if (status.get('requester_binding') != PROTOCOL or not status.get('requester') or
                not 0<=time.time()-status.get('ts',0)<=90):
            raise BindingError('requester binding service is unavailable; start/update taskd before submitting')
        rid = str(uuid.uuid4())
        request = {'protocol':PROTOCOL, 'request_id':rid, 'operation':operation, 'payload':payload,
                   'created_at':utcnow(), 'expires_at':time.time()+self.timeout}
        if len(json.dumps(request,ensure_ascii=False).encode())>LIMIT:
            raise BindingError('requester command exceeds size limit')
        inbox = self.root/'requester-commands/inbox'
        receipts = self.root/'requester-commands/receipts'
        if not inbox.is_dir() or not receipts.is_dir():
            raise BindingError('requester binding service directories are unavailable')
        try:
            atomic_json(inbox/(rid+'.json'), request)
        except OSError as e:
            if e.errno not in (errno.EPERM,errno.EACCES,errno.EROFS):raise
            if operation in ('register_harness_source','recover_harness_source'):
                raise BindingError('harness registration requires the owner-private node inbox') from e
            # A workspace-write sandbox may also forbid writing ~/.anet. The daemon
            # discovers this inbox from loaded thread metadata and verifies the explicit source.
            queue=private_dir(workspace_queue(self.root,payload['source']['cwd']))
            inbox=private_dir(queue/'inbox')
            atomic_json(inbox/(rid+'.json'),request)
        deadline = time.monotonic()+self.timeout
        uncertain={'sent':None,'do_not_resubmit':True,'auto_return':False} if operation=='submit_task' else None
        while time.monotonic()<deadline:
            try:receipt=read_json(receipts/(rid+'.json'))
            except (OSError,ValueError) as e:
                raise BindingError('could not read requester receipt: '+str(e),rid,uncertain) from e
            if receipt:
                if (receipt.get('protocol')!=PROTOCOL or receipt.get('request_id')!=rid or
                        receipt.get('request_sha256')!=fingerprint(request)):
                    raise BindingError('requester binding receipt does not match request',rid,uncertain)
                if receipt.get('state')!='verified':
                    raise BindingError(receipt.get('error') or 'source binding failed',rid,receipt.get('result') or uncertain)
                result = receipt['result']
                if operation in ('register_harness_source','recover_harness_source'):
                    source=payload['binding']
                    if (result.get('source_key')!=payload['source_key'] or result.get('client')!=source.get('client')
                        or result.get('session')!=source.get('session') or result.get('cwd')!=source.get('cwd')
                        or result.get('binding',{}).get('state')!='verified'):
                        raise BindingError('harness registration receipt does not match exact source',rid)
                    return result
                source = payload['source']
                disabled=operation=='submit_task' and payload.get('no_callback') is True
                if (not disabled and (result.get('session')!=source['session'] or result.get('cwd')!=source['cwd'] or
                        result.get('binding',{}).get('state')!='verified') or
                        (operation=='bind_task' and result.get('task_id')!=payload['task_id']) or
                        (operation=='submit_task' and (not isinstance(result.get('task_id'),str) or
                            not result['task_id'] or result.get('sent') is not True or result.get('tracked') is not True or
                            result.get('auto_return') is not (not disabled) or
                            (disabled and result.get('binding',{}).get('state')!='disabled')))):
                    raise BindingError('requester binding result does not match source/task',rid,uncertain)
                if inbox!=self.root/'requester-commands/inbox':
                    (inbox/(rid+'.json')).unlink(missing_ok=True)
                return result
            time.sleep(.05)
        raise BindingError('requester binding timed out; inspect receipt %s before retrying' % rid,rid,
                           {'sent':None,'do_not_resubmit':True,'auto_return':False} if operation=='submit_task' else None)

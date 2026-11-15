"""Durable desktop submission controller. Exactly one BindingService handoff.

It has no ANet transport, Relay, provider executor, cancellation, or model calls.
An uncertain service handoff is reconciled by reading its SAME receipt only.
"""
import copy
import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path
from . import private_directory as private
from .records import atomic_json,canonical,create_json,file_lock,read_json,utcnow
from .profiles import PRODUCT

SCHEMA='owned-source-submissions/1'
PROTOCOL='taskd-binding/1'
CAPABILITY='anet-owned-sources/1'
TASK=re.compile('ix_[0-9a-f]{32}')
MAX_ACTIONS=32

def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def canonical_id(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value:raise ValueError('canonical_action_uuid_required')
    return value
def source_projection(source):
    if not isinstance(source,dict) or source.get('source_purpose')!=PRODUCT or source.get('real_submission_eligible') is not True:
        raise ValueError('product_owned_source_required_synthetic_is_not_eligible')
    if source.get('binding',{}).get('state')!='verified':raise ValueError('explicit_source_binding_required')
    result={k:source.get(k) for k in ('client','session','cwd')}
    if not isinstance(result['client'],str) or not re.fullmatch('codex_owned_[0-9a-f]{32}',result['client']):raise ValueError('registered_owned_source_client_required')
    if not isinstance(result['session'],str) or not result['session'] or len(result['session'])>100:raise ValueError('source_session_required')
    if not isinstance(result['cwd'],str) or not Path(result['cwd']).is_absolute():raise ValueError('source_cwd_required')
    return result

class ExistingBindingGateway:
    """Existing local file protocol only. Missing product capability fails closed.

    No endpoint discovery, config read, identity creation, direct ANet send, or
    service start occurs. The future deployed BindingService advertises the exact
    registration capability after its reviewed adapter integration is installed.
    """
    def __init__(self,node_root,timeout=20):self.root=private.plain(node_root);self.timeout=timeout
    def available(self):
        status=read_json(self.root/'status.json',{})
        return bool(status.get('requester_binding')==PROTOCOL and status.get('requester') is True
            and status.get('requester_owned_sources')==CAPABILITY
            and status.get('maintenance') is not True
            and 0<=time.time()-status.get('ts',0)<=90)
    def lookup(self,request):
        receipt=read_json(self.root/'requester-commands/receipts'/(canonical_id(request['request_id'])+'.json'))
        if receipt is None:return None
        if (receipt.get('protocol')!=PROTOCOL or receipt.get('request_id')!=request['request_id']
            or receipt.get('request_sha256')!=fingerprint(request)):raise ValueError('binding_receipt_identity_mismatch')
        return receipt
    def _exchange(self,request):
        if not self.available():raise ValueError('owned_source_service_not_advertised_no_handoff')
        if len(canonical(request))>262144:raise ValueError('binding_request_too_large')
        inbox=private.plain(self.root/'requester-commands/inbox')
        receipts=private.plain(self.root/'requester-commands/receipts')
        if not inbox.is_dir() or not receipts.is_dir():raise ValueError('existing_binding_service_directories_missing')
        prior=self.lookup(request)
        if prior is not None:return prior
        target=inbox/(canonical_id(request['request_id'])+'.json')
        if target.exists():
            if read_json(target)!=request:raise ValueError('existing_binding_request_differs')
        else:create_json(target,request)
        deadline=time.monotonic()+self.timeout
        while time.monotonic()<deadline:
            found=self.lookup(request)
            if found is not None:return found
            time.sleep(.05)
        raise TimeoutError('binding_receipt_unconfirmed_query_same_request')
    def source_preflight(self,source):
        request={'protocol':PROTOCOL,'request_id':str(uuid.uuid4()),'operation':'verify_source',
            'created_at':utcnow(),'expires_at':time.time()+self.timeout,'payload':{'source':source}}
        receipt=self._exchange(request)
        if receipt.get('state')!='verified':raise ValueError('source_preflight_rejected')
        verified=receipt.get('result') or {}
        if source_projection(verified)!=source:raise ValueError('source_preflight_identity_mismatch')
        return verified
    def submit_prepared(self,request):return self._exchange(request)
    def register_source(self,request):
        if request.get('operation') not in ('register_owned_source','recover_owned_source'):raise ValueError('registration_request_required')
        return self._exchange(request)
    def task_status(self,task_id,source,peer):
        if not isinstance(task_id,str) or not TASK.fullmatch(task_id):raise ValueError('exact_task_id_required')
        record=read_json(self.root/'state/tracked'/(task_id+'.json'))
        if record is None:return None
        if (record.get('task_id')!=task_id or record.get('peer')!=peer or
            any(record.get(key)!=source.get(key) for key in ('client','session','cwd')) or
            record.get('binding',{}).get('state')!='verified'):
            raise ValueError('tracked_task_does_not_match_original_source')
        stages=record.get('stages') or {}
        # Read only a bounded projection of the local Relay evidence. Never read
        # the remote report as code or treat a successful send as task success.
        value={'task_id':task_id,'observed_at':utcnow(),'record_state':record.get('state'),
            'execution':'provider_reported:'+str(stages.get('execution',{}).get('state','not_verified')),
            'acceptance':stages.get('acceptance',{}).get('state','not_verified'),
            'files':stages.get('files',{}).get('state','not_verified'),
            'source_return':stages.get('session',{}).get('state','not_started'),
            'message':stages.get('message',{}).get('state','not_verified'),
            'effect_status':stages.get('message',{}).get('effect_status'),
            'receipt_verified':stages.get('message',{}).get('receipt_verified'),
            'stage_times':{k:v.get('at') for k,v in stages.items() if isinstance(v,dict)}}
        session=stages.get('session') or {};result=session.get('result') or {}
        value['result_text']=str(result.get('text',''))[:12000]
        value['return_turn_id']=(session.get('ack') or {}).get('turn_id')
        value['file_manifest']=[{k:f.get(k) for k in ('path','size','sha256')}
            for f in (stages.get('files',{}).get('files') or [])[:100]]
        return value

class ProductSubmissions:
    def __init__(self,directory,gateway):
        self.root=private.plain(directory);self.gateway=gateway;self.guard=threading.RLock()
        if not self.root.exists():
            private.create_new(self.root)
            with (self.root/'submissions.lock').open('xb') as stream:stream.write(b'0')
            create_json(self.root/'owner.json',{'schema':SCHEMA,'operator':private.owner_id(),
                'root':[self.root.stat().st_dev,self.root.stat().st_ino]})
            create_json(self.root/'actions.json',{'schema':SCHEMA,'actions':{}})
        self._read()
    def _read(self):
        private.verify(self.root,root=True);owner=read_json(self.root/'owner.json')
        if owner!={'schema':SCHEMA,'operator':private.owner_id(),'root':[self.root.stat().st_dev,self.root.stat().st_ino]}:
            raise ValueError('product_submission_owner_changed')
        for name in ('owner.json','actions.json','submissions.lock'):private.verify(self.root/name)
        value=read_json(self.root/'actions.json')
        if value.get('schema')!=SCHEMA or not isinstance(value.get('actions'),dict) or len(value['actions'])>MAX_ACTIONS:
            raise ValueError('product_submission_store_invalid')
        return value
    def _save(self,value):atomic_json(self.root/'actions.json',value)
    def prepare(self,action_id,*,source,peer,spec):
        canonical_id(action_id);selected=source_projection(source)
        if not isinstance(peer,str) or not peer or len(peer)>200:raise ValueError('explicit_target_aid_required')
        if not isinstance(spec,dict) or spec.get('taskd')!='submit' or spec.get('protocol')!='taskd/1':raise ValueError('explicit_taskd_submit_spec_required')
        if not isinstance(spec.get('goal'),str) or not spec['goal'].strip():raise ValueError('goal_required')
        if spec.get('orchestration','auto') not in ('auto','native','planned'):raise ValueError('orchestration_invalid')
        payload={'source':selected,'peer':peer,'spec':copy.deepcopy(spec),'no_callback':False}
        if len(canonical(payload))>200000:raise ValueError('product_request_budget')
        pin=fingerprint(payload)
        with self.guard,file_lock(self.root/'submissions.lock'):
            table=self._read();old=table['actions'].get(action_id)
            if old:
                if old['payload_sha256']!=pin:raise ValueError('action_id_reuse_with_changed_request')
                return copy.deepcopy(old)
            if len(table['actions'])>=MAX_ACTIONS:raise ValueError('product_action_capacity')
            value={'action_id':action_id,'payload_sha256':pin,'payload':payload,'source_evidence':copy.deepcopy(source),
                'state':'prepared','task_id':None,'sent':False,'ui_attached':True,'created_at':utcnow(),
                'source_return':'not_started','remote_execution':'not_verified'}
            table['actions'][action_id]=value;self._save(table);return copy.deepcopy(value)
    def _consume(self,value,receipt):
        if receipt is None:
            value.update(state='dispatch_unconfirmed',sent=None,do_not_resubmit=True);return
        if (receipt.get('protocol')!=PROTOCOL or receipt.get('request_id')!=value['request']['request_id']
            or receipt.get('request_sha256')!=fingerprint(value['request'])):raise ValueError('binding_receipt_identity_mismatch')
        result=receipt.get('result') or {};task_id=result.get('task_id')
        # Preserve a known ID even when later source tracking or verification
        # fails. No UI availability check can rewrite the earlier send fact.
        if isinstance(task_id,str) and TASK.fullmatch(task_id):
            if value.get('task_id') and value['task_id']!=task_id:raise ValueError('existing_task_id_mismatch')
            value.update(task_id=task_id,sent=True,do_not_resubmit=True)
            bound=(result.get('tracked') is True and result.get('binding',{}).get('state')=='verified'
                   and result.get('session')==value['payload']['source']['session']
                   and result.get('cwd')==value['payload']['source']['cwd'])
            value.update(state='sent_bound' if bound else 'sent_binding_pending',source_return='waiting_result' if bound else 'binding_attention')
        elif value.get('task_id'):
            value.update(state='sent_binding_pending',sent=True,do_not_resubmit=True)
        elif result.get('sent') is False and result.get('do_not_resubmit') is not True:
            value.update(state='rejected_before_send',sent=False,do_not_resubmit=False)
        else:value.update(state='dispatch_unconfirmed',sent=None,do_not_resubmit=True)
        value['binding_receipt']=copy.deepcopy(receipt);value['updated_at']=utcnow()
    def send(self,action_id):
        canonical_id(action_id)
        with self.guard,file_lock(self.root/'submissions.lock'):
            table=self._read();value=table['actions'][action_id]
            if value['state']!='prepared':return self._refresh_locked(table,value)
            if not self.gateway.available():
                value['attention']='owned_source_service_unavailable_no_handoff';self._save(table);return copy.deepcopy(value)
            # Fresh per-source preflight, not global readiness. No remote send can
            # have happened yet, so failures here remain prepared and unsent.
            fresh=self.gateway.source_preflight(value['payload']['source'])
            if source_projection(fresh)!=value['payload']['source']:raise ValueError('current_source_does_not_match_prepared_request')
            request={'protocol':PROTOCOL,'request_id':action_id,'operation':'submit_task','created_at':value['created_at'],
                'expires_at':time.time()+40,'payload':value['payload']}
            value.update(state='dispatch_requested',request=request,sent=None,do_not_resubmit=True,
                         source_evidence=copy.deepcopy(fresh));self._save(table)
            try:receipt=self.gateway.submit_prepared(copy.deepcopy(request));self._consume(value,receipt)
            except Exception:
                value.update(state='dispatch_unconfirmed',sent=True if value.get('task_id') else None,do_not_resubmit=True,
                             attention='binding_handoff_outcome_unconfirmed_query_same_request')
            self._save(table);return copy.deepcopy(value)
    def _refresh_locked(self,table,value):
        if value.get('request'):
            try:
                receipt=self.gateway.lookup(value['request'])
                if receipt is not None:self._consume(value,receipt)
            except Exception:value['attention']='same_binding_receipt_temporarily_unavailable'
        if value.get('task_id') and hasattr(self.gateway,'task_status'):
            try:
                status=self.gateway.task_status(value['task_id'],value['payload']['source'],value['payload']['peer'])
                if status is not None:
                    value['task_observation']=status
                    value['source_return']=status['source_return']
            except Exception:value['attention']='original_task_observation_temporarily_unavailable'
        self._save(table)
        return copy.deepcopy(value)
    def refresh(self,action_id):
        with self.guard,file_lock(self.root/'submissions.lock'):
            table=self._read();return self._refresh_locked(table,table['actions'][canonical_id(action_id)])
    def close_ui(self):
        with self.guard,file_lock(self.root/'submissions.lock'):
            table=self._read()
            for value in table['actions'].values():value.update(ui_attached=False,ui_closed_at=utcnow())
            self._save(table)
        return {'ui_closed':True,'remote_tasks_canceled':False,'business_replayed':False}
    def list_actions(self):
        with self.guard,file_lock(self.root/'submissions.lock'):
            return [{k:value.get(k) for k in ('action_id','task_id','state','source_return','created_at')}
                    for value in self._read()['actions'].values()]
    def view(self,action_id):
        with self.guard,file_lock(self.root/'submissions.lock'):
            value=copy.deepcopy(self._read()['actions'][canonical_id(action_id)])
        value['submit_enabled']=value['state']=='prepared' and self.gateway.available()
        value['check_original_enabled']=bool(value.get('request'))
        value['remote_execution']='not_verified'  # submission/binding is not execution
        return value

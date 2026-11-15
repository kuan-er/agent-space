"""Goal-aware facade using the canonical service's owner-input guard boundary."""
import json
import time
from datetime import datetime
from anet_harnesses.service import HarnessService
from anet_harnesses.adapters import identifier
from .controller import GoalHold,TERMINAL,GoalCancellation,identity
from anet_provider_harnesses.store import fingerprint
from .sdk_adapter import GoalAwareCodexAdapter
from .owner_policy import OwnerPolicy


def _instant(value):
    if not isinstance(value,str):raise GoalHold('goal_stop_publication_time_missing')
    try:stamp=datetime.fromisoformat(value)
    except ValueError:raise GoalHold('goal_stop_publication_time_invalid') from None
    if stamp.tzinfo is None or stamp.utcoffset() is None:raise GoalHold('goal_stop_publication_time_not_aware')
    return stamp


def _confirmed_related_ack(receipt,row,source,stamp,cutoff):
    """Only immutable ACKs preceding this stop can associate another operation."""
    try:
        return (isinstance(receipt,dict) and receipt.get('schema')=='anet-confirmed-codex-goal/1'
            and receipt.get('source_id')==source['id'] and receipt.get('goal_operation_id')==row['id']
            and row['session_id']==source['id'] and row['kind'] in ('goal_active','goal_paused')
            and row['native_id'] in (None,source['native_id'])
            and receipt.get('workspace')==source['workspace']
            and receipt.get('original_intent_digest')==row['digest']
            and receipt.get('exclusive_source_at_ack') is True
            and identity(receipt.get('goal_ack'))==stamp==receipt.get('native_goal_identity')
            and receipt['goal_ack']['status']==row['kind'].removeprefix('goal_')
            and _instant(receipt.get('ack_received_at'))<=cutoff)
    except (GoalHold,KeyError,TypeError,ValueError):return False


def publish_verified_goal_stop(ledger,controls,source_id,goal_operation_id,control_id,*,only_operation_ids=None):
    """Project an existing stop proof; no native RPC, replay or lease changes.

    Source lock and one SQLite transaction bind every row to its own immutable
    ACK. A cached stop must never terminate operations acknowledged afterwards.
    only_operation_ids narrows a reviewed metadata forward repair, not RPC input.
    """
    allowed=None if only_operation_ids is None else frozenset(only_operation_ids)
    with controls.source_lock(source_id):
        source=controls._source(source_id)
        record=controls.store.load(control_id)
        receipt=controls.store.read(controls._binding_path(source_id,goal_operation_id))
        if (not isinstance(record,dict) or record.get('schema')!='anet-codex-goal-stop/1'
            or (record.get('operation_id'),record.get('source_id'),record.get('goal_operation_id'))
               !=(control_id,source_id,goal_operation_id)
            or record.get('state')!='cancel_verified' or record.get('cancel_verified') is not True
            or record.get('original_goal_absent_verified') is not True
            or record.get('source_idle_verified') is not True or not isinstance(receipt,dict)):
            raise GoalHold('verified_goal_stop_publication_proof_required')
        stamp=record.get('native_goal_identity');proof=record.get('proof',{});preflight=record.get('preflight',{})
        if (stamp!=receipt.get('native_goal_identity') or not isinstance(stamp,dict)
            or stamp.get('threadId')!=source['native_id']
            or record.get('original_receipt_sha256')!=fingerprint(receipt)
            or proof.get('goal','missing') is not None or proof.get('runtime_status')!='idle'
            or proof.get('active_turn_ids')!=[] or preflight.get('goal') not in (None,stamp)):
            raise GoalHold('verified_goal_stop_publication_binding_differs')
        cutoff=_instant(record.get('created_at'))
        if not cutoff<=_instant(preflight.get('observed_at'))<=_instant(proof.get('observed_at')):
            raise GoalHold('verified_goal_stop_publication_order_invalid')
        target=record.get('target_turn_id');turns=proof.get('turns')
        if not isinstance(turns,list):raise GoalHold('verified_goal_stop_turn_proof_missing')
        targets=[turn for turn in turns if isinstance(turn,dict) and turn.get('id')==target]
        if target is not None:
            if (not isinstance(target,str) or not target or len(targets)!=1 or targets[0].get('status') not in TERMINAL
                or record.get('original_turn_terminal_verified') is not True or preflight.get('active_turn_ids')!=[target]):
                raise GoalHold('verified_goal_stop_original_turn_not_terminal')
        elif preflight.get('active_turn_ids')!=[]:
            raise GoalHold('verified_goal_stop_target_missing')
        result=controls.view(record)
        published=[];terminal=[];unverified=[]
        with ledger.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            bound=connection.execute('SELECT * FROM sessions WHERE id=?',(source_id,)).fetchone()
            if not bound or dict(bound)!=source:raise GoalHold('goal_source_changed_during_cancel_publication')
            current=connection.execute('SELECT * FROM operations WHERE id=?',(goal_operation_id,)).fetchone()
            # Preserve the original operation's hard checks even for a scoped
            # forward repair which only publishes a related resume operation.
            if (not current or current['session_id']!=source_id or current['kind'] not in ('goal_active','goal_paused')
                or current['digest']!=receipt.get('original_intent_digest')
                or current['native_id'] not in (None,source['native_id'])
                or not _confirmed_related_ack(receipt,current,source,stamp,cutoff)):
                raise GoalHold('original_goal_operation_changed_during_cancel_publication')
            rows=connection.execute('SELECT * FROM operations WHERE session_id=? AND kind IN (?,?)',
                                    (source_id,'goal_active','goal_paused')).fetchall()
            for row in rows:
                if allowed is not None and row['id'] not in allowed:continue
                if row['state'] in TERMINAL:terminal.append(row['id']);continue
                try:
                    ack=receipt if row['id']==goal_operation_id else controls.store.read(controls._binding_path(source_id,row['id']))
                    confirmed=_confirmed_related_ack(ack,row,source,stamp,cutoff)
                except Exception:confirmed=False
                if not confirmed:unverified.append(row['id']);continue
                prior=json.loads(row['result']) if row['result'] else {}
                if not isinstance(prior,dict):prior={}
                projection=dict(prior,goal_stop_control=result,business_result_verified=False,
                    native_evidence={'kind':'codex_goal_stop','session_id':source['native_id'],
                        'activity_id':target,'final_message_id':None,'final_assistant':False,
                        'terminal_status':'goal_absent_and_original_turn_terminal_or_no_active_turn'})
                connection.execute('UPDATE operations SET state=?,native_id=?,result=?,updated=? WHERE id=?',
                    ('interrupted',source['native_id'],json.dumps(projection,ensure_ascii=False),time.time(),row['id']))
                published.append(row['id'])
        return {'published_operation_ids':published,'preserved_terminal_operation_ids':terminal,
                'unverified_operation_ids':unverified,'native_RPCs':0}


def install_candidate_adapter(service,*,exclusive_sources=(),fault=lambda point,record:None):
    """Caller owns this NEW HarnessService; config is local owner policy.

    Invoke before creating/starting the independent Goal source. This does not
    adopt old Goal operations whose original immutable ACK receipt is missing.
    """
    original=service._adapter('codex')
    if isinstance(original,GoalAwareCodexAdapter):return original
    wrapped=GoalAwareCodexAdapter(original.rpc,service.ledger,service.root/'goal-control-r7',
        exclusive_sources=exclusive_sources,fault=fault)
    wrapped.loaded_source_ids=set(original.loaded_source_ids)
    service.adapters['codex']=wrapped
    return wrapped


def goal_control(service,action,source_id,goal_operation_id,control_id,*,user_authorized=False):
    adapter=service._adapter('codex')
    if not isinstance(adapter,GoalAwareCodexAdapter):raise GoalHold('goal_control_candidate_not_installed')
    row=service.ledger.get(goal_operation_id)
    if not row or row['session_id']!=source_id or row['kind'] not in ('goal_active','goal_paused'):
        raise GoalHold('exact_owned_goal_operation_required')
    if action=='cancel':
        result=adapter.cancel(source_id,goal_operation_id,control_id=control_id,user_authorized=user_authorized)
    elif action in ('reconcile','advance','status'):
        current=adapter.goal_controls.status(control_id)
        if not current or (current['source_id'],current['goal_operation_id'])!=(source_id,goal_operation_id):
            raise GoalHold('control_does_not_match_owned_goal')
        if action=='reconcile':result=adapter.reconcile_goal_cancel(control_id)
        elif action=='advance':result=adapter.advance_goal_cancel(control_id,user_authorized=user_authorized)
        else:result=current
    else:raise GoalHold('unsupported_goal_control_action')
    if result.get('cancel_verified'):
        publish_verified_goal_stop(service.ledger,adapter.goal_controls,source_id,goal_operation_id,control_id)
    return result


class CandidateHarnessService(HarnessService):
    """Goal-aware service. Production policy comes from owner-private bindings."""
    def __init__(self,*args,goal_exclusive_sources=(),goal_owner_policy=False,goal_fault=lambda point,record:None,**kwargs):
        self.goal_exclusive_sources=frozenset(goal_exclusive_sources)
        self.goal_fault=goal_fault
        super().__init__(*args,**kwargs)
        if goal_owner_policy:
            self.goal_exclusive_sources=OwnerPolicy(self.ledger,self.root)

    def _adapter(self,harness,surface=None):
        adapter=super()._adapter(harness,surface=surface)
        if harness!='codex' or isinstance(adapter,GoalAwareCodexAdapter):return adapter
        with self.lock:
            existing=self.adapters.get(harness)
            if isinstance(existing,GoalAwareCodexAdapter):return existing
            wrapped=GoalAwareCodexAdapter(adapter.rpc,self.ledger,self.root/'goal-control-r7',
                exclusive_sources=self.goal_exclusive_sources,fault=self.goal_fault)
            wrapped.loaded_source_ids=set(adapter.loaded_source_ids)
            self.adapters[harness]=wrapped
            return wrapped

    def control(self,action,source_id,operation_id,user_authorized=False,text=None,control_id=None):
        row=self.ledger.get(operation_id)
        if action=='cancel' and row and row['kind'] in ('goal_active','goal_paused'):
            if not control_id:raise GoalHold('goal_cancel_requires_independent_control_uuid')
            return goal_control(self,'cancel',source_id,operation_id,control_id,user_authorized=user_authorized)
        return super().control(action,source_id,operation_id,user_authorized=user_authorized,text=text,control_id=control_id)

    def _goal_handle(self,request):
        actions={'goal_cancel':'cancel','goal_cancel_reconcile':'reconcile','goal_cancel_advance':'advance','goal_cancel_status':'status'}
        action=request.get('action') if isinstance(request,dict) else None
        if action=='goal_cancel_capabilities':
            if set(request)-{'id','action','source_id','operation_id'}:raise GoalHold('unknown_goal_control_request_fields')
            source=request.get('source_id');operation=request.get('operation_id')
            # Capability reads must not start a native client. The controller's
            # capabilities path reads only local policy, ledger and ACK metadata.
            controls=GoalCancellation(self.ledger,None,self.root/'goal-control-r7',exclusive_sources=self.goal_exclusive_sources)
            try:
                result=controls.capabilities(source,operation)
                if isinstance(self.goal_exclusive_sources,OwnerPolicy):
                    result['owner_policy']=self.goal_exclusive_sources.status(source)
            except GoalHold as error:
                result={'schema':'anet-goal-stop-capability/1','cooperative_cancel_candidate_available':False,
                    'arbitrary_native_goal_cancel_available':False,'backend_cas_available':False,'reason':str(error)}
            return True,result
        if action in actions:
            if set(request)-{'id','action','source_id','operation_id','control_id','user_authorized'}:
                raise GoalHold('unknown_goal_control_request_fields')
            if type(request.get('user_authorized',False)) is not bool:raise GoalHold('user_authorized_must_be_boolean')
            return True,goal_control(self,actions[action],identifier(request.get('source_id')),
                identifier(request.get('operation_id')),identifier(request.get('control_id')),
                user_authorized=request.get('user_authorized',False))
        return False,None

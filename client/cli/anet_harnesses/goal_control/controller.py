"""At-most-one local dispatch per clear/interrupt stage, never backend CAS.

The public 0.166 protocol has no expected Goal identity on clear. This adapter
therefore defaults to HOLD and requires an owner-configured exclusive SDK source.
That cooperative contract does not exclude an uncooperative native UI client.
"""
from contextlib import contextmanager
import datetime
import hashlib
import json
from pathlib import Path
import uuid

from anet_provider_harnesses.store import ProviderStore, fingerprint
from anet_harnesses.network import private_folder, exclusive, plain, NetworkError

TERMINAL={'completed','failed','interrupted'}
GOAL_STATUSES={'active','paused','blocked','usageLimited','budgetLimited','complete'}


class GoalHold(RuntimeError): pass


def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def identity(goal):
    if (not isinstance(goal,dict) or not isinstance(goal.get('threadId'),str)
        or type(goal.get('createdAt')) is not int or goal['createdAt']<0
        or not isinstance(goal.get('objective'),str) or not goal['objective'].strip()
        or goal.get('status') not in GOAL_STATUSES):
        raise GoalHold('native_goal_identity_invalid')
    return {key:goal[key] for key in ('threadId','createdAt','objective')}


class GoalCancellation:
    def __init__(self,ledger,rpc,state_root,*,exclusive_sources=(),fault=lambda point,record:None):
        self.ledger,self.rpc=ledger,rpc
        self.store=ProviderStore(state_root)
        self.locks=self.store.root/'source-locks'; private_folder(self.locks)
        # OwnerPolicy resolves the exact private source/native binding freshly.
        # Explicit constructor lists remain available to isolated integrations.
        self.exclusive_sources=exclusive_sources if hasattr(exclusive_sources,'approved') else frozenset(exclusive_sources)
        self.fault=fault

    @staticmethod
    def _id(value):
        if not isinstance(value,str) or not value or len(value)>512: raise GoalHold('source_or_operation_id_invalid')
        return value

    def _source(self,source_id):
        source=self.ledger.session(self._id(source_id),'codex')
        if source['transport']!='app-server/stdio': raise GoalHold('source_transport_not_exclusive_stdio')
        return source

    def _binding_path(self,source_id,goal_op):
        return self.store.root/('goal-ack-'+fingerprint([source_id,goal_op])+'.json')

    def _lease_path(self,source_id):
        return self.store.root/('goal-stop-source-'+fingerprint(source_id)+'.json')

    @contextmanager
    def source_lock(self,source_id):
        folder=self.locks/fingerprint(self._id(source_id)); private_folder(folder)
        try:
            with exclusive(folder): yield
        except NetworkError as error:
            raise GoalHold('source_lock_busy_or_unverified') from None

    def capabilities(self,source_id=None,goal_operation_id=None):
        result={'schema':'anet-goal-stop-capability/1','cooperative_cancel_candidate_available':False,
            'arbitrary_native_goal_cancel_available':False,'backend_cas_available':False,
            'source_lock_scope':'participating SDK clients only','native_identity_scope':'threadId+createdAt(seconds)+objective',
            'separate_user_control_uuid_required':True,'unknown_ack_mutation_retry':False,'reconcile_read_only':True,
            'reason':'owner_exclusive_source_policy_not_granted'}
        if source_id not in self.exclusive_sources:return result
        if not goal_operation_id:
            result['reason']='confirmed_original_goal_operation_required';return result
        try:self._receipt(source_id,goal_operation_id)
        except GoalHold as error:result['reason']=str(error);return result
        result.update(cooperative_cancel_candidate_available=True,reason='fresh_native_preflight_still_required')
        return result

    def assert_mutation_allowed_locked(self,source_id):
        if self.store.read(self._lease_path(source_id)):
            raise GoalHold('source_has_unresolved_goal_stop')

    def record_goal_ack_locked(self,source_id,goal_op,params,goal):
        """Called only from the real goal/set ACK handler, before dispatch returns.

        The parent Ledger already durably reserved the exact request digest.
        A lost native ACK has no receipt and cannot be reconstructed from text.
        """
        source=self._source(source_id); row=self.ledger.get(self._id(goal_op))
        expected=fingerprint(params)
        if (not row or row['session_id']!=source_id or row['kind']!='goal_'+params.get('status','')
            or row['state']!='dispatching' or row['digest']!=expected or params.get('origin')!='user'
            or params.get('threadId')!=source['native_id']):
            raise GoalHold('original_goal_dispatch_intent_differs')
        stamp=identity(goal)
        if stamp['threadId']!=source['native_id'] or goal['status']!=params['status']:
            raise GoalHold('original_goal_ack_differs')
        if params.get('objective') is not None and stamp['objective']!=params['objective']:
            raise GoalHold('original_goal_objective_ack_differs')
        receipt={'schema':'anet-confirmed-codex-goal/1','source_id':source_id,'goal_operation_id':goal_op,
            'native_goal_identity':stamp,'original_intent_digest':row['digest'],
            'goal_ack':goal,'ack_received_at':now(),'workspace':source['workspace'],
            'exclusive_source_at_ack':source_id in self.exclusive_sources}
        path=self._binding_path(source_id,goal_op)
        prior=self.store.read(path)
        if prior and any(prior[key]!=receipt[key] for key in ('native_goal_identity','original_intent_digest','source_id','workspace')):
            raise GoalHold('immutable_original_goal_receipt_conflict')
        if not prior:self.store.write(path,receipt)
        return prior or receipt

    def _receipt(self,source_id,goal_op):
        source=self._source(source_id); receipt=self.store.read(self._binding_path(source_id,goal_op))
        row=self.ledger.get(goal_op)
        if (not receipt or not row or row['session_id']!=source_id or row['kind'] not in ('goal_active','goal_paused')
            or row['digest']!=receipt['original_intent_digest'] or receipt['workspace']!=source['workspace']
            or receipt['native_goal_identity']['threadId']!=source['native_id']
            or row.get('native_id') not in (None,source['native_id'])):
            raise GoalHold('confirmed_original_goal_receipt_missing_or_different')
        if source_id not in self.exclusive_sources or receipt.get('exclusive_source_at_ack') is not True:
            raise GoalHold('owner_exclusive_source_policy_not_granted')
        return source,receipt

    def _goal(self,thread_id):
        response=self.rpc.request('thread/goal/get',{'threadId':thread_id})
        if not isinstance(response,dict) or 'goal' not in response:raise GoalHold('native_goal_read_incomplete')
        goal=response['goal']
        if goal is not None:
            stamp=identity(goal)
            if stamp['threadId']!=thread_id:raise GoalHold('native_goal_read_wrong_thread')
        return goal

    def _thread(self,source):
        result=self.rpc.request('thread/read',{'threadId':source['native_id']})
        thread=result.get('thread') if isinstance(result,dict) else None
        if not isinstance(thread,dict) or thread.get('id')!=source['native_id']:
            raise GoalHold('native_thread_identity_differs')
        if not isinstance(thread.get('cwd'),str) or str(plain(thread['cwd']))!=source['workspace']:
            raise GoalHold('native_thread_workspace_differs')
        status=thread.get('status',{}).get('type')
        if status not in ('idle','active'):raise GoalHold('native_source_runtime_not_owned_and_loaded')
        return status

    def _turns(self,thread_id):
        values=[]; seen=set(); cursors=set(); cursor=None
        for _ in range(100):
            params={'threadId':thread_id,'itemsView':'notLoaded','limit':100,'sortDirection':'desc'}
            if cursor:params['cursor']=cursor
            page=self.rpc.request('thread/turns/list',params)
            if not isinstance(page,dict) or not isinstance(page.get('data'),list):raise GoalHold('native_turn_history_incomplete')
            for turn in page['data']:
                if (not isinstance(turn,dict) or not isinstance(turn.get('id'),str) or not turn['id']
                    or turn['id'] in seen or turn.get('status') not in TERMINAL|{'inProgress'}):
                    raise GoalHold('native_turn_history_identity_or_status_invalid')
                seen.add(turn['id']);values.append({key:turn.get(key) for key in ('id','status','startedAt','completedAt')})
            cursor=page.get('nextCursor')
            if not cursor:return values
            if not isinstance(cursor,str) or cursor in cursors:raise GoalHold('native_turn_cursor_invalid')
            cursors.add(cursor)
        raise GoalHold('native_turn_history_scan_not_exhausted')

    def _snapshot(self,source,expected):
        goal1=self._goal(source['native_id']); status1=self._thread(source)
        turns=self._turns(source['native_id'])
        goal2=self._goal(source['native_id']); status2=self._thread(source)
        stamp1=identity(goal1) if goal1 is not None else None
        stamp2=identity(goal2) if goal2 is not None else None
        if stamp1!=stamp2 or status1!=status2:raise GoalHold('native_snapshot_changed_during_read')
        if stamp2 is not None and stamp2!=expected:raise GoalHold('native_goal_was_replaced')
        active=[turn['id'] for turn in turns if turn['status']=='inProgress']
        if len(active)>1:raise GoalHold('multiple_in_progress_turns')
        if (status2=='active')!=bool(active):raise GoalHold('native_runtime_history_disagree')
        return {'goal':stamp2,'goal_status':goal2.get('status') if goal2 else None,'runtime_status':status2,
                'turns':turns,'active_turn_ids':active,'observed_at':now()}

    def _save(self,record):
        record['updated_at']=now(); self.store.save(record); return self.view(record)

    @staticmethod
    def view(record):
        return {key:record.get(key) for key in ('operation_id','source_id','goal_operation_id','state','reason_code',
            'target_turn_id','clear','interrupt','original_goal_absent_verified','original_turn_terminal_verified',
            'source_idle_verified','cancel_verified','native_goal_identity','proof','capability_boundary')}

    def status(self,control_id):
        value=self.store.load(control_id)
        return self.view(value) if value else None

    def _hold(self,record,reason):
        record.update(state='hold',reason_code=reason,cancel_verified=False)
        return self._save(record)

    def cancel(self,source_id,goal_operation_id,control_id,*,user_authorized=False):
        self.store.operation_path(control_id)
        if control_id==goal_operation_id or self.ledger.get(control_id):raise GoalHold('independent_control_uuid_required')
        with self.source_lock(source_id):
            old=self.store.load(control_id)
            if old:
                if (old['source_id'],old['goal_operation_id'])!=(source_id,goal_operation_id):raise GoalHold('control_uuid_reused_with_different_input')
                return self._reconcile_locked(old)
            record={'schema':'anet-codex-goal-stop/1','operation_id':control_id,'source_id':source_id,
                'goal_operation_id':goal_operation_id,'created_at':now(),'state':'prepared',
                'user_cancel_authorized':user_authorized is True,
                'clear':{'state':'unattempted','dispatch_count':0},'interrupt':{'state':'unattempted','dispatch_count':0},
                'target_turn_id':None,'cancel_verified':False,'original_goal_absent_verified':False,
                'original_turn_terminal_verified':False,'source_idle_verified':False,
                'capability_boundary':{'scope':'cooperative_exclusive_sdk_source','backend_cas_available':False,
                    'safe_against_uncooperative_native_ui_replacement':False,'goal_identity_globally_unique':False,
                    'verification_is_historical_at_proof_timestamp':True,
                    'turn_goal_association_basis':'exclusive_source_contract_and_exact_runtime_history; no native Goal ID on Turn',
                    'business_effect_verified':False,'detached_or_background_processes_stopped_verified':False}}
            self._save(record)
            if user_authorized is not True:return self._hold(record,'explicit_user_cancel_authority_required')
            try:
                source,receipt=self._receipt(source_id,goal_operation_id)
                record['native_goal_identity']=receipt['native_goal_identity']
                record['original_receipt_sha256']=fingerprint(receipt)
                lease=self.store.read(self._lease_path(source_id))
                if lease and lease.get('control_id')!=control_id:raise GoalHold('another_control_owns_source')
                # No unrelated SDK prompt may be adopted as a Goal's turn.
                for row in self.ledger.operations(source_id):
                    if row['id']==goal_operation_id or row['state'] in TERMINAL:continue
                    if row['kind']=='prompt':raise GoalHold('another_sdk_operation_on_source')
                    if row['kind'] in ('goal_active','goal_paused'):
                        related=self.store.read(self._binding_path(source_id,row['id']))
                        if not related or related['native_goal_identity']!=record['native_goal_identity']:
                            raise GoalHold('another_sdk_goal_identity_on_source')
                snapshot=self._snapshot(source,record['native_goal_identity'])
                record['preflight']=snapshot
                record['target_turn_id']=snapshot['active_turn_ids'][0] if snapshot['active_turn_ids'] else None
                if record['target_turn_id']:
                    turn=next(turn for turn in snapshot['turns'] if turn['id']==record['target_turn_id'])
                    if type(turn['startedAt']) is not int or turn['startedAt']<record['native_goal_identity']['createdAt']:
                        raise GoalHold('active_turn_predates_confirmed_goal_or_has_no_native_time')
                self.store.write(self._lease_path(source_id),{'control_id':control_id,'goal_operation_id':goal_operation_id})
                self._save(record)
                return self._advance_locked(record)
            except GoalHold as error:return self._hold(record,str(error))

    def advance(self,control_id,*,user_authorized=False):
        record=self.store.load(control_id)
        if not record:raise GoalHold('control_not_found')
        if user_authorized is not True:return self._hold(record,'explicit_user_cancel_authority_required')
        with self.source_lock(record['source_id']):
            record=self.store.load(control_id)
            return self._advance_locked(record)

    def reconcile(self,control_id):
        record=self.store.load(control_id)
        if not record:raise GoalHold('control_not_found')
        with self.source_lock(record['source_id']):return self._reconcile_locked(self.store.load(control_id))

    def _check_record(self,record):
        source,receipt=self._receipt(record['source_id'],record['goal_operation_id'])
        if record.get('original_receipt_sha256')!=fingerprint(receipt) or record.get('native_goal_identity')!=receipt['native_goal_identity']:
            raise GoalHold('control_original_receipt_differs_or_unavailable')
        lease=self.store.read(self._lease_path(record['source_id']))
        if not lease or lease.get('control_id')!=record['operation_id']:raise GoalHold('control_does_not_own_source')
        return source

    def _release_verified_lease(self,record):
        lease=self.store.read(self._lease_path(record['source_id']))
        if lease and lease.get('control_id')==record['operation_id']:
            self._lease_path(record['source_id']).unlink()

    def _advance_locked(self,record):
        if record.get('cancel_verified'):
            self._release_verified_lease(record)
            return self.view(record)
        try:
            source=self._check_record(record)
            snapshot=self._snapshot(source,record['native_goal_identity'])
            if record['clear']['state']=='unattempted':
                if snapshot['goal'] is None:
                    record['clear'].update(state='observed_absent',dispatch_count=0)
                else:
                    record['clear'].update(state='intent',dispatch_count=1,params={'threadId':source['native_id'],'origin':'user'})
                    self._save(record);self.fault('clear_intent_persisted',record)
                    # A fresh check detects replacements visible before send;
                    # the API cannot close a concurrent external write gap.
                    before=self._snapshot(source,record['native_goal_identity'])
                    if before['goal'] is None:
                        record['clear'].update(state='not_sent_goal_already_absent',dispatch_count=0)
                    elif before['active_turn_ids']!=snapshot['active_turn_ids']:
                        record['clear'].update(state='not_sent_turn_changed',dispatch_count=0)
                        raise GoalHold('active_turn_changed_before_clear')
                    else:
                        try:
                            ack=self.rpc.request('thread/goal/clear',record['clear']['params'])
                            if not isinstance(ack,dict) or type(ack.get('cleared')) is not bool:raise GoalHold('clear_ack_shape_invalid')
                            self.fault('clear_ack_received_before_journal',record)
                            record['clear'].update(state='acknowledged',ack=ack)
                            self._save(record)
                        except Exception as error:
                            record['clear'].update(state='unknown',error_type=type(error).__name__)
                            record.update(state='reconcile_required',reason_code='clear_ack_unknown')
                            return self._save(record)
            # Any old intent/unknown stage is reconciled, never resent.
            view=self._reconcile_locked(record)
            if record.get('cancel_verified') or not record.get('original_goal_absent_verified'):return view
            if record['interrupt']['state']!='unattempted':return view
            snapshot=self._snapshot(source,record['native_goal_identity'])
            if snapshot['goal'] is not None:return self._hold(record,'goal_present_before_interrupt')
            target=record['target_turn_id']
            if target is None:
                record['interrupt']['state']='not_required'
                return self._reconcile_locked(record)
            original=next((turn for turn in snapshot['turns'] if turn['id']==target),None)
            if original and original['status'] in TERMINAL:return self._reconcile_locked(record)
            if snapshot['active_turn_ids']!=[target]:return self._hold(record,'exact_original_turn_no_longer_the_only_active_turn')
            record['interrupt'].update(state='intent',dispatch_count=1,params={'threadId':source['native_id'],'turnId':target})
            self._save(record);self.fault('interrupt_intent_persisted',record)
            before=self._snapshot(source,record['native_goal_identity'])
            if before['goal'] is not None or before['active_turn_ids']!=[target]:
                record['interrupt'].update(state='not_sent_snapshot_changed',dispatch_count=0)
                return self._hold(record,'native_snapshot_changed_before_interrupt')
            try:
                ack=self.rpc.request('turn/interrupt',record['interrupt']['params'])
                if not isinstance(ack,dict):raise GoalHold('interrupt_ack_shape_invalid')
                self.fault('interrupt_ack_received_before_journal',record)
                record['interrupt'].update(state='acknowledged',ack=ack)
                self._save(record)
            except Exception as error:
                record['interrupt'].update(state='unknown',error_type=type(error).__name__)
                record.update(state='reconcile_required',reason_code='interrupt_ack_unknown')
                return self._save(record)
            return self._reconcile_locked(record)
        except GoalHold as error:return self._hold(record,str(error))

    def _reconcile_locked(self,record):
        if record.get('cancel_verified'):
            self._release_verified_lease(record)
            return self.view(record)
        try:
            source=self._check_record(record)
            snapshot=self._snapshot(source,record['native_goal_identity'])
            record['proof']=snapshot
            record['original_goal_absent_verified']=snapshot['goal'] is None
            if snapshot['goal'] is not None:
                record.update(state='reconcile_required',reason_code='original_goal_still_present_no_repeat_clear')
                return self._save(record)
            record['clear']['state']='observed_absent'
            target=record['target_turn_id']
            original=next((turn for turn in snapshot['turns'] if turn['id']==target),None) if target else None
            record['original_turn_terminal_verified']=bool(original and original['status'] in TERMINAL) if target else None
            record['source_idle_verified']=snapshot['runtime_status']=='idle' and not snapshot['active_turn_ids']
            done=(record['original_turn_terminal_verified'] is True or target is None) and record['source_idle_verified']
            if done:
                record['interrupt']['state']='observed_terminal' if target else 'not_required'
                record.update(state='cancel_verified',cancel_verified=True)
                record.pop('reason_code',None)
                self._save(record)
                self.fault('verified_before_source_release',record)
                self._release_verified_lease(record)
                return self.view(record)
            record.update(state='ready_for_interrupt' if record['interrupt']['state']=='unattempted' else 'reconcile_required',
                          reason_code='original_turn_or_source_not_terminal')
            return self._save(record)
        except GoalHold as error:return self._hold(record,str(error))

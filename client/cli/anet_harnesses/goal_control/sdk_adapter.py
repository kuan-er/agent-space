"""Codex adapter preserving ordinary turns and durable, owner-bound Goal control."""
from anet_harnesses.adapters import CodexAdapter
from anet_harnesses.ledger import OwnershipError
from .controller import GoalCancellation, GoalHold, identity


class GoalAwareCodexAdapter(CodexAdapter):
    def __init__(self,rpc,ledger,control_root,*,exclusive_sources=(),fault=lambda point,record:None):
        super().__init__(rpc,ledger)
        self.goal_controls=GoalCancellation(ledger,rpc,control_root,exclusive_sources=exclusive_sources,fault=fault)

    def goal(self,local_id,op,objective=None,status='active',user_authorized=False):
        if user_authorized is not True:raise OwnershipError('explicit user goal authority required')
        if status not in ('active','paused'):raise ValueError('only explicit active/paused Goal mutations supported')
        source=self.ledger.session(local_id,self.id)
        params={'threadId':source['native_id'],'status':status,'origin':'user'}
        if objective is not None:params['objective']=objective
        with self.goal_controls.source_lock(local_id):
            self.goal_controls.assert_mutation_allowed_locked(local_id)
            # A malformed/replaced owner policy fails before any native mutation.
            local_id in self.goal_controls.exclusive_sources
            def send():
                response=self.rpc.request('thread/goal/set',params)
                goal=response.get('goal') if isinstance(response,dict) else None
                stamp=identity(goal)
                if stamp['threadId']!=source['native_id'] or goal['status']!=status:raise GoalHold('goal_mutation_ack_differs')
                self.goal_controls.record_goal_ack_locked(local_id,op,params,goal)
                self.goal_controls.fault('goal_ack_saved_before_execution_commit',{'source_id':local_id,'goal_operation_id':op})
                return {'native_id':source['native_id'],'result':{'control':'goal_'+status,'goal':goal}}
            return self.ledger.dispatch(op,local_id,'goal_'+status,params,send)

    def submit(self,local_id,op,text):
        with self.goal_controls.source_lock(local_id):
            self.goal_controls.assert_mutation_allowed_locked(local_id)
            return super().submit(local_id,op,text)

    def steer(self,local_id,op,control_id,text):
        with self.goal_controls.source_lock(local_id):
            self.goal_controls.assert_mutation_allowed_locked(local_id)
            return super().steer(local_id,op,control_id,text)

    def restore(self,local_id):
        with self.goal_controls.source_lock(local_id):
            self.goal_controls.assert_mutation_allowed_locked(local_id)
            return super().restore(local_id)

    def cancel(self,local_id,op,*,control_id=None,user_authorized=False):
        row=self.ledger.get(op)
        if not row or row['session_id']!=local_id:raise OwnershipError('cancel requires exact owned operation')
        if row['kind'] in ('goal_active','goal_paused'):
            if not control_id:raise GoalHold('goal_cancel_requires_independent_control_uuid')
            return self.goal_controls.cancel(local_id,op,control_id,user_authorized=user_authorized)
        if row['kind']!='prompt':raise GoalHold('cancel_operation_kind_unsupported')
        with self.goal_controls.source_lock(local_id):
            self.goal_controls.assert_mutation_allowed_locked(local_id)
            return super().cancel(local_id,op)

    def reconcile_goal_cancel(self,control_id):
        return self.goal_controls.reconcile(control_id)

    def advance_goal_cancel(self,control_id,*,user_authorized=False):
        return self.goal_controls.advance(control_id,user_authorized=user_authorized)

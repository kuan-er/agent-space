"""Local PC WorkBuddy observations; no invented native session or terminal."""
import threading
import uuid
from .adapters import UnsupportedCapability
from .ledger import OwnershipError,ConflictingOperation
from anet_provider_harnesses.workbuddy_local import WorkBuddyLocalRunner,LocalHTTP,shared_local_state_root

LOCAL_TRANSPORT='workbuddy-local-assistant/1'


def runtime_id(source_id,operation_id):
    try:
        if str(uuid.UUID(operation_id))==operation_id:return operation_id
    except (ValueError,TypeError,AttributeError):pass
    return str(uuid.uuid5(uuid.NAMESPACE_URL,'anet-local:'+source_id+':'+operation_id))


class LocalNotOnline(RuntimeError):pass


class WorkBuddyLocalAdapter:
    id='workbuddy'
    def __init__(self,ledger,node_root,token_supplier,runner=None,timeout=30):
        self.ledger=ledger;self.timeout=timeout;self.cancels={};self.lock=threading.RLock()
        self.runner=runner or WorkBuddyLocalRunner(shared_local_state_root(node_root),LocalHTTP(token_supplier))

    def _source(self,source_id):
        source=self.ledger.session(source_id,'workbuddy')
        if source['transport']!=LOCAL_TRANSPORT:raise OwnershipError('source is not a local WorkBuddy context')
        return source

    def prepare_submit(self,source_id,operation_id,text):
        source=self._source(source_id)
        # Only reads/authentication before the service records mutation intent.
        if not self.runner.readiness()['online']:raise LocalNotOnline('WorkBuddy PC local assistant is offline')
        native_op=runtime_id(source_id,operation_id)
        def send():
            cancel=threading.Event()
            with self.lock:self.cancels[operation_id]=cancel
            try:
                value=self.runner.run(text,work_dir=source['workspace'],timeout=self.timeout,
                                      operation_id=native_op,cancel=cancel)
                return self._receipt(value,native_op)
            finally:
                with self.lock:self.cancels.pop(operation_id,None)
        def dispatch():return self.ledger.dispatch(operation_id,source_id,'workbuddy_local_prompt',{'text':text},send)
        dispatch.close=lambda:None
        return dispatch

    @staticmethod
    def _receipt(value,native_op):
        result=dict(value,text=value.get('reply') or '',runtime_operation_id=native_op,
                    local_observation_only=True,local_observation_finished=value.get('state')=='reply_observed',
                    native_terminal_verified=False,business_result_verified=False,business_effect_verified=False,
                    workspace_binding_verified=False,pc_identity_verified=False,native_session_id=None)
        return {'native_id':value.get('native_message_id'),'state':'reply_observed' if value.get('state')=='reply_observed' else 'result_unverified',
                'result':result,'local_observation_only':True}

    def status(self,source_id,operation_id,refresh=True):
        self._source(source_id);row=self.ledger.get(operation_id)
        if not row or row['session_id']!=source_id or row['kind']!='workbuddy_local_prompt':raise OwnershipError('local operation binding differs')
        native_op=runtime_id(source_id,operation_id)
        value=self.runner.status(native_op)
        if value is None:return row
        if refresh and value['state']!='reply_observed':value=self.runner.observe(native_op,timeout=3)
        receipt=self._receipt(value,native_op)
        # This is a local observation projection; never a native completed turn.
        with self.ledger.connect() as c:
            current=c.execute('SELECT * FROM operations WHERE id=?',(operation_id,)).fetchone()
            if not current or current['session_id']!=source_id or current['kind']!='workbuddy_local_prompt':raise OwnershipError('local operation changed')
            if current['native_id'] and receipt['native_id']!=current['native_id']:raise OwnershipError('native message identity changed')
            import json,time
            c.execute('UPDATE operations SET state=?,native_id=?,result=?,updated=? WHERE id=?',
                      (receipt['state'],receipt['native_id'],json.dumps(receipt['result'],ensure_ascii=False),time.time(),operation_id))
        return self.ledger.get(operation_id)

    def cancel(self,source_id,operation_id):
        self._source(source_id)
        with self.lock:cancel=self.cancels.get(operation_id)
        if cancel is None:raise UnsupportedCapability('没有可停止的本地观察；WorkBuddy 未提供原生任务取消接口。')
        cancel.set()
        return {'observation_cancel_requested':True,'native_cancel_confirmed':False,'terminal_observation_required':False}

    def goal(self,*args,**kwargs):raise UnsupportedCapability('WorkBuddy 本地 API 未提供独立原生 Goal、暂停或恢复接口。')

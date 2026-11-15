"""Windows form contract. Calls only the supplied durable BindingService gateway.

Construction/hello/close never starts a source, model, task, service or process.
The source receipt is an explicit registration result; send rechecks it live.
"""
import copy
import uuid
from .profiles import PRODUCT
from .submissions import source_projection

PROTOCOL='owned-source-form/1'
MAX_GOAL_BYTES=32768

def form_spec(fields):
    if not isinstance(fields,dict) or set(fields)!={'peer','goal','acceptance','orchestration'}:
        raise ValueError('explicit_form_fields_required')
    peer=fields['peer'];goal=fields['goal'];checks=fields['acceptance']
    if not isinstance(peer,str) or not peer.strip() or len(peer)>200:raise ValueError('target_aid_required')
    if not isinstance(goal,str) or not goal.strip() or len(goal.encode('utf-8'))>MAX_GOAL_BYTES:
        raise ValueError('goal_required_within_32_KiB')
    if not isinstance(checks,list) or not checks or any(not isinstance(v,dict) or not v for v in checks):
        raise ValueError('explicit_acceptance_checks_required')
    if fields['orchestration'] not in ('native','planned','auto'):raise ValueError('explicit_orchestration_required')
    return peer,{'taskd':'submit','protocol':'taskd/1','goal':goal,
                 'acceptance':copy.deepcopy(checks),'orchestration':fields['orchestration']}

def presentation(value,*,source=None,service_available=False):
    state=(value or {}).get('state','draft');known=(value or {}).get('task_id')
    observation=(value or {}).get('task_observation') or {}
    labels={'draft':'尚未创建提交意图','prepared':'已准备；尚未交给发送服务',
        'dispatch_requested':'已交给本机服务；发送结果待核对',
        'dispatch_unconfirmed':'发送结果未知；只核对原请求，不重复发送',
        'sent_bound':'已有任务 ID；来源绑定已验证，对端执行尚未核验',
        'sent_binding_pending':'已有任务 ID；来源回传绑定需要处理',
        'rejected_before_send':'发送前明确拒绝；原请求保留'}
    eligible=bool(source and source.get('source_purpose')==PRODUCT and
                  source.get('real_submission_eligible') is True and source.get('binding',{}).get('state')=='verified')
    return {'protocol':PROTOCOL,'state':state,'state_text':labels.get(state,'状态未识别；保留原记录'),
        'task_id':known,'action_id':(value or {}).get('action_id'),
        'source_session':(source or {}).get('session'),'source_client':(source or {}).get('client'),
        'source_purpose':(source or {}).get('source_purpose','not_registered'),
        'source_binding':'verified_at_registration' if eligible else 'not_verified',
        'service_available':bool(service_available),
        'prepare_enabled':state=='draft' and eligible and service_available,
        'submit_enabled':state=='prepared' and eligible and service_available,
        'refresh_enabled':bool((value or {}).get('request')),
        'edit_enabled':state=='draft','close_enabled':True,
        'execution':observation.get('execution','not_verified'),
        'acceptance':observation.get('acceptance','not_verified'),'files':observation.get('files','not_verified'),
        'message':observation.get('message','not_verified'),'observed_at':observation.get('observed_at'),
        'effect_status':observation.get('effect_status'),'receipt_verified':observation.get('receipt_verified'),
        'result_text':observation.get('result_text',''),'return_turn_id':observation.get('return_turn_id'),
        'file_manifest':observation.get('file_manifest',[]),
        'source_return':(value or {}).get('source_return','not_started'),
        'reason':(value or {}).get('attention') or ('not_provided' if service_available else 'owned_source_service_not_advertised'),
        'notice':'关闭此表单不取消远端任务；来源宿主退出后自动回传会等待。已有任务 ID 永久保留。'}

class ProductController:
    def __init__(self,submissions,source):
        self.submissions=submissions;self.source=copy.deepcopy(source);self.action_id=None;self.closed=False
        source_projection(self.source)
    def view(self):
        value=self.submissions.view(self.action_id) if self.action_id else None
        return presentation(value,source=self.source,service_available=self.submissions.gateway.available())
    def handle(self,command):
        if not isinstance(command,dict) or command.get('protocol')!=PROTOCOL:raise ValueError('form_protocol_required')
        op=command.get('operation')
        if op=='close':
            self.closed=True;return dict(self.submissions.close_ui(),protocol=PROTOCOL)
        if self.closed:raise ValueError('form_already_closed')
        if op=='hello':return self.view()
        if op=='prepare':
            action_id=command.get('action_id')
            if not isinstance(action_id,str) or str(uuid.UUID(action_id))!=action_id:raise ValueError('explicit_action_uuid_required')
            if self.action_id and self.action_id!=action_id:raise ValueError('one_intent_per_form_close_to_open_another')
            if not self.submissions.gateway.available():raise ValueError('owned_source_service_not_advertised')
            peer,spec=form_spec(command.get('fields'))
            self.submissions.prepare(action_id,source=self.source,peer=peer,spec=spec);self.action_id=action_id
        elif op in ('submit','refresh','open_original'):
            action_id=command.get('action_id')
            if op=='open_original':
                # Explicitly reopen a preserved intent. This path is read-only.
                value=self.submissions.view(action_id)
                if value['payload']['source']!=source_projection(self.source):raise ValueError('original_intent_has_different_source')
                self.action_id=action_id
            elif not self.action_id or action_id!=self.action_id:raise ValueError('exact_prepared_action_required')
            elif op=='submit':self.submissions.send(action_id)
            else:self.submissions.refresh(action_id)
        else:raise ValueError('unsupported_form_operation')
        return self.view()

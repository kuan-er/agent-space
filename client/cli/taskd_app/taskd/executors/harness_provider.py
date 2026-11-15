"""Opt-in candidate provider executors. Import alone never enables a provider."""
import json
import time
import uuid

from .base import Executor, ExecResult
from anet_provider_harnesses.workbuddy_local import WorkBuddyLocalRunner, LocalHTTP, shared_local_state_root
from anet_provider_harnesses.sdk_provider import SDKHarnessRunner


class WorkBuddyLocalExecutor(Executor):
    name = 'workbuddy_local'
    caps = {'resume': False, 'assign_session': False, 'schema': False, 'images': False}
    durable_step_required = True

    def __init__(self, runner, *, allow_shared_local_assistant=False):
        if allow_shared_local_assistant is not True:
            raise ValueError('explicit owner opt-in to shared local-assistant execution required')
        self.runner = runner

    def native_capability(self):
        return {'available': False, 'reason': 'local_message_api_has_no_verified_native_goals',
                'orchestration': 'planned', 'surface': 'workbuddy_local_assistant',
                'native_terminal_available': False, 'native_cancel_available': False}

    def describe(self):
        result = super().describe()
        result.update(surface='workbuddy_local_assistant', native_goal=False,
                      native_terminal_available=False, native_cancel_available=False,
                      workspace_binding_verified=False, enforced_readonly_review=False,
                      execution_success='correlated_reply_observed; taskd_acceptance_still_required')
        return result

    def run(self, prompt, *, work_dir, timeout, schema=None, session=None, mode='task', images=(), cancel=None, operation_id=None):
        start = time.monotonic()
        if operation_id is None:
            return ExecResult(False,error='durable_taskd_step_identity_required')
        # Executor.review promises read-only enforcement. The local OpenAPI has
        # no permission-setting interface, so this adapter cannot make that promise.
        if mode != 'task' or session is not None or images:
            return ExecResult(False, error='unsupported_local_execution_capability', duration=time.monotonic()-start)
        if schema is not None:
            prompt += '\n\nReturn only JSON matching this schema:\n' + json.dumps(schema, ensure_ascii=False)
        try:
            evidence = self.runner.run(prompt, work_dir=work_dir, timeout=timeout, mode=mode, cancel=cancel,operation_id=operation_id)
        except Exception as error:
            # Native credentials/error bodies are never copied into task logs.
            result = ExecResult(False, error='workbuddy_local_' + type(error).__name__, duration=time.monotonic()-start)
            result.native_terminal_verified = False
            result.business_effect_verified = False
            return result
        observed = evidence.get('state') == 'reply_observed' and bool(evidence.get('reply'))
        result = ExecResult(observed, text=evidence.get('reply') or '', session_id=None,
                            error=None if observed else 'workbuddy_local_' + str(evidence.get('state', 'unverified')),
                            duration=time.monotonic()-start, timed_out=bool(evidence.get('timed_out')),
                            canceled=bool(evidence.get('canceled')))
        result.harness_evidence = evidence
        result.native_terminal_verified = False
        result.business_effect_verified = False
        result.native_cancel_confirmed = False
        return result

    def selftest(self, work_dir):
        # This is an explicit real model message, not an online flag relabeled as
        # execution. Callers may instead use runner.readiness() without a model.
        result = self.run('Do not use tools or change anything. Reply with exactly {"ok":true}.',
                         work_dir=work_dir, timeout=60,operation_id=str(uuid.uuid4()))
        return _selftest_result(result,'correlated_reply_observed; native_terminal_unverified; business_unverified')

    def observe(self,operation_id,*,work_dir,timeout):
        # The operation journal, not caller-supplied text, selects the original
        # workspace and prompt. Check the engine's owner workspace separately.
        original = self.runner.store.load(operation_id)
        from anet_harnesses.network import plain
        if original['input']['work_dir'] != str(plain(work_dir)):
            raise ValueError('provider observation workspace differs')
        evidence = self.runner.observe(operation_id,timeout=timeout)
        return _observed_result(evidence,local=True)


def build_workbuddy_local(config, *, token_supplier=None):
    allowed = {'type', 'node_root', 'allow_shared_local_assistant'}
    if set(config) - allowed or config.get('type') != 'workbuddy_local':
        raise ValueError('invalid fixed WorkBuddy local provider configuration')
    if token_supplier is None:
        from anet_harnesses.workbuddy_auth import PortalWorkBuddyTokenBroker
        token_supplier = PortalWorkBuddyTokenBroker(config['node_root'], surface='local')
    runner = WorkBuddyLocalRunner(shared_local_state_root(config['node_root']), LocalHTTP(token_supplier))
    return WorkBuddyLocalExecutor(runner, allow_shared_local_assistant=config.get('allow_shared_local_assistant'))


class TaskdHarnessExecutor(Executor):
    """Dedicated source per workspace; SDK native evidence, planned taskd loop."""
    name = 'harness_sdk'
    caps = {'resume':False,'assign_session':False,'schema':False,'images':False}
    durable_step_required = True
    durable_quick_supported = True
    owner_input_supported = True

    def __init__(self, runner):
        self.runner = runner

    def native_capability(self):
        return {'available':False,'reason':'provider_uses_planned_orchestration','orchestration':'planned',
                'harness':self.runner.harness}

    def describe(self):
        return dict(super().describe(),harness=self.runner.harness,native_goal=False,
                    source_scope='dedicated_native_source_per_workspace',enforced_readonly_review=False,
                    durable_quick=True, native_terminal_available=True)

    def run(self,prompt,*,work_dir,timeout,schema=None,session=None,mode='task',images=(),cancel=None,operation_id=None,owner_input=None):
        start = time.monotonic()
        if operation_id is None:
            return ExecResult(False,error='durable_taskd_step_identity_required')
        if mode != 'task' or session is not None or images:
            return ExecResult(False,error='unsupported_sdk_provider_capability')
        if schema is not None:
            prompt += '\n\nReturn only JSON matching this schema:\n' + json.dumps(schema,ensure_ascii=False)
        try:
            extra={'owner_input':owner_input} if owner_input is not None else {}
            evidence = self.runner.run(prompt,work_dir=work_dir,timeout=timeout,mode=mode,cancel=cancel,operation_id=operation_id,**extra)
        except Exception as error:
            return ExecResult(False,error='harness_sdk_'+type(error).__name__,duration=time.monotonic()-start)
        ok = evidence.get('state') == 'completed' and evidence.get('native_terminal_verified') is True
        result = ExecResult(ok,text=evidence.get('reply') or '',session_id=None,
                            error=None if ok else 'harness_sdk_'+evidence.get('state','unverified'),
                            duration=time.monotonic()-start,timed_out=evidence.get('timed_out',False),
                            canceled=evidence.get('canceled',False))
        result.harness_evidence = evidence
        result.native_terminal_verified = evidence.get('native_terminal_verified',False)
        result.native_cancel_confirmed = evidence.get('native_cancel_confirmed',False)
        result.business_effect_verified = False
        return result

    def selftest(self,work_dir):
        result = self.run('Do not use tools. Reply with exactly {"ok":true}.',work_dir=work_dir,timeout=60,operation_id=str(uuid.uuid4()))
        return _selftest_result(result,'native_final_observed; business_unverified')

    def observe(self,operation_id,*,work_dir,timeout,owner_input=None,cancel=None):
        extra={'owner_input':owner_input} if owner_input is not None else {}
        return _observed_result(self.runner.observe(operation_id,work_dir=work_dir,timeout=timeout,cancel=cancel,**extra),local=False)


def _observed_result(evidence,*,local):
    ok = (evidence.get('state') == 'reply_observed' and bool(evidence.get('reply')) if local else
          evidence.get('state') == 'completed' and evidence.get('native_terminal_verified') is True)
    result = ExecResult(ok,text=evidence.get('reply') or '',error=None if ok else 'provider_observation_'+evidence.get('state','unverified'),
                        timed_out=evidence.get('timed_out',False),canceled=evidence.get('canceled',False))
    result.harness_evidence = evidence
    result.native_terminal_verified = evidence.get('native_terminal_verified',False)
    result.native_cancel_confirmed = evidence.get('native_cancel_confirmed',False)
    result.business_effect_verified = False
    return result


def _selftest_result(result,success_detail):
    try:
        value = json.loads(result.text)
        good = result.ok and isinstance(value,dict) and set(value)=={'ok'} and value['ok'] is True
    except (ValueError,TypeError):
        good = False
    return bool(good), success_detail if good else 'selftest_reply_or_execution_unverified'


def build_sdk_provider(config):
    allowed = {'type','harness','state_root','harness_config'}
    if set(config)-allowed or config.get('type') != 'harness_sdk':
        raise ValueError('invalid SDK provider configuration')
    return TaskdHarnessExecutor(SDKHarnessRunner(config['state_root'],config['harness'],config=config.get('harness_config')))

"""Small engine hook: stable durable identity before the executor can mutate."""
import uuid
from .store import fingerprint


class DurableStepConflict(RuntimeError):
    pass


def record_result(result):
    record = {key:getattr(result,key,None) for key in ('ok','text','session_id','error','duration','timed_out','canceled')}
    record['harness_evidence'] = getattr(result,'harness_evidence',None)
    return record


def restore_result(record):
    from taskd.executors.base import ExecResult
    result = ExecResult(**{key:record[key] for key in ('ok','text','session_id','error','duration','timed_out','canceled')})
    result.harness_evidence = record.get('harness_evidence') or {}
    result.native_terminal_verified = result.harness_evidence.get('native_terminal_verified',False)
    result.native_cancel_confirmed = result.harness_evidence.get('native_cancel_confirmed',False)
    return result


def reserve_step(task, *, phase_id, purpose, prompt, work_dir, schema, session):
    """Initial identity includes input digest; recovery MUST reuse its intent.

    Never recompute a new ID from changed input while an old intent exists.
    The caller atomically saves this record before invoking a provider.
    """
    identity = {'task_id':task['id'],'step_number':task['used']['steps']+1,
                'phase_id':phase_id,'purpose':purpose}
    digest = fingerprint({'prompt':prompt,'work_dir':str(work_dir),'schema':schema,
                          'session':session,'mode':'task'})
    old = task.get('provider_step_intent')
    if old:
        if old.get('identity') != identity or old.get('input_sha256') != digest:
            raise DurableStepConflict('uncheckpointed provider intent differs; inspect original operation')
        return old['operation_id']
    operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL,'anet-taskd-provider-intent/1:' + fingerprint(identity)+':'+digest))
    task['provider_step_intent'] = {'schema':'anet-taskd-provider-intent/1','operation_id':operation_id,
                                  'identity':identity,'input_sha256':digest,'state':'reserved'}
    return operation_id


def step_operation_id(task_id, step_number):
    if not isinstance(task_id,str) or not task_id or len(task_id) > 512:
        raise ValueError('durable task identity required')
    if type(step_number) is not int or step_number < 1:
        raise ValueError('durable positive step number required')
    return str(uuid.uuid5(uuid.NAMESPACE_URL,'anet-taskd-provider-step/1:' + task_id + ':' + str(step_number)))


def invoke_executor(executor, *, task_id, step_number, **kwargs):
    """Use t.used.steps+1; recovery must not advance it before checkpoint."""
    if getattr(executor,'durable_step_required',False):
        kwargs['operation_id'] = step_operation_id(task_id,step_number)
    return executor.run(**kwargs)

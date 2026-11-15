"""Taskd's native-goal control path: leases, observable state and independent acceptance."""
from __future__ import annotations
import json
import time
from . import lightweight,prompts,instructions,facts
from .activity import execution
from .protocol import Rejected
from .resources import DESKTOP_CHECKS,desktop_enabled
from .store import utcnow


def observed_error(data, turn_id):
    """Safe summary for the owner portal, not a raw upstream body/header dump."""
    error = data.get('error')
    if not isinstance(error, dict):return None
    message = error.get('message')
    detail = message
    if isinstance(message, str):
        try:
            decoded = json.loads(message)
            if isinstance(decoded, dict):detail = decoded.get('detail')
        except ValueError:pass
    known = {'Bad Request', 'Unauthorized', 'Forbidden', 'Too Many Requests',
             'Internal Server Error', 'Service Unavailable'}
    summary = detail if isinstance(detail, str) and detail in known else 'Native executor request failed; inspect local diagnostics'
    result = {'message': summary, 'turn_id': turn_id, 'observed_at': utcnow()}
    if type(data.get('willRetry')) is bool:result['will_retry'] = data['willRetry']
    return result


def is_native(task):return task['spec'].get('orchestration')=='native'


def select(spec,executor,config):
    if spec['orchestration']!='planned':return
    preference=spec.get('execution_preference','auto')
    if preference=='planned':
        spec['routing_reason']='explicit_planned';return
    cap=executor.native_capability() if hasattr(executor,'native_capability') else {'available':False,'reason':'adapter_has_no_verified_native_goals'}
    if not cap.get('available'):
        if preference=='native':raise Rejected('native_unavailable',cap.get('reason','native goals unavailable'))
        spec['routing_reason']=cap.get('reason','native goals unavailable');return
    spec['orchestration']='native';spec['routing_reason']='native_capability_verified'
    if spec['resource']=='auto':
        spec['resource']='desktop' if any(c['type'] in DESKTOP_CHECKS for c in spec['acceptance']) else 'background'
    if spec['resource']=='desktop' and not desktop_enabled(config):
        raise Rejected('resource_unavailable','desktop execution is not enabled on this node')
    if spec['resource']=='background' and any(c['type'] in DESKTOP_CHECKS for c in spec['acceptance']):
        raise Rejected('invalid_spec','desktop acceptance requires resource=desktop')


def prepare(engine,task):
    if not task['spec']['acceptance']:
        task['checks'].append({'id':'goal_condition','type':'llm_review',
            'params':{'question':task['spec']['goal'],'evidence':[]},'origin':'requester','scope':'final'})
    for c in task['spec']['acceptance']:
        if c['type']=='natural':
            task['checks'].append({'id':c['id'],'type':'llm_review','params':{'question':c['text'],'evidence':[]},
                                   'origin':'requester','scope':'final'})
    lightweight.prepare(engine,task)
    task['plan']['notes']='Native goal on one durable session. No taskd planning or progress/replanning loop.'
    engine.store.save(task)


def step(engine,tid):
    with engine.store.lock(tid):
        t=engine.store.load(tid);work=engine.task_work_dir(t)
        remaining=None  # no cumulative goal deadline
        cleanup_only=t.get('native_cleanup_only',False)
        if not cleanup_only and (t.get('native_input') or {}).get('state') in instructions.UNCERTAIN:
            engine._block(t,'Native input delivery is unconfirmed; inspect the existing turn before any retry',kind='native_input_unconfirmed')
            return
        if cleanup_only:remaining=60
        else:t.setdefault('native',{}).pop('error',None)  # new invocation; prior errors remain in task log
        reconcile_only=t.get('native_reconcile_only',False)
        # Legacy max_steps must not interrupt a healthy native goal.
        remaining_turns=None
        details=(prompts.UNTRUSTED+'\n'+prompts.task_block(t['spec'],work,t['checks'],t.get('inputs_dir'))+
            '\n'+prompts.resource_rules(t['spec']['resource'])+
            '\nAcceptance is checked independently after you finish. Keep evidence in out/. '
            'Do not weaken checks, rewrite this instruction file, or expand scope. '
            'Use the native goal lifecycle; complete only with evidence and then give a short final answer.\n'+
            prompts.fence('FOLLOW_UP',json.dumps(t['inbox'],ensure_ascii=False)))
        path=work/'.taskd-native-instructions.txt'
        if not cleanup_only:path.write_text(details,encoding='utf-8')
        objective=('Complete the authorized task described in .taskd-native-instructions.txt in the current directory. '
                   'Read it first and follow its acceptance and constraints. Goal: '+t['spec']['goal'][:1600])
        if t['used']['verify_rounds']:
            objective+=' Previous acceptance failed; read the feedback in that file and fix it before completion.'
        if not cleanup_only:instructions.prepare(t)
        t['step_in_progress']={'phase':'run','kind':'native','since':utcnow()}
        engine.store.save(t)
    def checkpoint(sid):
        with engine.store.lock(tid):
            t=engine.store.load(tid)
            if not t.get('session'):t['sessions_used']+=1
            t['session']=sid
            t.setdefault('native',{}).update(thread_id=sid,harness=engine.executor.name)
            engine.store.save(t)
    def emit(method,data):
        with engine.store.lock(tid):
            t=engine.store.load(tid)
            t.setdefault('native',{}).update(last_event=method,updated_at=utcnow(),observed_at=time.time())
            if method=='thread/goal/updated':t['native']['goal_status']=(data.get('goal') or {}).get('status')
            if method=='native/goal_confirmed':t['native']['goal_status']=data.get('status')
            if method=='native/retry':t['native']['retry']=data
            if method=='native/input_acknowledged':instructions.acknowledge(t,'executor_acknowledged',data)
            if method=='native/input_observed':instructions.acknowledge(t,'observed_in_turn',data)
            if method=='turn/started':
                new_turn=t['native'].get('turn_id')!=data['turn']['id']
                t['native'].update(turn_id=data['turn']['id'],turn_status='inProgress',
                    iteration=t['native'].get('iteration',0)+int(new_turn),stop_requested=False)
                t['native'].pop('retry',None);t['native'].pop('stop',None);t['native'].pop('error',None)
            if method in ('error','turn/completed'):
                payload = data if method=='error' else data.get('turn',{})
                turn_id = data.get('turnId') if method=='error' else payload.get('id')
                if not turn_id or turn_id==t['native'].get('turn_id'):
                    failure = observed_error(payload,turn_id)
                    if failure:
                        previous = t['native'].get('error',{})
                        if previous.get('turn_id')==turn_id and 'will_retry' not in failure and 'will_retry' in previous:
                            failure['will_retry']=previous['will_retry']
                        if method=='turn/completed' and payload.get('status')=='failed':failure['will_retry']=False
                        t['native']['error']=failure
            if method=='native/stopping':
                t['native']['stop_requested']=True;t['native'].pop('stop',None)
            if method in ('item/completed','item/commandExecution/outputDelta'):
                t['native'].pop('retry',None)
            if method=='turn/completed':
                t['native']['turn_status']=data['turn']['status'];t['native'].pop('retry',None)
                t['native']['completed_turn_id']=data['turn'].get('id')
            if method=='native/stopped':
                t['native']['stop']=data
                if data.get('goal_status'):t['native']['goal_status']=data['goal_status']
            if method=='native/reconciled':t['native'].update(goal_status='complete',turn_status='completed')
            # Keep resolved errors as history, not as an active will_retry flag.
            if method in ('turn/completed','native/reconciled'):
                t['native'] = facts.native_view(t['native'])
            engine.store.log(tid,'native_event',method=method,data=data)
            engine.store.save(t)
            engine.store.activity.record(tid,'executor_output')
    def messages():
        with engine.store.lock(tid):
            t=engine.store.load(tid)
            pending=instructions.reserve(t)
            if not pending:return ''
            engine.store.log(tid,'native_steer_dispatch',receipt=t['native']['input_delivery'])
            engine.store.save(t)
            return pending
    with execution(engine.store.activity,tid,remaining,purpose='native_goal'):
        result=engine.executor.run_goal(objective,work_dir=work,session=t.get('session'),timeout=remaining,
            max_turns=remaining_turns,cancel=engine.cancel_events.get(tid),emit=emit,checkpoint=checkpoint,messages=messages,
            reconcile_complete=not t['used']['verify_rounds'] and not any(t['inbox'].values()),stop_only=cleanup_only)
    with engine.store.lock(tid):
        t=engine.store.load(tid)
        t['step_in_progress']=None
        if not cleanup_only:
            t['used']['seconds']+=result.duration
            t['used']['steps']+=max(1,getattr(result,'native_turns',1))
        else:
            engine.store.log(tid,'native_cleanup_finished',duration=result.duration,reconcile_only=reconcile_only)
        uncertain = (result.error or '').startswith(('native_outcome_unknown', 'native_stop_unconfirmed'))
        closed_transport = t.get('stop_reason') == 'ended_by_requester'
        if cleanup_only and closed_transport:
            uncertain = not result.canceled
            t['native_cleanup_only'] = False
        t['native_quarantine'] = uncertain
        if uncertain:
            engine.resources.retain(tid,t['spec']['resource'],work)
            if t['flags'].get('cancel'):
                t['native_cleanup_only']=True
                t['flags']['cancel']=False
        else:
            engine.resources.release_retained(tid)
        if closed_transport:
            t['state'] = 'stopped'
            if cleanup_only:
                t['block_reason'] = (result.error or 'native_stop_unconfirmed') if uncertain else None
                if not uncertain:
                    t['blocker'] = None
                engine.store.log(tid, 'native_stop_reconcile_finished', confirmed=not uncertain)
            engine.store.save(t)
            return
        if reconcile_only and t.get('native_reconcile_only'):
            t.pop('native_reconcile_only',None)
            t.pop('native_cleanup_only',None)
            # A cleanup ACK is necessary but never a claim of business completion.
            if result.canceled and not uncertain:
                t['native_pause']=True
                engine._block(t,'Native stop reconciled; original task preserved. Explicit resume is required.',kind='native_paused')
            else:
                t['native_quarantine']=True
                engine.resources.retain(tid,t['spec']['resource'],work)
                engine._block(t,result.error or 'native_stop_unconfirmed',kind='native_incomplete')
            return
        if result.canceled:
            if t.get('native_pause'):engine._block(t,'Native goal paused by requester',kind='native_paused')
            elif engine.stopping.is_set():engine._block(t,'Native goal paused for service shutdown; resume the same task',kind='native_paused')
            else:engine._stop(t,'canceled')
            return
        if not result.ok:
            reason=result.error or 'Native execution did not complete'
            failure=t.get('native',{}).get('error')
            if failure:reason+='; '+failure['message']
            engine._block(t,reason,kind='native_incomplete');return
        if any(t['inbox'].values()) or (t.get('native_input') or {}).get('state') in instructions.UNCERTAIN:
            engine._block(t,'Native task ended without observed follow-up input; acceptance not run',kind='native_input_unconfirmed');return
        (work/'native-final.txt').write_text(result.text,encoding='utf-8')
        artifacts=['native-final.txt']
        if (work/'out').exists():
            artifacts += [str(p.relative_to(work)) for p in sorted((work/'out').rglob('*')) if p.is_file()][:49]
        lightweight._record(engine,t,{'status':'claims_done','summary':result.text[:2000],'artifacts':artifacts,'detail':''})
        t['state']='verifying';engine.store.save(t)


def verification_failed(engine,t,results):
    # No taskd replanning: bounded feedback goes to the original native session.
    t['inbox']['feedback'].append('Independent acceptance failed: '+json.dumps(results,ensure_ascii=False)[:12000])
    if t['used']['verify_rounds']-t.get('verification_retry_base',0)>=engine.policy['max_verify_rounds']:
        engine._block(t,'Native goal claimed done but independent acceptance failed',kind='verification',results=results)
    else:
        t['state']='running';engine.store.save(t)

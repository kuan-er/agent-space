"""Taskd's native-goal control path: leases, observable state and independent acceptance."""
from __future__ import annotations
import json
import time
from . import lightweight,prompts
from .activity import execution
from .protocol import Rejected
from .resources import DESKTOP_CHECKS,desktop_enabled
from .store import utcnow


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
        budget=t['spec']['budget'];remaining=budget['max_hours']*3600-t['used']['seconds']
        cleanup_only=t.get('native_cleanup_only',False)
        if cleanup_only:remaining=60
        remaining_turns=max(1,int(budget['max_steps']-t['used']['steps']))
        details=(prompts.UNTRUSTED+'\n'+prompts.task_block(t['spec'],work,t['checks'],t.get('inputs_dir'))+
            '\n'+prompts.resource_rules(t['spec']['resource'])+
            '\nAcceptance is checked independently after you finish. Keep evidence in out/. '
            'Do not weaken checks, rewrite this instruction file, or expand scope. '
            'Use the native goal lifecycle; complete only with evidence and then give a short final answer.\n'+
            prompts.fence('FOLLOW_UP',json.dumps(t['inbox'],ensure_ascii=False)))
        path=work/'.taskd-native-instructions.txt';path.write_text(details,encoding='utf-8')
        objective=('Complete the authorized task described in .taskd-native-instructions.txt in the current directory. '
                   'Read it first and follow its acceptance and constraints. Goal: '+t['spec']['goal'][:1600])
        if t['used']['verify_rounds']:
            objective+=' Previous acceptance failed; read the feedback in that file and fix it before completion.'
        t['inbox']={'hints':[],'answers':[],'feedback':[]}
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
            if method=='native/retry':t['native']['retry']=data
            if method=='turn/started':
                new_turn=t['native'].get('turn_id')!=data['turn']['id']
                t['native'].update(turn_id=data['turn']['id'],turn_status='inProgress',
                    iteration=t['native'].get('iteration',0)+int(new_turn),stop_requested=False)
                t['native'].pop('retry',None);t['native'].pop('stop',None)
            if method=='native/stopping':
                t['native']['stop_requested']=True;t['native'].pop('stop',None)
            if method in ('item/completed','item/commandExecution/outputDelta'):
                t['native'].pop('retry',None)
            if method=='turn/completed':t['native']['turn_status']=data['turn']['status'];t['native'].pop('retry',None)
            if method=='native/stopped':t['native']['stop']=data
            if method=='native/reconciled':t['native'].update(goal_status='complete',turn_status='completed')
            engine.store.log(tid,'native_event',method=method,data=data)
            engine.store.save(t)
            engine.store.activity.record(tid,'executor_output')
    def messages():
        with engine.store.lock(tid):
            t=engine.store.load(tid)
            pending=t['inbox']['hints']+t['inbox']['answers']
            if not pending:return ''
            # Reserve before send; uncertain delivery is never blindly replayed.
            t['inbox']['hints']=[];t['inbox']['answers']=[]
            engine.store.log(tid,'native_steer_dispatch',messages=pending)
            engine.store.save(t)
            return '\n'.join(pending)
    with execution(engine.store.activity,tid,max(.01,remaining),purpose='native_goal'):
        result=engine.executor.run_goal(objective,work_dir=work,session=t.get('session'),timeout=max(.01,remaining),
            max_turns=remaining_turns,cancel=engine.cancel_events.get(tid),emit=emit,checkpoint=checkpoint,messages=messages,
            reconcile_complete=not t['used']['verify_rounds'],stop_only=cleanup_only)
    with engine.store.lock(tid):
        t=engine.store.load(tid)
        t['step_in_progress']=None;t['used']['seconds']+=result.duration
        t['used']['steps']+=max(1,getattr(result,'native_turns',1))
        uncertain = (result.error or '').startswith(('native_outcome_unknown', 'native_stop_unconfirmed'))
        t['native_quarantine'] = uncertain
        if uncertain:
            engine.resources.retain(tid,t['spec']['resource'],work)
            if t['flags'].get('cancel'):
                t['native_cleanup_only']=True
                t['flags']['cancel']=False
        else:
            engine.resources.release_retained(tid)
        if result.canceled:
            if t.get('native_pause'):engine._block(t,'Native goal paused by requester',kind='native_paused')
            elif engine.stopping.is_set():engine._block(t,'Native goal paused for service shutdown; resume the same task',kind='native_paused')
            else:engine._stop(t,'canceled')
            return
        if not result.ok:
            engine._block(t,result.error or 'Native execution did not complete',kind='native_incomplete');return
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

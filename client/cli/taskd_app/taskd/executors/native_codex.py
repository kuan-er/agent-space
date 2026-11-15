"""Native Codex Goals: one persisted thread, streamed events, no outer planner.

Stdio is private to the worker. An owner-configured loopback endpoint additionally
allows the native TUI to attach. Neither requests nor peers select an endpoint.
"""
from __future__ import annotations
import json
import hashlib
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid

from ..platform import proc
from ..requesters.appserver import AppServer, RPCError
from .base import ExecResult
from ..instructions import observed_user_input


class NativeRPC:
    def __init__(self, executor, on_event=lambda event: None):
        self.executor, self.on_event = executor, on_event
        self.events = queue.Queue(maxsize=8192)
        self.process = self.ws = self.stderr = None
        self.closed = False

    def __enter__(self):
        ex = self.executor
        if ex.native_endpoint:
            self.ws = AppServer(ex.native_endpoint, token_file=ex.native_token_file).__enter__()
            self.ws.sock.settimeout(None)
        else:
            argv = [ex.command, 'app-server', '-c', 'features.goals=true']
            for setting in ex.config:
                argv += ['-c', setting]
            self.stderr = tempfile.TemporaryFile()
            opts = proc.background_options(new_group=True) if os.name == 'nt' else {'start_new_session': True}
            self.process = subprocess.Popen(proc.windows_script_argv(argv), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.stderr, env=ex.env(), **opts)
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            if not self.ws:
                self.rpc('initialize', {'clientInfo': {'name': 'taskd-native', 'version': '1.9.0'},
                                       'capabilities': {'experimentalApi': True}})
                self.send({'method': 'initialized', 'params': {}})
        except BaseException:
            self.__exit__()
            raise
        return self

    def _reader(self):
        try:
            while not self.closed:
                if self.ws:
                    value = self.ws.receive()
                else:
                    line = self.process.stdout.readline(16 * 1024 * 1024 + 1)
                    if not line: raise EOFError('native app server disconnected')
                    if len(line) > 16 * 1024 * 1024: raise RPCError('oversized native event')
                    value = json.loads(line)
                self.events.put(value)
        except Exception as error:
            if not self.closed: self.events.put(error)

    def send(self, value):
        if self.ws: self.ws.send(value)
        else:
            self.process.stdin.write((json.dumps(value, ensure_ascii=False)+'\n').encode())
            self.process.stdin.flush()

    def receive(self, timeout=1):
        try: value = self.events.get(timeout=timeout)
        except queue.Empty: raise TimeoutError('waiting for native event') from None
        if isinstance(value, Exception): raise value
        return value

    def dispatch(self, value):
        # Remote work cannot grant its own privilege escalation. User decisions
        # are delivered through the native UI or a future explicit approval adapter.
        if value.get('method') and 'id' in value:
            self.on_event({'method': 'native/needsApproval', 'params': {'method': value['method']}})
            if value['method']=='item/commandExecution/requestApproval':
                self.send({'id':value['id'],'result':{'decision':'decline'}})
            else:
                self.send({'id':value['id'],'error':{'code':-32601,'message':'Approval requires the node owner'}})
        elif value.get('method'):
            self.on_event(value)

    def rpc(self, method, params, timeout=20):
        rid = str(uuid.uuid4())
        self.send({'id':rid, 'method':method, 'params':params})
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            try: value=self.receive(min(1,max(.01,end-time.monotonic())))
            except TimeoutError: continue
            if value.get('id')==rid:
                if 'error' in value: raise RPCError(method+': '+str(value['error'])[:500])
                return value.get('result')
            self.dispatch(value)
        raise TimeoutError('native RPC acknowledgement unknown: '+method)

    def __exit__(self,*args):
        self.closed=True
        if self.ws:
            try: self.ws.sock.shutdown(2)
            except OSError: pass
            self.ws.close()
        if self.process:
            try:self.process.stdin.close()
            except OSError:pass
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:proc.kill_tree(self.process)
            self.process.stdout.close()
        if self.stderr:self.stderr.close()


def probe(executor):
    if not executor.native_goals:
        return {'available':False,'reason':'disabled_by_owner'}
    try:
        with NativeRPC(executor) as rpc:
            t=rpc.rpc('thread/start',{'cwd':str(Path.cwd()),'ephemeral':True,
                'sandbox':'read-only','approvalPolicy':'never'})['thread']
            try:
                rpc.rpc('thread/goal/get',{'threadId':t['id']})
            except RPCError as error:
                # Native goals intentionally require persistence. This precise
                # rejection proves the method exists without creating a saved task.
                if 'ephemeral thread does not support goals' not in str(error):raise
        return {'available':True,'harness':'codex','probe':'app-server goal/get; no model call',
                'persistent_session':True,'steer':True,'pause':True,'cancel_cleanup':True,
                'transport':'websocket' if executor.native_endpoint else 'stdio',
                'interactive_attach':bool(executor.native_endpoint)}
    except Exception as error:
        return {'available':False,'reason':'native_probe_failed: '+str(error)[:240]}


def retry_state(executor, tid):
    home=Path(executor.codex_home or os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    path=home/'logs_2.sqlite'
    if not path.exists():return None
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2) as c:
            row=c.execute("SELECT ts,feedback_log_body FROM logs WHERE thread_id=? AND target='codex_core::responses_retry' ORDER BY id DESC LIMIT 1",(tid,)).fetchone()
        if not row:return None
        ts,body=row
        delay=re.search(r'retrying sampling request \((\d+)/(\d+) in ([0-9.]+)(ms|s)\)',body)
        turn=re.search(r'turn.id=([0-9a-f-]+)',body)
        if not delay:return None
        seconds=float(delay[3])/(1000 if delay[4]=='ms' else 1)
        return {'at':ts,'turn_id':turn[1] if turn else None,'attempt':int(delay[1]),
                'retry_at':ts+seconds,'reason':'model_at_capacity' if 'Selected model is at capacity' in body else 'upstream_retry'}
    except (OSError,sqlite3.Error):return None


def run(executor, objective, *, work_dir, session, timeout, max_turns, cancel, emit, checkpoint, messages,
        reconcile_complete=True, stop_only=False):
    if stop_only and not session:
        return ExecResult(False,error='native_stop_unconfirmed: missing existing session')
    start=time.monotonic();tid=session;active=None;goal=None;final='';ended=False
    turns=0;stop_confirmed=False;blocked=None;last_retry=None;approval=False
    pending_delivery=None;next_input_observation=0
    goal_revision=0;turn_revision=0;live_event_turn=None;current_turn=None;completed_turns=set()
    def event(v):
        nonlocal active,goal,final,ended,turns,approval,goal_revision,turn_revision,live_event_turn,current_turn
        m=v.get('method');p=v.get('params',{});item=p.get('item',{})
        if p.get('threadId') and tid and p['threadId']!=tid:return
        if m=='turn/started':
            incoming=p['turn']['id']
            if incoming in completed_turns:
                emit('native/stale_turn_event',{'method':m,'turn_id':incoming});return
            if incoming==active:
                emit('native/duplicate_turn_event',{'method':m,'turn_id':incoming});return
            turns+=1
            active=live_event_turn=current_turn=incoming;turn_revision+=1;ended=False;final=''
        if m=='turn/completed':
            incoming=p['turn'].get('id')
            if not incoming or (current_turn is not None and incoming!=current_turn):
                if incoming:completed_turns.add(incoming)
                emit('native/stale_turn_event',{'method':m,'turn_id':incoming});return
            completed_turns.add(incoming);turn_revision+=1
            active=live_event_turn=None;ended=p['turn']['status']=='completed'
        if m in ('thread/goal/updated','item/completed','item/commandExecution/outputDelta') and p.get('turnId') and current_turn and p['turnId']!=current_turn:
            emit('native/stale_turn_event',{'method':m,'turn_id':p['turnId']});return
        if m=='thread/goal/updated':
            incoming=p.get('goal')
            old_stamp=(goal or {}).get('updatedAt');new_stamp=(incoming or {}).get('updatedAt')
            if isinstance(old_stamp,(int,float)) and isinstance(new_stamp,(int,float)) and new_stamp<old_stamp:
                emit('native/stale_goal_event',{'updated_at':new_stamp});return
            goal=incoming;goal_revision+=1
        if m=='item/completed' and item.get('type')=='agentMessage' and item.get('phase') in ('final_answer','final',None):final=item.get('text','')
        if m=='native/needsApproval':approval=True
        if m in ('turn/started','turn/completed','thread/goal/updated','native/needsApproval','error'):
            emit(m, p)
        elif m=='item/completed' and item.get('type') in ('commandExecution','agentMessage'):
            emit(m,{'type':item['type'],'command':item.get('command'), 'exit_code':item.get('exitCode'),
                    'text':(item.get('text') or item.get('aggregatedOutput') or '')[-4000:]})
        elif m=='item/commandExecution/outputDelta':
            emit(m,{'text':p.get('delta','')[-4000:]})
    def goal_request(rpc, method, params):
        # RPC replies and notifications can interleave. Never keep the pre-activation
        # paused snapshot merely because goal/updated arrives after the ACK.
        nonlocal goal
        before=goal_revision
        response=rpc.rpc(method,params)
        snapshot=response.get('goal')
        if isinstance(snapshot,dict):
            old_stamp=(goal or {}).get('updatedAt');new_stamp=snapshot.get('updatedAt')
            newer=isinstance(old_stamp,(int,float)) and isinstance(new_stamp,(int,float)) and new_stamp>old_stamp
            older=isinstance(old_stamp,(int,float)) and isinstance(new_stamp,(int,float)) and new_stamp<old_stamp
            if not older and (before==goal_revision or newer):goal=snapshot
        return snapshot

    def confirmed_goal(rpc):
        # updatedAt has only second precision; equal/missing timestamps cannot
        # order a late paused notification against an activation ACK. Before
        # acting on a terminal Goal, demand an uncontended fresh read instead.
        nonlocal goal
        for _ in range(3):
            before=goal_revision
            snapshot=rpc.rpc('thread/goal/get',{'threadId':tid}).get('goal')
            if before!=goal_revision:continue
            if not isinstance(snapshot,dict):raise RPCError('goal state confirmation missing; no cleanup')
            old_stamp=(goal or {}).get('updatedAt');new_stamp=snapshot.get('updatedAt')
            if isinstance(old_stamp,(int,float)) and isinstance(new_stamp,(int,float)) and new_stamp<old_stamp:continue
            goal=snapshot
            emit('native/goal_confirmed',{'status':goal.get('status'),'updated_at':goal.get('updatedAt')})
            return goal
        raise RPCError('goal state changed during confirmation; no cleanup or retry')

    result=None
    try:
        if stop_only and not tid:
            raise ValueError('native stop requires an existing thread ID')
        with NativeRPC(executor,event) as rpc:
            args={'cwd':str(work_dir),'sandbox':executor.sandbox,'approvalPolicy':'on-request'}
            if executor.model:args['model']=executor.model
            t=rpc.rpc('thread/resume' if tid else 'thread/start',dict(args,threadId=tid) if tid else args)['thread']
            if session and t.get('id')!=session:raise RPCError('resumed thread identity mismatch')
            tid=t['id'];checkpoint(tid)  # durable BEFORE activating work
            goal_request(rpc,'thread/goal/get',{'threadId':tid})
            if not stop_only and session and reconcile_complete and goal and goal.get('status')=='complete' and t.get('status',{}).get('type')=='idle':
                history=rpc.rpc('thread/read',{'threadId':tid,'includeTurns':True})['thread'].get('turns',[])
                completed=next((v for v in reversed(history) if v['status']=='completed'),None)
                items=[]
                if completed:
                    page=rpc.rpc('thread/items/list',{'threadId':tid,'turnId':completed['id'],'limit':100,'sortDirection':'desc'})
                    items=[entry['item'] for entry in page.get('data',[]) if entry.get('turnId')==completed['id']]
                text=next((v.get('text','') for v in items
                           if v.get('type')=='agentMessage' and v.get('phase') in ('final_answer','final',None)),None)
                if text:
                    result=ExecResult(True,text,tid,duration=time.monotonic()-start)
                    result.native_turns=0;result.native_goal=goal
                    emit('native/reconciled',{'thread_id':tid,'goal_status':'complete'})
                    return result
                raise RPCError('completed goal has no observed final answer; inspect existing history, do not reactivate')
            # Do not adopt a running thread from an unrelated owner operation.
            if t.get('status',{}).get('type')=='active':
                before=turn_revision
                snapshot=rpc.rpc('thread/read',{'threadId':tid,'includeTurns':True})['thread']
                candidates=[x['id'] for x in snapshot.get('turns',[]) if x.get('status')=='inProgress' and x['id'] not in completed_turns]
                if before==turn_revision and live_event_turn is None:
                    if len(candidates)>1:raise RPCError('ambiguous active turn history; no turn adopted')
                    active=candidates[0] if candidates else None
                    if active:current_turn=active
            elif not stop_only and not (cancel and cancel.is_set()):
                # Activation is one-shot. Consume its ACK and then refresh the Goal;
                # a stale paused snapshot must never initiate cleanup of a new turn.
                goal_request(rpc,'thread/goal/set',{'threadId':tid,'objective':objective,'status':'active'})
                goal_request(rpc,'thread/goal/get',{'threadId':tid})
                if not isinstance(goal,dict):raise RPCError('goal activation outcome unknown; no retry')
            next_retry=0
            while timeout is None or time.monotonic()-start<timeout:
                if stop_only or (cancel and cancel.is_set()):blocked='canceled';break
                if approval:blocked='native_approval_required';break
                if pending_delivery and time.monotonic()>=next_input_observation:
                    target_turn,pending,receipt=pending_delivery
                    snapshot=rpc.rpc('thread/items/list',{'threadId':tid,'turnId':target_turn,'limit':100,'sortDirection':'desc'})
                    items=[entry['item'] for entry in snapshot.get('data',[]) if entry.get('turnId')==target_turn]
                    visible={'turns':[{'id':target_turn,'items':items}]}
                    if observed_user_input(visible,target_turn,pending):
                        emit('native/input_observed',receipt);pending_delivery=None
                    else:
                        next_input_observation=time.monotonic()+1
                        if ended and goal and goal.get('status')=='complete':
                            raise RPCError('follow-up acknowledged but not observed in the intended completed turn; no replay')
                if goal and goal.get('status')=='complete' and ended and final and not pending_delivery:
                    result=ExecResult(True,final,tid,duration=time.monotonic()-start);break
                # max_turns is a legacy compatibility argument, not a hard stop.
                # Respect the harness' own blocked/budget state below; never bypass it.
                if goal and goal.get('status') in ('paused','blocked','budget_limited','budgetLimited'):
                    state=confirmed_goal(rpc).get('status')
                    if state not in ('paused','blocked','budget_limited','budgetLimited'):continue
                    blocked='native_'+state;break
                if ended and (not goal or goal.get('status')!='complete'):
                    # Codex owns continuation. Allow its next turn; do not inject one.
                    ended=False
                if active and not pending_delivery:
                    pending=messages()
                    if pending:
                        target_turn=active
                        receipt={'turn_id':target_turn,'sha256':hashlib.sha256(pending.encode('utf-8')).hexdigest()}
                        ack=rpc.rpc('turn/steer',{'threadId':tid,'expectedTurnId':target_turn,
                            'input':[{'type':'text','text':pending}]})
                        if not isinstance(ack,dict) or ack.get('turnId')!=target_turn:
                            raise RPCError('follow-up acknowledgement names a different turn; no replay')
                        emit('native/input_acknowledged',receipt)
                        pending_delivery=(target_turn,pending,receipt)
                        next_input_observation=0
                if time.monotonic()>=next_retry:
                    retry=retry_state(executor,tid);next_retry=time.monotonic()+10
                    if retry and retry!=last_retry and retry['turn_id']==active:
                        emit('native/retry',retry);last_retry=retry
                try:rpc.dispatch(rpc.receive(1))
                except TimeoutError:pass
            if not result:
                blocked=blocked or 'native_time_budget'
                emit('native/stopping', {})
                # A stop ACK alone is not terminal-process cleanup.
                if not (goal and goal.get('status')=='complete'):
                    goal_request(rpc,'thread/goal/set',{'threadId':tid,'status':'paused'})
                # Read all turns, not only the last notification (which may predate a disconnect).
                snapshot=rpc.rpc('thread/read',{'threadId':tid,'includeTurns':True})['thread']
                live=[v['id'] for v in snapshot.get('turns',[]) if v.get('status')=='inProgress']
                if not isinstance(snapshot.get('turns'),list):
                    raise RPCError('stop inspection did not return turns')
                # Persisted history may contain an old inProgress turn. Only use the
                # streamed current ID, or an unambiguous active-thread snapshot.
                candidates=[v for v in live if v not in completed_turns]
                interrupt_id=None
                if snapshot.get('status',{}).get('type')=='active':
                    if live_event_turn is not None:interrupt_id=live_event_turn
                    elif len(candidates)==1:interrupt_id=candidates[0]
                    else:raise RPCError('ambiguous active turn during stop; no interrupt sent')
                elif candidates:
                    raise RPCError('non-active thread has unfinished historical turns; no interrupt sent')
                if interrupt_id:
                    # A concurrent change is allowed to reject this exact ID. Never
                    # retarget/replay an interrupt against a newly observed turn.
                    rpc.rpc('turn/interrupt',{'threadId':tid,'turnId':interrupt_id})
                rpc.rpc('thread/backgroundTerminals/clean',{'threadId':tid})
                observed={}
                for _ in range(20):
                    snapshot=rpc.rpc('thread/read',{'threadId':tid,'includeTurns':True})['thread']
                    live=[v.get('id') for v in snapshot.get('turns',[]) if v.get('status')=='inProgress']
                    observed={'thread_status':snapshot.get('status',{}).get('type'),'active_turn_ids':live}
                    if observed['thread_status']=='idle' and isinstance(snapshot.get('turns'),list) and not live:stop_confirmed=True;break
                    try:rpc.dispatch(rpc.receive(.2))
                    except TimeoutError:pass
                emit('native/stopped',{'confirmed':stop_confirmed,'reason':blocked,'observed':observed,'goal_status':(goal or {}).get('status'),
                                      'scope':'native turn and managed background terminals; detached services require their own checks'})
                result=ExecResult(False,final,tid,error=blocked if stop_confirmed else 'native_stop_unconfirmed',
                                  duration=time.monotonic()-start,canceled=blocked=='canceled' and stop_confirmed,
                                  timed_out=blocked=='native_time_budget')
    except Exception as error:
        # Unknown outcome: never silently fall back to a second executor.
        result=ExecResult(False,final,tid,error='native_outcome_unknown: '+str(error)[:300],duration=time.monotonic()-start)
    result.native_turns=turns
    result.native_goal=goal
    return result

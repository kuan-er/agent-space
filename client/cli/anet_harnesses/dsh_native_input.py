"""One-shot owner input for an exact live DeepSeek SDK root session.

No permission mode/rules, native settings or credentials are read or changed.
Unknown sessions (including unbound descendants) are rejected. Only the owner
broker's validated decision can produce allowed-once; no response is retried.
"""
import copy
import json
import threading
import time

from .ledger import OwnershipError
from .native_input import NativeInputClosed,NativeInputTimeout


def _copy(value):
    raw=json.dumps(value,ensure_ascii=False,allow_nan=False)
    if len(raw.encode('utf-8'))>65536:raise ValueError('native DeepSeek input exceeds bound')
    return json.loads(raw)


def approval_validator(result):
    if not isinstance(result,dict) or set(result)!={'decision'} or result['decision'] not in ('accept','decline','cancel'):
        raise ValueError('choose one explicit owner decision')
    return {'outcome':{'accept':'allowed-once','decline':'rejected','cancel':'cancelled'}[result['decision']]}


def question_validator(questions):
    originals={}
    if not isinstance(questions,list) or not 1<=len(questions)<=32:raise ValueError('native question batch required')
    for q in questions:
        if not isinstance(q,dict) or not isinstance(q.get('id'),str) or not q['id'] or q['id'] in originals or not isinstance(q.get('question'),str) or not q['question']:
            raise ValueError('native questions require unique exact IDs and text')
        options=q.get('options',[])
        if not isinstance(options,list) or len(options)>64 or type(q.get('multiSelect',False)) is not bool:
            raise ValueError('native question options or selection mode invalid')
        labels=[]
        for option in options:
            if not isinstance(option,dict) or not isinstance(option.get('label'),str) or not option['label'] or option['label'] in labels:
                raise ValueError('native option labels must be unique')
            labels.append(option['label'])
        originals[q['id']]=(q,labels)
    def validate(result):
        if result=={'decision':'cancel'}:return {'cancelled':True}
        if not isinstance(result,dict) or set(result)!={'answers'} or not isinstance(result['answers'],dict) or set(result['answers'])!=set(originals):
            raise ValueError('answer every original native question ID exactly once')
        answers=[]
        for ident,(question,labels) in originals.items():
            value=result['answers'][ident];custom=None
            if isinstance(value,str):
                selected=[value] if value in labels else []
                if value and value not in labels:custom=value
            elif isinstance(value,list):selected=value
            elif isinstance(value,dict) and set(value) in ({'selected'},{'selected','custom'}):
                selected=value['selected'];custom=value.get('custom')
            else:raise ValueError('native question answer has an unsupported shape')
            if not isinstance(selected,list) or any(not isinstance(v,str) or v not in labels for v in selected) or len(set(selected))!=len(selected):
                raise ValueError('select only original native option labels')
            if custom is not None and (not isinstance(custom,str) or not custom or len(custom.encode('utf-8'))>8192):raise ValueError('custom native answer invalid')
            intent=question.get('intent') or {}
            plan_feedback=(isinstance(intent,dict) and intent.get('kind')=='plan-review' and len(selected)==1 and selected[0]!=intent.get('approve'))
            if not question.get('multiSelect',False) and (len(selected)>1 or custom is not None and selected and not plan_feedback):
                raise ValueError('single-select answer must be one label or custom text')
            item={'id':ident,'selected':list(selected)}
            if custom is not None:item['custom']=custom
            answers.append(item)
        return {'answers':answers}
    return validate


class DeepSeekNativeInput:
    def __init__(self,broker,*,timeout_seconds=600,max_workers=8):
        if isinstance(timeout_seconds,bool) or not isinstance(timeout_seconds,(int,float)) or not 0<timeout_seconds<=3600:raise ValueError('bounded owner input timeout required')
        if type(max_workers) is not int or not 1<=max_workers<=16:raise ValueError('bounded native input workers required')
        self.broker=broker;self.timeout=timeout_seconds;self.lock=threading.RLock();self.stop=threading.Event()
        self.contexts={};self.closed_operations=set();self.seen=set();self.sent=set();self.jobs=set()
        self.slots=threading.BoundedSemaphore(max_workers);self.client=None;self.reader=None;self.last_error=None

    def bind_operation(self,source_id,operation_id,native_id):
        if not all(isinstance(v,str) and 0<len(v)<=256 for v in (source_id,operation_id,native_id)):raise ValueError('exact native operation identity required')
        with self.lock:
            if self.stop.is_set() or (source_id,operation_id) in self.closed_operations:raise NativeInputClosed('native input activity is closed')
            prior=self.contexts.get(native_id)
            if prior and prior!=(source_id,operation_id):raise OwnershipError('another operation owns this native session')
            self.contexts[native_id]=(source_id,operation_id)

    def _context(self,native_id):
        with self.lock:return self.contexts.get(native_id) if not self.stop.is_set() else None

    def begin_control(self,source_id,operation_id,native_id,action):
        """Share an existing activity only for native state-only pause/show."""
        if action not in ('pause','show','resume'):raise ValueError('unsupported native Goal control')
        with self.lock:
            prior=self._context(native_id)
            if prior:
                if prior[0]!=source_id:raise OwnershipError('native control belongs to another source')
                if action=='resume':raise OwnershipError('finish the existing native activity before Goal resume')
                return False
            self.bind_operation(source_id,operation_id,native_id)
            return True

    def close_operation(self,source_id,operation_id):
        with self.lock:
            self.closed_operations.add((source_id,operation_id))
            self.contexts={native:ctx for native,ctx in self.contexts.items() if ctx!=(source_id,operation_id)}
        self.broker.close_operation(source_id,operation_id)

    def start(self,client):
        """Start the request drain before enabling/starting native SDK requests."""
        with self.lock:
            if self.stop.is_set():raise NativeInputClosed('native input bridge is closed')
            if self.client is not None:
                if self.client is not client:raise OwnershipError('native input runtime changed')
                return
            self.client=client
            self.reader=threading.Thread(target=self._read,name='anet-dsh-owner-input',daemon=True);self.reader.start()

    def _respond(self,request_id,result=None,error=None,context=None):
        marker=(type(request_id).__name__,str(request_id))
        with self.lock:
            if marker in self.sent:return
            self.sent.add(marker);client=self.client
            if context and (self.stop.is_set() or self.contexts.get(context[0])!=context[1]):result={'outcome':'rejected'}
            try:
                if error:client.respond_error(request_id,code=-32601,message=error)
                else:client.respond(request_id,result)
            except Exception as problem:
                # The first response may have reached native execution; never resend.
                self.last_error=type(problem).__name__

    def _read(self):
        while not self.stop.is_set():
            try:request=self.client.next_request()
            except Exception as error:
                self.last_error=type(error).__name__;break
            ident=getattr(request,'id',None);method=getattr(request,'method',None)
            if type(ident) not in (str,int) or isinstance(ident,str) and not 0<len(ident)<=256:continue
            marker=(type(ident).__name__,str(ident))
            with self.lock:
                if marker in self.seen:continue
                if len(self.seen)>=4096:
                    self._respond(ident,{'outcome':'rejected'});continue
                self.seen.add(marker)
            if method!='approval/request':
                self._respond(ident,error='Unsupported owner input request; native user questions require the configured callback');continue
            if not self.slots.acquire(blocking=False):
                self._respond(ident,{'outcome':'rejected'});continue
            def run(request=request):
                try:self._approval(request)
                finally:
                    self.slots.release()
                    with self.lock:self.jobs.discard(threading.current_thread())
            worker=threading.Thread(target=run,name='anet-dsh-owner-approval',daemon=True)
            with self.lock:self.jobs.add(worker)
            worker.start()

    def _approval(self,request):
        ident=request.id
        try:
            payload=_copy(request.payload);native_id=payload.get('sessionId');ctx=self._context(native_id)
            if not ctx or not isinstance(payload.get('toolName'),str) or not payload['toolName']:
                self._respond(ident,{'outcome':'rejected'});return
            call_id=payload.get('callId')
            if call_id is not None:
                if not isinstance(call_id,str) or not call_id:raise ValueError('native tool call identity invalid')
                marker=('call',native_id,call_id)
                with self.lock:
                    if marker in self.seen:self._respond(ident,{'outcome':'rejected'});return
                    self.seen.add(marker)
            detail={'tool_name':payload['toolName'],'native_payload':payload,'native_request_id':ident,
                    'reason':payload.get('reason',''),'operation_correlation_verified':False,
                    'binding_scope':'owned_native_session_local_activity_binding'}
            for field in ('input','title','description'):
                if field in payload:detail[field]=copy.deepcopy(payload[field])
            result=self.broker.ask_sync(*ctx,native_id,'deepseek/approval/request',detail,approval_validator,timeout_seconds=self.timeout)
            if not isinstance(result,dict) or set(result)!={'outcome'} or result['outcome'] not in ('allowed-once','rejected','cancelled'):
                raise ValueError('owner broker did not return a one-shot native decision')
            self._respond(ident,result,context=(native_id,ctx))
        except (NativeInputClosed,NativeInputTimeout):self._respond(ident,{'outcome':'rejected'})
        except Exception as error:
            self.last_error=type(error).__name__;self._respond(ident,{'outcome':'rejected'})

    def on_user_question(self,request,cancelled):
        if not self.slots.acquire(blocking=False):return {'cancelled':True}
        finished=threading.Event();watcher=None;ctx=None
        try:
            if self.client is None or not hasattr(cancelled,'is_set') or cancelled.is_set():return {'cancelled':True}
            if isinstance(request,dict):payload=_copy(request)
            elif callable(getattr(request,'model_dump',None)):payload=_copy(request.model_dump(by_alias=True))
            else:payload=_copy({'requestId':request.request_id,'sessionId':request.session_id,'questions':request.questions})
            native_id=payload.get('sessionId');ctx=self._context(native_id);native_request=payload.get('requestId')
            if not ctx or not isinstance(native_request,str) or not native_request:return {'cancelled':True}
            marker=('question',native_id,native_request)
            with self.lock:
                if marker in self.seen or len(self.seen)>=4096:return {'cancelled':True}
                self.seen.add(marker)
            questions=payload.get('questions');validator=question_validator(questions)
            detail={'questions':copy.deepcopy(questions),'native_payload':payload,'native_request_id':native_request,
                    'operation_correlation_verified':False,'binding_scope':'owned_native_session_local_activity_binding'}
            def watch():
                while not finished.wait(.05):
                    if cancelled.is_set():
                        # SDK withdrew this activity; do not leave a stale owner
                        # prompt or allow any later answer to this operation.
                        self.close_operation(*ctx);break
            watcher=threading.Thread(target=watch,name='anet-dsh-question-cancel',daemon=True);watcher.start()
            value=self.broker.ask_sync(*ctx,native_id,'deepseek/user/requestInput',detail,validator,timeout_seconds=self.timeout)
            return {'cancelled':True} if cancelled.is_set() or self._context(native_id)!=ctx else value
        except (NativeInputClosed,NativeInputTimeout):return {'cancelled':True}
        except Exception as error:
            self.last_error=type(error).__name__;return {'cancelled':True}
        finally:
            finished.set()
            if watcher:watcher.join(timeout=.2)
            self.slots.release()

    def close(self):
        self.stop.set()
        with self.lock:contexts=set(self.contexts.values());self.contexts.clear()
        for source,operation in contexts:self.broker.close_operation(source,operation)

    def join(self,timeout_seconds=1):
        """After caller closes its own SDK, the native reader is woken normally."""
        deadline=time.monotonic()+timeout_seconds
        with self.lock:threads=([self.reader] if self.reader else [])+list(self.jobs)
        for worker in threads:
            if worker is not threading.current_thread():worker.join(max(0,deadline-time.monotonic()))
        return not any(worker.is_alive() for worker in threads)

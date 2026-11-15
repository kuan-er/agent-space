"""Finite in-process interface for the desktop source broker.

The root broker supplies adapters/owned runtimes, not arbitrary commands from UI.
Only model submission runs on a worker; status/cancel/control remain callable.
"""
from concurrent.futures import ThreadPoolExecutor
import threading,hashlib,json
from .adapters import UnsupportedCapability

class HarnessManager:
    def __init__(self,ledger,adapters):
        self.ledger=ledger;self.adapters=dict(adapters);self.pool=ThreadPoolExecutor(max_workers=4);self.jobs={};self.inputs={};self.lock=threading.RLock()
    def create_source(self,harness,local_id,workspace):
        if harness not in self.adapters:raise UnsupportedCapability('harness runtime is not configured and verified')
        return self.adapters[harness].create(local_id,workspace)
    def restore_source(self,local_id):
        source=self.ledger.session(local_id);a=self.adapters[source['harness']]
        if not hasattr(a,'restore'):raise UnsupportedCapability('this source must recover its exact acknowledged task through operation id')
        return a.restore(local_id)
    def submit(self,local_id,operation_id,text,goal=False,user_authorized=False,restore=False):
        source=self.ledger.session(local_id);a=self.adapters[source['harness']]
        digest=hashlib.sha256(json.dumps([local_id,text,goal,restore],ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            if operation_id in self.jobs:
                if self.inputs[operation_id]!=digest:raise ValueError('operation id input conflict')
                return {'operation_id':operation_id,'state':'already_submitted_local'}
            def run():
                if goal:
                    kw={'user_authorized':user_authorized}
                    if source['harness']=='claude_code':kw['restore']=restore
                    return a.goal(local_id,operation_id,text,**kw)
                if source['harness']=='claude_code':return a.run(local_id,operation_id,text,restore=restore)
                if hasattr(a,'submit'):return a.submit(local_id,operation_id,text)
                return a.run(local_id,operation_id,text)
            self.inputs[operation_id]=digest;self.jobs[operation_id]=self.pool.submit(run)
        return {'operation_id':operation_id,'state':'submitted_local','backend_acknowledged':False}
    def status(self,operation_id):
        row=self.ledger.get(operation_id)
        with self.lock:job=self.jobs.get(operation_id)
        if job and job.done() and job.exception():
            # Never return provider error bodies, prompts, environment or credentials.
            return {'operation':row,'local_error':type(job.exception()).__name__,'retry_authorized':False}
        return {'operation':row,'local_worker_active':bool(job and not job.done()),'retry_authorized':False}
    def close(self):self.pool.shutdown(wait=False,cancel_futures=False)

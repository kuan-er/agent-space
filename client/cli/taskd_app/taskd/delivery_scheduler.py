"""Fair bounded batches. Limits yield control; they never fail a transfer."""
import time


class DeliveryScheduler:
    def __init__(self,workers,*,stop_requested,clock=time.monotonic,max_steps=32,max_seconds=2,idle_seconds=5):
        self.workers=[(name,worker) for name,worker in workers if worker is not None]
        self.stop_requested=stop_requested;self.clock=clock
        self.max_steps=max_steps;self.max_seconds=max_seconds;self.idle_seconds=idle_seconds
        self.cursor=0;self.due={name:0.0 for name,_ in self.workers}
        self.last={'steps':0,'progress_steps':0,'yielded':False};self.deadline=None

    def should_yield(self):
        return self.stop_requested() or (self.deadline is not None and self.clock()>=self.deadline)

    def batch(self):
        start=self.clock();self.deadline=start+self.max_seconds
        result={'steps':0,'progress_steps':0,'yielded':False,'failed_by_budget':False,'errors':[]}
        try:
            while result['steps']<self.max_steps and not self.should_yield():
                chosen=None
                for _ in range(len(self.workers)):
                    index=self.cursor%len(self.workers);self.cursor=(index+1)%len(self.workers)
                    name,worker=self.workers[index]
                    if self.due[name]<=self.clock():chosen=(name,worker);break
                if chosen is None:break
                name,worker=chosen;worker.last_step_progress=False
                failed=False
                try:worker.tick()
                except Exception as error:
                    failed=True
                    # Runtime loops remain alive; no task is called failed or
                    # executed again because a delivery step could not advance.
                    worker.last_step_progress=False
                    result['errors'].append({'worker':name,'type':type(error).__name__})
                result['steps']+=1
                progressed=worker.last_step_progress is True
                result['progress_steps']+=int(progressed)
                retry=self.idle_seconds
                if not progressed and not failed and callable(getattr(worker,'delivery_delay',None)):
                    try:retry=max(0.0,min(retry,worker.delivery_delay()))
                    except Exception:pass  # Damaged queue keeps the ordinary bounded backoff.
                self.due[name]=self.clock() if progressed else self.clock()+retry
            result['yielded']=(result['steps']>=self.max_steps or self.clock()>=self.deadline)
            self.last=result
            return dict(result)
        finally:self.deadline=None

    def delay(self):
        if not self.workers:return self.idle_seconds
        return max(0.0,min(self.idle_seconds,min(self.due.values())-self.clock()))

    def public_state(self):
        return {'mode':'fair_bounded_progress_batches','workers':[name for name,_ in self.workers],
            'max_steps_per_batch':self.max_steps,'max_seconds_before_yield':self.max_seconds,
            'waiting_backoff_seconds':self.idle_seconds,'limits_fail_transfers':False,'last_batch':self.last}

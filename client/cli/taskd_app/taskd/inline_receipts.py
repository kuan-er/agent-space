"""Legacy inline receipts outside Relay/admission, using its own pull connection.

No external-delivery capability or external queue quota is consumed. This worker
never writes tracked/session records and never executes or dispatches a task.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid

from . import attachments,private_artifact_dir as private
from .artifact_core import auto_artifacts as core

SCHEMA='taskd-inline-receipts/1'
LIMIT=512*1024

def encode(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()
def read(path):
    path=private.plain(path)
    with path.open('rb') as stream:raw=stream.read(LIMIT+1)
    if len(raw)>LIMIT:raise ValueError('inline_record_limit')
    return json.loads(raw)
def publish(path,value):
    raw=encode(value)
    if len(raw)>LIMIT:raise ValueError('inline_record_limit')
    temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    with private.open_new(temporary) as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    # Never replace an unknown receipt or concurrently-created intent.
    try:os.link(temporary,path)
    except FileExistsError:
        if read(path)!=value:raise ValueError('inline_record_conflict')
    finally:temporary.unlink()
def stamp(path):
    info=private.plain(path).stat()
    return [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns]

def verified_stamp(path,size,expected):
    """Bind the receipt's identity to the same bytes we actually hash."""
    path=private.plain(path);before=stamp(path)
    if before[2]!=size:raise ValueError('inline_received_file_changed')
    digest=hashlib.sha256();count=0
    with path.open('rb') as stream:
        while True:
            data=stream.read(min(1024*1024,size-count+1))
            if not data:break
            count+=len(data)
            if count>size:raise ValueError('inline_received_file_changed')
            digest.update(data)
    if count!=size or digest.hexdigest()!=expected or stamp(path)!=before:
        raise ValueError('inline_received_file_changed')
    return before


class InlineReceipts:
    def __init__(self,directory,transport,*,stop_requested=lambda:False):
        self.root=private.plain(directory);self.transport=transport;self.stop_requested=stop_requested
        self.busy=threading.Lock();self.cursor=0;self.last_step_progress=False
        if not self.root.exists():
            private.create_new(self.root)
            for name in ('jobs','results','downloads'):private.create_new(self.root/name)
            publish(self.root/'owner.json',{'schema':SCHEMA,'operator':private.owner_id(),
                'root':core.identity(self.root,True),
                'children':{name:core.identity(self.root/name,True) for name in ('jobs','results','downloads')}})
        self.validate()

    def validate(self):
        private.verify(self.root,root=True);owner=read(self.root/'owner.json')
        if owner.get('schema')!=SCHEMA or owner.get('operator')!=private.owner_id():raise ValueError('inline_owner')
        core.require_identity(self.root,owner['root'],True)
        for name,value in owner['children'].items():
            if name not in ('jobs','results','downloads'):raise ValueError('inline_owner')
            private.verify(self.root/name,root=True);core.require_identity(self.root/name,value,True)

    @staticmethod
    def required(message,envelope):
        return bool((envelope or {}).get('attachments') or message.get('attachments') or message.get('files'))

    def offer(self,task_id,message,envelope,attempt=0):
        self.validate()
        if not re.fullmatch(r'ix_[0-9a-f]{32}',task_id):raise ValueError('inline_task_id')
        if type(attempt)is not int or not 0<=attempt<=2:raise ValueError('inline_attempt')
        expected=(envelope or {}).get('attachments') or message.get('attachments') or []
        announced=message.get('files') or []
        if not isinstance(expected,list) or len(expected)>64 or not isinstance(announced,list) or len(announced)>64:
            raise ValueError('inline_manifest_limit')
        value={'schema':SCHEMA,'task_id':task_id,'message_id':message['id'],
            'body_sha256':hashlib.sha256(message['text'].encode()).hexdigest(),
            'expected':copy.deepcopy(expected),'announced':copy.deepcopy(announced),'attempt':attempt}
        key=hashlib.sha256(encode(value)).hexdigest();path=self.root/'jobs'/(key+'.json')
        if path.exists():
            if read(path)!=value:raise ValueError('inline_intent_pin_mismatch')
        else:publish(path,value)
        return key

    def result(self,key):
        if not re.fullmatch('[0-9a-f]{64}',key):raise ValueError('inline_key')
        self.validate();path=self.root/'results'/(key+'.json')
        if not path.exists():return None
        value=read(path)
        if value.get('key')!=key or value.get('schema')!=SCHEMA:raise ValueError('inline_receipt_pin')
        intent=read(self.root/'jobs'/(key+'.json'))
        if hashlib.sha256(encode(intent)).hexdigest()!=key:raise ValueError('inline_intent_changed')
        result=value['receipt']
        if result.get('state')=='verified':
            try:
                for file in result['files']:
                    path=private.plain(file['path'])
                    if not path.is_relative_to(self.root/'downloads') or stamp(path)!=file['fingerprint']:
                        raise ValueError('inline_received_file_changed')
            except (ValueError,OSError):
                return {'state':'failed','retryable':False,'files':[],'error':'inline_received_file_changed'}
        return copy.deepcopy(result)

    def tick(self):
        self.last_step_progress=False
        if self.stop_requested() or not self.busy.acquire(False):return
        try:
            self.validate()
            # Only pending receipts are active. Completed small jobs do not use
            # the separate external queue's permanent 32-job capacity.
            pending=[p for p in sorted((self.root/'jobs').glob('*.json'))
                if not (self.root/'results'/p.name).exists()]
            if not pending:return
            path=pending[self.cursor%len(pending)];self.cursor+=1;value=read(path);key=path.stem
            if hashlib.sha256(encode(value)).hexdigest()!=key:raise ValueError('inline_intent_changed')
            # receive already namespaces by task id and complete manifest hash.
            # Reuse that namespace; do not add another 64-character path layer.
            destination=self.root/'downloads'
            private.verify(destination,root=True)
            with core.Lock(self.root/('work-'+key+'.lock')):
                if (self.root/'results'/path.name).exists():return
                result=attachments.receive(self.transport,value['task_id'],
                    {'files':value['announced']},{'attachments':value['expected']},destination)
                if result.get('state')=='verified':
                    try:
                        for file in result['files']:
                            checked=private.plain(file['path'])
                            if not checked.is_relative_to(destination):raise ValueError('inline_received_file_changed')
                            file['fingerprint']=verified_stamp(checked,file['size'],file['sha256'])
                    except (ValueError,OSError):
                        result={'state':'failed','retryable':False,'files':[],'error':'inline_received_file_changed'}
                publish(self.root/'results'/path.name,{'schema':SCHEMA,'key':key,'receipt':result})
                self.last_step_progress=True
        finally:self.busy.release()

    def public_state(self):
        self.validate()
        jobs=list((self.root/'jobs').glob('*.json'));done=sum((self.root/'results'/p.name).exists() for p in jobs)
        return {'enabled':True,'ready':True,'pending':len(jobs)-done,'receipts':done,
            'busy':self.busy.locked(),'network_in_relay':False,'external_queue_slots_used':0}

    def close(self):self.transport.close()


def from_node(node,config,transport,store):
    downloads=private.plain((config.get('requester') or {}).get('downloads_dir') or attachments.download_root(store.root))
    root=private.plain((config.get('requester_inline') or {}).get('queue_dir') or downloads/'.inline-receipts-v1')
    excluded=private.plain(node.data_dir)
    if root==excluded or excluded in root.parents:raise ValueError('inline_outside_node_data_required')
    missing=[];cursor=root.parent
    while not cursor.exists():missing.append(cursor);cursor=cursor.parent
    for path in reversed(missing):private.create_new(path)
    from .transports.anet import AnetTransport
    return InlineReceipts(root,AnetTransport(anet=transport.anet,data_dir=node.data_dir))

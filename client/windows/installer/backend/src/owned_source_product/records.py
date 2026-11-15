"""Bounded local journal helpers; exclusive temp writes, no identity/config IO."""
from contextlib import contextmanager
from datetime import datetime,timezone
import json
import os
import time
from pathlib import Path
import uuid
from . import private_directory as private

LIMIT=1024*1024
def utcnow():return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
def read_json(path,default=None):
    path=private.plain(path)
    for attempt in range(8):
        try:
            with path.open('rb') as stream:raw=stream.read(LIMIT+1)
            break
        except FileNotFoundError:return default
        except PermissionError as error:
            # A Windows atomic receipt replacement may briefly deny shared
            # reads. Observe the same file again; never resend its operation.
            # Actual access denial (winerror 5) is not a sharing conflict.
            if os.name!='nt' or getattr(error,'winerror',None) not in (32,33) or attempt==7:
                raise
            time.sleep(.02*(attempt+1))
    if len(raw)>LIMIT:raise ValueError('owned_source_record_too_large')
    def pairs(items):
        value={}
        for key,item in items:
            if key in value:raise ValueError('duplicate_record_key')
            value[key]=item
        return value
    return json.loads(raw,object_pairs_hook=pairs)
def create_json(path,value):
    path=private.plain(path);raw=canonical(value)
    if len(raw)>LIMIT:raise ValueError('owned_source_record_too_large')
    with path.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
def atomic_json(path,value):
    path=private.plain(path);temp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex[:12])
    create_json(temp,value);os.replace(temp,path)
@contextmanager
def file_lock(path):
    path=private.plain(path)
    with path.open('r+b') as stream:
        if stream.read(2)!=b'0':raise ValueError('registry_lock_changed')
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)

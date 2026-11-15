"""Bounded single-task proof source; never /results, identity or token files."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading

from .terminal_proof import ProofUnavailable, reconstruct_record, raw_task, fail

def bounded_cli(executable, args, env, *, limit=16*1024*1024, timeout=30):
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    try:
        process = subprocess.Popen([str(executable), *args], env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, **options)
    except OSError: fail('proof_cli_unavailable')
    chunks=[]; count=0; oversized=threading.Event()
    def collect():
        nonlocal count
        while True:
            block=process.stdout.read(65536)
            if not block: break
            count+=len(block)
            if count>limit:
                oversized.set()
                try: process.terminate()
                except OSError: pass
                break
            chunks.append(block)
    reader=threading.Thread(target=collect,daemon=True);reader.start()
    try:
        process.wait(timeout=timeout);reader.join(timeout=2)
        if reader.is_alive(): fail('proof_cli_pipe_unclosed')
        if oversized.is_set(): fail('proof_cli_output_limit')
        if process.returncode: fail('proof_cli_rejected')
        return b''.join(chunks)
    except subprocess.TimeoutExpired: fail('proof_cli_timeout')
    finally:
        if process.poll() is None:
            process.kill();process.wait(timeout=5)  # only this owned Popen handle
        process.stdout.close()

class SingleTaskSource:
    """Fixed binary/schema and task-ID whitelist. All getters are read-only.

    The public nonce is read from the existing SQLite schema 3 in mode=ro.
    Ciphertext, identity/credential tables/files and full result lists are never
    queried. The reconstructed bytes still require actual CLI verification.
    """
    def __init__(self, executable, data_dir, *, local_aid, role, allowed_task,
                 binary_sha256, runner=bounded_cli):
        self.executable=Path(executable).resolve();self.data_dir=Path(data_dir).resolve()
        self.local_aid=local_aid;self.role=role;self.allowed_task=allowed_task
        self.binary_sha256=binary_sha256;self.runner=runner
        self.env=dict(os.environ,ANET_DATA_DIR=str(self.data_dir))

    def fetch(self, task_id):
        if not re.fullmatch('ix_[0-9a-f]{32}',str(task_id)) or not self.allowed_task(task_id):
            fail('task_not_owned_by_artifact_queue')
        if hashlib.sha256(self.executable.read_bytes()).hexdigest()!=self.binary_sha256:fail('anet_binary_changed')
        status=json.loads(self.runner(self.executable,['status','--json'],self.env,limit=524288))
        if status.get('aid')!=self.local_aid:fail('local_node_identity_changed')
        full=json.loads(self.runner(self.executable,['task','get',task_id,'--full','--history','100'],self.env))
        thread=json.loads(self.runner(self.executable,['thread',task_id],self.env))
        db=self.data_dir/'interactions'/'interactions.db'
        if not db.is_file():fail('public_task_nonce_store_unavailable')
        try:
            con=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=2)
            try:
                con.execute('PRAGMA query_only=ON')
                if con.execute('PRAGMA user_version').fetchone()[0]!=3:fail('interaction_schema_not_3')
                con.row_factory=sqlite3.Row
                row=con.execute('SELECT id,role,peer_aid,state,request_cid,result_cid,receipt_verified,task_nonce '
                                'FROM interaction WHERE id=?',(task_id,)).fetchone()
                if row is None:fail('public_task_row_missing')
                row=dict(row)
            finally:con.close()
        except sqlite3.Error:fail('public_task_nonce_read_unavailable')
        kel=None
        if self.role=='requester':
            value=raw_task(full).get('metadata',{}).get('anet.receipt',{}).get('provider_kel')
            if not isinstance(value,str):fail('requester_public_kel_unavailable')
            try:kel=base64.b64decode(value,validate=True)
            except ValueError:fail('requester_public_kel_encoding')
        record=reconstruct_record(full,thread,row,role=self.role,public_kel=kel)
        # /pull exports all stored attachments, not just the final signed ones.
        # Bound that compatibility path before asking the daemon to write files.
        messages=thread.get('thread',thread).get('messages',[])
        files=[f for m in messages for f in (m.get('attachments') or [])]
        if any(type(f.get('size')) is not int or f['size']<0 for f in files):fail('thread_attachment_size')
        record['local_pull_bytes']=sum(f['size'] for f in files)
        record['local_pull_count']=len(files)
        return full,record

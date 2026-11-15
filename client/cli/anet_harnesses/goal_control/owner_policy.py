"""Owner-local dedicated Codex source policy; never writable through service RPC.

Policy binds the exact ledger/source/native/workspace and directory identities.
The CLI and Goal controller cooperate on the same source lock. Same-OS-owner
code can invoke the owner CLI; this is a local ownership boundary, not remote CAS.
"""
from contextlib import contextmanager,closing
import datetime
import os
from pathlib import Path
import sqlite3

from anet_harnesses.adapters import identifier
from anet_harnesses.network import plain,private_existing,private_folder,read_private,write_private,exclusive
from anet_provider_harnesses.store import fingerprint
from .controller import GoalHold

SCHEMA='anet-goal-owner-policy/1'
class PolicyWriteUncertain(GoalHold):pass

def file_identity(path):
    info=path.stat();return [info.st_dev,info.st_ino]

def token(path):
    info=path.stat();return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_nlink)


class ReadOnlyLedger:
    """No schema creation, auth lookup, native process, or session adoption."""
    def __init__(self,state_root):
        self.path=plain(Path(state_root)/'harness.sqlite')

    def session(self,source_id,harness=None):
        private_existing(self.path.parent,True);private_existing(self.path)
        if self.path.stat().st_nlink!=1:raise GoalHold('owner_policy_ledger_not_ordinary')
        before=file_identity(self.path)
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=2)) as connection:
            connection.row_factory=sqlite3.Row
            row=connection.execute('SELECT * FROM sessions WHERE id=?',(source_id,)).fetchone()
        private_existing(self.path)
        if file_identity(self.path)!=before:raise GoalHold('owner_policy_ledger_replaced')
        if not row or harness and row['harness']!=harness:raise GoalHold('owner_policy_owned_codex_source_required')
        return dict(row)


class OwnerPolicy:
    def __init__(self,ledger,state_root):
        self.ledger=ledger;self.state_root=plain(Path(state_root));self.root=self.state_root/'goal-control-r7'

    def _binding(self,source_id,native_id=None,workspace=None):
        identifier(source_id);private_existing(self.state_root,True);private_existing(self.ledger.path)
        if self.ledger.path.parent!=self.state_root or self.ledger.path.stat().st_nlink!=1:raise GoalHold('owner_policy_ledger_root_differs')
        source=self.ledger.session(source_id,'codex')
        if source['transport']!='app-server/stdio':raise GoalHold('owner_policy_exclusive_stdio_source_required')
        if native_id is not None and identifier(native_id)!=source['native_id']:raise GoalHold('owner_policy_native_id_differs')
        if workspace is not None and str(plain(Path(workspace)))!=source['workspace']:raise GoalHold('owner_policy_workspace_differs')
        return {'native_id':source['native_id'],'workspace':source['workspace'],
                'state_root_identity':file_identity(self.state_root),'ledger_identity':file_identity(self.ledger.path)}

    def _path(self,source_id):return self.root/('owner-policy-'+fingerprint(source_id)+'.json')

    def _read(self,source_id):
        path=plain(self._path(source_id))
        if not path.exists():return None
        before=token(path);value=read_private(path)
        if token(path)!=before:raise GoalHold('owner_policy_changed_during_read')
        if (set(value)!={'schema','source_id','binding','granted','updated_at'} or value['schema']!=SCHEMA
            or value['source_id']!=source_id or type(value['granted']) is not bool):raise GoalHold('owner_policy_record_invalid')
        return value

    def approved(self,source_id):
        try:
            binding=self._binding(source_id);record=self._read(source_id)
            if record and record['binding']!=binding:raise GoalHold('owner_policy_source_binding_changed')
            return bool(record and record['granted'])
        except GoalHold:raise
        except Exception:raise GoalHold('owner_policy_private_binding_unverified') from None

    def __contains__(self,source_id):
        if source_id is None:return False
        return self.approved(source_id)

    def status(self,source_id,native_id=None,workspace=None):
        try:
            binding=self._binding(source_id,native_id,workspace);record=self._read(source_id)
            if record and record['binding']!=binding:raise GoalHold('owner_policy_source_binding_changed')
            granted=bool(record and record['granted'])
            return {'schema':SCHEMA,'state':'granted' if granted else 'not_granted','source_id':source_id,
                    'native_id':binding['native_id'],'binding_verified':True,'granted':granted,
                    'default_policy':'hold','requires_new_goal_ack':True,'authority':'local_owner_only',
                    'remote_grant_available':False}
        except GoalHold:raise
        except Exception:raise GoalHold('owner_policy_private_binding_unverified') from None

    @contextmanager
    def source_lock(self,source_id):
        private_folder(self.root);locks=self.root/'source-locks';private_folder(locks)
        folder=locks/fingerprint(identifier(source_id));private_folder(folder)
        try:
            with exclusive(folder):yield
        except GoalHold:raise
        except Exception:raise GoalHold('owner_policy_source_lock_busy_or_unverified') from None

    def set_local(self,source_id,native_id,workspace,*,granted,user_authorized=False):
        """Only called by owner_cli. No HarnessService action exposes this method."""
        if user_authorized is not True or type(granted) is not bool:raise GoalHold('explicit_local_owner_policy_action_required')
        self._binding(source_id,native_id,workspace)
        write_attempted=False
        try:
            with self.source_lock(source_id):
                binding=self._binding(source_id,native_id,workspace);prior=self._read(source_id)
                if prior and prior['binding']!=binding:raise GoalHold('owner_policy_source_binding_changed')
                value={'schema':SCHEMA,'source_id':source_id,'binding':binding,'granted':granted,
                       'updated_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
                # Include write/readback AND lock release in this uncertainty
                # boundary: none of their failures proves a rejected policy.
                write_attempted=True
                write_private(self._path(source_id),value)
                result=self.status(source_id,native_id,workspace)
            if not granted:result['state']='revoked'
            return result
        except Exception:
            if write_attempted:raise PolicyWriteUncertain('owner_policy_write_outcome_unverified') from None
            raise

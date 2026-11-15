"""Bounded ANet account connection; tokens enter only through stdin JSON.

This implements the portal's existing two-phase pairing protocol. It never runs
connector setup/configure/install, changes peer trust, opens inbound policy,
sets payment limits, imports a key, or selects another node identity or Hub.
"""
import argparse
from contextlib import contextmanager
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError

PORTAL='https://124.222.173.36:8888'
HUB='https://124.222.173.36'
SCHEMA='anet-network/1'
LIMIT=262144


class NetworkError(RuntimeError):
    def __init__(self,code):self.code=code;super().__init__(code)
class RemoteFailure(NetworkError):
    def __init__(self,status):self.status=status;super().__init__('portal_request_unverified')
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise NetworkError('portal_redirect_refused')


def plain(path):
    path=Path(path)
    if not path.is_absolute():raise NetworkError('runtime_path_invalid')
    for item in (path,*path.parents):
        if item.exists():
            info=item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:raise NetworkError('private_path_invalid')
    return path.resolve()


def private_existing(path,directory=False):
    """Accept existing connector owner-only ACL or owner+SYSTEM; never relax it."""
    path=plain(path)
    if directory and not path.is_dir() or not directory and not path.is_file():raise NetworkError('private_state_unavailable')
    if os.name!='nt':
        info=path.stat()
        if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:raise NetworkError('private_acl_invalid')
        return path
    from . import ipc_windows as win
    class ACL_INFO(C.Structure):
        _fields_=[('count',win.W.DWORD),('used',win.W.DWORD),('free',win.W.DWORD)]
    def sid_text(pointer):
        value=win.W.LPWSTR();win.check(win.ConvertSidToStringSidW(pointer,C.byref(value)))
        try:return value.value
        finally:win.LocalFree(C.cast(value,C.c_void_p))
    owner=C.c_void_p();acl=C.c_void_p();descriptor=C.c_void_p()
    error=win.GetNamedSecurityInfoW(win.filesystem_path(path),1,5,C.byref(owner),None,C.byref(acl),None,C.byref(descriptor))
    if error:raise NetworkError('private_acl_unverified')
    try:
        sid=win.owner_sid()
        # Historical elevated installs can give Administrators ownership to a
        # file whose inherited DACL still grants only this exact user. Accept
        # that owner only after the same strict ACE checks below; do not change
        # ownership or ACLs. Group/Everyone/other-user access is still refused.
        if not owner or not acl or sid_text(owner) not in (sid,'S-1-5-32-544'):raise NetworkError('private_owner_mismatch')
        control=win.W.WORD();revision=win.W.DWORD();win.check(win.GetSecurityDescriptorControl(descriptor,C.byref(control),C.byref(revision)))
        if directory and not control.value&0x1000:raise NetworkError('private_acl_not_protected')
        info=ACL_INFO();win.check(win.GetAclInformation(acl,C.byref(info),C.sizeof(info),2));principals=[]
        for i in range(info.count):
            ace=C.c_void_p();win.check(win.GetAce(acl,i,C.byref(ace)));header=C.string_at(ace,8)
            if header[0]!=0 or header[1]&~0x13 or int.from_bytes(header[4:8],'little')!=0x1f01ff:raise NetworkError('private_acl_invalid')
            principal=sid_text(ace.value+8)
            if principal not in (sid,'S-1-5-18'):raise NetworkError('private_acl_invalid')
            principals.append(principal)
        if sid not in principals:raise NetworkError('private_acl_invalid')
    finally:win.LocalFree(descriptor)
    return path


def private_folder(path):
    path=plain(path)
    if path.exists():return private_existing(path,True)
    if os.name=='nt':
        from .ipc_windows import create_private_directory
        create_private_directory(path)
    else:path.mkdir(mode=0o700)
    return private_existing(path,True)


def read_private(path):
    path=Path(path)
    if not path.exists():return None
    private_existing(path.parent,True);private_existing(path)
    if path.stat().st_nlink!=1 or path.stat().st_size>LIMIT:raise NetworkError('private_state_invalid')
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict):raise NetworkError('private_state_invalid')
    return value


def new_private_fd(path):
    if os.name!='nt':return os.open(path,os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
    # Elevated Windows tokens can otherwise create a file owned by the local
    # Administrators group even inside a protected directory. Set the exact
    # current-user owner and protected DACL atomically at CREATE_NEW.
    from . import ipc_windows as win
    import msvcrt
    sid=win.owner_sid();descriptor=C.c_void_p()
    win.check(win.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        'O:'+sid+'D:P(A;;FA;;;SY)(A;;FA;;;'+sid+')',1,C.byref(descriptor),None))
    security=win.SA(C.sizeof(win.SA),descriptor,False)
    try:
        # CreateFileW does not inherit Python's long-path handling. Keep the
        # already validated absolute owner-private path, including UNC paths.
        handle=win.CreateFileW(win.filesystem_path(path),0xC0000000,7,C.byref(security),1,0x80,None)
        if handle==win.INVALID:raise C.WinError(C.get_last_error())
        try:return msvcrt.open_osfhandle(handle,os.O_RDWR|os.O_BINARY)
        except BaseException:win.CloseHandle(handle);raise
    finally:win.LocalFree(descriptor)


def write_private(path,value):
    path=Path(path);private_folder(path.parent)
    if path.exists():private_existing(path)
    raw=json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8')
    if len(raw)>LIMIT:raise NetworkError('private_state_too_large')
    temporary=path.parent/('.network-'+secrets.token_hex(16));fd=new_private_fd(temporary)
    try:
        if os.name!='nt':os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        private_existing(temporary);os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


@contextmanager
def exclusive(folder):
    private_folder(folder);path=folder/'network.lock'
    try:descriptor=new_private_fd(path)
    except FileExistsError:descriptor=os.open(path,os.O_RDWR)
    with os.fdopen(descriptor,'r+b') as stream:
        private_existing(path)
        if path.stat().st_size==0:stream.write(b'0');stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise NetworkError('connection_operation_in_progress') from None
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream,fcntl.LOCK_UN)


class PortalClient:
    def __init__(self, portal=PORTAL): self.portal=portal
    def __call__(self,path,payload,credential=None):
        if path not in ('/api/node/info','/api/pair/info','/api/pair/prepare','/api/pair/complete'):
            raise NetworkError('portal_operation_refused')
        headers={'Content-Type':'application/json','Accept':'application/json'}
        if credential:
            if not isinstance(credential,str) or any(c.isspace() for c in credential):raise NetworkError('credential_invalid')
            headers['Authorization']='Bearer '+credential
        request=Request(self.portal+path,data=json.dumps(payload,ensure_ascii=False).encode(),headers=headers,method='POST')
        try:
            with build_opener(NoRedirect).open(request,timeout=15) as response:raw=response.read(LIMIT+1)
        except HTTPError as error:
            error.close();raise RemoteFailure(error.code) from None
        except (OSError,TimeoutError):raise NetworkError('portal_connection_unverified') from None
        if len(raw)>LIMIT:raise NetworkError('portal_response_too_large')
        value=json.loads(raw)
        if not isinstance(value,dict):raise NetworkError('portal_response_invalid')
        return value


class NativeRunner:
    def __init__(self,runtime):
        self.root=plain(runtime['anet_root']);taskd=plain(runtime.get('node_root',self.root/'taskd'));self.hub=runtime.get('hub',HUB)
        if taskd!=self.root/'taskd':raise NetworkError('runtime_node_mismatch')
        binary=Path(runtime['anet_binary']) if runtime.get('anet_binary') else self.root/'bin'/('anet.exe' if os.name=='nt' else 'anet')
        config=taskd/'config.json'
        if config.is_file():
            value=json.loads(config.read_text(encoding='utf-8')).get('transport',{})
            if value.get('data_dir') and plain(value['data_dir'])!=self.root:raise NetworkError('runtime_node_mismatch')
            if value.get('binary') and not runtime.get('anet_binary'):binary=plain(value['binary'])
        if not binary.is_file() or (os.name=='nt' and binary.suffix.lower()!='.exe'):raise NetworkError('node_runtime_unavailable')
        self.binary=str(binary);self.env=dict(os.environ,ANET_DATA_DIR=str(self.root))
    def run(self,*args):
        if not args or args[0] not in ('status','up','hub-register','profile','peers'):raise NetworkError('node_operation_refused')
        if args[0]=='status' and args!=('status','--json'):raise NetworkError('node_operation_refused')
        if args[0]=='peers' and args!=('peers','list','--json'):raise NetworkError('node_operation_refused')
        if args[0]=='up' and args!=('up',):raise NetworkError('node_operation_refused')
        if args[0]=='hub-register' and (len(args)!=5 or args[1]!=self.hub or args[2]!='--name' or args[4]!='--passphrase-later'):raise NetworkError('node_operation_refused')
        if args[0]=='profile' and (len(args) not in (4,6) or args[1:3]!=('set','--summary') or len(args)==6 and args[4]!='--readme'):raise NetworkError('node_operation_refused')
        try:result=subprocess.run([self.binary,*args],env=self.env,capture_output=True,timeout=40,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        except (OSError,TimeoutError):raise NetworkError('node_operation_unverified') from None
        if result.returncode:raise NetworkError('node_operation_unverified')
        if args[0] in ('status','peers'):
            if len(result.stdout)>LIMIT:raise NetworkError('node_response_invalid')
            value=json.loads(result.stdout)
            if not isinstance(value,dict):raise NetworkError('node_response_invalid')
            return value
        return None
    def status(self):return self.run('status','--json')


class Network:
    def __init__(self,runtime,runner=None,remote=None,sleep=time.sleep):
        if runtime.get('schema')!='anet-desktop-runtime/1':raise NetworkError('runtime_schema_invalid')
        self.root=plain(runtime['anet_root']);self.folder=self.root/'portal';self.portal=runtime.get('portal',PORTAL);self.hub=runtime.get('hub',HUB)
        self.state_file=self.folder/'connection.json';self.pending_file=self.folder/'pair-pending.json'
        self.runner=runner or NativeRunner(runtime);self.remote=remote or PortalClient(self.portal);self.sleep=sleep
    def _state(self):
        state=read_private(self.state_file);pending=read_private(self.pending_file)
        for saved in (state,pending):
            if saved and saved.get('portal','').rstrip('/')!=self.portal:raise NetworkError('existing_portal_preserved')
        return state,pending
    def _local(self):
        value=self.runner.status();aid=value.get('aid')
        if not isinstance(aid,str) or not aid or len(aid)>256:raise NetworkError('node_identity_unavailable')
        hub=value.get('hub_url') or (value.get('hub') or {}).get('url')
        if hub and hub.rstrip('/')!=self.hub:raise NetworkError('existing_hub_preserved')
        return value
    def _authenticate(self,saved,aid):
        if saved.get('aid')!=aid:raise NetworkError('existing_identity_preserved')
        token=saved.get('node_token')
        if not isinstance(token,str) or not token:raise NetworkError('credential_unavailable')
        try:info=self.remote('/api/node/info',{},token)
        except RemoteFailure as error:
            if error.status in (401,403):return None
            raise
        if info.get('aid')!=aid or info.get('agent_id')!=saved.get('agent_id'):
            raise NetworkError('existing_identity_preserved')
        return {**saved,**info,'portal':self.portal,'aid':aid,'anet':getattr(self.runner,'binary','anet')}
    def _view(self,local,state,paired=False,pending=False,reason=None):
        connected=local.get('hub',{}).get('state')=='registered'
        if state=='connected' and not connected:state='hub_registration_pending'
        return {'schema':SCHEMA,'state':state,'hub_connected':connected,'portal_paired':paired,
                'pending':pending,'reason':reason or REASONS.get(state,'连接尚未核验，请保留原连接并刷新状态。'),
                'identity_changed':False,'peer_policy_changed':False,'payment_policy_changed':False,
                'portal':self.portal}
    def status(self):
        local=self._local()
        try:
            state,pending=self._state()
            for saved in (state,pending):
                if saved and saved.get('aid')!=local['aid']:raise NetworkError('existing_identity_preserved')
            if state and self._authenticate(state,local['aid']):
                return self._view(local,'paired_restore_pending' if pending and pending.get('challenge') else 'connected',True,bool(pending))
            return self._view(local,'recovery_required' if state or pending else 'needs_pairing',False,bool(pending))
        except Exception as error:
            code=error.code if isinstance(error,NetworkError) else 'portal_connection_unverified'
            result=self._view(local,'needs_attention',False,False,REASONS.get(code,'账户连接尚未通过核验；Hub 状态已单独检查。'))
            result['reason_code']=code;return result
    def _restore(self,pending):
        if not pending or not pending.get('challenge'):return True
        try:
            current=self._local()
            if current.get('profile',{}).get('summary','')==pending['challenge']:
                # Preserve a readme independently edited since pairing started.
                readme=current.get('readme',pending.get('original_readme',''))
                self.runner.run('profile','set','--summary',pending.get('original_summary',''),'--readme',readme)
            return True
        except Exception:return False
    def _finish(self,value,pending,local):
        saved={key:value[key] for key in ('agent_id','node_token','portal','name','kind','executor','aid','anet') if key in value}
        write_private(self.state_file,saved)
        restored=self._restore(pending)
        if restored:self.pending_file.unlink(missing_ok=True)
        return self._view(self._local(),'connected' if restored else 'paired_restore_pending',True,not restored)
    def pair(self,token=None,resume=False):
        if token is not None and (not isinstance(token,str) or not token or len(token)>4096 or any(c.isspace() for c in token)):
            raise NetworkError('credential_invalid')
        with exclusive(self.folder):
            state,pending=self._state()
            if resume and not (state or pending):raise NetworkError('no_saved_connection')
            local=self._local()
            if local.get('hub',{}).get('state')!='registered':self.runner.run('up');local=self._local()
            aid=local['aid']
            for saved in (state,pending):
                if saved and saved.get('aid')!=aid:raise NetworkError('existing_identity_preserved')
            if pending:
                recovered=self._authenticate(pending,aid)
                if recovered:return self._finish(recovered,pending,local)
            if state:
                active=self._authenticate(state,aid)
                if active:
                    # Already connected: verify the original credential in
                    # place. Do not rewrite its bytes, owner or ACL.
                    restored=self._restore(pending)
                    if pending and restored:self.pending_file.unlink(missing_ok=True)
                    return self._view(local,'connected' if restored else 'paired_restore_pending',True,not restored)
            if pending and pending.get('hub_register_requested') and local.get('hub',{}).get('state')!='registered':
                return self._view(local,'hub_registration_pending',False,True)
            credential=None;info=None
            if pending:
                try:info=self.remote('/api/pair/info',{'token':pending['pair_token']});credential=pending['pair_token']
                except RemoteFailure as error:
                    if error.status!=404:raise
                    if resume:raise NetworkError('pair_credential_expired') from None
            if not info:
                if resume or not token:raise NetworkError('pair_credential_required')
                credential=token;info=self.remote('/api/pair/info',{'token':credential})
            if info.get('pair_protocol')!=2:raise NetworkError('pair_protocol_unavailable')
            if info.get('hub','').rstrip('/')!=self.hub:raise NetworkError('existing_hub_preserved')
            if not all(isinstance(info.get(k),str) and info[k] for k in ('agent_id','name','kind','executor')):raise NetworkError('portal_response_invalid')
            for saved in (state,pending):
                if saved and saved.get('agent_id')!=info['agent_id']:raise NetworkError('existing_connection_preserved')
            if info.get('expected_aid') and info['expected_aid']!=aid:raise NetworkError('existing_identity_preserved')
            if info['kind']=='agent':
                peers=self.runner.run('peers','list','--json')
                if peers.get('allow') or peers.get('trust') or peers.get('public_capabilities'):raise NetworkError('existing_peer_policy_preserved')
            if pending and not self._restore(pending):raise NetworkError('profile_restore_pending')
            local=self._local()
            pending={'portal':self.portal,'aid':aid,'agent_id':info['agent_id'],'pair_token':credential,
                     'node_token':pending['node_token'] if pending else secrets.token_urlsafe(32),
                     'name':info['name'],'kind':info['kind'],'executor':info['executor'],'anet':getattr(self.runner,'binary','anet'),
                     'original_summary':local.get('profile',{}).get('summary',''),'original_readme':local.get('readme','')}
            write_private(self.pending_file,pending)
            proof=self.remote('/api/pair/prepare',{'token':credential,'aid':aid})
            if not isinstance(proof.get('challenge'),str) or not proof['challenge'] or len(proof['challenge'])>2048:raise NetworkError('portal_response_invalid')
            pending['challenge']=proof['challenge'];write_private(self.pending_file,pending)
            if local.get('hub',{}).get('state')!='registered':
                pending['hub_register_requested']=True;write_private(self.pending_file,pending)
                self.runner.run('hub-register',self.hub,'--name',info['name'],'--passphrase-later')
                for _ in range(20):
                    local=self._local()
                    if local.get('hub',{}).get('state')=='registered':break
                    self.sleep(1)
                else:return self._view(local,'hub_registration_pending',False,True)
            try:
                self.runner.run('profile','set','--summary',pending['challenge'])
                for attempt in range(5):
                    try:
                        result=self.remote('/api/pair/complete',{'token':credential,'aid':aid,'node_token':pending['node_token']})
                        break
                    except RemoteFailure as error:
                        # A structured conflict response proves this attempt did
                        # not commit. Allow bounded signed-profile propagation.
                        if error.status==409 and attempt<4:self.sleep(1);continue
                        result=self._authenticate(pending,aid)
                        if not result:raise
                        break
                    except Exception:
                        # An unknown response is reconciled by the precommitted
                        # same node credential, never by another connection.
                        result=self._authenticate(pending,aid)
                        if not result:raise
                        break
                if result.get('node_token')!=pending['node_token']:raise NetworkError('credential_ack_mismatch')
                if result.get('agent_id')!=pending['agent_id'] or result.get('aid',aid)!=aid:raise NetworkError('existing_connection_preserved')
                return self._finish(dict(result,portal=self.portal,aid=aid,anet=getattr(self.runner,'binary','anet')),pending,local)
            finally:
                if self.pending_file.exists():self._restore(pending)


REASONS={
 'connected':'ANet 网络与账户连接已核验。','needs_pairing':'请打开 ANet 账户，创建或恢复原连接，再粘贴接入凭证。',
 'recovery_required':'原连接需要恢复，请保留同一身份并从原连接详情继续。','paired_restore_pending':'账户已绑定，临时资料恢复仍待核对，请继续恢复原连接。',
 'hub_registration_pending':'Hub 注册尚未确认，原身份与连接进度已保存。','pair_credential_required':'请粘贴账户页面提供的接入或恢复凭证。',
 'pair_credential_expired':'原接入凭证已失效，请在原连接详情获取恢复凭证。','existing_identity_preserved':'本地身份与连接记录不匹配，已保留原身份并停止连接。',
 'existing_hub_preserved':'检测到另一 Hub 或非本安装网络地址，已保留原配置。','existing_portal_preserved':'检测到另一账户平台，已保留原连接。',
 'existing_connection_preserved':'凭证属于另一连接，请从原连接详情恢复。','existing_peer_policy_preserved':'原节点已有接收或信任策略，需要保留并由原管理员核对。',
 'no_saved_connection':'尚无可恢复进度，请先使用账户页面的接入凭证连接。','private_acl_invalid':'账户连接目录的私有权限未通过核验。',
 'connection_operation_in_progress':'原连接操作仍在核对，请稍后刷新。','portal_connection_unverified':'网络连接尚未核验，请稍后恢复原进度。'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--runtime',required=True);args=parser.parse_args()
    try:
        raw=sys.stdin.buffer.readline(LIMIT+1)
        if len(raw)>LIMIT:raise NetworkError('request_too_large')
        request=json.loads(raw)
        if not isinstance(request,dict) or set(request)-{'action','token'}:raise NetworkError('request_invalid')
        action=request.get('action','status')
        if action not in ('status','pair','resume'):raise NetworkError('request_invalid')
        if action!='pair' and request.get('token'):raise NetworkError('request_invalid')
        runtime=json.loads(plain(args.runtime).read_text(encoding='utf-8-sig'))
        network=Network(runtime)
        result=network.status() if action=='status' else network.pair(request.get('token'),resume=action=='resume')
    except BaseException as error:
        code=error.code if isinstance(error,NetworkError) else 'connection_unverified'
        result={'schema':SCHEMA,'state':'needs_attention','hub_connected':False,'portal_paired':False,'reason':REASONS.get(code,'连接尚未核验，原身份与进度已保留。'),
                'reason_code':code,'portal':PORTAL,'identity_changed':False,'peer_policy_changed':False,'payment_policy_changed':False}
    print(json.dumps(result,ensure_ascii=False,separators=(',',':')),flush=True)


if __name__=='__main__':main()

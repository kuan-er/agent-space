"""RAM-only WorkBuddy access via the explicitly paired ANet node token broker.

No OAuth app secret or refresh token belongs on this desktop. The existing node
credential is read in place under the network module's private ACL checks, and
its AID/connection is verified before requesting a short-lived access token.
"""
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from urllib.request import Request,build_opener
from urllib.error import HTTPError

from .network import PORTAL,HUB,NativeRunner,NoRedirect,NetworkError,read_private,plain
from .transport import ProtocolError

CLOUD_SCOPES=frozenset(('user.task.readable','user.task.invokable'))
LOCAL_SCOPES=frozenset(('user.localassistant.readable','user.localassistant.invokable'))


class AuthorizationRequired(ProtocolError):
    def __init__(self,reason_code='needs_authorization',*,surface='cloud'):
        self.reason_code=reason_code
        reasons=LOCAL_REASONS if surface=='local' else REASONS
        super().__init__(reasons.get(reason_code,reasons['needs_authorization']))


class BrokerHTTPError(ProtocolError):
    def __init__(self,status):self.status=status;super().__init__('WorkBuddy node authorization broker refused the read-only request')


def broker_request(path,payload,node_token):
    if path not in ('/api/node/info','/api/node/workbuddy/token'):raise AuthorizationRequired('invalid_broker_request')
    if not isinstance(node_token,str) or not node_token or len(node_token)>8192 or any(c.isspace() for c in node_token):
        raise AuthorizationRequired('needs_pairing')
    if path=='/api/node/info' and payload!={}:raise AuthorizationRequired('invalid_broker_request')
    if path=='/api/node/workbuddy/token':
        if set(payload)-{'rejected_token_sha256'}:raise AuthorizationRequired('invalid_broker_request')
        digest=payload.get('rejected_token_sha256')
        if digest is not None and (not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)):
            raise AuthorizationRequired('invalid_broker_request')
    request=Request(PORTAL+path,data=json.dumps(payload).encode(),method='POST',
        headers={'Authorization':'Bearer '+node_token,'Content-Type':'application/json','Accept':'application/json'})
    try:
        with build_opener(NoRedirect).open(request,timeout=15) as response:raw=response.read(65537)
    except HTTPError as error:
        error.close();raise BrokerHTTPError(error.code) from None
    except (OSError,TimeoutError):raise AuthorizationRequired('broker_unavailable') from None
    if len(raw)>65536:raise AuthorizationRequired('invalid_broker_response')
    try:value=json.loads(raw)
    except (ValueError,UnicodeError):raise AuthorizationRequired('invalid_broker_response') from None
    if not isinstance(value,dict):raise AuthorizationRequired('invalid_broker_response')
    return value


class PortalWorkBuddyTokenBroker:
    def __init__(self,node_root,runner=None,request=None,clock=time.time,*,surface='local'):
        if surface not in ('local','cloud'):raise ValueError('Unknown WorkBuddy authorization surface')
        self.surface=surface
        self.node_root=plain(node_root)
        if self.node_root.name.lower()!='taskd':raise AuthorizationRequired('needs_pairing')
        self.anet_root=self.node_root.parent
        self.connection_file=self.anet_root/'portal/connection.json'
        self.runner=runner or NativeRunner({'anet_root':str(self.anet_root),'node_root':str(self.node_root)})
        self.request=request or broker_request;self.clock=clock;self.lock=threading.RLock()
        self.binding=None;self.access_token=None;self.expires_at=0;self.scope=frozenset()
        self.last_state='authorization_unverified'

    def _connection(self):
        try:connection=read_private(self.connection_file)
        except (NetworkError,OSError,ValueError):raise AuthorizationRequired('connection_unverified') from None
        if not connection:raise AuthorizationRequired('needs_pairing')
        if connection.get('portal','').rstrip('/')!=PORTAL:raise AuthorizationRequired('connection_mismatch')
        token=connection.get('node_token')
        if not isinstance(token,str) or not token or any(c.isspace() for c in token):raise AuthorizationRequired('needs_pairing')
        try:local=self.runner.status()
        except Exception:raise AuthorizationRequired('node_identity_unverified') from None
        if not isinstance(local,dict) or not local.get('aid') or local['aid']!=connection.get('aid'):
            raise AuthorizationRequired('connection_mismatch')
        hub=local.get('hub_url') or local.get('hub',{}).get('url')
        if hub and hub.rstrip('/')!=HUB:raise AuthorizationRequired('connection_mismatch')
        pin={'aid':connection['aid'],'agent_id':connection.get('agent_id'),'portal':PORTAL}
        if not isinstance(pin['agent_id'],str) or not pin['agent_id']:raise AuthorizationRequired('connection_unverified')
        if self.binding and pin!=self.binding:raise AuthorizationRequired('connection_mismatch')
        self.binding=pin
        return connection

    def paired_status(self):
        """Local pairing metadata only: never fetch a token or infer OAuth."""
        try:self._connection();state='paired_authorization_unverified';paired=True
        except AuthorizationRequired as error:state=error.reason_code;paired=False
        return public_status(state,paired=paired,surface=self.surface)

    def get(self,force=False,rejected=None):
        with self.lock:
            connection=self._connection()
            if not force and rejected is None and self.access_token and self.expires_at>self.clock()+30:
                return self.access_token
            try:
                observed=self.request('/api/node/info',{},connection['node_token'])
                if observed.get('aid')!=connection['aid'] or observed.get('agent_id')!=connection['agent_id']:
                    raise AuthorizationRequired('connection_mismatch')
                payload={'rejected_token_sha256':hashlib.sha256(rejected.encode()).hexdigest()} if rejected else {}
                value=self.request('/api/node/workbuddy/token',payload,connection['node_token'])
            except BrokerHTTPError as error:
                self.access_token=None;self.expires_at=0
                code='needs_authorization' if error.status in (401,403,409,412) else 'broker_unavailable'
                self.last_state=code;raise AuthorizationRequired(code) from None
            token=value.get('access_token');expiry=value.get('expires_at');scope=value.get('scope')
            if (not isinstance(token,str) or not token or len(token)>65536 or any(c.isspace() for c in token)
                    or type(expiry) is not int or not self.clock()+5<expiry<self.clock()+31*86400):
                self.last_state='invalid_broker_response';raise AuthorizationRequired(self.last_state)
            scopes=frozenset(scope.split()) if isinstance(scope,str) else frozenset()
            required=LOCAL_SCOPES if self.surface=='local' else CLOUD_SCOPES
            declaration='local_assistant_scope' if self.surface=='local' else 'cloud_tasks_scope'
            if value.get(declaration) is not True or not required.issubset(scopes):
                self.access_token=None;self.expires_at=0;self.scope=scopes;self.last_state='needs_scope'
                raise AuthorizationRequired('needs_scope',surface=self.surface)
            self.access_token=token;self.expires_at=expiry;self.scope=scopes;self.last_state='authorization_available_unverified'
            return token

    def __call__(self):return self.get()
    def refresh(self,rejected):
        if not isinstance(rejected,str) or not rejected:raise AuthorizationRequired('needs_authorization')
        return self.get(force=True,rejected=rejected)

    def status(self,refresh=True):
        paired=self.paired_status()
        if not paired['paired_locally']:return paired
        try:
            self.get(force=refresh)
            return public_status('authorization_available_unverified',paired=True,token=True,scope=True,expires_at=self.expires_at,surface=self.surface)
        except AuthorizationRequired as error:return public_status(error.reason_code,paired=True,surface=self.surface)
        except Exception:return public_status('broker_unavailable',paired=True,surface=self.surface)


class EnvironmentToken:
    """Explicit advanced override; no file discovery or OAuth verification claim."""
    def __init__(self,name):self.name=name
    def __call__(self):
        value=os.environ.get(self.name)
        if not isinstance(value,str) or not value or any(c.isspace() for c in value):raise AuthorizationRequired('needs_authorization')
        return value
    def refresh(self,rejected):return self()


def public_status(state,paired=False,token=False,scope=False,expires_at=None,surface='cloud'):
    if surface not in ('local','cloud'):raise ValueError('Unknown WorkBuddy authorization surface')
    reasons=LOCAL_REASONS if surface=='local' else REASONS
    result={'schema':'anet-workbuddy-auth/1','state':state,'auth_source':'portal_node_broker',
            'paired_locally':paired,'oauth_token_available':token,'oauth_verified_by_portal':token,
            'cloud_scope_declared':scope and surface=='cloud','cloud_api_verified':False,'cloud_task_e2e_verified':False,
            'local_scope_declared':scope and surface=='local','local_api_verified':False,'local_task_e2e_verified':False,
            'surface':'workbuddy_local_assistant' if surface=='local' else 'workbuddy_cloud_tasks',
            'local_desktop_session_verified':False,'native_terminal_verified':False,
            'credentials_persisted':False,'reason':reasons.get(state,reasons['broker_unavailable'])}
    if expires_at is not None:result['expires_at']=expires_at
    return result


REASONS={
 'needs_pairing':'请先连接 ANet 账户，再在该连接详情中授权 WorkBuddy。',
 'paired_authorization_unverified':'ANet 配对已找到；WorkBuddy 网页授权与云端接口仍待核验。',
 'environment_override_unverified':'已提供高级环境变量覆盖；OAuth 与 WorkBuddy 云端接口仍待核验。',
 'needs_authorization':'请在 ANet 网页的原连接详情中完成或更新 WorkBuddy 授权。',
 'needs_scope':'当前 WorkBuddy 授权没有确认云端任务权限，请在原连接详情中明确授权云端任务。',
 'authorization_available_unverified':'已取得门户授权的短期凭据；云端任务与回传还需实际验收。',
 'connection_unverified':'原 ANet 连接凭据的私有权限或记录尚未核验，已保留原连接。',
 'node_identity_unverified':'原 ANet 节点身份尚未核验，请先核对网络连接。',
 'connection_mismatch':'节点身份或原连接不匹配，已停止读取授权并保留原身份。',
 'broker_unavailable':'WorkBuddy 授权服务暂时不可用；已有任务不会自动重发。',
 'invalid_broker_response':'WorkBuddy 授权响应尚未通过核验。',
 'invalid_broker_request':'授权请求超出固定接口。'}

LOCAL_REASONS=dict(REASONS,**{
 'paired_authorization_unverified':'ANet 配对已找到；WorkBuddy 本机助理授权与在线状态仍待核验。',
 'environment_override_unverified':'已提供高级环境变量覆盖；WorkBuddy 本机接口仍待核验。',
 'needs_scope':'当前授权尚未确认本机助理权限，请在原连接详情中明确授权本机助理。',
 'authorization_available_unverified':'已取得本机助理短期授权；PC 在线、实际执行与结果仍需验收。'})

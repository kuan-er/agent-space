"""Account sign-in and recoverable enrollment. Passwords and cookies stay in memory."""
import json
import re
import secrets
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPCookieProcessor

from .network import (Network, NativeRunner, NetworkError, RemoteFailure, NoRedirect,
                      read_private, write_private, exclusive, plain, PORTAL, HUB, LIMIT)
from . import service


def origin(value):
    if not isinstance(value,str) or any(c.isspace() for c in value):raise NetworkError('portal_invalid')
    p=urlsplit(value)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.path not in ('','/') or p.query or p.fragment:
        raise NetworkError('portal_invalid')
    try:p.port
    except ValueError:raise NetworkError('portal_invalid') from None
    return 'https://'+p.netloc.lower()


class Session:
    def __init__(self,portal):
        self.portal=origin(portal);self.csrf=None
        self.opener=build_opener(NoRedirect,HTTPCookieProcessor(CookieJar()))
    def request(self,path,body=None):
        if not (path in ('/api/auth/login','/api/auth/logout','/api/me','/api/agents','/api/devices','/api/client/enroll','/api/client/config') or
                path == '/api/client/receiver' and isinstance(body, dict) and
                set(body) == {'agent_id','aid','executor','confirm'} and re.fullmatch(r'[a-f0-9]{24}', str(body.get('agent_id',''))) and
                isinstance(body.get('aid'),str) and 0 < len(body['aid']) <= 256 and
                body.get('executor') in ('codex','claude','deepseek') and body.get('confirm') is True):
            raise NetworkError('portal_operation_refused')
        h={'Accept':'application/json','Content-Type':'application/json','Origin':self.portal}
        if self.csrf:h['X-CSRF-Token']=self.csrf
        req=Request(self.portal+path,data=json.dumps(body).encode() if body is not None else None,headers=h,method='POST' if body is not None else 'GET')
        try:
            with self.opener.open(req,timeout=20) as response:raw=response.read(LIMIT+1)
        except HTTPError as e:
            status=e.code;e.close()
            raise RemoteFailure(status) from None
        except (OSError,URLError,TimeoutError):raise NetworkError('portal_unavailable') from None
        if len(raw)>LIMIT:raise NetworkError('portal_response_invalid')
        try:value=json.loads(raw)
        except (ValueError,UnicodeError):raise NetworkError('portal_response_invalid') from None
        if not isinstance(value,dict):raise NetworkError('portal_response_invalid')
        return value
    def login(self,username,password):
        self.request('/api/auth/login',{'username':username,'password':password})
        me=self.request('/api/me');self.csrf=me.get('csrf')
        if not isinstance(self.csrf,str) or not self.csrf or not me.get('id'):raise NetworkError('portal_response_invalid')
        return me
    def close(self):
        if self.csrf:
            try:self.request('/api/auth/logout',{})
            except Exception:pass
        self.csrf=None


MESSAGES={
 'portal_invalid':'请输入 HTTPS 门户地址，例如 https://124.222.173.36:8888。',
 'portal_unavailable':'暂时无法连接门户，请检查地址和网络后重试。',
 'login_invalid':'请输入账户和密码。',
 'login_failed':'账户或密码不正确，或账户当前不可用。',
 'account_mismatch':'这台电脑已绑定其他账户。请使用原账户登录。',
 'existing_portal_preserved':'这台电脑已连接其他门户，请先使用原门户登录。',
 'enrollment_unverified':'连接尚未完成，进度已保存。请用同一账户再次登录继续。',
 'enrollment_not_supported':'此门户尚不支持客户端登录连接，请先更新门户服务。',
 'request_invalid':'配置请求无效。',
 'connection_operation_in_progress':'正在连接，请稍后重试。',
 'private_acl_invalid':'账户配置的访问权限需要修复。',
 'node_runtime_unavailable':'ANet 服务尚未安装，请先完成安装。',
 'node_operation_unverified':'ANet 服务尚未就绪，请在服务页面启动后重试。',
 'existing_hub_preserved':'门户与本机网络设置不一致，原连接已保留。',
 'companion_missing':'连接服务组件缺失，请重新安装 ANet 客户端。',
 'companion_install_unverified':'账户已连接，后台连接服务尚未启动。请检查系统登录启动或用户服务设置后重试。',
}


def _view(account,connection,message=None,companion=None):
    detail={
      'connected':'账户连接正常。',
      'needs_pairing':'登录账户后，这台电脑会自动连接 ANet。',
      'recovery_required':'请用原账户重新登录以恢复连接。',
      'hub_registration_pending':'正在连接网络，进度已保存。稍后刷新即可。',
      'paired_restore_pending':'账户已连接，正在完成本机设置。',
    }.get(connection.get('state'),'连接尚未就绪，请检查网络和服务状态。')
    ready=connection.get('portal_paired') is True and connection.get('hub_connected') is True
    if companion is not None:ready=ready and companion.get('running') is True
    state='ready' if ready else 'needs_attention'
    return {'schema':'anet-account/1','state':state,'message':message or ('账户已连接。请在智能体设置中完成 MCP 配置。' if ready else (companion or {}).get('detail',detail)),
            'account':{'url':account.get('portal',PORTAL),'username':account.get('username',''),'state':'connected' if ready else 'disconnected'},
            'connection':{'state':connection.get('state','unknown'),'detail':detail,'hub_connected':connection.get('hub_connected',False),'portal_paired':connection.get('portal_paired',False)},'service':companion}


def execute(request,context,*,session_factory=Session,network_factory=Network,runner_factory=NativeRunner,service_api=service):
    """JSON-safe request boundary; never returns raw server errors, passwords or tokens."""
    account={};session=None
    try:
        if not isinstance(request,dict) or not isinstance(context,dict):raise NetworkError('request_invalid')
        root=plain(context['anet_root']);folder=root/'portal';account_path=folder/'account.json';enroll_path=folder/'account-enrollment.json'
        account=read_private(account_path) or {}
        saved=read_private(folder/'connection.json') or {};pending=read_private(folder/'pair-pending.json') or {}
        portal=origin(request.get('portal',request.get('url',account.get('portal',saved.get('portal',PORTAL)))))
        for item in (account,saved,pending):
            if item.get('portal') and origin(item['portal'])!=portal:raise NetworkError('existing_portal_preserved')
        runtime={**context,'portal':portal,'hub':account.get('hub',context.get('hub',HUB))}
        action=request.get('action','status')
        if action=='logout':
            # Forget account display metadata only; don't revoke an active service.
            if account_path.exists():account_path.unlink()
            return {'schema':'anet-account/1','state':'ready','message':'已退出账户设置；ANet 服务仍保留原连接。','account':{'url':portal,'username':'','state':'signed_out'},'connection':{'state':'preserved','detail':'原连接已保留。'}}
        if action=='status':return _view({**account,'portal':portal},network_factory(runtime).status(),companion=service_api.inspect(runtime))
        if action!='login':raise NetworkError('request_invalid')
        username=request.get('username');password=request.get('password')
        if not isinstance(username,str) or not username.strip() or len(username)>80 or not isinstance(password,str) or not password or len(password)>200:
            raise NetworkError('login_invalid')
        session=session_factory(portal)
        try:me=session.login(username.strip(),password)
        except RemoteFailure as e:
            if e.status in (401,403):raise NetworkError('login_failed') from None
            raise
        password=None
        if account.get('user_id') and account['user_id']!=me['id']:raise NetworkError('account_mismatch')
        runtime['hub']=origin(session.request('/api/client/config').get('hub',''))
        runner=runner_factory(runtime)
        try:local=runner.status()
        except NetworkError as e:
            if e.code!='node_operation_unverified':raise
            runner.run('up');local=runner.status()
        if not local.get('aid'):
            runner.run('up');local=runner.status()
        aid=local.get('aid')
        if not isinstance(aid,str) or not aid:raise NetworkError('node_identity_unavailable')
        local_hub=local.get('hub_url') or (local.get('hub') or {}).get('url')
        if local_hub and local_hub.rstrip('/')!=runtime['hub']:
            raise NetworkError('existing_hub_preserved')
        rows=session.request('/api/agents').get('agents',[])+session.request('/api/devices').get('devices',[])
        # A valid node credential alone does not prove this logged-in account owns it.
        existing_ids={x.get('agent_id') for x in (saved,pending) if x.get('agent_id')}
        if existing_ids and not existing_ids.issubset({x.get('id') for x in rows if x.get('is_owner') is True}):
            raise NetworkError('account_mismatch')
        match=next((x for x in rows if x.get('aid')==aid and x.get('is_owner') is True),None)
        if saved and match:
            try:result=network_factory(runtime).pair(resume=True)
            except NetworkError as e:
                if e.code not in ('pair_credential_required','pair_credential_expired'):raise
                result={}
            if result.get('portal_paired'):
                account={'portal':portal,'hub':runtime['hub'],'username':me['username'],'user_id':me['id']}
                write_private(account_path,account)
                return _view(account,result,companion=service_api.ensure(runtime,runner,new_connection=False))
        # Durable precommitted credential makes retries safe even after lost responses.
        with exclusive(folder):
            enrollment=read_private(enroll_path)
            if enrollment:
                if enrollment.get('user_id')!=me['id'] or enrollment.get('aid')!=aid or enrollment.get('portal')!=portal:raise NetworkError('account_mismatch')
            else:
                enrollment={'portal':portal,'user_id':me['id'],'aid':aid,'pair_token':secrets.token_urlsafe(32),
                            'name':str(request.get('name') or local.get('name') or '我的电脑')[:60],
                            'executor':request.get('executor','codex'),'kind':request.get('kind','device')}
                write_private(enroll_path,enrollment)
            payload={k:enrollment[k] for k in ('aid','name','executor','pair_token','kind')}
            if existing_ids:payload['agent_id']=next(iter(existing_ids))
            try:record=session.request('/api/client/enroll',payload)
            except RemoteFailure as e:
                if e.status==404:raise NetworkError('enrollment_not_supported') from None
                raise NetworkError('enrollment_unverified') from None
            runtime['hub']=origin(record.get('hub',''))
            enrollment['agent_id']=record.get('agent_id');write_private(enroll_path,enrollment)
        result=network_factory(runtime).pair(enrollment['pair_token'])
        if result.get('portal_paired'):
            account={'portal':portal,'hub':runtime['hub'],'username':me['username'],'user_id':me['id']}
            write_private(account_path,account);enroll_path.unlink(missing_ok=True)
        companion=service_api.ensure(runtime,runner,new_connection=not bool(saved)) if result.get('portal_paired') else None
        return _view({**account,'portal':portal,'username':me['username']},result,companion=companion)
    except Exception as error:
        code=error.code if isinstance(error,NetworkError) else 'enrollment_unverified'
        return {'schema':'anet-account/1','state':'error','code':code,'message':MESSAGES.get(code,'连接尚未完成，原身份和进度已保留。'),
                'account':{'url':account.get('portal',PORTAL),'username':account.get('username',''),'state':'needs_attention'},
                'connection':{'state':'needs_attention','detail':MESSAGES.get(code,'请检查服务状态后重试。')}}
    finally:
        if session:session.close()

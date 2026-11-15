#!/usr/bin/env python3
"""Agent Space local companion. No third-party packages, never reads identity keys."""
import argparse, base64, getpass, hashlib, hmac, json, os, platform, plistlib, random, re, secrets, shutil, socketserver, subprocess, sys, threading, time, tempfile
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

CAP='team.portal.admit'
VERSION='0.9.3'

def background_options(new_group=False):
    # pythonw/WSH parents have no console; redirecting pipes alone still opens one.
    return {'creationflags':0x08000000 | (0x00000200 if new_group else 0)} if sys.platform=='win32' else {}

def harness_launcher(folder):return folder/('harness-bridge.exe' if sys.platform=='win32' else 'harness-bridge')

def harness_fingerprint(root):
    folder=root/'portal';cfg=json.loads((root/'config.json').read_text(encoding='utf-8'))
    h=hashlib.sha256(json.dumps(cfg.get('auto_reply',{}),sort_keys=True).encode())
    for p in [folder/'harness-executor.json',folder/'harness_bridge.py',folder/'pi_bridge.py',harness_launcher(folder)]:h.update(p.read_bytes())
    if (folder/'harness-launcher.json').is_file():h.update((folder/'harness-launcher.json').read_bytes())
    return h.hexdigest()

def harness_ready(root,ar,executor):
    try:
        folder=root/'portal';settings=json.loads((folder/'harness-executor.json').read_text(encoding='utf-8'))
        cfg=json.loads((root/'config.json').read_text(encoding='utf-8')).get('auto_reply',{})
        check=json.loads((folder/'harness-check.json').read_text(encoding='utf-8'))
        dependency=Path(settings['connection_file'] if settings.get('workbuddy_auth')=='portal' else settings['credential_file']).is_file() and not (folder/'workbuddy-pending.json').exists() if executor=='workbuddy' else Path(settings['command']).is_file()
        return bool(ar.get('backend')=='exec' and ar.get('agent')=='codex' and ar.get('admitted')=='direct'
            and ar.get('untrusted','off')=='off' and settings.get('mode')=='direct' and settings.get('harness')==executor
            and dependency and Path(settings['work_dir']).is_dir() and Path(ar.get('work_dir','')).resolve()==Path(settings['work_dir']).resolve()
            and cfg.get('command')==str(harness_launcher(folder)) and not cfg.get('model')
            and check.get('harness')==executor and check.get('ok') is True and check.get('fingerprint')==harness_fingerprint(root))
    except (OSError,ValueError,KeyError,TypeError):return False

def pi_launcher(folder):return folder/('pi-bridge.exe' if sys.platform=='win32' else 'pi-bridge')

def pi_fingerprint(root):
    folder=root/'portal';cfg=json.loads((root/'config.json').read_text(encoding='utf-8'))
    h=hashlib.sha256(json.dumps(cfg.get('auto_reply',{}),sort_keys=True).encode())
    for p in [folder/'pi-executor.json',folder/'pi_bridge.py',pi_launcher(folder)]:h.update(p.read_bytes())
    if (folder/'harness-launcher.json').is_file():h.update((folder/'harness-launcher.json').read_bytes())
    return h.hexdigest()

def pi_ready(root, ar):
    try:
        folder=root/'portal';settings=json.loads((folder/'pi-executor.json').read_text(encoding='utf-8'))
        cfg=json.loads((root/'config.json').read_text(encoding='utf-8')).get('auto_reply',{})
        check=json.loads((folder/'pi-check.json').read_text(encoding='utf-8'))
        return bool(ar.get('backend')=='exec' and ar.get('agent')=='codex' and ar.get('admitted')=='direct'
            and ar.get('untrusted','off')=='off' and settings.get('mode')=='direct'
            and Path(settings['pi_command']).is_file() and Path(settings['work_dir']).is_dir()
            and Path(ar.get('work_dir','')).resolve()==Path(settings['work_dir']).resolve()
            and cfg.get('command')==str(pi_launcher(folder))
            and check.get('fingerprint')==pi_fingerprint(root) and check.get('ok') is True)
    except (OSError,ValueError,KeyError,TypeError):return False
def protect_private(p):
    if p.is_symlink() or (getattr(p.lstat(), 'st_file_attributes', 0) & 0x400):
        raise RuntimeError('拒绝对符号链接或 Windows 重解析点写入平台凭证：'+str(p))
    if sys.platform!='win32':
        p.chmod(0o700 if p.is_dir() else 0o600)
        return
    # Windows chmod does not restrict readers. Replace the DACL using the current SID;
    # pass only the path via the process environment, never any credential.
    script = r"""
$ErrorActionPreference='Stop'
$p=$env:AGENT_SPACE_ACL_PATH
$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User
if ((Get-Item -LiteralPath $p).PSIsContainer) {
  $acl=New-Object System.Security.AccessControl.DirectorySecurity
  $rule=New-Object System.Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
} else {
  $acl=New-Object System.Security.AccessControl.FileSecurity
  $rule=New-Object System.Security.AccessControl.FileSystemAccessRule($sid,'FullControl','Allow')
}
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true,$false)
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $p -AclObject $acl
"""
    r=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],env={**os.environ,'AGENT_SPACE_ACL_PATH':str(p)},capture_output=True,timeout=30,**background_options())
    if r.returncode:raise RuntimeError('无法限制平台凭证文件的 Windows ACL；停止配置，不会使用宽松权限。')

def endpoint(state):
    from urllib.parse import urlsplit
    u=urlsplit(state['portal'])
    if u.scheme!='https' or not u.netloc or u.username or u.password or u.query or u.fragment:
        raise RuntimeError('远程权限接口必须是有效的 HTTPS 平台地址')
    return state['portal'].rstrip('/')+'/api/node/inbound'

def write_private(p,d):
    p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    protect_private(p.parent)
    if p.exists() and p.is_symlink():raise RuntimeError('拒绝覆盖符号链接')
    fd,name=tempfile.mkstemp(prefix='.portal-write-',dir=p.parent)
    tmp=Path(name)
    try:
        with os.fdopen(fd,'wb' if isinstance(d,bytes) else 'w',**({} if isinstance(d,bytes) else {'encoding':'utf-8'})) as f:
            f.write(d);f.flush();os.fsync(f.fileno())
        protect_private(tmp)
        os.replace(tmp,p)
        if sys.platform!='win32':
            directory=os.open(p.parent,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
    finally:tmp.unlink(missing_ok=True)

class RemoteError(RuntimeError):
    def __init__(self,message,status):super().__init__(message);self.status=status

class NoCredentialRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise RemoteError('平台请求出现重定向，已停止以保护连接凭证。',code)

def remote(base,path,d,credential=None,timeout=4):
    if not base.startswith('https://') and not base.startswith('http://127.0.0.1:'):raise RuntimeError('平台必须使用 HTTPS')
    req=Request(base+path,data=json.dumps(d,ensure_ascii=path!='/api/node/work-content').encode(),headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+credential} if credential else {})})
    try:
        with build_opener(NoCredentialRedirect()).open(req,timeout=timeout) as r:return json.load(r)
    except HTTPError as e:
        with e:message=json.load(e).get('error','平台请求失败')
        raise RemoteError(message,e.code) from None


NATIVE_LAUNCHER_HASHES = {'amd64': '2c50044979af89e9c2449e186bc9010cf795f675cc0faef84cac14c41f1870d9', 'arm64': '2a339f909337788cf95c7b02de030deed95bb4852b4f87de0a19afdd5d01d6a0'}
def install_native_launcher(folder,bridge,launcher,portal):
    metadata=folder/'harness-launcher.json'
    # Preserve a user-repaired executable; never write script text over .exe bytes.
    if launcher.exists() and not metadata.exists():
        if not launcher.read_bytes().startswith(b'MZ'):raise RuntimeError('既有 exe 不是 Windows 原生程序，请先检查，不会覆盖。')
        return
    arch='arm64' if platform.machine().lower() in ['arm64','aarch64'] else 'amd64'
    content=fetch_bytes(portal+'/harness-launcher-windows-'+arch+'.exe',8*1024*1024)
    if hashlib.sha256(content).hexdigest()!=NATIVE_LAUNCHER_HASHES[arch] or not content.startswith(b'MZ'):raise RuntimeError('原生桥接校验失败，未写入启动器。')
    write_private(launcher,content)
    write_private(metadata,json.dumps({'python':sys.executable,'bridge':str(bridge)},ensure_ascii=False))

def pair_node(args,run,status,preflight):
    """Durable two-phase pairing. The pending secret is saved before server commit.

    The same identity can recover a lost response without reusing a pair token.
    An owner-issued recovery link rotates only the credential, never the AID.
    """
    folder=args.data_dir.expanduser().resolve()/'portal'
    state_file=folder/'connection.json';pending_file=folder/'pair-pending.json'
    state=json.loads(state_file.read_text(encoding='utf-8')) if state_file.exists() else None
    pending=json.loads(pending_file.read_text(encoding='utf-8')) if pending_file.exists() else None
    base=(args.portal or (state or pending or {}).get('portal','')).rstrip('/')
    if not base:raise RuntimeError('首次 pair 需要 --portal HTTPS地址；恢复时请使用原 --data-dir。')
    for saved in (state,pending):
        if saved and saved['portal'].rstrip('/')!=base:raise RuntimeError('此目录属于另一平台；保留原绑定，不覆盖凭证。')
    if args.action=='resume' and not (state or pending):raise RuntimeError('此目录没有绑定或待恢复进度。请确认原数据目录；旧版凭证丢失时，在原连接详情选择“继续安装 / 恢复连接”。')
    run('up');s=status();aid=s['aid']
    for saved in (state,pending):
        if saved and saved['aid']!=aid:raise RuntimeError('数据目录的节点身份与绑定进度不一致；不会覆盖，请使用原身份。')

    def authenticated(saved):
        try:info=remote(base,'/api/node/info',{},saved['node_token'])
        except RemoteError as e:
            if e.status in (401,403):return None
            raise
        if info.get('aid') and info['aid']!=aid:raise RuntimeError('服务端节点身份与本地不一致，停止恢复。')
        if saved.get('agent_id') and info.get('agent_id') and saved['agent_id']!=info['agent_id']:raise RuntimeError('服务端连接记录不匹配，停止恢复。')
        return {**saved,**info,'portal':base,'aid':aid,'anet':args.anet}

    def restore(p):
        if not p or not p.get('challenge'):return True
        try:
            # Do not overwrite a profile changed by the user since this attempt.
            if status().get('profile',{}).get('summary','')==p['challenge']:
                run('profile','set','--summary',p['original_summary'],'--readme',p['original_readme'])
            return True
        except Exception:
            print('提示：原 profile 尚未恢复，进度已保留。绑定不会因此撤销；稍后在同一目录运行 resume 重试。',file=sys.stderr)
            return False

    def finish(value,p):
        # Never copy the pair token or temporary profile into the active credential.
        saved={k:value[k] for k in ('agent_id','node_token','portal','name','kind','executor','aid','anet') if k in value}
        write_private(state_file,json.dumps(saved,ensure_ascii=False,indent=2))
        if restore(p):pending_file.unlink(missing_ok=True)
        print('身份绑定已保存，后续安装可继续；不要新建连接。'+('请检查 MCP，然后运行 install 同步连接状态。' if saved.get('kind')=='device' else '请继续 configure、对应执行器检查和 install，保留原工作目录与权限。'))
        return saved

    # A newer pending recovery may have rotated the old active credential.
    if pending:
        recovered=authenticated(pending)
        if recovered:return finish(recovered,pending)
    if state:
        active=authenticated(state)
        if active:return finish(active,pending)

    credential=None;info=None
    if pending:
        try:info=remote(base,'/api/pair/info',{'token':pending['pair_token']});credential=pending['pair_token']
        except RemoteError as e:
            if e.status!=404:raise
            if args.action=='resume':raise RuntimeError('进度已保留，但接入凭证已过期或被替换。请在原连接详情生成恢复说明，再用同一目录运行 pair；不要新建连接。') from None
    if not info:
        if args.action=='resume':raise RuntimeError('原节点凭证已失效。请在原连接详情选择“继续安装 / 恢复连接”，使用同一身份恢复。')
        credential=getpass.getpass('粘贴原连接的接入或恢复凭证（输入不显示）：')
        globals()['_SETUP_PAIR_TOKEN']=credential
        info=remote(base,'/api/pair/info',{'token':credential})
    if info.get('pair_protocol')!=2:raise RuntimeError('平台尚不支持断点恢复，请先升级平台；未消耗接入凭证。')
    for saved in (state,pending):
        if saved and saved.get('agent_id')!=info['agent_id']:raise RuntimeError('该凭证属于另一个连接。请在原连接上恢复，不新建记录、不覆盖本地进度。')
    if info.get('expected_aid') and info['expected_aid']!=aid:raise RuntimeError('恢复凭证只适用于原节点；请确认原 ANet 身份和数据目录。')
    oldhub=s.get('hub_url') or s.get('hub',{}).get('url')
    if oldhub and oldhub.rstrip('/')!=info['hub'].rstrip('/'):raise RuntimeError('节点属于另一 Hub，先与用户确认迁移或创建独立身份。')
    if info.get('kind','agent')=='agent':preflight()
    if pending and not restore(pending):raise RuntimeError('请先恢复上次临时 profile，再继续；绑定进度已保留。')
    s=status()
    pending={'portal':base,'aid':aid,'agent_id':info['agent_id'],'pair_token':credential,
        'node_token':pending['node_token'] if pending else secrets.token_urlsafe(32),'name':info['name'],
        'kind':info['kind'],'executor':info['executor'],'anet':args.anet,
        'original_summary':s.get('profile',{}).get('summary',''),'original_readme':s.get('readme','')}
    # ACL and durable-write errors must happen before prepare/complete consumes anything.
    write_private(pending_file,json.dumps(pending,ensure_ascii=False,indent=2))
    proof=remote(base,'/api/pair/prepare',{'token':credential,'aid':aid})
    pending['challenge']=proof['challenge']
    write_private(pending_file,json.dumps(pending,ensure_ascii=False,indent=2))
    if s.get('hub',{}).get('state')!='registered':
        run('hub-register',info['hub'],'--name',info['name'],'--passphrase-later')
        for _ in range(30):
            time.sleep(2)
            if status().get('hub',{}).get('state')=='registered':break
        else:raise RuntimeError('Hub 注册尚未完成；进度已保存，请检查 anet status 后运行 resume。')
    try:
        run('profile','set','--summary',pending['challenge'])
        try:result=remote(base,'/api/pair/complete',{'token':credential,'aid':aid,'node_token':pending['node_token']})
        except Exception:
            # The server may have committed even if this response never reached us.
            result=authenticated(pending)
            if not result:raise
        if result['node_token']!=pending['node_token']:raise RuntimeError('平台返回的节点凭证不匹配；已保留恢复进度。')
        return finish({**result,'aid':aid,'anet':args.anet},pending)
    finally:
        # A failed active-file write must still leave the private pending file intact.
        if pending_file.exists():restore(pending)

# Structured installation telemetry. Never store argv, stdout/stderr, paths or free text.
DIAG_STAGES={'setup','pair','resume','configure','configure-pi','configure-harness','test-pi','test-harness','install','diagnose','report','anet-up','anet-status','anet-peers','anet-hub-register','anet-profile','anet-other'}
DIAG_ERRORS={'none','network','timeout','certificate','acl','pair_expired','identity_mismatch','approval_required','missing_dependency','command_failed','permission','configuration','unknown','executor_pending','heartbeat_pending','native_launcher','dsh_profile','model_auth'}
DIAG_RESULTS={'started','ok','failed','pending','unverified'}
LAST_ARGS=None

class SetupPending(RuntimeError):pass

def diagnostic_error(error):
    text=str(error).lower()
    if isinstance(error,SetupPending):return 'executor_pending' if '执行器' in text else 'heartbeat_pending' if '心跳' in text else 'configuration'
    if 'headless 模型授权' in text:return 'model_auth'
    if '原生桥接' in text or '既有 exe' in text:return 'native_launcher'
    if '不能重复 profile' in text:return 'dsh_profile'
    for words,code in [(['certificate','ssl','证书'],'certificate'),(['acl','宽松权限'],'acl'),(['timeout','timed out','超时'],'timeout'),(['过期','被替换','已使用'],'pair_expired'),(['另一平台','另一 hub','原身份','节点身份','另一个连接','不匹配'],'identity_mismatch'),(['授权','approval','批准'],'approval_required'),(['请先从官方','no such file','找不到','not found'],'missing_dependency'),(['permission','拒绝访问','权限不足'],'permission'),(['网络','connection','urlopen'],'network'),(['anet 命令失败'],'command_failed')]:
        if any(w in text for w in words):return code
    return 'configuration' if isinstance(error,(ValueError,KeyError)) else 'unknown'

def diagnostic_event(root,stage,result,error='none',elapsed_ms=0,exit_code=None,checks=None):
    if stage not in DIAG_STAGES:return
    run_id=os.environ.get('AGENT_SPACE_INSTALL_RUN_ID','')
    if not re.fullmatch('[a-f0-9]{24}',run_id):
        run_id=secrets.token_hex(12);os.environ['AGENT_SPACE_INSTALL_RUN_ID']=run_id
    folder=Path(root)/'portal'/'install-logs';path=folder/(run_id+'.json')
    try:
        report=json.loads(path.read_text()) if path.exists() else {'schema':1,'run_id':run_id,'os':platform.system() if platform.system() in ['Windows','Darwin','Linux'] else 'Other','arch':platform.machine() if platform.machine() in ['amd64','AMD64','x86_64','arm64','aarch64'] else 'other','connector_version':VERSION,'python_version':'.'.join(map(str,sys.version_info[:3])),'events':[]}
        entry={'stage':stage,'result':result if result in DIAG_RESULTS else 'failed','error':error if error in DIAG_ERRORS else 'unknown','at':int(time.time()),'elapsed_ms':max(0,min(int(elapsed_ms),86400000))}
        if type(exit_code)==int:entry['exit_code']=max(-255,min(exit_code,255))
        if checks:entry['checks']={k:v for k,v in checks.items() if k in ['binding','hub','policy','executor','heartbeat','mcp'] and v in DIAG_RESULTS}
        report['events']=(report['events']+[entry])[-100:]
        write_private(path,json.dumps(report,ensure_ascii=False))
        for old in sorted(folder.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[10:]:old.unlink()
    except Exception:print('提示：安装诊断暂时无法写入，安装结果不受影响。',file=sys.stderr)

def diagnostic_report(root):
    files=sorted((Path(root)/'portal'/'install-logs').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
    run_id=os.environ.get('AGENT_SPACE_INSTALL_RUN_ID','')
    path=next((p for p in files if p.stem==run_id),files[0] if files else None)
    return json.loads(path.read_text()) if path else None

def upload_diagnostics(args):
    root=args.data_dir.expanduser().resolve();folder=root/'portal'
    if getattr(args,'no_report',False) or (folder/'diagnostics-off').exists():return None
    report=diagnostic_report(root)
    if not report:return None
    try:
        connection=folder/'connection.json';pending=folder/'pair-pending.json'
        state=json.loads(connection.read_text()) if connection.exists() else None
        progress=json.loads(pending.read_text()) if pending.exists() else None
        # Credentials are bound to their saved origin, never a newly supplied --portal.
        for saved in (state,progress):
            if not saved:continue
            try:return remote(saved['portal'].rstrip('/'),'/api/node/diagnostics',{'report':report},saved['node_token'])
            except RemoteError as e:
                if e.status not in (401,403):raise
        credential=(progress or {}).get('pair_token') or globals().get('_SETUP_PAIR_TOKEN')
        base=((progress or state or {}).get('portal') or args.portal or '').rstrip('/')
        if credential:return remote(base,'/api/pair/diagnostics',{'token':credential,'report':report})
    except Exception:print('诊断回传暂未成功，脱敏记录已保存在原数据目录；稍后运行 report 重试。',file=sys.stderr)
    return None

def installation_checks(root,state,info,status,peers):
    s=status();active=s.get('hub',{}).get('state')=='registered'
    kind=info.get('kind','agent');executor=info.get('executor','codex');ar=s.get('auto_reply') or {}
    checks={'binding':'ok','hub':'ok' if active else 'pending','mcp':'unverified','heartbeat':'ok' if info.get('node_connected') and time.time()-info.get('last_seen',0)<90 else 'pending'}
    if kind=='device':checks.update(policy='unverified',executor='unverified')
    else:
        x=peers();checks['policy']='ok' if not (x.get('allow') or x.get('trust') or x.get('public_capabilities')) and (info.get('policy_ok') or x.get('admit')) else 'pending'
        ready=harness_ready(root,ar,executor) if executor in ['workbuddy','deepseek','custom'] else pi_ready(root,ar) if executor=='pi' else bool(info.get('executor_ready'))
        checks['executor']='ok' if ready else 'pending'
    return checks

def setup_connection(args,run,status,preflight):
    print('安装诊断只记录阶段、版本、检查结果和错误类别；不上传对话、文件、路径或凭证。可用 --no-report 关闭回传。')
    root=args.data_dir.expanduser().resolve();started=time.monotonic()
    pair_node(args,run,status,preflight);diagnostic_event(root,'pair','ok',elapsed_ms=(time.monotonic()-started)*1000)
    state=json.loads((root/'portal/connection.json').read_text());info=remote(state['portal'],'/api/node/info',{},state['node_token'])
    common=['--data-dir',str(root),'--anet',args.anet]
    def step(action,extra=()):
        code=subprocess.call([sys.executable,str(Path(__file__).resolve()),action,*common,*extra])
        if code:raise SetupPending('安装停在 '+action+'；进度已保留，处理后重新运行 setup。')
    if info['kind']=='agent':
        step('configure')
        executor=info['executor'];folder=root/'portal'
        if executor=='deepseek':print('DeepSeek headless 需要可用的启动环境或 .env 模型授权；GUI 登录不一定适用。检查本机已有配置，不打印或上传密钥。')
        configured=(folder/('pi-executor.json' if executor=='pi' else 'harness-executor.json')).exists()
        if executor!='codex' and not configured and args.work_dir and args.mode:
            extra=['--work-dir',str(args.work_dir),'--mode',args.mode]
            if executor=='pi' and args.pi_command:step('configure-pi',[*extra,'--pi-command',args.pi_command])
            elif executor in ['deepseek','custom'] and args.command:step('configure-harness',[*extra,'--harness',executor,'--command',args.command,*['--arg='+x for x in args.arg]])
            elif executor=='workbuddy':step('configure-harness',[*extra,'--harness',executor,*(['--credential-file',str(args.credential_file)] if args.credential_file else [])])
            configured=(folder/('pi-executor.json' if executor=='pi' else 'harness-executor.json')).exists()
        if executor=='pi' and configured:step('test-pi')
        elif executor in ['deepseek','workbuddy','custom'] and configured:step('test-harness')
    step('install')
    for _ in range(6):
        info=remote(state['portal'],'/api/node/info',{},state['node_token'])
        if info.get('node_connected') and time.time()-info.get('last_seen',0)<90:break
        time.sleep(2)
    checks=installation_checks(root,state,info,status,lambda:run('peers','list','--json',decode=True))
    diagnostic_event(root,'diagnose','ok',checks=checks)
    if info['kind']=='agent' and any(checks.get(k)!='ok' for k in ('policy','executor')):
        diagnostic_event(root,'setup','pending','executor_pending')
        raise SetupPending('电脑已绑定并安装连接心跳，接收任务还需要完成执行器授权/自检。继续使用这条连接和原数据目录；不要重新 pair。')
    if checks['heartbeat']!='ok':raise SetupPending('登录启动项已安装，尚未确认心跳。请在网页查看状态，保留原进度。')
    print('连接和心跳检查通过。下一步由当前助手实际调用 ANet 工具检查 MCP；此检查尚未自动完成。')

def cli_entry():
    global LAST_ARGS
    start=time.monotonic();error=None;code=0
    try:main()
    except Exception as e:
        error=e;code=2 if isinstance(e,SetupPending) else 1
        print(('等待完成：' if code==2 else '接入未完成：')+str(e),file=sys.stderr)
    args=LAST_ARGS
    if args and args.action in DIAG_STAGES:
        root=args.data_dir.expanduser().resolve()
        if args.action!='report':diagnostic_event(root,args.action,'pending' if code==2 else 'failed' if code else 'ok',diagnostic_error(error) if error else 'none',elapsed_ms=(time.monotonic()-start)*1000,exit_code=code)
        result=upload_diagnostics(args)
        if result:print('安装诊断编号：'+result['report_id'])
    return code

def taskd_state(root, requester=False):
    """A taskd daemon that takes tasks for this node answers them instead of auto-reply (taskd/README.md)."""
    try:st=json.loads((root/'taskd'/'status.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return None
    try:age=time.time()-float(st.get('ts',0))
    except (ValueError,TypeError,AttributeError):return None
    if not 0<=age<=120 or not st.get('requester' if requester else 'accept_tasks'):return None
    return st

def taskd_requester_ready(root):
    st=taskd_state(root,requester=True)
    if not st:return False
    if 'requester_ready' in st:return bool(st['requester_ready'])
    try:
        cfg=json.loads((root/'taskd'/'config.json').read_text(encoding='utf-8'))
        return 'codex' in (cfg.get('requester') or {}).get('clients',['codex'])
    except (OSError,ValueError,TypeError,AttributeError):return False

SECRET=re.compile(r'(-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)|\bsk-[A-Za-z0-9_\-]{16,}|\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=\-]{16,}|\b(?:ghp|gho|github_pat|xox[bap]|AKIA)[A-Za-z0-9_\-]{12,}|(?i:(?:api[_-]?key|secret|token|password|passwd)["\']?\s*[:=]\s*)["\']?[^\s"\',;]{6,})',re.S)
TERMINAL={'TASK_STATE_COMPLETED','TASK_STATE_FAILED','TASK_STATE_CANCELED','TASK_STATE_REJECTED'}

def redact(t):return SECRET.sub('[已隐藏]',t)

def sync_flag(root):return root/'portal'/'sync-enabled'
def sync_enabled(root):return sync_flag(root).is_file()

def thread_record(th,days=30):
    """One anet thread as the portal stores it: text and attachment names only, secrets masked, status heartbeats dropped."""
    created=th.get('created_at','')
    msgs=[]
    for m in th.get('messages',[]):
        atts=[{'name':str(a.get('name',''))[:120],'size':a.get('size'),'mime':str(a.get('mime',''))[:80]} for a in m.get('attachments') or [] if isinstance(a,dict)]
        body=redact(str(m.get('body') or ''))[:8000]
        if m.get('kind')=='status' and not body and not atts:continue
        msgs.append({'msg_id':str(m.get('msg_id','')),'from':m.get('from'),'kind':m.get('kind','text'),'body':body,'attachments':atts,'created_at':m.get('created_at','')})
    return {'id':th.get('interaction_id'),'role':th.get('role'),'peer':th.get('peer'),'goal':redact(str(th.get('goal','')))[:8000],
        'state':str(th.get('state','working')).replace('_','-'),'created_at':created,'updated_at':th.get('updated_at',created),'messages':msgs[-300:]}

def push_interactions(state,records):
    for i in range(0,len(records),5):
        remote(state['portal'],'/api/node/sync',{'interactions':records[i:i+5]},state['node_token'],timeout=20)

def run_sync(run,state,seen):
    """One scan. Finished tasks are read once per state; open ones every pass."""
    tasks=run('task','list',decode=True,timeout=40).get('tasks',[])[:40];fresh=[]
    cutoff=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(time.time()-30*86400))
    for t in tasks:
        tid=t.get('id');st=t.get('status') or {};sig=(st.get('state'),st.get('timestamp'))
        if not isinstance(tid,str) or (st.get('state') in TERMINAL and seen.get(tid)==sig):continue
        try:th=run('thread',tid,decode=True,timeout=30).get('thread') or {}
        except Exception:continue
        if th.get('created_at','')<cutoff:seen[tid]=sig;continue
        rec=thread_record(th);fresh.append((tid,sig,rec))
    # Only re-send an open task when its message count or state moved.
    send=[(tid,sig,r) for tid,sig,r in fresh if seen.get(('n',tid))!=(r['state'],len(r['messages']),r['updated_at'])]
    if send:
        push_interactions(state,[r for _,_,r in send])
        for tid,sig,r in send:seen[tid]=sig;seen[('n',tid)]=(r['state'],len(r['messages']),r['updated_at'])
    return len(send)

# ---- Signed self-update -------------------------------------------------------------
# The release key is held offline by the operator; only its public half is here. A node
# applies a new connector.py only when the manifest verifies against UPDATE_KEY, the file
# matches the signed SHA-256, and the serial never goes backwards. Bridge files are not
# updated here: their hashes are part of the executor readiness fingerprint.
UPDATE_KEY=bytes.fromhex('6064403f01345978fc14e4a577dc2290abf238394409cb3e077adeeaf948c907')
_P=2**255-19;_Q=2**252+27742317777372353535851937790883648493;_D=-121665*pow(121666,_P-2,_P)%_P;_I=pow(2,(_P-1)//4,_P)
def _inv(x):return pow(x,_P-2,_P)
def _x(y,sign):
    if y>=_P:return None
    xx=(y*y-1)*_inv(_D*y*y+1)%_P
    if xx==0:return None if sign else 0
    x=pow(xx,(_P+3)//8,_P)
    if (x*x-xx)%_P:x=x*_I%_P
    if (x*x-xx)%_P:return None
    return _P-x if (x&1)!=sign else x
_GY=4*_inv(5)%_P;_GX=_x(_GY,0);_G=(_GX,_GY,1,_GX*_GY%_P)
def _add(a,b):
    A=(a[1]-a[0])*(b[1]-b[0])%_P;B=(a[1]+a[0])*(b[1]+b[0])%_P;C=2*a[3]*b[3]*_D%_P;E=2*a[2]*b[2]%_P
    e,f,g,h=B-A,E-C,E+C,B+A
    return (e*f%_P,g*h%_P,f*g%_P,e*h%_P)
def _mul(n,pt):
    r=(0,1,1,0)
    while n:
        if n&1:r=_add(r,pt)
        pt=_add(pt,pt);n>>=1
    return r
def _enc(pt):
    z=_inv(pt[2]);x=pt[0]*z%_P;y=pt[1]*z%_P;return (y|((x&1)<<255)).to_bytes(32,'little')
def _dec(b):
    if len(b)!=32:return None
    y=int.from_bytes(b,'little');sign=y>>255;y&=(1<<255)-1;x=_x(y,sign)
    return None if x is None else (x,y,1,x*y%_P)
def ed25519_verify(public,message,signature):
    if len(public)!=32 or len(signature)!=64:return False
    a=_dec(public);r=_dec(signature[:32]);s=int.from_bytes(signature[32:],'little')
    if a is None or r is None or s>=_Q:return False
    h=int.from_bytes(hashlib.sha512(signature[:32]+public+message).digest(),'little')%_Q
    l=_mul(s,_G);rr=_add(r,_mul(h,a))
    return (l[0]*rr[2]-rr[0]*l[2])%_P==0 and (l[1]*rr[2]-rr[1]*l[2])%_P==0
def ed25519_public(secret):
    h=hashlib.sha512(secret).digest();a=int.from_bytes(h[:32],'little');a&=(1<<254)-8;a|=1<<254;return _enc(_mul(a,_G))
def ed25519_sign(secret,message):
    h=hashlib.sha512(secret).digest();a=int.from_bytes(h[:32],'little');a&=(1<<254)-8;a|=1<<254
    pub=_enc(_mul(a,_G));r=int.from_bytes(hashlib.sha512(h[32:]+message).digest(),'little')%_Q;rb=_enc(_mul(r,_G))
    k=int.from_bytes(hashlib.sha512(rb+pub+message).digest(),'little')%_Q
    return rb+((r+k*a)%_Q).to_bytes(32,'little')

def update_dir(folder):return folder/'updates'
def update_state(folder):
    try:return json.loads((update_dir(folder)/'state.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}
def save_update_state(folder,**changes):
    d=update_dir(folder);d.mkdir(parents=True,exist_ok=True);st={**update_state(folder),**changes}
    write_private(d/'state.json',json.dumps(st))
    return st
def auto_update_on(folder):return not (folder/'auto-update-off').exists()
def vtuple(v):return tuple(int(x) for x in v.split('.'))

def fetch_bytes(url,limit):
    if not url.startswith('https://') and not url.startswith('http://127.0.0.1:'):raise RuntimeError('更新通道必须使用 HTTPS')
    with urlopen(Request(url),timeout=30) as r:data=r.read(limit+1)
    if len(data)>limit:raise RuntimeError('更新文件超过允许大小')
    return data

def restart_self():
    argv=[sys.executable,str(Path(__file__).resolve()),*sys.argv[1:]]
    if sys.platform=='win32':
        subprocess.Popen(argv,close_fds=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**background_options(new_group=True))
        os._exit(0)
    os.execv(sys.executable,argv)

def check_update(folder,here,portal,apply=True,key=None):
    """Returns 'current', 'available', 'updated' or 'blocked'. Raises on anything untrusted."""
    key=key or UPDATE_KEY;base=portal.rstrip('/')+'/downloads/agent-space-updates'
    raw=fetch_bytes(base+'/manifest.json',65536)
    try:sig=base64.b64decode(fetch_bytes(base+'/manifest.sig',1024).strip(),validate=True)
    except ValueError:raise RuntimeError('更新清单签名格式不正确。') from None
    if not ed25519_verify(key,raw,sig):raise RuntimeError('更新清单签名无效，已忽略。')
    m=json.loads(raw);st=update_state(folder)
    if m.get('schema')!=1 or type(m.get('serial')) is not int:raise RuntimeError('更新清单格式不正确。')
    if m['serial']<st.get('serial',0):raise RuntimeError('更新清单比已接受的更旧，已拒绝（防回滚）。')
    comp=(m.get('components') or {}).get('connector') or {};ver=str(comp.get('version',''));digest=str((comp.get('files') or {}).get('connector.py',''))
    if not re.fullmatch(r'\d+\.\d+\.\d+',ver) or not re.fullmatch(r'[0-9a-f]{64}',digest):raise RuntimeError('更新清单缺少 connector 版本或校验值。')
    now=int(time.time());save_update_state(folder,serial=m['serial'],last_check=now)
    if ver in st.get('blocked',[]):save_update_state(folder,result='blocked:'+ver);return 'blocked'
    if vtuple(ver)<=vtuple(VERSION):save_update_state(folder,result='current');return 'current'
    if not apply:save_update_state(folder,result='available:'+ver);return 'available'
    code=fetch_bytes(base+'/connector-'+ver+'/connector.py',600000)
    if hashlib.sha256(code).hexdigest()!=digest:raise RuntimeError('下载的 connector.py 与签名清单的校验值不一致。')
    compile(code,'connector.py','exec')
    with tempfile.TemporaryDirectory() as d:
        probe=Path(d)/'connector.py';probe.write_bytes(code)
        out=subprocess.run([sys.executable,str(probe),'version'],capture_output=True,text=True,timeout=30,**background_options())
        if out.returncode or out.stdout.strip()!=ver:raise RuntimeError('新版 connector 自检未通过，未替换。')
    backup=update_dir(folder)/'backup'/(VERSION+'-connector.py');backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(here,backup)
    fd,name=tempfile.mkstemp(prefix='.connector-',dir=here.parent);tmp=Path(name)
    try:
        with os.fdopen(fd,'wb') as f:f.write(code)
        protect_private(tmp);os.replace(tmp,here)
    finally:tmp.unlink(missing_ok=True)
    write_private(update_dir(folder)/'pending.json',json.dumps({'version':ver,'from':VERSION,'tries':0,'backup':str(backup)}))
    save_update_state(folder,result='updated:'+ver,updated_at=now)
    return 'updated'

def update_guard(folder,here):
    """A new version that cannot start three times is rolled back and blocked."""
    p=update_dir(folder)/'pending.json'
    try:d=json.loads(p.read_text(encoding='utf-8'))
    except (OSError,ValueError):return
    d['tries']=d.get('tries',0)+1
    if d['tries']<=3:write_private(p,json.dumps(d));return
    backup=Path(d.get('backup',''))
    if backup.is_file():
        shutil.copy2(backup,here);st=update_state(folder)
        save_update_state(folder,blocked=sorted(set(st.get('blocked',[])+[str(d.get('version'))])),result='rolled-back:'+str(d.get('version')))
        p.unlink(missing_ok=True);print('新版 connector 连续启动失败，已回滚。',flush=True);restart_self()
    p.unlink(missing_ok=True)

def confirm_update(folder):
    """Only the process that runs the new version may confirm it; an older process still alive after a manual update must not."""
    p=update_dir(folder)/'pending.json'
    try:pending=json.loads(p.read_text(encoding='utf-8'))
    except (OSError,ValueError):return
    if pending.get('version')==VERSION:p.unlink(missing_ok=True);save_update_state(folder,result='confirmed:'+VERSION)

def taskd_config(root):
    try:return json.loads((root/'taskd'/'config.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}

def taskd_serves(td,executor):
    """taskd names its executor 'codex' or 'command'. A command executor runs the non-Codex harnesses (Pi, DeepSeek, custom)."""
    kind=td.get('executor')
    return kind==executor or (kind=='command' and executor!='codex')

def runtime_info(status, ar, executor, root):
    """Self-reported environment shown on the agent page. No credentials, tokens or file contents."""
    ar=ar or {};sandbox=ar.get('sandbox') if isinstance(ar.get('sandbox'),dict) else {}
    egress=ar.get('egress_allow') if isinstance(ar.get('egress_allow'),list) else []
    td=taskd_state(root);tc=taskd_config(root) if td else {}
    wb_auth=''
    if executor=='workbuddy':
        try:wb_auth=json.loads((root/'portal/harness-executor.json').read_text()).get('workbuddy_auth','file')
        except (OSError,ValueError):pass
    return {'harness':executor,'backend':ar.get('backend'),'agent':ar.get('agent') or ((tc.get('executor') or {}).get('type') if td else None),'model':ar.get('model'),
        'workbuddy_auth':wb_auth,
        'mode':ar.get('admitted') or ('taskd' if td else None),'untrusted':ar.get('untrusted','off') if ar else None,
        'work_dir':ar.get('work_dir') or (tc.get('work_dir') if td else None),
        'read_paths':[str(x) for x in (tc.get('read_paths') or [])][:5] if td else [],'gui':bool(tc.get('probes')) if td else False,
        'sandbox':sandbox.get('state'),'egress_allow':[str(h) for h in egress][:20],
        'anet_version':status.get('version'),'connector_version':VERSION,'os':platform.system(),'machine':platform.machine(),
        'taskd':bool(td),'taskd_version':str(td.get('version',''))[:20] if td else '','taskd_update':str(td.get('update_state',''))[:60] if td else '',
        'sync':sync_enabled(root),'auto_update':auto_update_on(root/'portal'),'update_state':str(update_state(root/'portal').get('result',''))[:60]}

def clean_execution_facts(raw, observed_now=None):
    """Content-free schema. Never forward session IDs, model text, paths or arbitrary errors."""
    import math
    import time
    if not isinstance(raw, dict) or raw.get('schema') != 1:
        return None
    now = time.time() if observed_now is None else observed_now
    out = {'schema': 1}
    choices = {
        'route': ('native', 'planned', 'direct', 'quick'),
        'routing_reason': ('explicit_planned', 'native_capability_verified', 'disabled_by_owner',
                           'adapter_has_no_verified_native_goals', 'native_probe_failed', 'unavailable'),
        'goal_status': ('active', 'paused', 'blocked', 'complete', 'budget_limited', 'budgetLimited'),
        'turn_status': ('inProgress', 'completed', 'interrupted', 'failed'),
        'last_event': ('turn/started', 'turn/completed', 'thread/goal/updated', 'native/retry',
                       'native/stopping', 'native/stopped', 'native/reconciled', 'native/needsApproval',
                       'item/completed', 'item/commandExecution/outputDelta', 'error'),
    }
    for key, allowed in choices.items():
        if isinstance(raw.get(key), str) and raw[key] in allowed:
            out[key] = raw[key]
    def stamp(value, future=30):
        return value if type(value) in (int, float) and math.isfinite(value) and 0 < value <= now + future else None
    at = stamp(raw.get('updated_at'))
    if at: out['updated_at'] = at
    if type(raw.get('iteration')) is int and 0 <= raw['iteration'] <= 100000:
        out['iteration'] = raw['iteration']
    retry = raw.get('retry')
    if isinstance(retry, dict) and retry.get('reason') in ('model_at_capacity', 'upstream_retry'):
        out['retry'] = {'reason': retry['reason']}
        if type(retry.get('attempt')) is int and 0 <= retry['attempt'] <= 100000:
            out['retry']['attempt'] = retry['attempt']
        for key, future in (('at', 30), ('retry_at', 86400)):
            at = stamp(retry.get(key), future)
            if at: out['retry'][key] = at
    stop = raw.get('stop')
    if isinstance(stop, dict):
        out['stop'] = {k: stop[k] for k in ('requested', 'confirmed', 'resource_retained') if type(stop.get(k)) is bool}
    return out


# Content protocol is separate from bounded heartbeat metadata. Never carries raw output.
WORK_CONTENT_FIELDS = ('goal', 'phase', 'milestone', 'summary', 'next', 'reason', 'step_blocker', 'diagnostic')
def clean_work_content(raw):
    if not isinstance(raw, dict) or raw.get('schema') != 1 or not isinstance(raw.get('fields'), dict):
        return None
    remaining = 32768
    fields, truncated = {}, []
    for key in WORK_CONTENT_FIELDS:
        value = raw['fields'].get(key)
        if not isinstance(value, str): continue
        value = redact(value)
        limit = min(16384, remaining)
        fields[key] = value[:limit]
        if len(value) > limit or key in (raw.get('truncated') if isinstance(raw.get('truncated'), list) else []):
            truncated.append(key)
        remaining -= len(fields[key])
    return {'schema': 1, 'fields': fields, 'truncated': truncated, 'field_limit': 16384, 'total_limit': 32768}

def work_content_records(root, executor):
    if not sync_enabled(root): return []
    td = taskd_state(root)
    if not td or not taskd_serves(td, executor): return []
    work = td.get('work') or {}
    if work.get('schema') != 1: return []
    records = []
    for task in (work.get('tasks') if isinstance(work.get('tasks'), list) else [])[:50]:
        if not isinstance(task, dict) or not isinstance(task.get('id'), str): continue
        phase, step, blocker = task.get('phase') or {}, task.get('last_step') or {}, task.get('blocker') or {}
        if not all(isinstance(x, dict) for x in (phase, step, blocker)): continue
        milestone = phase.get('milestone') or {}
        content = clean_work_content({'schema': 1, 'fields': {
            'goal': task.get('goal'), 'phase': phase.get('title'),
            'milestone': milestone.get('current') if isinstance(milestone, dict) else None,
            'summary': step.get('summary'), 'next': step.get('next'), 'reason': task.get('reason'),
            'step_blocker': step.get('blocker'), 'diagnostic': blocker.get('detail')}})
        records.append({'task_id': task['id'], 'observed_at': work.get('observed_at'), 'content': content})
    return records

def push_work_content(root, executor, state):
    sent = 0
    for record in work_content_records(root, executor):
        if not sync_enabled(root): break
        try:
            remote(state['portal'], '/api/node/work-content', record, state['node_token'], timeout=3)
            sent += 1
        except RemoteError as e:
            print('任务正文同步未完成：'+type(e).__name__,flush=True)
            if e.status in (401,403,404,429): break
            # A missing/racing task or oversized record must not starve later tasks.
        except Exception as e:print('任务正文同步未完成：'+type(e).__name__,flush=True)
    return sent

def work_info(root, executor):
    """Only taskd-managed ANet work. Content sync remains opt-in, independently of status."""
    td=taskd_state(root)
    if not td or not taskd_serves(td,executor):return None
    work=td.get('work')
    if not isinstance(work,dict) or work.get('schema')!=1:return None
    work={k:work[k] for k in ('schema','source','scope','observed_at','counts','state','total','truncated') if k in work}
    tasks=[]
    content=sync_enabled(root)
    for task in (td['work'].get('tasks') if isinstance(td['work'].get('tasks'),list) else [])[:50]:
        if not isinstance(task,dict):continue
        clean={k:task[k] for k in ('id','mode','state','started_at','last_activity_at','last_event','last_progress_at',
                                 'progress_event','events','execution','attention','no_progress_steps','scheduling','plan_revision','revision_pending','last_purpose','timing') if k in task}
        facts=clean_execution_facts(task.get('execution_facts'))
        if facts:clean['execution_facts']=facts
        phase=task.get('phase')
        if isinstance(phase,dict):clean['phase']={k:phase[k] for k in ('index','total') if k in phase}
        if isinstance(phase,dict) and isinstance(phase.get('milestone'),dict):
            clean['phase']['milestone']={k:phase['milestone'][k] for k in ('done','total') if k in phase['milestone']}
        if content:
            if isinstance(task.get('blocker'),dict):
                clean['blocker']={k:redact(task['blocker'][k])[:500] for k in ('code','action','detail') if isinstance(task['blocker'].get(k),str)}
            for k in ('goal','reason'):
                if isinstance(task.get(k),str):clean[k]=redact(task[k])[:500]
            if isinstance(task.get('last_step'),dict):
                clean['last_step']={k:redact(task['last_step'][k])[:1000] for k in ('summary','next','blocker','at') if isinstance(task['last_step'].get(k),str)}
            if isinstance(phase,dict) and isinstance(phase.get('title'),str):clean['phase']['title']=redact(phase['title'])[:160]
            if isinstance(phase,dict) and isinstance(phase.get('milestone'),dict):
                clean['phase']['milestone']['current']=redact(str(phase['milestone'].get('current','')))[:160]
        tasks.append(clean)
    return dict(work,tasks=tasks,content_synced=content)

def main():
    global LAST_ARGS
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['setup','diagnose','report','pair','resume','configure','configure-pi','test-pi','configure-harness','test-harness','serve','install','enable-sync','disable-sync','update','auto-update-on','auto-update-off','version']);p.add_argument('--portal');p.add_argument('--data-dir',type=Path,default=Path(os.environ.get('ANET_DATA_DIR',Path.home()/'.anet')));p.add_argument('--anet',default=shutil.which('anet'));p.add_argument('--transport',choices=['unix','https'],help='Windows defaults to HTTPS; other systems default to Unix socket');p.add_argument('--work-dir',type=Path);p.add_argument('--mode',choices=['direct']);p.add_argument('--pi-command');p.add_argument('--model');p.add_argument('--harness',choices=['deepseek','workbuddy','custom']);p.add_argument('--command');p.add_argument('--arg',action='append',default=[]);p.add_argument('--credential-file',type=Path);p.add_argument('--check',action='store_true',help='update: only check, do not install');p.add_argument('--no-report',action='store_true',help='保留本地脱敏安装日志，关闭自动回传');args=p.parse_args();LAST_ARGS=args
    if args.action=='version':print(VERSION);return
    root=args.data_dir.expanduser().resolve();folder=root/'portal';state_file=folder/'connection.json';sock=folder/'admit.sock';conf_path=root/'config.json'
    if args.no_report:write_private(folder/'diagnostics-off','1')
    if args.action=='report':
        report=diagnostic_report(root)
        if not report:raise RuntimeError('还没有本地安装诊断记录。')
        print(json.dumps(report,ensure_ascii=False,indent=2));return
    if not args.anet:raise RuntimeError('请先从官方发布安装 ANet >=0.3.9')
    args.anet=str(Path(args.anet).resolve());env={**os.environ,'ANET_DATA_DIR':str(root)}
    def run(*cmd,decode=False,timeout=30):
        started=time.monotonic()
        try:r=subprocess.run([args.anet,*cmd],env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout,**background_options())
        except Exception as e:
            diagnostic_event(root,'anet-'+cmd[0] if 'anet-'+cmd[0] in DIAG_STAGES else 'anet-other','failed',diagnostic_error(e),elapsed_ms=(time.monotonic()-started)*1000);raise
        if args.action!='serve':diagnostic_event(root,'anet-'+cmd[0] if 'anet-'+cmd[0] in DIAG_STAGES else 'anet-other','failed' if r.returncode else 'ok','command_failed' if r.returncode else 'none',elapsed_ms=(time.monotonic()-started)*1000,exit_code=r.returncode)
        if r.returncode:raise RuntimeError('ANet 命令失败：'+r.stderr.strip()[:800])
        return json.loads(r.stdout) if decode else r.stdout
    def status():return run('status','--json',decode=True)
    def peers():return run('peers','list','--json',decode=True)
    def preflight():
        x=peers()
        if x.get('allow') or x.get('trust') or x.get('public_capabilities'):
            raise RuntimeError('检测到既有 allow/trust 或公开 capability；它们可能绕过平台权限。请先与用户确认并迁移，不会自动修改这些设置。')
        return x
    if args.action=='setup':
        setup_connection(args,run,status,preflight);return
    if args.action in ['pair','resume']:
        pair_node(args,run,status,preflight);return
    if not state_file.exists():raise RuntimeError('请先运行 pair')
    state=json.loads(state_file.read_text(encoding='utf-8'))
    info=remote(state['portal'],'/api/node/info',{},state['node_token'])
    kind=info.get('kind',state.get('kind','agent'))
    executor=info.get('executor',state.get('executor','codex'))
    if args.action=='diagnose':
        checks=installation_checks(root,state,info,status,peers)
        diagnostic_event(root,'diagnose','ok',checks=checks);print(json.dumps(checks,ensure_ascii=False));return
    if args.action in ['auto-update-on','auto-update-off']:
        flag=folder/'auto-update-off'
        if args.action=='auto-update-off':write_private(flag,'1');print('已关闭自动更新。仍可手动运行 update。')
        else:flag.unlink(missing_ok=True);print('已开启自动更新：每 6 小时检查一次已签名的 connector 更新。')
        return
    if args.action=='update':
        stable=folder/'connector.py';here=stable if stable.is_file() else Path(__file__).resolve()
        result=check_update(folder,here,state['portal'],apply=not args.check)
        print({'current':'已是最新版（'+VERSION+'）。','available':'有新版可用；去掉 --check 安装。','blocked':'该版本此前启动失败已被屏蔽。','updated':'已更新并保留旧版备份。重启连接程序（或等待它下次自动重启）后生效。'}[result])
        return
    if args.action in ['enable-sync','disable-sync']:
        if kind!='agent':raise RuntimeError('调用设备不同步交互内容。')
        if args.action=='disable-sync':sync_flag(root).unlink(missing_ok=True);print('已停止同步。平台上已有的记录会在断开连接时删除，或 30 天后过期。');return
        if not sys.stdin.isatty():raise RuntimeError('请在你自己的终端运行，由设备主人确认。')
        print('开启后，本机这个 ANet 身份的任务对话（目标、消息正文、附件名称和大小）会上传到 '+state['portal']+'，\n所有者可在“智能体详情 → 交互”查看；是否对其他成员可见由你在网页上单独开关。\n上传前会遮盖常见密钥格式，但不保证识别全部敏感内容。附件文件本身不上传，保留 30 天。')
        if input('输入 yes 确认开启：').strip().lower()!='yes':raise RuntimeError('已取消。')
        write_private(sync_flag(root),'1');print('已开启。serve 运行中的连接程序会在几秒内开始同步；重新 install 后保持开启。');return
    if args.action in ['configure-harness','test-harness']:
        if kind!='agent' or executor not in ['deepseek','workbuddy','custom']:raise RuntimeError('此入口仅用于 DeepSeek、WorkBuddy 或自定义 Agent；调用设备、Codex 和 Pi 保持原设置。')
        if args.harness and args.harness!=executor:raise RuntimeError('所选 harness 与平台记录不一致，请检查接入说明。')
        preflight();cfg=json.loads(conf_path.read_text(encoding='utf-8'));ar=cfg.get('auto_reply') or {};launcher=harness_launcher(folder)
        if args.action=='test-harness':
            if ar.get('command')!=str(launcher):raise RuntimeError('请先运行 configure-harness')
            answer=run('autoreply','test','Reply with exactly AGENT_SPACE_HARNESS_OK and do not use any tools.',timeout=660)
            if not answer.strip():raise RuntimeError('Harness 自检没有返回检查结果。')
            write_private(folder/'harness-check.json',json.dumps({'ok':True,'harness':executor,'fingerprint':harness_fingerprint(root),'checked_at':int(time.time())}))
            print(executor+' 本地接入自检通过；尚未验证远程任务。');return
        if args.mode!='direct' or not args.work_dir:raise RuntimeError('请由设备主人明确指定 --work-dir 和 --mode direct。')
        work=args.work_dir.expanduser().resolve()
        if not work.is_dir() or work==Path.home().resolve() or work.parent==work:raise RuntimeError('请选择已存在的具体工作目录，不能使用主目录或根目录。')
        if ar and ar.get('command')!=str(launcher):raise RuntimeError('已有其他执行器配置，不自动覆盖。')
        settings={'schema':1,'harness':executor,'mode':'direct','work_dir':str(work),'timeout':600,'args':args.arg,'node_command':shutil.which('node') or ''}
        if executor=='workbuddy':
            if args.credential_file:
                credential=args.credential_file.expanduser().resolve()
                if not credential.is_file():raise RuntimeError('WorkBuddy 授权文件不存在。')
                if not json.loads(credential.read_text(encoding='utf-8')).get('access_token'):raise RuntimeError('WorkBuddy 授权文件缺少 access_token。')
                protect_private(credential);settings['credential_file']=str(credential)
                print('使用本机授权文件；此兼容模式不自动续期。客户长期接入请使用网页授权。')
            else:
                auth=remote(state['portal'],'/api/node/workbuddy/token',{},state['node_token'],timeout=20)
                if not isinstance(auth.get('access_token'),str) or not auth['access_token']:raise RuntimeError('请先在网页连接详情完成 WorkBuddy 授权。')
                settings.update(workbuddy_auth='portal',connection_file=str(folder/'connection.json'))
            if (folder/'workbuddy-pending.json').exists():raise RuntimeError('存在效果未确认的 WorkBuddy 任务，请先在助理核实。')
        else:
            command=args.command or (shutil.which('dsh') if executor=='deepseek' else None)
            command=shutil.which(command) or str(Path(command).expanduser().absolute()) if command else None
            if not command or not Path(command).is_file():raise RuntimeError('未找到 harness 命令，请指定 --command 可执行文件路径。')
            settings['command']=command
        sources=[Path(__file__).resolve().with_name(name) for name in ['harness_bridge.py','pi_bridge.py']]
        if not all(p.is_file() for p in sources):raise RuntimeError('请把 harness_bridge.py 和 pi_bridge.py 下载到 connector.py 同一目录。')
        if not sys.stdin.isatty():raise RuntimeError('请由设备主人在交互终端完成 ANet 原生 direct 授权，不接受管道或代答。')
        affected=[conf_path,folder/'harness-executor.json',folder/'harness-check.json',folder/'harness_bridge.py',folder/'pi_bridge.py',launcher,folder/'harness-launcher.json']
        backup=folder/('harness-before-'+str(time.time_ns()));backup.mkdir(mode=0o700)
        for old in affected:
            if old.exists():write_private(backup/old.name,old.read_bytes())
        write_private(folder/'harness-executor.json',json.dumps(settings,indent=2))
        for source in sources:write_private(folder/source.name,source.read_text(encoding='utf-8'))
        bridge=folder/'harness_bridge.py'
        if sys.platform=='win32':install_native_launcher(folder,bridge,launcher,state['portal'])
        else:
            import shlex
            line='#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(bridge))+' "$@"\n'
            write_private(launcher,line);launcher.chmod(0o700)
        print('Harness 将使用已授权的本机/助理权限，工作目录不是文件隔离。请亲自完成 ANet 的终端授权。',flush=True)
        cmd=[args.anet,'autoreply','set','--backend','exec','--agent','codex','--command',str(launcher),'--work-dir',str(work),'--model','','--untrusted','off','--admitted','direct','--api-timeout','900']
        try:
            result=subprocess.run(cmd,env=env)
            if result.returncode:raise RuntimeError('ANet 未完成执行授权。')
        except (OSError,RuntimeError):
            if (json.loads(conf_path.read_text(encoding='utf-8')).get('auto_reply') or {})==ar:
                for target in affected[1:]:
                    saved=backup/target.name
                    if saved.exists():write_private(target,saved.read_bytes())
                    else:target.unlink(missing_ok=True)
                if launcher.exists() and sys.platform!='win32':launcher.chmod(0o700)
            raise RuntimeError('执行授权未完成，请检查 anet status。配置备份：'+str(backup)) from None
        (folder/'harness-check.json').unlink(missing_ok=True)
        print('Harness 已配置。请运行 test-harness，再 install 同步就绪状态。');return
    if args.action in ['configure-pi','test-pi']:
        if kind!='agent' or executor!='pi':raise RuntimeError('仅 Pi Agent 记录可以配置 Pi；调用设备和 Codex Agent 保持原设置。')
        preflight()
        cfg=json.loads(conf_path.read_text(encoding='utf-8'));ar=cfg.get('auto_reply') or {}
        if args.action=='test-pi':
            if ar.get('command')!=str(pi_launcher(folder)):raise RuntimeError('请先运行 configure-pi')
            # Native test runs through the actual ANet adapter, without sending a task to another node.
            # ANet 0.3.9 returns only a pass/fail summary, deliberately hiding reply text.
            # Its CLI exits nonzero if any native execution path failed or returned an empty reply.
            answer=run('autoreply','test','Reply with exactly AGENT_SPACE_PI_OK and do not use any tools.',timeout=660)
            if not answer.strip():raise RuntimeError('Pi 本地自检没有返回检查结果，请检查 ANet 版本。')
            write_private(folder/'pi-check.json',json.dumps({'ok':True,'fingerprint':pi_fingerprint(root),'checked_at':int(time.time())}))
            print('Pi 本地模型自检通过；ANet → Pi → 结果返回已验证，尚未验证远程任务。')
            return
        if args.mode!='direct' or not args.work_dir:raise RuntimeError('需要用户明确指定 --work-dir 和 --mode direct；Pi 适配尚未验证 sandbox。')
        work=args.work_dir.expanduser().resolve()
        if not work.is_dir() or work==Path.home().resolve() or work.parent==work:raise RuntimeError('请选择已存在的具体工作目录，不能使用主目录或文件系统根目录。')
        command=args.pi_command or shutil.which('pi')
        if not command or not Path(command).expanduser().is_file():raise RuntimeError('未找到 Pi，请先安装并登录 Pi。')
        command=str(Path(command).expanduser().absolute())
        if ar and ar.get('command')!=str(pi_launcher(folder)):raise RuntimeError('已有其他执行器配置，不自动覆盖。请先由用户处理。')
        source=Path(__file__).resolve().with_name('pi_bridge.py')
        if not source.is_file():raise RuntimeError('请先把平台的 pi_bridge.py 下载到 connector.py 同一目录，并阅读代码。')
        if not sys.stdin.isatty():raise RuntimeError('请由用户在交互终端运行 configure-pi，完成 ANet 原生 direct 授权；不接受代答或管道批准。')
        # Save every affected file before preparing the adapter. Never enable a different inbound policy.
        backup=folder/('pi-before-'+str(time.time_ns()));backup.mkdir(mode=0o700)
        for old in [conf_path,folder/'pi-executor.json',folder/'pi_bridge.py',pi_launcher(folder),folder/'pi-check.json']:
            if old.exists():write_private(backup/old.name,old.read_bytes())
        write_private(folder/'pi_bridge.py',source.read_text(encoding='utf-8'))
        settings={'schema':1,'mode':'direct','pi_command':command,'node_command':shutil.which('node') or '', 'pi_config_dir':os.environ.get('PI_CODING_AGENT_DIR',''), 'work_dir':str(work),'model':args.model or '', 'timeout':600}
        write_private(folder/'pi-executor.json',json.dumps(settings,indent=2))
        bridge=folder/'pi_bridge.py';launcher=pi_launcher(folder)
        if sys.platform=='win32':install_native_launcher(folder,bridge,launcher,state['portal'])
        else:
            import shlex
            line='#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(bridge))+' "$@"\n'
            write_private(launcher,line);launcher.chmod(0o700)
        print('Pi 将以当前本地用户权限执行，工作目录不是文件隔离。请亲自完成下面 ANet 的终端授权。',flush=True)
        cmd=[args.anet,'autoreply','set','--backend','exec','--agent','codex','--command',str(launcher),'--work-dir',str(work),'--model',args.model or '', '--untrusted','off','--admitted','direct','--api-timeout','900']
        try:
            result=subprocess.run(cmd,env=env)
            if result.returncode:raise RuntimeError('ANet 未完成执行授权；未进行本地自检。')
        except (OSError,RuntimeError):
            # A refusal before native configuration changed must preserve the previous working adapter.
            current=json.loads(conf_path.read_text(encoding='utf-8')).get('auto_reply') or {}
            if current==ar:
                for target in [folder/'pi-executor.json',folder/'pi_bridge.py',launcher,folder/'harness-launcher.json']:
                    saved=backup/target.name
                    if saved.exists():write_private(target,saved.read_bytes())
                    else:target.unlink(missing_ok=True)
                if launcher.exists() and sys.platform!='win32':launcher.chmod(0o700)
            raise RuntimeError('执行授权未完成，请检查 anet status。备份：'+str(backup)) from None
        print('Pi 适配器已配置。请运行 test-pi 验证模型，再 install 同步状态。')
        return
    if args.action=='configure':
        if kind=='device':
            print('这是调用设备：身份绑定即可发送任务，无需配置权限后端或本机执行器。请检查 MCP 后运行 install。')
            return
        transport=args.transport or ('https' if sys.platform=='win32' else 'unix')
        if sys.platform=='win32' and transport!='https':raise RuntimeError('Windows 仅支持远程 HTTPS 权限接口；不写入 Unix socket 或本机 TCP 配置。')
        preflight();cfg=json.loads(conf_path.read_text(encoding='utf-8'));folder.mkdir(mode=0o700,exist_ok=True)
        protect_private(folder)
        svc=cfg.setdefault('modules',{}).setdefault('service',{});caps=svc.setdefault('capabilities',[])
        if any(not c.get('internal') for c in caps):raise RuntimeError('已有对外 service 能力，须先检查权限旁路。')
        old_hook=cfg.get('inbound',{}).get('admit')
        if old_hook and old_hook.get('capability')!=CAP:raise RuntimeError('已有其他准入服务，不自动覆盖。')
        target=endpoint(state) if transport=='https' else 'unix://'+str(sock)+':/admit'
        token_file=folder/('native-token.txt' if transport=='https' else 'backend-token.txt')
        # A native HTTPS backend authenticates as this bound node at the platform.
        local_token=state['node_token'] if transport=='https' else secrets.token_urlsafe(32)
        if transport=='https':
            remote(state['portal'],'/api/node/heartbeat',{'policy_ok':False,'executor_ready':False},state['node_token'])
        write_private(token_file,local_token+'\n')
        caps[:]=[c for c in caps if c.get('id')!=CAP]
        caps.append({'id':CAP,'url':target,'internal':True,'token_file':str(token_file)})
        if transport=='https':svc['allow_tcp']=True
        inbound=cfg.setdefault('inbound',{});inbound['policy']='closed';inbound['admit']={'capability':CAP,'cache_seconds':5}
        backup=folder/('config.before-portal-'+str(time.time_ns())+'.json')
        original=conf_path.read_text(encoding='utf-8');write_private(backup,original)
        run('stop')
        try:
            write_private(conf_path,json.dumps(cfg,indent=2));run('up')
            current=peers()
            if (current.get('admit') or {}).get('capability')!=CAP:raise RuntimeError('节点未加载平台权限接口')
        except Exception as error:
            try:run('stop')
            except RuntimeError:pass
            write_private(conf_path,original);run('up')
            raise RuntimeError('权限配置未通过检查，已恢复原配置并重新启动节点。原因：'+str(error)) from None
        print('权限接口已配置（'+transport+'）并备份。执行器设置保持原值；下一步请按用户授权'+('运行 configure-pi。' if executor=='pi' else '运行 configure-harness。' if executor in ['deepseek','workbuddy','custom'] else '配置 Codex 后端和 admitted 执行模式。'))
        return
    if args.action=='install':
        stable=folder/'connector.py'
        if Path(__file__).resolve()!=stable:shutil.copy2(__file__,stable)
        protect_private(stable)
        cmd=[sys.executable,str(stable),'serve','--data-dir',str(root),'--anet',args.anet]
        if sys.platform=='darwin':
            # Per-identity label prevents one identity overwriting another.
            import hashlib
            label='org.agentspace.connector.'+hashlib.sha256(str(root).encode()).hexdigest()[:8]
            f=Path.home()/'Library/LaunchAgents'/f'{label}.plist';f.parent.mkdir(parents=True,exist_ok=True)
            doc={'Label':label,'ProgramArguments':cmd,'RunAtLoad':True,'KeepAlive':True,'StandardOutPath':str(folder/'connector.log'),'StandardErrorPath':str(folder/'connector.log')}
            f.write_bytes(plistlib.dumps(doc));f.chmod(0o600)
            subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}',str(f)],capture_output=True)
            subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(f)],check=True)
            print('已安装当前用户登录启动：'+str(f))
        elif sys.platform.startswith('linux'):
            import shlex,hashlib
            name='agent-space-'+hashlib.sha256(str(root).encode()).hexdigest()[:8]
            f=Path.home()/'.config/systemd/user'/f'{name}.service';f.parent.mkdir(parents=True,exist_ok=True)
            # systemd does not use a shell; double quotes and escaping per systemd.syntax.
            quote=lambda x:'"'+x.replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'
            f.write_text('[Unit]\nDescription=Agent Space connector\nAfter=network-online.target\n[Service]\nExecStart='+' '.join(map(quote,cmd))+'\nRestart=on-failure\nRestartSec=5\n[Install]\nWantedBy=default.target\n')
            subprocess.run(['systemctl','--user','daemon-reload'],check=True);subprocess.run(['systemctl','--user','enable','--now',name],check=True)
            print('已安装 systemd 用户服务。退出登录后是否持续运行取决于用户 linger 设置。')
        elif sys.platform=='win32':
            # Current-user logon startup avoids administrator privileges and saved passwords.
            # A startup VBS uses WScript.Shell.Run in hidden mode; arguments are quoted by
            # subprocess.list2cmdline and the generated script contains no credentials.
            import hashlib
            name='AgentSpace-'+hashlib.sha256(str(root).encode()).hexdigest()[:8]
            pythonw=Path(sys.executable).with_name('pythonw.exe')
            if pythonw.exists():cmd[0]=str(pythonw)
            if any('\r' in x or '\n' in x for x in cmd):raise RuntimeError('启动路径不能包含换行')
            startup=Path(os.environ['APPDATA'])/'Microsoft/Windows/Start Menu/Programs/Startup'
            startup.mkdir(parents=True,exist_ok=True)
            line=subprocess.list2cmdline(cmd).replace('"','""')
            vbs='Set sh = CreateObject("WScript.Shell")\r\nsh.Run "'+line+'", 0, False\r\n'
            # Do not alter the ACL of Windows' shared Startup folder.
            f=startup/(name+'.vbs');f.write_text(vbs,encoding='utf-16');protect_private(f)
            subprocess.Popen(['wscript.exe',str(f)],close_fds=True)
            print('已安装当前用户登录启动项并启动状态心跳：'+str(f)+'。Windows 节点直接使用中心 HTTPS 权限接口。')
        else:raise RuntimeError('此系统需手动运行 serve。')
        return
    # serve: accept only daemon-authenticated local requests, check central ACL each time.
    protect_private(folder)
    if sys.platform=='win32':
        log=folder/'connector.log'
        stream=log.open('a',encoding='utf-8',buffering=1);protect_private(log)
        sys.stdout=sys.stderr=stream
    update_guard(folder,Path(__file__).resolve())
    # Recover ANet after machine reboot; never change its execution settings here.
    run('up')
    def heartbeat():
        while True:
            try:
                s=status()
                active=s.get('hub',{}).get('state')=='registered' and s.get('aid')==state['aid']
                if kind=='device':
                    remote(state['portal'],'/api/node/heartbeat',{'connected':active,'requester_ready':taskd_requester_ready(root)},state['node_token']);confirm_update(folder)
                    time.sleep(30)
                    continue
                x=peers();hook=x.get('admit') or {}
                good=x.get('policy')=='closed' and hook.get('capability')==CAP and not x.get('allow') and not x.get('trust') and not x.get('public_capabilities')
                active=s.get('hub',{}).get('state')=='registered' and s.get('aid')==state['aid']
                # Disabled/absent execution must never be reported as ready.
                ar=s.get('auto_reply') or {}
                ready=pi_ready(root,ar) if executor=='pi' else harness_ready(root,ar,executor) if executor in ['deepseek','workbuddy','custom'] else bool(ar.get('backend')=='exec' and ar.get('agent')=='codex' and ar.get('admitted') in ['direct','sandbox'] and ar.get('untrusted','off')=='off' and ar.get('work_dir') and Path(ar['work_dir']).is_dir() and (ar.get('admitted')!='sandbox' or ar.get('sandbox',{}).get('state')=='ready'))
                td=taskd_state(root)
                # With taskd, auto-reply is off; readiness is taskd's own self-test for the same executor.
                if td:ready=bool(td.get('executor_ready') and taskd_serves(td,executor) and not ar)
                remote(state['portal'],'/api/node/heartbeat',{'connected':active,'policy_ok':bool(good and active),'executor_ready':ready,'executor':executor,'requester_ready':taskd_requester_ready(root),'runtime':runtime_info(s,ar,executor,root),'work':work_info(root,executor),**({'capabilities':td.get('capabilities')} if td and ready else {})},state['node_token']);confirm_update(folder)
            except Exception as e:print('连接检查未完成：'+type(e).__name__,flush=True)
            time.sleep(15)
    def update_loop():
        time.sleep(random.randint(120,600))
        while True:
            if auto_update_on(folder):
                try:
                    if check_update(folder,Path(__file__).resolve(),state['portal'])=='updated':
                        print('已安装新版 connector，正在重启。',flush=True);restart_self()
                except Exception as e:
                    save_update_state(folder,last_check=int(time.time()),result='error:'+str(e)[:50]);print('更新检查未完成：'+str(e)[:120],flush=True)
            time.sleep(6*3600+random.randint(0,1800))
    def work_content_loop():
        # Independent from heartbeat and interaction sync; 50/minute stays below 600/10min.
        while True:
            try: push_work_content(root, executor, state)
            except Exception as e:print('任务正文读取未完成：'+type(e).__name__,flush=True)
            time.sleep(60)
    def sync_loop():
        seen={}
        while True:
            try:
                if sync_enabled(root):run_sync(run,state,seen)
            except Exception as e:print('交互同步未完成：'+type(e).__name__,flush=True)
            time.sleep(5)
    cfg=json.loads(conf_path.read_text(encoding='utf-8'))
    cap=next((c for c in cfg.get('modules',{}).get('service',{}).get('capabilities',[]) if c.get('id')==CAP),{})
    if kind=='device' or (cap.get('url','').startswith('https://') and cap.get('url')==endpoint(state)):
        # Hold a process-lifetime lock so repeated install does not spawn duplicate heartbeats.
        lock_path=folder/'heartbeat.lock';lock=lock_path.open('a+b');protect_private(lock_path)
        if lock_path.stat().st_size==0:lock.write(b'0');lock.flush()
        for attempt in range(30):  # a self-restart briefly overlaps the exiting process
            lock.seek(0)
            try:
                if sys.platform=='win32':
                    import msvcrt
                    msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except OSError:
                if attempt==29:raise RuntimeError('本身份的状态心跳已经运行；不再重复启动。') from None
                time.sleep(1)
        print('调用端模式：仅保持节点和连接心跳，不启用任务执行。' if kind=='device' else '中心 HTTPS 权限接口模式；本地进程仅报告连接状态。',flush=True)
        threading.Thread(target=update_loop,daemon=True).start()
        if kind!='device':
            threading.Thread(target=sync_loop,daemon=True).start()
            threading.Thread(target=work_content_loop,daemon=True).start()
        heartbeat()
        return
    if sys.platform=='win32':raise RuntimeError('Windows 节点尚未配置 HTTPS 权限接口，请运行 configure。')
    class UnixServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):daemon_threads=True
    class Local(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            admitted=False
            try:
                local=(folder/'backend-token.txt').read_text().strip()
                if self.path!='/admit' or not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+local):
                    self.send_response(403);self.end_headers();return
                n=int(self.headers.get('Content-Length','0'))
                if n>4096:raise RuntimeError('body too large')
                self.rfile.read(n)
                admitted=remote(state['portal'],'/api/node/admit',{'caller':self.headers.get('X-ANet-Caller','')},state['node_token']).get('admit') is True
            except Exception:pass  # fail closed; never echo credentials
            b=json.dumps({'admit':admitted}).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
    # Do not steal an active companion's socket.
    if sock.exists():
        import socket
        probe=socket.socket(socket.AF_UNIX)
        try:probe.connect(str(sock))
        except OSError:sock.unlink()
        else:raise RuntimeError('权限辅助程序已经运行')
        finally:probe.close()
    with UnixServer(str(sock),Local) as server:
        sock.chmod(0o600);threading.Thread(target=heartbeat,daemon=True).start();threading.Thread(target=sync_loop,daemon=True).start();threading.Thread(target=work_content_loop,daemon=True).start();threading.Thread(target=update_loop,daemon=True).start();print('本地权限接口已启动',flush=True)
        try:server.serve_forever()
        finally:sock.unlink(missing_ok=True)
if __name__=='__main__':
    sys.exit(cli_entry())

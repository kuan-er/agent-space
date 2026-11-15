"""Finite installer and UI contract; no credential/config discovery."""
from pathlib import Path
import datetime,os,re,shutil,subprocess

SOURCES={
 'codex':['https://developers.openai.com/codex/app-server/'],
 'claude_code':['https://code.claude.com/docs/en/setup','https://code.claude.com/docs/en/headless','https://code.claude.com/docs/en/goal'],
 'workbuddy':['https://open.workbuddy.cn/docs/openapi'],
 'deepseek_harness':['https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md','https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sdk/protocol/src/types.ts']}
NAMES={'codex':'Codex','claude_code':'Claude Code','workbuddy':'WorkBuddy','deepseek_harness':'DeepSeekHarness'}
INSTALL={
 'codex':{'kind':'reuse_or_official_install','official_page':'https://developers.openai.com/codex/cli/','manual_required':True,'artifact_url':None,'version':None,'sha256':None,'auth':'native Codex login; ANet never copies auth.json'},
 'claude_code':{'kind':'winget','package_id':'Anthropic.ClaudeCode','official_page':'https://code.claude.com/docs/en/setup','manual_required':True,'artifact_url':None,'version':None,'sha256':None,'requires_node':False,'auth':'native browser sign-in; do not bundle user credentials'},
 'workbuddy':{'kind':'official_desktop_and_optional_oauth','official_page':'https://www.workbuddy.cn/','manual_required':True,'artifact_url':None,'version':None,'sha256':None,'auth':'Active stdio MCP needs desktop login, no developer OAuth app; passive local-assistant/cloud API needs explicit owner OAuth. Client secret stays server-only.','runtime_surfaces':['active_stdio_mcp','local_assistant_messages','cloud_tasks_acp'],'not_equivalent_to':'CodeBuddy CLI'},
 'deepseek_harness':{'kind':'pinned_python_wheels','package_id':'deepseek-harness-sdk','official_page':SOURCES['deepseek_harness'][0],'manual_required':True,'artifact_url':None,'version':None,'sha256':None,'auth':'provider/model + explicit DSH_HOME and native provider auth; no ~/.dsh discovery','runtime_dependency':'deepseek-harness-runtime-bin at exact matching version'}
}
DOCUMENTED={
 'codex':dict(session_resume=True,native_goal=True,goal_pause=True,goal_resume=True,cancel=True,status=True,result=True),
 'claude_code':dict(session_resume=True,native_goal=True,goal_pause=False,goal_resume=False,cancel=True,status=True,result=True),
 'workbuddy':dict(session_resume=False,native_goal=False,goal_pause=False,goal_resume=False,cancel=False,status=True,result=True),
 'deepseek_harness':dict(session_resume=True,native_goal=True,goal_pause=True,goal_resume=True,cancel=False,status=True,result=True)
}

def candidates(harness):
    found=[]
    command={'codex':'codex','claude_code':'claude','deepseek_harness':'dsh'}.get(harness)
    if command:
        p=shutil.which(command)
        if p:found.append(Path(p))
    local=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))
    if harness=='codex':found.append(local/'Programs/OpenAI/Codex/bin/codex.exe')
    if harness=='claude_code':found.extend([Path.home()/'.local/bin/claude.exe',Path.home()/'.claude/local/claude.exe'])
    return list(dict.fromkeys(str(p.resolve()) for p in found if p.is_file() and p.suffix.lower() not in ('.cmd','.bat','.ps1')))

def probe_one(harness,explicit_path=None,*,surface=None):
    if harness not in NAMES:raise ValueError('unknown harness')
    paths=[str(Path(explicit_path).resolve())] if explicit_path else candidates(harness)
    row={'id':harness,'name':NAMES[harness],'available':False,'path':None,'version':None,'state':'needs_setup','auth_state':'not_checked','detail':'尚未发现可用原生运行时','source_binding_verified':False,'model_request_started':False,'sources':SOURCES[harness],'install':dict(INSTALL[harness]),'capabilities':{key:{'documented':value,'implemented':value,'probe_verified':False,'e2e_verified':False} for key,value in DOCUMENTED[harness].items()}}
    if harness=='workbuddy':
        surface=surface or 'local'
        if surface not in ('local','cloud'):raise ValueError('WorkBuddy surface must be local or cloud')
        row.update(surface='workbuddy_local_assistant' if surface=='local' else 'workbuddy_cloud_tasks',
                   native_session_available=surface=='cloud',native_terminal_available=surface=='cloud',
                   authentication_surfaces={
                    'active_stdio_mcp':{'requires_oauth_application':False,'requires_desktop_login':True,'login_verified':False},
                    'passive_local_assistant':{'requires_oauth_application':True,'requires_owner_grant':True,
                        'required_scopes':['user.localassistant.readable','user.localassistant.invokable'],'authorization_verified':False},
                    'cloud_tasks_acp':{'requires_oauth_application':True,'requires_owner_grant':True,
                        'required_scopes':['user.task.readable','user.task.invokable'],'authorization_verified':False}})
        if surface=='cloud':row['capabilities']['session_resume'].update(documented=True,implemented=True)
        row['capabilities']['status']['scope']='online+message_history' if surface=='local' else 'exact_cloud_task'
        row['capabilities']['result']['scope']='correlated_reply_only' if surface=='local' else 'cloud_task+ACP'
        row['detail']=('主动 MCP 接入无需开发者应用；ANet 被动派活需本人本机助理授权，回复仅作为关联观察。'
                       if surface=='local' else '云端任务使用单独的账号授权权限与 ACP 来源。')
        return row
    if not paths:return row
    p=Path(paths[0]);
    if not p.is_file() or p.suffix.lower() in ('.cmd','.bat','.ps1'):raise ValueError('probe requires installed native executable')
    try:
        r=subprocess.run([str(p),'--version'],capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        text=(r.stdout+r.stderr)[:8192].decode('utf8','replace')
        pattern={'codex':r'codex-cli\s+([\w.\-]+)','claude_code':r'(\d+\.\d+\.\d+[^\s]*)','deepseek_harness':r'(\d+\.\d+\.\d+[^\s]*)'}[harness]
        match=re.search(pattern,text)
        if r.returncode or not match:row['detail']='运行时版本探针未通过';return row
        row.update(available=True,path=str(p),version=match.group(1),state='installed_unverified',detail='运行时已发现；认证、能力握手和端到端来源绑定仍需验证')
        row['install']['manual_required']=False;row['install']['kind']='reuse_existing'
    except (OSError,subprocess.TimeoutExpired):row['detail']='运行时探针暂不可用；不会自动安装或发起模型请求'
    return row

def probe_all():
    return {'schema':'anet-harnesses/1','observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'harnesses':[probe_one(h) for h in NAMES],'credentials_read':False,'tasks_started':0}

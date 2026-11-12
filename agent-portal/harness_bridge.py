#!/usr/bin/env python3
"""ANet adapter for DeepSeek headless, WorkBuddy Open API, and stdin CLIs.

Configuration and credentials stay on the executor. No shell is used. Native
permission checks remain active; ANet owns its admission and receipt protocol.
"""
import json
import hashlib
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from pi_bridge import MAX_REPLY, parse_invocation, terminate_tree


def run_command(settings, work, prompt):
    command = Path(settings['command'])
    if not command.is_file():
        raise RuntimeError('Harness 命令不存在，请在本机重新配置。')
    kind = settings['harness']
    args = settings.get('args', [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise RuntimeError('固定参数必须是字符串数组。')
    argv = [str(command), *args]
    if kind == 'deepseek':
        if any(a in ['--profile','--json','--approval-policy'] or a.startswith(('--profile=','--approval-policy=','--json=')) for a in args):
            raise RuntimeError('DeepSeek 固定参数不能重复 profile、JSON 或审批策略；请直接使用原始 dsh 命令。')
        argv = [str(command), '--profile', 'headless', *args, '--json', '--approval-policy', 'never', '-']
    if sys.platform == 'win32' and command.suffix.lower() in ['.cmd', '.bat']:
        # cmd expands metacharacters even in some quoted contexts. Require a
        # real executable/wrapper for arbitrary custom arguments on Windows.
        if any(c in a for a in argv for c in '\r\n%&|<>^!'):
            raise RuntimeError('Windows 启动参数含不支持的字符，请使用可执行文件包装器。')
        argv = [os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/s', '/c', subprocess.list2cmdline(argv)]
    env = dict(os.environ)
    if settings.get('node_command'):
        node = Path(settings['node_command'])
        if not node.is_file():raise RuntimeError('Node 程序不存在，请重新配置。')
        env['PATH'] = str(node.parent) + os.pathsep + env.get('PATH', '')
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, cwd=work, env=env, stdin=subprocess.PIPE, stdout=out, stderr=err)
        try:
            proc.communicate(prompt.encode('utf-8'), timeout=settings.get('timeout', 600))
        except subprocess.TimeoutExpired:
            terminate_tree(proc);proc.wait()
            raise RuntimeError('Harness 执行超时，已终止本次本机进程。') from None
        if proc.returncode:
            if kind == 'deepseek':
                err.seek(0);diagnostic=err.read(8192).decode('utf-8',errors='replace').lower()
                if any(word in diagnostic for word in ['deepseek_api_key','unauthorized','authentication','api key','api_key','credentials']):
                    raise RuntimeError('DeepSeek headless 模型授权未就绪；请在本机检查启动环境或 .env，不要上传密钥。')
            raise RuntimeError('Harness 执行失败（退出码 %s）；请在本机检查登录、权限和模型配置。' % proc.returncode)
        out.seek(0)
        if kind == 'deepseek':
            total = 0;final = None
            for line in out:
                total += len(line)
                if total > 32 * MAX_REPLY:raise RuntimeError('Harness 事件输出超过允许大小。')
                try:event = json.loads(line)
                except ValueError:raise RuntimeError('DeepSeek 未返回有效 JSON 事件，请升级到支持 headless --json 的版本。') from None
                if not isinstance(event, dict):raise RuntimeError('DeepSeek 事件格式不正确。')
                if final is not None:raise RuntimeError('DeepSeek 在最终事件后仍有输出。')
                if event.get('type') == 'error':raise RuntimeError('DeepSeek 报告执行错误。')
                if event.get('type') == 'final':final = event
            if not final or not isinstance(final.get('reason'), dict) or final['reason'].get('kind') != 'completed':
                raise RuntimeError('DeepSeek 未确认本轮完成，不会标记成功。')
            reply = final.get('text', '')
        else:
            data = out.read(MAX_REPLY + 1)
            if len(data) > MAX_REPLY:raise RuntimeError('Harness 返回内容超过允许大小。')
            reply = data.decode('utf-8', errors='strict')
        if not isinstance(reply, str) or not reply.strip() or len(reply.encode('utf-8')) > MAX_REPLY:
            raise RuntimeError('Harness 未返回有效的最终文本。')
        return reply.strip()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('WorkBuddy API 重定向被拒绝，请检查官方服务状态。')


class WorkBuddyUnauthorized(RuntimeError):
    pass


def workbuddy_request(access_token, path, data=None, timeout=15):
    req = Request('https://www.workbuddy.cn/openapi/v2' + path,
        data=json.dumps(data, ensure_ascii=False).encode() if data is not None else None,
        headers={'Authorization':'Bearer ' + access_token, 'Accept':'application/json', 'Content-Type':'application/json'})
    try:
        with build_opener(NoRedirect).open(req, timeout=timeout) as r:
            raw = r.read(MAX_REPLY + 1)
        if len(raw) > MAX_REPLY:raise RuntimeError('WorkBuddy 响应超过允许大小。')
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get('code') != 0:
            raise RuntimeError('WorkBuddy 未接受请求，请在本机检查授权和助理状态。')
        value = result.get('data')
        if not isinstance(value, dict):raise RuntimeError('WorkBuddy 响应格式不正确。')
        return value
    except HTTPError as error:
        if error.code == 401:
            raise WorkBuddyUnauthorized('WorkBuddy 授权已失效，请在连接详情中重新授权。') from None
        raise RuntimeError('WorkBuddy API 返回 HTTP %s；请在本机刷新授权或检查访问权限。' % error.code) from None
    except (URLError, TimeoutError, ValueError):
        raise RuntimeError('WorkBuddy 网络或响应异常；若已发送任务，请先在助理核实，不要直接重发。') from None


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:json.dump(value, f)


def workbuddy_broker_token(connection_file, rejected=None, timeout=15):
    try:
        state = json.loads(Path(connection_file).read_text(encoding='utf-8'))
        base = state['portal'].rstrip('/')
        u = urlsplit(base)
        if u.scheme != 'https' or not u.netloc or u.username or u.password or u.path or u.query or u.fragment:
            raise ValueError('invalid origin')
        node_token = state['node_token']
        if not isinstance(node_token,str) or not node_token or any(c.isspace() for c in node_token):
            raise ValueError('invalid credential')
        body = {'rejected_token_sha256':hashlib.sha256(rejected.encode()).hexdigest()} if rejected else {}
        req = Request(base + '/api/node/workbuddy/token', data=json.dumps(body).encode(),
                      headers={'Authorization':'Bearer '+node_token,'Content-Type':'application/json'})
        with build_opener(NoRedirect).open(req, timeout=timeout) as response:
            raw = response.read(65537)
        if len(raw)>65536:raise ValueError('oversize')
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError('invalid token response')
        token=value.get('access_token')
        if not isinstance(token,str) or not token or any(c.isspace() for c in token) or type(value.get('expires_at')) is not int:
            raise ValueError('invalid access token')
        return value
    except (OSError,ValueError,KeyError,TypeError):
        raise RuntimeError('WorkBuddy 授权服务不可用或需要重新授权，请在网页连接详情检查；不会自动重发任务。') from None


def run_workbuddy(settings, work, prompt, folder):
    broker = settings.get('workbuddy_auth') == 'portal'
    credential = Path(settings['connection_file'] if broker else settings['credential_file'])
    pending = folder / 'workbuddy-pending.json'
    # A stable sidecar survives atomic credential-file rotation. It also avoids
    # Windows byte locks blocking another process reading connection.json.
    fd = os.open(credential.with_name(credential.name+'.workbuddy.lock'), os.O_RDWR | os.O_CREAT | getattr(os,'O_NOFOLLOW',0), 0o600)
    with os.fdopen(fd,'r+b') as lock:
        try:
            if sys.platform == 'win32':
                import msvcrt
                if not os.fstat(lock.fileno()).st_size:lock.write(b'\0');lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:raise RuntimeError('此 WorkBuddy 助理正在处理另一个接入任务，请稍后再试。') from None
        if pending.exists():
            raise RuntimeError('上次 WorkBuddy 任务效果未确认。请先在助理核实，再由设备主人移除 portal/workbuddy-pending.json；不会自动重发。')
        deadline = time.monotonic() + settings.get('timeout', 600)
        authorization = {} if broker else {'access_token':json.loads(credential.read_text(encoding='utf-8')).get('access_token','')}
        if not broker and (not isinstance(authorization['access_token'],str) or not authorization['access_token'].strip() or any(c.isspace() for c in authorization['access_token'])):
            raise RuntimeError('WorkBuddy 授权文件需包含有效 access_token。')
        def request(path, data=None):
            nonlocal authorization
            remaining = deadline - time.monotonic()
            if remaining <= 0:raise RuntimeError('等待 WorkBuddy 超时，原任务可能仍在运行，请先在本机核实。')
            if broker and authorization.get('expires_at',0) <= time.time()+60:
                authorization = workbuddy_broker_token(credential, timeout=min(15,remaining))
            token = authorization['access_token']
            try:
                return workbuddy_request(token, path, data, min(15,max(0.001,deadline-time.monotonic())))
            except WorkBuddyUnauthorized:
                if not broker:raise
                authorization = workbuddy_broker_token(credential, token, timeout=min(15,max(0.001,deadline-time.monotonic())))
                if data is not None:
                    raise RuntimeError('WorkBuddy 授权已更新，但本次发送结果未确认，请先在助理核实；不会自动重发。') from None
                return workbuddy_request(authorization['access_token'],path,timeout=min(15,max(0.001,deadline-time.monotonic())))
        if request('/localassistant').get('online') is not True:
            raise RuntimeError('WorkBuddy PC 本地助理不在线，请先启动并连接助理。')
        marker = 'AGENT_SPACE_' + secrets.token_hex(16)
        begin, end = marker + '_BEGIN', marker + '_END'
        task = (prompt + '\n\n本次任务授权目录：' + str(work)
            + '\n如需要访问工作区，请先核对 WorkBuddy 当前工作区；不匹配时停止并在最终结果中说明。'
            + '\n任务结束后，将最终答复放在以下两行标记之间；中间步骤不要输出这些标记：\n' + begin + '\n最终答复\n' + end)
        # Written before sending: a network failure must not cause an automatic duplicate effect.
        private_json(pending, {'phase':'sending','created_at':int(time.time())})
        message_id = request('/localassistant/message', {'content':task, 'msg_type':'text'}).get('message_id')
        if not isinstance(message_id, str) or not message_id:raise RuntimeError('WorkBuddy 未返回消息标识；请在本机核实任务是否已发送。')
        private_json(pending, {'phase':'sent','message_id':message_id,'created_at':int(time.time())})
        while time.monotonic() < deadline:
            messages = request('/localassistant/message?' + urlencode({'message_id':message_id})).get('messages', [])
            if not isinstance(messages, list):raise RuntimeError('WorkBuddy 消息格式不正确。')
            parts = []
            for message in messages:
                if not isinstance(message, dict):continue
                if message.get('role') == 'user' and message.get('message_id') != message_id:
                    raise RuntimeError('WorkBuddy 出现其他用户消息，任务关联已不确定；请在本机核实。')
                if message.get('role') != 'assistant':continue
                content = message.get('content', [])
                if not isinstance(content, list):continue
                parts.extend(x if isinstance(x, str) else x.get('text', '') if isinstance(x, dict) else '' for x in content)
            text = '\n'.join(parts)
            if len(text.encode('utf-8')) > MAX_REPLY:raise RuntimeError('WorkBuddy 返回内容超过允许大小。')
            if begin in text and end in text.split(begin, 1)[1]:
                reply = text.split(begin, 1)[1].split(end, 1)[0].strip()
                if not reply:raise RuntimeError('WorkBuddy 最终答复为空。')
                pending.unlink();return reply
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise RuntimeError('等待 WorkBuddy 最终答复超时；原任务可能仍在运行，请先在助理核实。')


def main(argv=None):
    args = parse_invocation(sys.argv[1:] if argv is None else argv)
    folder = Path(__file__).resolve().parent
    settings = json.loads((folder / 'harness-executor.json').read_text(encoding='utf-8'))
    if settings.get('mode') != 'direct' or settings.get('harness') not in ['deepseek','workbuddy','custom']:
        raise RuntimeError('仅支持用户明确授权的 harness 和 direct 模式。')
    work = Path(settings['work_dir']).resolve()
    if args.cd.resolve() != work or not work.is_dir() or args.output_last_message.parent.resolve() != work:
        raise RuntimeError('执行或回复目录与已授权目录不一致。')
    if args.model:raise RuntimeError('此 harness 的模型由本机原生配置管理，不接受 Codex 模型参数覆盖。')
    instructions = []
    for override in args.config:
        key, sep, value = override.partition('=')
        if key != 'developer_instructions' or not sep:raise RuntimeError('不支持的执行配置覆盖。')
        value = json.loads(value)
        if not isinstance(value, str):raise RuntimeError('固定指令必须是文本。')
        instructions.append(value)
    conversation = sys.stdin.buffer.read(MAX_REPLY + 1)
    if len(conversation) > MAX_REPLY:raise RuntimeError('任务输入超过允许大小。')
    prompt = ('\n'.join(instructions) + '\n\n' if instructions else '') + conversation.decode('utf-8')
    reply = run_workbuddy(settings, str(work), prompt, folder) if settings['harness'] == 'workbuddy' else run_command(settings, str(work), prompt)
    fd = os.open(args.output_last_message, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:f.write(reply)


if __name__ == '__main__':
    try:main()
    except Exception as error:
        # No provider response bodies, stderr, prompt, or credentials leave this bridge.
        message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print('Agent Space harness: ' + message, file=sys.stderr);sys.exit(1)

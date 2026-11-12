#!/usr/bin/env python3
"""Pi executor for ANet 0.3.9's Codex CLI wire format; no Codex process is launched.

Only explicitly authorized direct runs are supported. ANet still owns admission,
task history, execution budgets, cancellation, and signed task receipts.
"""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

MAX_REPLY = 4 * 1024 * 1024


def parse_invocation(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['exec'])
    p.add_argument('--skip-git-repo-check', action='store_true')
    p.add_argument('--ephemeral', action='store_true')
    p.add_argument('-s', '--sandbox', choices=['workspace-write'], required=True)
    p.add_argument('-o', '--output-last-message', type=Path, required=True)
    p.add_argument('-C', '--cd', type=Path, required=True)
    p.add_argument('-m', '--model')
    p.add_argument('-c', '--config', action='append', default=[])
    p.add_argument('prompt', choices=['-'])
    return p.parse_args(argv)


def terminate_tree(proc):
    """Children share ANet's process group; an ANet cancellation also kills them."""
    if sys.platform == 'win32':
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True)
        return
    children = [proc.pid]
    try:
        rows = subprocess.check_output(['/bin/ps', '-axo', 'pid=,ppid='], text=True)
        pairs = [tuple(map(int, line.split())) for line in rows.splitlines()]
        for parent in children:
            children.extend(pid for pid, ppid in pairs if ppid == parent and pid not in children)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    for pid in reversed(children):
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass


def run_pi(settings, work_dir, conversation, instructions='', model=None):
    command = Path(settings['pi_command'])
    if not command.is_file():
        raise RuntimeError('Pi 程序不存在，请在本机重新配置 Pi 路径。')
    argv = [str(command), '--print', '--mode', 'text', '--no-session', '--no-approve']
    if model or settings.get('model'):
        argv += ['--model', model or settings['model']]
    # Keep both the peer's task and the fixed instructions off the process command line.
    prompt = (instructions + '\n\n' if instructions else '') + conversation
    if sys.platform == 'win32' and command.suffix.lower() in ['.cmd', '.bat']:
        argv = [os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/s', '/c', subprocess.list2cmdline(argv)]
    env = dict(os.environ)
    # Login services have a small PATH; Pi's npm launcher still needs its installed Node runtime.
    node = settings.get('node_command')
    if node:
        if not Path(node).is_file():raise RuntimeError('Node 程序不存在，请重新配置 Pi 执行器。')
        env['PATH'] = str(Path(node).parent) + os.pathsep + env.get('PATH', '')
    if settings.get('pi_config_dir'):
        env['PI_CODING_AGENT_DIR'] = settings['pi_config_dir']
    # Spool output to private files instead of buffering arbitrary tool output in memory.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, cwd=work_dir, env=env, stdin=subprocess.PIPE, stdout=out, stderr=err)
        try:
            proc.communicate(prompt.encode('utf-8'), timeout=settings.get('timeout', 600))
        except subprocess.TimeoutExpired:
            terminate_tree(proc); proc.wait()
            raise RuntimeError('Pi 执行超时，已终止本次进程。') from None
        if proc.returncode:
            # Pi's stderr can contain provider details: never return it to a remote caller.
            err.seek(0); diagnostic = err.read(16384).decode('utf-8', errors='replace')
            for key, value in env.items():
                if value and any(word in key.upper() for word in ['TOKEN','SECRET','PASSWORD','API_KEY']):diagnostic=diagnostic.replace(value,'[redacted]')
            diagnostic = re.sub(r'(?i)(Bearer\s+)[^\s"\']+', r'\1[redacted]', diagnostic)
            diagnostic = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[redacted]', diagnostic)
            try:
                path=Path(__file__).with_name('pi-last-error.log')
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0),0o600)
                with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(diagnostic)
                path.chmod(0o600)
            except OSError:pass
            raise RuntimeError('Pi 执行失败（退出码 %s）；请在本机检查 Pi 登录和模型配置。' % proc.returncode)
        out.seek(0); data = out.read(MAX_REPLY + 1)
        if len(data) > MAX_REPLY:
            raise RuntimeError('Pi 返回内容超过允许大小。')
        reply = data.decode('utf-8', errors='replace').strip()
        if not reply:
            raise RuntimeError('Pi 未返回有效结果。')
        return reply


def main(argv=None):
    args = parse_invocation(sys.argv[1:] if argv is None else argv)
    settings = json.loads(Path(__file__).with_name('pi-executor.json').read_text(encoding='utf-8'))
    if settings.get('mode') != 'direct':
        raise RuntimeError('Pi 适配器仅支持用户明确授权的 direct 执行。')
    work = Path(settings['work_dir']).resolve()
    if args.cd.resolve() != work or not work.is_dir():
        raise RuntimeError('Pi 工作目录与已授权目录不一致。')
    target = args.output_last_message
    if target.parent.resolve() != work:
        raise RuntimeError('回复文件必须位于已授权工作目录。')
    instructions = []
    for override in args.config:
        key, sep, value = override.partition('=')
        if key != 'developer_instructions' or not sep:
            raise RuntimeError('不支持的执行器配置覆盖。')
        instructions.append(json.loads(value))  # ANet tomlString is a JSON-compatible quoted string.
    conversation = sys.stdin.read(MAX_REPLY + 1)
    if len(conversation.encode('utf-8')) > MAX_REPLY:
        raise RuntimeError('任务输入超过允许大小。')
    reply = run_pi(settings, str(work), conversation, '\n'.join(instructions), args.model)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(target, flags, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(reply)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Pi Agent: ' + str(error), file=sys.stderr)
        sys.exit(1)

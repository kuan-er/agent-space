import argparse
import getpass
import json
import os
from pathlib import Path
import socket
import sys

from . import __version__
from .backend import execute
from .native import discover, install

DEFAULT_PORTAL = 'https://124.222.173.36:8888'
HARNESSES = ('codex', 'claude_code', 'workbuddy', 'deepseek_harness')
NAMES = dict(zip(HARNESSES, ('Codex', 'Claude Code', 'WorkBuddy', 'DeepSeek Harness')))
STATE_LABELS = {
    'connected': '已连接', 'installed': '安装完成', 'reused': '已复用原来的 ANet 程序',
    'ready': '已就绪', 'needs_setup': '需要设置', 'not_logged_in': '尚未登录',
    'signed_in': '已登录', 'signed_out': '已退出登录', 'configured': '已配置',
    'not_configured': '尚未配置', 'unconfigured': '尚未连接', 'conflict': '现有配置需要检查',
    'disabled': '已停用', 'update_available': '配置可更新', 'running': '运行中',
    'stopped': '已停止', 'stopping': '正在停止', 'missing': '尚未安装',
    'not_installed': '尚未安装', 'needs_login': '请在智能体软件中登录',
    'unavailable': '暂不可用', 'not_enabled': '尚未开启', 'prepared': '等待确认',
    'testing': '正在自检', 'needs_test': '需要自检', 'needs_attention': '需要处理',
    'error': '操作未完成', 'failed': '操作未完成', 'unknown': '状态待确认',
    'unverified': '结果尚未确认', 'verified': '连接已验证',
}


def parser():
    p = argparse.ArgumentParser(prog='anet-config', description='ANet · 连接账户、配置智能体和检查服务。任务请在智能体软件中发起。')
    p.add_argument('--version', action='version', version='ANet Config ' + __version__)
    p.add_argument('--data-dir', type=Path, default=Path(os.environ.get('ANET_DATA_DIR', Path.home() / '.anet')))
    p.add_argument('--anet-binary', help='指定已有的 ANet 程序')
    p.add_argument('--runtime', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--json', action='store_true', help='输出机器可读结果')
    p.add_argument('--no-color', action='store_true', help='纯文本输出（默认）')
    sub = p.add_subparsers(dest='command', required=True)
    for name, help_text in [('setup', '首次设置'), ('login', '登录 ANet 账户并连接本机')]:
        s = sub.add_parser(name, help=help_text)
        s.add_argument('--portal', default=DEFAULT_PORTAL, help='门户地址')
        s.add_argument('--username', help='账户名；未提供时交互输入')
        s.add_argument('--password-stdin', action='store_true', help='从标准输入读取密码，用于自动化')
        s.add_argument('--name', default=socket.gethostname(), help='设备显示名称')
        if name == 'setup':
            s.add_argument('--workspace', type=Path, help='智能体工作目录')
            s.add_argument('--agents', nargs='+', choices=HARNESSES, help='要接入的智能体软件')
    sub.add_parser('status', help='显示连接与软件状态')
    sub.add_parser('doctor', help='检查服务并说明需要处理的问题')
    sub.add_parser('logout', help='退出本地账户显示，保留设备连接')
    agents = sub.add_parser('agents', help='查看或接入智能体软件')
    agents.add_argument('operation', nargs='?', choices=('list', 'connect', 'verify'), default='list')
    agents.add_argument('harness', nargs='?', choices=HARNESSES)
    workspace = sub.add_parser('workspace', help='查看或设置工作目录')
    workspace.add_argument('path', type=Path, nargs='?')
    service = sub.add_parser('service', help='管理本机 ANet 服务')
    service.add_argument('operation', choices=('status', 'start', 'stop'), default='status', nargs='?')
    receiver = sub.add_parser('receiver', help='设置此设备接收获授权的协作任务')
    receiver.add_argument('operation', choices=('status', 'prepare', 'enable'), default='status', nargs='?')
    receiver.add_argument('--harness', choices=HARNESSES, help='用于接收任务的软件')
    receiver.add_argument('--workspace', type=Path, help='任务工作目录')
    receiver.add_argument('--username', help='开启时用于核验账户归属')
    receiver.add_argument('--password-stdin', action='store_true', help='从标准输入读取门户密码')
    sub.add_parser('install-node', help='安装签名验证后的 ANet 节点；复用已有程序')
    rpc = sub.add_parser('rpc', help=argparse.SUPPRESS)
    rpc.add_argument('--runtime', type=Path, default=argparse.SUPPRESS)
    for child in sub.choices.values():
        child.add_argument('--json', action='store_true', default=argparse.SUPPRESS, help='输出机器可读结果')
        child.add_argument('--no-color', action='store_true', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return p


def context(args):
    if args.runtime:
        value = json.loads(args.runtime.read_text(encoding='utf-8-sig'))
        if value.get('schema') != 'anet-desktop-runtime/1':
            raise ValueError('运行配置格式不正确。')
        return value
    root = args.data_dir.expanduser().absolute()
    from .binary import resolved_context
    value = {'schema': 'anet-desktop-runtime/1', 'anet_root': str(root), 'node_root': str(root / 'taskd'),
            'anet_binary': str(args.anet_binary) if args.anet_binary else '', 'python_exe': sys.executable,
            'harness_root': str(Path(__file__).resolve().parent.parent),
            'persistent_root': str(root / 'client'), 'harness_config_path': str(root / 'client/harness-config.json')}
    return resolved_context(value)[0]


def prompt(label, default=None):
    if not sys.stdin.isatty():
        raise ValueError('非交互运行请提供 ' + label + '。')
    value = input(label + (' [' + str(default) + ']' if default else '') + '：').strip()
    return value or default or ''


def login_request(args):
    if args.command == 'setup' and sys.stdin.isatty() and args.portal == DEFAULT_PORTAL:
        args.portal = prompt('门户地址', args.portal)
    username = args.username or prompt('账户名')
    if args.password_stdin:
        password = sys.stdin.readline(8193).rstrip('\r\n')
    else:
        if not sys.stdin.isatty():
            raise ValueError('非交互运行请用 --password-stdin，密码不要放进命令参数。')
        password = getpass.getpass('密码：')
    if not username or not password or len(password) > 8192:
        raise ValueError('请输入有效的账户名和密码。')
    return {'action': 'account.login', 'portal': args.portal, 'username': username,
            'password': password, 'name': args.name, 'executor': 'codex'}


def successful(result):
    if isinstance(result, list):
        return all(successful(item) for item in result)
    if not isinstance(result, dict):
        return True
    if result.get('ok') is False or result.get('state') in ('error', 'failed', 'needs_attention'):
        return False
    return all(successful(item) for item in result.values() if isinstance(item, (dict, list)))


def display(value, as_json=False):
    if as_json:
        print(json.dumps(value, ensure_ascii=False))
        return
    if isinstance(value, dict):
        for key in ('message', 'reason', 'summary', 'detail_check', 'detail'):
            if isinstance(value.get(key), str) and value[key]:
                print(value[key])
                break
        else:
            if value.get('state'):
                print(STATE_LABELS.get(value['state'], value['state']))
        for key, item in value.items():
            if key in ('schema', 'message', 'reason', 'summary', 'state', 'ok', 'detail', 'detail_check', 'binary_resolution'):
                continue
            if isinstance(item, list):
                for row in item:
                    if isinstance(row, dict):
                        description = row.get('message') or row.get('reason') or row.get('detail') or STATE_LABELS.get(row.get('state'), row.get('state', ''))
                        print('  ' + str(row.get('name') or NAMES.get(row.get('harness') or row.get('id'), key)) + ' · ' + str(description))
                    else:
                        print('  ' + str(row))
            elif isinstance(item, dict):
                display(item)
            elif key in ('portal', 'url', 'username', 'workspace', 'path', 'binary', 'version'):
                print('  ' + {'portal': '门户', 'url': '门户', 'username': '账户', 'workspace': '工作目录', 'path': '目录', 'binary': 'ANet 程序', 'version': '版本'}[key] + '：' + str(item))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        ctx = context(args)
        if args.command == 'rpc':
            raw = sys.stdin.buffer.read(65537)
            if len(raw) > 65536:
                raise ValueError('配置请求过大。')
            request = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(request, dict):
                raise ValueError('配置请求格式不正确。')
            result = execute(request, ctx)
            display(result, True)
            return 0 if successful(result) else 1
        if args.command in ('install-node', 'setup'):
            result = install(args.data_dir, explicit=ctx.get('anet_binary') or args.anet_binary)
            ctx['anet_binary'] = result['binary']
            if args.command == 'install-node':
                display(result, args.json)
                return 0
        if args.command in ('login', 'setup'):
            request = login_request(args)
            try:
                result = execute(request, ctx)
            finally:
                request['password'] = ''
            if args.command == 'setup' and successful(result):
                results = {'account': result}
                workspace = args.workspace
                if not workspace and sys.stdin.isatty():
                    workspace = Path(prompt('工作目录', Path.home() / 'ANetWork'))
                if workspace:
                    workspace.expanduser().absolute().mkdir(parents=True, exist_ok=True)
                    results['workspace'] = execute({'action': 'config.workspace', 'path': str(workspace.expanduser().absolute())}, ctx)
                agents = args.agents
                if not agents and sys.stdin.isatty():
                    print('可接入：1 Codex  2 Claude Code  3 WorkBuddy  4 DeepSeek Harness')
                    choices = prompt('输入软件编号，以空格分隔；直接回车稍后配置')
                    try:
                        if any(n not in ('1', '2', '3', '4') for n in choices.split()):
                            raise ValueError('invalid choice')
                        agents = [HARNESSES[int(n) - 1] for n in choices.split()]
                    except (ValueError, IndexError):
                        raise ValueError('软件编号不正确。')
                if agents:
                    results['agents'] = [execute({'action': 'config.connect', 'harness': h}, ctx) for h in dict.fromkeys(agents)]
                results['service'] = execute({'action': 'config.service', 'operation': 'start'}, ctx)
                result = results
                if not args.json:
                    print('设置结果：')
            display(result, args.json)
        elif args.command == 'status':
            result = {'account': execute({'action': 'account.status'}, ctx), 'configuration': execute({'action': 'config.status'}, ctx)}
            display(result, args.json)
        elif args.command == 'receiver':
            request = {'action': 'config.receiver', 'operation': 'status' if args.operation == 'status' else 'prepare'}
            if args.operation != 'status':
                if not args.harness:
                    raise ValueError('请用 --harness 选择接收任务的软件。')
                request['harness'] = args.harness
                if args.workspace:
                    request['workspace'] = str(args.workspace.expanduser().absolute())
            result = execute(request, ctx)
            if args.operation == 'enable' and successful(result) and result.get('plan_id'):
                if not args.json:
                    print('开启接收任务将运行一次简短模型自检；原有软件权限仍然有效。')
                username = args.username or prompt('门户账户名')
                password = sys.stdin.readline(8193).rstrip('\r\n') if args.password_stdin else getpass.getpass('门户密码：') if sys.stdin.isatty() else ''
                if not username or not password:
                    raise ValueError('开启接收任务需要门户账户和密码；自动化请使用 --password-stdin。')
                enable = {'action': 'account.enable_receiver', 'plan_id': result['plan_id'], 'confirm': True,
                          'username': username, 'password': password}
                try:
                    result = execute(enable, ctx)
                finally:
                    enable['password'] = ''; password = None
            display(result, args.json)
        else:
            requests = {'doctor': {'action': 'config.doctor'}, 'logout': {'action': 'account.logout'},
                        'service': {'action': 'config.service', 'operation': getattr(args, 'operation', 'status')},
                        'workspace': {'action': 'config.workspace'}}
            if args.command == 'agents':
                if args.operation != 'list' and not args.harness:
                    raise ValueError('请指定要接入或检查的软件。')
                request = {'action': 'config.status' if args.operation == 'list' else 'config.' + args.operation, 'harness': args.harness}
            else:
                request = requests[args.command]
                if args.command == 'workspace' and args.path:
                    request['path'] = str(args.path.expanduser().absolute())
            result = execute(request, ctx)
            display(result, args.json)
        return 0 if successful(result) else 1
    except (KeyboardInterrupt, EOFError):
        print('已退出设置。', file=sys.stderr)
        return 130
    except Exception as error:
        # Backend error messages must be safe public codes; never echo requests.
        message = str(error) if isinstance(error, ValueError) else '操作未完成，请运行 anet-config doctor 检查。'
        if args.json or args.command == 'rpc':
            print(json.dumps({'ok': False, 'state': 'error', 'message': message}, ensure_ascii=False))
        else:
            print(message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

"""Optional receiving-agent setup. No task dispatch, trust expansion or password storage."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from .client_config import Client, envelope, ensure_directory
from .network import plain
from .workbuddy_mcp import Conflict, read_bytes, parse, private_raw

PORTAL_EXECUTORS = {'codex': 'codex', 'claude_code': 'claude', 'deepseek_harness': 'deepseek'}
# Exact previous client-shipped helper. Unknown/operator-owned code is preserved.
PREVIOUS_COMPANION_SHA256 = '5ae35873f795a9fe2dd2ee1a76d9d44c438922f60984784df3e0f41d0aa1a20a'
MESSAGES = {
    'receiver_account_required': '请先登录并连接当前设备。',
    'receiver_runtime_missing': '接收任务组件尚未安装，请修复 ANet 安装。',
    'receiver_python_required': '缺少独立 Python 运行环境，请修复 ANet 安装。',
    'receiver_agent_missing': '请先安装并在执行智能体中登录。',
    'receiver_sdk_missing': '此智能体的执行组件尚未安装，请修复 ANet 安装。',
    'receiver_workspace_invalid': '请选择 ANet 数据目录之外的具体工作文件夹。',
    'receiver_existing_preserved': '已有接收任务设置，已保留；请先停止并检查原有设置。',
    'receiver_policy_preserved': '已有独立的访问规则或自动执行器，已保留；请先检查原有设置。',
    'receiver_busy': '仍有任务或返回结果正在处理，请完成后再配置。',
    'receiver_state_unknown': '无法确认执行服务空闲，请稍后检查。',
    'receiver_plan_changed': '设备配置已变化，请重新检查接收任务设置。',
    'receiver_confirmation_required': '请确认将此设备设为接收任务的智能体，并运行一次简短模型测试。',
    'receiver_account_enable_required': '账户尚未开启此设备的接收任务功能。',
    'receiver_workbuddy_unavailable': 'WorkBuddy 工具连接可用；接收任务还缺少独立授权和可靠完成回执，暂不能开启。',
    'receiver_setup_failed': '接收任务配置尚未完成；原有备份已保留，请检查服务状态。',
    'receiver_native_input_required': '模型自检尚未通过，请在执行智能体中完成登录或所需确认后检查。',
    'receiver_companion_upgrade_required': '现有账户连接组件属于另一套安装，已保留；请先修复 ANet 安装。',
}


def digest(raw):
    return hashlib.sha256(raw or b'').hexdigest()


def safe_context(context):
    return {key: context[key] for key in ('schema', 'anet_root', 'node_root', 'anet_binary', 'anet_exe',
            'python_exe', 'harness_root', 'config_home', 'harness_config_path', 'portal', 'hub') if key in context}


class Receiver:
    def __init__(self, context):
        self.context = context
        self.client = Client(context)
        self.root = self.client.node
        self.task = self.root / 'taskd'
        self.config_path = self.task / 'config.json'
        self.plan_root = self.root / 'client/receiver-plans'
        self.receipt = self.root / 'client/receiver.json'

    def daemon_running(self):
        """Probe only an existing process-lifetime lock; status creates no file."""
        lock = plain(self.task/'daemon.lock')
        if not lock.is_file() or lock.stat().st_nlink != 1:
            return False
        try:
            with lock.open('r+b') as stream:
                if os.name == 'nt':
                    import msvcrt
                    try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                    except OSError:return True
                    stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:return True
                    fcntl.flock(stream,fcntl.LOCK_UN)
            return False
        except OSError:
            return False

    def status(self):
        cfg = parse(read_bytes(self.config_path))
        executor = cfg.get('executor') or {}
        saved = parse(read_bytes(self.receipt))
        heartbeat = parse(read_bytes(self.task / 'status.json'))
        fresh = bool(heartbeat and 0 <= time.time() - heartbeat.get('ts', 0) < 60)
        kind = executor.get('type')
        harness = executor.get('harness') if kind == 'harness_sdk' else 'workbuddy' if kind == 'workbuddy_local' else kind
        ready = bool(executor and fresh and heartbeat.get('executor_ready') and heartbeat.get('accept_tasks') and self.daemon_running())
        state = 'ready' if ready else saved.get('state') or ('needs_test' if executor else 'not_enabled')
        stopped = state == 'ready' and not ready
        if stopped:state='needs_attention'
        detail = {'ready': '此设备可以接收已获账户授权的任务。',
                  'not_enabled': '此设备当前只连接工具；可按需开启接收任务。',
                  'testing': '正在运行一次简短模型自检，请等待完成。',
                  'needs_test': '执行设置已保存，需要完成模型自检。',
                  'needs_attention': '接收任务尚未就绪，请检查执行智能体的登录和服务状态。',
                  'configured': '执行设置已保存，正在准备自检。'}.get(state, '请检查接收任务的设置。')
        if state == 'needs_attention':
            detail = MESSAGES.get(saved.get('code'), detail)
        if stopped:detail='接收任务服务尚未运行或状态已过期，请在服务页面检查。'
        return envelope(receiver={'state': state, 'ready': ready, 'configured': bool(executor),
            'requires_setup': not ready, 'harness': harness, 'workspace': cfg.get('work_dir'),
            'detail': detail, 'code': saved.get('code'), 'plan_id': saved.get('plan_id'),
            'model_test_started': saved.get('model_test_started', False),
            'supported': ['codex', 'claude_code', 'deepseek_harness'],
            'limitations': {'workbuddy': MESSAGES['receiver_workbuddy_unavailable']}})

    def idle(self):
        result = self.client.run_native(['status', '--json'])
        try:
            status = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise Conflict('receiver_state_unknown') from None
        if result.returncode or not isinstance(status, dict):
            raise Conflict('receiver_state_unknown')
        if status.get('maintenance', {}).get('active'):
            raise Conflict('receiver_busy')
        for state in ('submitted', 'working', 'input-required'):
            result = self.client.run_native(['task', 'list', '--state', state])
            try:
                tasks = json.loads(result.stdout).get('tasks')
            except (ValueError, AttributeError):
                raise Conflict('receiver_state_unknown') from None
            if result.returncode or not isinstance(tasks, list):
                raise Conflict('receiver_state_unknown')
            if tasks:
                raise Conflict('receiver_busy')
        beat = parse(read_bytes(self.task / 'status.json'))
        if beat and 0 <= time.time() - beat.get('ts', 0) < 60:
            counts = beat.get('work', {}).get('counts')
            if not isinstance(counts, dict):
                raise Conflict('receiver_state_unknown')
            if any(counts.get(key) for key in ('running', 'queued', 'waiting', 'blocked')) or beat.get('inline_receipts', {}).get('pending'):
                raise Conflict('receiver_busy')
        return status

    def paths(self):
        python = plain(self.context.get('python_exe') or sys.executable)
        if getattr(sys, 'frozen', False) and not self.context.get('python_exe'):
            raise Conflict('receiver_python_required')
        if not python.is_file() or not python.name.lower().startswith('python'):
            raise Conflict('receiver_python_required')
        app = self.task / 'app'
        if not (app / 'run_taskd.py').is_file() or not (app / 'taskd/cli.py').is_file():
            source = plain(Path(self.context.get('harness_root', Path(__file__).resolve().parents[1])) / 'taskd_app')
            if app.exists() or not (source/'run_taskd.py').is_file() or not (source/'taskd/cli.py').is_file():
                raise Conflict('receiver_runtime_missing')
            package_root = plain(Path(self.context.get('harness_root', Path(__file__).resolve().parents[1])))
            if any(not (package_root/name/'__init__.py').is_file() for name in ('anet_harnesses','anet_provider_harnesses')):
                raise Conflict('receiver_runtime_missing')
            app = source
        return python, app

    def executor(self, harness, settings):
        if harness == 'workbuddy':
            raise Conflict('receiver_workbuddy_unavailable')
        if harness not in PORTAL_EXECUTORS or not isinstance(settings, dict):
            raise Conflict('unknown_agent')
        allowed = {'executable', 'codex_home', 'dsh_home', 'provider', 'model'}
        if set(settings) - allowed:
            raise Conflict('receiver_settings_invalid')
        package = parse(read_bytes(Path(self.context['harness_config_path']))) if self.context.get('harness_config_path') else {}
        defaults = package.get('harnesses', {}).get(harness, {})
        if harness in ('codex', 'claude_code'):
            command = settings.get('executable') or defaults.get('executable') or shutil.which('codex' if harness == 'codex' else 'claude')
            if not command:
                raise Conflict('receiver_agent_missing')
            command = plain(command)
            if not command.is_file() or command.suffix.lower() in ('.cmd', '.bat', '.ps1'):
                raise Conflict('receiver_agent_missing')
            if harness == 'codex':
                home = plain(settings.get('codex_home') or os.environ.get('CODEX_HOME') or self.client.home / '.codex')
                return {'type': 'codex', 'command': str(command), 'codex_home': str(home), 'sandbox': 'workspace-write', 'native_goals': True}
            if importlib.util.find_spec('claude_agent_sdk') is None:
                raise Conflict('receiver_sdk_missing')
            options = {'executable': str(command)}
        else:
            if importlib.util.find_spec('deepseek_harness') is None or importlib.util.find_spec('deepseek_harness_runtime') is None:
                raise Conflict('receiver_sdk_missing')
            home = plain(settings.get('dsh_home') or defaults.get('dsh_home') or os.environ.get('DSH_HOME') or self.client.home / '.dsh')
            if not home.is_dir():
                raise Conflict('receiver_agent_missing')
            options = {'dsh_home': str(home), 'provider': settings.get('provider') or defaults.get('provider') or 'deepseek-official',
                       'model': settings.get('model') or defaults.get('model') or 'deepseek-v4-flash'}
            if any(not re.fullmatch(r'[A-Za-z0-9._:/-]{1,128}', options[key]) for key in ('provider', 'model')):
                raise Conflict('receiver_settings_invalid')
        return {'type': 'harness_sdk', 'harness': harness, 'state_root': str(self.task / 'receiver-sdk'),
                'harness_config': {'schema': 'anet-harness-config/1', 'harnesses': {harness: options}}}

    def companion_snapshot(self, connection):
        from anet_account import service
        desired = read_bytes(Path(service.__file__).with_name('companion')/'connector.py')
        installed = read_bytes(self.root/'portal/connector.py')
        marker = parse(read_bytes(self.root/'portal/client-companion.json'))
        if not desired or not installed:
            raise Conflict('receiver_runtime_missing')
        old, new = digest(installed), digest(desired)
        if old != new and not (connection.get('kind') == 'device' and old == PREVIOUS_COMPANION_SHA256
                               and marker.get('schema') == 'anet-client-companion/1' and marker.get('source') == 'bundled'):
            raise Conflict('receiver_companion_upgrade_required')
        return old, new

    def update_companion(self, plan):
        from anet_account import service
        target = self.root/'portal/connector.py'
        before = read_bytes(target)
        desired = read_bytes(Path(service.__file__).with_name('companion')/'connector.py')
        if digest(before) != plan['before_companion'] or digest(desired) != plan['target_companion']:
            raise Conflict('receiver_plan_changed')
        if before != desired:
            if plan.get('connection_kind') != 'device' or digest(before) != PREVIOUS_COMPANION_SHA256:
                raise Conflict('receiver_companion_upgrade_required')
            with self.client.locked():
                self.client.commit(target, before, desired)

    def prepare(self, request):
        self.paths()
        harness = request.get('harness')
        executor = self.executor(harness, request.get('settings') or {})
        workspace = plain(request.get('workspace') or self.client.workspace()['workspace'])
        if not workspace.is_dir() or workspace == self.client.home or workspace.parent == workspace or workspace == self.root or self.root in workspace.parents:
            raise Conflict('receiver_workspace_invalid')
        connection = parse(read_bytes(self.root / 'portal/connection.json'))
        if not connection.get('agent_id') or not connection.get('aid'):
            raise Conflict('receiver_account_required')
        native_raw = read_bytes(self.root / 'config.json')
        native = parse(native_raw)
        inbound = native.get('inbound') or {}
        caps = native.get('modules', {}).get('service', {}).get('capabilities', [])
        if native.get('auto_reply') or inbound.get('policy', 'closed') != 'closed' or inbound.get('allow') or inbound.get('trust') or any(not c.get('internal') for c in caps):
            raise Conflict('receiver_policy_preserved')
        if (inbound.get('admit') or {}).get('capability') not in (None, 'team.portal.admit'):
            raise Conflict('receiver_policy_preserved')
        before = read_bytes(self.config_path)
        cfg = parse(before)
        if cfg.get('executor'):
            old = cfg['executor']
            old_harness = old.get('harness') if old.get('type') == 'harness_sdk' else old.get('type')
            if old_harness == harness and cfg.get('work_dir') == str(workspace):
                return envelope(**self.status(), reused=True, model_requests=0)
            raise Conflict('receiver_existing_preserved')
        before_companion, target_companion = self.companion_snapshot(connection)
        self.idle()
        peers = self.client.run_native(['peers', 'list', '--json'])
        try:
            rules = json.loads(peers.stdout)
        except ValueError:
            raise Conflict('receiver_state_unknown') from None
        if peers.returncode or rules.get('policy') != 'closed' or rules.get('allow') or rules.get('trust') or rules.get('public_capabilities'):
            raise Conflict('receiver_policy_preserved')
        plan_id = str(uuid.uuid4())
        proposed = copy.deepcopy(cfg)
        proposed.update(version=1, installed_ts=cfg.get('installed_ts') or time.time(),
            transport={'type': 'anet', 'binary': str(self.client.require_binary()), 'data_dir': str(self.root)},
            executor=executor, reviewer=None, work_dir=str(workspace), plain_text='quick',
            quick_timeout_s=300, accept_peers='admitted', concurrency=1, isolate_tasks=True, scheduler_version=2)
        proposed.setdefault('guard', [])
        proposed.setdefault('probes', [])
        plan = {'schema': 'anet-receiver-plan/1', 'plan_id': plan_id, 'created_at': time.time(),
            'aid': connection['aid'], 'agent_id': connection['agent_id'], 'harness': harness,
            'portal_executor': PORTAL_EXECUTORS[harness], 'workspace': str(workspace),
            'before_taskd': digest(before), 'before_native': digest(native_raw), 'taskd_config': proposed,
            'context': safe_context(self.context), 'state': 'prepared'}
        plan.update(before_companion=before_companion, target_companion=target_companion,
                    connection_kind=connection.get('kind'))
        with self.client.locked():
            ensure_directory(self.plan_root)
            private_raw(self.plan_root / (plan_id + '.json'), json.dumps(plan).encode())
        return envelope(receiver={'state': 'prepared', 'ready': False, 'requires_setup': True,
            'detail': '接收任务设置已检查；开启后会运行一次简短模型自检。', 'harness': harness,
            'workspace': str(workspace), 'plan_id': plan_id}, plan_id=plan_id,
            portal_executor=plan['portal_executor'], agent_id=plan['agent_id'], aid=plan['aid'], model_requests=0)

    def plan(self, plan_id):
        if not isinstance(plan_id, str) or not re.fullmatch(r'[0-9a-f-]{36}', plan_id):
            raise Conflict('receiver_plan_changed')
        path = self.plan_root / (plan_id + '.json')
        plan = parse(read_bytes(path))
        if plan.get('schema') != 'anet-receiver-plan/1' or plan.get('plan_id') != plan_id:
            raise Conflict('receiver_plan_changed')
        return path, plan

    def validate(self, plan):
        self.paths()
        if digest(read_bytes(self.config_path)) != plan['before_taskd'] or digest(read_bytes(self.root/'config.json')) != plan['before_native']:
            raise Conflict('receiver_plan_changed')
        if not plain(plan['workspace']).is_dir():
            raise Conflict('receiver_workspace_invalid')
        if digest(read_bytes(self.root/'portal/connector.py')) != plan['before_companion']:
            raise Conflict('receiver_plan_changed')
        self.idle()

    def launch(self, request):
        if request.get('confirm') is not True:
            raise Conflict('receiver_confirmation_required')
        path, plan = self.plan(request.get('plan_id'))
        with self.client.locked():
            if plan.get('state') != 'prepared':
                return self.status()
            self.validate(plan)
            python, _ = self.paths()
            from anet_account.network import Network
            network = Network({**self.context, 'portal': parse(read_bytes(self.root/'portal/connection.json'))['portal']})
            saved, _ = network._state()
            info = network._authenticate(saved, plan['aid'])
            if not info or info.get('kind') != 'agent' or info.get('executor') != plan['portal_executor'] or info.get('agent_id') != plan['agent_id']:
                raise Conflict('receiver_account_enable_required')
            plan.update(state='testing', model_test_started=True)
            self.client.commit(path, read_bytes(path), json.dumps(plan).encode())
            self.client.commit(self.receipt, read_bytes(self.receipt), json.dumps({'state':'testing','plan_id':plan['plan_id'],'model_test_started':True}).encode())
            worker = Path(__file__).with_name('receiver_worker.py')
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            try:
                process = subprocess.Popen([str(python), str(worker), str(path)], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except OSError:
                plan.update(state='needs_attention', code='receiver_worker_unavailable', model_test_started=False)
                self.client.commit(path, read_bytes(path), json.dumps(plan).encode())
                self.client.commit(self.receipt, read_bytes(self.receipt), json.dumps({'state':'needs_attention',
                    'plan_id':plan['plan_id'],'code':'receiver_worker_unavailable','model_test_started':False}).encode())
                raise Conflict('receiver_worker_unavailable') from None
            # The worker owns subsequent state transitions; never overwrite its
            # receipt after spawn merely to add process metadata.
        return self.status()


def execute(request, context):
    try:
        receiver = Receiver(context)
        operation = request.get('operation', 'status')
        if operation == 'status':
            return receiver.status()
        if operation == 'prepare':
            return receiver.prepare(request)
        if operation == 'enable':
            return receiver.launch(request)
        raise Conflict('unknown_receiver_operation')
    except Conflict as error:
        return envelope(ok=False, code=str(error), state='requires_setup', requires_setup=True,
                        detail=MESSAGES.get(str(error), '接收任务设置尚未完成，请检查现有配置。'))
    except Exception as error:
        return envelope(ok=False, code=type(error).__name__, state='requires_setup', requires_setup=True,
                        detail='接收任务设置尚未完成，请检查账户和运行环境。')


def _run_worker(plan_path):
    """Run one model selftest, then activate exact prepared configuration.

    The durable testing marker is written before spawning. A second enable call
    observes that marker and cannot repeat the model request.
    """
    plan = parse(read_bytes(plain(plan_path)))
    receiver = Receiver(plan['context'])
    path, checked = receiver.plan(plan['plan_id'])
    if path != plain(plan_path) or checked != plan or plan.get('state') != 'testing':
        return 2
    python, app = receiver.paths()
    if app != receiver.task/'app':
        ensure_directory(receiver.task)
        # New public runtime only. An existing installation belongs to its own
        # update mechanism and is never replaced by receiver configuration.
        shutil.copytree(app, receiver.task/'app')
        app = receiver.task/'app'
        package_root = plain(Path(receiver.context.get('harness_root', Path(__file__).resolve().parents[1])))
        for package in ('anet_harnesses','anet_provider_harnesses'):
            if not (app/package).exists():
                shutil.copytree(package_root/package, app/package)
    sys.path.insert(0, str(app))
    from taskd.cli import Node, run_selftest
    from taskd.platform.locks import SingleInstance
    from taskd.platform import autostart
    from anet_account import service as companion
    locks = []
    original = read_bytes(receiver.config_path)
    configured = json.dumps(plan['taskd_config'], ensure_ascii=False, indent=2).encode()
    committed = False
    activated = False
    companion_stopped = False
    heartbeat = parse(read_bytes(receiver.task/'status.json'))
    was_running = bool(heartbeat and 0 <= time.time()-heartbeat.get('ts', 0) < 60)
    stop = receiver.task/'stop'
    argv = [str(python), str(app/'run_taskd.py'), '--data-dir', str(receiver.root),
            '--anet', str(receiver.client.require_binary()), 'supervise']
    def write_result(state, code=None):
        value = {'state': state, 'plan_id': plan['plan_id'], 'model_test_started': True,
                 'code': code, 'updated_at': time.time()}
        with receiver.client.locked():
            receiver.client.commit(receiver.receipt, read_bytes(receiver.receipt), json.dumps(value).encode())
            plan.update(state=state, code=code)
            receiver.client.commit(path, read_bytes(path), json.dumps(plan).encode())
    try:
        receiver.idle()
        if digest(original) != plan['before_taskd'] or digest(read_bytes(receiver.root/'config.json')) != plan['before_native']:
            raise Conflict('receiver_plan_changed')
        # Cooperative shutdown does not kill workers; locks remain owned until
        # the existing daemon and supervisor actually release them.
        if not stop.exists():
            receiver.client.commit(stop, None, b'')
        for name in ('supervise.lock', 'daemon.lock'):
            lock = SingleInstance(receiver.task/name)
            deadline = time.monotonic()+45
            while not lock.acquire():
                if time.monotonic() >= deadline:
                    raise Conflict('receiver_busy')
                time.sleep(.25)
            locks.append(lock)
        with receiver.client.locked():
            if digest(read_bytes(receiver.config_path)) != plan['before_taskd']:
                raise Conflict('receiver_plan_changed')
            receiver.client.commit(receiver.config_path, original, configured)
            committed = True
        node = Node(receiver.root, str(receiver.client.require_binary()))
        good, _ = run_selftest(node, plan['taskd_config'], app)
        if not good:
            raise Conflict('receiver_native_input_required')
        receiver.idle()
        companion.control(receiver.context, 'stop')
        companion_stopped = True
        receiver.update_companion(plan)
        connector = receiver.root/'portal/connector.py'
        result = subprocess.run([str(python), str(connector), 'configure', '--data-dir', str(receiver.root),
            '--anet', str(receiver.client.require_binary()), '--transport', 'https', '--no-report'],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=100,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise Conflict('receiver_setup_failed')
        for lock in reversed(locks):
            lock.release()
        locks.clear()
        if stop.exists():
            stop.unlink()
        autostart.install(receiver.task, argv, receiver.task/'taskd.out.log', start=True)
        activated = True
        companion.control(receiver.context, 'start')
        companion_stopped = False
        deadline = time.monotonic()+45
        while time.monotonic() < deadline:
            current = receiver.status()['receiver']
            if current['ready']:
                write_result('ready')
                return 0
            time.sleep(.5)
        raise Conflict('receiver_service_unverified')
    except Exception as error:
        from anet_account.network import NetworkError
        from .network import NetworkError as HarnessNetworkError
        code = str(error) if isinstance(error, Conflict) else error.code if isinstance(error, (NetworkError, HarnessNetworkError)) else type(error).__name__
        # Restore only our exact write. A concurrent operator edit is preserved.
        if committed and not activated and read_bytes(receiver.config_path) == configured:
            with receiver.client.locked():
                if original is not None:
                    receiver.client.commit(receiver.config_path, configured, original)
                else:
                    receiver.config_path.unlink()
        write_result('needs_attention', code)
        return 2
    finally:
        for lock in reversed(locks):
            lock.release()
        if plan.get('state') != 'ready' and was_running and not activated:
            if stop.exists():
                stop.unlink()
            autostart.install(receiver.task, argv, receiver.task/'taskd.out.log', start=True)
        if companion_stopped:
            try:
                companion.control(receiver.context, 'start')
            except Exception:
                pass


def run_worker(plan_path):
    try:
        return _run_worker(plan_path)
    except Exception as error:
        # Include initialization failures in the durable result too. Do not
        # leave an unexplained running indicator or replay a reserved test.
        try:
            plan = parse(read_bytes(plain(plan_path)))
            receiver = Receiver(plan['context'])
            with receiver.client.locked():
                value = {'state':'needs_attention','plan_id':plan['plan_id'],
                         'model_test_started':True,'code':type(error).__name__}
                receiver.client.commit(receiver.receipt, read_bytes(receiver.receipt), json.dumps(value).encode())
        except Exception:
            pass
        return 2

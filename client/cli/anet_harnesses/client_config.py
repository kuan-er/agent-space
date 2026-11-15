"""Configuration-only desktop/CLI backend. Never starts a harness conversation.

Only ANet's entry is merged into native configuration; unrelated settings and
approval choices are preserved. Credentials and native chat history are not read.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
try:
    import tomllib
except ImportError:  # Python 3.10 portable install
    import tomli as tomllib
import uuid
from contextlib import contextmanager

from .network import plain, private_folder
from .workbuddy_mcp import (Conflict, read_bytes, parse, private_raw,
                           native_replace, native_probe, native_client, norm)

NAMES = {'codex': 'Codex', 'claude_code': 'Claude Code',
         'workbuddy': 'WorkBuddy', 'deepseek_harness': 'DeepSeek Harness'}
SCHEMA = 'anet-client-config/1'
BEGIN = '# >>> anet (managed by `anet agents wire`; edits inside this block are overwritten) >>>'
END = '# <<< anet <<<'
YBEGIN = '# >>> ANet client MCP >>>'
YEND = '# <<< ANet client MCP <<<'


def envelope(**value):
    return {'schema': SCHEMA, 'tasks_started': 0, **value}


def ensure_directory(path):
    path = plain(path)
    if not path.exists():
        ensure_directory(path.parent)
        private_folder(path)
    return path


class Client:
    def __init__(self, context):
        self.context = context
        self.node = plain(context.get('anet_root') or Path(context['node_root']).parent)
        self.home = plain(context.get('config_home') or Path.home())
        candidate = context.get('anet_binary') or context.get('anet_exe')
        if not candidate:
            for path in (self.node / 'bin' / ('anet.exe' if os.name == 'nt' else 'anet'),
                         self.home / '.anet-bin' / ('anet.exe' if os.name == 'nt' else 'anet')):
                if path.is_file():
                    candidate = str(path)
                    break
        self.binary = plain(candidate) if candidate else None
        self.private = self.node / 'client'
        self.settings_path = self.private / 'settings.json'

    def settings(self):
        return parse(read_bytes(self.settings_path))

    def require_binary(self):
        if not self.binary or not self.binary.is_file():
            raise Conflict('anet_not_installed')
        return self.binary

    @contextmanager
    def locked(self):
        ensure_directory(self.private)
        lockpath = self.private / 'config.lock'
        if not lockpath.exists():
            try:
                private_raw(lockpath, b'0')
            except FileExistsError:
                pass
        with lockpath.open('r+b') as stream:
            if os.name == 'nt':
                import msvcrt
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise Conflict('configuration_busy') from None
            else:
                import fcntl
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise Conflict('configuration_busy') from None
            yield

    def commit(self, path, before, after):
        if before == after:
            return None
        path = plain(path)
        ensure_directory(path.parent)
        ensure_directory(self.private)
        backup = None
        if before is not None:
            backup = self.private / ('backup-' + uuid.uuid4().hex + '.bin')
            private_raw(backup, before)
        if read_bytes(path) != before:
            raise Conflict('configuration_changed_please_retry')
        temporary = path.parent / ('.anet-' + uuid.uuid4().hex)
        # A sibling temporary permits atomic rename across filesystem volumes.
        # new_private_fd creates the file owner-private without tightening the
        # user's existing configuration directory.
        from .network import new_private_fd
        fd = new_private_fd(temporary)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(after)
                stream.flush()
                os.fsync(stream.fileno())
            native_replace(path, temporary)
        finally:
            temporary.unlink(missing_ok=True)
        if read_bytes(path) != after:
            raise Conflict('configuration_write_unconfirmed')
        return str(backup) if backup else None

    def path_for(self, harness):
        if harness not in NAMES:
            raise Conflict('unknown_agent')
        # Explicit overrides allow custom profiles without discovering secrets.
        overrides = self.context.get('agent_config_paths', {})
        if harness in overrides:
            return plain(overrides[harness])
        isolated = 'config_home' in self.context
        if harness == 'codex':
            return plain(Path(os.environ.get('CODEX_HOME', str(self.home / '.codex')))
                         / 'config.toml') if not isolated else self.home / '.codex/config.toml'
        if harness == 'claude_code':
            directory = os.environ.get('CLAUDE_CONFIG_DIR') if not isolated else None
            return plain(Path(directory) / '.claude.json') if directory else self.home / '.claude.json'
        if harness == 'workbuddy':
            return self.home / '.workbuddy/mcp.json'
        dsh_home = self.context.get('dsh_home') or (None if isolated else os.environ.get('DSH_HOME'))
        profile = self.context.get('dsh_profile', 'all')
        if profile not in ('all', 'desktop', 'headless', 'web', 'sdk', 'sdk-minimal', 'acp'):
            raise Conflict('unknown_deepseek_profile')
        root = plain(Path(dsh_home) if dsh_home else self.home / '.dsh')
        return root / 'cordis.patch.yml' if profile == 'all' else root / 'profiles' / profile / 'cordis.patch.yml'

    def check_deepseek_layers(self, path):
        # A home patch applies to desktop, headless and SDK together. Do not add
        # a duplicate MCP name already owned by a per-profile user patch.
        if 'deepseek_harness' in self.context.get('agent_config_paths', {}):
            return
        profile = self.context.get('dsh_profile', 'all')
        if profile == 'all':
            directory = path.parent / 'profiles'
            if not directory.exists():
                return
            candidates = [plain(p) / 'cordis.patch.yml' for p in directory.iterdir() if p.is_dir()]
        else:
            candidates = [path.parents[2] / 'cordis.patch.yml']
        for other in candidates:
            if self.deepseek_row(read_bytes(other)) is not None:
                raise Conflict('deepseek_anet_already_in_another_layer')

    def desired(self, harness):
        result = {'command': str(self.require_binary()), 'args': ['mcp'],
                  'env': {'ANET_DATA_DIR': str(self.node)}}
        if harness == 'claude_code':
            result['type'] = 'stdio'
        if harness == 'deepseek_harness':
            result.update(serverName='anet', transport='stdio', toolCallTimeoutMs=900000)
        return result

    def owned(self, entry):
        if not isinstance(entry, dict) or entry.get('args') != ['mcp']:
            raise Conflict('existing_anet_entry_belongs_to_another_server')
        env = entry.get('env') or {}
        if not isinstance(env, dict) or not isinstance(env.get('ANET_DATA_DIR'), str):
            raise Conflict('existing_anet_entry_has_no_device_binding')
        if norm(env['ANET_DATA_DIR']) != norm(self.node) or env.get('ANET_ID') not in (None, '', 'default'):
            raise Conflict('existing_anet_entry_uses_another_device')
        command = entry.get('command')
        if not isinstance(command, str) or not Path(command).is_absolute() or Path(command).name.lower() not in ('anet', 'anet.exe'):
            raise Conflict('existing_anet_command_requires_review')
        if entry.get('type', 'stdio') != 'stdio' or entry.get('transport', 'stdio') != 'stdio':
            raise Conflict('existing_anet_transport_requires_review')

    def yaml_nodes(self, raw):
        try:
            import yaml
        except ImportError:
            raise Conflict('deepseek_yaml_support_not_installed') from None
        try:
            tree = yaml.compose(raw.decode('utf-8-sig')) if raw else None
        except (yaml.YAMLError, UnicodeError):
            raise Conflict('deepseek_configuration_needs_review') from None
        if tree is not None and not isinstance(tree, yaml.SequenceNode):
            raise Conflict('deepseek_configuration_must_be_list')
        return yaml, tree

    def deepseek_row(self, raw):
        yaml, tree = self.yaml_nodes(raw)
        found = []
        def primitive(node):
            if isinstance(node, yaml.ScalarNode):
                # Compose never evaluates Cordis !!js tags or constructors.
                if node.tag.endswith(':str'):
                    return node.value
                if node.tag.endswith(':bool'):
                    return node.value.lower() == 'true'
                if node.tag.endswith(':int'):
                    return int(node.value)
                return None
            if isinstance(node, yaml.SequenceNode):
                return [primitive(x) for x in node.value]
            if isinstance(node, yaml.MappingNode):
                pairs = [(primitive(k), primitive(v)) for k, v in node.value]
                if len({k for k, _ in pairs}) != len(pairs):
                    raise Conflict('duplicate_deepseek_configuration_key')
                return dict(pairs)
            return None
        def inspect(value):
            if not isinstance(value, dict):
                raise Conflict('deepseek_configuration_needs_review')
            cfg = value.get('config')
            if value.get('id') == 'anet-mcp' or isinstance(cfg, dict) and cfg.get('serverName') == 'anet':
                found.append(value)
            if isinstance(cfg, dict) and isinstance(cfg.get('servers'), dict) and 'anet' in cfg['servers']:
                raise Conflict('deepseek_anet_already_managed_in_settings')
            # Cordis patch files insert new entries; bare id rows only override
            # already-present entries. Inspect both forms and nested groups.
            for key in ('insert', 'config'):
                children = value.get(key)
                if isinstance(children, list):
                    for child in children:
                        inspect(child)
        for item in tree.value if tree else []:
            inspect(primitive(item))
        if len(found) > 1:
            raise Conflict('duplicate_deepseek_anet_entry')
        if not found:
            return None
        row = found[0]
        if row.get('id') != 'anet-mcp' or row.get('name') != '@deepseek-ai/dsh-mcp-client':
            raise Conflict('existing_deepseek_anet_entry_requires_review')
        return row

    def deepseek_entry(self, raw):
        row = self.deepseek_row(raw)
        return row.get('config') if row else None

    def read_entry(self, harness, raw):
        if harness == 'codex':
            try:
                value = tomllib.loads((raw or b'').decode('utf-8-sig'))
                return value.get('mcp_servers', {}).get('anet')
            except (ValueError, UnicodeError, AttributeError):
                raise Conflict('codex_configuration_needs_review') from None
        if harness == 'deepseek_harness':
            return self.deepseek_entry(raw)
        return parse(raw).get('mcpServers', {}).get('anet')

    def merge(self, harness, raw):
        old = self.read_entry(harness, raw)
        if old is not None:
            self.owned(old)
        wanted = self.desired(harness)
        entry = copy.deepcopy(old or {})
        entry.update({k: v for k, v in wanted.items() if k != 'env'})
        entry.setdefault('env', {}).update(wanted['env'])
        if harness == 'codex':
            text = (raw or b'').decode('utf-8-sig')
            match = re.search(r'(?m)^# >>> anet.*\r?\n[\s\S]*?^# <<< anet <<<[^\S\r\n]*\r?$', text)
            if old is not None and not match:
                raise Conflict('codex_anet_entry_requires_manual_merge')
            if text.count('# >>> anet') > 1 or text.count(END) > 1:
                raise Conflict('codex_duplicate_managed_block')
            # Preserve all entry flags/permissions; TOML has no stdlib writer.
            # Only known primitive fields are accepted in the managed block.
            def literal(value):
                if isinstance(value, dict):
                    return '{ ' + ', '.join(json.dumps(k) + ' = ' + literal(v) for k, v in value.items()) + ' }'
                if isinstance(value, (str, int, float, bool, list)):
                    return json.dumps(value, ensure_ascii=False)
                raise Conflict('codex_managed_value_requires_review')
            entry.setdefault('startup_timeout_sec', 30)
            entry.setdefault('tool_timeout_sec', 900)
            block = BEGIN + '\n[mcp_servers.anet]\n' + ''.join(k + ' = ' + literal(v) + '\n' for k, v in entry.items()) + END
            updated = text[:match.start()] + block + text[match.end():] if match else text.rstrip() + ('\n\n' if text.strip() else '') + block + '\n'
            parsed = tomllib.loads(updated)
            previous = tomllib.loads(text)
            previous.setdefault('mcp_servers', {})['anet'] = entry
            if parsed != previous:
                raise Conflict('codex_unrelated_configuration_changed')
            return updated.encode('utf-8')
        if harness == 'deepseek_harness':
            text = (raw or b'').decode('utf-8-sig')
            match = re.search(r'(?m)^' + re.escape(YBEGIN) + r'\r?\n[\s\S]*?^' + re.escape(YEND) + r'[^\S\r\n]*\r?$', text)
            if old is not None and not match:
                raise Conflict('deepseek_anet_entry_requires_manual_merge')
            if text.count(YBEGIN) > 1 or text.count(YEND) > 1:
                raise Conflict('deepseek_duplicate_managed_block')
            # JSON flow values are legal YAML and safely escape paths/quotes.
            item = copy.deepcopy(self.deepseek_row(raw) or {})
            item.update(id='anet-mcp', name='@deepseek-ai/dsh-mcp-client', config=entry)
            block = YBEGIN + '\n- ' + json.dumps({'insert': [item]}, ensure_ascii=False) + '\n' + YEND
            base = '' if text.strip() in ('[]', '---\n[]') else text
            updated = text[:match.start()] + block + text[match.end():] if match else base.rstrip() + ('\n' if base.strip() else '') + block + '\n'
            self.yaml_nodes(updated.encode())
            return updated.encode('utf-8')
        root = parse(raw)
        root.setdefault('mcpServers', {})['anet'] = entry
        return (json.dumps(root, ensure_ascii=False, indent=2) + '\n').encode('utf-8')

    def agent(self, harness):
        path = self.path_for(harness)
        configured = False
        state = 'not_configured'
        detail = '点击连接，将 ANet 工具加入此智能体。'
        disabled = False
        try:
            raw = read_bytes(path)
            if harness == 'deepseek_harness':
                self.check_deepseek_layers(path)
            entry = self.read_entry(harness, raw)
            if entry is not None:
                self.owned(entry)
                configured = bool(self.binary and norm(entry['command']) == norm(self.binary))
                disabled = entry.get('disabled') is True or entry.get('enabled') is False
                if harness == 'deepseek_harness':
                    disabled = disabled or self.deepseek_row(raw).get('disabled') is True
                state = 'disabled' if disabled else 'configured' if configured else 'update_available'
                detail = '配置已保存；在智能体中重新加载 ANet 工具即可使用。' if configured else '可更新 ANet 连接。'
                if disabled:
                    detail = '智能体中的 ANet 工具已停用，请在其设置中启用。'
        except Conflict as error:
            state = 'conflict'
            detail = FRIENDLY.get(str(error), '已有 ANet 配置需要确认，请检查它是否属于当前设备。')
        command = {'codex': 'codex', 'claude_code': 'claude', 'workbuddy': 'WorkBuddy', 'deepseek_harness': 'dsh'}[harness]
        installed = bool(shutil.which(command))
        if harness == 'workbuddy' and 'config_home' not in self.context:
            installed = native_client()['presence'] == 'detected'
        # Profile presence is evidence of setup, not login or executable health.
        profile_present = path.parent.is_dir() if harness != 'claude_code' else (self.home / '.claude').is_dir()
        return {'id': harness, 'name': NAMES[harness], 'installed': installed,
                'profile_present': profile_present, 'authenticated': 'not_checked',
                'configured': configured, 'disabled': disabled, 'state': state,
                'detail': detail, 'config_path': str(path), 'native_use': 'not_checked'}

    def agents(self):
        return envelope(agents=[self.agent(harness) for harness in NAMES])

    def configure_agent(self, harness):
        path = self.path_for(harness)
        with self.locked():
            if harness == 'deepseek_harness':
                self.check_deepseek_layers(path)
            before = read_bytes(path)
            after = self.merge(harness, before)
            backup = self.commit(path, before, after)
        return envelope(**self.agent(harness), backup_path=backup, reload_required=True)

    def verify(self, harness):
        result = self.agent(harness)
        if not result['configured']:
            return envelope(**result)
        proof = native_probe(self.require_binary(), self.node)
        # Transport handshake is not proof the selected harness has reloaded it.
        return envelope(**result, mcp_handshake=proof['mcp_handshake'],
                        tool_count=proof['tool_count'], detail_check='ANet 工具服务连接正常。')

    def workspace(self, path=None):
        if path is None:
            value = self.settings().get('workspace')
            if not value:
                config = parse(read_bytes(self.node / 'taskd/config.json'))
                value = config.get('work_dir') or str(self.home / 'ANet')
            return envelope(workspace=value, detail='新任务使用此工作文件夹。')
        folder = plain(Path(path).expanduser())
        if not folder.is_dir():
            raise Conflict('workspace_must_be_existing_folder')
        if not os.access(folder, os.W_OK):
            raise Conflict('workspace_not_writable')
        with self.locked():
            before = read_bytes(self.settings_path)
            value = parse(before)
            value['workspace'] = str(folder)
            taskd_path = self.node / 'taskd/config.json'
            taskd_before = read_bytes(taskd_path)
            if taskd_before is not None:
                taskd = parse(taskd_before)
                taskd['work_dir'] = str(folder)
                self.commit(taskd_path, taskd_before, (json.dumps(taskd, ensure_ascii=False, indent=2) + '\n').encode())
            self.commit(self.settings_path, before, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())
        return self.workspace()

    def run_native(self, args, timeout=20):
        env = dict(os.environ, ANET_DATA_DIR=str(self.node))
        env.pop('ANET_ID', None)
        return subprocess.run([str(self.require_binary()), *args], cwd=str(self.node), env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def service(self, operation='status', service_id='node'):
        if service_id not in ('node', 'anet') or operation not in ('status', 'start', 'stop', 'restart'):
            raise Conflict('unknown_service_action')
        try:
            from anet_account.service import control as connector_control
        except ImportError:
            connector_control = None
        if operation in ('stop', 'restart'):
            # A user-triggered service control must not terminate active work.
            status = self.run_native(['status', '--json'])
            try:
                raw = json.loads(status.stdout)
            except ValueError:
                raise Conflict('service_state_unknown') from None
            if raw.get('maintenance', {}).get('active', 0):
                raise Conflict('service_busy_finish_tasks_first')
            # The updater's active count is not a task-execution count. Check
            # native A2A states too, without exposing task content in the UI.
            for task_state in ('submitted', 'working', 'input-required'):
                pending = self.run_native(['task', 'list', '--state', task_state])
                try:
                    tasks = json.loads(pending.stdout).get('tasks')
                except (ValueError, AttributeError):
                    raise Conflict('service_state_unknown') from None
                if pending.returncode or not isinstance(tasks, list):
                    raise Conflict('service_state_unknown')
                if tasks:
                    raise Conflict('service_busy_finish_tasks_first')
            taskd_path = self.node / 'taskd/status.json'
            if (self.node / 'taskd/config.json').exists():
                state = parse(read_bytes(taskd_path))
                if not state or time.time() - state.get('ts', 0) > 60:
                    raise Conflict('service_state_unknown')
                work = state.get('work', {})
                if not isinstance(work.get('counts'), dict):
                    raise Conflict('service_state_unknown')
                if any(work['counts'].get(k, 0) for k in ('running', 'queued', 'waiting', 'blocked')):
                    raise Conflict('service_busy_finish_tasks_first')
                if state.get('inline_receipts', {}).get('busy') or state.get('inline_receipts', {}).get('pending'):
                    raise Conflict('service_busy_finish_tasks_first')
            if connector_control:
                companion = connector_control(self.context, 'stop')
                if companion.get('running'):
                    raise Conflict('connector_stop_unconfirmed')
            result = self.run_native(['stop'])
            if result.returncode:
                raise Conflict('service_stop_failed')
        if operation in ('start', 'restart'):
            result = self.run_native(['up'])
            if result.returncode:
                raise Conflict('service_start_failed')
            if connector_control:
                connector_control(self.context, 'start')
        result = self.run_native(['status', '--json'])
        try:
            raw = json.loads(result.stdout)
        except ValueError:
            raw = {}
        running = result.returncode == 0 and bool(raw.get('aid')) and raw.get('running') is not False
        connected = running and (raw.get('hub', {}).get('state') in ('registered', 'connected', 'online'))
        services = [{'id': 'node', 'name': 'ANet 连接服务',
                                  'state': 'running' if running else 'stopped',
                                  'running': running, 'connected': connected,
                                  'detail': '服务正常' if connected else '请启动服务或检查网络'}]
        if (self.node / 'taskd/config.json').exists():
            taskd = parse(read_bytes(self.node / 'taskd/status.json'))
            healthy = bool(taskd and 0 <= time.time() - taskd.get('ts', 0) < 60)
            services.append({'id': 'taskd', 'name': '智能体执行服务', 'running': healthy,
                             'state': 'running' if healthy else 'stopped',
                             'detail': '服务正常' if healthy and taskd.get('executor_ready') else '请检查执行智能体的安装和登录',
                             'executor_ready': bool(healthy and taskd.get('executor_ready')),
                             'control_supported': False})
        if connector_control:
            companion = connector_control(self.context, 'status')
            services.append({'id': 'portal', 'name': '门户连接服务', 'running': bool(companion.get('running')),
                             'state': companion.get('state', 'unavailable'),
                             'detail': companion.get('detail', '请检查账户连接'),
                             'control_supported': False})
        return envelope(services=services,
                        running=running, connected=connected,
                        version=raw.get('version'), operation=operation)


def execute(request, context):
    try:
        if not isinstance(request, dict):
            raise Conflict('request_must_be_object')
        client = Client(context)
        action = request.get('action', 'status')
        if action == 'agents':
            return client.agents()
        if action in ('connect', 'configure_agent'):
            return client.configure_agent(request.get('harness'))
        if action == 'verify':
            return client.verify(request.get('harness'))
        if action == 'workspace':
            return client.workspace(request.get('path'))
        if action == 'service':
            return client.service(request.get('operation', 'status'), request.get('service_id', 'node'))
        if action in ('status', 'doctor'):
            result = client.agents()
            result.update(client.workspace())
            try:
                result.update(client.service())
            except (Conflict, OSError, subprocess.TimeoutExpired):
                result.update(services=[{'id': 'node', 'name': 'ANet 连接服务', 'state': 'unavailable', 'running': False, 'detail': '尚未发现可用的 ANet 服务'}])
            return result
        raise Conflict('unknown_configuration_action')
    except Conflict as error:
        return envelope(ok=False, state='needs_attention', code=str(error), detail=FRIENDLY.get(str(error), '配置未更改，请检查现有设置。'))
    except Exception as error:
        return envelope(ok=False, state='needs_attention', code=type(error).__name__, detail='操作未完成，请稍后重试检查。')


FRIENDLY = {
    'anet_not_installed': '请先安装或修复 ANet 客户端。',
    'workspace_must_be_existing_folder': '请选择一个已存在的文件夹。',
    'workspace_not_writable': '当前账户无法写入此文件夹，请选择其他位置。',
    'service_busy_finish_tasks_first': '有任务正在运行，完成后再停止服务。',
    'service_state_unknown': '暂时无法确认服务是否空闲，请稍后重新检查。',
    'deepseek_yaml_support_not_installed': 'DeepSeek 配置组件缺失，请修复 ANet 安装。',
    'configuration_busy': '另一个配置操作正在进行，请稍后重试。',
    'existing_anet_entry_uses_another_device': '已有 ANet 配置指向另一设备，请确认后再连接。',
    'existing_anet_entry_has_no_device_binding': '已有 ANet 配置未指定设备，请先检查原有设置。',
    'existing_anet_entry_belongs_to_another_server': '已有同名工具连接，为避免覆盖，请先检查原有设置。',
    'existing_anet_command_requires_review': '已有 ANet 配置使用自定义程序，请先检查原有设置。',
    'deepseek_anet_already_managed_in_settings': 'DeepSeek 已经配置了 ANet，请先检查它的工具设置。',
    'deepseek_anet_entry_requires_manual_merge': 'DeepSeek 中已有 ANet 设置，请先确认后再更新。',
    'codex_anet_entry_requires_manual_merge': 'Codex 中已有 ANet 设置，请先确认后再更新。',
    'deepseek_anet_already_in_another_layer': 'DeepSeek 的另一配置层已连接 ANet，请先检查原有设置，避免重复连接。',
}


def main():
    parser = argparse.ArgumentParser(description='ANet client configuration backend')
    parser.add_argument('--runtime', required=True)
    args = parser.parse_args()
    context = json.loads(Path(args.runtime).read_text(encoding='utf-8-sig'))
    line = sys.stdin.buffer.readline(65537)
    if len(line) > 65536:
        raise ValueError('bounded request required')
    value = execute(json.loads(line), context)
    print(json.dumps(value, ensure_ascii=False))
    return 1 if value.get('ok') is False else 0


if __name__ == '__main__':
    raise SystemExit(main())

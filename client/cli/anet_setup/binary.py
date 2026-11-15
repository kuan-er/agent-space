"""Choose this node's executable without silently reverting a managed update.

The maintenance state is owner-controlled local configuration, not a signature
authority. This verifies its fixed managed path, bytes and reported version; the
native maintenance service remains responsible for release signature validation.
No persistent cache, service operation, network request or model call is used.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time


class BinaryResolutionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('ANet 当前程序校验失败（' + code + '）。请检查维护状态，或用 --anet-binary 明确指定已确认的程序；未改用旧版本。')


def plain(path):
    """Inspect uncollapsed names so resolving a link cannot hide it."""
    path = Path(path).expanduser().absolute()
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise BinaryResolutionError('linked_path')
    return path


def bounded(path, limit):
    path = plain(path)
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise BinaryResolutionError('not_regular_file')
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise BinaryResolutionError('metadata_too_large')
        return data
    except OSError:
        raise BinaryResolutionError('metadata_unavailable') from None


def identity(info):
    # On this Windows Python, stat().st_ctime is creation time while
    # fstat().st_ctime is change time. Comparing them rejects unchanged files.
    extra = getattr(info, 'st_birthtime_ns', None) if os.name == 'nt' else info.st_ctime_ns
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, extra


def verify_managed(root, current, raw_state, state_path):
    sha = current.get('sha256')
    version = current.get('version')
    name = 'anet.exe' if os.name == 'nt' else 'anet'
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
        raise BinaryResolutionError('invalid_sha256')
    if not isinstance(version, str) or not re.fullmatch(r'[0-9][A-Za-z0-9.+_-]{0,99}', version):
        raise BinaryResolutionError('invalid_version')
    expected = root / 'maintenance' / 'versions' / sha / name
    candidate = Path(current.get('path', ''))
    # Do not collapse '..', accept a sibling directory, or follow a link.
    if not candidate.is_absolute() or '..' in candidate.parts or candidate != expected:
        raise BinaryResolutionError('managed_path_mismatch')
    candidate = plain(candidate)
    try:
        before = candidate.stat()
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 128 * 1024 * 1024:
            raise BinaryResolutionError('invalid_binary_file')
        digest = hashlib.sha256()
        with candidate.open('rb') as stream:
            if identity(os.fstat(stream.fileno())) != identity(before):
                raise BinaryResolutionError('binary_changed')
            while block := stream.read(1024 * 1024):
                digest.update(block)
            if identity(os.fstat(stream.fileno())) != identity(before):
                raise BinaryResolutionError('binary_changed')
        if digest.hexdigest() != sha:
            raise BinaryResolutionError('binary_hash_mismatch')
        # Only execute after the owner-recorded digest and boundary match.
        result = subprocess.run([str(candidate), '--version'],
                                env=dict(os.environ, ANET_DATA_DIR=str(root)),
                                capture_output=True, timeout=8,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        output = result.stdout.decode('utf-8', 'replace')
        if result.returncode or len(result.stdout) > 8192 or not re.match(r'^anet ' + re.escape(version) + r'(?:\s|$)', output):
            raise BinaryResolutionError('binary_version_mismatch')
        if identity(plain(candidate).stat()) != identity(before):
            raise BinaryResolutionError('binary_changed')
        if bounded(state_path, 65536) != raw_state:
            raise BinaryResolutionError('maintenance_state_changed')
    except (OSError, subprocess.TimeoutExpired):
        raise BinaryResolutionError('managed_binary_unavailable') from None
    return candidate


def legacy_candidates(root, home, name):
    config = root / 'taskd' / 'config.json'
    if config.is_file():
        transport = json.loads(bounded(config, 1024 * 1024)).get('transport', {})
        if transport.get('data_dir') and Path(transport['data_dir']).resolve() != root.resolve():
            raise ValueError('已有服务使用另一数据目录，请指定原来的目录。')
        p = Path(transport.get('binary', ''))
        if p.is_file():
            yield p
    yield root / 'bin' / name
    yield home / '.local' / 'bin' / name
    yield home / '.anet-bin' / name
    found = shutil.which(name)
    if found:
        yield Path(found)


def resolve(root, explicit=None, *, home=None):
    started = time.perf_counter()
    # Inspect original components first: collapsing a linked/../ segment before
    # checking it would hide the traversal. Then accept normal ../node inputs.
    root = plain(root).resolve()
    home = Path(home or Path.home()).expanduser().absolute()
    name = 'anet.exe' if os.name == 'nt' else 'anet'
    selected = None
    source = 'legacy'
    detail = ''
    if explicit:
        # An explicit owner choice retains the old CLI contract, including a
        # deliberate symlink. Managed pointers never get this exception.
        selected = Path(explicit).expanduser().resolve()
        if not selected.is_file():
            raise ValueError('指定的 ANet 程序不存在。')
        source = 'explicit'
    else:
        state_path = root / 'maintenance' / 'state.json'
        # lexists catches broken symlinks rather than treating them as absent.
        if os.path.lexists(state_path):
            raw_state = bounded(state_path, 65536)
            try:
                state = json.loads(raw_state)
            except (ValueError, UnicodeError):
                raise BinaryResolutionError('invalid_maintenance_state') from None
            if not isinstance(state, dict) or state.get('schema') != 1 or not isinstance(state.get('current'), dict):
                raise BinaryResolutionError('invalid_maintenance_state')
            current = state['current']
            value = current.get('path')
            if not isinstance(value, str) or not value or not Path(value).is_absolute():
                raise BinaryResolutionError('invalid_current_path')
            candidate = Path(value)
            versions = root / 'maintenance' / 'versions'
            managed = 'sha256' in current or candidate.is_relative_to(versions)
            if managed:
                selected = verify_managed(root, current, raw_state, state_path)
                source = 'maintenance_verified'
            else:
                # Initial native maintenance can record a pre-download bootstrap
                # with no digest. Never execute an arbitrary pointer as a result.
                bootstraps = {root / 'bin' / name, home / '.anet-maintenance-bin' / name,
                              home / '.local' / 'bin' / name, home / '.anet-bin' / name}
                if candidate not in bootstraps or '..' in candidate.parts:
                    raise BinaryResolutionError('unrecognized_legacy_current')
                plain(candidate)
                source = 'legacy_fallback_unverified'
                detail = '维护系统尚未登记可校验的版本；沿用已有安装路径，未确认它是最新版本。'
        if selected is None:
            for candidate in legacy_candidates(root, home, name):
                if candidate.is_file():
                    selected = candidate.resolve()
                    break
    return {'binary': str(selected) if selected else '', 'selection': source,
            'detail': detail, 'verification_ms': round((time.perf_counter() - started) * 1000, 3)}


class ResolvedContext(dict):
    """A per-call-chain memo, never persisted or accepted from JSON runtime."""


def resolved_context(context):
    if isinstance(context, ResolvedContext) and context._selection_key == selection_key(context):
        return context, context._resolution
    root = context.get('anet_root') or Path(context['node_root']).parent
    result = resolve(root, context.get('anet_binary') or context.get('anet_exe'), home=context.get('config_home'))
    selected = ResolvedContext(context, anet_binary=result['binary'])
    selected._resolution = result
    selected._selection_key = selection_key(selected)
    return selected, result


def selection_key(context):
    return tuple(context.get(key) for key in ('anet_root', 'node_root', 'anet_binary', 'anet_exe', 'config_home'))

"""Minimal Windows client launcher; explicit local startup pin, no discovery."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import winlocal

ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT=Path(os.environ.get('ANET_SOURCE_BROKER_ROOT',str(ROOT)))
if not STATE_ROOT.is_absolute():raise ValueError('absolute_source_broker_root_required')
def configured_codex():
    configured = os.environ.get('ANET_CODEX_EXE')
    if configured:
        supplied = Path(configured)
        if not supplied.is_absolute() or supplied.suffix.lower() != '.exe' or not supplied.is_file():
            raise ValueError('explicit_installed_codex_executable_required')
        # Official Codex installations use a versioned bin junction. Resolve it
        # once, then pin and launch those exact bytes, never the mutable alias.
        path = supplied.resolve(strict=True)
        for parent in (path, *path.parents):
            if parent.is_symlink() or parent.is_junction():
                raise ValueError('codex_reparse_path_refused')
        return str(path.resolve())
    return str(Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'Programs/OpenAI/Codex/bin/codex.exe')

CODEX = configured_codex()

def launch(pipe_name=None):
    instance = str(uuid.uuid4())
    pipe = pipe_name or '\\\\.\\pipe\\anet-source-' + instance
    if os.environ.get('ANET_SOURCE_BROKER_ROOT'):winlocal.ensure_private_runtime_root(STATE_ROOT)
    state_root = winlocal.ensure_private_runtime_root(STATE_ROOT / 'state')
    # A distribution has no private runtime directories. Check both containers
    # before starting a host, never repair an existing directory's owner/ACL.
    winlocal.ensure_private_runtime_root(STATE_ROOT / 'workspaces')
    runtime_dir = winlocal.create_private_directory(state_root / instance)
    record_path = runtime_dir / 'startup.json'
    script = ROOT / 'src' / 'broker.py'
    expected_script = winlocal.sha_file(script)
    expected_codex = winlocal.sha_file(CODEX)
    process = subprocess.Popen([sys.executable, str(script), '--instance', instance,
        '--pipe', pipe, '--record', str(record_path), '--codex', CODEX],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=open(runtime_dir / 'stderr.txt', 'wb'),
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        identity = winlocal.process_identity(process.pid)
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if process.poll() is not None: raise RuntimeError('owned broker startup failed; inspect isolated stderr ' + instance)
            if record_path.exists():
                record = json.loads(record_path.read_text(encoding='utf-8'))
                if record['broker'] != identity or record['instance'] != instance or record['pipe'] != pipe:
                    raise RuntimeError('startup identity mismatch')
                if record['script_sha256'] != expected_script or record['codex']['exe_sha256'] != expected_codex:
                    raise RuntimeError('startup executable/source hash mismatch')
                if record['launcher'] != winlocal.process_identity(os.getpid()): raise RuntimeError('launcher identity mismatch')
                winlocal.request(record, 'describe')
                return process, record
            time.sleep(.05)
        raise TimeoutError('broker startup timed out; no source created')
    except BaseException:
        # No source is created before launch returns. Cleanup is tied to this
        # Popen handle; its private Windows job also owns its Codex descendants.
        if process.poll() is None:
            process.terminate(); process.wait(5)
        raise

def stop(process, record):
    winlocal.request(record, 'shutdown')
    process.wait(10)
    return process.returncode

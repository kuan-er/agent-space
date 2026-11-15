"""Running child processes: output spooled to private files, the whole tree killed on timeout or
cancellation, nothing the task wrote ever placed on a command line."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from ..activity import emit

MAX_OUTPUT = 8 * 1024 * 1024


def background_options(*, new_group=False):
    """Console children of pythonw need this on every launch, even with redirected stdio.

    Do not combine CREATE_NO_WINDOW with DETACHED_PROCESS: Windows ignores it then.
    Interactive commands (for example login) deliberately do not use this helper.
    """
    if sys.platform != 'win32':
        return {}
    return {'creationflags': 0x08000000 | (0x00000200 if new_group else 0)}


class ProcResult:
    def __init__(self, code, stdout, stderr, timed_out=False, canceled=False, duration=0.0):
        self.code, self.stdout, self.stderr = code, stdout, stderr
        self.timed_out, self.canceled, self.duration = timed_out, canceled, duration

    @property
    def ok(self):
        return self.code == 0 and not self.timed_out and not self.canceled


def windows_script_argv(argv):
    """npm installs .cmd launchers on Windows, which CreateProcess cannot start directly."""
    if sys.platform == 'win32' and Path(argv[0]).suffix.lower() in ('.cmd', '.bat'):
        return [os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/s', '/c', subprocess.list2cmdline(argv)]
    return argv


def kill_tree(proc):
    if proc.poll() is not None:
        return
    if sys.platform == 'win32':
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, **background_options())
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def run(argv, *, stdin_text='', cwd=None, env=None, timeout=None, cancel=None, max_output=MAX_OUTPUT):
    """Run argv to completion; stdin_text is written to its standard input."""
    argv = windows_script_argv([str(a) for a in argv])
    kwargs = {}
    if sys.platform == 'win32':
        kwargs.update(background_options(new_group=True))
    else:
        kwargs['start_new_session'] = True
    start = time.monotonic()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=out, stderr=err, **kwargs)
        timed_out = canceled = False
        output_size = 0
        try:
            proc.stdin.write(stdin_text.encode('utf-8'))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        while proc.poll() is None:
            size = os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size
            if size > output_size:
                emit('executor_output')
                output_size = size
            if timeout is not None and time.monotonic() - start > timeout:
                timed_out = True
                kill_tree(proc)
                break
            if cancel is not None and cancel.is_set():
                canceled = True
                kill_tree(proc)
                break
            time.sleep(0.2)
        proc.wait()
        out.seek(0)
        err.seek(0)
        stdout = out.read(max_output).decode('utf-8', errors='replace')
        stderr = err.read(64 * 1024).decode('utf-8', errors='replace')
    return ProcResult(proc.returncode, stdout, stderr, timed_out, canceled, time.monotonic() - start)


def start_detached(argv, log_path=None):
    """Start a process that outlives this one, with no console window on Windows."""
    out = open(log_path, 'a') if log_path else subprocess.DEVNULL
    kwargs = {'stdin': subprocess.DEVNULL, 'stdout': out, 'stderr': out, 'close_fds': True}
    if sys.platform == 'win32':
        kwargs.update(background_options(new_group=True))
    else:
        kwargs['start_new_session'] = True
    return subprocess.Popen([str(a) for a in argv], **kwargs)

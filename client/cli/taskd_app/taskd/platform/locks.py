"""Process-lifetime file locks (fcntl on POSIX, msvcrt on Windows)."""
from __future__ import annotations

from pathlib import Path
import sys


class SingleInstance:
    """A process-lifetime lock: a second daemon for the same data directory refuses to start."""

    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        self.file = open(self.path, 'a+b')
        if self.path.stat().st_size == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            return False
        return True

    def held_elsewhere(self):
        if self.acquire():
            self.release()
            return False
        return True

    def release(self):
        if self.file:
            self.file.close()
            self.file = None

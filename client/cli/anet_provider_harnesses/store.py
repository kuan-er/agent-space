"""Owner-private operation evidence, separate from native credentials and workspaces."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import uuid

from anet_harnesses.network import (plain, private_folder, private_existing,
                                   new_private_fd, exclusive)

MAX_RECORD = 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class ProviderStore:
    def __init__(self, root):
        self.root = plain(root)
        if os.name == 'nt':
            # Keep all private journal descendants in the extended Win32
            # namespace. A short scope directory can contain an operation
            # filename beyond MAX_PATH; do not change hashed workspace input.
            from anet_harnesses.ipc_windows import filesystem_path
            self.root = Path(filesystem_path(self.root))
        private_folder(self.root)
        self.operations = self.root / 'operations'
        private_folder(self.operations)

    def operation_path(self, operation_id):
        if not isinstance(operation_id, str) or str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError('canonical provider operation UUID required')
        return self.operations / (operation_id + '.json')

    def read(self, path):
        path = plain(path)
        if not path.exists():
            return None
        private_existing(path.parent, True)
        private_existing(path)
        if path.stat().st_nlink != 1 or path.stat().st_size > MAX_RECORD:
            raise ValueError('provider evidence file is not an ordinary bounded private file')
        with path.open('rb') as stream:
            raw = stream.read(MAX_RECORD + 1)
        if len(raw) > MAX_RECORD:
            raise ValueError('provider evidence too large')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('provider evidence must be an object')
        return value

    def write(self, path, value):
        path = plain(path)
        if path.parent not in (self.root, self.operations):
            raise ValueError('provider evidence outside fixed state directories')
        raw = canonical(value)
        if len(raw) > MAX_RECORD:
            raise ValueError('provider evidence too large')
        if path.exists():
            private_existing(path)
            if path.stat().st_nlink != 1:
                raise ValueError('provider evidence hardlink refused')
        temporary = path.parent / ('.provider-' + uuid.uuid4().hex)
        descriptor = new_private_fd(temporary)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            private_existing(temporary)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, operation):
        self.write(self.operation_path(operation['operation_id']), operation)

    def load(self, operation_id):
        return self.read(self.operation_path(operation_id))

    def lease(self):
        return self.read(self.root / 'assistant-lease.json')

    def reserve(self, operation):
        old = self.lease()
        if old and old.get('operation_id') != operation['operation_id']:
            raise ValueError('another local-assistant effect remains unresolved')
        self.save(operation)
        self.write(self.root / 'assistant-lease.json', {
            'schema': 'anet-workbuddy-local-lease/1',
            'operation_id': operation['operation_id'],
            'input_sha256': operation['input_sha256'],
        })

    def release_observed_reply(self, operation):
        lease = self.lease()
        if not lease or lease.get('operation_id') != operation['operation_id']:
            raise ValueError('local assistant lease identity differs')
        if operation.get('state') != 'reply_observed':
            raise ValueError('an unknown native effect cannot release its lease')
        (self.root / 'assistant-lease.json').unlink()

    @contextmanager
    def locked(self):
        # This is an OS byte/flock lock, not a stale PID file. The durable lease
        # still blocks another operation after a crash or observation timeout.
        with exclusive(self.root):
            yield

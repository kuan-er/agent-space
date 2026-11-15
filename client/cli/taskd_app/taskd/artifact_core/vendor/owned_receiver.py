"""Explicit owned workspace for receiving already-completed artifact deliveries.

An existing ordinary directory is never adopted. No existing file is truncated
or replaced. Resume requires recorded filesystem identities and verified bytes.
"""
from repair_protocol import *
from repair_sender import KnownFailure, UnknownOutcome

PART = 24 * 1024


def identity(path, directory=False):
    p = plain_path(path)
    info = p.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise KnownFailure('workspace entry has unexpected type')
    return [info.st_dev, info.st_ino]


def require_identity(path, expected, directory=False):
    if identity(path, directory) != expected:
        raise KnownFailure('owned workspace entry was replaced; no write performed')


def create_json(path, value):
    # Exclusive creation deliberately fails on a collision; never os.replace.
    with open_private_new(plain_path(path)) as out:
        out.write(canonical(value))
        out.flush()
        os.fsync(out.fileno())


def workspace(destination, mid, make_directory=None):
    dest = plain_path(destination)
    meta = dest / '.delivery'
    if not dest.exists():
        # A crash before owner.json exists leaves an unowned directory. Preserve
        # it and require a new destination instead of silently adopting files.
        if make_directory is None: dest.mkdir(parents=True, exist_ok=False, mode=0o700)
        else: make_directory(dest)
        if make_directory is None: meta.mkdir(mode=0o700)
        else: make_directory(meta)
        partials = meta / 'parts'
        if make_directory is None: partials.mkdir(mode=0o700)
        else: make_directory(partials)
        with open_private_new(meta / 'receive.lock') as lock:
            lock.write(b'0')
            lock.flush()
            os.fsync(lock.fileno())
        create_json(meta / 'owner.json', {
            'schema': 'artifact-receiver-workspace/1', 'manifest_sha256': mid,
            'workspace_id': uuid.uuid4().hex, 'root': identity(dest, True),
            'metadata': identity(meta, True), 'parts': identity(partials, True),
            'lock': identity(meta / 'receive.lock')})
    owner_path = plain_path(meta / 'owner.json')
    if not owner_path.is_file():
        raise KnownFailure('destination is not an owned receiver workspace; choose a new directory')
    owner = read_json(owner_path)
    if owner.get('schema') != 'artifact-receiver-workspace/1' or owner.get('manifest_sha256') != mid:
        raise KnownFailure('workspace belongs to another delivery or owner format')
    if not re.fullmatch('[a-f0-9]{32}', str(owner.get('workspace_id', ''))):
        raise KnownFailure('invalid workspace identity')
    for path, key, directory in ((dest, 'root', True), (meta, 'metadata', True),
                                 (meta / 'parts', 'parts', True), (meta / 'receive.lock', 'lock', False)):
        require_identity(path, owner.get(key), directory)
    if os.name == 'posix' and any(p.stat().st_mode & 0o077 for p in (dest, meta, meta / 'parts')):
        raise KnownFailure('owned receiver directories are not private; no permission change attempted')
    return dest, meta


def get_fragment(wire, mid, fi, offset, size):
    data, claimed = wire.request('GET', f'/api/node/artifacts/read/{mid}/{fi}/{offset}', binary=True)
    if len(data) != min(PART, size - offset) or digest(data) != claimed:
        raise KnownFailure('download fragment mismatch')
    return data


def verify_existing_prefix(wire, mid, fi, f, staging):
    """Verify complete manifest chunks locally, then at most one tail chunk.

    No truncation is used, even after an interrupted write. A partial fragment is
    compared with the authenticated server bytes and later completed by append.
    """
    size = staging.stat().st_size
    if size > f['size']:
        raise KnownFailure('owned staging exceeds manifest size')
    with staging.open('rb') as inp:
        offset = 0
        while size - offset >= CHUNK:
            if digest(inp.read(CHUNK)) != f['chunks'][offset // CHUNK]:
                raise KnownFailure('owned staging prefix corrupt; preserved without overwrite')
            offset += CHUNK
        # Cloud read offsets are multiples of PART, while CHUNK need not be.
        # Only verify the remaining tail; overlapping bytes are read from file.
        start = offset // PART * PART
        while start < size:
            fragment = get_fragment(wire, mid, fi, start, f['size'])
            inp.seek(start)
            existing = inp.read(min(len(fragment), size - start))
            if existing != fragment[:len(existing)]:
                raise KnownFailure('owned staging tail corrupt; preserved without overwrite')
            start += PART
    return size


def receive_from_portal(wire, mid, destination, make_directory=None):
    state = wire.json({'op': 'status', 'delivery_id': mid})
    manifest = state.get('manifest')
    if validate_manifest(manifest) != mid or not state.get('receipt'):
        raise KnownFailure('committed artifact manifest unavailable')
    dest, meta = workspace(destination, mid, make_directory=make_directory)
    with Lock(meta / 'receive.lock'):
        allowed = {f['name'] for f in manifest['files']} | {'.delivery'}
        if any(p.name not in allowed for p in dest.iterdir()):
            raise KnownFailure('unknown file in owned receiver workspace; no overwrite')
        if any(p.name not in {'owner.json', 'receive.lock', 'parts', 'receipt.json'} for p in meta.iterdir()):
            raise KnownFailure('unknown receiver metadata; no overwrite')
        expected_parts = {str(i) + suffix for i in range(len(manifest['files'])) for suffix in ('.partial', '.owner.json')}
        if any(p.name not in expected_parts for p in (meta / 'parts').iterdir()):
            raise KnownFailure('unknown partial entry; no overwrite')
        prior_receipt = plain_path(meta / 'receipt.json')
        if prior_receipt.exists():
            try:
                local_receipt = read_json(prior_receipt)
            except (ValueError, OSError):
                raise KnownFailure('existing receipt cannot be verified; preserved') from None
            if not state.get('recipient_receipt') or local_receipt != state['recipient_receipt']:
                raise KnownFailure('existing receipt not acknowledged by server; preserved')
        for fi, f in enumerate(manifest['files']):
            target = source_path(dest, f['name'])
            if target.exists():
                if not target.is_file() or target.stat().st_size != f['size'] or sha_file(target) != f['sha256']:
                    raise KnownFailure('existing destination differs; no overwrite')
                continue
            staging = plain_path(meta / 'parts' / (str(fi) + '.partial'))
            record = plain_path(meta / 'parts' / (str(fi) + '.owner.json'))
            if not record.exists():
                if staging.exists():
                    raise KnownFailure('partial has no ownership record; preserved')
                with open_private_new(staging) as out:
                    out.flush()
                    os.fsync(out.fileno())
                create_json(record, {'manifest_sha256': mid, 'file': fi,
                                     'identity': identity(staging), 'descriptor': f})
            owner = read_json(record)
            if owner.get('manifest_sha256') != mid or owner.get('file') != fi or owner.get('descriptor') != f:
                raise KnownFailure('partial ownership record differs; preserved')
            require_identity(staging, owner.get('identity'))
            offset = verify_existing_prefix(wire, mid, fi, f, staging)
            with staging.open('ab') as output:
                if [os.fstat(output.fileno()).st_dev, os.fstat(output.fileno()).st_ino] != owner['identity']:
                    raise KnownFailure('partial replaced before append')
                while offset < f['size']:
                    start = offset // PART * PART
                    data = get_fragment(wire, mid, fi, start, f['size'])
                    # Existing partial bytes were checked above. Only append the
                    # missing suffix; never truncate even an interrupted fragment.
                    suffix = data[offset - start:]
                    output.write(suffix)
                    output.flush()
                    os.fsync(output.fileno())
                    offset += len(suffix)
            require_identity(staging, owner['identity'])
            if staging.stat().st_size != f['size'] or sha_file(staging) != f['sha256']:
                raise KnownFailure('download full-file hash mismatch; preserved')
            try:
                os.link(staging, target, follow_symlinks=False)
            except FileExistsError:
                raise KnownFailure('destination appeared during download; no overwrite') from None
            require_identity(staging, owner['identity'])
            staging.unlink()
        files = [{k: f[k] for k in ('name', 'size', 'sha256')} for f in manifest['files']]
        try:
            receipt = wire.json({'op': 'received', 'delivery_id': mid, 'files': files})
        except UnknownOutcome:
            state = wire.json({'op': 'status', 'delivery_id': mid})
            receipt = state.get('recipient_receipt')
            if state.get('recipient_state') != 'reported_verified' or not receipt:
                raise UnknownOutcome('recipient confirmation unknown; inspect same delivery')
        if not isinstance(receipt, dict) or receipt.get('manifest_sha256') != mid or receipt.get('files') != files or receipt.get('verification') != 'reported_by_authenticated_requester' or receipt.get('anet.receipt_verified') is not None:
            raise KnownFailure('recipient acknowledgement mismatch')
        path = plain_path(meta / 'receipt.json')
        if path.exists():
            if read_json(path) != receipt:
                raise KnownFailure('existing receipt differs; preserved without overwrite')
        else:
            create_json(path, receipt)
        return receipt

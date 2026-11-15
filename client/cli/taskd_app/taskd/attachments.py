"""Attachment integrity receipts. File presence alone is never a successful download."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

from .checkers.generic import sha256_file
from .platform.locks import SingleInstance
from .store import atomic_json, private_dir, read_json, safe_name, utcnow

FAILURE_CACHE_SECONDS = 300


def _measured(path, size, digest, root):
    """Neither a cached receipt nor a transport's CID replaces measuring local bytes."""
    path, root = Path(path), Path(root).resolve()
    try:
        return (not path.is_symlink() and path.is_file()
                and path.resolve().is_relative_to(root)
                and not any(p.is_symlink() for p in path.parents if p != root and p.is_relative_to(root))
                and path.stat().st_size == size and sha256_file(path) == digest)
    except OSError:
        return False


def _cached(receipt, expected, parent):
    if receipt.get('state') != 'verified' or len(receipt.get('files', [])) != len(expected):
        return False
    try:
        files = {f['name']: f for f in receipt['files']}
        return all(f['name'] in files and files[f['name']].get('size') == f['size']
                   and files[f['name']].get('sha256') == f['sha256']
                   and _measured(files[f['name']]['path'], f['size'], f['sha256'], parent)
                   for f in expected)
    except (KeyError, TypeError, ValueError):
        return False


def _select(dest, result, expected):
    """Use measured CID mappings for numbered historical versions; keep strict legacy matching."""
    candidates = [p for p in dest.rglob('*') if p.is_file() and not p.is_symlink()]
    named_paths = {}
    for p in candidates:
        named_paths.setdefault(p.name, []).append(p)
    by_name = {f['name']: (f['size'], f['sha256']) for f in expected}
    selected = []
    if isinstance(result, dict):
        result = result.get('files', [])
    cid_paths = {}
    for item in result if isinstance(result, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get('path'), str):
            continue
        digest = cid_sha256(item.get('cid'))
        if digest:
            cid_paths.setdefault(digest, []).append(Path(item['path']))
    for f in expected:
        # ANet numbers colliding historical filenames. Its structured pull result
        # associates each written path with its CID, independently of that name.
        mapped = []
        for p in cid_paths.get(f['sha256'], []):
            if _measured(p, f['size'], f['sha256'], dest):
                mapped.append(p)
        named = named_paths.get(f['name'], [])
        matches = [p for p in named if _measured(p, f['size'], f['sha256'], dest)]
        alias = None
        if len(matches) != 1 and mapped:
            # Same digest means identical bytes, even if several historical names
            # point to them. Never trust a CID without the local size/hash check.
            matches = mapped[:1]
        if not named and not matches:
            matches = [p for p in candidates if by_name.get(p.name) == (f['size'], f['sha256'])
                       and _measured(p, f['size'], f['sha256'], dest)][:1]
            if matches:
                alias = matches[0].name
        if len(matches) != 1:
            raise ValueError('missing, corrupt or ambiguous attachment: ' + f['name'])
        selected.append((f, matches[0], alias))
    return selected


def download_root(state_root):
    node = hashlib.sha256(str(Path(state_root).resolve()).encode()).hexdigest()[:12]
    # A launchd daemon may block on macOS protected Downloads/Desktop folders.
    # Use an ordinary dedicated directory, without requesting broader OS access.
    return Path.home() / 'ANet' / 'received' / node


def cid_sha256(cid):
    try:
        if not isinstance(cid, str) or not cid.startswith('b'):
            return None
        raw = base64.b32decode(cid[1:].upper() + '=' * (-(len(cid) - 1) % 8))
        # CIDv1, raw, sha2-256, 32 bytes. Reject other codecs/hash functions.
        return raw[4:].hex() if len(raw) == 36 and raw[:4] == b'\x01\x55\x12\x20' else None
    except (ValueError, base64.binascii.Error):
        return None


def descriptors(message):
    out = []
    for part in (message or {}).get('parts', []):
        if 'raw' not in part and 'url' not in part:
            continue
        meta = part.get('metadata') or {}
        out.append({'name': part.get('filename') or 'file', 'size': meta.get('anet.size'),
                    'sha256': cid_sha256(meta.get('anet.attachment_cid') or meta.get('anet.cid')),
                    'cid': meta.get('anet.attachment_cid') or meta.get('anet.cid')})
    return out


def receive(transport, task_id, message, envelope, root):
    """Stable manifest receipts and task-local content objects; discard only new staging.

    Both are provider declarations, not independent business correctness. The local
    size/hash measurements prove only that those declared bytes arrived intact.
    """
    expected = (envelope or {}).get('attachments') or message.get('attachments') or []
    announced = message.get('files') or []
    if not expected and not announced:
        return {'state': 'not_required', 'at': utcnow(), 'files': []}
    receipt = {'state': 'failed', 'at': utcnow(), 'files': [], 'expected': expected, 'retryable': False}
    lock = None
    receipt_path = None
    try:
        if not expected:
            raise ValueError('attachments have no verifiable size/hash metadata')
        names = set()
        for f in expected:
            name = f.get('name', '')
            if not name or name in ('.', '..') or '/' in name or '\\' in name or name in names:
                raise ValueError('unsafe or duplicate attachment name')
            names.add(name)
            if type(f.get('size')) is not int or f['size'] < 0 or not re.fullmatch('[0-9a-f]{64}', f.get('sha256') or ''):
                raise ValueError('missing expected size/SHA-256 for ' + name)
        if set(announced) - names:
            raise ValueError('manifest does not cover all announced attachments')
        parent = private_dir(Path(root) / safe_name(task_id)).resolve()
        if any(p.name.startswith('.anet') for p in (parent, *parent.parents)):
            raise ValueError('attachment directory must be outside .anet')
        lock = SingleInstance(parent / '.receive.lock')
        if not lock.acquire():
            return dict(receipt, error='another attachment receive is in progress', retryable=True)
        fingerprint = hashlib.sha256(json.dumps(sorted(expected, key=lambda f: f['name']),
                                                sort_keys=True).encode()).hexdigest()
        dest = parent / ('receive-' + fingerprint)
        if dest.is_symlink():
            raise ValueError('unsafe receive directory')
        private_dir(dest)
        receipt_path = dest / 'receipt.json'
        try:
            prior = read_json(receipt_path, {})
        except (ValueError, OSError):
            prior = {}
        if not isinstance(prior, dict):
            prior = {}
        if _cached(prior, expected, parent):
            receipt = dict(prior, reused=True)
            return receipt
        if (prior.get('state') == 'failed' and not prior.get('retryable', True)
                and prior.get('retry_after', 0) > time.time()):
            receipt = dict(prior, reused=True)
            return receipt
        receipt['directory'] = str(dest)
        objects = parent / 'objects'
        if objects.is_symlink():
            raise ValueError('unsafe content object directory')
        private_dir(objects)
        missing = [f for f in expected if not _measured(objects / f['sha256'], f['size'], f['sha256'], objects)]
        aliases = {}
        if missing:
            # Even old ANet versions may export the entire history. It is temporary:
            # only selected, verified content survives, including on pull failure.
            with tempfile.TemporaryDirectory(prefix='.staging-', dir=parent) as staging:
                receipt['retryable'] = True
                result = transport.pull(task_id, staging)
                receipt['retryable'] = False
                selected = _select(Path(staging), result, missing)
                for f, source, alias in selected:
                    target = objects / f['sha256']
                    if target.is_symlink():
                        raise ValueError('unsafe content object')
                    if not _measured(target, f['size'], f['sha256'], objects):
                        # Same-filesystem rename retains ANet quarantine attributes,
                        # and avoids writing another full copy of the payload.
                        source.chmod(0o400)
                        if target.exists() and os.name == 'nt':
                            target.chmod(0o600)
                        os.replace(source, target)
                        if not _measured(target, f['size'], f['sha256'], objects):
                            raise OSError('attachment changed while storing: ' + f['name'])
                    if alias:
                        aliases[f['name']] = alias
        views = dest / 'files'
        if views.is_symlink():
            raise ValueError('unsafe attachment view directory')
        private_dir(views)
        content_paths = {}
        for f in expected:
            source = objects / f['sha256']
            target = content_paths.get(f['sha256'], views / f['name'])
            if not _measured(target, f['size'], f['sha256'], views):
                if target.is_symlink():
                    raise ValueError('unsafe attachment view')
                if target.exists() and os.name == 'nt':
                    target.chmod(0o600)
                target.unlink(missing_ok=True)
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copy2(source, target)
                if not _measured(target, f['size'], f['sha256'], views):
                    raise OSError('attachment changed while materializing: ' + f['name'])
            content_paths[f['sha256']] = target
            record = dict(name=f['name'], path=str(target), size=f['size'], sha256=f['sha256'],
                          verified_at=utcnow(), source_path=f.get('source_path'))
            if f['name'] in aliases:
                record['aliased_from'] = aliases[f['name']]
            receipt['files'].append(record)
        receipt.update(state='verified', at=utcnow())
    except Exception as e:
        receipt.update(state='failed', error=str(e)[:500], at=utcnow())
        receipt['retryable'] = not isinstance(e, ValueError)
        receipt['retry_after'] = time.time() + FAILURE_CACHE_SECONDS
    finally:
        try:
            if receipt_path and receipt.get('directory'):
                atomic_json(receipt_path, receipt)
        finally:
            if lock:
                lock.release()
    return receipt

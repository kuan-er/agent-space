"""Public, read-only desktop release distribution. No model or node action occurs."""
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

SCHEMA = 'anet-desktop-release/1'
CAPABILITY_SCHEMA = 'anet-harness-capabilities/1'
PUBLIC_SOURCE = 'https://124.222.173.36:8888'
HARNESS_NAMES = {'codex': 'Codex', 'claude': 'Claude Code',
                 'workbuddy': 'WorkBuddy', 'deepseek': 'DeepSeek Harness'}
_verified = {}
_lock = threading.Lock()


def _load(root, name):
    path = root / name
    if path.is_symlink() or path.stat().st_size > 131072:
        raise ValueError('invalid public release manifest')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('invalid public release manifest')
    return data


def _artifact(root, data):
    a = data['artifact']
    name = a['name']
    if not re.fullmatch(r'ANetSetup-[A-Za-z0-9][A-Za-z0-9._-]{0,90}\.exe', name):
        raise ValueError('invalid artifact name')
    digest = a['sha256']
    size = a['bytes']
    if not re.fullmatch('[a-f0-9]{64}', digest) or type(size) is not int or not 1 <= size <= 2**31:
        raise ValueError('invalid artifact integrity metadata')
    download_root = Path(os.environ.get('PORTAL_DESKTOP_DOWNLOADS', root / 'desktop-downloads'))
    path = download_root / name
    if path.is_symlink() or download_root.is_symlink():
        raise ValueError('artifact must be a regular release file')
    stat = path.stat()
    if not path.is_file() or stat.st_size != size:
        raise ValueError('artifact size mismatch')
    key = (str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, size, digest)
    with _lock:
        if key not in _verified:
            with path.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != digest:
                raise ValueError('artifact digest mismatch')
            _verified.clear()
            _verified[key] = True
    return path, {'name': name, 'url': '/downloads/' + name, 'sha256': digest,
                  'bytes': size, 'architecture': a.get('architecture', 'x64'),
                  'signing': a.get('signing', 'not_provided')}


def release(root):
    try:
        data = _load(root, 'desktop-release.json')
        if data.get('schema') != SCHEMA or not re.fullmatch('[A-Za-z0-9._-]{1,96}', data.get('version', '')):
            raise ValueError('unsupported manifest')
        _, artifact = _artifact(root, data)
        minimum = data.get('minimum_version', '0.2.0-preview.5')
        if not isinstance(minimum,str) or not re.fullmatch('[A-Za-z0-9._-]{1,96}', minimum):
            raise ValueError('invalid minimum version')
        signing = artifact['signing'] if artifact['signing'] in ('unsigned','verified') else 'not_provided'
        download_url = PUBLIC_SOURCE + artifact['url']
        return {'schema': SCHEMA, 'available': True, 'version': data['version'],
                'channel': data.get('channel', 'preview'), 'artifact': artifact,
                'source': PUBLIC_SOURCE, 'minimum_version': minimum,
                'download_url': download_url, 'bytes': artifact['bytes'], 'sha256': artifact['sha256'],
                'signature': {'status': signing},
                'windows': {'amd64': {'url': download_url, 'bytes': artifact['bytes'],
                                      'sha256': artifact['sha256'], 'authenticode': signing}},
                'operating_systems': ['Windows 10', 'Windows 11'],
                'setup': 'per_user_single_installer', 'capabilities_url': '/api/harness-capabilities'}
    except (OSError, ValueError, KeyError, TypeError):
        return {'schema': SCHEMA, 'available': False, 'reason': 'release_not_verified',
                'capabilities_url': '/api/harness-capabilities'}


def capabilities(root):
    result = {'schema': CAPABILITY_SCHEMA, 'scope': 'desktop_adapter_contract',
              'node_readiness': 'requires_local_probe', 'remote_task_verified': False,
              'harnesses': []}
    try:
        data = _load(root, 'harness-capabilities.json')
        if data.get('schema') != CAPABILITY_SCHEMA or not isinstance(data.get('harnesses'), list):
            raise ValueError('invalid capabilities')
        supplied = {h['id']: h for h in data['harnesses'] if isinstance(h, dict) and h.get('id') in HARNESS_NAMES}
    except (OSError, ValueError, KeyError, TypeError):
        supplied = {}
    statuses = {'implemented', 'verified', 'not_verified', 'not_supported', 'requires_login'}
    for key, name in HARNESS_NAMES.items():
        source = supplied.get(key, {})
        row = {'id': key, 'name': name}
        for field in ('adapter', 'isolated_validation', 'live_model_validation',
                      'execution', 'session_resume', 'cancel', 'result_return'):
            value = source.get(field, 'not_verified')
            row[field] = value if isinstance(value,str) and value in statuses else 'not_verified'
        # The public catalog deliberately carries no machine identity, paths, credentials,
        # private logs or unverified assertion that a particular device is ready.
        row['node_readiness'] = 'requires_local_probe'
        contract=source.get('capabilities')
        if isinstance(contract,dict):
            row['capabilities']={}
            for feature in ('session_resume','native_goal','goal_pause','goal_resume','cancel','status','result'):
                claim=contract.get(feature,{})
                if not isinstance(claim,dict):claim={}
                row['capabilities'][feature]={field:claim.get(field) is True for field in
                    ('documented','implemented','probe_verified','e2e_verified')}
            row['business_result_verified']=source.get('business_result_verified') is True
            row['automatic_source_callback_verified']=source.get('automatic_source_callback_verified') is True
        result['harnesses'].append(row)
    return result


def _range(header, size):
    if not header:
        return 0, size - 1, False
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not match or not any(match.groups()):
        raise ValueError('invalid range')
    left, right = match.groups()
    if not left:
        suffix = int(right)
        if suffix < 1:
            raise ValueError('invalid suffix range')
        return max(0, size - suffix), size - 1, True
    start = int(left)
    end = min(int(right), size - 1) if right else size - 1
    if start >= size or end < start:
        raise ValueError('unsatisfiable range')
    return start, end, True


def route(handler, method, root):
    path = urlsplit(handler.path).path
    if path not in ('/api/desktop-release', '/api/harness-capabilities') and not path.startswith('/downloads/'):
        return False
    if method != 'GET':
        handler.respond({'error': '只支持读取下载信息'}, 405)
        return True
    if path == '/api/desktop-release':
        handler.respond(release(root))
        return True
    if path == '/api/harness-capabilities':
        handler.respond(capabilities(root))
        return True
    data = release(root)
    artifact = data.get('artifact', {})
    if not data['available'] or path != artifact.get('url'):
        handler.respond({'error': '安装包不存在或尚未完成完整性校验'}, 404)
        return True
    etag = '"' + artifact['sha256'] + '"'
    size = artifact['bytes']
    if handler.headers.get('If-None-Match') == etag and not handler.headers.get('Range'):
        handler.send_response(304)
        handler.send_header('ETag', etag)
        handler.send_header('Cache-Control', 'public, max-age=3600')
        handler.end_headers()
        return True
    requested = handler.headers.get('Range')
    if handler.headers.get('If-Range') not in (None, etag):
        requested = None
    try:
        start, end, partial = _range(requested, size)
    except ValueError:
        handler.send_response(416)
        handler.send_header('Content-Range', 'bytes */' + str(size))
        handler.send_header('Content-Length', '0')
        handler.end_headers()
        return True
    # Keep this response bound to the requested immutable version even if a
    # newer manifest is atomically published between metadata and streaming.
    actual, _ = _artifact(root, {'artifact': artifact})
    # Open the verified file before committing headers, then stream bounded chunks.
    with actual.open('rb') as stream:
        handler.send_response(206 if partial else 200)
        handler.send_header('Content-Type', 'application/vnd.microsoft.portable-executable')
        handler.send_header('Content-Disposition', 'attachment; filename="' + artifact['name'] + '"')
        handler.send_header('Content-Length', str(end - start + 1))
        handler.send_header('Accept-Ranges', 'bytes')
        handler.send_header('ETag', etag)
        handler.send_header('Cache-Control', 'public, max-age=3600')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        if partial:
            handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        handler.end_headers()
        if handler.command != 'HEAD':
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                block = stream.read(min(1048576, remaining))
                if not block:
                    break
                handler.wfile.write(block)
                remaining -= len(block)
    return True

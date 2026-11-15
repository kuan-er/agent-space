"""Install the official signed node, without replacing an existing identity."""
import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from .binary import resolve

MIRROR = 'https://124.222.173.36:8888/downloads/anet-mirror/releases/0.3.9-20261107/dl'
SIGNER = 'anet-release@agentnetwork.org.cn'
KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAMTUwPlzeKmU7qr+eicaQVuxmltc5mY1sTmwfhIJJEL'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('下载地址发生跳转，已停止安装。')


def download(url, maximum):
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError('下载地址必须使用 HTTPS。')
    with build_opener(NoRedirect).open(Request(url), timeout=45) as response:
        raw = response.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('下载文件超出大小限制。')
    return raw


def discover(root, explicit=None):
    result = resolve(root, explicit)
    return Path(result['binary']) if result['binary'] else None


def safe_parent(path):
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('安装目录不能经过符号链接。')


def install(root, mirror=MIRROR, explicit=None):
    root = Path(root).expanduser().absolute()
    safe_parent(root)
    found = discover(root, explicit)
    if found:
        return {'state': 'reused', 'binary': str(found)}
    # Missing executable for an existing identity is a repair case. Never guess
    # an older replacement for a potentially operator-specific build.
    if (root / 'config.json').exists():
        raise ValueError('发现已有身份，但未找到原程序。请使用 --anet-binary 指定原程序。')
    system = platform.system().lower()
    arch = {'x86_64': 'amd64', 'AMD64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
    if system not in ('linux', 'darwin') or not arch:
        raise ValueError('此安装命令支持 macOS 和 Linux；Windows 请使用安装包。')
    verifier = shutil.which('ssh-keygen')
    if not verifier:
        raise ValueError('需要 OpenSSH 的 ssh-keygen 来验证发行签名。')
    raw = download(mirror + '/release.json', 1024 * 1024)
    sig = download(mirror + '/release.json.sig', 16384)
    with tempfile.TemporaryDirectory(prefix='anet-verify-') as tmp:
        folder = Path(tmp)
        (folder / 'signature').write_bytes(sig)
        (folder / 'signers').write_text(SIGNER + ' namespaces="' + SIGNER + '" ' + KEY + '\n', encoding='ascii')
        result = subprocess.run([verifier, '-Y', 'verify', '-f', str(folder / 'signers'), '-I', SIGNER,
                                 '-n', SIGNER, '-s', str(folder / 'signature')], input=raw, capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError('ANet 官方签名验证失败。')
    release = json.loads(raw)
    expiry = datetime.datetime.fromisoformat(release['expires_at'].replace('Z', '+00:00'))
    if release.get('schema') != 'anet-release/1' or expiry <= datetime.datetime.now(datetime.timezone.utc):
        raise ValueError('发行信息格式错误或已经过期。')
    asset = 'anet-' + system + '-' + arch
    row = release['assets'][asset]
    if row['variant'] != 'default' or row['os'] != system or row['arch'] != arch or not (0 < row['size'] < 100_000_000):
        raise ValueError('发行平台信息不匹配。')
    packed = download(mirror + '/' + asset + '.gz', row['gz_size'])
    if len(packed) != row['gz_size'] or hashlib.sha256(packed).hexdigest() != row['gz_sha256']:
        raise ValueError('安装文件校验失败。')
    import io
    with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
        binary = stream.read(row['size'] + 1)
    if len(binary) != row['size'] or hashlib.sha256(binary).hexdigest() != row['sha256']:
        raise ValueError('ANet 程序校验失败。')
    target = root / 'bin/anet'
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    safe_parent(target)
    fd, temporary = tempfile.mkstemp(prefix='.anet-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(binary)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o700)
        # An exclusive hard link prevents two installers from overwriting each other.
        os.link(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    (target.parent / 'anet.release.json').write_bytes(raw)
    (target.parent / 'anet.release.json.sig').write_bytes(sig)
    return {'state': 'installed', 'binary': str(target), 'version': release['version'], 'signature': 'verified'}

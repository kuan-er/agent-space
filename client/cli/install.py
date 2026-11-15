#!/usr/bin/env python3
"""Install only this versioned configuration application, never node state."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import platform


def install(source, prefix):
    if sys.version_info < (3, 11):
        raise ValueError('ANet Config 需要 Python 3.11 或更新版本。')
    source = Path(source).resolve()
    prefix = Path(prefix).expanduser().absolute()
    for path in (prefix, *prefix.parents):
        if path.is_symlink():
            raise ValueError('安装目录不能经过符号链接。')
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    native = manifest.get('native')
    if native:
        if native.get('system') != platform.system() or native.get('machine') != platform.machine():
            raise ValueError('安装包与本机系统或处理器不匹配，请下载对应版本。')
    version = manifest['version']
    import re
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-preview\.[0-9]+)?', version):
        raise ValueError('版本号格式无效。')
    paths = []
    seen = set()
    for row in manifest['files']:
        relative = row['path']
        if not isinstance(relative, str) or Path(relative).is_absolute() or '\\' in relative or ':' in relative or any(p in ('', '.', '..') for p in relative.split('/')) or relative in seen:
            raise ValueError('安装文件路径无效。')
        seen.add(relative)
        path = source / row['path']
        if path.is_symlink() or not path.is_file() or source not in path.resolve().parents:
            raise ValueError('安装文件路径无效。')
        raw = path.read_bytes()
        if len(raw) != row['bytes'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('安装包校验失败：' + row['path'])
        mode = row.get('mode', 0o644)
        if mode not in (0o644, 0o755):
            raise ValueError('安装文件权限无效。')
        paths.append((path, row['path'], mode))
    releases = prefix / 'lib/anet-config'
    bindir = prefix / 'bin'
    for target in (releases, bindir):
        for path in (target, *target.parents):
            if path.is_symlink():
                raise ValueError('安装目录不能经过符号链接。')
    releases.mkdir(parents=True, exist_ok=True)
    destination = releases / version
    if destination.is_symlink():
        raise ValueError('安装目录不能经过符号链接。')
    if destination.exists():
        if (destination / 'manifest.json').read_bytes() != (source / 'manifest.json').read_bytes():
            raise ValueError('同版本安装内容不同，请使用新的版本号。')
        for row in manifest['files']:
            installed = destination / row['path']
            if installed.is_symlink() or destination.resolve() not in installed.resolve().parents or hashlib.sha256(installed.read_bytes()).hexdigest() != row['sha256']:
                raise ValueError('已安装内容发生变更，请先检查该目录。')
    else:
        stage = Path(tempfile.mkdtemp(prefix='.install-', dir=releases))
        try:
            for path, relative, mode in paths:
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                os.chmod(target, mode)
            shutil.copyfile(source / 'manifest.json', stage / 'manifest.json')
            os.rename(stage, destination)
        finally:
            if stage.exists():
                if stage.parent.resolve() != releases.resolve() or not stage.name.startswith('.install-'):
                    raise ValueError('安装临时目录边界无效。')
                shutil.rmtree(stage)
    bindir.mkdir(parents=True, exist_ok=True)
    launcher = bindir / 'anet-config'
    marker = '# ANet Config managed launcher\n'
    if launcher.exists() and (launcher.is_symlink() or marker not in launcher.read_text(encoding='utf-8')):
        raise ValueError('已有同名命令，未覆盖：' + str(launcher))
    import shlex
    command = (shlex.quote(str(destination / 'native/anet-config')) if native else
               shlex.quote(sys.executable) + ' ' + shlex.quote(str(destination / 'main.py')))
    text = '#!/bin/sh\n' + marker + 'exec ' + command + ' "$@"\n'
    fd, name = tempfile.mkstemp(prefix='.anet-config-', dir=bindir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o755)
        os.replace(name, launcher)
    finally:
        Path(name).unlink(missing_ok=True)
    return launcher


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='安装 ANet Config，不改变已有节点或智能体配置。')
    parser.add_argument('--prefix', type=Path, default=Path.home() / '.local')
    args = parser.parse_args()
    try:
        command = install(Path(__file__).parent, args.prefix)
        print('ANet Config 已安装。开始设置：\n  ' + str(command) + ' setup')
        if str(command.parent) not in os.environ.get('PATH', '').split(os.pathsep):
            print('可将此目录加入 PATH：' + str(command.parent))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

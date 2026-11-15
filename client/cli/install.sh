#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$script_dir/native-platform" ]; then
    actual="$(uname -s)-$(uname -m)"
    expected=$(cat "$script_dir/native-platform")
    if [ "$actual" != "$expected" ]; then
        printf '%s\n' "此安装包适用于 $expected，当前系统为 $actual。请下载对应版本。" >&2
        exit 1
    fi
    cd "$script_dir"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum --check --status SHA256SUMS || { printf '%s\n' '安装包校验失败，请重新下载。' >&2; exit 1; }
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 --check --status SHA256SUMS || { printf '%s\n' '安装包校验失败，请重新下载。' >&2; exit 1; }
    else
        printf '%s\n' '缺少 SHA-256 校验工具。请安装 shasum 或 sha256sum 后重试。' >&2
        exit 1
    fi
    exec "$script_dir/native/anet-config" --install-package "$@"
fi
for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3.14 python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        exec "$candidate" "$script_dir/install.py" "$@"
    fi
done
printf '%s\n' 'ANet Config 需要 Python 3.11 或更新版本。安装 Python 后，重新运行 sh install.sh。' >&2
exit 1

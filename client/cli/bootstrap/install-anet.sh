#!/bin/sh
# ANet Config verified release installer; no administrator privileges required.
set -eu
version=0.3.0-preview.2
base=https://124.222.173.36:8888/downloads/anet-config/0.3.0-preview.2
prefix=${HOME:?}/.local
while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix) [ "$#" -ge 2 ] || { echo '缺少 --prefix 目录。' >&2; exit 2; }; prefix=$2; shift 2 ;;
        --base-url) [ "$#" -ge 2 ] || { echo '缺少 --base-url 地址。' >&2; exit 2; }; base=${2%/}; shift 2 ;;
        *) echo '用法：sh install-anet.sh [--prefix 安装目录] [--base-url HTTPS发布地址]' >&2; exit 2 ;;
    esac
done
case "$base" in https://*) ;; *) echo '下载地址必须使用 HTTPS。' >&2; exit 2 ;; esac
command -v curl >/dev/null 2>&1 || { echo '需要 curl 下载软件包。' >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then
    hash_command=sha256sum
elif command -v shasum >/dev/null 2>&1; then
    hash_command=shasum
else
    echo '需要 shasum 或 sha256sum 校验软件包。' >&2; exit 1
fi
platform="$(uname -s)-$(uname -m)"
at_least() {
    awk -v actual="$1" -v required="$2" 'BEGIN { split(actual,a,"."); split(required,r,"."); for(i=1;i<=3;i++){ if(a[i]+0>r[i]+0)exit 0; if(a[i]+0<r[i]+0)exit 1 } exit 0 }'
}
case "$platform" in
    Darwin-arm64)
        label=macos-arm64
        system_version=$(/usr/bin/sw_vers -productVersion)
        if ! at_least "$system_version" 13.5; then
            echo '当前 macOS 版本使用 Python 兼容版。原生完整包需要 macOS 13.5 或更新版本。'
            label=macos-linux
        fi ;;
    Linux-x86_64)
        label=linux-amd64
        libc_version=$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}') || libc_version=
        if [ -z "$libc_version" ] || ! at_least "$libc_version" 2.34; then
            echo '当前系统使用 Python 兼容版。原生包需要 glibc 2.34 或更新版本。'
            label=macos-linux
        fi ;;
    Darwin-x86_64|Linux-aarch64|Linux-arm64)
        label=macos-linux
        echo '此架构使用 Python 兼容版，需要 Python 3.11 或更新版本。' ;;
    *) echo "暂未提供此平台安装包：$platform" >&2; exit 1 ;;
esac
if [ "$label" = macos-linux ]; then
    echo 'Python 兼容版需要 Python 3.11 或更新版本。'
fi
asset="anet-config-$version-$label.tar.gz"
folder="anet-config-$version-$label"
[ "$label" != macos-linux ] || folder="anet-config-$version"
workspace=$(mktemp -d "${TMPDIR:-/tmp}/anet-config-install.XXXXXXXX")
cleanup() {
    case "$workspace" in "${TMPDIR:-/tmp}"/anet-config-install.*) rm -rf -- "$workspace" ;; esac
}
trap cleanup EXIT HUP INT TERM
fetch() {
    code=$(curl --proto '=https' --tlsv1.2 --fail --silent --show-error --connect-timeout 20 --max-time 900 \
        --output "$2" --write-out '%{http_code}' "$1")
    [ "$code" = 200 ] || { echo '下载未返回完整文件，请稍后重试。' >&2; exit 1; }
}
fetch "$base/SHA256SUMS" "$workspace/SHA256SUMS"
expected=$(awk -v file="$asset" '$2 == file { print $1 }' "$workspace/SHA256SUMS")
[ "${#expected}" -eq 64 ] || { echo '未找到该平台的软件包校验信息。' >&2; exit 1; }
case "$expected" in *[!0-9a-f]*) echo '软件包校验信息格式错误。' >&2; exit 1 ;; esac
echo "正在下载 ANet Config · $platform"
fetch "$base/$asset" "$workspace/$asset"
if [ "$hash_command" = sha256sum ]; then
    actual=$(sha256sum "$workspace/$asset" | awk '{print $1}')
else
    actual=$(shasum -a 256 "$workspace/$asset" | awk '{print $1}')
fi
[ "$expected" = "$actual" ] || { echo '软件包校验失败，未执行安装。' >&2; exit 1; }
tar -tzf "$workspace/$asset" > "$workspace/contents"
while IFS= read -r entry; do
    case "$entry" in "$folder"|"$folder"/|"$folder"/*) ;; *) echo '安装包目录格式错误。' >&2; exit 1 ;; esac
    case "/$entry/" in */../*|*/./*) echo '安装包路径不安全。' >&2; exit 1 ;; esac
done < "$workspace/contents"
tar -xzf "$workspace/$asset" -C "$workspace"
sh "$workspace/$folder/install.sh" --prefix "$prefix"

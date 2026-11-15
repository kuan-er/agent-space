"""Make desktop-launched SDK networking follow the user's OS proxy choice."""
import os
from pathlib import PureWindowsPath
import sys
from urllib.parse import urlsplit
from urllib.request import getproxies


def native_runtime_cwd(environ=None, platform=None):
    """Select only the native process launch directory on Windows.

    The pinned single-file DeepSeek runtime fails loading its embedded Koffi
    module from a non-system drive. The official runtime_cwd setting separates
    this launch location from initialize.cwd (the actual agent workspace).
    No files, permissions, or global environment values are changed here.
    """
    if (sys.platform if platform is None else platform) != 'win32':
        return None
    environ = os.environ if environ is None else environ
    root = environ.get('SystemRoot') or environ.get('SYSTEMROOT')
    if not isinstance(root, str) or not PureWindowsPath(root).is_absolute() or not os.path.isdir(root):
        raise RuntimeError('windows_system_directory_unavailable')
    return root


def system_proxy_environment(environ=None, proxies=None):
    """Return child-only additions, never overwrite explicit harness proxy env.

    Node fetch and the official DeepSeek launcher do not detect the Windows or
    macOS system proxy. Python does. Unsupported SOCKS/PAC schemes stay out;
    this function does not weaken certificate checks or change global settings.
    """
    environ = os.environ if environ is None else environ
    if any(key.lower() in ('http_proxy', 'https_proxy', 'all_proxy') for key in environ):
        return {}
    proxies = getproxies() if proxies is None else proxies
    result = {}
    for scheme in ('http', 'https'):
        value = proxies.get(scheme)
        if not isinstance(value, str):
            continue
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            continue
        result[scheme.upper() + '_PROXY'] = value
    if result and not any(k.lower() == 'no_proxy' for k in environ):
        bypass = proxies.get('no', '')
        compatible = [item.strip() for item in bypass.replace(';', ',').split(',')
                      if item.strip() and '<' not in item and '/' not in item]
        result['NO_PROXY'] = ','.join(dict.fromkeys(compatible + ['localhost', '127.0.0.1', '::1']))
    return result

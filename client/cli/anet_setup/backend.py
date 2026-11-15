"""Shared desktop/terminal entry. No business task submission is exposed."""


def execute(request, context):
    action = request.get('action', '')
    group, _, name = action.partition('.')
    if action == 'account.enable_receiver':
        from anet_account.receiver import execute as handler
    elif action == 'config.receiver':
        from anet_harnesses.receiver_config import execute as handler
    elif group == 'account' and name in ('status', 'login', 'logout'):
        from anet_account import execute as handler
    elif group == 'config' and name in ('status', 'agents', 'connect', 'configure_agent', 'verify', 'workspace', 'service', 'doctor'):
        from anet_harnesses.client_config import execute as handler
    else:
        raise ValueError('不支持的配置操作。')
    from .binary import resolved_context
    selected, resolution = resolved_context(context)
    result = handler(request if action in ('account.enable_receiver', 'config.receiver') else {**request, 'action': name}, selected)
    if isinstance(result, dict):
        result = {**result, 'binary_resolution': resolution}
    return result

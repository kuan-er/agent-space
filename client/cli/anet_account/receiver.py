"""One-time reauthentication to enable the exact prepared receiving device."""
from . import Session, origin, MESSAGES
from .network import Network, NativeRunner, NetworkError, RemoteFailure, read_private, write_private, plain


def execute(request, context, *, session_factory=Session, receiver_factory=None, network_factory=Network):
    session = None
    try:
        from anet_harnesses.receiver_config import Receiver
        receiver = (receiver_factory or Receiver)(context)
        if request.get('confirm') is not True:
            raise NetworkError('receiver_confirmation_required')
        _, plan = receiver.plan(request.get('plan_id'))
        if plan.get('state') != 'prepared':
            return receiver.status()
        receiver.validate(plan)
        root = plain(context['anet_root'])
        saved = read_private(root/'portal/connection.json') or {}
        account = read_private(root/'portal/account.json') or {}
        portal = origin(saved.get('portal'))
        if account.get('portal') and origin(account['portal']) != portal:
            raise NetworkError('existing_portal_preserved')
        if saved.get('agent_id') != plan['agent_id'] or saved.get('aid') != plan['aid']:
            raise NetworkError('existing_identity_preserved')
        username = request.get('username') or account.get('username')
        password = request.get('password')
        if not isinstance(username,str) or not username.strip() or len(username)>80 or not isinstance(password,str) or not password or len(password)>200:
            raise NetworkError('login_invalid')
        session = session_factory(portal)
        try:
            me = session.login(username.strip(), password)
        except RemoteFailure as error:
            if error.status in (401,403):
                raise NetworkError('login_failed') from None
            raise
        password = None
        if account.get('user_id') and me.get('id') != account['user_id']:
            raise NetworkError('account_mismatch')
        rows = session.request('/api/agents').get('agents', []) + session.request('/api/devices').get('devices', [])
        match = next((row for row in rows if row.get('id') == plan['agent_id'] and row.get('aid') == plan['aid'] and row.get('is_owner') is True), None)
        if not match:
            raise NetworkError('account_mismatch')
        if match.get('kind') == 'device':
            # Only this mutation is sent, once. A lost response is reconciled by
            # node/info on the next user invocation, never a duplicate identity.
            session.request('/api/client/receiver', {'agent_id':plan['agent_id'], 'aid':plan['aid'],
                            'executor': plan['portal_executor'], 'confirm': True})
        elif match.get('executor') != plan['portal_executor']:
            raise NetworkError('existing_executor_preserved')
        network = network_factory({**context, 'portal': portal})
        info = network._authenticate(saved, plan['aid'])
        if not info or info.get('kind') != 'agent' or info.get('executor') != plan['portal_executor']:
            raise NetworkError('receiver_conversion_unverified')
        write_private(root/'portal/connection.json', {**saved, 'kind':'agent', 'executor':plan['portal_executor']})
        return receiver.launch({'plan_id': plan['plan_id'], 'confirm': True})
    except Exception as error:
        code = error.code if isinstance(error, NetworkError) else str(error) if type(error).__name__ == 'Conflict' else type(error).__name__
        from anet_harnesses.receiver_config import MESSAGES as RECEIVER_MESSAGES
        return {'schema':'anet-account/1','ok':False,'state':'requires_setup','requires_setup':True,
                'code':code,'message':RECEIVER_MESSAGES.get(code,MESSAGES.get(code,'接收任务尚未完成；请检查当前连接后继续。'))}
    finally:
        if session:
            session.close()

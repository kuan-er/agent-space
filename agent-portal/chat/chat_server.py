"""Loopback HTTP adapter for browser sessions and scoped bearer clients."""
import argparse
import importlib.util
from pathlib import Path
import re
import sys
import json
from urllib.parse import urlsplit
from chat_service import Service, ChatError


def make_handler(portal, service):
    class Handler(portal.Handler):
        server_version = 'ANetChat/1'

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def dispatch(self, method):
            try:
                parsed = urlsplit(self.path)
                path = parsed.path
                if path == '/healthz' and method == 'GET':
                    return self.respond({'ok': True, 'service': 'anet-chat'})
                if parsed.query or parsed.fragment or self.headers.get('Transfer-Encoding'):
                    raise ChatError('请求地址或格式无效。')
                external = path.startswith('/api/v1/')
                if external:
                    auth = self.headers.get('Authorization', '')
                    if not auth.startswith('Bearer '):
                        raise ChatError('需要 Authorization: Bearer 令牌。', 401)
                    principal = service.authenticate(auth[7:], 'chat:read' if method == 'GET' else 'chat:write')
                    user_id = principal['user_id']
                    path = '/api/chat/' + path[len('/api/v1/'):]
                else:
                    if self.headers.get('Origin') and self.headers['Origin'] != portal.BASE:
                        raise ChatError('不接受跨站请求。', 403)
                    with portal.db() as c:
                        user = self.user(c, method != 'GET')
                    user_id, principal = user['id'], None
                self.rate('direct-chat:' + user_id, 600 if method == 'GET' else 60)
                if method in ('POST', 'DELETE'):
                    try:
                        length = int(self.headers.get('Content-Length', '0'))
                    except ValueError:
                        raise ChatError('请求格式无效。')
                    if not 0 <= length <= 60000:
                        raise ChatError('请求内容过大。', 413)
                    try:
                        data = json.loads(self.rfile.read(length) or b'{}')
                    except (ValueError, UnicodeError):
                        raise ChatError('请求内容必须为 JSON 对象。')
                    if not isinstance(data, dict):
                        raise ChatError('请求内容必须为 JSON 对象。')
                else:
                    data = {}
                status = 200
                if path == '/api/chat/agents' and method == 'GET':
                    result = service.agents(user_id, principal)
                elif path == '/api/chat/conversations' and method == 'GET':
                    result = service.conversations(user_id, principal)
                elif path == '/api/chat/conversations' and method == 'POST':
                    result = service.create_conversation(user_id, data, principal)
                    status = 201
                elif match := re.fullmatch(r'/api/chat/conversations/([a-f0-9]{32})(/messages)?', path):
                    if method == 'GET' and not match[2]:
                        result = service.conversation(user_id, match[1], principal)
                    elif method == 'POST' and match[2]:
                        result = service.send(user_id, match[1], data, principal)
                        status = 202
                    else:
                        raise ChatError('请求方法无效。', 405)
                elif not external and path == '/api/access-tokens' and method == 'GET':
                    result = service.tokens(user_id)
                elif not external and path == '/api/access-tokens' and method == 'POST':
                    result = service.create_token(user_id, data)
                    status = 201
                elif not external and (match := re.fullmatch(r'/api/access-tokens/([a-f0-9]{32})', path)) and method == 'DELETE':
                    result = service.revoke_token(user_id, match[1])
                else:
                    raise ChatError('接口不存在。', 404)
                return self.respond(result, status)
            except (ChatError, portal.Problem) as error:
                return self.respond({'error': error.message}, error.status)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                return self.respond({'error': '操作暂未完成，请查询原请求状态。'}, 500)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--portal-code', required=True)
    parser.add_argument('--port', type=int, default=8892)
    args = parser.parse_args()
    code = Path(args.portal_code).resolve()
    sys.path.insert(0, str(code))
    spec = importlib.util.spec_from_file_location('live_portal', code / 'server.py')
    portal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(portal)
    from native_transport import PortalNode
    service = Service(portal, PortalNode)
    portal.ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(portal, service)).serve_forever()


if __name__ == '__main__':
    main()

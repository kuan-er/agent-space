"""A protocol fixture, not a real Codex. Production round trips need separate live evidence."""
import base64
import hashlib
import json
import socketserver
import struct
import threading
import uuid


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.rfile.readline()
        headers = {}
        while True:
            line = self.rfile.readline()
            if line == b'\r\n' or not line:
                break
            k, v = line.split(b':', 1); headers[k.lower()] = v.strip()
        accept = base64.b64encode(hashlib.sha1(headers[b'sec-websocket-key'] + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
        self.wfile.write(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
        while True:
            head = self.rfile.read(2)
            if not head:
                return
            first, second = head
            n = second & 127
            if n == 126: n = struct.unpack('!H', self.rfile.read(2))[0]
            elif n == 127: n = struct.unpack('!Q', self.rfile.read(8))[0]
            mask = self.rfile.read(4)
            data = self.rfile.read(n)
            msg = json.loads(bytes(x ^ mask[i % 4] for i, x in enumerate(data)))
            if 'id' not in msg:
                continue
            try:
                response = {'id': msg['id'], 'result': self.server.fixture.rpc(msg['method'], msg.get('params', {}))}
            except Exception as e:
                response = {'id': msg['id'], 'error': {'code': -32600, 'message': str(e)}}
            data = json.dumps(response).encode()
            self.wfile.write(b'\x81' + (bytes([len(data)]) if len(data) < 126 else b'\x7e' + struct.pack('!H', len(data))) + data)


class FakeAppServer:
    def __init__(self, sid, cwd):
        self.sid, self.cwd = sid, str(cwd)
        self.calls = []
        self.turns = []
        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.fixture = self
        self.endpoint = 'ws://127.0.0.1:%d' % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)

    def rpc(self, method, params):
        self.calls.append({'method': method, 'params': params})
        if method == 'initialize':
            return {'userAgent': 'fixture'}
        if method == 'thread/loaded/list':
            return {'data': [self.sid]}
        if method in ('thread/read', 'thread/resume'):
            return {'thread': {'id': self.sid, 'cwd': self.cwd, 'status': {'type': 'idle'}}}
        if method == 'turn/start':
            turn = {'id': str(uuid.uuid4()), 'status': 'completed', 'items': [
                {'type': 'userMessage', 'id': 'u', 'clientId': params['clientUserMessageId'], 'content': params['input']},
                {'type': 'agentMessage', 'id': 'a', 'phase': 'final_answer', 'text': '已为你核对结果。'}]}
            self.turns.insert(0, turn)
            return {'turn': dict(turn, status='inProgress')}
        if method == 'thread/turns/list':
            return {'data': self.turns[:params.get('limit', 100)], 'nextCursor': None}
        raise ValueError('unknown method: ' + method)

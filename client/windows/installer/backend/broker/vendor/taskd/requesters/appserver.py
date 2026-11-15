"""Small bounded WebSocket JSON-RPC client for an EXISTING local Codex app server.

Never starts a second app server: its idle state would not describe the user's app.
Unix sockets and explicitly configured loopback ws URLs are supported. No credentials
or model output are logged by this transport.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import time
import urllib.parse
import uuid

LIMIT = 16 * 1024 * 1024


class RPCError(RuntimeError):
    pass


class RemoteRPCError(RPCError):
    """A well-formed error response for this exact request, not a transport fault."""


def rpc_result(message, request_id):
    """Decode only a matching response; malformed responses remain uncertain."""
    if not isinstance(message, dict) or message.get('id') != request_id:
        raise RPCError('RPC response does not match request')
    if 'error' in message:
        error = message['error']
        if ('result' in message or not isinstance(error, dict) or
                type(error.get('code')) is not int or not isinstance(error.get('message'), str)):
            raise RPCError('invalid RPC error response')
        raise RemoteRPCError(str(error)[:500])
    if 'result' not in message:
        raise RPCError('RPC response missing result')
    return message['result']


class AppServer:
    def __init__(self, endpoint, timeout=15, token_file=None):
        self.timeout = timeout
        self.sock = None
        self.buffer = bytearray()
        self.endpoint = str(endpoint)
        self.token_file = token_file

    def __enter__(self):
        endpoint = self.endpoint
        if endpoint.startswith('ws://'):
            u = urllib.parse.urlsplit(endpoint)
            if u.hostname not in ('127.0.0.1', '::1', 'localhost') or u.username or u.password:
                raise ValueError('Codex endpoint must be a local control socket or loopback ws URL')
            self.sock = socket.create_connection((u.hostname, u.port or 80), self.timeout)
            target, host = u.path or '/', u.netloc
        else:
            family = getattr(socket, 'AF_UNIX', None)
            if family is None:
                raise OSError('shared Unix control sockets are unavailable; configure a local codex_endpoint')
            self.sock = socket.socket(family, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            try:
                self.sock.connect(str(Path(endpoint.removeprefix('unix://'))))
            except BaseException:
                self.close()
                raise
            target, host = '/', 'localhost'
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            auth = ''
            if self.token_file:
                token = Path(self.token_file).read_text().strip()
                if not token or any(c.isspace() for c in token):
                    raise ValueError('invalid capability token file')
                auth = 'Authorization: Bearer ' + token + '\r\n'
            self.sock.sendall(('GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n'
                               'Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n'
                               'Sec-WebSocket-Version: 13\r\n%s\r\n' % (target, host, key, auth)).encode())
            while b'\r\n\r\n' not in self.buffer:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise EOFError('Codex closed handshake')
                self.buffer.extend(chunk)
                if len(self.buffer) > 65536:
                    raise RPCError('oversized WebSocket handshake')
            headers, rest = bytes(self.buffer).split(b'\r\n\r\n', 1)
            self.buffer = bytearray(rest)
            expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            fields = {k.lower(): v.strip() for line in headers.split(b'\r\n')[1:] if b':' in line for k, v in [line.split(b':', 1)]}
            if b' 101 ' not in headers.split(b'\r\n')[0] or fields.get(b'sec-websocket-accept') != expected:
                raise RPCError('Codex control socket rejected WebSocket handshake')
            self.rpc('initialize', {'clientInfo': {'name': 'taskd', 'version': '1.2.0'},
                                    'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized', 'params': {}})
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def __exit__(self, *args):
        self.close()

    def _read(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(min(65536, n - len(self.buffer)))
            if not chunk:
                raise EOFError('Codex control socket closed')
            self.buffer.extend(chunk)
        data = bytes(self.buffer[:n])
        del self.buffer[:n]
        return data

    def _frame(self, data, opcode=1):
        n = len(data)
        if n > LIMIT:
            raise RPCError('oversized WebSocket message')
        header = bytes([0x80 | opcode])
        header += bytes([0x80 | n]) if n < 126 else (b'\xfe' + struct.pack('!H', n) if n < 65536 else b'\xff' + struct.pack('!Q', n))
        mask = os.urandom(4)
        self.sock.sendall(header + mask + bytes(x ^ mask[i % 4] for i, x in enumerate(data)))

    def send(self, obj):
        self._frame(json.dumps(obj, ensure_ascii=False).encode())

    def receive(self):
        chunks = bytearray()
        while True:
            first, second = self._read(2)
            opcode, n = first & 15, second & 127
            if n == 126:
                n = struct.unpack('!H', self._read(2))[0]
            elif n == 127:
                n = struct.unpack('!Q', self._read(8))[0]
            if n + len(chunks) > LIMIT or second & 128 or first & 0x70:
                raise RPCError('invalid or oversized server frame')
            data = self._read(n)
            if opcode == 8:
                raise EOFError('Codex closed WebSocket')
            if opcode == 9:
                self._frame(data, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise RPCError('unexpected WebSocket opcode')
            chunks.extend(data)
            if first & 128:
                return json.loads(chunks)

    def rpc(self, method, params, request_id=None):
        rid = request_id or str(uuid.uuid4())
        self.send({'id': rid, 'method': method, 'params': params})
        end = time.monotonic() + self.timeout
        while time.monotonic() < end:
            self.sock.settimeout(max(0.1, end - time.monotonic()))
            msg = self.receive()
            if not isinstance(msg, dict):
                raise RPCError('invalid RPC message')
            if msg.get('id') == rid:
                return rpc_result(msg, rid)
            # This client never authorizes tool execution on behalf of the user.
            if msg.get('method') and 'id' in msg:
                self.send({'id': msg['id'], 'error': {'code': -32601, 'message': 'taskd cannot approve requests'}})
        raise TimeoutError('Codex did not acknowledge ' + method)

    def pages(self, method, params, limit=100):
        cursor = None
        for _ in range(limit):
            out = self.rpc(method, dict(params, cursor=cursor, limit=100))
            yield from out['data']
            cursor = out.get('nextCursor')
            if not cursor:
                return
        raise RPCError('Codex pagination limit exceeded')

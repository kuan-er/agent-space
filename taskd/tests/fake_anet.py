#!/usr/bin/env python3
"""A stand-in for the anet CLI and its MCP server, enough for taskd: several nodes (ANET_DATA_DIR)
share one network file (FAKE_ANET_NET). It follows the 0.3.9 rules taskd relies on: reply_task
states (tasks_api.go replyTask), list_tasks roles, the task view's metadata."""
import base64
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import secrets
import sys
import time

DATA = Path(os.environ['ANET_DATA_DIR'])
NET = Path(os.environ['FAKE_ANET_NET'])


def identity():
    p = DATA / 'fake_identity.json'
    if not p.exists():
        p.write_text(json.dumps({'aid': 'b' + secrets.token_hex(20), 'name': DATA.name}))
    return json.loads(p.read_text())


ME = identity()['aid']


class Net:
    def __enter__(self):
        self.lock = open(str(NET) + '.lock', 'a')
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        self.data = json.loads(NET.read_text()) if NET.exists() else {'tasks': {}}
        return self.data

    def __exit__(self, *exc):
        if not exc[0]:
            tmp = NET.with_name(NET.name + '.tmp')
            tmp.write_text(json.dumps(self.data))
            os.replace(tmp, NET)
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()


def now():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def view(t, history=None):
    role = 'requester' if t['requester'] == ME else 'provider'
    hist = t['history'] if history is None else (t['history'][-history:] if history else [])
    return {'id': t['id'], 'contextId': 'ctx-' + t['id'],
            'status': {'state': 'TASK_STATE_' + t['state'].upper().replace('-', '_'),
                       'message': t['history'][-1] if t.get('status_is_message') and t['history'] else None,
                       'timestamp': t['updated']},
            'history': hist,
            'metadata': {'anet.role': role, 'anet.peer_aid': t['provider'] if role == 'requester' else t['requester'],
                         'anet.state_seq': t['seq']}}


def message(role, text, files=()):
    parts = [{'text': text}] if text else []
    for f in files or []:
        parts.append({'raw': f['content_base64'], 'filename': f['name'], 'mediaType': f.get('media_type')})
    return {'messageId': 'm' + secrets.token_hex(6), 'role': role, 'parts': parts}


def touch(t, state=None):
    if state:
        t['state'] = state
    t['seq'] += 1
    t['updated'] = now()


TERMINAL = {'completed', 'failed', 'canceled', 'rejected'}


def tool(name, a):
    with Net() as net:
        tasks = net['tasks']
        if name == 'node_status':
            return {'aid': ME}
        if name == 'list_tasks':
            role = a.get('role')
            out = []
            for t in sorted(tasks.values(), key=lambda t: t['updated'], reverse=True):
                mine = t['requester'] == ME or t['provider'] == ME
                if not mine:
                    continue
                if role == 'requester' and t['requester'] != ME or role == 'provider' and t['provider'] != ME:
                    continue
                out.append(view(t, a.get('history_length', 1)))
            return {'tasks': out[:a.get('page_size', 50)]}
        if name in ('get_task', 'wait_task'):
            t = tasks[a['task_id']]
            return view(t, a.get('history_length'))
        if name == 'send_message':
            if a.get('to'):
                tid = 'ix_' + secrets.token_hex(8)
                tasks[tid] = {'id': tid, 'requester': ME, 'provider': a['to'], 'state': 'submitted', 'history': [],
                              'seq': 0, 'updated': now()}
            else:
                tid = a['task_id']
            t = tasks[tid]
            if t['requester'] != ME:
                raise ValueError('not a task this node sent')
            if t['state'] in TERMINAL:
                raise ValueError('task ended')
            t['history'].append(message('ROLE_USER', a.get('text', ''), a.get('files')))
            t['status_is_message'] = True
            touch(t, 'working' if t['state'] == 'input-required' else None)
            return view(t, 0)
        if name == 'reply_task':
            t = tasks[a['task_id']]
            if t['provider'] != ME:
                raise ValueError('not a task sent to this node')
            if t['state'] in TERMINAL:
                raise ValueError('task %s is %s' % (t['id'], t['state']))
            state, text, files = a.get('state', 'input-required'), a.get('text'), a.get('files')
            if state == 'input-required' and not (text or files):
                raise ValueError('a reply that asks for input needs text or files')
            if state not in ('working', 'input-required', 'completed', 'failed', 'rejected', 'canceled'):
                raise ValueError('bad state')
            if state == 'working' and not (text or files):
                t['status_is_message'] = False
                touch(t, 'working')
                return view(t, 0)
            if state in ('failed', 'rejected'):
                files = None  # SendStatus carries text only
            if state != 'canceled' and (text or files):
                t['history'].append(message('ROLE_AGENT', text, files))
                t['status_is_message'] = True
            touch(t, state)
            return view(t, 0)
    raise ValueError('unknown tool ' + name)


def mcp():
    tools = ['node_status', 'list_tasks', 'get_task', 'wait_task', 'send_message', 'reply_task']
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        if 'id' not in req:
            continue
        try:
            if req['method'] == 'initialize':
                result = {'protocolVersion': '2024-11-05', 'capabilities': {}, 'serverInfo': {'name': 'fake-anet'}}
            elif req['method'] == 'tools/list':
                result = {'tools': [{'name': n} for n in tools]}
            elif req['method'] == 'tools/call':
                try:
                    out = tool(req['params']['name'], req['params'].get('arguments', {}))
                    result = {'content': [{'type': 'text', 'text': json.dumps(out)}], 'structuredContent': out}
                except (ValueError, KeyError) as e:
                    result = {'content': [{'type': 'text', 'text': str(e)}], 'isError': True}
            else:
                raise ValueError('unknown method')
            resp = {'jsonrpc': '2.0', 'id': req['id'], 'result': result}
        except Exception as e:
            resp = {'jsonrpc': '2.0', 'id': req['id'], 'error': {'code': -1, 'message': str(e)}}
        sys.stdout.write(json.dumps(resp) + '\n')
        sys.stdout.flush()


def main(argv):
    cfg_path = DATA / 'config.json'
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    cmd = argv[0]
    if cmd == 'mcp':
        return mcp()
    if cmd == 'status':
        print(json.dumps({'aid': ME, 'name': identity()['name'], 'hub': {'state': 'registered'},
                          'inbound_policy': 'closed', 'auto_reply': cfg.get('auto_reply')}))
    elif cmd in ('up', 'stop'):
        log = DATA / 'fake_calls.log'
        with open(log, 'a') as f:
            f.write(cmd + '\n')
        print('ok')
    elif cmd == 'autoreply' and argv[1] == 'off':
        cfg.pop('auto_reply', None)
        cfg_path.write_text(json.dumps(cfg))
        print('{}')
    elif cmd == 'task' and argv[1] == 'get':
        history = int(argv[argv.index('--history') + 1]) if '--history' in argv else None
        print(json.dumps(tool('get_task', {'task_id': argv[2], 'history_length': history})))
    elif cmd == 'pull':
        out = Path(argv[argv.index('--out') + 1])
        out.mkdir(parents=True, exist_ok=True)
        t = tool('get_task', {'task_id': argv[1]})
        mine = 'ROLE_USER' if t['metadata']['anet.role'] == 'requester' else 'ROLE_AGENT'
        saved = []
        for m in t['history']:
            if m['role'] == mine:
                continue
            for p in m['parts']:
                if 'raw' in p:
                    (out / p['filename']).write_bytes(base64.b64decode(p['raw']))
                    saved.append(p['filename'])
        print(json.dumps({'saved': saved}))
    else:
        print('unsupported: ' + ' '.join(argv), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]) or 0)

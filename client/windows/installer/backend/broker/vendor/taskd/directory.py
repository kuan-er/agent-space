"""Owner-scoped Agent Space discovery for private nodes that the public hub cannot list."""
from __future__ import annotations

import json
from urllib.request import Request, HTTPRedirectHandler, build_opener

from .store import read_json


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise ValueError('directory redirect refused')


def lookup(data_dir, query=''):
    connection = read_json(data_dir / 'portal' / 'connection.json', {})
    base = connection.get('portal', '')
    if not (base.startswith('https://') or base.startswith('http://127.0.0.1:')):
        raise ValueError('an Agent Space HTTPS connection is required')
    credential = connection.get('node_token')
    if not credential:
        raise ValueError('this node has no Agent Space connection')
    request = Request(base.rstrip('/') + '/api/node/directory', data=b'{}',
                      headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + credential})
    with build_opener(NoRedirect()).open(request, timeout=10) as response:
        result = json.load(response)
    needle = query.strip().casefold()
    result['agents'] = [agent for agent in result.get('agents', []) if not needle or needle in
                        ' '.join(str(agent.get(k) or '') for k in ('name','description','aid','executor')).casefold()]
    return result

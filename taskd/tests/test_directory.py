import http.server
import json
from pathlib import Path
import tempfile
import threading
import unittest

from helpers import ROOT
from taskd.directory import lookup
from taskd.store import atomic_json


class DirectoryTests(unittest.TestCase):
    def test_owner_scoped_lookup_filters_locally_and_refuses_credential_redirect(self):
        requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                requests.append((self.path, self.headers.get('Authorization'), self.rfile.read(int(self.headers['Content-Length']))))
                if self.server.redirect:
                    self.send_response(307); self.send_header('Location','/credential-leak'); self.end_headers(); return
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({'source':'agent-space','agents':[
                    {'name':'EPS agent','aid':'eps-id','status':'ready','capabilities':{'concurrency':2}},
                    {'name':'another','aid':'other','status':'offline'}]}).encode())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'portal').mkdir()
            server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
            server.redirect=False
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                atomic_json(root/'portal/connection.json',{'portal':'http://127.0.0.1:'+str(server.server_port),'node_token':'test-credential'})
                result=lookup(root,'eps')
                self.assertEqual([a['aid'] for a in result['agents']],['eps-id'])
                self.assertEqual(requests[0],('/api/node/directory','Bearer test-credential',b'{}'))
                server.redirect=True
                with self.assertRaises(ValueError): lookup(root)
                self.assertEqual(len(requests),2)
            finally:
                server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()

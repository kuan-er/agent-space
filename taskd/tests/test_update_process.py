"""A real daemon process updates itself from a signed channel and replaces itself in place (a bare process,
no supervisor), then runs the new code. The channel is signed with a test key the wrapper trusts; the new
process is the installed program as released, so it trusts only the embedded key."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, tmpdir, wait_for

sys.path.insert(0, str(Path(__file__).resolve().parent))
from taskd.service import VERSION  # noqa: E402
from test_update import NEW, PUB, channel, Fixture  # noqa: E402
from test_e2e import script  # noqa: E402

HERE = Path(__file__).resolve().parent


@unittest.skipIf(sys.platform == 'win32', 'the simulated ANet uses fcntl; Windows is covered by the decision tests')
class ProcessTests(Fixture):
    def test_serve_updates_itself_and_execs_the_new_program(self):
        node = Path(tempfile.mkdtemp(dir=self.tmp.name)) / 'node'
        node.mkdir()
        bin_dir = Path(self.tmp.name) / 'bin'
        bin_dir.mkdir()
        anet = script(bin_dir / 'anet', HERE / 'fake_anet.py')
        env = {k: v for k, v in os.environ.items() if k not in ('TASKD_SUPERVISED', 'INVOCATION_ID', 'TASKD_RESTARTED')}
        env.update(PATH=str(bin_dir) + os.pathsep + env['PATH'], FAKE_ANET_NET=str(Path(self.tmp.name) / 'net.json'), ANET_DATA_DIR=str(node),
                   XPC_SERVICE_NAME='0')
        base = [sys.executable, str(ROOT / 'run_taskd.py'), '--data-dir', str(node), '--anet', str(anet)]
        r = subprocess.run(base + ['install', '--no-requester', '--no-autostart'], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg_path = node / 'taskd' / 'config.json'
        cfg = json.loads(cfg_path.read_text())
        cfg.update(update_portal=self.url, intervals={'status': 0.5, 'guard': 3600})
        cfg_path.write_text(json.dumps(cfg))
        app = node / 'taskd' / 'app'
        wrapper = Path(self.tmp.name) / 'wrapper.py'
        wrapper.write_text("import sys\nsys.path.insert(0, %r)\nfrom taskd import update\nupdate.UPDATE_KEY = bytes.fromhex(%r)\n"
                           "update.random.randint = lambda a, b: 1\nfrom taskd.cli import main\nsys.exit(main(sys.argv[1:]))\n" % (str(app), PUB.hex()))
        self.files.update(channel())
        err = open(Path(self.tmp.name) / 'serve.err', 'w')
        self.addCleanup(err.close)
        p = subprocess.Popen([sys.executable, str(wrapper), '--data-dir', str(node), '--anet', str(anet), 'serve'], env=env,
                             stdout=subprocess.DEVNULL, stderr=err)
        try:
            status = node / 'taskd' / 'status.json'

            def current():
                try:
                    return json.loads(status.read_text())
                except (OSError, ValueError):
                    return {}
            self.assertTrue(wait_for(lambda: current().get('version') == NEW, timeout=40), (Path(self.tmp.name) / 'serve.err').read_text())
            st = current()
            self.assertEqual(st['pid'], p.pid)            # replaced in place, not a new process
            self.assertEqual(st['update_state'], 'updated:' + NEW)
            self.assertTrue(st['auto_update'])
            self.assertIsNone(p.poll())                   # still running: execv, not exit
            pending = json.loads((node / 'taskd' / 'updates' / 'pending.json').read_text())
            self.assertEqual((pending['version'], pending['tries']), (NEW, 1))
            self.assertEqual(json.loads((node / 'taskd' / 'updates' / 'state.json').read_text())['serial'], 100)
            self.assertIn("VERSION = '%s'" % NEW, (app / 'taskd' / 'service.py').read_text())
            self.assertTrue((node / 'taskd' / 'updates' / 'backup' / VERSION / 'run_taskd.py').exists())
        finally:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(15)
                except subprocess.TimeoutExpired:
                    p.kill()


if __name__ == '__main__':
    unittest.main()

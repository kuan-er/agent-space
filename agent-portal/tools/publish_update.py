#!/usr/bin/env python3
"""Operator tool: sign a connector release for the Agent Space update channel.

  publish_update.py init            create the signing key (once, kept outside the repo)
  publish_update.py build OUT_DIR [--taskd taskd-<version>.zip]
                                    write manifest.json, manifest.sig and connector-<version>/connector.py;
                                    with --taskd, also taskd-<version>/taskd-<version>.zip (taskd/build.py's output)

The private key never leaves this machine. Nodes trust only UPDATE_KEY embedded in connector.py.
"""
import hashlib, importlib.util, json, os, re, shutil, sys, time, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY = Path(os.environ.get('AGENT_SPACE_UPDATE_KEY', Path.home() / '.agent-space-keys' / 'update-ed25519.key'))

def load_connector():
    spec = importlib.util.spec_from_file_location('connector_for_signing', ROOT / 'connector.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def taskd_component(zip_path, out):
    """The manifest entry for a taskd release zip; checks the zip is what the nodes will accept."""
    m = re.fullmatch(r'taskd-(\d+\.\d+\.\d+)\.zip', zip_path.name)
    if not m: sys.exit('expected a file named taskd-<version>.zip')
    version, data = m.group(1), zip_path.read_bytes()
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if not all(n.startswith('taskd-%s/' % version) for n in names): sys.exit('zip entries must all live under taskd-%s/' % version)
        service = z.read('taskd-%s/taskd/service.py' % version).decode()
        if "VERSION = '%s'" % version not in service: sys.exit('taskd/service.py VERSION does not match the zip name')
        if 'taskd-%s/taskd/update.py' % version not in names: sys.exit('this taskd build has no updater; nodes could never update again')
    (out / ('taskd-' + version)).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(zip_path, out / ('taskd-' + version) / zip_path.name)
    return {'version': version, 'file': zip_path.name, 'sha256': hashlib.sha256(data).hexdigest()}

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    c = load_connector()
    if cmd == 'init':
        if KEY.exists(): sys.exit('key already exists: %s' % KEY)
        KEY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f: f.write(os.urandom(32).hex() + '\n')
        print('public key:', c.ed25519_public(bytes.fromhex(KEY.read_text().strip())).hex())
    elif cmd == 'build' and len(sys.argv) in (3, 5) and (len(sys.argv) == 3 or sys.argv[3] == '--taskd'):
        secret = bytes.fromhex(KEY.read_text().strip())
        if c.ed25519_public(secret) != c.UPDATE_KEY: sys.exit('connector.py UPDATE_KEY does not match the signing key')
        code = (ROOT / 'connector.py').read_bytes(); version = c.VERSION
        assert re.fullmatch(r'\d+\.\d+\.\d+', version)
        out = Path(sys.argv[2]); (out / ('connector-' + version)).mkdir(parents=True, exist_ok=True)
        (out / ('connector-' + version) / 'connector.py').write_bytes(code)
        components = {'connector': {'version': version, 'files': {'connector.py': hashlib.sha256(code).hexdigest()}}}
        if len(sys.argv) == 5:
            components['taskd'] = taskd_component(Path(sys.argv[4]), out)
        manifest = json.dumps({'schema': 1, 'serial': int(time.time()), 'issued': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                               'components': components}, sort_keys=True, separators=(',', ':')).encode()
        import base64
        (out / 'manifest.json').write_bytes(manifest)
        (out / 'manifest.sig').write_text(base64.b64encode(c.ed25519_sign(secret, manifest)).decode() + '\n')
        assert c.ed25519_verify(c.UPDATE_KEY, manifest, base64.b64decode((out / 'manifest.sig').read_text()))
        print('signed connector', version, 'sha256', hashlib.sha256(code).hexdigest())
        if 'taskd' in components: print('signed taskd', components['taskd']['version'], 'sha256', components['taskd']['sha256'])
    else:
        sys.exit(__doc__)

if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Build dist/taskd-<version>.zip (the program only: no tests, no caches) and its SHA256."""
import hashlib
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent


def main():
    version = next(line.split("'")[1] for line in (ROOT / 'taskd' / 'service.py').read_text().splitlines()
                   if line.startswith('VERSION'))
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    out = dist / ('taskd-%s.zip' % version)
    files = sorted(p for p in (ROOT / 'taskd').rglob('*.py')) + [ROOT / 'run_taskd.py', ROOT / 'README.md', ROOT / 'LICENSE', ROOT / 'NOTICE']
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in files:
            info = zipfile.ZipInfo(str(Path('taskd-' + version) / p.relative_to(ROOT)), (2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            z.writestr(info, p.read_bytes(), zipfile.ZIP_DEFLATED)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (dist / (out.name + '.sha256')).write_text('%s  %s\n' % (digest, out.name))
    print(out, digest)


if __name__ == '__main__':
    main()

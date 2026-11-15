"""Fixed Python release closure shared by startup, registry and installer."""
import hashlib
import json
from pathlib import Path

SCHEMA='anet-harness-release-closure/2'
PACKAGES=('anet_harnesses','anet_provider_harnesses')

def ordinary(path):
    return not path.is_symlink() and not (getattr(path.lstat(),'st_file_attributes',0)&0x400)


def closure_map():
    # No caller path, plugin discovery, user-configured package or imported code.
    app=Path(__file__).resolve().parents[1]
    result={}
    for name in PACKAGES:
        root=app/name
        if not root.is_dir() or not ordinary(root) or not (root/'__init__.py').is_file():
            raise ValueError('fixed harness release package missing')
        for path in sorted(root.rglob('*.py')):
            if not ordinary(path) or not path.is_file() or any(not ordinary(parent) for parent in path.parents if parent.is_relative_to(app)):
                raise ValueError('harness release must contain ordinary files')
            result[name+'/'+path.relative_to(root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def package_digest():
    return hashlib.sha256(json.dumps(closure_map(),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()

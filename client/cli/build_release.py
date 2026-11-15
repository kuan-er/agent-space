"""Build the generic macOS/Linux client from the repository sources."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import tempfile

from anet_setup import __version__

ROOT = Path(__file__).resolve().parent
PACKAGES = (
    "anet_account", "anet_harnesses", "anet_provider_harnesses", "anet_setup",
    "taskd_app", "yaml",
)


def build(output):
    output.mkdir(parents=True, exist_ok=True)
    name = f"anet-config-{__version__}"
    with tempfile.TemporaryDirectory() as temporary:
        stage = Path(temporary) / name
        stage.mkdir()
        sources = [ROOT / filename for filename in ("main.py", "install.py", "install.sh", "README.txt")]
        for package in PACKAGES:
            sources.extend(path for path in (ROOT / package).rglob("*.py") if "__pycache__" not in path.parts)
        for source in sorted(sources):
            target = stage / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        for notice in ("LICENSE", "NOTICE", "licenses/PyYAML.txt"):
            target = stage / notice
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT.parents[1] / notice, target)
        manifest = []
        for path in sorted(p for p in stage.rglob("*") if p.is_file()):
            data = path.read_bytes()
            manifest.append({"path": path.relative_to(stage).as_posix(), "bytes": len(data),
                             "sha256": hashlib.sha256(data).hexdigest()})
        (stage / "manifest.json").write_text(json.dumps({"version": __version__, "files": manifest}, indent=2) + "\n", encoding="utf-8")
        archive = output / f"{name}-macos-linux.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            stream.add(stage, arcname=name)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    print(build(parser.parse_args().output))

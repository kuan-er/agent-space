"""Exact prepared receiver worker launched by the configuration backend."""
import sys
from pathlib import Path
package = Path(__file__).resolve().parent
# Direct script startup adds this directory, whose http.py would otherwise
# shadow Python's standard-library http package during urllib imports.
sys.path[:] = [entry for entry in sys.path if Path(entry or '.').resolve() != package]
sys.path.insert(0, str(package.parent))
from anet_harnesses.receiver_config import run_worker
raise SystemExit(run_worker(Path(sys.argv[1])))

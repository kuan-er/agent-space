#!/usr/bin/env python3
"""Run the existing two-daemon integration scenario without real model or Hub access."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
print('Local demo: two real taskd daemons; simulated ANet, executor and source-session API.', flush=True)
print('Checks: submit -> execute -> acceptance -> artifact hashes -> return to source session.', flush=True)
raise SystemExit(subprocess.call([
    sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_e2e.py', '-k', 'test_long_task_round_trip', '-v'
], cwd=root / 'taskd'))

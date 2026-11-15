"""Restore normal script-local imports in this isolated embedded distribution.

The embedded runtime keeps its explicit standard library/site-packages search
path and ignores PYTHONPATH. Only a real entry script's own directory is added,
matching ordinary CPython script invocation; -c/-m never add the working dir.
"""
import os,sys
if sys.argv and sys.argv[0] not in ('','-c','-m'):
    _entry=os.path.abspath(sys.argv[0])
    if os.path.isfile(_entry):sys.path.insert(0,os.path.dirname(_entry))

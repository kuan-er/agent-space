#!/usr/bin/python3
"""Root-owned narrow sudo helper. Only admits/revokes validated AIDs, never arbitrary shell."""
import re, subprocess, sys
if len(sys.argv)!=3 or sys.argv[1] not in ['admit','revoke'] or not re.fullmatch(r'b[a-z2-7]{40,100}',sys.argv[2]):
    sys.exit(2)
base=['/usr/sbin/runuser','-u','anet-hub','--','/opt/anet-hub/bin/anet-hub','--data','/var/lib/anet-hub']
args=['admit',sys.argv[2],'--label','agent-space'] if sys.argv[1]=='admit' else ['admissions','revoke',sys.argv[2]]
sys.exit(subprocess.run(base+args,timeout=12).returncode)

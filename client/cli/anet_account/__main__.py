import argparse
import json
import sys
from pathlib import Path
from . import execute

parser=argparse.ArgumentParser(description='ANet account configuration (JSON stdin)')
parser.add_argument('--runtime',required=True)
args=parser.parse_args()
try:
    raw=sys.stdin.buffer.readline(262145)
    if len(raw)>262144:raise ValueError('request too large')
    request=json.loads(raw)
    context=json.loads(Path(args.runtime).read_text(encoding='utf-8-sig'))
    result=execute(request,context)
except Exception:
    result={'schema':'anet-account/1','state':'error','code':'request_invalid','message':'配置请求无效，请重新打开设置。'}
print(json.dumps(result,ensure_ascii=False),flush=True)

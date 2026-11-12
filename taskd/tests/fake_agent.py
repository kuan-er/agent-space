#!/usr/bin/env python3
"""A scripted executor for the end-to-end test (command adapter: prompt on stdin, answer on stdout)."""
import json
from pathlib import Path
import sys

prompt = sys.stdin.read()
if 'Reply with exactly: OK' in prompt:
    print('OK')
elif 'Right now your job is only to write the plan' in prompt:
    print(json.dumps({'phases': [{'id': 'p1', 'title': '生成结果', 'done_when': 'out/result.txt 存在', 'checks': ['result']}],
                      'drafted_checks': [], 'approval_reasons': [], 'notes': ''}, ensure_ascii=False))
else:
    out = Path('out')
    out.mkdir(exist_ok=True)
    (out / 'result.txt').write_text('42\n')
    print(json.dumps({'phase': 'p1', 'status': 'claims_done', 'summary': '已生成 out/result.txt',
                      'attempts': [{'what': '计算并写出结果', 'outcome': 'worked', 'observed': '文件已写出', 'evidence': ['out/result.txt']}],
                      'facts': [{'statement': '结果是 42', 'basis': 'observed', 'evidence': ['out/result.txt']}],
                      'artifacts': ['out/result.txt'], 'next': '无', 'blocker': None, 'question': None}, ensure_ascii=False))

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anet_setup import __main__ as cli
from anet_setup import native


class CliTests(unittest.TestCase):
    def test_human_status_is_chinese_while_json_stays_machine_readable(self):
        result = {'state': 'testing', 'agents': [{'name': 'Codex', 'state': 'not_configured', 'detail': '请先连接 ANet。'}, {'name': 'Claude Code', 'state': 'not_configured'}]}
        result['binary_resolution'] = {'binary': '/private/internal/anet', 'selection': 'maintenance_verified', 'verification_ms': 177}
        with contextlib.redirect_stdout(io.StringIO()) as human:
            cli.display(result)
        self.assertIn('正在自检', human.getvalue())
        self.assertIn('Codex · 请先连接 ANet。', human.getvalue())
        self.assertIn('Claude Code · 尚未配置', human.getvalue())
        self.assertNotIn('not_configured', human.getvalue())
        self.assertNotIn('/private/internal/anet', human.getvalue())
        self.assertNotIn('maintenance_verified', human.getvalue())
        self.assertNotIn('177', human.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as machine:
            cli.display(result, True)
        self.assertEqual(json.loads(machine.getvalue()), result)

    def test_nested_failure_sets_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(cli, 'execute', return_value={'state': 'error', 'message': 'failed'}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--data-dir', tmp, '--json', 'status']), 1)

    def test_no_password_argument(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.parser().parse_args(['login', '--password', 'do-not-store-me'])

    def test_rpc_runtime_after_subcommand(self):
        args = cli.parser().parse_args(['rpc', '--runtime', 'a.json'])
        self.assertEqual(args.runtime, Path('a.json'))

    def test_configure_needs_harness(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--data-dir', tmp, 'agents', 'connect']), 1)

    def test_preserve_existing_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / 'operator'
            executable.write_bytes(b'operator')
            with patch.object(native, 'download', side_effect=AssertionError('must not download')):
                result = native.install(root, explicit=executable)
            self.assertEqual(result['state'], 'reused')
            self.assertEqual(executable.read_bytes(), b'operator')

    def test_missing_binary_existing_identity_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'config.json').write_text('{}')
            with patch.object(native, 'discover', return_value=None):
                with self.assertRaisesRegex(ValueError, '已有身份'):
                    native.install(root)

    def test_signature_failure_has_no_install_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'node'
            class Result:
                returncode = 1
            with patch.object(native, 'discover', return_value=None), patch.object(native.platform, 'system', return_value='Linux'), patch.object(native.platform, 'machine', return_value='x86_64'), patch.object(native.shutil, 'which', return_value='/usr/bin/ssh-keygen'), patch.object(native, 'download', side_effect=[b'{}', b'invalid']), patch.object(native.subprocess, 'run', return_value=Result()):
                with self.assertRaisesRegex(ValueError, '签名验证失败'):
                    native.install(root)
            self.assertFalse(root.exists())

    def test_no_task_routes(self):
        from anet_setup.backend import execute
        for action in ('task.submit', 'config.submit', 'account.pair', 'config.shell'):
            with self.assertRaises(ValueError):
                execute({'action': action}, {})
    def test_receiver_prepare_does_not_reauthenticate_or_enable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(cli,'execute',return_value={'state':'ready','plan_id':'prepared-id'}) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--data-dir',tmp,'receiver','prepare','--harness','codex','--json']),0)
            self.assertEqual(run.call_count,1)
            self.assertEqual(run.call_args.args[0],{'action':'config.receiver','operation':'prepare','harness':'codex'})
    def test_receiver_enable_prepares_once_then_clears_password(self):
        observed=[];references=[]
        def run(request,context):
            observed.append(dict(request));references.append(request)
            return {'state':'ready','plan_id':'prepared-id'} if len(observed)==1 else {'state':'ready','receiver':{'state':'testing'}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(cli,'execute',side_effect=run),patch.object(cli.sys,'stdin',io.StringIO('fixture-password\n')):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(['--data-dir',tmp,'receiver','enable','--harness','codex','--username','owner','--password-stdin','--json']),0)
            self.assertEqual(len(observed),2);self.assertEqual(observed[1]['action'],'account.enable_receiver')
            self.assertTrue(observed[1]['confirm']);self.assertEqual(observed[1]['plan_id'],'prepared-id')
            self.assertEqual(references[1]['password'],'');self.assertNotIn('fixture-password',output.getvalue())


if __name__ == '__main__':
    unittest.main()

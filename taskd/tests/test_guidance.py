import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest import mock

from taskd.cli import parser, wire_requesters, Node
from taskd.requesters.codex import CodexRequester
from taskd.requesters import guidance


class GuidanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.adapter = CodexRequester(codex_home=self.home)
        self.root = self.home / 'node with spaces'
        self.runner = self.root / 'taskd/app/run_taskd.py'

    def test_preserves_user_and_transport_guidance_and_is_idempotent(self):
        original = '用户自己的说明。\n<!-- anet:begin -->\nANet guidance\n<!-- anet:end -->\n'
        p = self.home / 'AGENTS.md'; p.write_text(original)
        self.adapter.wire_guidance(self.root, self.runner, '/python path/python3')
        text = p.read_text(); self.assertTrue(text.startswith(original))
        self.assertIn('"taskd":"submit"', text)
        self.assertIn('"taskd":"capabilities"', text)
        self.assertGreater(text.index('<!-- taskd:'), text.index('<!-- anet:end -->'))
        stamp = p.stat().st_mtime_ns
        self.adapter.wire_guidance(self.root, self.runner, '/python path/python3')
        self.assertEqual(p.stat().st_mtime_ns, stamp)
        self.assertEqual(len(list(self.home.glob('AGENTS.md.taskd-bak-*'))), 1)
        self.assertEqual(next(self.home.glob('AGENTS.md.taskd-bak-*')).read_text(), original)
        self.adapter.wire_guidance(self.root, self.runner, '/python path/python3', remove=True)
        self.assertTrue(p.read_text().startswith(original)); self.assertNotIn('<!-- taskd:', p.read_text())

    def test_each_node_has_its_own_block(self):
        p = self.adapter.wire_guidance(self.root, self.runner, '/python3')
        other = self.home / 'other'
        self.adapter.wire_guidance(other, other/'run_taskd.py', '/python3')
        self.adapter.wire_guidance(self.root, self.runner, '/python3', remove=True)
        self.assertEqual(p.read_text().count(':begin -->'), 1)
        self.assertIn(str(other), p.read_text())

    def test_existing_override_is_respected(self):
        p = self.home / 'AGENTS.override.md'; p.write_text('override\n')
        result = self.adapter.wire_guidance(self.root, self.runner, '/python3')
        self.assertEqual(result, p.resolve())
        self.assertFalse((self.home/'AGENTS.md').exists())

    def test_malformed_markers_leave_the_original_untouched(self):
        p = self.adapter.wire_guidance(self.root, self.runner, '/python3')
        text = p.read_text().replace(':end -->', ':broken -->'); p.write_text(text)
        with self.assertRaises(ValueError):self.adapter.wire_guidance(self.root, self.runner, '/python3')
        self.assertEqual(p.read_text(), text)

    def test_commands_parse_with_spaces_and_global_options_before_subcommand(self):
        with mock.patch('sys.platform', 'darwin'):
            words = shlex.split(guidance.command('/python path/python3', self.runner, self.root) + ' guide')
        args = parser().parse_args(words[2:])
        self.assertEqual(args.cmd, 'guide'); self.assertEqual(args.data_dir, self.root)
        with mock.patch('sys.platform', 'win32'):
            text = guidance.command("C:/O'Neil/python.exe", 'C:/app/run_taskd.py', 'C:/data dir')
        self.assertIn("& 'C:/O''Neil/python.exe'", text)

    def test_disabled_requester_does_not_write_guidance(self):
        self.assertEqual(wire_requesters(Node(self.root), {'requester': None}), [])
        self.assertFalse((self.home/'AGENTS.md').exists())


if __name__ == '__main__':
    unittest.main()

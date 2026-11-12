"""The core must not depend on any executor, platform, domain or transport (design §4)."""
import re
import unittest

from helpers import ROOT

CORE = ['protocol.py', 'store.py', 'engine.py', 'lightweight.py', 'prompts.py', 'messages.py', 'service.py', 'relay.py',
        'checkers/base.py', 'checkers/generic.py', 'executors/base.py']
BANNED = re.compile(r'codex|claude|cursor|\bpi\b|windows|win32|darwin|linux|macos|powershell|\buia\b|\beps\b|anet|workbuddy|deepseek', re.I)


class GeneralityTests(unittest.TestCase):
    def test_core_names_nothing_specific(self):
        for rel in CORE:
            text = (ROOT / 'taskd' / rel).read_text(encoding='utf-8')
            for n, line in enumerate(text.splitlines(), 1):
                m = BANNED.search(line)
                self.assertIsNone(m, '%s:%d names %r: %s' % (rel, n, m and m.group(0), line.strip()))

    def test_core_imports_no_plugin(self):
        for rel in CORE:
            lines = [l for l in (ROOT / 'taskd' / rel).read_text(encoding='utf-8').splitlines()
                     if l.startswith(('from ', 'import '))]
            for mod in ('executors.codex', 'executors.command', 'transports', 'requesters', 'probes', 'checkers.windows', 'builtin', 'platform.powershell'):
                for l in lines:
                    self.assertNotIn(mod, l, '%s imports %s' % (rel, mod))


if __name__ == '__main__':
    unittest.main()

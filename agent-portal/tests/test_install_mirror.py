import importlib.util
from pathlib import Path
import unittest


class MirrorOnboardingTest(unittest.TestCase):
    def test_every_onboarding_role_uses_mirror_and_preserves_existing_identity(self):
        spec = importlib.util.spec_from_file_location('mirror_portal', Path(__file__).parents[1] / 'server.py')
        portal = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(portal)
        portal.BASE = 'https://portal.example'
        for kind, executor in [('device', 'codex'), ('agent', 'codex'), ('agent', 'pi'),
                               ('agent', 'deepseek'), ('agent', 'workbuddy'), ('agent', 'custom')]:
            with self.subTest(kind=kind, executor=executor):
                text = portal.onboarding({'kind': kind, 'executor': executor, 'name': 'test'}, 'test-token')
                self.assertIn('https://portal.example/install/instructions.txt', text)
                self.assertIn('签名', text)
                self.assertIn('operator', text)
                self.assertNotIn('从 https://github.com/ANetResearch/ANet', text)
                if kind == 'device':
                    self.assertIn('不要开启 autoreply', text)


if __name__ == '__main__':
    unittest.main()

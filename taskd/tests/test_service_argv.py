"""The login launcher and the supervisor start the daemon with a command line the real parser accepts."""
import unittest
from pathlib import Path
from taskd import cli


class ServiceArgvTest(unittest.TestCase):
    def test_generated_command_lines_parse(self):
        for target in ('serve', 'supervise'):
            argv = cli.service_argv(Path('/x/run_taskd.py'), target, Path('/d'), '/bin/anet')
            args = cli.parser().parse_args(argv[2:])
            self.assertEqual((str(args.data_dir), args.anet), ('/d', '/bin/anet'))
            self.assertEqual(args.fn, {'serve': cli.cmd_serve, 'supervise': cli.cmd_supervise}[target])

    def test_anet_is_optional(self):
        argv = cli.service_argv(Path('/x/run_taskd.py'), 'serve', '/d')
        self.assertNotIn('--anet', argv)
        cli.parser().parse_args(argv[2:])


if __name__ == '__main__':
    unittest.main()

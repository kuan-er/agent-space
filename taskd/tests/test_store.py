import json
from pathlib import Path
import unittest
from unittest.mock import patch

from helpers import ROOT  # configure the package import path
from taskd.store import read_json


class StateReadTests(unittest.TestCase):
    @patch('taskd.store.time.sleep')
    def test_transient_windows_sharing_violation_retries_then_reads(self, sleep):
        with patch.object(Path, 'read_text', side_effect=[PermissionError('sharing violation'), '{"state":"running"}']) as read:
            self.assertEqual(read_json('task.json'), {'state': 'running'})
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once_with(0.05)

    @patch('taskd.store.time.sleep')
    def test_permanent_denial_is_not_hidden_as_missing_state(self, sleep):
        with patch.object(Path, 'read_text', side_effect=PermissionError('denied')) as read:
            with self.assertRaises(PermissionError):
                read_json('task.json', {})
        self.assertEqual(read.call_count, 20)
        self.assertEqual(sleep.call_count, 19)

    @patch('taskd.store.time.sleep')
    def test_missing_file_preserves_default_without_retry(self, sleep):
        with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
            self.assertEqual(read_json('task.json', {'missing': True}), {'missing': True})
        sleep.assert_not_called()

    @patch('taskd.store.time.sleep')
    def test_corrupt_json_is_not_hidden_by_retry(self, sleep):
        with patch.object(Path, 'read_text', return_value='{invalid'):
            with self.assertRaises(json.JSONDecodeError):
                read_json('task.json')
        sleep.assert_not_called()

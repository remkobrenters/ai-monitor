import json
from pathlib import Path
import tempfile
import unittest

from ai_monitor.codex_permissions import MAX_CONTEXT_BYTES, approval_reviewer


class CodexPermissionsTests(unittest.TestCase):
    def test_review_routing_uses_the_exact_turn_and_latest_context_without_returning_other_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = home / 'sessions/example.jsonl'
            path.parent.mkdir()

            def context(turn, reviewer):
                return json.dumps({'type': 'turn_context', 'payload': {
                    'turn_id': turn, 'approvals_reviewer': reviewer, 'developer_instructions': 'fixture-private'}}) + '\n'

            path.write_text(context('old', 'auto_review') + context('current', 'user'))
            self.assertEqual(approval_reviewer(str(path), 'current', home), 'user')
            self.assertIsNone(approval_reviewer(str(path), 'missing', home))
            with path.open('a') as stream:
                stream.write(context('current', 'auto_review'))
            self.assertEqual(approval_reviewer(str(path), 'current', home), 'auto_review')
            with path.open('a') as stream:
                stream.write(context('current', 'unsupported'))
            self.assertIsNone(approval_reviewer(str(path), 'current', home))

    def test_context_reads_are_bounded_and_do_not_follow_paths_outside_the_sessions_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = home / 'sessions/example.jsonl'
            path.parent.mkdir()
            context = json.dumps({'type': 'turn_context', 'payload': {
                'turn_id': 'current', 'approvals_reviewer': 'auto_review'}}) + '\n'
            path.write_text(context + 'x' * (MAX_CONTEXT_BYTES + 1))
            self.assertIsNone(approval_reviewer(str(path), 'current', home))
            outside = home / 'outside.jsonl'
            outside.write_text(context)
            self.assertIsNone(approval_reviewer(str(outside), 'current', home))
            path.unlink()
            path.symlink_to(outside)
            self.assertIsNone(approval_reviewer(str(path), 'current', home))
            self.assertIsNone(approval_reviewer(None, 'current', home))

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_monitor.hook import read_payload, report
from ai_monitor.terminal import discover


class HookTests(unittest.TestCase):
    def test_permission_hook_forwards_only_effective_review_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            transcript = home / 'sessions/example.jsonl'
            transcript.parent.mkdir()
            transcript.write_text(json.dumps({'type': 'turn_context', 'payload': {
                'turn_id': 'current', 'approvals_reviewer': 'auto_review',
                'developer_instructions': 'fixture-private-context'}}) + '\n')
            payload = {'session_id': 'parent', 'hook_event_name': 'PermissionRequest', 'turn_id': 'current',
                       'permission_mode': 'default', 'transcript_path': str(transcript),
                       'tool_name': 'Bash', 'tool_input': {'command': 'fixture-private-command'}}
            with patch.dict(os.environ, {'CODEX_HOME': str(home)}), \
                    patch('ai_monitor.hook.read_payload', return_value=payload), \
                    patch('ai_monitor.hook.capture', return_value={'process': {'pid': 1, 'start': '2'}}), \
                    patch('ai_monitor.hook.request') as request, patch('ai_monitor.hook.diagnostic'):
                report('codex')
                message = request.call_args.args[0]
                self.assertEqual(message['approvalsReviewer'], 'auto_review')
                self.assertEqual(message['permissionMode'], 'default')
                self.assertNotIn('fixture-private', json.dumps(message))

    def test_subagent_payload_forwards_identity_and_counts_without_task_contents(self):
        for tool, name in (('claude', 'SubagentStart'), ('codex', 'SubagentStop'), ('claude', 'Stop')):
            payload = {'session_id': 'parent', 'hook_event_name': name, 'agent_id': 'child',
                       'last_assistant_message': 'private answer', 'background_tasks': [
                           {'id': 'a', 'type': 'subagent', 'status': 'running', 'description': 'private task'},
                           {'id': 'b', 'type': 'shell', 'status': 'running', 'command': 'private command'},
                           {'id': 'c', 'type': 'subagent', 'status': 'completed'}]}
            with patch('ai_monitor.hook.read_payload', return_value=payload), \
                    patch('ai_monitor.hook.capture', return_value={'process': {'pid': 1, 'start': '2'}}), \
                    patch('ai_monitor.hook.request') as request, patch('ai_monitor.hook.diagnostic'):
                report(tool)
                message = request.call_args.args[0]
                self.assertEqual(message['session'], 'parent')
                if name == 'Stop':
                    self.assertEqual(message['backgroundSubagents'], 1)
                else:
                    self.assertEqual(message['agentId'], 'child')
                self.assertNotIn('private', json.dumps(message))

    def test_complete_json_is_read_even_while_runner_keeps_stdin_open(self):
        reader, writer = os.pipe()
        try:
            payload = {"session_id": "test", "hook_event_name": "Stop", "cwd": "/project"}
            # Deliberately no newline and no EOF. This used to wait for hook timeout.
            os.write(writer, json.dumps(payload).encode())
            with os.fdopen(reader, "rb", closefd=False) as stream:
                self.assertEqual(read_payload(stream), payload)
        finally:
            os.close(reader)
            os.close(writer)

    def test_incomplete_json_has_a_deadline(self):
        reader, writer = os.pipe()
        try:
            os.write(writer, b'{"unfinished":')
            with os.fdopen(reader, "rb", closefd=False) as stream:
                with self.assertRaisesRegex(ValueError, "Incomplete"):
                    read_payload(stream, timeout=0.01)
        finally:
            os.close(reader)
            os.close(writer)

    def test_daemon_events_are_forwarded_without_prompt_or_tool_arguments(self):
        payload = {"session_id": "session", "hook_event_name": "Stop", "cwd": "/project",
                   "prompt": "private", "tool_input": {"password": "secret"}}
        with patch('ai_monitor.hook.read_payload', return_value=payload), \
                patch('ai_monitor.hook.capture', return_value={'process': {'pid': 1, 'start': '2'}, 'tty': ''}), \
                patch('ai_monitor.hook.request') as request, patch('ai_monitor.hook.diagnostic') as diagnostic:
            report('codex')
            message = request.call_args.args[0]
            self.assertNotIn('private', json.dumps(message))
            self.assertNotIn('secret', json.dumps(message))
            self.assertEqual(message['event'], 'Stop')
            diagnostic.assert_called_with('codex', 'Stop', 'delivered')

    def test_absent_service_is_nonfatal_and_diagnosable(self):
        with patch('ai_monitor.hook.read_payload', return_value={'session_id': 'a', 'hook_event_name': 'Stop'}), \
                patch('ai_monitor.hook.capture', return_value={'process': {'pid': 2}, 'tty': ''}), \
                patch('ai_monitor.hook.request', side_effect=ConnectionRefusedError), \
                patch('ai_monitor.hook.diagnostic') as diagnostic:
            report('codex')
            diagnostic.assert_called_with('codex', 'Stop', 'unavailable')

    def test_real_terminal_client_discovery_excludes_app_server(self):
        try:
            master, slave = os.openpty()
        except PermissionError:
            self.skipTest('Sandbox prohibits PTY creation')
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        env = {key: value for key, value in os.environ.items()
               if key not in ('TMUX', 'TMUX_PANE', 'SSH_TTY', 'SSH_CONNECTION')}
        env.update(GNOME_TERMINAL_SERVICE=':1.42',
                   GNOME_TERMINAL_SCREEN='/org/gnome/Terminal/screen/12345678_abcd_abcd_abcd_123456789abc')
        with tempfile.TemporaryDirectory() as temporary:
            script = "import time;print('ready',flush=True);time.sleep(30)"
            child = subprocess.Popen(['codex', '-c', script], executable=sys.executable,
                                     stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     cwd=temporary, env=env)
            try:
                self.assertEqual(child.stdout.readline(), b'ready\n')
                rows = [row for row in discover() if row['terminal']['process']['pid'] == child.pid]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['tool'], 'codex')
                self.assertEqual(rows[0]['project'], temporary)
                self.assertTrue(rows[0]['terminal']['tty'].startswith('/dev/pts/'))
                self.assertEqual(rows[0]['terminal']['gnomeTerminal']['screenId'], '12345678-abcd-abcd-abcd-123456789abc')
                backend_script = Path(temporary) / 'backend.py'
                backend_script.write_text(script)
                backend = subprocess.Popen(['codex', str(backend_script), 'app-server'], executable=sys.executable,
                                           stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           cwd=temporary, env=env)
                try:
                    self.assertEqual(backend.stdout.readline(), b'ready\n')
                    self.assertFalse(any(row['terminal']['process']['pid'] == backend.pid for row in discover()))
                finally:
                    backend.terminate()
                    backend.wait(timeout=3)
                    backend.stdout.close()
                    backend.stderr.close()
            finally:
                child.terminate()
                child.wait(timeout=3)
                child.stdout.close()
                child.stderr.close()

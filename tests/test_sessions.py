import json
from pathlib import Path
import tempfile
import unittest

from ai_monitor.sessions import Sessions


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.live = True
        self.store = Sessions(Path(self.temp.name) / "sessions.json", clock=lambda: self.now,
                              is_alive=lambda _: self.live)
        self.base = {"tool": "claude", "session": "abc", "cwd": "/project/shop",
                     "terminal": {"process": {"pid": 123, "start": "42"}}, "sentAt": self.now}

    def event(self, name, **kwargs):
        self.now += 1
        self.store.event({**self.base, "event": name, "sentAt": self.now, **kwargs})
        return self.store.snapshot()

    def test_full_attention_lifecycle_and_acknowledgement(self):
        self.assertEqual(self.event("SessionStart")[0]["state"], "idle")
        self.assertEqual(self.event("UserPromptSubmit")[0]["state"], "working")
        row = self.event("PermissionRequest")[0]
        self.assertTrue(row["attention"])
        self.store.acknowledge(row["id"])
        row = self.store.snapshot()[0]
        self.assertEqual(row["state"], "approval")
        self.assertFalse(row["attention"])
        self.assertEqual(self.event("PostToolUse")[0]["state"], "working")
        self.assertEqual(self.event("Stop")[0]["state"], "ready")
        self.assertEqual(self.event("SessionEnd"), [])

    def test_duplicate_notifications_do_not_rearm_acknowledged_session(self):
        row = self.event("Stop")[0]
        self.store.acknowledge(row["id"])
        repeated = self.event("Notification", notification="idle_prompt")[0]
        self.assertEqual(repeated["revision"], row["revision"])
        self.assertFalse(repeated["attention"])

    def test_explicit_questions_and_background_work(self):
        self.assertEqual(self.event("PreToolUse", toolName="AskUserQuestion")[0]["state"], "question")
        self.assertEqual(self.event("PostToolUse")[0]["state"], "working")
        self.assertEqual(self.event("Stop", background=True)[0]["state"], "working")

    def test_codex_automatic_permission_reviews_remain_working_for_long_commands(self):
        self.base['tool'] = 'codex'
        self.event('UserPromptSubmit')
        for metadata in ({'approvalsReviewer': 'auto_review'}, {'permissionMode': 'dontAsk'},
                         {'permissionMode': 'bypassPermissions'}):
            row = self.event('PermissionRequest', **metadata)[0]
            self.assertEqual((row['state'], row['attention']), ('working', False))
            self.now += 600
            row = self.store.snapshot()[0]
            self.assertEqual((row['state'], row['attention']), ('working', False))

    def test_codex_review_routing_is_retained_for_long_turns_but_never_reused_for_another_turn(self):
        self.base['tool'] = 'codex'
        self.event('UserPromptSubmit', turnId='first')
        self.event('PreToolUse', toolName='Bash', turnId='first', approvalsReviewer='auto_review')
        self.event('PermissionRequest', turnId='first')  # Context is beyond the bounded read window.
        self.now += 600
        row = self.store.snapshot()[0]
        self.assertEqual((row['state'], row['attention']), ('working', False))
        self.assertNotIn('_codexReviewer', row)
        self.event('UserPromptSubmit', turnId='second')
        self.event('PermissionRequest', turnId='second')  # Unknown routing must not suppress a real prompt.
        self.now += 3
        row = self.store.snapshot()[0]
        self.assertEqual((row['state'], row['attention']), ('approval', True))

    def test_codex_permission_prompts_must_persist_and_are_cancelled_when_work_resumes(self):
        self.base['tool'] = 'codex'
        self.event('UserPromptSubmit')
        row = self.event('PermissionRequest', approvalsReviewer='user')[0]
        revision = row['revision']
        self.assertEqual((row['state'], row['attention']), ('working', False))
        self.now += 2
        self.event('PostToolUse')
        self.now += 10
        self.assertFalse(self.store.snapshot()[0]['attention'])
        self.event('PermissionRequest', approvalsReviewer='user')
        self.now += 1
        self.event('PermissionRequest', approvalsReviewer='user')
        self.now += 1
        row = self.store.snapshot()[0]
        self.assertEqual((row['state'], row['attention']), ('approval', True))
        self.assertGreater(row['revision'], revision)
        self.store.acknowledge(row['id'])
        self.event('PermissionRequest', approvalsReviewer='user')
        self.assertFalse(self.store.snapshot()[0]['attention'])
        self.event('PostToolUse')
        self.event('PermissionRequest')  # Missing metadata must still allow real prompts.
        self.now += 3
        self.assertTrue(self.store.snapshot()[0]['attention'])

    def test_codex_only_blocking_question_tools_request_attention_even_with_automatic_permissions(self):
        self.base['tool'] = 'codex'
        self.event('UserPromptSubmit')
        for tool in ('functions.request_user_input_async', 'mcp__reviewer__request_user_input',
                     'functions.request_user_input_summary'):
            self.event('PreToolUse', toolName=tool)
            self.now += 10
            row = self.store.snapshot()[0]
            self.assertEqual((row['state'], row['attention']), ('working', False))
        self.event('PreToolUse', toolName='functions.request_user_input')
        self.now += 3
        row = self.store.snapshot()[0]
        self.assertEqual((row['state'], row['attention']), ('question', True))
        row = self.event('PermissionRequest', approvalsReviewer='auto_review')[0]
        self.assertEqual((row['state'], row['attention']), ('question', True))
        self.event('PostToolUse')
        self.event('PreToolUse', toolName='request_user_input')
        self.event('PostToolUse')
        self.now += 10
        self.assertFalse(self.store.snapshot()[0]['attention'])

    def test_subagents_and_out_of_order_events_do_not_inflate_counts(self):
        self.event("UserPromptSubmit")
        self.event("Stop", subagent=True)
        self.event("PermissionRequest", sentAt=1)
        self.assertEqual(len(self.store.snapshot()), 1)
        self.assertEqual(self.store.snapshot()[0]["state"], "working")

    def test_subagent_counts_waiting_and_completion_remain_in_the_parent_row(self):
        for tool in ('claude', 'codex'):
            self.base['tool'] = tool
            self.event('UserPromptSubmit')
            for identifier in ('one', 'two', 'three'):
                rows = self.event('SubagentStart', subagent=True, agentId=identifier)
            row = next(row for row in rows if row['tool'] == tool)
            self.assertEqual(row['subagentCount'], 3)
            self.assertFalse(row['attention'])
            self.assertNotIn('_subagents', row)
            self.event('Stop')
            row = next(row for row in self.store.snapshot() if row['tool'] == tool)
            self.assertEqual(row['state'], 'working')
            self.assertTrue(row['waitingForSubagents'])
            self.assertFalse(row['attention'])
            self.event('SubagentStop', subagent=True, agentId='one')
            self.event('SubagentStop', subagent=True, agentId='one')
            row = next(row for row in self.store.snapshot() if row['tool'] == tool)
            self.assertEqual(row['subagentCount'], 2)
            self.event('PermissionRequest')
            if tool == 'codex':
                self.now += 3
                self.store.snapshot()
            self.event('SubagentStop', subagent=True, agentId='two')
            row = next(row for row in self.store.snapshot() if row['tool'] == tool)
            self.assertEqual((row['state'], row['attention']), ('approval', True))
            self.event('PostToolUse')
            self.event('SubagentStop', subagent=True, agentId='three')
            row = next(row for row in self.event('Stop') if row['tool'] == tool)
            self.assertEqual((row['subagentCount'], row['state'], row['attention']), (0, 'ready', True))
        self.assertEqual(len(self.store.snapshot()), 2)

    def test_subagent_events_are_ordered_per_child_and_reset_on_interrupt(self):
        self.event('UserPromptSubmit')
        self.event('SubagentStart', agentId='one')
        old = self.now
        self.event('PostToolUse')
        self.event('SubagentStart', agentId='two', sentAt=old + 0.5)
        self.assertEqual(self.store.snapshot()[0]['subagentCount'], 2)
        self.event('SubagentStop', agentId='one')
        self.event('SubagentStart', agentId='one', sentAt=old)
        self.assertEqual(self.store.snapshot()[0]['subagentCount'], 1)
        self.event('PreToolUse', toolName='functions.wait_agent')
        self.assertTrue(self.store.snapshot()[0]['waitingForSubagents'])
        self.event('Interrupt')
        self.event('SubagentStart', agentId='late', sentAt=old)
        self.assertEqual((self.store.snapshot()[0]['subagentCount'], self.store.snapshot()[0]['state']), (0, 'idle'))

    def test_background_snapshot_recovers_missed_starts_without_double_counting_stops(self):
        row = self.event('Stop', background=True, backgroundSubagents=3)[0]
        self.assertEqual((row['state'], row['subagentCount'], row['waitingForSubagents']), ('working', 3, True))
        self.event('SubagentStop', agentId='one')
        self.event('Stop', background=True, backgroundSubagents=2)
        self.event('SubagentStop', agentId='one')
        self.assertEqual(self.store.snapshot()[0]['subagentCount'], 2)
        self.event('SubagentStop', agentId='two')
        self.assertEqual(self.store.snapshot()[0]['subagentCount'], 1)
        self.event('SubagentStop', agentId='three')
        self.assertEqual(self.store.snapshot()[0]['subagentCount'], 0)
        row = self.event('Stop', backgroundSubagents=0)[0]
        self.assertEqual((row['state'], row['subagentCount']), ('ready', 0))

    def test_exited_processes_disappear_and_restart_preserves_ack(self):
        row = self.event("Stop")[0]
        self.store.acknowledge(row["id"])
        restarted = Sessions(self.store.path, is_alive=lambda _: True)
        self.assertFalse(restarted.snapshot()[0]["attention"])
        self.live = False
        self.assertEqual(self.store.snapshot(), [])
        self.assertEqual(json.loads(self.store.path.read_text()), {})

    def test_summaries_never_contain_terminal_metadata_or_prompt(self):
        rows = self.event("Stop", prompt="a very private prompt")
        self.assertNotIn("private", json.dumps(rows))
        self.assertNotIn("terminal", rows[0])

    def test_old_session_end_cannot_remove_resumed_process(self):
        self.event("UserPromptSubmit")
        self.event("UserPromptSubmit", terminal={"process": {"pid": 456, "start": "99"}})
        self.event("SessionEnd")
        self.assertEqual(len(self.store.snapshot()), 1)

    def test_daemon_event_does_not_guess_between_two_terminals_in_the_same_project(self):
        candidates = [{"tool": "codex", "project": "/project", "terminal": {"process": {"pid": pid}}}
                      for pid in (10, 11)]
        event = {'tool': 'codex', 'session': 'id', 'cwd': '/project', 'event': 'Stop'}
        self.assertIsNone(self.store.resolve_terminal(event, candidates))
        self.assertEqual(self.store.resolve_terminal(event, candidates[:1]), candidates[0]['terminal'])

    def test_discovery_refreshes_ubuntu_tab_identity_without_resetting_status(self):
        row = self.event('Stop')[0]
        terminal = {**self.base['terminal'], 'gnomeTerminal': {'busName': ':1.8', 'screenId': 'uuid'}}
        self.store.discover([{'tool': 'claude', 'project': '/project/shop', 'terminal': terminal}])
        self.assertEqual(self.store.get(row['id'])['terminal']['gnomeTerminal']['screenId'], 'uuid')
        self.assertEqual(self.store.snapshot()[0]['state'], 'ready')
        self.assertEqual(len(self.store.snapshot()), 1)

    def test_native_names_and_explicit_names_survive_discovery(self):
        row = self.event('Stop')[0]
        self.store.refresh_titles({row['id']: 'Native name'})
        self.assertEqual(self.store.snapshot()[0]['title'], 'Native name')
        self.event('PostToolUse', title='Explicit label')
        self.store.refresh_titles({row['id']: 'Another native name'})
        self.assertEqual(self.store.snapshot()[0]['title'], 'Explicit label')

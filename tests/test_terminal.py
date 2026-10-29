import os
from unittest.mock import patch
import unittest

from ai_monitor.terminal import alive, ancestors, focus_target, process, gnome_target, visible_views


class TerminalTests(unittest.TestCase):
    def test_visible_tmux_views_only_include_clients_on_the_exact_pane_and_tty(self):
        calls = []

        def runner(socket, *args):
            calls.append((socket, *args))
            self.assertEqual(args[0], 'list-clients')
            return '123\t%3\t/dev/pts/42\n456\t%4\t/dev/pts/43\n789\t%3\t/dev/pts/99'

        def environment(pid):
            return {'WINDOWID': str(pid)}

        cache = {}
        with patch('ai_monitor.terminal.process_environment', side_effect=environment):
            self.assertEqual(visible_views(self.terminal(), cache, runner),
                             [{'windowId': '123', 'gnomeTerminal': None}])
            self.assertEqual(visible_views({**self.terminal(), 'tmuxPane': '%4', 'tty': '/dev/pts/43'}, cache, runner),
                             [{'windowId': '456', 'gnomeTerminal': None}])
            self.assertEqual(len(calls), 1)

    def test_detached_tmux_never_reuses_the_servers_original_window(self):
        self.assertEqual(visible_views({**self.terminal(), 'windowId': 'old'}, runner=lambda *_: ''), [])
        self.assertEqual(visible_views({**self.terminal(), 'remote': True}), [])
        def unavailable(*_):
            raise OSError('gone')
        self.assertEqual(visible_views(self.terminal(), runner=unavailable), [])

    def test_plain_terminal_visibility_preserves_gnome_screen_identity(self):
        target = {'busName': ':1.42', 'screenId': 'screen'}
        terminal = {**self.terminal(), 'tmuxSocket': '', 'gnomeTerminal': target}
        self.assertEqual(visible_views(terminal), [{'windowId': '', 'gnomeTerminal': target}])

    def test_ubuntu_tab_identity_is_normalized_and_preserved_for_focus(self):
        environment = {'GNOME_TERMINAL_SERVICE': ':1.42',
                       'GNOME_TERMINAL_SCREEN': '/org/gnome/Terminal/screen/12345678_abcd_abcd_abcd_123456789abc'}
        target = gnome_target(environment)
        self.assertEqual(target, {'busName': ':1.42', 'screenId': '12345678-abcd-abcd-abcd-123456789abc'})
        terminal = {**self.terminal(), 'tmuxSocket': '', 'gnomeTerminal': target}
        self.assertEqual(focus_target(terminal)['gnomeTerminal'], target)
        self.assertIsNone(gnome_target({**environment, 'GNOME_TERMINAL_SCREEN': '/unexpected/path'}))

    def test_namespace_root_process_is_included_in_ancestry(self):
        values = {2: {"pid": 2, "parent": 1, "start": "2"},
                  1: {"pid": 1, "parent": 0, "start": "1"}}
        with patch('ai_monitor.terminal.process', side_effect=values.get):
            self.assertEqual([row['pid'] for row in ancestors(2)], [2, 1])

    def test_process_identity_detects_pid_reuse(self):
        current = process(os.getpid())
        self.assertTrue(alive(current))
        self.assertFalse(alive({**current, "start": "not-the-same-process"}))
        self.assertEqual(ancestors(os.getpid())[0]["pid"], os.getpid())

    def terminal(self):
        current = process(os.getpid())
        return {"process": current, "ancestors": [current], "tty": "/dev/pts/42", "tmuxSocket": "/tmp/fake",
                "tmuxPane": "%3", "windowId": "", "remote": False}

    def test_tmux_selects_exact_window_and_pane_then_finds_client_window(self):
        calls = []

        def runner(socket, *args):
            calls.append((socket, *args))
            if args[0] == "display-message":
                return "/dev/pts/42\t$1\t@2"
            if args[0] == "list-clients":
                return f"{os.getpid()}\t/dev/pts/1\t$1\t100"
            return ""

        result = focus_target(self.terminal(), runner)
        self.assertTrue(result["tmux"])
        self.assertIn(os.getpid(), result["pids"])
        self.assertIn(("/tmp/fake", "select-pane", "-t", "%3"), calls)
        self.assertIn(("/tmp/fake", "select-window", "-t", "$1:@2"), calls)

    def test_reused_pane_and_detached_server_fail_without_selecting(self):
        def reused(socket, *args):
            self.assertEqual(args[0], "display-message")
            return "/dev/pts/99\t$1\t@2"
        with self.assertRaisesRegex(ValueError, "original tmux pane"):
            focus_target(self.terminal(), reused)

        def detached(socket, *args):
            if args[0] == "display-message":
                return "/dev/pts/42\t$1\t@2"
            self.assertEqual(args[0], "list-clients")
            return ""
        with self.assertRaisesRegex(ValueError, "detached"):
            focus_target(self.terminal(), detached)

    def test_ordinary_terminal_returns_window_candidates_without_shell_commands(self):
        terminal = {**self.terminal(), "tmuxSocket": "", "tmuxPane": "", "windowId": "12345"}
        result = focus_target(terminal, lambda *_: self.fail("must not execute tmux"))
        self.assertEqual(result["pids"], [os.getpid()])
        self.assertEqual(result["windowId"], "12345")
        self.assertFalse(result["tmux"])

    def test_remote_and_exited_sessions_never_focus_unrelated_windows(self):
        with self.assertRaisesRegex(ValueError, "remote"):
            focus_target({**self.terminal(), "remote": True})
        with patch("ai_monitor.terminal.alive", return_value=False):
            with self.assertRaisesRegex(ValueError, "exited"):
                focus_target(self.terminal())

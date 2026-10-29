import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_monitor.client import request
from ai_monitor.service import Handler, Monitor, Server
from ai_monitor.terminal import process


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.monitor = Monitor(Path(self.temp.name), network=False)
        patch.object(self.monitor.titles, 'lookup', return_value={}).start()
        self.discovery = patch('ai_monitor.service.discover', return_value=[]).start()
        self.addCleanup(patch.stopall)

    def test_events_still_work_when_usage_collection_is_unavailable(self):
        self.monitor.error = "Usage unavailable"
        self.monitor.dispatch({"op": "event", "tool": "codex", "session": "session",
                               "event": "Stop", "terminal": {"process": process(os.getpid()), "tty": "/dev/pts/123"}})
        snapshot = self.monitor.dispatch({"op": "snapshot", "active": True})
        self.assertEqual(snapshot["sessions"][0]["state"], "ready")
        self.assertEqual(snapshot["collectorError"], "Usage unavailable")
        self.monitor.dispatch({"op": "ack"})
        self.assertFalse(self.monitor.dispatch({"op": "snapshot"})["sessions"][0]["attention"])

    def test_terminal_is_counted_before_hooks_then_daemon_hook_binds_without_duplicates(self):
        terminal = {"process": process(os.getpid()), "tty": "/dev/pts/123"}
        self.discovery.return_value = [{"tool": "codex", "project": "/project", "terminal": terminal}]
        snapshot = self.monitor.dispatch({"op": "snapshot"})
        self.assertEqual(len(snapshot['sessions']), 1)
        self.assertEqual(snapshot['sessions'][0]['state'], 'unknown')
        self.monitor.dispatch({"op": "event", "tool": "codex", "session": "native-id", "event": "Stop",
                               "cwd": "/project", "terminal": {"process": {"pid": 999999, "start": "daemon"}, "tty": ''}})
        snapshot = self.monitor.dispatch({"op": "snapshot"})
        self.assertEqual(len(snapshot['sessions']), 1)
        self.assertEqual(snapshot['sessions'][0]['state'], 'ready')
        self.assertEqual(snapshot['health']['eventsReceived'], 1)
        self.assertEqual(snapshot['health']['unlinkedEvents'], 0)

    def test_private_socket_request_response_and_malformed_requests(self):
        path = Path(self.temp.name) / "control.sock"
        try:
            server = Server(str(path), Handler)
        except PermissionError:
            self.skipTest("This sandbox prohibits Unix socket creation; in-memory protocol tests still run")
        self.addCleanup(server.server_close)
        server.monitor = self.monitor
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.assertEqual(request({"op": "snapshot"}, path=path)["sessions"], [])
        with self.assertRaisesRegex(ValueError, "Unknown operation"):
            request({"op": "nonexistent"}, path=path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))
            client.sendall(b"not-json\n")
            self.assertFalse(json.loads(client.recv(4096))["ok"])

    def test_protocol_dispatch_and_bounded_json_without_live_socket(self):
        class Connection:
            def settimeout(self, _):
                pass

            def getsockopt(self, *_):
                import struct
                return struct.pack("3i", os.getpid(), os.getuid(), os.getgid())

        for raw, expected in [(b'{"op":"snapshot"}\n', True), (b"not-json\n", False),
                              (b"[]\n", False), (b"x" * 65537, False)]:
            handler = object.__new__(Handler)
            handler.connection = Connection()
            handler.rfile = io.BytesIO(raw)
            handler.wfile = io.BytesIO()
            handler.server = type("Server", (), {"monitor": self.monitor})()
            handler.handle()
            self.assertEqual(json.loads(handler.wfile.getvalue())["ok"], expected)

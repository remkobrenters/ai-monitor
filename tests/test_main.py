import io
import json
import unittest
from unittest.mock import patch

from ai_monitor.__main__ import watch


class WatchTests(unittest.TestCase):
    def test_focus_reply_preserves_each_click_timestamp_across_the_bridge(self):
        messages = [{"op": "focus", "id": "first", "timestamp": 12345},
                    {"op": "focus", "id": "second", "timestamp": 23456}]
        incoming = b"".join(json.dumps(message).encode() + b"\n" for message in messages)
        output = io.StringIO()
        target = {"pids": [123], "windowId": "291"}

        def request(message):
            return {"sessions": []} if message["op"] == "snapshot" else target

        with (
            patch("ai_monitor.__main__.request", side_effect=request),
            patch("ai_monitor.__main__.sys.stdin") as stdin,
            patch("ai_monitor.__main__.sys.stdout", output),
            patch("ai_monitor.__main__.select.select", return_value=([stdin], [], [])),
            patch("ai_monitor.__main__.os.read", side_effect=[incoming, b""]),
            patch("ai_monitor.__main__.time.monotonic", return_value=0),
        ):
            watch()

        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(replies[1:], [{"type": "focus", "id": message["id"], "target": target,
                                       "timestamp": message["timestamp"]} for message in messages])

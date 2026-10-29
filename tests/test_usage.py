import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.response
from email.message import Message
from unittest.mock import patch

from ai_monitor import usage as ai_usage
from ai_monitor.usage import (ClaudeScanner, CodexScanner, UsageMonitor, LimitsUnavailable,
                      RateLimited, claude_limits, codex_rate_limits, parse_codex_limits)


def at(*parts):
    """Epoch seconds of a local wall-clock time."""
    return dt.datetime(*parts).timestamp()


def iso(epoch):
    return dt.datetime.fromtimestamp(epoch).astimezone().isoformat()


def compact(record):
    # Claude Code and Codex write compact JSON lines.
    return json.dumps(record, separators=(",", ":"))


def claude_line(when, message_id="msg_1", request_id="req_1", output=10, model="claude-opus-5-5"):
    return compact({"type": "assistant", "timestamp": iso(when), "requestId": request_id,
                    "message": {"id": message_id, "model": model, "content": [{"type": "text", "text": "secret"}],
                                "usage": {"input_tokens": 2, "output_tokens": output,
                                          "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}}})


def codex_line(when, total_input, cached, output, rate=None):
    return compact({"timestamp": iso(when), "type": "event_msg", "payload": {
        "type": "token_count", "rate_limits": rate,
        "info": {"total_token_usage": {"input_tokens": total_input, "cached_input_tokens": cached,
                                       "cache_write_input_tokens": 0, "output_tokens": output,
                                       "reasoning_output_tokens": 0, "total_tokens": total_input + output},
                 "last_token_usage": {"input_tokens": 1, "total_tokens": 1}}}})


def write(path, lines, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode) as f:
        f.write("".join(f"{line}\n" for line in lines))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)


class ClaudeScannerTests(Fixture):
    def test_dedups_across_files_keeping_the_final_output_count(self):
        day = at(2026, 10, 26, 9)
        write(self.root / "p" / "s.jsonl", [claude_line(day, output=8), claude_line(day, output=303),
                                            claude_line(day, "msg_2", "req_2", 5)])
        # A subagent transcript copies the same message.
        write(self.root / "p" / "s" / "subagents" / "agent-1.jsonl", [claude_line(day, output=303)])
        scanner = ClaudeScanner([self.root])
        scanner.scan(at(2026, 10, 1), set())
        self.assertEqual(scanner.days["2026-10-26"],
                         {"input": 4, "cacheRead": 2000, "cacheWrite": 200, "output": 308})

    def test_reads_only_appended_complete_lines_and_restarts_replaced_files(self):
        path = self.root / "s.jsonl"
        write(path, [claude_line(at(2026, 10, 26, 9))])
        scanner = ClaudeScanner([self.root])
        scanner.scan(at(2026, 10, 1), set())
        partial = claude_line(at(2026, 10, 26, 10), "msg_2", "req_2", 7)
        with open(path, "a") as f:
            f.write(partial[:40])
        scanner.scan(at(2026, 10, 1), set())
        self.assertEqual(scanner.days["2026-10-26"]["output"], 10)
        with open(path, "a") as f:
            f.write(partial[40:] + "\n")
        scanner.scan(at(2026, 10, 1), set())
        self.assertEqual(scanner.days["2026-10-26"]["output"], 17)
        path.unlink()
        write(path, [claude_line(at(2026, 10, 26, 11), "msg_3", "req_3", 1)])
        scanner.scan(at(2026, 10, 1), set())
        self.assertEqual(scanner.days["2026-10-26"]["output"], 18)

    def test_buckets_by_local_day_and_skips_synthetic_old_and_archived_days(self):
        write(self.root / "s.jsonl", [
            claude_line(at(2026, 10, 25, 23, 59), "a", "1"), claude_line(at(2026, 10, 26, 0, 1), "b", "2"),
            claude_line(at(2026, 10, 26, 9), "c", "3", model="<synthetic>"),
            claude_line(at(2026, 9, 30, 9), "d", "4"), claude_line(at(2026, 10, 24, 9), "e", "5"),
            "not json", compact({"type": "user", "message": {"content": "usage"}})])
        scanner = ClaudeScanner([self.root])
        scanner.scan(at(2026, 10, 1), {"2026-10-24"})
        self.assertEqual(sorted(scanner.days), ["2026-10-25", "2026-10-26"])
        self.assertEqual(scanner.days["2026-10-26"]["output"], 10)


class CodexScannerTests(Fixture):
    def test_counts_positive_deltas_per_day_with_cache_exclusive_input(self):
        weekly = {"limit_id": "codex", "plan_type": "plus",
                  "primary": {"used_percent": 21.0, "window_minutes": 300, "resets_at": 1_800_000_000},
                  "secondary": {"used_percent": 9.0, "window_minutes": 10080, "resets_at": 1_800_500_000}}
        premium = {"limit_id": "premium", "primary": {"used_percent": 99, "window_minutes": 1, "resets_at": 1}}
        write(self.root / "2026" / "10" / "25" / "rollout-a.jsonl", [
            codex_line(at(2026, 9, 30, 9), 1000, 800, 50),        # before the window: baseline only
            codex_line(at(2026, 10, 25, 22), 3000, 2500, 100, weekly),
            codex_line(at(2026, 10, 25, 22, 1), 3000, 2500, 100),  # duplicate / compaction repeat
            codex_line(at(2026, 10, 26, 8), 4000, 3400, 160, premium),  # resumed next day
        ])
        scanner = CodexScanner(self.root)
        scanner.scan(at(2026, 10, 1), set())
        self.assertEqual(scanner.days["2026-10-25"], {"input": 300, "cacheRead": 1700, "cacheWrite": 0, "output": 50})
        self.assertEqual(scanner.days["2026-10-26"], {"input": 100, "cacheRead": 900, "cacheWrite": 0, "output": 60})
        self.assertEqual(scanner.limits["plan"], "Plus")
        self.assertEqual([w["minutes"] for w in scanner.limits["windows"]], [300, 10080])
        self.assertEqual(scanner.limits["source"], "session")

    def test_appended_events_continue_from_the_session_total(self):
        path = self.root / "rollout-b.jsonl"
        write(path, [codex_line(at(2026, 10, 26, 8), 1000, 900, 10)])
        scanner = CodexScanner(self.root)
        scanner.scan(at(2026, 10, 1), set())
        write(path, [codex_line(at(2026, 10, 26, 9), 1500, 1300, 30)], mode="a")
        scanner.scan(at(2026, 10, 1), set())
        self.assertEqual(scanner.days["2026-10-26"], {"input": 200, "cacheRead": 1300, "cacheWrite": 0, "output": 30})


class MonitorTests(Fixture):
    def monitor(self, **kwargs):
        return UsageMonitor(self.root, self.root / "state", network=False, claude_roots=[self.root / "claude"],
                            codex_root=self.root / "codex", **kwargs)

    def test_periods_and_archived_days_survive_deleted_transcripts(self):
        write(self.root / "claude" / "a.jsonl", [claude_line(at(2026, 10, 2, 9), "a", "1"),
                                                 claude_line(at(2026, 10, 26, 9), "b", "2")])
        write(self.root / "claude" / "b.jsonl", [claude_line(at(2026, 10, 25, 9), "c", "3")])
        now = at(2026, 10, 26, 12)
        monitor = self.monitor()
        monitor.scan(now)
        tokens = monitor.snapshot(now)["tokens"]["claude"]
        self.assertEqual([tokens[p]["output"] for p in ("today", "week", "month")], [10, 10, 30])
        self.assertTrue((self.root / "state" / "ai-days" / "2026-10-02.json").exists())
        self.assertTrue((self.root / "state" / "ai-days" / "2026-10-25.json").exists())
        self.assertFalse((self.root / "state" / "ai-days" / "2026-10-26.json").exists())
        # Claude Code removes old transcripts; a restarted helper keeps the archived month.
        (self.root / "claude" / "b.jsonl").unlink()
        restarted = self.monitor()
        restarted.scan(now)
        self.assertEqual(restarted.snapshot(now)["tokens"]["claude"]["month"]["output"], 30)

    def test_yesterday_is_archived_only_after_late_lines_had_time_to_arrive(self):
        write(self.root / "claude" / "a.jsonl", [claude_line(at(2026, 10, 25, 23))])
        monitor = self.monitor()
        monitor.scan(at(2026, 10, 26, 0, 5))
        self.assertFalse((self.root / "state" / "ai-days" / "2026-10-25.json").exists())
        monitor.scan(at(2026, 10, 26, 0, 11))
        self.assertTrue((self.root / "state" / "ai-days" / "2026-10-25.json").exists())

    def test_ytd_counts_january_and_does_not_invent_empty_archives(self):
        write(self.root / "claude" / "a.jsonl", [claude_line(at(2026, 1, 2, 9), "jan", "1"),
                                                 claude_line(at(2026, 10, 26, 9), "oct", "2")])
        monitor = self.monitor()
        monitor.scan(at(2026, 10, 26, 12))
        periods = monitor.snapshot(at(2026, 10, 26, 12))["tokens"]["claude"]
        self.assertEqual(periods["ytd"]["output"], 20)
        self.assertEqual(periods["month"]["output"], 10)
        self.assertFalse((self.root / "state/ai-days/2026-01-01.json").exists())

    def test_damaged_archive_is_rebuilt_from_sources(self):
        write(self.root / "claude" / "a.jsonl", [claude_line(at(2026, 10, 25, 9))])
        archive = self.root / "state" / "ai-days" / "2026-10-25.json"
        archive.parent.mkdir(parents=True)
        archive.write_text("{broken")
        monitor = self.monitor()
        monitor.scan(at(2026, 10, 26, 12))
        self.assertEqual(json.loads(archive.read_text())["tools"]["claude"]["output"], 10)

    def test_limits_are_looked_up_only_while_active_and_at_most_every_ten_minutes(self):
        calls = []
        monitor = self.monitor()
        monitor.network = True
        monitor._codex = lambda: calls.append("codex") or {"windows": [], "fetchedAt": 1, "error": ""}
        ai_usage.claude_limits, original = (lambda credentials, now: calls.append("claude") or
                                            {"windows": [], "fetchedAt": now, "error": ""}), ai_usage.claude_limits
        self.addCleanup(setattr, ai_usage, "claude_limits", original)
        self.assertFalse(monitor.refresh_limits(1000, active=False))
        self.assertTrue(monitor.refresh_limits(1000, active=True))
        self.assertFalse(monitor.refresh_limits(1599, active=True))
        self.assertFalse(monitor.refresh_limits(5000, active=False))  # in a meeting
        self.assertTrue(monitor.refresh_limits(5001, active=True))    # back at the desk
        self.assertEqual(calls, ["claude", "codex", "claude", "codex"])
        self.assertTrue((self.root / "state" / "ai-limits.json").exists())

    def test_rate_limited_lookups_back_off_and_keep_the_last_values(self):
        monitor = self.monitor()
        monitor.network = True
        monitor.limits["claude"] = {"windows": [{"minutes": 300, "usedPercent": 40}], "error": ""}
        monitor._codex = lambda: {"windows": [], "fetchedAt": 1, "error": ""}

        def limited(credentials, now):
            raise RateLimited(0)
        ai_usage.claude_limits, original = limited, ai_usage.claude_limits
        self.addCleanup(setattr, ai_usage, "claude_limits", original)
        monitor.refresh_limits(1000, active=True)
        self.assertEqual(monitor.limits["claude"]["windows"][0]["usedPercent"], 40)
        self.assertIn("rate limited", monitor.limits["claude"]["error"])
        self.assertEqual(monitor.next_lookup["claude"], 1600)
        monitor.refresh_limits(1600, active=True)
        self.assertEqual(monitor.next_lookup["claude"], 2800)

    def test_a_newer_codex_request_supplies_windows_but_keeps_resets_from_the_lookup(self):
        monitor = self.monitor()
        monitor.limits["codex"] = {"windows": [{"minutes": 10080, "usedPercent": 5}], "fetchedAt": 100,
                                   "resetsAvailable": 1, "resetExpiresAt": 900, "error": ""}
        monitor.scanners["codex"].limits = {"windows": [{"minutes": 10080, "usedPercent": 7}], "fetchedAt": 200,
                                            "source": "session", "error": ""}
        merged = monitor.codex_limits()
        self.assertEqual((merged["windows"][0]["usedPercent"], merged["resetsAvailable"]), (7, 1))
        monitor.limits["codex"]["fetchedAt"] = 300
        self.assertEqual(monitor.codex_limits()["windows"][0]["usedPercent"], 5)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ClaudeLimitsTests(Fixture):
    def test_default_transport_rejects_redirects_without_forwarding_the_login(self):
        seen = []

        class Transport(urllib.request.HTTPSHandler):
            def https_open(self, request):
                seen.append(request.full_url)
                headers = Message()
                headers['Location'] = 'https://untrusted.example/collect'
                response = urllib.response.addinfourl(io.BytesIO(b''), headers, request.full_url, 302)
                response.msg = 'Found'
                return response

        with patch('urllib.request.HTTPSHandler', Transport):
            with self.assertRaisesRegex(LimitsUnavailable, 'HTTP 302'):
                claude_limits(self.credentials(2e12), 1000)
        self.assertEqual(seen, [ai_usage.CLAUDE_USAGE_URL])

    def test_oversized_provider_responses_are_rejected(self):
        with self.assertRaisesRegex(LimitsUnavailable, 'too large'):
            claude_limits(self.credentials(2e12), 1000,
                          lambda *_args, **_kwargs: Response(b'x' * (1024 * 1024 + 1)))
        path = self.root / 'oversized-response'
        path.write_bytes(b'x' * (2 * 1024 * 1024 + 1))
        with path.open('rb') as stream:
            with self.assertRaisesRegex(LimitsUnavailable, 'too large'):
                list(ai_usage.json_rpc_messages(stream.fileno(), time.monotonic() + 2))

    def credentials(self, expires_ms):
        path = self.root / ".credentials.json"
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "token", "expiresAt": expires_ms,
                                                      "subscriptionType": "team",
                                                      "rateLimitTier": "default_claude_max_5x"}}))
        return path

    def test_reads_five_hour_and_weekly_windows(self):
        seen = {}

        def opener(request, timeout):
            seen.update(url=request.full_url, beta=request.get_header("Anthropic-beta"))
            return Response(json.dumps({
                "five_hour": {"utilization": 38.0, "resets_at": "2026-10-26T13:20:00.460897+00:00"},
                "seven_day": {"utilization": 78, "resets_at": "2026-10-29T15:00:00+00:00"},
                "seven_day_opus": None}).encode())

        limits = claude_limits(self.credentials(2e12), 1000, opener)
        self.assertEqual(seen, {"url": ai_usage.CLAUDE_USAGE_URL, "beta": "oauth-2025-04-20"})
        self.assertEqual([(w["minutes"], w["usedPercent"]) for w in limits["windows"]], [(300, 38.0), (10080, 78)])
        self.assertEqual(limits["windows"][0]["resetsAt"], dt.datetime(2026, 10, 26, 13, 20, 0, 460897,
                                                                       tzinfo=dt.timezone.utc).timestamp())

    def test_expired_login_is_reported_without_a_request(self):
        def opener(request, timeout):
            raise AssertionError("no request with an expired login")
        with self.assertRaisesRegex(LimitsUnavailable, "expired"):
            claude_limits(self.credentials(1_000_000), 1000, opener)
        with self.assertRaisesRegex(LimitsUnavailable, "Not signed in"):
            claude_limits(self.root / "missing.json", 1000, opener)

    def test_rate_limit_responses_carry_retry_after(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {"retry-after": "1200"}, None)
        with self.assertRaises(RateLimited) as caught:
            claude_limits(self.credentials(2e12), 1000, opener)
        self.assertEqual(caught.exception.retry_after, 1200)


FAKE_CODEX = textwrap.dedent('''\
    #!{python}
    import json, sys
    assert sys.argv[1:] == ["app-server"]
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("method") == "initialize":
            assert message["params"]["clientInfo"]["name"] == "ai-monitor"
            assert message["params"]["clientInfo"]["title"] == "ai-monitor"
            print(json.dumps({{"id": 1, "result": {{"userAgent": "fake"}}}}), flush=True)
        elif message.get("method") == "account/rateLimits/read":
            print(json.dumps({{"method": "account/rateLimits/updated", "params": {{}}}}), flush=True)
            print(json.dumps({{"id": 2, "result": json.loads({result})}}), flush=True)
    ''')


class CodexLimitsTests(Fixture):
    RESULT = {"rateLimits": {"limitId": "codex", "planType": "self_serve_business_prolite",
                             "primary": {"usedPercent": 9, "windowDurationMins": 10080, "resetsAt": 1793600270},
                             "secondary": None, "rateLimitReachedType": None},
              "rateLimitResetCredits": {"availableCount": 2, "credits": [
                  {"id": "a", "status": "available", "expiresAt": 1795000000, "grantedAt": 1, "resetType": "codexRateLimits"},
                  {"id": "b", "status": "available", "expiresAt": 1794000000, "grantedAt": 1, "resetType": "codexRateLimits"},
                  {"id": "c", "status": "redeemed", "expiresAt": 1700000000, "grantedAt": 1, "resetType": "codexRateLimits"}]}}

    def test_reads_limits_and_resets_through_the_app_server(self):
        codex = self.root / "codex"
        codex.write_text(FAKE_CODEX.format(python=sys.executable, result=repr(json.dumps(self.RESULT))))
        codex.chmod(0o755)
        limits = parse_codex_limits(codex_rate_limits(str(codex), os.environ["PATH"], timeout=10), 1000)
        self.assertEqual(limits["plan"], "Business Pro Lite")
        self.assertEqual(limits["windows"], [{"minutes": 10080, "usedPercent": 9, "resetsAt": 1793600270}])
        self.assertEqual((limits["resetsAvailable"], limits["resetExpiresAt"]), (2, 1794000000))

    def test_errors_and_silence_become_unavailable(self):
        codex = self.root / "codex"
        codex.write_text(f"#!{sys.executable}\nimport json, sys\nsys.stdin.readline()\n"
                         "print(json.dumps({'id': 1, 'error': {'message': 'fixture-private-provider-detail'}}), flush=True)\n")
        codex.chmod(0o755)
        with self.assertRaisesRegex(LimitsUnavailable, 'check its login') as caught:
            codex_rate_limits(str(codex), os.environ["PATH"], timeout=10)
        self.assertNotIn('fixture-private-provider-detail', str(caught.exception))
        codex.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
        with self.assertRaisesRegex(LimitsUnavailable, "did not answer"):
            codex_rate_limits(str(codex), os.environ["PATH"], timeout=0.5)


if __name__ == "__main__":
    unittest.main()

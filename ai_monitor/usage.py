#!/usr/bin/env python3
"""Summarise Claude Code and Codex token usage and subscription limits for GNOME.

The local service owns this collector. Only token counts, percentages and timestamps leave this
process; message text is parsed in memory and never stored, printed or logged.

Network: only while the user is active and at most every 10 minutes, a read-only limits
lookup through the installed codex CLI and through Claude Code's existing login, which
is never refreshed here (Claude Code rotates it itself).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from . import __version__

FIELDS = ("input", "cacheRead", "cacheWrite", "output")  # input excludes cache reads/writes
TOOLS = ("claude", "codex")
SCAN_INTERVAL = 60
LIMITS_INTERVAL = 600
BACKOFF_MAX = 3600
DAY_FINAL_AFTER = 600  # A finished day is archived 10 minutes after midnight.
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_PLANS = {"free": "Free", "go": "Go", "plus": "Plus", "pro": "Pro", "prolite": "Pro Lite",
               "promax": "Pro Max", "team": "Team", "business": "Business",
               "self_serve_business_prolite": "Business Pro Lite",
               "self_serve_business_usage_based": "Business", "enterprise": "Enterprise",
               "edu": "Edu", "edu_plus": "Edu Plus", "edu_pro": "Edu Pro"}


class LimitsUnavailable(Exception):
    pass


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # An allowance lookup must never forward the login to a redirect target.
        return None


class RateLimited(LimitsUnavailable):
    def __init__(self, retry_after: float) -> None:
        super().__init__("Usage lookup rate limited; retrying later")
        self.retry_after = retry_after


def count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0
    return max(0, int(value))


def zero() -> dict:
    return dict.fromkeys(FIELDS, 0)


def add(days: dict, day: str, counts: dict) -> None:
    bucket = days.setdefault(day, zero())
    for name in FIELDS:
        bucket[name] += counts.get(name, 0)


def local_day(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch).date().isoformat()


def midnight(day: dt.date) -> float:
    return dt.datetime.combine(day, dt.time()).timestamp()


def parse_iso(value) -> float | None:
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def period_starts(today: dt.date) -> dict[str, dt.date]:
    # Calendar periods use local dates; weeks start Monday.
    return {"today": today, "week": today - dt.timedelta(days=today.weekday()),
            "month": today.replace(day=1), "ytd": today.replace(month=1, day=1)}


def window_start(today: dt.date) -> dt.date:
    starts = period_starts(today)
    return min(starts.values())


def day_range(first: dt.date, last: dt.date):
    while first <= last:
        yield first
        first += dt.timedelta(days=1)


def write_private(path: Path, value) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Atomic write; readers never see a half-written JSON record.
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2)
            f.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def jsonl_files(roots, since: float):
    """(path, stat) of every *.jsonl below roots modified at or after `since`."""
    pending = [Path(root) for root in roots]
    while pending:
        try:
            entries = list(os.scandir(pending.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.name.endswith(".jsonl"):
                    stat = entry.stat()
                    if stat.st_mtime >= since:
                        yield Path(entry.path), stat
            except OSError:
                continue


def appended_lines(path: Path, state: dict, stat):
    """Complete lines added since the last call. A replaced or truncated file starts over
    (clearing per-file state); a trailing partial line waits for its newline."""
    if state.get("inode") != stat.st_ino or stat.st_size < state.get("offset", 0):
        state.clear()
        state.update(inode=stat.st_ino, offset=0)
    if stat.st_size == state["offset"]:
        return
    with open(path, "rb") as handle:
        handle.seek(state["offset"])
        partial = b""
        while chunk := handle.read(1 << 22):
            lines = (partial + chunk).split(b"\n")
            partial = lines.pop()
            for line in lines:
                state["offset"] += len(line) + 1
                yield line


class ClaudeScanner:
    """Claude Code transcripts. Messages repeat per content block and across files
    (subagent transcripts copy them), so usage is deduplicated globally by
    message id + request id, keeping the largest value: early copies of a message can
    carry a partial, mid-stream output count."""

    def __init__(self, roots) -> None:
        self.roots = roots
        self.files: dict[Path, dict] = {}
        self.messages: dict[str, tuple[str, dict]] = {}
        self.days: dict[str, dict] = {}

    @staticmethod
    def entry(line: bytes):
        if b'"assistant"' not in line or b'"usage"' not in line:
            return None
        try:
            record = json.loads(line)
        except ValueError:
            return None
        message = record.get("message") if record.get("type") == "assistant" else None
        usage = message.get("usage") if isinstance(message, dict) else None
        timestamp = parse_iso(record.get("timestamp"))
        if not isinstance(usage, dict) or timestamp is None or message.get("model") == "<synthetic>":
            return None
        key = (f"{message['id']}:{record['requestId']}"
               if message.get("id") and record.get("requestId") else None)
        return timestamp, key, {
            "input": count(usage.get("input_tokens")),
            "cacheRead": count(usage.get("cache_read_input_tokens")),
            "cacheWrite": count(usage.get("cache_creation_input_tokens")),
            "output": count(usage.get("output_tokens")),
        }

    def scan(self, since: float, archived: set[str]) -> None:
        for path, stat in jsonl_files(self.roots, since):
            for line in appended_lines(path, self.files.setdefault(path, {}), stat):
                parsed = self.entry(line)
                if parsed is None:
                    continue
                timestamp, key, counts = parsed
                day = local_day(timestamp)
                if timestamp < since or day in archived:
                    continue
                if key is None:
                    add(self.days, day, counts)
                    continue
                seen = self.messages.get(key)
                if seen is None:
                    self.messages[key] = (day, counts)
                    add(self.days, day, counts)
                    continue
                growth = {name: max(0, counts[name] - seen[1][name]) for name in FIELDS}
                if any(growth.values()):
                    add(self.days, seen[0], growth)
                    self.messages[key] = (seen[0], {name: seen[1][name] + growth[name] for name in FIELDS})

    def forget(self, keep) -> None:
        self.days = {day: counts for day, counts in self.days.items() if keep(day)}
        self.messages = {key: seen for key, seen in self.messages.items() if keep(seen[0])}


def rate_windows(rate: dict, percent: str, minutes: str, resets: str) -> list[dict]:
    windows = []
    for name in ("primary", "secondary"):
        window = rate.get(name)
        if isinstance(window, dict) and isinstance(window.get(percent), (int, float)):
            windows.append({"minutes": window.get(minutes), "usedPercent": window[percent],
                            "resetsAt": window.get(resets)})
    return sorted(windows, key=lambda window: window["minutes"] or 0)


def codex_plan(plan) -> str:
    if not isinstance(plan, str) or plan == "unknown":
        return ""
    return CODEX_PLANS.get(plan, plan.replace("_", " ").title())


class CodexScanner:
    """Codex rollout files. `total_token_usage` is cumulative per session; positive
    deltas are counted (duplicate events and context compaction repeat totals).
    A resumed session appends to its original file, so files are picked by mtime."""

    RAW = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "total_tokens")

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: dict[Path, dict] = {}
        self.days: dict[str, dict] = {}
        self.limits: dict | None = None  # Newest rate limits seen in any session

    def scan(self, since: float, archived: set[str]) -> None:
        for path, stat in jsonl_files([self.root], since):
            state = self.files.setdefault(path, {})
            for line in appended_lines(path, state, stat):
                if b'"token_count"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                payload = record.get("payload")
                timestamp = parse_iso(record.get("timestamp"))
                if (record.get("type") != "event_msg" or not isinstance(payload, dict)
                        or payload.get("type") != "token_count" or timestamp is None):
                    continue
                self._limits(payload.get("rate_limits"), timestamp)
                usage = (payload.get("info") or {}).get("total_token_usage")
                if not isinstance(usage, dict):
                    continue
                current = {name: count(usage.get(name)) for name in self.RAW}
                last = state.setdefault("last", dict.fromkeys(self.RAW, 0))
                if current["total_tokens"] <= last["total_tokens"]:
                    continue
                delta = {name: max(0, current[name] - last[name]) for name in self.RAW}
                state["last"] = current
                day = local_day(timestamp)
                if timestamp < since or day in archived:
                    continue
                # OpenAI counts cached input inside input; normalise to cache-exclusive input.
                cached, written = delta["cached_input_tokens"], delta["cache_write_input_tokens"]
                add(self.days, day, {"input": max(0, delta["input_tokens"] - cached - written),
                                     "cacheRead": cached, "cacheWrite": written,
                                     "output": delta["output_tokens"]})

    def _limits(self, rate, timestamp: float) -> None:
        # Other metered buckets (e.g. "premium") are not the Codex plan limits.
        if (not isinstance(rate, dict) or rate.get("limit_id") not in (None, "codex")
                or not rate.get("primary")):
            return
        if self.limits and self.limits["fetchedAt"] >= timestamp:
            return
        self.limits = {"plan": codex_plan(rate.get("plan_type")),
                       "windows": rate_windows(rate, "used_percent", "window_minutes", "resets_at"),
                       "limitReached": rate.get("rate_limit_reached_type") is not None,
                       "fetchedAt": timestamp, "source": "session", "error": ""}

    def forget(self, keep) -> None:
        self.days = {day: counts for day, counts in self.days.items() if keep(day)}


def valid_archive(record, day: str) -> bool:
    tools = record.get("tools") if isinstance(record, dict) else None
    return (record.get("schemaVersion") == 1 and record.get("date") == day and isinstance(tools, dict)
            and all(isinstance(tools.get(tool), dict) and all(
                isinstance(tools[tool].get(name), int) and tools[tool][name] >= 0 for name in FIELDS)
                for tool in TOOLS))


class DayArchive:
    """Finished days, so month totals survive Claude Code's 30-day transcript cleanup
    and restarts only rescan unarchived days. Derived data: a damaged file is rebuilt."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.days: dict[str, dict] = {}

    def load(self, first: dt.date, last: dt.date) -> None:
        for day in day_range(first, last):
            key = day.isoformat()
            if key in self.days:
                continue
            try:
                record = json.loads((self.directory / f"{key}.json").read_text())
            except (OSError, ValueError):
                continue
            if valid_archive(record, key):
                self.days[key] = record["tools"]

    def save(self, day: str, tools: dict) -> None:
        write_private(self.directory / f"{day}.json", {"schemaVersion": 1, "date": day, "tools": tools})
        self.days[day] = tools


def search_path(home: Path) -> str:
    extra = [str(home / ".npm-packages" / "bin"), str(home / ".local" / "bin")]
    return os.pathsep.join([*os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep), *extra])


def json_rpc_messages(fd: int, deadline: float):
    buffer = b""
    while (remaining := deadline - time.monotonic()) > 0:
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            return
        chunk = os.read(fd, 65536)
        if not chunk:
            return
        *lines, buffer = (buffer + chunk).split(b"\n")
        if len(buffer) > 2 * 1024 * 1024 or any(len(line) > 2 * 1024 * 1024 for line in lines):
            raise LimitsUnavailable('Codex usage response is too large')
        for line in lines:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                yield message


def codex_rate_limits(executable: str, path: str, timeout: float = 20) -> dict:
    """Ask `codex app-server` (stdio JSON-RPC) for account limits; Codex uses its own login."""
    process = subprocess.Popen([executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, bufsize=0, env={**os.environ, "PATH": path})

    def send(message: dict) -> None:
        process.stdin.write(json.dumps(message).encode() + b"\n")

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {
            "name": "ai-monitor", "title": "ai-monitor", "version": __version__}}})
        for message in json_rpc_messages(process.stdout.fileno(), time.monotonic() + timeout):
            if "error" in message and message.get("id") in (1, 2):
                raise LimitsUnavailable('Codex could not read account usage; check its login')
            if message.get("id") == 1:
                send({"method": "initialized"})
                send({"id": 2, "method": "account/rateLimits/read", "params": {}})
            elif message.get("id") == 2:
                return message.get("result") or {}
        raise LimitsUnavailable("Codex did not answer the usage request")
    except BrokenPipeError:
        raise LimitsUnavailable("Codex app-server exited") from None
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        process.terminate()
        try:
            process.wait(3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


def parse_codex_limits(result: dict, now: float) -> dict:
    rate = (result.get("rateLimitsByLimitId") or {}).get("codex") or result.get("rateLimits") or {}
    resets = result.get("rateLimitResetCredits") or {}
    available = resets.get("availableCount")
    expiries = [credit.get("expiresAt") for credit in resets.get("credits") or []
                if isinstance(credit, dict) and credit.get("status") == "available"
                and isinstance(credit.get("expiresAt"), int)]
    return {"plan": codex_plan(rate.get("planType")),
            "windows": rate_windows(rate, "usedPercent", "windowDurationMins", "resetsAt"),
            "limitReached": rate.get("rateLimitReachedType") is not None,
            "resetsAvailable": available if isinstance(available, int) else None,
            "resetExpiresAt": min(expiries) if expiries else None,
            "fetchedAt": now, "source": "codex", "error": ""}


def claude_plan(oauth: dict) -> str:
    tier = re.search(r"max_(\d+)x", str(oauth.get("rateLimitTier") or ""))
    if tier:
        return f"Max {tier[1]}x"
    subscription = oauth.get("subscriptionType")
    return subscription.title() if isinstance(subscription, str) else ""


def parse_claude_limits(data: dict, oauth: dict, now: float) -> dict:
    windows = []
    for name, minutes in (("five_hour", 300), ("seven_day", 10080)):
        window = data.get(name)
        if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float)):
            windows.append({"minutes": minutes, "usedPercent": window["utilization"],
                            "resetsAt": parse_iso(window.get("resets_at"))})
    return {"plan": claude_plan(oauth), "windows": windows, "fetchedAt": now, "source": "claude", "error": ""}


def claude_limits(credentials: Path, now: float, opener=None) -> dict:
    """The lookup Claude Code's /usage screen makes, with its current login. Read-only:
    an expired login is reported, never refreshed (refresh tokens rotate)."""
    try:
        oauth = json.loads(credentials.read_text()).get("claudeAiOauth") or {}
    except (OSError, ValueError, AttributeError):
        raise LimitsUnavailable("Not signed in to Claude Code") from None
    token, expires = oauth.get("accessToken"), oauth.get("expiresAt")
    if not token:
        raise LimitsUnavailable("Not signed in to Claude Code")
    if isinstance(expires, (int, float)) and expires / 1000 <= now + 60:
        raise LimitsUnavailable("Claude Code login expired; it renews when you use Claude Code")
    request = urllib.request.Request(CLAUDE_USAGE_URL, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json", "User-Agent": f"ai-monitor/{__version__}"})
    if opener is None:
        opener = urllib.request.build_opener(NoRedirects()).open
    try:
        with opener(request, timeout=10) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise LimitsUnavailable('Claude usage response is too large')
            data = json.loads(raw)
    except urllib.error.HTTPError as error:
        if error.code in (403, 429):
            try:
                retry_after = float(error.headers.get("retry-after") or 0)
            except (TypeError, ValueError):
                retry_after = 0
            raise RateLimited(retry_after) from None
        raise LimitsUnavailable(f"Claude usage lookup failed (HTTP {error.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise LimitsUnavailable("Claude usage lookup failed; offline?") from None
    return parse_claude_limits(data if isinstance(data, dict) else {}, oauth, now)


class UsageMonitor:
    def __init__(self, home: Path, state_dir: Path, *, network: bool = True,
                 claude_roots=None, codex_root: Path | None = None) -> None:
        if claude_roots is None:
            config = os.environ.get("CLAUDE_CONFIG_DIR")
            claude_roots = [Path(config) / "projects"] if config else [
                home / ".config" / "claude" / "projects", home / ".claude" / "projects"]
        if codex_root is None:
            codex_root = Path(os.environ.get("CODEX_HOME") or home / ".codex") / "sessions"
        self.home = home
        self.network = network
        self.scanners = {"claude": ClaudeScanner(claude_roots), "codex": CodexScanner(codex_root)}
        self.archive = DayArchive(state_dir / "ai-days")
        self.limits_file = state_dir / "ai-limits.json"
        self.credentials = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude") / ".credentials.json"
        self.errors = dict.fromkeys(TOOLS, "")
        try:
            saved = json.loads(self.limits_file.read_text())
            self.limits = {tool: saved[tool] for tool in ("claude", "codex") if isinstance(saved.get(tool), dict)}
        except (OSError, ValueError, AttributeError):
            self.limits = {}
        self.next_lookup = {"claude": 0.0, "codex": 0.0}
        self.failures = {"claude": 0, "codex": 0}

    def scan(self, now: float) -> None:
        today = dt.date.fromtimestamp(now)
        start = window_start(today)
        self.archive.load(start, today - dt.timedelta(days=1))
        archived = set(self.archive.days)
        # Only days that are not archived yet need their sources read again.
        first = next((day for day in day_range(start, today) if day.isoformat() not in archived), today)
        for tool, scanner in self.scanners.items():
            try:
                scanner.scan(midnight(first), archived)
                self.errors[tool] = ""
            except (OSError, ValueError, TypeError, AttributeError, OverflowError) as error:
                self.errors[tool] = f"Could not read {tool} usage ({type(error).__name__})"
        for day in day_range(first, today - dt.timedelta(days=1)):
            key = day.isoformat()
            if key not in self.archive.days and now >= midnight(day + dt.timedelta(days=1)) + DAY_FINAL_AFTER \
                    and not any(self.errors.values()) and any(
                        any(scanner.days.get(key, zero()).values()) for scanner in self.scanners.values()):
                try:
                    self.archive.save(key, {tool: scanner.days.get(key, zero())
                                            for tool, scanner in self.scanners.items()})
                except OSError:
                    break  # Retried next scan; live counts stay in memory meanwhile.
        live = start.isoformat()
        for scanner in self.scanners.values():
            scanner.forget(lambda day: day >= live and day not in self.archive.days)

    def refresh_limits(self, now: float, active: bool) -> bool:
        if not (self.network and active):
            return False
        lookups = (("claude", lambda: claude_limits(self.credentials, now)), ("codex", self._codex))
        changed = False
        for tool, lookup in lookups:
            if now < self.next_lookup[tool]:
                continue
            changed = True
            try:
                self.limits[tool] = lookup()
                self.failures[tool] = 0
                self.next_lookup[tool] = now + LIMITS_INTERVAL
            except RateLimited as limited:
                self.failures[tool] += 1
                backoff = min(BACKOFF_MAX, LIMITS_INTERVAL * 2 ** (self.failures[tool] - 1))
                self.next_lookup[tool] = now + max(backoff, limited.retry_after)
                self.limits[tool] = {**self.limits.get(tool, {}), "error": str(limited)}
            except LimitsUnavailable as error:
                self.next_lookup[tool] = now + LIMITS_INTERVAL
                self.limits[tool] = {**self.limits.get(tool, {}), "error": str(error)}
        if changed:
            try:
                write_private(self.limits_file, self.limits)
            except OSError:
                pass
        return changed

    def _codex(self) -> dict:
        path = search_path(self.home)
        executable = shutil.which("codex", path=path)
        if not executable:
            raise LimitsUnavailable("codex CLI not found")
        return parse_codex_limits(codex_rate_limits(executable, path), time.time())

    def codex_limits(self) -> dict | None:
        lookup, session = self.limits.get("codex"), self.scanners["codex"].limits
        if not session or (lookup and lookup.get("fetchedAt", 0) >= session["fetchedAt"]):
            return lookup
        # A Codex request after the last lookup has fresher windows; resets come from the lookup.
        merged = dict(session)
        if lookup:
            for name in ("resetsAvailable", "resetExpiresAt", "error"):
                if name in lookup:
                    merged[name] = lookup[name]
        return merged

    def snapshot(self, now: float) -> dict:
        today = dt.date.fromtimestamp(now)
        starts = period_starts(today)
        tokens = {tool: {period: zero() for period in starts} for tool in TOOLS}
        for day in day_range(window_start(today), today):
            key = day.isoformat()
            for tool in TOOLS:
                counts = (self.archive.days[key][tool] if key in self.archive.days
                          else self.scanners[tool].days.get(key))
                for period, start in starts.items():
                    if counts and day >= start:
                        for name in FIELDS:
                            tokens[tool][period][name] += counts[name]
        return {"heartbeat": now, "tokens": tokens, "days": self.daily(now),
                "coverage": "Available local records only; older history may be missing.", "errors": self.errors,
                "limits": {"claude": self.limits.get("claude"), "codex": self.codex_limits()}}
    def daily(self, now):
        today = dt.date.fromtimestamp(now)
        result = []
        for day in day_range(today - dt.timedelta(days=27), today):
            key = day.isoformat()
            values = {tool: (self.archive.days.get(key, {}).get(tool) or scanner.days.get(key, zero()))
                      for tool, scanner in self.scanners.items()}
            result.append({"date": key, "tools": values})
        return result

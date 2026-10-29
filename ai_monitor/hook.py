"""Passive lifecycle hook. It never approves, blocks, or changes agent behavior."""

import json
import codecs
import os
import select
import sys
import time

from .client import request
from .codex_permissions import approval_reviewer
from .terminal import capture
from .paths import private_json, state_dir


def read_payload(stream, timeout=0.6):
    """Read one complete JSON value; the runner need not close stdin first."""
    limit = 2 * 1024 * 1024
    decoder = codecs.getincrementaldecoder('utf-8')()
    parser = json.JSONDecoder()
    buffer = ''
    size = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([stream], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        chunk = os.read(stream.fileno(), min(65536, limit + 1 - size))
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise ValueError('Hook payload too large')
        buffer += decoder.decode(chunk)
        try:
            value, end = parser.raw_decode(buffer.lstrip())
            if not buffer.lstrip()[end:].strip():
                return value
        except ValueError:
            continue
    raise ValueError('Incomplete hook payload')


def diagnostic(tool, event, status):
    try:
        private_json(state_dir() / 'hooks' / f'{tool}.json',
                     {'event': event, 'status': status, 'at': time.time()})
    except OSError:
        pass


def report(tool):
    name = ''
    try:
        payload = read_payload(sys.stdin.buffer)
        if not isinstance(payload, dict):
            return
        name = payload.get('hook_event_name', '')
        terminal = capture(tool)
        if not terminal:
            diagnostic(tool, name, 'no-cli-ancestor')
            return
        background = payload.get("background_tasks")
        event = {"op": "event", "tool": tool, "session": payload.get("session_id"),
                 "event": payload.get("hook_event_name"), "cwd": payload.get("cwd", ""),
                 "title": os.environ.get("AI_MONITOR_NAME", ""),
                 "notification": payload.get("notification_type", ""),
                 "toolName": payload.get("tool_name", ""), "sentAt": time.time(),
                 "transcript": payload.get('transcript_path', ''),
                 "subagent": bool(payload.get("agent_id")),
                 "background": bool(background), "terminal": terminal}
        if tool == 'codex' and not event['subagent'] and name in ('UserPromptSubmit', 'PreToolUse', 'PermissionRequest'):
            turn = payload.get('turn_id')
            if isinstance(turn, str) and 0 < len(turn) <= 256:
                event['turnId'] = turn
            mode = payload.get('permission_mode')
            if mode in ('default', 'acceptEdits', 'plan', 'dontAsk', 'bypassPermissions'):
                event['permissionMode'] = mode
            reviewer = approval_reviewer(payload.get('transcript_path'), turn)
            if reviewer:
                event['approvalsReviewer'] = reviewer
        if name in ('SubagentStart', 'SubagentStop'):
            identifier = payload.get('agent_id')
            if isinstance(identifier, str) and 0 < len(identifier) <= 256:
                event['agentId'] = identifier
        if name == 'Stop' and isinstance(background, list):
            event['backgroundSubagents'] = sum(1 for task in background if isinstance(task, dict)
                and task.get('type') in ('subagent', 'teammate') and task.get('status') not in
                ('completed', 'failed', 'killed', 'cancelled', 'canceled', 'stopped'))
        request(event, timeout=0.3)
        diagnostic(tool, name, 'delivered')
    except (OSError, ValueError, TypeError):
        # Missing/restarting monitors never disrupt the CLI. Keep metadata-only diagnostics.
        diagnostic(tool, name, 'unavailable')

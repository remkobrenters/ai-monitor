"""Event-driven session state. A silent process is never inferred to be idle."""

import hashlib
import math
from pathlib import Path
import re
import threading
import time

from .paths import private_json
from .terminal import alive, visible_views
from . import subagents

ATTENTION = {"approval", "question", "ready"}
MAX_SESSIONS = 512
CODEX_INPUT_CONFIRM_SECONDS = 3


def session_key(tool, session):
    return hashlib.sha256(f"{tool}:{session}".encode()).hexdigest()[:24]


def clean(value, limit=120):
    return "".join(c for c in str(value) if c.isprintable())[:limit]


class Sessions:
    def __init__(self, path, clock=time.time, is_alive=alive):
        self.path, self.clock, self.is_alive = path, clock, is_alive
        self.lock = threading.RLock()
        self.rows = {}
        try:
            import json
            saved = json.loads(path.read_text())
            if isinstance(saved, dict):
                self.rows = {key: row for key, row in saved.items()
                             if isinstance(row, dict) and row.get("tool") in ("claude", "codex")
                             and isinstance(row.get("terminal"), dict)
                             and self.is_alive(row["terminal"].get("process", {}))}
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def event(self, event):
        tool, session = event.get("tool"), event.get("session")
        name = event.get("event")
        if tool not in ("claude", "codex") or not isinstance(session, str) or not session or len(session) > 256:
            raise ValueError("Invalid session identity")
        # Internal subagents share the parent's terminal and must not inflate counts.
        lifecycle = name in subagents.LIFECYCLE
        if event.get("subagent") and not lifecycle:
            return
        key = session_key(tool, session)
        now = self.clock()
        with self.lock:
            row = self.rows.get(key)
            if name == "SessionEnd":
                sent = event.get("sentAt", now)
                terminal = event.get("terminal", {})
                same_process = row and terminal.get("process") == row["terminal"].get("process")
                if row and same_process and isinstance(sent, (int, float)) and sent >= row["eventAt"]:
                    del self.rows[key]
                    self.save()
                return
            terminal = event.get("terminal")
            if not isinstance(terminal, dict) or not self.is_alive(terminal.get("process", {})):
                return
            if name != "Discover":
                # Replace provisional discovery rows once a real hook identifies the session.
                for other_key, other in list(self.rows.items()):
                    if other_key != key and other["tool"] == tool and other.get("discovered") and \
                            other["terminal"]["process"] == terminal["process"]:
                        del self.rows[other_key]
            new_process = not row or row["terminal"]["process"] != terminal["process"]
            if new_process:
                if len(self.rows) >= MAX_SESSIONS:
                    self.prune()
                    if len(self.rows) >= MAX_SESSIONS:
                        return
                cwd = clean(event.get("cwd", ""), 500)
                row = {"id": key, "tool": tool, "title": clean(event.get("title") or Path(cwd).name or tool),
                       "project": cwd, "state": "idle", "since": now, "updated": now,
                       "attention": False, "revision": 0, "terminal": terminal, "eventAt": 0,
                       "discovered": name == "Discover", "sessionId": session,
                       "titleSource": 'custom' if event.get('title') else 'project'}
                self.rows[key] = row
            sent = event.get("sentAt", now)
            if not isinstance(sent, (int, float)) or not math.isfinite(sent):
                raise ValueError("Invalid event timestamp")
            if lifecycle:
                subagents.update(row, event, sent)
                if subagents.count(row) and row['state'] in ('idle', 'unknown', 'ready'):
                    row.update(state='working', since=now, attention=False, revision=row['revision'] + 1)
                row['updated'] = now
                self.save()
                return
            if sent < row["eventAt"]:
                return
            row["eventAt"] = min(sent, now)
            row["terminal"] = terminal
            if tool == 'codex':
                turn = event.get('turnId')
                if isinstance(turn, str) and turn and len(turn) <= 256:
                    if turn != row.get('_codexTurn'):
                        row['_codexTurn'] = turn
                        row.pop('_codexReviewer', None)
                    reviewer = event.get('approvalsReviewer')
                    if reviewer in ('auto_review', 'user'):
                        row['_codexReviewer'] = reviewer
            if event.get("title"):
                row["title"] = clean(event["title"])
                row['titleSource'] = 'custom'
            row['sessionId'] = session
            if event.get('transcript'):
                row['_transcript'] = str(event['transcript'])
            if name in ('SessionStart', 'Interrupt'):
                subagents.reset(row, sent)
                row.pop('_pendingAttention', None)
            if name == 'Stop':
                subagents.snapshot(row, event, sent)
            state = None
            notification = event.get("notification", "")
            tool_name = str(event.get("toolName", ""))
            if name == "Discover":
                state = "unknown"
            elif name in ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure"):
                state = "working"
            elif name == "PreToolUse":
                question = 'request_user_input' if tool == 'codex' else 'AskUserQuestion'
                state = 'question' if tool_name.rsplit('.', 1)[-1] == question else 'working'
            elif name == "PermissionRequest":
                reviewer = event.get('approvalsReviewer')
                if reviewer is None and event.get('turnId') and event['turnId'] == row.get('_codexTurn'):
                    reviewer = row.get('_codexReviewer')
                automatic = tool == 'codex' and (reviewer == 'auto_review' or
                    event.get('permissionMode') in ('dontAsk', 'bypassPermissions'))
                # PermissionRequest runs before other approval hooks and automatic
                # review. It is not proof that a human prompt will be displayed.
                state = ('question' if row['state'] == 'question' else 'working') if automatic else 'approval'
            elif name == "Stop":
                state = "working" if event.get("background") else "ready"
            elif name == "Interrupt":
                state = "idle"
            elif name == "Notification":
                state = {"permission_prompt": "approval", "idle_prompt": "ready",
                         "elicitation_dialog": "question", "elicitation_url_dialog": "question",
                         "agent_needs_input": "question", "elicitation_complete": "working"}.get(notification)
            if tool == 'codex' and state in ('approval', 'question'):
                if state != row['state']:
                    pending = row.get('_pendingAttention')
                    if not pending or pending['state'] != state:
                        row['_pendingAttention'] = {'state': state, 'due': now + CODEX_INPUT_CONFIRM_SECONDS}
                    state = 'working'
            elif state:
                row.pop('_pendingAttention', None)
            if state == 'ready' and subagents.count(row):
                state = 'working'
                row['waitingForSubagents'] = True
            elif name == 'Stop':
                row['waitingForSubagents'] = bool(subagents.count(row))
            elif name in ('UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure'):
                row['waitingForSubagents'] = bool(name == 'PreToolUse' and subagents.count(row) and
                    re.search(r'(?:^|[._])(?:wait|wait_agent|wait_for_agents|wait_for_subagents)$', tool_name))
            if state and state != row["state"]:
                row.update(state=state, since=now, attention=state in ATTENTION,
                           revision=row["revision"] + 1)
            row["updated"] = now
            self.save()

    def discover(self, candidates):
        self.prune()
        for candidate in candidates:
            identity = candidate["terminal"]["process"]
            with self.lock:
                matches = [row for row in self.rows.values() if row['tool'] == candidate['tool'] and
                           row['terminal']['process'] == identity]
                if matches:
                    changed = False
                    for row in matches:
                        if row['terminal'] != candidate['terminal']:
                            row['terminal'] = candidate['terminal']
                            changed = True
                    if changed:
                        self.save()
                    continue
                self.event({**candidate, "cwd": candidate["project"], "event": "Discover",
                            "session": f"process:{identity['pid']}:{identity['start']}"})

    def resolve_terminal(self, event, candidates):
        """Hook processes may belong to app-server, not the terminal client."""
        key = session_key(event.get("tool"), event.get("session"))
        with self.lock:
            existing = self.rows.get(key)
            if existing and self.is_alive(existing["terminal"].get("process", {})):
                return existing["terminal"]
        own = [row for row in candidates if row["tool"] == event.get("tool") and
               row["project"] == event.get("cwd")]
        if len(own) == 1:
            return own[0]["terminal"]
        if event.get("event") == "SessionStart":
            from .terminal import recently_started
            recent = [row for row in own if recently_started(row["terminal"]["process"])]
            if len(recent) == 1:
                return recent[0]["terminal"]
        return None

    def refresh_titles(self, titles):
        with self.lock:
            changed = False
            for key, value in titles.items():
                row = self.rows.get(key)
                if row and row.get('titleSource') != 'custom' and row['title'] != clean(value):
                    row['title'] = clean(value)
                    row['titleSource'] = 'session'
                    changed = True
            if changed:
                self.save()

    def title_records(self):
        with self.lock:
            return [dict(row) for row in self.rows.values() if row.get('titleSource') != 'custom']

    def acknowledge(self, key=None):
        with self.lock:
            for row in self.rows.values():
                if key is None or row["id"] == key:
                    row["attention"] = False
                    row.pop('_pendingAttention', None)
            self.save()

    def prune(self):
        with self.lock:
            removed = [key for key, row in self.rows.items()
                       if not self.is_alive(row["terminal"].get("process", {}))]
            for key in removed:
                del self.rows[key]
            if removed:
                self.save()

    def get(self, key):
        with self.lock:
            if key not in self.rows:
                raise ValueError("This session has ended")
            return dict(self.rows[key])

    def snapshot(self):
        self.prune()
        with self.lock:
            changed = False
            now = self.clock()
            for row in self.rows.values():
                pending = row.get('_pendingAttention')
                if pending and pending['due'] <= now:
                    row.update(state=pending['state'], since=now, updated=now, attention=True,
                               revision=row['revision'] + 1)
                    del row['_pendingAttention']
                    changed = True
            if changed:
                self.save()
            rows = [dict(row) for row in sorted(self.rows.values(), key=lambda row:
                                               (not row["attention"], row["tool"], -row["since"]))]
        # Query each tmux server once, outside the event lock.
        cache = {}
        return [{**{k: v for k, v in row.items() if not k.startswith('_') and k not in ("terminal", "eventAt", "sessionId")},
                 "windowId": row["terminal"].get("windowId", ""),
                 "subagentCount": subagents.count(row),
                 "views": visible_views(row["terminal"], cache)} for row in rows]

    def save(self):
        private_json(self.path, self.rows)

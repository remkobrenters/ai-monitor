"""Read effective review routing from exact-turn metadata, without retaining its contents."""

import json
import os
from pathlib import Path

MAX_CONTEXT_BYTES = 1024 * 1024


def approval_reviewer(transcript, turn_id, codex_home=None):
    if not isinstance(transcript, str) or not transcript or not isinstance(turn_id, str) or not turn_id:
        return None
    try:
        home = Path(codex_home or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        root = (home / 'sessions').resolve()
        path = Path(transcript).resolve()
        if not path.is_relative_to(root) or path.suffix != '.jsonl':
            return None
        with path.open('rb') as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - MAX_CONTEXT_BYTES)
            stream.seek(start)
            if start:
                stream.readline()  # Discard the partial first record.
            lines = stream.read(MAX_CONTEXT_BYTES).splitlines()
        for line in reversed(lines):
            if b'"turn_context"' not in line:
                continue
            try:
                record = json.loads(line)
                context = record.get('payload')
                if record.get('type') != 'turn_context' or not isinstance(context, dict):
                    continue
                if context.get('turn_id') != turn_id:
                    continue
                reviewer = context.get('approvals_reviewer')
                return reviewer if reviewer in ('auto_review', 'user') else None
            except (ValueError, AttributeError):
                return None
    except (OSError, ValueError, TypeError):
        pass
    return None

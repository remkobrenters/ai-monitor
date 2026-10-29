"""Read native session names by exact session ID; never infer names from a project."""

import json
import os
from pathlib import Path
import re
import sqlite3

from .sessions import session_key


class Titles:
    def __init__(self, codex_home=None, claude_home=None):
        self.codex = Path(codex_home or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        self.claude = Path(claude_home or os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
        self.legacy_claude = claude_home is None and 'CLAUDE_CONFIG_DIR' not in os.environ
        self.names = {}
        self.index_state = None
        self.transcripts = {}
        self.recovered = {}

    def codex_names(self, rows):
        index = self.codex / 'session_index.jsonl'
        try:
            stat = index.stat()
            stamp = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if stamp != self.index_state:
                names = {}
                with index.open() as stream:
                    for line in stream:
                        try:
                            value = json.loads(line)
                            if isinstance(value.get('id'), str) and value.get('thread_name'):
                                names[value['id']] = value['thread_name']
                        except (ValueError, AttributeError):
                            continue
                self.names = names
                self.index_state = stamp
        except OSError:
            pass
        result = {}
        identifiers = {}
        for row in rows:
            identifier = row.get('sessionId')
            # Older saved sessions only stored a hash. Recover IDs from the title index.
            if not identifier:
                identifier = next((value for value in self.names if session_key('codex', value) == row['id']), None)
            if identifier and not identifier.startswith('process:'):
                identifiers[identifier] = row['id']
        try:
            databases = sorted(self.codex.glob('state_*.sqlite'), key=lambda p: int(p.stem.split('_')[-1]), reverse=True)
            if databases and identifiers:
                connection = sqlite3.connect(f'{databases[0].as_uri()}?mode=ro', uri=True, timeout=0.2)
                try:
                    columns = {entry[1] for entry in connection.execute('PRAGMA table_info(threads)')}
                    expression = "COALESCE(NULLIF(name, ''), title)" if 'name' in columns else 'title'
                    placeholders = ','.join('?' for _ in identifiers)
                    for identifier, title in connection.execute(
                            f'SELECT id, {expression} FROM threads WHERE id IN ({placeholders})', list(identifiers)):
                        if title:
                            result[identifiers[identifier]] = title
                finally:
                    connection.close()
        except (OSError, ValueError, sqlite3.Error):
            pass
        for identifier, key in identifiers.items():
            if self.names.get(identifier):
                result[key] = self.names[identifier]
        return result

    def claude_name(self, row):
        roots = [self.claude / 'projects']
        if self.legacy_claude:
            roots.append(Path.home() / '.config/claude/projects')
        identifier = row.get('sessionId') or self.recovered.get(row['id'], '')
        if not identifier:
            # Migrate hashes from the first release using filenames, not conversation contents.
            for root in roots:
                found = next((path.stem for path in root.glob('*/*.jsonl')
                              if session_key('claude', path.stem) == row['id']), None)
                if found:
                    identifier = self.recovered[row['id']] = found
                    break
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', identifier):
            return None
        paths = []
        if row.get('_transcript'):
            candidate = Path(row['_transcript']).resolve()
            if any(candidate.is_relative_to(root.resolve()) for root in roots):
                paths.append(candidate)
        if not paths:
            for root in roots:
                paths.extend(root.glob(f'*/{identifier}.jsonl'))
        for path in paths:
            try:
                index = path.parent / 'sessions-index.json'
                summary = None
                if index.exists():
                    entries = json.loads(index.read_text()).get('entries', [])
                    for entry in entries:
                        if entry.get('sessionId') == identifier:
                            if entry.get('customTitle'):
                                return entry['customTitle']
                            summary = entry.get('summary')
                stat = path.stat()
                seen = self.transcripts.setdefault(path, {'inode': stat.st_ino, 'offset': 0, 'title': None, 'summary': None})
                if stat.st_ino != seen['inode'] or stat.st_size < seen['offset']:
                    seen.update(inode=stat.st_ino, offset=0, title=None, summary=None)
                with path.open('rb') as stream:
                    stream.seek(seen['offset'])
                    # Incremental metadata scan. Message bodies are never saved or returned.
                    for line in stream:
                        if not line.endswith(b'\n'):
                            break
                        seen['offset'] += len(line)
                        if b'"custom-title"' not in line and b'"summary"' not in line:
                            continue
                        try:
                            value = json.loads(line)
                            if value.get('type') == 'custom-title' and value.get('customTitle') and \
                                    value.get('sessionId', identifier) == identifier:
                                seen['title'] = value['customTitle']
                            elif value.get('type') == 'summary' and isinstance(value.get('summary'), str):
                                seen['summary'] = value['summary']
                        except (ValueError, AttributeError):
                            continue
                if seen['title']:
                    return seen['title']
                if summary:
                    return summary
                if seen['summary']:
                    return seen['summary']
            except (OSError, ValueError, TypeError):
                continue
        return None

    def lookup(self, rows):
        result = self.codex_names([row for row in rows if row['tool'] == 'codex'])
        for row in rows:
            if row['tool'] == 'claude':
                title = self.claude_name(row)
                if title:
                    result[row['id']] = title
        return result

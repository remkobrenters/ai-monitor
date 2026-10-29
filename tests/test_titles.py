import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ai_monitor.sessions import session_key
from ai_monitor.titles import Titles


class TitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = self.root / 'codex'
        self.claude = self.root / 'claude'
        self.codex.mkdir()
        self.claude.mkdir()
        self.titles = Titles(self.codex, self.claude)

    def row(self, tool, identifier):
        return {'id': session_key(tool, identifier), 'sessionId': identifier, 'tool': tool, 'project': '/same-project'}

    def test_codex_names_match_exact_session_even_in_the_same_project(self):
        a, b = self.row('codex', 'id-a'), self.row('codex', 'id-b')
        index = self.codex / 'session_index.jsonl'
        index.write_text(json.dumps({'id': 'id-a', 'thread_name': 'Fix checkout'}) + '\n' +
                         json.dumps({'id': 'id-b', 'thread_name': 'Review worker'}) + '\n')
        self.assertEqual(self.titles.lookup([a, b]), {a['id']: 'Fix checkout', b['id']: 'Review worker'})
        with index.open('a') as stream:
            stream.write(json.dumps({'id': 'id-a', 'thread_name': 'Renamed checkout session'}) + '\n')
        self.assertEqual(self.titles.lookup([a])[a['id']], 'Renamed checkout session')

    def test_old_saved_codex_session_hash_recovers_native_name(self):
        a = self.row('codex', 'id-a')
        del a['sessionId']
        (self.codex / 'session_index.jsonl').write_text(json.dumps({'id': 'id-a', 'thread_name': 'Actual name'}) + '\n')
        self.assertEqual(self.titles.lookup([a]), {a['id']: 'Actual name'})

    def test_codex_readonly_database_uses_name_before_title(self):
        database = self.codex / 'state_5.sqlite'
        connection = sqlite3.connect(database)
        connection.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, name TEXT)')
        connection.executemany('INSERT INTO threads VALUES (?, ?, ?)', [('a', 'Old title', 'Session name'),
                                                                     ('b', 'Generated session title', None)])
        connection.commit()
        connection.close()
        rows = [self.row('codex', identifier) for identifier in ('a', 'b')]
        self.assertEqual(self.titles.lookup(rows), {rows[0]['id']: 'Session name', rows[1]['id']: 'Generated session title'})

    def test_claude_custom_titles_and_renames_ignore_conversation_bodies(self):
        row = self.row('claude', 'claude-session')
        directory = self.claude / 'projects/project'
        directory.mkdir(parents=True)
        path = directory / 'claude-session.jsonl'
        path.write_text(json.dumps({'type': 'assistant', 'message': {'text': 'private prompt text'}}) + '\n' +
                        json.dumps({'type': 'custom-title', 'sessionId': 'claude-session', 'customTitle': 'Review migrations'}) + '\n')
        self.assertEqual(self.titles.lookup([row]), {row['id']: 'Review migrations'})
        with path.open('a') as stream:
            stream.write(json.dumps({'type': 'custom-title', 'sessionId': 'claude-session', 'customTitle': 'Migrations reviewed'}) + '\n')
        self.assertEqual(self.titles.lookup([row]), {row['id']: 'Migrations reviewed'})
        self.assertNotIn('private', json.dumps(self.titles.lookup([row])))

    def test_claude_partial_metadata_waits_and_renames_override_stale_summary(self):
        row = self.row('claude', 'a')
        directory = self.claude / 'projects/project'
        directory.mkdir(parents=True)
        path = directory / 'a.jsonl'
        path.write_text('{"type":"custom-title","customTitle":"New name"}')
        (directory / 'sessions-index.json').write_text(json.dumps({'entries': [{'sessionId': 'a', 'summary': 'Old summary'}]}))
        self.assertEqual(self.titles.lookup([row])[row['id']], 'Old summary')
        with path.open('a') as stream:
            stream.write('\n')
        self.assertEqual(self.titles.lookup([row])[row['id']], 'New name')

    def test_missing_titles_do_not_borrow_names_from_other_sessions(self):
        (self.codex / 'session_index.jsonl').write_text(json.dumps({'id': 'other', 'thread_name': 'Wrong name'}) + '\n')
        self.assertEqual(self.titles.lookup([self.row('codex', 'not-indexed')]), {})

    def test_old_claude_hash_recovers_name_from_exact_transcript_filename(self):
        row = self.row('claude', 'old-session')
        del row['sessionId']
        directory = self.claude / 'projects/project'
        directory.mkdir(parents=True)
        (directory / 'old-session.jsonl').write_text('{"type":"custom-title","customTitle":"Original Claude session"}\n')
        self.assertEqual(self.titles.lookup([row]), {row['id']: 'Original Claude session'})

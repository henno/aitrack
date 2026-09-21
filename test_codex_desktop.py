"""Codex desktop regression tests; synthetic logs, no services or credentials."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aitrack as A

UTC = dt.timezone.utc
SINCE = dt.datetime(2026, 9, 21, 6, tzinfo=UTC)


def event(kind, payload, timestamp='2026-09-21T06:15:00.125Z'):
    return {'type': kind, 'timestamp': timestamp, 'payload': payload}


def message(role, text, timestamp='2026-09-21T06:15:00.125Z'):
    return event('response_item', {'type': 'message', 'role': role,
                 'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}]}, timestamp)


class CodexDesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.history = self.root / 'history.jsonl'
        for name, value in [('CODEX_SESSIONS', self.sessions), ('CODEX_HISTORY', self.history)]:
            p = patch.object(A, name, value)
            p.start()
            self.addCleanup(p.stop)

    def rollout(self, items, sid='desktop', cwd='/work/Placements', source='vscode'):
        path = self.sessions / ('rollout-' + sid + '.jsonl')
        meta = event('session_meta', {'id': sid, 'cwd': cwd, 'source': source})
        path.write_text('\n'.join(json.dumps(x) for x in [meta] + items) + '\n')
        return path

    def transcripts(self):
        with patch.object(A, 'claude_transcript_records', return_value=[]), \
             patch.object(A, 'pi_transcript_records', return_value=[]), \
             patch.object(A, 'opencode_transcript_records', return_value=[]), \
             patch.object(A, 'antigravity_records', return_value=[]):
            return A.collect_transcript_records(SINCE)

    def test_desktop_without_history_and_assistant_reply(self):
        self.rollout([message('user', 'Paranda automaatne logimine'),
                      message('assistant', 'Logide lugeja on parandatud.')])
        records = A.codex_records(SINCE)
        self.assertEqual([(r.project, r.text) for r in records],
                         [('/work/Placements', 'Paranda automaatne logimine')])
        self.assertEqual([(r.role, r.text) for r in self.transcripts()],
                         [('user', 'Paranda automaatne logimine'),
                          ('assistant', 'Logide lugeja on parandatud.')])

    def test_deduplicate_history_response_and_event_preserve_repeated_turns(self):
        self.rollout([message('user', 'Jätka'),
                      event('event_msg', {'type': 'user_message', 'message': 'Jätka'}),
                      message('assistant', 'Valmis'),
                      event('event_msg', {'type': 'agent_message', 'message': 'Valmis'}),
                      message('user', 'Jätka', '2026-09-21T06:17:00Z')])
        self.history.write_text(json.dumps({'session_id': 'desktop',
            'ts': int(dt.datetime(2026, 9, 21, 6, 15, tzinfo=UTC).timestamp()), 'text': 'Jätka'}) + '\n')
        self.assertEqual(len(A.codex_records(SINCE)), 2)
        self.assertEqual([r.role for r in self.transcripts()], ['user', 'assistant', 'user'])

    def test_context_filtering_guardian_and_malformed_lines(self):
        path = self.rollout([
            message('user', '# AGENTS.md instructions\nInternal configuration'),
            message('user', '<environment_context>internal</environment_context>'),
            message('developer', 'Internal instruction'),
            event('response_item', {'type': 'reasoning', 'text': 'private reasoning'}),
            event('response_item', {'type': 'message', 'role': 'assistant', 'channel': 'analysis',
                                   'content': [{'type': 'output_text', 'text': 'private analysis'}]}),
            event('response_item', {'type': 'function_call_output', 'output': 'tool data'}),
            message('user', '<in-app-browser-context>context</in-app-browser-context>\nParanda nupp'),
            message('assistant', 'Nupp parandatud.')])
        with path.open('a') as f:
            f.write('null\n[]\n{"type":"response_item","payload":[] }\n{"unfinished":')
        self.rollout([message('user', 'Approve this action')], sid='guardian',
                     source={'subagent': {'other': 'guardian'}})
        self.assertEqual([r.text for r in A.codex_records(SINCE)], ['Paranda nupp'])
        self.assertEqual([r.text for r in self.transcripts()], ['Paranda nupp', 'Nupp parandatud.'])

    def test_turn_context_time_window_and_unknown_project(self):
        self.rollout([message('user', 'Old', '2026-09-21T05:59:00Z'),
                      message('user', 'Boundary', '2026-09-21T06:00:00Z'),
                      event('turn_context', {'cwd': '/work/Placements/aitrack'}),
                      message('user', 'New')])
        self.rollout([message('user', 'Missing project')], sid='unknown', cwd='')
        self.assertEqual([(r.project, r.text) for r in A.codex_records(SINCE)],
                         [('/work/Placements/aitrack', 'New')])

    def test_legacy_history_and_event_only_rollouts(self):
        self.rollout([], sid='cli', source='cli')
        self.rollout([event('event_msg', {'type': 'user_message', 'message': 'Desktop event'})])
        self.history.write_text('broken\n' + json.dumps({'session_id': 'cli',
            'ts': int(dt.datetime(2026, 9, 21, 6, 20, tzinfo=UTC).timestamp()), 'text': 'CLI history'}) + '\n')
        self.assertEqual([r.text for r in A.codex_records(SINCE)], ['Desktop event', 'CLI history'])

    def test_hourly_pipeline_allowlist_and_completed_hours(self):
        self.rollout([message('user', 'Fix logging'), message('assistant', 'Logging fixed'),
                      message('user', 'Current hour', '2026-09-21T07:10:00Z')])
        self.rollout([message('user', 'Unrelated')], sid='outside', cwd='/private/project')
        transcripts = self.transcripts()
        records = A.codex_records(SINCE)
        cfg = {'timezone': 'Europe/Tallinn', 'group_by': 'hour', 'sink': {'type': 'local'}}
        with patch.object(A, 'load_state', return_value={'last_processed_hour': SINCE.isoformat()}), \
             patch.object(A, '_now_utc', return_value=dt.datetime(2026, 9, 21, 7, 15, tzinfo=UTC)), \
             patch.object(A, '_migrate_old_log'), patch.object(A, 'resolve_engine', return_value=('none', None)), \
             patch.object(A, 'collect_records', return_value=records), \
             patch.object(A, 'collect_transcript_records', return_value=transcripts), \
             patch.object(A, 'load_notes', return_value={}), patch.object(A, 'heartbeat_lock'), \
             patch.object(A, 'log'), patch.object(A, 'save_state'), \
             patch.object(A, 'summarize', return_value=dict.fromkeys(A.ITEM_FIELDS, 'summary')) as summary, \
             patch.object(A, 'append_rows', return_value=True) as append:
            A._run_once_locked(cfg, ['/work/Placements'], None)
        self.assertEqual(summary.call_count, 1)
        inputs = ' '.join(summary.call_args.args[0])
        self.assertIn('Fix logging', inputs)
        self.assertIn('Logging fixed', inputs)
        self.assertNotIn('Unrelated', inputs)
        self.assertNotIn('Current hour', inputs)
        self.assertEqual(append.call_args.args[0][0][1], '09:00–10:00')


if __name__ == '__main__':
    unittest.main()

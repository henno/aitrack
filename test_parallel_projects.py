"""Regression coverage for parallel hourly work and preserved diary summaries."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aitrack as A

UTC = dt.timezone.utc
START = dt.datetime(2026, 9, 21, 8, tzinfo=UTC)
DATE = '2026-09-21'


class ParallelProjectTests(unittest.TestCase):
    def run_hour(self, records, transcripts, group_by='hour', existing=None):
        cfg = {'timezone': 'Europe/Tallinn', 'group_by': group_by,
               'max_prompts_per_bucket': 2, 'sink': {'type': 'local'},
               'object_names': {'alpha': 'Alpha', 'beta': 'Beta'}}
        def summarize(inputs, project, hour, config):
            return {'objekt': 'Changed ' + project, 'saavutus': 'Completed ' + project,
                    'takistus': A._NA, 'teadmine': 'Learned ' + project}
        with patch.object(A, 'load_state', return_value={'last_processed_hour': START.isoformat()}), \
             patch.object(A, '_now_utc', return_value=START + dt.timedelta(hours=1, minutes=5)), \
             patch.object(A, '_migrate_old_log'), patch.object(A, 'resolve_engine', return_value=('none', None)), \
             patch.object(A, 'collect_records', return_value=records), \
             patch.object(A, 'collect_transcript_records', return_value=transcripts), \
             patch.object(A, 'load_notes', return_value={}), patch.object(A, 'heartbeat_lock'), \
             patch.object(A, 'log'), patch.object(A, 'save_state'), \
             patch.object(A, 'fetch_existing_keys', return_value=existing), \
             patch.object(A, 'summarize', side_effect=summarize) as summary, \
             patch.object(A, 'append_rows', return_value=True) as append:
            A._run_once_locked(cfg, ['/projects/alpha', '/projects/beta'], 1 if existing is not None else None)
        return summary, append

    def test_busy_project_cannot_crowd_out_quiet_project(self):
        transcripts = [A.TranscriptRecord('Codex', '/projects/alpha', START, 'assistant', 'Alpha ' + str(i))
                       for i in range(10)]
        transcripts.append(A.TranscriptRecord('Codex', '/projects/beta', START, 'assistant', 'Beta finished'))
        summary, append = self.run_hour([], transcripts)
        self.assertEqual(summary.call_count, 2)
        self.assertEqual([c.args[1] for c in summary.call_args_list], ['alpha', 'beta'])
        self.assertIn('Beta finished', ' '.join(summary.call_args_list[1].args[0]))
        self.assertLessEqual(len(summary.call_args_list[0].args[0]), 4)
        rows, keys, _ = append.call_args.args
        self.assertEqual(len(rows), 1)
        self.assertEqual(keys, [A.bucket_key(START, None)])
        for col in (2, 3, 4, 5):
            self.assertIn('Alpha:', rows[0][col])
            self.assertIn('Beta:', rows[0][col])
        self.assertEqual(A._day_row(DATE, [rows[0] + [keys[0]]])[1], 1)

    def test_project_without_transcript_keeps_its_prompt(self):
        records = [A.Record('Claude', '/projects/beta', START, 'Beta task')]
        transcripts = [A.TranscriptRecord('Codex', '/projects/alpha', START, 'assistant', 'Alpha result')]
        summary, _ = self.run_hour(records, transcripts)
        self.assertEqual(summary.call_count, 2)
        self.assertEqual(summary.call_args_list[1].args[0], ['Beta task'])

    def test_project_mode_and_backfill_keys_stay_compatible(self):
        records = [A.Record('Codex', p, START, 'task') for p in ['/projects/alpha', '/projects/beta']]
        summary, append = self.run_hour(records, [], group_by='project')
        self.assertEqual(summary.call_count, 2)
        self.assertEqual(append.call_args.args[1], [A.bucket_key(START, p) for p in ['/projects/alpha', '/projects/beta']])
        summary, append = self.run_hour(records, [], existing={A.bucket_key(START, None)})
        summary.assert_not_called()
        append.assert_not_called()


class ServerSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'server.db'
        self.token = A._db_add_user(self.db, 'test-user')
        A._db_allow_projects(self.db, self.token, {'projects': [
            {'root_path': '/projects', 'local_path': '/projects', 'project_key': 'local:projects', 'name': 'projects'}]})
        self.row = [DATE, '11:00–12:00', 'aitrack - parallel project summaries',
                    'Implemented project grouping; tested raw_events; documented server token behavior',
                    A._NA, A._NA, 'Codex']
        self.key = A.bucket_key(START, None)

    def add_event(self, name, text=''):
        A._db_ingest_events(self.db, self.token, [{'event_key': 'event-' + name, 'tool': 'Codex',
            'project': '/projects/' + name, 'project_key': 'local:' + name, 'project_name': name,
            'prompt_text': text, 'started_at': START.isoformat(),
            'ended_at': (START + dt.timedelta(minutes=1)).isoformat(), 'duration_seconds': 60}])

    def test_metadata_must_not_replace_completed_summary(self):
        A._db_ingest_rows(self.db, self.token, [self.row], [self.key])
        self.add_event('diarabooks')
        rows = A._db_rows_for_day(self.db, self.token, DATE, {'timezone': 'Europe/Tallinn'})
        self.assertEqual(rows, [self.row + [self.key]])

    def test_real_prompt_does_not_override_or_reinterpret_saved_summary(self):
        A._db_ingest_rows(self.db, self.token, [self.row], [self.key])
        self.add_event('diarabooks', 'paranda activity mittekuvamine')
        rows = A._db_rows_for_day(self.db, self.token, DATE, {'timezone': 'Europe/Tallinn'})
        self.assertEqual(rows, [self.row + [self.key]])

    def test_partial_model_failure_keeps_other_project_summary(self):
        row = self.row[:]
        row[2] = '• Alpha: Invoice validation completed\n• Beta: AI-toega tööülesannete lahendamine (2 teemat)'
        row[3] = '• Alpha: Validation tests pass\n• Beta: Tegelesin AI-tööriista abil 2 tööteemaga'
        A._db_ingest_rows(self.db, self.token, [row], [self.key])
        self.add_event('beta', 'Loo arve')
        rows = A._db_rows_for_day(self.db, self.token, DATE, {'timezone': 'Europe/Tallinn'})
        self.assertEqual(rows, [row + [self.key]])

    def test_missing_summary_includes_both_projects_without_double_hours(self):
        self.add_event('alpha', 'Loo kliendile arve')
        self.add_event('beta', 'Kontrolli e-posti seadistusi')
        rows = A._db_rows_for_day(self.db, self.token, DATE, {'timezone': 'Europe/Tallinn'})
        self.assertEqual(len(rows), 1)
        for col in (2, 3, 4, 5):
            self.assertIn('alpha:', rows[0][col])
            self.assertIn('beta:', rows[0][col])
        self.assertEqual(A._day_row(DATE, rows)[1], 1)

    def test_explicit_placeholder_can_still_use_substantive_prompt(self):
        placeholder = self.row[:]
        placeholder[2] = placeholder[3] = '(1 prompt, automaatkokkuvõte puudub)'
        A._db_ingest_rows(self.db, self.token, [placeholder], [self.key])
        self.add_event('alpha', 'selgita Tailscale ühendust')
        rows = A._db_rows_for_day(self.db, self.token, DATE, {'timezone': 'Europe/Tallinn'})
        self.assertNotIn('automaatkokkuvõte puudub', rows[0][2])
        self.assertIn('Tailscale', rows[0][2])


if __name__ == '__main__':
    unittest.main()

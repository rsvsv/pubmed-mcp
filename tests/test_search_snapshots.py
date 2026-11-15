"""Context-efficient search. All IDs, counts and limits are 本项目自订测试输入.
These tests compare complete pages and warnings, not literature relevance or model behavior.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import anyio

spec = importlib.util.spec_from_file_location('snapshot_subject', Path(__file__).resolve().parents[1] / 'pubmed_mcp.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


class SearchSnapshots(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.page = {'_term': 'fixture', 'count': '5', 'idlist': ['22', '11', '33'], 'retstart': '0',
                     'querytranslation': 'fixture[All Fields]', 'translationset': [{'from': 'fixture', 'to': 'fixture[All Fields]'}],
                     '_daterange': 'fixture-date-window', '_windows': [{'source': 'fixture'}],
                     '_request_params': {'sort': 'relevance', 'api_key': 'fixture-secret'}}
        self.records = [(pid, {'title': 'Full title ' + pid, 'pubdate': '2026', 'source': 'Journal',
                              'pubtype': ['Journal Article'], 'authors': [{'name': 'A'}]}) for pid in self.page['idlist']]
        self.search = AsyncMock(return_value=self.page)
        self.summary = AsyncMock(return_value=self.records)
        for name, value in (('_esearch', self.search), ('_esummary_dicts', self.summary)):
            hook = patch.object(m, name, value)
            hook.start()
            self.addCleanup(hook.stop)

    def call(self, name, **kwargs):
        return json.loads(run(m.search, 'fixture', as_json=True, out_path=str(self.root / name), **kwargs))

    def snapshot(self, result):
        return json.loads(Path(result['path']).read_text(encoding='utf-8'))

    def test_same_page_queries_records_warnings_and_pagination(self):
        baseline = json.loads(run(m.search, 'fixture', as_json=True))
        first_calls = (self.search.call_args, self.summary.call_args)
        saved = self.call('first.json', max_show=1)
        self.assertEqual((self.search.call_args, self.summary.call_args), first_calls)
        self.assertEqual(self.search.await_count, 2)
        self.assertEqual(self.summary.await_count, 2)
        full = self.snapshot(saved)['result']
        # Audit completion is attached after file publication, never predeclared inside the file.
        for key in baseline.keys() - {'records', 'audit_status'}:
            self.assertEqual(full[key], baseline[key], key)
        self.assertEqual(saved['audit_status'], baseline['audit_status'])
        self.assertEqual(full['search_pmids'], ['22', '11', '33'])
        self.assertEqual([r['pmid'] for r in full['records']], full['search_pmids'])
        self.assertEqual(full['records'], [m._record_parts(*pair) for pair in self.records])
        self.assertEqual(saved['records'], baseline['records'][:1])
        self.assertEqual(saved['returned'], 3)
        self.assertEqual(saved['displayed'], 1)
        self.assertEqual(saved['deferred_pmids'], ['11', '33'])
        self.assertEqual(saved['next_retstart'], 3)
        self.assertTrue(saved['has_more'])
        self.assertNotIn('fixture-secret', Path(saved['path']).read_text(encoding='utf-8'))

    def test_only_displayed_unchanged_records_are_suppressed(self):
        first = self.call('first.json', max_show=1)
        second = self.call('second.json', previous_path=first['path'], max_show=1)
        self.assertEqual([r['pmid'] for r in second['records']], ['11'])
        self.assertEqual(second['unchanged_count'], 1)
        self.assertEqual(second['deferred_pmids'], ['33'])
        third = self.call('third.json', previous_path=second['path'])
        self.assertEqual([r['pmid'] for r in third['records']], ['33'])
        final = self.call('final.json', previous_path=third['path'])
        self.assertEqual(final['records'], [])
        self.assertEqual(final['count'], 5)
        self.assertEqual(final['returned'], 3)
        self.assertEqual(final['unchanged_count'], 3)
        self.assertEqual(final['deferred_count'], 0)
        self.assertTrue(final['has_more'])
        self.assertEqual(len(self.snapshot(final)['result']['records']), 3)

    def test_changed_record_is_shown_and_retraction_always_warns(self):
        first = self.call('first.json')
        self.records[0][1]['pubtype'].append('Retracted Publication')
        second = self.call('second.json', previous_path=first['path'])
        self.assertEqual([r['pmid'] for r in second['records']], ['22'])
        self.assertIn('Retracted Publication', '\n'.join(second['warnings']))
        third = self.call('third.json', previous_path=second['path'])
        self.assertFalse(third['records'])
        self.assertIn('Retracted Publication', '\n'.join(third['warnings']))
        self.records[1][1]['title'] = 'Corrected title'
        fourth = self.call('fourth.json', previous_path=third['path'])
        self.assertEqual([r['pmid'] for r in fourth['records']], ['11'])

    def test_hidden_citation_change_is_presented_before_suppression(self):
        self.records[0][1]['articleids'] = [{'idtype': 'doi', 'value': 'fixture-old-doi'}]
        first = self.call('first.json', detail='full')
        self.assertEqual(first['records'][0]['doi'], 'fixture-old-doi')
        self.records[0][1]['articleids'][0]['value'] = 'fixture-corrected-doi'
        deferred = self.call('deferred.json', previous_path=first['path'], max_show=0)
        self.assertIn('22', deferred['deferred_pmids'])
        second = self.call('second.json', previous_path=deferred['path'])
        self.assertEqual(second['records'][0]['doi'], 'fixture-corrected-doi')
        self.assertEqual(second['refreshed_pmids'], ['22'])
        self.assertEqual(self.snapshot(second)['display_history']['22']['detail'], 'full')
        third = self.call('third.json', previous_path=second['path'])
        self.assertEqual(third['records'], [])

    def test_missing_metadata_remains_visible_and_is_not_seen(self):
        self.summary.return_value = self.records[:1]
        first = self.call('first.json')
        second = self.call('second.json', previous_path=first['path'])
        self.assertEqual(second['missing_pmids'], ['11', '33'])
        self.assertEqual(second['esummary_missing'], 2)
        self.assertEqual(second['returned'], 1)
        self.assertEqual(second['unchanged_count'], 1)
        self.assertEqual(set(self.snapshot(second)['display_history']), {'22'})
        self.assertIn('缺失 PMID', '\n'.join(second['warnings']))
        self.summary.return_value = self.records
        third = self.call('third.json', previous_path=second['path'])
        self.assertEqual([r['pmid'] for r in third['records']], ['11', '33'])

    def test_saved_is_not_seen_and_detail_upgrade_is_not_suppressed(self):
        first = self.call('first.json', max_show=0)
        self.assertFalse(self.snapshot(first)['display_history'])
        second = self.call('second.json', previous_path=first['path'])
        self.assertEqual(second['displayed'], 3)
        third = self.call('third.json', detail='full', previous_path=second['path'])
        self.assertEqual(third['displayed'], 3)
        fourth = self.call('fourth.json', previous_path=third['path'])
        self.assertEqual(fourth['displayed'], 0)

    def test_ids_never_fetches_metadata_and_cannot_hide_brief(self):
        first = self.call('first.json', detail='ids')
        self.summary.assert_not_awaited()
        self.assertFalse(first['metadata_requested'])
        self.assertIsNone(first['esummary_missing'])
        self.assertIn('未检查撤稿', '\n'.join(first['warnings']))
        second = self.call('second.json', previous_path=first['path'])
        self.assertEqual(second['displayed'], 3)

    def test_ids_does_not_erase_richer_history(self):
        self.records[0][1]['articleids'] = [{'idtype': 'doi', 'value': 'fixture-old-doi'}]
        first = self.call('first.json', detail='full')
        ids = self.call('ids.json', detail='ids', previous_path=first['path'])
        self.assertEqual(ids['displayed'], 0)
        self.assertEqual(self.snapshot(ids)['display_history'], self.snapshot(first)['display_history'])
        self.records[0][1]['articleids'][0]['value'] = 'fixture-new-doi'
        updated = self.call('updated.json', previous_path=ids['path'])
        self.assertEqual(updated['records'][0]['doi'], 'fixture-new-doi')

    def test_query_warning_window_and_date_context_survive(self):
        self.page.update(count='10005', retstart='9996', errorlist={'phrasesnotfound': ['missing fixture']})
        first = self.call('first.json')
        second = self.call('second.json', previous_path=first['path'])
        self.assertEqual(second['querytranslation'], self.page['querytranslation'])
        self.assertEqual(second['daterange'], self.page['_daterange'])
        self.assertEqual(second['executed_windows'], self.page['_windows'])
        self.assertTrue(second['window_warning'])
        self.assertTrue(second['has_more'])
        self.assertIsNone(second['next_retstart'])
        self.assertIn('phrasesnotfound', '\n'.join(second['warnings']))

    def test_zero_count_and_count_only_keep_explanations(self):
        for name, updates, phrase in [('zero', {'count': '0', 'idlist': []}, '未匹配记录'),
                                       ('count', {'idlist': [], '_retmax': 0}, '只要计数')]:
            self.page.update(updates)
            if name == 'count':
                self.page['count'] = '5'
            result = self.call(name + '.json')
            self.assertIn(phrase, result['zero_note'])
            self.assertEqual(result['displayed'], 0)
            self.assertFalse(result['metadata_requested'])

    def test_invalid_inputs_fail_before_network(self):
        invalid = self.root / 'invalid.json'
        invalid.write_text('{"schema":"pubmed.diff.v1"}', encoding='utf-8')
        for opts in ({'previous_path': str(invalid)}, {'max_show': 1},
                     {'out_path': str(invalid)}, {'out_path': str(self.root / 'new.json'), 'max_show': -1},
                     {'out_path': str(self.root / 'new.json'), 'previous_path': str(invalid)},
                     {'out_path': str(self.root / 'new.json'), 'previous_path': str(self.root / 'missing.json')}):
            with self.subTest(options=opts), self.assertRaises(m.NcbiError):
                run(m.search, 'fixture', **opts)
        self.search.assert_not_awaited()

    def test_new_snapshot_is_not_complete_baseline(self):
        first = self.call('first.json')
        with self.assertRaises(m.NcbiError):
            m._read_pmid_file(first['path'], baseline=True)

    def test_write_failure_and_metadata_failure_are_not_small_successes(self):
        with patch.object(m, '_write_new_file', side_effect=m.NcbiError('fixture write failed')):
            with self.assertRaisesRegex(m.NcbiError, 'write failed'):
                self.call('failed.json')
        self.assertFalse((self.root / 'failed.json').exists())
        self.summary.side_effect = m.NcbiError('fixture metadata failed')
        with self.assertRaisesRegex(m.NcbiError, 'metadata failed'):
            self.call('metadata.json')
        self.assertFalse((self.root / 'metadata.json').exists())

    def test_file_hash_and_no_implicit_state(self):
        first = self.call('first.json')
        raw = Path(first['path']).read_bytes()
        self.assertEqual(first['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(first['bytes'], len(raw))
        independent = self.call('independent.json')
        self.assertEqual(independent['displayed'], 3)
        normal = json.loads(run(m.search, 'fixture', as_json=True))
        self.assertEqual(len(normal['records']), 3)
        self.assertNotIn('displayed', normal)

    def test_snapshot_byte_limit_accepts_exact_size_and_rejects_overflow(self):
        self.records[0][1]['title'] = '中文题录'
        with patch.object(m, '_utc_now', return_value='fixture-time'):
            first = self.call('measure.json')
            raw = Path(first['path']).read_bytes()
            self.assertGreater(len(raw), len(raw.decode('utf-8')))
            with patch.object(m._tools_module, '_SEARCH_SNAPSHOT_MAX_BYTES', len(raw), create=True):
                exact = self.call('exact.json')
                self.assertEqual(exact['bytes'], len(raw))
                history, binding = m._tools_impl._read_search_snapshot(exact['path'])
                self.assertEqual(history, self.snapshot(exact)['display_history'])
                self.assertEqual(binding['sha256'], exact['sha256'])
                self.records[0][1]['title'] += '中'
                with patch.object(m, '_write_new_file', wraps=m._write_new_file) as writer:
                    with self.assertRaisesRegex(m.NcbiError, '快照.*大小上限'):
                        self.call('too-large.json')
                    writer.assert_not_called()
                self.assertFalse((self.root / 'too-large.json').exists())
                self.assertEqual(Path(exact['path']).read_bytes(), raw)

    def test_oversized_previous_snapshot_fails_before_network(self):
        first = self.call('first.json')
        raw = Path(first['path']).read_bytes()
        self.search.reset_mock()
        with patch.object(m._tools_module, '_SEARCH_SNAPSHOT_MAX_BYTES', len(raw) - 1, create=True):
            with self.assertRaisesRegex(m.NcbiError, '快照过大'):
                self.call('next.json', previous_path=first['path'])
        self.search.assert_not_awaited()
        self.assertFalse((self.root / 'next.json').exists())

    def test_sdk_exposes_optional_snapshot_arguments(self):
        tool = next(t for t in run(m.mcp.list_tools) if t.name == 'search')
        schema = getattr(tool, 'inputSchema', None) or getattr(tool, 'input_schema')
        for key in ('out_path', 'previous_path', 'max_show'):
            self.assertIn(key, schema['properties'])
            self.assertNotIn(key, schema.get('required', []))
        run(m.mcp.call_tool, 'search', {'term': 'fixture', 'out_path': str(self.root / 'sdk.json'), 'max_show': 1})
        self.assertTrue((self.root / 'sdk.json').is_file())

    def test_audit_failure_remains_in_response(self):
        with patch.object(m, '_write_audit', return_value={'status': 'failed', 'error': 'fixture audit failure'}):
            saved = self.call('first.json', max_show=0)
        self.assertEqual(saved['audit_status'], 'failed')
        self.assertIn('fixture audit failure', '\n'.join(saved['warnings']))


if __name__ == '__main__':
    unittest.main()

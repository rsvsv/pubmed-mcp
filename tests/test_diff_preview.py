"""Preview regressions; all IDs, counts and caps are 本项目自订测试输入."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
import anyio

spec = importlib.util.spec_from_file_location('preview_subject', Path(__file__).resolve().parents[1] / 'pubmed_mcp.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(**kwargs):
    async def call():
        return await m.diff('old', 'new', **kwargs)
    return anyio.run(call)


class DiffPreview(unittest.TestCase):
    def setUp(self):
        async def ids(query, **kwargs):
            values = ['1', '2', '3'] if query == 'old' else ['3', '4', '5']
            return values, len(values), {'querytranslation': query}, ''
        async def records(ids):
            return [(pid, {'title': 'Title ' + pid, 'pubdate': '2025',
                           'pubtype': ['Journal Article', 'Retracted Publication']}) for pid in reversed(ids)]
        self.metadata = AsyncMock(side_effect=records)
        for name, value in [('_ids_in_window', ids), ('_esummary_dicts', self.metadata)]:
            p = patch.object(m, name, value); p.start(); self.addCleanup(p.stop)

    def test_default_does_not_fetch_metadata(self):
        d = json.loads(run(as_json=True))
        self.assertNotIn('preview', d)
        self.assertEqual(d['added'], ['4', '5'])
        self.assertEqual(d['removed'], ['1', '2'])
        self.assertIn('不代表文献被数据库删除', d['caveat'])
        self.metadata.assert_not_awaited()

    def test_preview_preserves_order_types_and_full_sets(self):
        d = json.loads(run(as_json=True, preview=True, max_show=1))
        self.assertEqual(d['added'], ['4', '5'])
        self.assertEqual(d['preview']['added']['requested_pmids'], ['4'])
        self.assertEqual(d['preview']['added']['not_previewed_count'], 1)
        self.assertIn('Retracted Publication', d['preview']['added']['records'][0]['pubtype'])
        d = json.loads(run(as_json=True, preview=True))
        self.assertEqual([r['pmid'] for r in d['preview']['added']['records']], ['4', '5'])
        text = run(preview=True, max_show=1)
        self.assertIn('Title 4', text)
        self.assertIn('Title 1', text)
        self.assertIn('Retracted Publication', text)
        self.assertIn('其余未预览 1', text)
        self.assertIn('不是随机样本', text)

    def test_partial_metadata_and_failure_keep_differences(self):
        self.metadata.side_effect = [ [('5', {'title': 'Only title', 'pubtype': []})], m.NcbiError('metadata unavailable') ]
        d = json.loads(run(as_json=True, preview=True))
        self.assertEqual(d['added'], ['4', '5'])
        self.assertEqual(d['removed'], ['1', '2'])
        self.assertEqual(d['preview']['added']['missing_pmids'], ['4'])
        self.assertEqual(d['preview']['removed']['missing_pmids'], ['1', '2'])
        self.assertIn('metadata unavailable', d['preview']['removed']['error'])
        self.assertTrue(any('预览不完整' in w for w in d['warnings_a']))
        self.assertTrue(any('预览不完整' in w for w in d['warnings_b']))

    def test_zero_limit_and_identical_sets_skip_metadata(self):
        d = json.loads(run(preview=True, max_show=0, as_json=True))
        self.assertEqual(d['preview']['added']['not_previewed_count'], 2)
        self.metadata.assert_not_awaited()
        async def same(*args, **kwargs):
            return ['3'], 1, {}, ''
        with patch.object(m, '_ids_in_window', same):
            d = json.loads(run(preview=True, as_json=True))
        self.assertEqual(d['added_count'], 0)
        self.metadata.assert_not_awaited()

    def test_invalid_preview_fails_before_search(self):
        with patch.object(m, '_ids_in_window', AsyncMock()) as ids:
            with self.assertRaises(m.NcbiError):
                run(preview='not-a-bool')
            ids.assert_not_awaited()

    def test_sdk_exposes_opt_in_preview(self):
        async def tools(): return await m.mcp.list_tools()
        tool = next(t for t in anyio.run(tools) if t.name == 'diff')
        schema = getattr(tool, 'inputSchema', None) or getattr(tool, 'input_schema')
        self.assertIs(schema['properties']['preview']['default'], False)


if __name__ == '__main__':
    unittest.main()

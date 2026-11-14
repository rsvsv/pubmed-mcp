"""元数据与日期回归；全部数字/日期/PMID 为本项目自订夹具。"""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import anyio

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('metadata_subject', ROOT / 'pubmed_mcp.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class MetadataDates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {'PUBMED_AUDIT_DIR': ''})
        env.start()
        self.addCleanup(env.stop)

    def run_export(self, **kwargs):
        async def run():
            with patch.object(m, '_esearch', AsyncMock(return_value={'count': '0', 'idlist': [], 'querytranslation': 'x'})):
                return json.loads(await m.export('x', str(self.root / 'data.txt'), as_json=True, **kwargs))
        return anyio.run(run)

    def test_metadata_and_verify_statuses(self):
        result = self.run_export(metadata=True)
        data = self.root / 'data.txt'
        meta = Path(str(data) + '.meta.json')
        rec = json.loads(meta.read_text(encoding='utf-8'))
        self.assertEqual(result['metadata_status'], 'written')
        self.assertEqual(rec['sha256'], result['sha256'])
        self.assertNotIn('metadata_status', rec)
        self.assertEqual(m._verify_export(str(data))['status'], 'verified')
        data.write_text('changed', encoding='utf-8')
        self.assertEqual(m._verify_export(str(data))['status'], 'hash_mismatch')
        meta.write_text('{}', encoding='utf-8')
        self.assertEqual(m._verify_export(str(data))['status'], 'invalid_meta')
        meta.unlink()
        self.assertEqual(m._verify_export(str(data))['status'], 'missing_meta')
        data.unlink()
        self.assertEqual(m._verify_export(str(data))['status'], 'missing_data')

    def test_default_no_metadata_and_failure_preserves_data(self):
        with patch.object(m, '_write_new_file', side_effect=m.NcbiError('fixture publish failure')):
            result = self.run_export(metadata=True)
        self.assertEqual(result['metadata_status'], 'failed')
        self.assertTrue((self.root / 'data.txt').exists())
        self.assertFalse((self.root / 'data.txt.meta.json').exists())
        self.assertIn('伴随元数据', ' '.join(result['warnings']))

    def test_existing_metadata_rejected_before_network(self):
        (self.root / 'data.txt.meta.json').write_text('keep')
        with patch.object(m, '_get_async', AsyncMock()) as net:
            with self.assertRaises(m.NcbiError):
                self.run_export(metadata=True)
            net.assert_not_called()
        self.assertFalse((self.root / 'data.txt').exists())

    def test_edat_request_and_note(self):
        async def run():
            with patch.object(m, '_get_async', AsyncMock(return_value=json.dumps({'esearchresult': {'count': '0'}}))) as net:
                result = await m._esearch('x', mindate='2020', maxdate='2021', datetype='edat')
                self.assertEqual(net.call_args.args[1]['datetype'], 'edat')
                self.assertIn('入库日期', result['_daterange'])
                await m._esearch('x', datetype='edat')
                self.assertNotIn('datetype', net.call_args.args[1])
                with self.assertRaises(m.NcbiError):
                    await m._esearch('x', datetype='bad')
        anyio.run(run)

    def test_export_frozen_relative_and_explicit_dates(self):
        qt = 'all[sb] AND 2026/01/02:2026/01/04[Date - Entry]'
        async def run():
            with patch.object(m, '_esearch', AsyncMock(return_value={'querytranslation': qt})):
                query = await m._freeze_export_query('x OR y', 'edat', '2020', '2026', 3)
                self.assertIn('(x OR y)', query)
                self.assertIn('2020-01-01:2026-12-31[edat]', query)
                self.assertIn('2026-01-02:2026-01-04[edat]', query)
            with patch.object(m, '_esearch', AsyncMock(return_value={'querytranslation': ''})):
                with self.assertRaisesRegex(m.NcbiError, '回显'):
                    await m._freeze_export_query('x', 'edat', '', '', 3)
        anyio.run(run)

    def test_frozen_condition_survives_pagination_and_recount(self):
        queries = []
        async def search(term, **kwargs):
            queries.append(term)
            return {'count': '2', 'idlist': ['2'] if kwargs.get('retstart') else ['1'], 'querytranslation': term}
        async def run():
            with patch.object(m, '_esearch', search):
                await m.export('topic OR other', str(self.root / 'paged.txt'), datetype='edat', mindate='2020', maxdate='2021')
        anyio.run(run)
        self.assertTrue(queries)
        self.assertEqual(len(set(queries)), 1)
        self.assertIn('2020-01-01:2021-12-31[edat]', queries[0])
        self.assertEqual((self.root / 'paged.txt').read_text().split(), ['1', '2'])

    def test_edat_over_window_fails_before_publish(self):
        async def run():
            with patch.object(m, '_esearch', AsyncMock(return_value={'count': str(m.WINDOW + 1)})):
                with self.assertRaisesRegex(m.NcbiError, 'edat 超窗'):
                    await m.export('x', str(self.root / 'none.txt'), datetype='edat')
        anyio.run(run)
        self.assertFalse((self.root / 'none.txt').exists())

    def test_verify_files_in_report_is_explicit(self):
        result = self.run_export(metadata=True)
        log = self.root / 'source.jsonl'
        log.write_text(json.dumps({'event': 'export', **result}) + '\n', encoding='utf-8')
        report = self.root / 'report.csv'
        m._audit_report([str(log)], str(report), verify_files=True)
        import csv
        with report.open(encoding='utf-8-sig', newline='') as f:
            row = next(csv.DictReader(f))
        self.assertEqual(json.loads(row['file_verification'])['status'], 'verified')

    def test_sdk_metadata_and_date_parameters(self):
        async def run():
            with patch.object(m, '_esearch', AsyncMock(return_value={'count': '0', 'idlist': [], 'querytranslation': 'x'})):
                result = await m.mcp.call_tool('export', {'term': 'x', 'path': str(self.root/'sdk.txt'), 'metadata': True, 'as_json': True, 'datetype': 'edat', 'mindate': '2020'})
                self.assertIn('written', str(result))
                self.assertTrue((self.root/'sdk.txt.meta.json').exists())
        anyio.run(run)

if __name__ == '__main__':
    unittest.main()

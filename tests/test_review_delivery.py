"""Review handoff regression. All counts, IDs and dates here are project-defined fixtures."""
import csv
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import anyio

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('delivery_subject', ROOT / 'pubmed_review.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
m = r.m


def article(pid, abstract=True):
    return {'pmid': pid, 'title': '=formula' if pid == '1' else 'Study ' + pid,
            'abstract': [{'label': 'RESULTS', 'text': 'fixture abstract'}] if abstract else [],
            'authors': [{'last': 'Fixture', 'fore': 'Author', 'initials': 'A'}],
            'journal': {'title': 'Fixture Journal'}, 'pubdate': '2020',
            'ids': {'doi': 'fixture-doi'}, 'lang': ['eng'], 'mesh': [], 'pt': [], 'comments': []}


class Delivery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = {'question': 'Fixture question', 'eligibility': 'Fixture eligibility',
                     'term': 'fixture[tiab]', 'strategy_status': 'approved',
                     'strategy_review_note': 'Project-defined test approval; not a real research decision',
                     'known_relevant': [{'pmid': '1', 'source': 'independent test fixture'}],
                     'other_databases': 'Fixture scope', 'citation_tracking': 'Fixture scope'}
        self.plan_path = self.root / 'plan.json'
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        self.out = self.root / 'out'
        self.calls = []
        async def esearch(term, **kwargs):
            ids = ['1'] if '[uid]' in term else ['1', '2']
            return {'count': str(len(ids)), 'idlist': ids if kwargs.get('retmax', 0) else [],
                    'querytranslation': term, '_term': term}
        async def fetch(ids):
            self.calls.append(list(ids))
            return [article(pid, pid != '2') for pid in ids]
        for p in (patch.object(m, '_esearch', esearch), patch.object(m, '_efetch_xml_all', fetch),
                  patch.dict(os.environ, {'PUBMED_AUDIT_DIR': ''})):
            p.start()
            self.addCleanup(p.stop)

    def execute(self):
        return anyio.run(r.run, str(self.plan_path), str(self.out))

    def test_complete_handoff_has_no_silent_exclusions(self):
        result = self.execute()
        self.assertEqual(result['handoff_status'], 'ready_for_screening')
        self.assertEqual(result['metrics']['no_abstract_pmids'], ['2'])
        with (self.out / 'screening.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([x['pmid'] for x in rows], ['1', '2'])
        self.assertTrue(all(x['reviewer_decision'] == '待筛选' for x in rows))
        self.assertEqual(rows[0]['title'], "'=formula")
        self.assertEqual(rows[1]['abstract_status'], 'not_provided')
        self.assertEqual(os.environ['PUBMED_AUDIT_DIR'], '')
        self.assertEqual(r.verify(self.out)['status'], 'verified')

    def test_draft_is_not_called_ready(self):
        self.plan.update(strategy_status='draft', known_relevant=[], other_databases='')
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        self.assertEqual(self.execute()['handoff_status'], 'needs_methodology_review')
        codes = {i['code'] for i in r.read_json(self.out / 'delivery.json')['issues']}
        self.assertTrue({'strategy_draft', 'known_set_missing', 'other_databases'} <= codes)

    def test_missing_records_remain_visible_and_block_completion(self):
        async def fetch(ids):
            return [article('1')]
        with patch.object(m, '_efetch_xml_all', fetch):
            result = self.execute()
        self.assertEqual(result['handoff_status'], 'needs_execution_repair')
        self.assertEqual(result['metrics']['missing_pmids'], ['2'])
        self.assertIn('2', (self.out / 'screening.csv').read_text(encoding='utf-8-sig'))

    def test_failed_batch_resumes_without_refetching_finished_batch(self):
        async def fetch(ids):
            self.calls.append(list(ids))
            if ids == ['2']:
                raise m.NcbiError('fixture interruption')
            return [article(pid) for pid in ids]
        with patch.object(r, 'BATCH_SIZE', 1), patch.object(m, '_efetch_xml_all', fetch):
            with self.assertRaisesRegex(m.NcbiError, 'fixture interruption'):
                self.execute()
        self.assertFalse((self.out / 'delivery.json').exists())
        probe_lock = r.acquire_lock(self.out)
        probe_lock.close()
        self.calls.clear()
        with patch.object(r, 'BATCH_SIZE', 1):
            result = anyio.run(r.run, None, None, str(self.out))
        self.assertEqual(self.calls, [['2']])
        self.assertEqual(result['handoff_status'], 'ready_for_screening')

    def test_tamper_blocks_resume(self):
        self.execute()
        (self.out / 'all-pmids.txt').write_text('1\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '文件缺失或已改变'):
            anyio.run(r.run, None, None, str(self.out))

    def test_changed_plan_blocks_resume(self):
        async def fail(ids):
            raise m.NcbiError('fixture interruption')
        with patch.object(m, '_efetch_xml_all', fail), self.assertRaises(m.NcbiError):
            self.execute()
        (self.out / 'plan.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '方案已修改'):
            anyio.run(r.run, None, None, str(self.out))

    def test_output_cannot_overwrite_existing_directory(self):
        self.out.mkdir()
        with self.assertRaises(FileExistsError):
            self.execute()
        self.assertEqual(list(self.out.iterdir()), [])

    def test_typo_and_false_approval_rejected_before_output(self):
        for update in ({'termm': 'typo'}, {'strategy_review_note': ''}, {'mindate': '2020'}):
            with self.subTest(update=update):
                with self.assertRaises(ValueError):
                    r.validate_plan({**self.plan, **update})
        self.assertFalse(self.out.exists())

    def test_audit_failure_is_not_hidden(self):
        with patch.object(m, '_write_audit', return_value={'status': 'failed', 'error': 'fixture outage'}):
            with self.assertRaisesRegex(ValueError, '没有检索日志'):
                self.execute()
        self.assertFalse((self.out / 'delivery.json').exists())
        self.assertTrue((self.out / 'all-pmids.txt').is_file())

    def test_missing_original_audit_blocks_resume(self):
        async def fail(ids):
            raise m.NcbiError('fixture interruption')
        with patch.object(m, '_efetch_xml_all', fail), self.assertRaises(m.NcbiError):
            self.execute()
        for path in (self.out / 'audit').glob('*.jsonl'):
            path.unlink()
        with self.assertRaisesRegex(ValueError, '审计事件缺失'):
            anyio.run(r.run, None, None, str(self.out))

    def test_live_recall_cannot_substitute_for_exported_ids(self):
        self.plan['known_relevant'] = [{'pmid': '3', 'source': 'independent fixture'}]
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        async def esearch(term, **kwargs):
            ids = ['3'] if '[uid]' in term else ['1', '2']
            return {'count': str(len(ids)), 'idlist': ids if kwargs.get('retmax', 0) else [],
                    'querytranslation': term, '_term': term}
        with patch.object(m, '_esearch', esearch):
            result = self.execute()
        self.assertEqual(result['handoff_status'], 'needs_methodology_review')
        self.assertIn('known_not_in_export', {i['code'] for i in r.read_json(self.out / 'delivery.json')['issues']})

    def test_final_marker_failure_can_resume(self):
        original = r.write_json
        def fail(path, data):
            if Path(path).name == 'delivery.json':
                raise OSError('fixture disk error')
            return original(path, data)
        with patch.object(r, 'write_json', fail), self.assertRaises(OSError):
            self.execute()
        self.assertIn('否则本目录尚未完成交付', (self.out / 'HANDOFF.md').read_text(encoding='utf-8'))
        self.assertEqual(anyio.run(r.run, None, None, str(self.out))['handoff_status'], 'ready_for_screening')

    def test_concurrent_lock_is_rejected(self):
        self.out.mkdir()
        lock = r.acquire_lock(self.out)
        try:
            with self.assertRaises(OSError):
                r.acquire_lock(self.out)
        finally:
            lock.close()
        r.acquire_lock(self.out).close()

    def test_cli_initializes_output_encoding(self):
        code = "import runpy,sys;sys.stdout.reconfigure(encoding='ascii');sys.stderr.reconfigure(encoding='ascii');sys.argv=[sys.argv[1],'--help'];runpy.run_path(sys.argv[0],run_name='__main__')"
        result = subprocess.run([sys.executable, '-I', '-c', code, str(ROOT / 'pubmed_review.py')],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.assertIn('综述检索', result.stdout.decode('utf-8'))

    def test_placeholder_plan_rejected(self):
        with self.assertRaises(ValueError):
            r.validate_plan({**self.plan, 'question': '⚠️【待确认】'})

    def test_warning_prevents_ready_label(self):
        exported = {'integrity_status': 'no_discrepancy_detected', 'metadata_status': 'written',
                    'warnings': ['fixture query warning'], 'querytranslation': 'fixture',
                    'unique': 1, 'audit_status': 'written'}
        issues = r.assess(self.plan, exported, [], [], {'missing_pmids': []})
        self.assertIn('query_warnings', {i['code'] for i in issues})

    def test_verification_survives_folder_move(self):
        self.execute()
        moved = self.root / 'moved folder'
        self.out.rename(moved)
        self.assertEqual(r.verify(moved)['status'], 'verified')


if __name__ == '__main__':
    unittest.main()

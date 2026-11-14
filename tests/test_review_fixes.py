"""审查修复回归；所有数字、路径、哈希和题录均为本项目自订测试夹具。"""
import csv
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import anyio

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('review_subject', ROOT / 'pubmed_mcp.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


class ReviewFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {'PUBMED_AUDIT_DIR': ''})
        env.start()
        self.addCleanup(env.stop)

    def test_linkset_and_relation_errors_are_not_empty_success(self):
        for key in ('error', 'ERROR'):
            for nested in (False, True):
                entry = {'ids': ['1']}
                if nested:
                    entry['linksetdbs'] = [{key: 'fixture failure'}]
                else:
                    entry[key] = 'fixture failure'
                with self.subTest(key=key, nested=nested), patch.object(m, '_pmids_exist', AsyncMock(return_value={'1': 'present'})), patch.object(m, '_get_async', AsyncMock(return_value=json.dumps({'linksets': [entry]}))):
                    with self.assertRaisesRegex(m.NcbiError, 'fixture failure'):
                        run(m.links, '1', to='refs', as_json=True)

    def test_partial_links_keep_success_and_source_error(self):
        async def transport(path, params, timeout):
            entry = {'ids': [params['id']]}
            if params['id'] == '1':
                entry['error'] = 'fixture failure'
            else:
                entry['linksetdbs'] = [{'linkname': 'pubmed_pubmed_refs', 'links': ['3']}]
            return json.dumps({'linksets': [entry]})
        with patch.object(m, '_pmids_exist', AsyncMock(return_value={})), patch.object(m, '_get_async', transport):
            result = json.loads(run(m.links, '1 2', to='refs', as_json=True))
        self.assertEqual(result['candidate_pmids'], ['3'])
        self.assertEqual(result['sources'][0]['relation_status'], 'error')
        self.assertEqual(result['counts']['sources_with_errors'], 1)

    def files(self):
        data = self.root / 'data.txt'
        data.write_text('new', encoding='utf-8')
        meta = Path(str(data) + '.meta.json')
        meta.write_text(json.dumps({'schema': 'pubmed.export.v1', 'sha256': m._sha256_file(str(data))}), encoding='utf-8')
        return data, meta

    def test_report_detects_replaced_data_and_sidecar(self):
        data, meta = self.files()
        log = self.root / 'audit.jsonl'
        log.write_text(json.dumps({'event': 'export', 'path': str(data), 'metadata_path': str(meta), 'sha256': '0' * 64}) + '\n', encoding='utf-8')
        out = self.root / 'report.csv'
        m._audit_report([str(log)], str(out), verify_files=True)
        with out.open(encoding='utf-8-sig', newline='') as f:
            verification = json.loads(next(csv.DictReader(f))['file_verification'])
        self.assertEqual(verification['status'], 'event_hash_mismatch')
        self.assertEqual(verification['metadata_verification_status'], 'verified')
        self.assertEqual(verification['event_binding_status'], 'hash_mismatch')

    def test_missing_log_hash_cannot_claim_event_verified(self):
        data, meta = self.files()
        result = m._verify_audit_artifact({'path': str(data)})
        self.assertEqual(result['status'], 'event_hash_unavailable')
        self.assertEqual(result['metadata_verification_status'], 'verified')

    def test_data_read_failure_is_not_invalid_metadata(self):
        data, meta = self.files()
        for error, status in ((PermissionError('locked'), 'unreadable_data'), (FileNotFoundError('removed'), 'missing_data')):
            with self.subTest(status=status), patch.object(m, '_sha256_file', side_effect=error):
                result = m._verify_export(str(data))
            self.assertEqual(result['status'], status)

    def test_reserved_output_rejected_before_creating_job(self):
        source = self.root / 'ids.txt'
        source.write_text('1\n', encoding='utf-8')
        job = self.root / 'job'
        for relative in ('manifest.json', 'batches/batch-00000.txt', '.resume.lock', 'results.txt'):
            with self.subTest(relative=relative), self.assertRaisesRegex(m.NcbiError, '作业目录'):
                run(m._export_pmids, str(source), str(job / relative), checkpoint_dir=str(job))
            self.assertFalse(job.exists())

    def test_resume_rejects_internal_output(self):
        source = self.root / 'ids.txt'
        source.write_text('1\n', encoding='utf-8')
        job = self.root / 'job'
        manifest = m._new_job_manifest(m._read_pmid_file(str(source)), str(job / 'manifest.json'), 'pmids', 1)
        with self.assertRaisesRegex(m.NcbiError, '作业目录'):
            m._validate_job_manifest(manifest, str(job), require_metadata_phase=True)

    def test_export_audit_preserves_http_count(self):
        logs = self.root / 'logs'
        payload = {'count': '0', 'idlist': [], 'querytranslation': 'fixture'}
        def transport(path, params, timeout, tries, *, before_attempt, wait):
            before_attempt()
            before_attempt()
            return json.dumps({'esearchresult': payload})
        # 两次尝试是自订模拟重试；逻辑请求由真实 _get_async 审计入口捕获。
        with patch.dict(os.environ, {'PUBMED_AUDIT_DIR': str(logs)}), patch.object(m, '_get', transport):
            result = json.loads(run(m.export, 'fixture', str(self.root/'export.txt'), as_json=True, metadata=True))
        rec = json.loads(next(logs.glob('*.jsonl')).read_text(encoding='utf-8'))
        meta = json.loads(Path(result['metadata_path']).read_text(encoding='utf-8'))
        self.assertIsInstance(rec['requests'], int)
        self.assertEqual(rec['requests'], result['requests'])
        self.assertEqual(rec['requests'], meta['requests'])
        self.assertIsInstance(rec['logical_requests'], list)
        self.assertTrue(rec['logical_requests'])
        self.assertEqual(rec['requests'], len(rec['logical_requests']) * 2)

    def test_diff_audit_failure_and_success(self):
        source = self.root/'ids.txt'
        source.write_text('1', encoding='utf-8')
        blocker = self.root/'blocker'
        blocker.write_text('not a directory', encoding='utf-8')
        with patch.dict(os.environ, {'PUBMED_AUDIT_DIR': str(blocker)}):
            result = m._diff_files(str(source), str(source), str(self.root/'diff.json'))
        self.assertEqual(result['audit_status'], 'failed')
        self.assertTrue(result['warnings'])
        self.assertTrue(Path(result['path']).exists())
        logs = self.root/'logs'
        with patch.dict(os.environ, {'PUBMED_AUDIT_DIR': str(logs)}):
            result = m._diff_files(str(source), str(source), str(self.root/'ok.json'))
            with self.assertRaises(m.NcbiError):
                m._diff_files(str(source), str(source), str(self.root/'ok.json'))
        records = [json.loads(line) for line in next(logs.glob('*.jsonl')).read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['outcome'] for r in records], ['success', 'failed'])
        self.assertEqual(result['audit_status'], 'written')
        self.assertEqual(records[0]['input_params']['baseline'], str(source))

    def test_csv_writer_uses_shared_header(self):
        out = self.root/'out.csv'
        header = tuple('fixture_' + name for name in m._PMIDS_CSV_HEADER)
        with patch.object(m, '_PMIDS_CSV_HEADER', header), patch.object(m, '_esummary_dicts', AsyncMock(return_value=[])):
            run(m._export_csv_batch, str(out), [])
        with out.open(encoding='utf-8-sig', newline='') as f:
            self.assertEqual(next(csv.reader(f)), list(header))


if __name__ == '__main__':
    unittest.main()

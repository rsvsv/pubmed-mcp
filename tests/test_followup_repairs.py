"""审查修复回归。所有 ID、计数、超时和路径编号均为本项目自订测试夹具。"""
import csv
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import anyio

spec = importlib.util.spec_from_file_location(
    'followup_subject', Path(__file__).resolve().parents[1] / 'pubmed_mcp.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Repairs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'PUBMED_AUDIT_DIR': ''})
        env.start()
        self.addCleanup(env.stop)

    def job(self):
        src = self.root / 'ids.txt'
        src.write_text('1\n2\n', encoding='utf-8')
        job = self.root / 'job'
        (job / 'batches').mkdir(parents=True)
        manifest = m._new_job_manifest(m._read_pmid_file(str(src)), str(self.root / 'out.txt'), 'pmids', 1)
        m._write_json_atomic(str(job / 'manifest.json'), manifest)
        return job, manifest

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('Host cannot create symlinks: ' + str(exc))

    def test_batch_alias_rejected_before_manifest_overwrite(self):
        job, manifest = self.job()
        target = job / 'manifest.json'
        original = target.read_bytes()
        self.symlink(job / manifest['batches'][0]['file'], target)
        with self.assertRaisesRegex(m.NcbiError, '符号链接'):
            m._validate_job_manifest(manifest, str(job))
        with self.assertRaisesRegex(m.NcbiError, '符号链接'):
            anyio.run(m._store_batch, str(job), manifest, manifest['batches'][0])
        self.assertEqual(target.read_bytes(), original)

    def test_batch_resolved_alias_rejected_without_symlink_privilege(self):
        job, manifest = self.job()
        real = m._job_path
        alias = manifest['batches'][0]['file']
        def resolve(root, rel):
            return str(job / 'manifest.json') if rel == alias else real(root, rel)
        original = (job / 'manifest.json').read_bytes()
        with patch.object(m, '_job_path', resolve):
            with self.assertRaisesRegex(m.NcbiError, '符号链接'):
                m._validate_job_manifest(manifest, str(job))
            with self.assertRaisesRegex(m.NcbiError, '符号链接'):
                anyio.run(m._store_batch, str(job), manifest, manifest['batches'][0])
        self.assertEqual((job / 'manifest.json').read_bytes(), original)

    def test_untrusted_state_path_full_resume_without_symlink_privilege(self):
        async def run():
            job, manifest = self.job()
            with patch.object(m, '_batch_state_path', side_effect=m.NcbiError('fixture 符号链接')):
                result = await m._resume_export(str(job))
                m._remove_batch_state(str(job), 0)
            self.assertEqual((self.root / 'out.txt').read_text().split(), ['1', '2'])
            self.assertTrue(any('不可信增量状态' in w for w in result['warnings']))
            self.assertEqual(json.loads((job / 'manifest.json').read_text(encoding='utf-8'))['phase'], 'published')
        anyio.run(run)

    def test_merge_detects_changes_after_batch_verification(self):
        async def run():
            job, manifest = self.job()
            for batch in manifest['batches']:
                await m._store_batch(str(job), manifest, batch)
                m._verify_done_batch(str(job), manifest, batch)
            first = job / manifest['batches'][0]['file']
            for replacement in (b'9\n', b''):
                first.write_bytes(replacement)
                with self.assertRaisesRegex(m.NcbiError, '合并时批文件哈希不符'):
                    await m._publish_export_file(str(job), manifest, str(self.root / 'out.txt'), 'pmids')
                self.assertFalse((self.root / 'out.txt').exists())
        anyio.run(run)

    def test_state_symlink_refetches_without_touching_target(self):
        async def run():
            job, manifest = self.job()
            (job / 'state').mkdir()
            sentinel = self.root / 'sentinel.txt'
            sentinel.write_text('KEEP', encoding='utf-8')
            self.symlink(job / 'state' / 'batch-00000.json', sentinel)
            result = await m._resume_export(str(job))
            self.assertEqual((self.root / 'out.txt').read_text().split(), ['1', '2'])
            self.assertEqual(sentinel.read_text(), 'KEEP')
            self.assertTrue(any('不可信增量状态' in w for w in result['warnings']))
            m._remove_batch_state(str(job), 0)
            self.assertEqual(sentinel.read_text(), 'KEEP')
        anyio.run(run)

    def test_event_binding_without_valid_sidecar(self):
        data = self.root / 'data.txt'
        data.write_bytes(b'original')
        rec = {'path': str(data), 'sha256': m._sha256_file(str(data))}
        result = m._verify_audit_artifact(rec)
        self.assertEqual(result['metadata_verification_status'], 'missing_meta')
        self.assertEqual(result['event_binding_status'], 'verified')
        Path(str(data) + '.meta.json').write_text('{}', encoding='utf-8')
        self.assertEqual(m._verify_audit_artifact(rec)['event_binding_status'], 'verified')
        data.write_bytes(b'changed')
        self.assertEqual(m._verify_audit_artifact(rec)['status'], 'event_hash_mismatch')
        rec['metadata_path'] = []
        self.assertEqual(m._verify_audit_artifact(rec)['status'], 'event_hash_mismatch')
        data.unlink()
        self.assertEqual(m._verify_audit_artifact(rec)['event_binding_status'], 'not_verified')

    def test_report_distinguishes_local_network_and_unknown(self):
        records = [
            {'event': '_diff_files', 'logical_requests': []},
            {'event': '_export_pmids', 'format': 'pmids', 'logical_requests': []},
            {'event': '_resume_export', 'format': 'pmids', 'logical_requests': []},
            {'event': 'count', 'logical_requests': [{'endpoint': 'esearch.fcgi'}]},
            {'event': 'mesh', 'logical_requests': [{'endpoint': 'esearch.fcgi'}]},
            {'event': 'count'},
        ]
        log = self.root / 'log.jsonl'
        log.write_text('\n'.join(json.dumps(r) for r in records), encoding='utf-8')
        out = self.root / 'report.csv'
        m._audit_report([str(log)], str(out))
        with out.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertTrue(all(row['platform'] == '本地文件处理' for row in rows[:3]))
        self.assertEqual(rows[3]['platform'], 'NCBI E-utilities')
        self.assertEqual(rows[4]['database'], 'MeSH')
        self.assertEqual(rows[5]['platform'], m._AUDIT_UNKNOWN)

    def test_metadata_outage_preserves_links_in_all_outputs(self):
        async def run():
            payload = json.dumps({'linksets': [{'ids': ['1'], 'linksetdbs': [
                {'linkname': 'pubmed_pubmed_refs', 'links': ['002']}]}]})
            with patch.object(m, '_pmids_exist', AsyncMock(side_effect=m.NcbiError('metadata outage'))), \
                    patch.object(m, '_get_async', AsyncMock(return_value=payload)):
                result = json.loads(await m.links('1', to='refs', as_json=True))
                self.assertEqual(result['candidate_pmids'], ['2'])
                self.assertEqual(result['sources'][0]['source_status'], 'unknown')
                self.assertTrue(any('metadata outage' in w for w in result['warnings']))
                text = await m.links('1', to='refs')
                self.assertIn('metadata outage', text)
                out = self.root / 'links.json'
                await m.links('1', to='refs', out_path=str(out))
                self.assertEqual(json.loads(out.read_text(encoding='utf-8'))['candidate_pmids'], ['2'])
        anyio.run(run)

    def test_snowball_retains_failed_child_audit_in_file_and_parent_log(self):
        async def run():
            seed, baseline = self.root / 'seed.txt', self.root / 'old.txt'
            seed.write_text('1', encoding='utf-8')
            baseline.write_text('', encoding='utf-8')
            real = m._write_audit
            attempts = []
            def audit(event, fields, **kwargs):
                attempts.append(event)
                if attempts == ['links']:
                    return {'status': 'failed', 'error': 'fixture audit outage'}
                return real(event, fields, **kwargs)
            payload = json.dumps({'linksets': [{'ids': ['1']}]})
            with patch.dict(os.environ, {'PUBMED_AUDIT_DIR': str(self.root / 'logs')}), \
                    patch.object(m, '_write_audit', audit), \
                    patch.object(m, '_pmids_exist', AsyncMock(return_value={'1': 'present'})), \
                    patch.object(m, '_get_async', AsyncMock(return_value=payload)):
                result = await m._snowball(str(seed), str(baseline), str(self.root / 'snow.json'))
            self.assertEqual(result['audit_status'], 'written')
            self.assertEqual(result['child_audit_failures'][0]['direction'], 'refs')
            self.assertTrue(any('留痕失败' in w for w in result['warnings']))
            stored = json.loads((self.root / 'snow.json').read_text(encoding='utf-8'))
            self.assertEqual(stored['child_audit_failures'], result['child_audit_failures'])
            logs = [json.loads(s) for s in Path(result['audit_path']).read_text(encoding='utf-8').splitlines()]
            self.assertEqual(logs[-1]['child_audit_failures'], result['child_audit_failures'])
        anyio.run(run)

    def test_report_with_nul_path_preserves_other_rows(self):
        log = self.root / 'nul.jsonl'
        records = [{'event': 'export', 'path': 'invalid\x00path', 'sha256': 'a' * 64},
                   {'event': 'count', 'count': 0}]
        log.write_text('\n'.join(json.dumps(r) for r in records), encoding='utf-8')
        out = self.root / 'nul.csv'
        result = m._audit_report([str(log)], str(out), verify_files=True)
        self.assertEqual(result['records'], 2)
        with out.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        verification = json.loads(rows[0]['file_verification'])
        self.assertEqual(verification['event_binding_status'], 'not_verified')
        self.assertIn('event_binding_error', verification)
        self.assertEqual(rows[0]['path'], r'invalid\u0000path')
        self.assertEqual(json.loads(rows[0]['record'])['path'], 'invalid\x00path')
        self.assertEqual(rows[1]['event'], 'count')

    def test_failed_links_and_failed_audit_reach_snowball_output(self):
        async def run():
            seed, baseline = self.root / 'seed.txt', self.root / 'old.txt'
            seed.write_text('1', encoding='utf-8')
            baseline.write_text('', encoding='utf-8')
            real = m._write_audit
            def audit(event, fields, **kwargs):
                if event == 'links' and fields['outcome'] == 'failed':
                    return {'status': 'failed', 'error': 'fixture audit outage'}
                return real(event, fields, **kwargs)
            async def transport(endpoint, params, timeout):
                if params.get('linkname') == 'pubmed_pubmed_refs':
                    raise m.NcbiError('fixture relation outage')
                return json.dumps({'linksets': [{'ids': ['1']}]})
            with patch.dict(os.environ, {'PUBMED_AUDIT_DIR': str(self.root / 'logs')}), \
                    patch.object(m, '_write_audit', audit), \
                    patch.object(m, '_pmids_exist', AsyncMock(return_value={'1': 'present'})), \
                    patch.object(m, '_get_async', transport):
                result = await m._snowball(str(seed), str(baseline), str(self.root / 'snow.json'))
            self.assertEqual(result['audit_status'], 'written')
            self.assertEqual(result['child_audit_failures'], [
                {'direction': 'refs', 'seeds': ['1'], 'error': 'fixture audit outage'}])
            self.assertTrue(result['source_errors'])
            stored = json.loads((self.root / 'snow.json').read_text(encoding='utf-8'))
            self.assertEqual(stored['child_audit_failures'], result['child_audit_failures'])
            logs = [json.loads(s) for s in Path(result['audit_path']).read_text(encoding='utf-8').splitlines()]
            self.assertEqual(logs[-1]['child_audit_failures'], result['child_audit_failures'])
        anyio.run(run)

    def test_audit_writer_is_off_event_loop_and_cancellation_is_logged(self):
        async def run():
            main_thread = threading.get_ident()
            recorded = []
            def writer(event, fields, **kwargs):
                self.assertNotEqual(threading.get_ident(), main_thread)
                recorded.append(fields['outcome'])
                return {'status': 'disabled'}
            @m._audited
            async def operation():
                return {'ok': True}
            @m._audited
            async def cancelled():
                scope.cancel()
                await anyio.lowlevel.checkpoint()
            with patch.object(m, '_write_audit', writer):
                await operation()
                with anyio.CancelScope() as scope:
                    await cancelled()
            self.assertEqual(recorded, ['success', 'cancelled'])
            self.assertIsNone(m._AUDIT_CONTEXT.get())
        anyio.run(run)


if __name__ == '__main__':
    unittest.main()

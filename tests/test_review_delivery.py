"""Review handoff regression. All counts, IDs and dates here are project-defined fixtures."""
import csv
import importlib.util
import json
import os
import re
import shutil
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
REAL_FETCH = m._efetch_xml_all
REAL_ESEARCH = m._esearch  # 真实包装层：测试内还原它，让日期/范围说明真正进入 warnings


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

    def dated_esearch(self, datetype='pdat'):
        """Echo the requested absolute window the way NCBI does; dates stay project fixtures."""
        def ncbi_echo(text):
            return re.sub(r'(\d{4})-(\d{2})-(\d{2}):(\d{4})-(\d{2})-(\d{2})\[(pdat|edat)\]',
                          lambda m: (f'{m[1]}/{m[2]}/{m[3]}:{m[4]}/{m[5]}/{m[6]}'
                                     f'[{"Date - Publication" if m[7] == "pdat" else "Date - Entry"}]'), text)
        async def esearch(term, **kwargs):
            ids = ['1'] if '[uid]' in term else ['1', '2']
            qt = term
            lo, hi = kwargs.get('mindate'), kwargs.get('maxdate')
            if '[uid]' in term and (lo or hi):
                start = r.services.expand_date(lo)
                end = r.services.expand_date(hi, True)
                qt = f"{term} AND {start}:{end}[{datetype}]"
            return {'count': str(len(ids)), 'idlist': ids if kwargs.get('retmax', 0) else [],
                    'querytranslation': ncbi_echo(qt), '_term': term}
        return esearch

    def rewrite_json(self, relative, mutate):
        path = self.out / relative
        value = r.read_json(path)
        mutate(value)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return path

    def rebind(self, relatives, stage):
        state_path = self.out / 'state.json'
        state = r.read_json(state_path)
        for relative in relatives:
            state['stages'][stage]['files'][relative] = r.digest(self.out / relative)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        delivery_path = self.out / 'delivery.json'
        delivery = r.read_json(delivery_path)
        for relative in relatives:
            delivery['files'][relative] = r.digest(self.out / relative)
        delivery['files']['state.json'] = r.digest(state_path)
        delivery_path.write_text(json.dumps(delivery, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def rewrite_recall(self, mutate):
        name = 'recall-00000'
        receipt_path = self.out / 'recall' / (name + '.json')
        state_path = self.out / 'state.json'
        state = r.read_json(state_path)
        result = json.loads(json.dumps(state['stages'][name]['result']))
        mutate(result)
        state['stages'][name]['result'] = result
        receipt_path.write_text(json.dumps({**result, 'evidence': r.EVIDENCE},
                                           ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        state['stages'][name]['files'][receipt_path.relative_to(self.out).as_posix()] = r.digest(receipt_path)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        delivery_path = self.out / 'delivery.json'
        delivery = r.read_json(delivery_path)
        delivery['files'][receipt_path.relative_to(self.out).as_posix()] = r.digest(receipt_path)
        delivery['files']['state.json'] = r.digest(state_path)
        delivery_path.write_text(json.dumps(delivery, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def wire_esearch(self):
        """esearch 线上响应的最小真实形状；让真实 _esearch 包装层的日期说明照常生成。"""
        def ncbi_echo(text):
            return re.sub(r'(\d{4})-(\d{2})-(\d{2}):(\d{4})-(\d{2})-(\d{2})\[(pdat|edat)\]',
                          lambda m: (f'{m[1]}/{m[2]}/{m[3]}:{m[4]}/{m[5]}/{m[6]}'
                                     f'[{"Date - Publication" if m[7] == "pdat" else "Date - Entry"}]'), text)
        async def wire(endpoint, params, timeout=0):
            self.assertEqual(endpoint, 'esearch.fcgi')
            term = params.get('term', '')
            ids = ['1'] if '[uid]' in term else ['1', '2']
            retmax = int(params.get('retmax', 0) or 0)
            retstart = int(params.get('retstart', 0) or 0)
            expected = min(retmax, max(0, len(ids) - retstart), m.WINDOW - retstart)
            translated = term
            if params.get('mindate') or params.get('maxdate'):
                # NCBI 把外置日期参数并入实际执行式回显；这是本次夹具里真实包装层要核对的形状。
                translated = (f"({term}) AND ({params['mindate']}:{params['maxdate']}"
                              f"[{params.get('datetype', 'pdat')}])")
            return json.dumps({'esearchresult': {
                'count': str(len(ids)), 'idlist': ids[retstart:retstart + max(0, expected)],
                'querytranslation': ncbi_echo(translated),
                'retstart': str(retstart), 'retmax': str(retmax)}})
        return wire

    def rewrite_search(self, mutate):
        state_path = self.out / 'state.json'
        state = r.read_json(state_path)
        result = json.loads(json.dumps(state['stages']['search']['result']))
        mutate(result)
        state['stages']['search']['result'] = result
        (self.out / 'search-result.json').write_text(
            json.dumps({**result, 'evidence': r.EVIDENCE}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        meta_path = self.out / 'all-pmids.txt.meta.json'
        meta = r.read_json(meta_path)
        mutate(meta)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        self.rebind(['search-result.json', 'all-pmids.txt.meta.json'], 'search')

    def rewrite_meta(self, mutate):
        """只改伴随说明，保留阶段回执；用于一侧缺失的反向对照。"""
        path = self.out / 'all-pmids.txt.meta.json'
        value = r.read_json(path)
        mutate(value)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        self.rebind(['all-pmids.txt.meta.json'], 'search')

    def drop_warning_contract(self, issues, status):
        """把当前交付改写成升级前完成的形状：有新范围字段，但没有警告口径声明。"""
        path = self.out / 'delivery.json'
        delivery = r.read_json(path)
        delivery.pop('warning_contract')
        delivery['issues'] = issues
        delivery['status'] = status
        path.write_text(json.dumps(delivery, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

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

    def test_other_abstract_and_empty_labels_through_delivery_and_resume(self):
        def node(pid, main, other):
            return (f'<PubmedArticle><MedlineCitation><PMID>{pid}</PMID><Article><ArticleTitle>Fixture</ArticleTitle>'
                    f'<Abstract><AbstractText Label="EMPTY"> </AbstractText>{main}</Abstract></Article>'
                    f'<OtherAbstract Type="Publisher" Language="fre"><AbstractText Label="EMPTY"/>{other}</OtherAbstract>'
                    '</MedlineCitation></PubmedArticle>')
        xml = '<PubmedArticleSet>' + node('1', '', '<AbstractText Label="RESULTS">Alternative body.</AbstractText>') + node('2', '', '') + '</PubmedArticleSet>'  # 本项目自订：仅替代正文与全部为空各一条
        async def wire(*args, **kwargs):
            return xml
        with patch.object(m, '_get_async', wire), patch.object(m, '_efetch_xml_all', REAL_FETCH):
            result = self.execute()
        self.assertEqual(result['metrics']['no_abstract_pmids'], ['2'])  # 本项目自订：标签空白才无摘要
        with (self.out / 'screening.csv').open(encoding='utf-8-sig', newline='') as stream:
            alt, empty = csv.DictReader(stream)
        self.assertEqual(alt['abstract_status'], 'other_abstract_only')
        self.assertEqual(empty['abstract_status'], 'not_provided')
        self.assertEqual(alt['abstract'], '[OtherAbstract Publisher/fre]\nRESULTS: Alternative body.')
        self.assertTrue(all(row['reviewer_decision'] == '待筛选' for row in (alt, empty)))
        ris = (self.out / 'references.ris').read_text(encoding='utf-8')
        self.assertIn('AB  - [OtherAbstract Publisher/fre] RESULTS: Alternative body.', ris)
        self.assertNotIn('EMPTY:', ris)
        self.assertEqual(r.verify(self.out)['status'], 'verified')
        resumed = anyio.run(r.run, None, None, str(self.out))
        self.assertEqual(resumed['metrics'], result['metrics'])

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

    def test_returned_but_unverified_known_paper_keeps_handoff_under_review(self):
        export_search = m._esearch
        async def esearch(term, **kwargs):
            page = await export_search(term, **kwargs)
            if '[uid]' in term:
                page['querytranslation'] = ''
            return page
        with patch.object(m, '_esearch', esearch):
            result = self.execute()
        receipt, = (self.out / 'recall').glob('*.json')
        check = r.read_json(receipt)
        self.assertEqual(check['retrieved'], check['unknown'])
        self.assertEqual(check['unknown'], ['1'])  # 本项目自订：已知 PMID 实取但执行式缺失
        self.assertEqual((check['found'], check['missing']), ([], []))
        self.assertEqual(result['handoff_status'], 'needs_methodology_review')
        self.assertIn('recall_unresolved', {i['code'] for i in r.read_json(self.out / 'delivery.json')['issues']})
        self.assertEqual(r.verify(self.out)['status'], 'verified')

    def test_partial_recall_timeout_keeps_receipt_but_blocks_screening(self):
        second = '2'  # 本项目自订：在导出名单内，唯独该号的复查请求失败
        self.plan['known_relevant'].append({'pmid': second, 'source': 'independent fixture'})
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        original = m._esearch
        async def esearch(term, **kwargs):
            if second + '[uid]' in term:
                raise m.NcbiError('fixture timeout')
            return await original(term, **kwargs)
        with patch.object(m, '_esearch', esearch), patch.object(m, 'GOLD_CHUNK', 1):  # 本项目自订：每号单独请求
            handoff = self.execute()
        receipt, = (self.out / 'recall').glob('*.json')
        data = r.read_json(receipt)
        self.assertEqual(data['found'], ['1'])  # 本项目自订：成功批次
        self.assertEqual(data['unknown'], [second])
        self.assertFalse(data['missing'])
        self.assertEqual(handoff['handoff_status'], 'needs_execution_repair')
        self.assertIn('recall_execution', {i['code'] for i in r.read_json(self.out / 'delivery.json')['issues']})
        self.assertEqual(r.verify(self.out)['status'], 'verified')
        with (self.out / 'search-log.csv').open(encoding='utf-8-sig', newline='') as stream:
            row = next(v for v in csv.DictReader(stream) if v['event'] == 'check_recall')
        self.assertEqual(json.loads(row['querytranslation']), data['querytranslations'])
        first_digest = r.digest(receipt)
        requests = []
        online = False
        def retry(endpoint, params, *args, **kwargs):
            term = params['term']
            requests.append(term)
            self.assertIn(second + '[uid]', term)
            self.assertNotIn('1[uid]', term)  # 本项目自订：已成功来源不能重查
            if not online:
                raise m.NcbiError('still offline')
            return json.dumps({'esearchresult': {'count': '1', 'idlist': [second], 'querytranslation': term}})  # 本项目自订：恢复后确认一个号
        with patch.object(m, '_esearch', REAL_ESEARCH), patch.object(m, '_get', retry):
            with patch.object(r, 'verify', wraps=r.verify) as verify:
                pending = anyio.run(r.run, None, None, str(self.out))
            self.assertEqual([call.args for call in verify.call_args_list].count((self.out,)), 1)  # 本项目自订：原路径仅作一次持锁核验
            self.assertEqual(pending['handoff_status'], 'needs_execution_repair')
            online = True
            repaired = anyio.run(r.run, None, None, pending['directory'])
        self.assertEqual(repaired['handoff_status'], 'ready_for_screening')
        self.assertEqual(len(requests), 2)  # 本项目自订：两次恢复各取一个失败批次
        self.assertEqual(r.digest(receipt), first_digest)
        self.assertEqual(self.calls, [['1', second]])  # 本项目自订：未重取已完成的题录
        self.assertEqual(r.verify(Path(repaired['directory']))['status'], 'verified')
        current = Path(repaired['directory'])
        prior = r.read_json(current / 'original' / 'state.json')['stages']['recall-00000']  # 本项目自订：首个回执
        result = r.read_json(current / 'state.json')['stages']['recall-00000']['result']  # 本项目自订：首个回执
        event = json.loads(r.AuditIndex(current).report_records['event', result['audit_event_id']])
        self.assertNotIn('previous', event['input_params'])
        self.assertEqual(event['previous_sha256'], r.digest(current / 'original' / 'recall' / receipt.name))
        for case in ('prior_event', 'changed_search', 'translation', 'observed'):
            with self.subTest(case=case):
                base = current if case != 'observed' else Path(pending['directory'])
                self.out = Path(shutil.copytree(base, self.root / case))
                if case == 'prior_event':
                    self.rewrite_recall(lambda v: v.update(audit_event_id=prior['result']['audit_event_id']))
                    self.rewrite_json('state.json', lambda v: v['stages']['recall-00000'].update(  # 本项目自订：绑定确实存在的旧事件
                        audit_event_sha256=prior['audit_event_sha256']))
                    self.rebind([], 'recall-00000')  # 本项目自订：首个回执
                    reason = '重试审计事件'
                elif case == 'changed_search':
                    self.rewrite_search(lambda v: v.update(warnings=['different search warning']))
                    reason = '原检索阶段'
                elif case == 'translation':
                    self.rewrite_recall(lambda v: v['querytranslations'].reverse())
                    reason = '汇总执行式'
                else:
                    self.rewrite_recall(lambda v: v['checks'][-1].update(error_kind='invalid_intersection', observed_pmids=[second]))
                    reason = '异常回包'
                with self.assertRaisesRegex(ValueError, reason):
                    r.verify(self.out)

    def test_verification_reads_its_own_large_audit_field(self):
        self.execute()
        index = r.AuditIndex(self.out)
        previous = csv.field_size_limit(128)  # 本项目自订：缩小解析上限模拟长列，避免膨胀夹具
        try:
            self.assertTrue(any(len(row) > csv.field_size_limit() for row in index.report_records.values()))
            self.assertEqual(r.verify(self.out)['status'], 'verified')
            self.assertEqual(csv.field_size_limit(), 128)  # 本项目自订：核验结束恢复原设置
        finally:
            csv.field_size_limit(previous)

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

    def test_entry_date_without_limits_is_informational(self):
        self.plan['datetype'] = 'edat'
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        with patch.object(m, '_esearch', REAL_ESEARCH), patch.object(m, '_get_async', self.wire_esearch()):
            result = self.execute()
        receipt = r.read_json(self.out / 'search-result.json')
        self.assertEqual(receipt['scope_status'], 'not_requested')
        self.assertTrue(any('未用于日期筛选' in line for line in receipt['warnings']))
        self.assertEqual(r.actionable_warnings(receipt['warnings']), [])
        self.assertEqual(result['handoff_status'], 'ready_for_screening')
        self.assertEqual(r.verify(self.out)['handoff_status'], 'ready_for_screening')

    def test_each_id_page_keeps_its_frozen_date_verdict(self):
        self.plan.update(term='tooth', mindate='2020-01-01', maxdate='2020-12-31')  # 本项目自订：冻结日期夹具
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        dated = self.dated_esearch()
        for bad_offset in (0, 1):  # 本项目自订：首页或下一页丢失日期
            with self.subTest(bad_offset=bad_offset):
                self.out = self.root / f'bad-page-{bad_offset}'
                async def search(term, **kw):
                    page = await dated(term, **kw)
                    if kw.get('retmax') and '[uid]' not in term:
                        start = kw.get('retstart', 0)  # 本项目自订：每页只返回一个测试 PMID
                        page['idlist'] = page['idlist'][start:start + 1]  # 本项目自订：分页夹具
                        if start == bad_offset:
                            page['querytranslation'] = '"tooth"[All Fields]'
                    return page
                with patch.object(m, '_esearch', search):
                    result = self.execute()
                receipt = r.read_json(self.out / 'search-result.json')
                self.assertEqual(receipt['scope_status'], 'unverified')
                pages = [c for c in receipt['retrieval_checks'] if 'retstart=' in c['stage']]
                self.assertEqual([c['stage'] for c in pages if c['query_needs_review'] and c['review_reasons']], [f'export：retstart={bad_offset}'])
                self.assertEqual((self.out / 'all-pmids.txt').read_text('utf-8').split(), ['1', '2'])  # 本项目自订：两个原始候选
                self.assertEqual(result['handoff_status'], 'needs_execution_repair')
                self.assertEqual(r.verify(self.out)['status'], 'verified')

    def test_real_date_scope_notes_do_not_block_ready(self):
        self.plan.update(mindate='2020-01-01', maxdate='2020-12-31')  # 本项目自订：绝对日期夹具
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        with patch.object(m, '_esearch', REAL_ESEARCH), patch.object(m, '_get_async', self.wire_esearch()):
            result = self.execute()
        self.assertEqual(result['handoff_status'], 'ready_for_screening')
        delivery = r.read_json(self.out / 'delivery.json')
        self.assertEqual(delivery['warning_contract'], r.WARNING_CONTRACT)
        self.assertNotIn('recall_warnings', {issue['code'] for issue in delivery['issues']})
        receipt = r.read_json(next((self.out / 'recall').glob('*.json')))
        self.assertTrue(any(note.startswith('日期条件：') for note in receipt['warnings']))
        self.assertEqual(r.actionable_warnings(receipt['warnings']), [])
        self.assertEqual(r.verify(self.out)['status'], 'verified')

        async def wrapper_page():
            with patch.object(m, '_get_async', self.wire_esearch()):
                return await REAL_ESEARCH('fixture[tiab]', retmax=0, mindate='2020-01-01',
                                          maxdate='2020-12-31', datetype='pdat')
        page = anyio.run(wrapper_page)
        staged = m._retrieval_check(page, 'export 起始计数：')['warnings']
        self.assertTrue(any(note.startswith('export 起始计数： 日期条件：') for note in staged))
        self.assertEqual(r.actionable_warnings(staged), [])
        # 分页检查的阶段标签带偏移（不以「：」收尾），说明行同样不能升级成待解决警告。
        offset = m._retrieval_check(page, 'export：retstart=0')['warnings']
        self.assertTrue(any('日期条件：' in note for note in offset))
        self.assertEqual(r.actionable_warnings(offset), [])
        # 反向对照：同一阶段前缀下真正的问题（执行式缺失）必须仍被当作待解决警告。
        missing = dict(page, querytranslation='')
        flagged = r.actionable_warnings(m._retrieval_check(missing, 'export 起始计数：')['warnings'])
        self.assertTrue(any(note.startswith('export 起始计数： ⚠️') for note in flagged))

    def test_prior_warning_classification_reproduces_prior_status(self):
        warnings = ['export 起始计数： 日期条件：2020-01-01 → 2020-12-31（datetype=pdat＝发表日期）',
                    '（原始输入 2 个，去重后 1 个）']  # 本项目自订：真实包装层会写出的说明行形状
        self.assertEqual(r.actionable_warnings(warnings), [])
        self.assertTrue(r.actionable_warnings(warnings, legacy=True))
        exported = {'integrity_status': 'no_discrepancy_detected', 'metadata_status': 'written',
                    'warnings': list(warnings), 'querytranslation': 'fixture',
                    'unique': 1, 'audit_status': 'written'}
        screening = {'missing_pmids': []}
        self.assertNotIn('query_warnings',
                         {issue['code'] for issue in r.assess(self.plan, exported, [], [], screening)})
        self.assertIn('query_warnings', {issue['code'] for issue in
                       r.assess(self.plan, exported, [], [], screening, warning_contract=None)})

    def test_scope_fields_bind_to_plan_sidecar_and_status(self):
        self.plan.update(mindate='2020-01-01', maxdate='2020-12-31')
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        with patch.object(m, '_esearch', self.dated_esearch()):
            self.execute()
        exported = r.read_json(self.out / 'search-result.json')
        self.assertEqual((exported['field'], exported['frozen_bounds'], exported['scope_status']),
                         ('', ['2020-01-01:2020-12-31'], 'verified'))
        self.assertEqual(r.verify(self.out)['status'], 'verified')

        originals = {name: (self.out / name).read_bytes() for name in
                     ('state.json', 'delivery.json', 'search-result.json', 'all-pmids.txt.meta.json')}
        scenarios = {
            'field_not_from_plan': lambda x: x.update(field='ti'),
            'frozen_bounds_missing': lambda x: x.update(frozen_bounds=[]),
            'scope_status_contradicts': lambda x: x.update(scope_status='not_requested'),
            'partial_new_fields': lambda x: x.pop('scope_status'),
        }
        for label, mutate in scenarios.items():
            with self.subTest(scenario=label):
                self.rewrite_search(mutate)
                with self.assertRaisesRegex(ValueError, '检索回执|范围'):
                    r.verify(self.out)
                for name, payload in originals.items():
                    (self.out / name).write_bytes(payload)
        with self.subTest(scenario='one_sided_sidecar'):
            self.rewrite_meta(lambda x: x.pop('frozen_bounds'))
            with self.assertRaisesRegex(ValueError, '检索伴随说明与回执不一致：frozen_bounds'):
                r.verify(self.out)
            for name, payload in originals.items():
                (self.out / name).write_bytes(payload)

    def test_unverified_slice_reaches_handoff_without_false_scope_contradiction(self):
        from test_rc_slices import search_fixture
        self.plan.update(term='tooth', known_relevant=[])
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        for omit_date in (False, True):
            with self.subTest(omit_date=omit_date):
                self.out = self.root / ('missing-date' if omit_date else 'changed-topic')
                fetch = search_fixture([], wrong_leaf=True)
                async def page(term, **kw):
                    result = await fetch(term, **kw)
                    if omit_date and kw.get('mindate') == kw.get('maxdate') == '2020-01-01':  # 本项目自订：问题叶片
                        result['querytranslation'] = '"jaw"[Title]'
                    return result
                with patch.object(m, '_esearch', page), patch.object(m, 'WINDOW', 1), patch.object(m, 'NO_BEGIN', '2020-01-01'), patch.object(m, 'NO_END', '2020-01-04'):  # 本项目自订：强制分片
                    result = self.execute()
                checked = r.verify(self.out)
                self.assertEqual(checked['status'], 'verified')
                self.assertEqual(checked['handoff_status'], result['handoff_status'])
                self.assertNotEqual(checked['handoff_status'], 'ready_for_screening')
                receipt = r.read_json(self.out / 'search-result.json')
                self.assertIn('slice_topic_unverified', receipt['integrity_issues'])
                self.assertEqual(receipt['scope_status'], 'unverified' if omit_date else 'not_requested')
                self.assertEqual((self.out / 'all-pmids.txt').read_text('utf-8').split(), ['999', '2'])  # 本项目自订：错误叶片的实取号照留

    def test_legacy_scope_stage_shape_still_verifies_under_new_contract(self):
        # 恢复或补取旧阶段时，search 回执可能是升级前形状；新交付仍按新警告口径核验，不因此拒绝。
        self.execute()
        self.assertEqual(r.read_json(self.out / 'search-result.json')['scope_status'], 'not_requested')
        self.rewrite_search(lambda x: [x.pop(key) for key in r.EXPORT_SCOPE_FIELDS])
        checked = r.verify(self.out)
        self.assertEqual((checked['status'], checked['handoff_status']), ('verified', 'ready_for_screening'))

    def test_prior_completed_handoff_without_warning_contract_still_verifies(self):
        self.plan.update(mindate='2020-01-01', maxdate='2020-12-31')
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        with patch.object(m, '_esearch', REAL_ESEARCH), patch.object(m, '_get_async', self.wire_esearch()):
            self.execute()
        state = r.read_json(self.out / 'state.json')
        plan = r.validate_plan(r.read_json(self.out / 'plan.json'))
        exported = state['stages']['search']['result']
        recalls = [state['stages'][name]['result'] for name in state['stages'] if name.startswith('recall-')]
        batches = [state['stages'][name]['result'] for name in state['stages'] if name.startswith('records-')]
        legacy_issues = r.assess(plan, exported, recalls, batches, state['stages']['screening']['result'],
                                 warning_contract=None)
        self.assertIn('recall_warnings', {issue['code'] for issue in legacy_issues})
        status = ('needs_execution_repair' if any(issue['kind'] == 'execution' for issue in legacy_issues) else
                  'needs_methodology_review' if legacy_issues else 'ready_for_screening')
        self.assertEqual(status, 'needs_methodology_review')
        original = (self.out / 'delivery.json').read_bytes()
        self.drop_warning_contract(legacy_issues, status)
        checked = r.verify(self.out)
        self.assertEqual((checked['status'], checked['handoff_status']), ('verified', 'needs_methodology_review'))
        # 反向对照：新口径交付删掉声明后不能借旧口径放行。
        (self.out / 'delivery.json').write_bytes(original)
        delivery = r.read_json(self.out / 'delivery.json')
        delivery.pop('warning_contract')
        (self.out / 'delivery.json').write_text(json.dumps(delivery, ensure_ascii=False, indent=2) + '\n',
                                                encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '交付问题清单与证据不一致'):
            r.verify(self.out)

    def test_pre_scope_field_handoff_still_verifies(self):
        self.execute()
        self.rewrite_search(lambda x: [x.pop(key) for key in r.EXPORT_SCOPE_FIELDS])
        delivery_path = self.out / 'delivery.json'
        delivery = r.read_json(delivery_path)
        delivery.pop('warning_contract')
        delivery_path.write_text(json.dumps(delivery, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        checked = r.verify(self.out)
        self.assertEqual((checked['status'], checked['handoff_status']), ('verified', 'ready_for_screening'))

    def test_slash_dates_normalized_in_plan_and_recall(self):
        self.plan.update(mindate='2020/01/01', maxdate='2020/12/31')
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        with patch.object(m, '_esearch', self.dated_esearch()):
            result = self.execute()
        self.assertEqual(result['handoff_status'], 'ready_for_screening')
        stored = r.read_json(self.out / 'plan.json')
        self.assertEqual((stored['mindate'], stored['maxdate']), ('2020-01-01', '2020-12-31'))
        receipt, = (self.out / 'recall').glob('*.json')
        check = r.read_json(receipt)
        self.assertEqual(check['term'], 'fixture[tiab]')
        self.assertEqual(check['scope'], {'field': '', 'mindate': '2020-01-01', 'maxdate': '2020-12-31',
                                          'reldate': 0, 'datetype': 'pdat'})
        self.assertEqual(check['receipt_binding'], r.RECALL_BINDING)
        self.assertEqual(r.verify(self.out)['status'], 'verified')

    def test_new_recall_receipt_must_bind_plan_term_and_window(self):
        self.plan.update(mindate='2020/01/01', maxdate='2020/12/31')
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        with patch.object(m, '_esearch', self.dated_esearch()):
            self.execute()
        effective = r.read_json(self.out / 'search-result.json')['effective_query']
        originals = {name: (self.out / name).read_bytes()
                     for name in ('state.json', 'delivery.json', 'recall/recall-00000.json')}
        scenarios = {
            'effective_query_term': lambda x: x.update(term=effective),
            'wrong_window': lambda x: x['scope'].update(maxdate='2021-12-31'),
            'missing_marker': lambda x: x.pop('receipt_binding'),
            'unknown_marker': lambda x: x.update(receipt_binding='other_contract'),
            'missing_checks': lambda x: x.pop('checks'),
        }
        for label, mutate in scenarios.items():
            with self.subTest(scenario=label):
                self.rewrite_recall(mutate)
                with self.assertRaisesRegex(ValueError, '已知集'):
                    r.verify(self.out)
                for name, payload in originals.items():
                    (self.out / name).write_bytes(payload)

    def test_prior_effective_query_receipt_is_preserved(self):
        self.plan.update(mindate='2020', maxdate='2020')
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        with patch.object(m, '_esearch', self.dated_esearch()):
            self.execute()
        effective = r.read_json(self.out / 'search-result.json')['effective_query']

        def as_prior(result):
            result.pop('receipt_binding')
            result['term'] = effective
            result['scope'] = {'field': '', 'mindate': '', 'maxdate': '', 'reldate': 0, 'datetype': 'pdat'}
        self.rewrite_recall(as_prior)
        checked = r.verify(self.out)
        self.assertEqual((checked['status'], checked['handoff_status']), ('verified', 'ready_for_screening'))

    def test_preupgrade_alternative_only_output_verifies_without_rewriting(self):
        def node(pid, main, other):
            return (f'<PubmedArticle><MedlineCitation><PMID>{pid}</PMID><Article><ArticleTitle>Fixture</ArticleTitle>'
                    f'<Abstract><AbstractText Label="EMPTY"> </AbstractText>{main}</Abstract></Article>'
                    f'<OtherAbstract Type="Publisher" Language="fre"><AbstractText Label="EMPTY"/>{other}</OtherAbstract>'
                    '</MedlineCitation></PubmedArticle>')
        xml = '<PubmedArticleSet>' + node('1', '', '<AbstractText Label="RESULTS">Alternative body.</AbstractText>') + node('2', '', '') + '</PubmedArticleSet>'  # 本项目自订：仅替代正文与全部为空各一条
        async def wire(*args, **kwargs):
            return xml
        with patch.object(m, '_get_async', wire), patch.object(m, '_efetch_xml_all', REAL_FETCH):
            self.execute()
        ids = ['1', '2']
        # 模拟升级前的题录快照：旧解析器会保留「有标签、无正文」的主摘要区段。
        batch = 'records/batch-00000.json'
        self.rewrite_json(batch, lambda data: [
            rec.update(abstract=[{'label': 'EMPTY', 'text': ''}]) for rec in data['records'] if rec['pmid'] == '1'])
        state_path = self.out / 'state.json'
        state = r.read_json(state_path)
        state['stages']['records-00000']['result']['sha256'] = r.digest(self.out / batch)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        self.rebind([batch], 'records-00000')
        # 用上一版本的筛选与 RIS 规则重写派生产物。
        materials = list(r.screening_materials(self.out, ids, legacy=True))
        with (self.out / 'screening.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=r.COLUMNS)
            writer.writeheader()
            writer.writerows([row for row, _ in materials])
        (self.out / 'references.ris').write_text(''.join(block for _, block in materials), encoding='utf-8')
        self.rebind(['screening.csv', 'references.ris'], 'screening')
        with (self.out / 'screening.csv').open(encoding='utf-8-sig', newline='') as stream:
            alt, empty = csv.DictReader(stream)
        self.assertEqual((alt['abstract_status'], alt['abstract']), ('available', 'EMPTY: '))
        self.assertIn('AB  - EMPTY:', (self.out / 'references.ris').read_text(encoding='utf-8'))
        before = {name: (self.out / name).read_bytes() for name in ('screening.csv', 'references.ris')}
        checked = r.verify(self.out)
        self.assertEqual((checked['status'], checked['handoff_status']), ('verified', 'ready_for_screening'))
        self.assertEqual(before, {name: (self.out / name).read_bytes() for name in before})
        with patch.object(m, '_efetch_xml_all', lambda ids: (_ for _ in ()).throw(AssertionError('must not refetch'))):
            resumed = anyio.run(r.run, None, None, str(self.out))
        self.assertEqual(resumed['handoff_status'], 'ready_for_screening')

    def test_unchanged_output_mismatch_is_not_called_human_edits(self):
        self.execute()
        path = self.out / 'screening.csv'
        path.write_text(path.read_text(encoding='utf-8-sig').replace('Study 2', 'Changed title'), encoding='utf-8-sig')
        self.rebind(['screening.csv'], 'screening')
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, '筛选表与题录') as caught:
            r.verify(self.out)
        message = str(caught.exception)
        self.assertNotIn('人工修改', message)
        self.assertIn('原版本', message)
        self.assertIn('新建任务', message)
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

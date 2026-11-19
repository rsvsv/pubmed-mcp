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


PUB_DATE = 'Date - Publication'


def esearch_reply(ids, count=None, translation=''):
    """离线响应用本项目自订夹具；translation 为模拟 NCBI 实际执行式。"""
    return {'_term': 'tooth', 'count': str(len(ids) if count is None else count),
            'idlist': list(ids), 'querytranslation': translation}


def pub_window(lo, hi):
    return f'{lo.replace("-", "/")}:{hi.replace("-", "/")}[{PUB_DATE}]'


class ExportFieldScope(unittest.TestCase):
    """export 的 field/日期范围下放与逐次核对；夹具值均为本项目自订测试输入。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {'PUBMED_AUDIT_DIR': ''})
        env.start()
        self.addCleanup(env.stop)

    def run_export(self, **kwargs):
        async def run():
            return json.loads(await m.export('tooth', str(self.root / kwargs.pop('name', 'data.txt')),
                                             as_json=True, **kwargs))
        return anyio.run(run)

    def test_field_preserved_on_baseline_window_and_end_requests(self):
        qt = '"tooth"[Title] AND ' + pub_window('2020-01-01', '2021-12-31')
        calls = []

        async def search(term, **kw):
            calls.append((term, dict(kw)))
            if kw.get('retmax') == 0:
                return esearch_reply([], count=2, translation=qt)
            return esearch_reply(['1', '2'], count=2, translation=qt)

        with patch.object(m, '_esearch', search):
            result = self.run_export(field='Title', mindate='2020', maxdate='2021')
        self.assertEqual(len(calls), 3)  # 本项目自订：起始计数、窗口取号、结束计数
        for term, kw in calls:
            self.assertEqual(kw.get('field'), 'Title')
            self.assertEqual(term, '(tooth) AND (2020-01-01:2021-12-31[pdat])')
        self.assertEqual(result['field'], 'Title')
        self.assertEqual(result['scope_status'], 'verified')
        self.assertEqual(result['integrity_status'], 'no_discrepancy_detected')
        self.assertNotIn('scope_unverified', result['integrity_issues'])
        self.assertFalse(any('范围未核实' in w for w in result['warnings']))

    def test_field_preserved_through_slice_count_and_page_requests(self):
        slices = {}
        calls = []

        async def search(term, **kw):
            calls.append((term, dict(kw)))
            lo, hi = kw.get('mindate') or '', kw.get('maxdate') or ''
            frozen = pub_window('2020-01-01', '2020-01-02')
            extra = '' if (lo, hi) in (('', ''), ('2020-01-01', '2020-01-02')) else ' AND ' + pub_window(lo, hi)
            translation = '"tooth"[Title] AND ' + frozen + extra
            if kw.get('retmax') == 0:
                if (lo, hi) == ('2020-01-01', '2020-01-02'):
                    slices['root'] = dict(kw)
                    return esearch_reply([], count=2, translation=translation)
                if (lo, hi) == ('2020-01-01', '2020-01-01'):
                    slices['left'] = dict(kw)
                    return esearch_reply([], count=1, translation=translation)
                if (lo, hi) == ('2020-01-02', '2020-01-02'):
                    slices['right'] = dict(kw)
                    return esearch_reply([], count=1, translation=translation)
                return esearch_reply([], count=2, translation=translation)
            if (lo, hi) == ('2020-01-01', '2020-01-01'):
                return esearch_reply(['1'], count=1, translation=translation)
            return esearch_reply(['2'], count=1, translation=translation)

        with patch.object(m, 'WINDOW', 1), \
                patch.object(m, 'NO_BEGIN', '2020-01-01'), patch.object(m, 'NO_END', '2020-01-02'), \
                patch.object(m, '_esearch', search):
            result = self.run_export(field='Title', mindate='2020-01-01', maxdate='2020-01-02')
        self.assertTrue(slices)
        for term, kw in calls:
            self.assertEqual(kw.get('field'), 'Title')
        self.assertEqual(result['scope_status'], 'verified')
        self.assertEqual(result['integrity_status'], 'no_discrepancy_detected')
        self.assertEqual((self.root / 'data.txt').read_text().split(), ['1', '2'])

    def test_same_window_translation_change_keeps_ids_for_review(self):
        ids = ['11', '22']  # 本项目自订：两个分页用的 PMID
        for changed in ('none', 'first', 'later', 'end'):
            stages = iter(('start', 'first', 'later', 'end'))
            async def wire(path, params, timeout):
                qt = 'jaw[Title]' if next(stages) == changed else 'tooth[Title]'
                got = ids[params['retstart']:][:params['retmax']]
                return json.dumps({'esearchresult': {'count': str(len(ids)), 'idlist': got, 'querytranslation': qt}})
            with self.subTest(changed=changed), patch.object(m, 'RETMAX_CAP', 1), patch.object(m, '_get_async', wire):  # 本项目自订：一页一条，走真实请求包装
                report = self.run_export(name=changed, field='Title')
                self.assertEqual((self.root / changed).read_text().split(), ids)
                expected = 'no_discrepancy_detected' if changed == 'none' else 'needs_review'
                self.assertEqual(report['integrity_status'], expected)
                if changed != 'none':
                    self.assertTrue(any('执行式' in w and '同一窗口' in w for w in report['warnings']))

    def test_leaf_id_page_keeps_user_window_as_well_as_slice_window(self):
        outer = ('2020-01-01', '2020-12-31')  # 本项目自订：用户窗口
        inner = (outer[0], outer[0])  # 本项目自订：取号分片
        base = 'tooth[Title] AND ' + pub_window(*inner)
        for keep_outer in (True, False):
            async def search(term, **kw):
                fetching = bool(kw.get('retmax'))
                qt = base if fetching and not keep_outer else base + ' AND ' + pub_window(*outer)
                return esearch_reply(['1'] if fetching else [], count=1, translation=qt)  # 本项目自订：仅一条候选
            state = {}
            with self.subTest(keep_outer=keep_outer), patch.object(m, '_esearch', search):
                ids, _, _ = anyio.run(lambda: m._export_slice('tooth', *inner, state, bounds=(outer,)))
                self.assertEqual(ids, ['1'])  # 本项目自订：不丢失实取候选
                probe, id_page = state['checks']
                self.assertFalse(probe['query_needs_review'])
                self.assertEqual(id_page['query_needs_review'], not keep_outer)
                self.assertEqual(bool(id_page['review_reasons']), not keep_outer)
                self.assertEqual(bool(state.get('scope_unverified')), not keep_outer)

    def test_empty_slice_needs_its_own_date_not_just_user_window(self):
        outer = ('2020-01-01', '2020-12-31')  # 本项目自订：用户全年范围
        inner = (outer[0], outer[0])  # 本项目自订：其中的一天
        for has_inner in (False, True):
            qt = 'tooth[Title] AND ' + pub_window(*outer)
            if has_inner:
                qt += ' AND ' + pub_window(*inner)
            state = {}
            with self.subTest(has_inner=has_inner), patch.object(m, '_esearch', AsyncMock(return_value=esearch_reply([], translation=qt))):
                anyio.run(lambda: m._export_slice('tooth', *inner, state, field='Title', bounds=(outer,)))
                self.assertEqual(bool(state.get('scope_unverified')), not has_inner)
                self.assertEqual(state['checks'][0]['query_needs_review'], not has_inner)  # 本项目自订：空片仅一条计数记录

    def test_zero_slice_with_unverified_scope_cannot_pass(self):
        state = {'drift': []}
        bad = '"tooth"[All Fields]'

        async def search(term, **kw):
            return esearch_reply([], count=0, translation=bad)

        async def run():
            with patch.object(m, '_esearch', search):
                return await m._export_slice('tooth', '2020-01-01', '2020-12-31', state,
                                             field='Title', bounds=(('2020-01-01', '2020-12-31'),))
        ids, slices, count = anyio.run(run)
        self.assertEqual((ids, slices, count), ([], 0, 0))  # 本项目自订：空片返回计数口径不变
        self.assertTrue(state['scope_unverified'])
        self.assertIn('计数探测', state['scope_unverified'][0]['stage'])

    def test_count_only_execution_with_unverified_scope_requires_review(self):
        bad = '"tooth"[All Fields]'

        async def search(term, **kw):
            return esearch_reply([], count=0, translation=bad)

        with patch.object(m, '_esearch', search):
            result = self.run_export(field='Title', mindate='2020', maxdate='2021')
        self.assertEqual(result['count_start'], 0)
        self.assertEqual(result['scope_status'], 'unverified')
        self.assertEqual(result['integrity_status'], 'needs_review')
        self.assertIn('scope_unverified', result['integrity_issues'])
        self.assertTrue(any('字段/日期范围未核实' in w for w in result['warnings']))
        self.assertEqual((self.root / 'data.txt').read_bytes(), b'')

    def test_end_count_with_unverified_scope_requires_review(self):
        good = '"tooth"[Title] AND ' + pub_window('2020-01-01', '2021-12-31')
        bad = '"tooth"[All Fields]'

        async def search(term, **kw):
            if kw.get('retmax') == 0:
                search.counts = getattr(search, 'counts', 0) + 1
                if search.counts >= 2:  # 本项目自订：第二次 retmax=0 是结束计数
                    return esearch_reply([], count=1, translation=bad)
                return esearch_reply([], count=1, translation=good)
            return esearch_reply(['1'], count=1, translation=good)

        with patch.object(m, '_esearch', search):
            result = self.run_export(field='Title', mindate='2020', maxdate='2021')
        self.assertEqual(result['unique'], 1)  # 本项目自订：原始名单保留
        self.assertEqual(self.root.joinpath('data.txt').read_text().split(), ['1'])
        self.assertEqual(result['scope_status'], 'unverified')
        self.assertEqual(result['integrity_status'], 'needs_review')
        self.assertTrue(any('结束计数' in w for w in result['warnings']))

    def test_relative_probe_is_not_field_limited(self):
        probe_qt = pub_window('2026-11-06', '2026-11-12')
        calls = []

        async def search(term, **kw):
            calls.append((term, dict(kw)))
            translation = ('all[sb] AND ' + probe_qt if term == 'all[sb]'
                           else '"tooth"[Title] AND ' + probe_qt)
            return esearch_reply([], count=0, translation=translation)

        with patch.object(m, '_esearch', search):
            result = self.run_export(field='Title', reldate=7)
        probe = [kw for term, kw in calls if term == 'all[sb]']
        self.assertEqual(len(probe), 1)
        self.assertNotIn('field', probe[0])
        for term, kw in calls:
            if term != 'all[sb]':
                self.assertEqual(kw.get('field'), 'Title')
        self.assertEqual(result['frozen_bounds'], ['2026-11-06:2026-11-12'])
        self.assertEqual(result['scope_status'], 'verified')

    def test_invalid_field_is_rejected_before_network(self):
        async def run(field):
            with patch.object(m, '_esearch', AsyncMock()) as net:
                with self.assertRaises(m.NcbiError):
                    await m.export('tooth', str(self.root / 'bad.txt'), field=field)
                net.assert_not_called()
            self.assertFalse((self.root / 'bad.txt').exists())
        for field in ('ZZZBOGUSFIELD', 'keywords'):  # 本项目自订：未知字段与实测被 NCBI 整条忽略的字段
            anyio.run(run, field)

    def test_no_scope_request_keeps_legacy_result(self):
        async def search(term, **kw):
            return esearch_reply(['1'], count=1, translation='"tooth"[All Fields]')

        with patch.object(m, '_esearch', search):
            result = self.run_export()
        self.assertEqual(result['field'], '')
        self.assertEqual(result['frozen_bounds'], [])
        self.assertEqual(result['scope_status'], 'not_requested')
        self.assertEqual(result['integrity_status'], 'no_discrepancy_detected')
        self.assertEqual((self.root / 'data.txt').read_text().split(), ['1'])


class DateUxAndRecordProjection(unittest.TestCase):
    """日期 UX 与题录日期投影回归；所有日期/PMID 为本项目自订夹具，不是文献事实。"""

    def note(self, **fields):
        d = {'_term': 'tooth', 'count': '1'}
        d.update(fields)
        return '\n'.join(m._note(d))

    def test_fixed_publication_range_states_inclusive_type_and_executed_window(self):
        qt = '"tooth"[All Fields] AND 2021/01/01:2021/12/31[Date - Publication]'
        text = self.note(querytranslation=qt,
                         _request_params={'mindate': '2021', 'maxdate': '2021', 'datetype': 'pdat'},
                         _daterange='2021-01-01 → 2021-12-31（datetype=pdat＝发表日期）')
        self.assertIn('首尾两端都包含', text)
        self.assertIn('按发表日期筛选（不是入库日期）', text)
        self.assertIn('NCBI 实际执行式里出现的日期窗口：2021-01-01 → 2021-12-31（发表日期[pdat]）', text)
        self.assertIn('只在 OR 某一支出现的日期不对每条记录生效', text)

    def test_fixed_entry_range_is_not_described_as_publication(self):
        qt = '"tooth"[All Fields] AND 2021/01/01:2021/12/31[Date - Entry]'
        text = self.note(querytranslation=qt,
                         _request_params={'mindate': '2021', 'maxdate': '2021', 'datetype': 'edat'},
                         _daterange='2021-01-01 → 2021-12-31（datetype=edat＝入库日期）')
        self.assertIn('按入库日期筛选（不是发表日期）', text)
        self.assertIn('2021-01-01 → 2021-12-31（入库日期[edat]）', text)

    def test_single_day_fixed_window_is_not_invented_as_range(self):
        qt = '33782057[UID] AND 2021/01/01[Date - Publication]'
        text = self.note(querytranslation=qt,
                         _request_params={'mindate': '2021-01-01', 'maxdate': '2021-01-01', 'datetype': 'pdat'},
                         _daterange='2021-01-01 → 2021-01-01（datetype=pdat＝发表日期）')
        self.assertIn('2021-01-01（发表日期[pdat]）', text)
        self.assertNotIn('2021-01-01 → 2021-01-01（发表日期[pdat]）', text)

    def test_duplicate_required_window_is_reported_not_deleted(self):
        qt = ('33782057[UID] AND 2021/01/01:2021/12/31[Date - Publication] '
              'AND 2021/01/01:2021/12/31[Date - Publication]')
        text = self.note(querytranslation=qt,
                         _request_params={'mindate': '2021', 'maxdate': '2021', 'datetype': 'pdat'},
                         _daterange='2021-01-01 → 2021-12-31（datetype=pdat＝发表日期）')
        self.assertIn('回显里同一日期窗口出现了多次', text)
        self.assertIn('×2', text)
        self.assertIn('本工具不替你删除任何一侧', text)
        self.assertNotIn('term 内日期条件与 mindate/maxdate 参数同时存在', text)

    def test_or_branch_date_is_not_claimed_duplicate_or_required(self):
        # 同一个窗口既在 OR 的一支、又作为外加参数下发：只有外加那一侧是每条记录都要求的。
        qt = ('("tooth"[All Fields] OR 2021/01/01:2021/12/31[Date - Publication]) '
              'AND 2021/01/01:2021/12/31[Date - Publication]')
        text = self.note(querytranslation=qt,
                         _request_params={'mindate': '2021', 'maxdate': '2021', 'datetype': 'pdat'},
                         _daterange='2021-01-01 → 2021-12-31（datetype=pdat＝发表日期）')
        self.assertNotIn('回显里同一日期窗口出现了多次', text)
        self.assertIn('只在 OR 某一支出现的日期', text)

    def test_cross_year_issue_and_epub_are_shown_together(self):
        rec = {'title': 'Fixture title', 'pubdate': '2010 Mar', 'epubdate': '2009 Nov 3',
               'source': 'Fixture Journal', 'pubtype': ['Journal Article']}
        full = m._fmt_record(rec, '19912982')
        brief = m._fmt_brief_record(rec, '19912982')
        self.assertIn('2010 Mar｜Epub 2009 Nov 3', full.splitlines()[0])
        self.assertIn('2010 Mar｜Epub 2009 Nov 3', brief.splitlines()[0])

    def test_same_year_or_missing_epub_is_not_expanded(self):
        same = {'title': 'T', 'pubdate': '2010 Mar', 'epubdate': '2010 Jan 3', 'source': 'J'}
        missing = {'title': 'T', 'pubdate': '2010 Mar', 'epubdate': '', 'source': 'J'}
        unknown = {'title': 'T', 'pubdate': '', 'epubdate': '2009 Nov 3', 'source': 'J'}
        for rec in (same, missing, unknown):
            with self.subTest(rec=rec):
                self.assertNotIn('Epub', m._fmt_record(rec, '1').splitlines()[0])
                self.assertNotIn('Epub', m._fmt_brief_record(rec, '1').splitlines()[0])
        self.assertIn('PMID 1｜?｜J', m._fmt_record(unknown, '1').splitlines()[0])

    def test_brief_json_adds_epubdate_only_for_known_cross_year(self):
        rec = {'title': 'T', 'pubdate': '2010 Mar', 'epubdate': '2009 Nov 3', 'source': 'J',
               'pubtype': ['Journal Article', 'Review']}
        same = {'title': 'T', 'pubdate': '2010 Mar', 'epubdate': '2010 Jan 3', 'source': 'J'}
        missing = {'title': 'T', 'pubdate': '2010 Mar', 'epubdate': '', 'source': 'J'}
        unknown = {'title': 'T', 'pubdate': '', 'epubdate': '2009 Nov 3', 'source': 'J'}
        brief = m._brief_record('1', rec)
        self.assertEqual(set(brief), {'pmid', 'pubdate', 'title', 'pubtype', 'epubdate'})
        self.assertEqual(brief['epubdate'], '2009 Nov 3')
        for other in (same, missing, unknown):
            with self.subTest(rec=other):
                self.assertEqual(set(m._brief_record('1', other)),
                                 {'pmid', 'pubdate', 'title', 'pubtype'})
        # 完整投影仍保留原有 epubdate 键，文本显示不改字段契约。
        self.assertEqual(m._record_parts('1', rec)['epubdate'], '2009 Nov 3')

    def test_diff_preview_json_and_text_show_cross_year_epub(self):
        rec = {'title': 'Fixture title', 'pubdate': '2010 Mar', 'epubdate': '2009 Nov 3',
               'source': 'Fixture Journal', 'pubtype': ['Journal Article']}

        async def ids(term, **kw):
            values = {'a': ['1'], 'b': ['1', '2']}
            count = len(values[term])
            qt = f'{term}[All Fields]'
            return (values[term], count,
                    {'querytranslation': qt, 'count': str(count),
                     '_retrieval_checks': [{'querytranslation': qt, 'query_needs_review': False,
                                            'stage': 'fixture'}]}, '')

        async def run():
            with patch.object(m, '_ids_in_window', ids), \
                    patch.object(m, '_esummary_dicts', AsyncMock(return_value=[('2', rec)])):
                data = json.loads(await m.diff('a', 'b', as_json=True, preview=True, max_show=1))
                text = await m.diff('a', 'b', preview=True, max_show=1)
            return data, text
        data, text = anyio.run(run)
        added = data['preview']['added']['records']
        self.assertEqual(added[0]['epubdate'], '2009 Nov 3')
        self.assertIn('2010 Mar｜Epub 2009 Nov 3', text)

    def test_ignored_added_field_downgrades_recall_reliability(self):
        term = 'PMC10461258[pmcid]'
        findings = m._syntax_findings(term)
        soft = [f for f in findings if f[0] == 'term_tag_ignored']
        self.assertTrue(soft)
        self.assertFalse(soft[0][1])  # lint 仍是软标记，不升级为 breaking
        d = {'_term': term, 'querytranslation': 'PMC10461258[All Fields]', 'count': '1', 'retstart': '0'}
        _, degraded = m._recall_warnings_and_degraded(term, [d])
        self.assertTrue(degraded)

    def test_check_recall_flags_ignored_added_field_as_unreliable(self):
        async def search(term, **kw):
            return {'_term': term, 'querytranslation': 'PMC10461258[All Fields]',
                    'count': '1', 'idlist': ['33782057'], 'retstart': '0'}
        async def run():
            with patch.object(m, '_esearch', search):
                return json.loads(await m.check_recall('PMC10461258[pmcid]', '33782057', as_json=True))
        result = anyio.run(run)
        self.assertTrue(result['term_degraded'])
        self.assertEqual(result['checks'][0]['status'], 'unreliable_query')


if __name__ == '__main__':
    unittest.main()

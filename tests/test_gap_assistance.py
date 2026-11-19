"""检索辅助回归；全部 PMID、计数、日期和预算替身均为本项目自订测试输入，不是真实文献证据。"""
import importlib.util
import json
import os
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import anyio

spec = importlib.util.spec_from_file_location('gap_subject', Path(__file__).resolve().parents[1] / 'pubmed_mcp.py')  # 本项目自订：测试源码位置
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


def response(term, count='0', ids=None):  # 本项目自订：默认零命中夹具
    return {'count': str(count), 'idlist': ids or [], 'querytranslation': term,
            '_term': term, 'retstart': '0', '_request_params': {'term': term}}  # 本项目自订：首页夹具


class Parser(unittest.TestCase):
    def test_nested_quotes_fields_and_not_preserved(self):
        query = '(("A AND B"[tiab] OR bone[MeSH Terms]) AND root[tiab] AND ai[tiab]) NOT review[pt]'
        plans, reason = m._query._relaxation_plan(query)
        self.assertFalse(reason)
        self.assertEqual(len(plans), 3)  # 本项目自订：可移除块数
        for row in plans:
            self.assertIn('NOT (review[pt])', row['term'])
        self.assertEqual(plans[-1]['removed_block'], 'ai[tiab]')
        self.assertTrue(plans[-1]['method_only'])

    def test_nested_not_block_cannot_be_dropped(self):
        plans, _ = m._query._relaxation_plan('(bone[tiab] NOT review[pt]) AND root[tiab]')
        self.assertEqual([p['removed_block'] for p in plans], ['root[tiab]'])
        self.assertIn('NOT (review[pt])', plans[0]['term'])  # 本项目自订：唯一派生查询

    def test_unsafe_or_malformed_is_not_guessed(self):
        for query in ('A OR B AND C', 'A AND B NOT C AND D', 'A AND', '(A AND B',
                      'A AND "B', 'A AND B[tiab', '(A OR B)[tiab] AND C',
                      'A and B', 'A AND (B OR)', 'A AND (B OR C AND D)'):
            with self.subTest(query=query):
                plans, reason = m._query._relaxation_plan(query)
                self.assertEqual(plans, [])
                self.assertIn('无法可靠拆分', reason)

    def test_method_hint_is_narrow_and_conditional(self):
        for block, expected in (('("machine learning"[tiab] OR AI[tiab])', True),
                                ('("convolutional neural network"[tiab] OR U-Net[tiab])', False),
                                ('("machine learning"[tiab] OR LASSO[tiab])', False)):
            plans, _ = m._query._relaxation_plan('tooth[tiab] AND ' + block)
            self.assertEqual(plans[-1]['method_only'], expected)


class LowHit(unittest.TestCase):
    def setUp(self):
        self.query = 'bone[tiab] AND root[tiab] AND "machine learning"[tiab]'
        self.calls = []
        async def fake(term, *args, **kwargs):
            self.calls.append((term, kwargs))
            return response(term, '0' if term == self.query else '12')  # 本项目自订：原式/放宽计数
        self.fake = fake

    def test_auto_preserves_scope_order_and_original_result(self):
        scope = dict(field='Title', mindate='2020', maxdate='2024', reldate=30, datetype='edat')  # 本项目自订：检索限制夹具
        relative = '2026/10/21:2026/11/19[Date - Entry]'  # 本项目自订：服务器相对窗口夹具
        absolute = '2020/01/01:2024/12/31[Date - Entry]'  # 本项目自订：mindate/maxdate 展开夹具
        async def scoped(term, *args, **kwargs):
            self.calls.append((term, kwargs))
            qt = term.replace('[tiab]', '[Title]') + f' AND {absolute} AND {relative}'  # 本项目自订：实际执行式带回字段与日期
            return response(qt, '0' if term == self.query else '12')  # 本项目自订：原式/放宽计数
        with patch.object(m, '_esearch', scoped), patch.dict(os.environ, {'PUBMED_AUDIT_DIR': ''}):
            data = json.loads(run(m.search, self.query, as_json=True, **scope))
        self.assertEqual(data['count'], 0)  # 本项目自订：原计数不被诊断覆盖
        self.assertEqual(len(self.calls), 4)  # 本项目自订：原式加逐块计数，不递归
        self.assertEqual(data['diagnostics']['status'], 'complete')
        rows = data['diagnostics']['rows']
        self.assertEqual([r['removed_block'] for r in rows], ['bone[tiab]', 'root[tiab]', '"machine learning"[tiab]'])
        for _, kwargs in self.calls[1:]:  # 本项目自订：子查询部分
            self.assertEqual({k: kwargs[k] for k in scope}, scope)
            self.assertEqual(kwargs['retmax'], 0)  # 本项目自订：诊断只计数
        text = '\n'.join(data['next_steps'])
        self.assertIn('方法不是研究问题本身', text)
        self.assertIn('PUBMED_AUDIT_DIR', text)
        self.assertNotIn('增幅最大', text)
        self.assertIn('check_recall', text)

    def test_count_default_stays_single_request(self):
        with patch.object(m, '_esearch', self.fake):
            text = run(m.count, self.query)
        self.assertEqual(len(self.calls), 1)  # 本项目自订：默认轻量调用
        self.assertIn('diagnose="auto"', text)

    def test_count_explicit_diagnosis_and_off(self):
        for mode, expected in [('off', 1), ('auto', 4), ('always', 4)]:  # 本项目自订：请求预算断言
            self.calls.clear()
            with patch.object(m, '_esearch', self.fake):
                run(m.count, self.query, diagnose=mode)
            self.assertEqual(len(self.calls), expected)

    def test_large_count_later_page_and_disabled_skip_extra_requests(self):
        for kwargs in ({'diagnose': 'off'}, {'retstart': 4}, {'low_hit_threshold': 0}):  # 本项目自订：跳过诊断场景
            mock = AsyncMock(return_value=response(self.query, '5'))  # 本项目自订：非零低命中
            with patch.object(m, '_esearch', mock):
                run(m.search, self.query, detail='ids', **kwargs)
            self.assertEqual(mock.await_count, 1)  # 本项目自订：不产生子请求
        mock = AsyncMock(return_value=response(self.query, '100'))  # 本项目自订：超过默认阈值
        with patch.object(m, '_esearch', mock):
            run(m.search, self.query, detail='ids')
        self.assertEqual(mock.await_count, 1)  # 本项目自订：不产生子请求

    def test_query_warnings_pause_diagnosis(self):
        d = response(self.query)
        d['errorlist'] = {'phrasesnotfound': ['bone']}
        mock = AsyncMock(return_value=d)
        with patch.object(m, '_esearch', mock):
            data = json.loads(run(m.search, self.query, as_json=True))
        self.assertEqual(mock.await_count, 1)  # 本项目自订：失真原式不继续诊断
        self.assertEqual(data['diagnostics']['status'], 'unreliable_query')

    def test_stopword_warning_does_not_disable_diagnosis(self):
        async def fake(term, *args, **kwargs):
            return {**response(term), 'warninglist': {'phrasesignored': ['of']}}
        with patch.object(m, '_esearch', fake):
            data = json.loads(run(m.search, 'effect of tooth[tiab] AND aspirin[tiab]', as_json=True))
        self.assertEqual(data['diagnostics']['status'], 'complete')
        self.assertTrue(all(row['status'] == 'ok' for row in data['diagnostics']['rows']))
        self.assertIn('停用词', '\n'.join(data['warnings']))

    def test_budget_and_partial_failure_are_explicit(self):
        query = ' AND '.join(f'concept{n}[tiab]' for n in range(8))  # 本项目自订：超过诊断预算
        calls = []
        async def fake(term, *args, **kwargs):
            calls.append(term)
            if len(calls) == 2:  # 本项目自订：首个子请求失败
                raise m.NcbiError('simulated outage')
            return response(term)
        with patch.object(m, '_esearch', fake):
            data = json.loads(run(m.search, query, as_json=True))
        self.assertEqual(len(calls), 7)  # 本项目自订：主请求加预算内子请求
        self.assertEqual(data['diagnostics']['status'], 'partial')
        self.assertIsNone(data['diagnostics']['rows'][0]['count'])  # 本项目自订：失败不写零
        self.assertIn('simulated outage', '\n'.join(data['next_steps']))
        self.assertTrue(data['diagnostics']['warnings'])

    def test_long_query_echo_once_without_truncation(self):
        executed = ' OR '.join(f'word{n}[Title]' for n in range(90))  # 本项目自订：超过旧截断长度的查询夹具
        for tool in (m.count, m.search):
            with patch.object(m, '_esearch', AsyncMock(return_value=response(executed, '90'))):  # 本项目自订：不触发低命中诊断
                text = run(tool, 'original-query', **({'detail': 'ids'} if tool is m.search else {}))
            self.assertEqual(text.count(executed), 1)  # 本项目自订：完整且仅一次
            self.assertNotIn('查询：original-query', text)
            self.assertNotIn('截断', text)

    def test_query_echo_does_not_hide_upstream_warnings(self):
        d = {**response('tooth[Title]'), 'warninglist': {'quotedphrasesnotfound': ['"tooth phrase"']}}
        with patch.object(m, '_esearch', AsyncMock(return_value=d)):
            text = run(m.count, '"tooth phrase"[ti]')
        self.assertIn('quotedphrasesnotfound', text)
        self.assertIn('NCBI 实际执行：tooth[Title]', text)
        self.assertIn('去掉引号', text)

    def test_invalid_options_fail_before_network(self):
        for kwargs in ({'diagnose': 'maybe'}, {'low_hit_threshold': -1}, {'low_hit_threshold': True}):  # 本项目自订：非法阈值
            mock = AsyncMock(side_effect=AssertionError('must not call'))
            with patch.object(m, '_esearch', mock), self.assertRaises(m.NcbiError):
                run(m.search, self.query, **kwargs)
            mock.assert_not_awaited()

    def test_audit_keeps_child_queries_and_snapshot_keeps_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / 'page.json')
            audit = Path(tmp) / 'audit'
            with patch.object(m, '_esearch', self.fake), patch.dict(os.environ, {'PUBMED_AUDIT_DIR': str(audit)}):
                run(m.search, self.query, as_json=True, out_path=target)
            saved = json.loads(Path(target).read_text(encoding='utf-8'))
            self.assertEqual(saved['result']['diagnostics']['status'], 'complete')
            logs = list(audit.glob('*.jsonl'))
            self.assertTrue(logs)
            record = json.loads(logs[0].read_text(encoding='utf-8').splitlines()[-1])  # 本项目自订：本次审计行
            self.assertIn('diagnostics', json.dumps(record))


class Neighbors(unittest.TestCase):
    def setUp(self):
        self.targets = ['1', '2', '3']  # 本项目自订：种子与候选 PMID
        self.search_calls = []
        async def elink(path, params, timeout):
            self.assertEqual(path, 'elink.fcgi')
            return json.dumps({'linksets': [{'ids': [params['id']], 'linksetdbs': [
                {'linkname': 'pubmed_pubmed', 'links': self.targets}]}]})
        async def search(term, **kwargs):
            self.search_calls.append((term, kwargs))
            return response(term, '1', ['2'])  # 本项目自订：命中候选
        self.search = search
        self.summary = AsyncMock(side_effect=lambda ids, **kw: [(p, {'title': 'Title ' + p, 'pubdate': 'fixture-date', 'lang': ['eng']}) for p in ids])
        for name, value in (('_get_async', elink), ('_pmids_exist', AsyncMock(return_value={'1': 'present'})),  # 本项目自订：种子状态
                            ('_esummary_dicts', self.summary)):
            patcher = patch.object(m, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, **kwargs):
        return json.loads(run(m.links, '1', to='pubmed', as_json=True, **kwargs))  # 本项目自订：源 PMID

    def test_titles_membership_and_filter_forwarding(self):
        scope = dict(field='Title', mindate='2020', maxdate='2024', reldate=30, datetype='edat')  # 本项目自订：范围夹具
        async def scoped(term, **kwargs):
            self.search_calls.append((term, kwargs))
            qt = term.replace('tooth', 'tooth[Title]') + ' AND 2020/01/01:2024/12/31[Date - Entry] AND 2024/11/01:2024/12/01[Date - Entry]'  # 本项目自订：两段服务器日期回显
            return response(qt, '1', ['2'])  # 本项目自订：仅一个候选符合全部限制
        with patch.object(m, '_esearch', scoped):
            data = self.call(term='tooth', with_titles=True, **scope)
        rows = {r['pmid']: r for r in data['candidate_details']}
        self.assertTrue(rows['2']['in_term'])  # 本项目自订：夹具命中
        self.assertFalse(rows['3']['in_term'])  # 本项目自订：夹具不命中
        self.assertEqual(rows['3']['title'], 'Title 3')  # 本项目自订：标题保留
        self.assertTrue(rows['1']['is_seed'])  # 本项目自订：种子明确标记
        self.assertEqual(data['candidate_pmids'], self.targets)
        self.assertEqual(data['outside_term_pmids'], ['1', '3'])  # 本项目自订：未命中按原名次
        self.assertEqual({k: self.search_calls[0][1][k] for k in scope}, scope)  # 本项目自订：首批参数
        self.assertEqual(data['match_scope']['datetype'], 'edat')

    def test_links_origin_survives_projection_without_claiming_query_additions(self):
        with patch.object(m, '_esearch', self.search), tempfile.TemporaryDirectory() as tmp:
            for compact in (False, True):
                target = Path(tmp) / (str(compact) + '.json')
                reply = self.call(term='tooth[tiab]', compact=compact, out_path=str(target))
                full = json.loads(target.read_text(encoding='utf-8'))
                self.assertTrue(full['outside_term_pmids'])
                self.assertEqual(reply['provenance'], full['provenance'])
                self.assertEqual(reply['provenance']['comparison'], 'not_compared')
            text = run(m.links, '1', to='pubmed')  # 本项目自订：测试种子
            self.assertIn('近邻候选', text)
            self.assertNotIn('未比对改式前后', text)
            self.assertNotIn('provenance', json.loads(run(m.links, '1', to='pmc', compact=True)))  # 本项目自订：非文献目标

    def test_scope_missing_from_intersection_is_unknown(self):
        for scope in ({'field':'Title'}, {'mindate':'2024', 'maxdate':'2024'}):  # 本项目自订：分别丢失字段或日期限制
            qt = 'tooth[All Fields] AND (1[UID] OR 2[UID] OR 3[UID])'  # 本项目自订：仅回显未限定交集
            with self.subTest(scope=scope), patch.object(m, '_esearch', AsyncMock(return_value=response(qt, '1', ['2']))):  # 本项目自订：实取不能证明受限命中
                page = self.call(term='tooth', compact=True, **scope)
                self.assertEqual(page['match_summary']['unknown'], len(self.targets))
                self.assertTrue(all(r['in_term'] is None for r in page['targets']))
                self.assertEqual(page['executions'], [{'status':'unreliable_query', 'querytranslation':qt}])
                self.assertIn(qt, run(m.links, '1', to='pubmed', term='tooth', **scope))  # 本项目自订：文本也保留执行式

    def test_partial_batch_echo_is_unknown_for_all_neighbors(self):
        qt = 'tooth[Title] AND 2[UID]'  # 本项目自订：回显仅检查实取号，非整批
        with patch.object(m, '_esearch', AsyncMock(return_value=response(qt, '1', ['2']))):  # 本项目自订：实取一个编号
            data = self.call(term='tooth', compact=True)
        self.assertTrue(all(row['in_term'] is None for row in data['targets']))
        self.assertEqual(data['match_summary']['unknown'], len(self.targets))

    def test_single_source_enriched_compact_recovers_link_order_after_paging(self):
        self.targets = ['90', '10', '2', '12']  # 本项目自订：关联结果顺序，检索只命中中间项
        with patch.object(m, '_esearch', self.search):
            page = self.call(term='tooth[tiab]', with_titles=True, compact=True, offset=1, limit=3)  # 本项目自订：跳过开头的候选
        rows = page['targets']
        self.assertEqual({r['pmid']: r['in_term'] for r in rows}, {'10': False, '2': True, '12': False})  # 本项目自订：命中分类保持
        self.assertEqual({r['pmid']: r['source_positions']['1'] for r in rows}, {'10': 2, '2': 3, '12': 4})  # 本项目自订：源列表的绝对位置

    def test_compact_without_enrichment_marks_seed(self):
        rows = self.call(compact=True)['targets']
        self.assertEqual([r['pmid'] for r in rows if r.get('is_seed')], ['1'])  # 本项目自订：唯一种子

    def test_failures_and_degraded_query_are_unknown_not_false(self):
        degraded = response('1[UID] OR 2[UID] OR 3[UID]', '3', self.targets)  # 本项目自订：原词被吞的响应
        cases = [m.NcbiError('outage'), degraded, response(''),
                 {**response('tooth[tiab]', '1', ['2']), 'errorlist': {'phrasesnotfound': ['tooth']}}]  # 本项目自订：失真响应
        for value in cases:
            mock = AsyncMock(side_effect=value) if isinstance(value, Exception) else AsyncMock(return_value=value)
            with self.subTest(value=value), patch.object(m, '_esearch', mock):
                data = self.call(term='tooth[tiab]')
            self.assertTrue(all(r['in_term'] is None for r in data['candidate_details']))
            self.assertFalse(data['outside_term_pmids'])
            self.assertTrue(data['warnings'])

    def test_missing_metadata_does_not_change_membership(self):
        self.summary.return_value = []
        self.summary.side_effect = None
        with patch.object(m, '_esearch', self.search):
            data = self.call(term='tooth[tiab]', with_titles=True)
        self.assertTrue(all(r['metadata_status'] == 'missing' for r in data['candidate_details']))
        self.assertTrue(any(r['in_term'] is False for r in data['candidate_details']))

    def test_budget_and_pagination_leave_remainder_unchecked(self):
        with patch.object(m._tools_module, '_NEIGHBOR_ENRICH_CAP', 2), patch.object(m, '_esearch', self.search):  # 本项目自订：缩小预算
            data = self.call(term='tooth[tiab]', with_titles=True)
        rows = {r['pmid']: r for r in data['candidate_details']}
        self.assertIsNone(rows['3']['in_term'])  # 本项目自订：预算外候选
        self.assertEqual(rows['3']['match_status'], 'not_checked')  # 本项目自订：未检查状态
        self.summary.assert_awaited_once_with(['1', '2'], include_unavailable=True)  # 本项目自订：预算内请求
        with patch.object(m, '_esearch', AsyncMock(return_value=response('tooth[tiab] AND 3[UID]', '0'))):  # 本项目自订：完整回显、分页候选未命中
            page = self.call(term='tooth[tiab]', offset=2, limit=1)  # 本项目自订：检查预算外页
        self.assertEqual(page['candidate_pmids'], ['3'])  # 本项目自订：分页目标
        self.assertFalse(page['candidate_details'][0]['in_term'])  # 本项目自订：已检查后才写 false

    def test_out_path_keeps_full_list_but_advances_check_window(self):
        search = AsyncMock(return_value=response('tooth[tiab] AND 3[UID]', '1', ['3']))  # 本项目自订：后页命中
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, '_esearch', search), \
                patch.object(m._tools_module, '_NEIGHBOR_ENRICH_CAP', 2):  # 本项目自订：让后页位于默认预算外
            target = str(Path(tmp) / 'links.json')
            self.call(term='tooth[tiab]', with_titles=True, offset=2, limit=1, out_path=target)  # 本项目自订：文件不裁名单，但检查后页
            saved = json.loads(Path(target).read_text(encoding='utf-8'))
        self.assertEqual(len(saved['candidate_details']), len(self.targets))
        self.assertEqual(saved['candidate_pmids'], self.targets)
        rows = {r['pmid']: r for r in saved['candidate_details']}
        self.assertTrue(rows['3']['in_term'])  # 本项目自订：预算外候选经翻页已检查
        self.assertEqual(rows['1']['match_status'], 'not_checked')  # 本项目自订：前页未检查
        self.summary.assert_awaited_once_with(['3'], include_unavailable=True)  # 本项目自订：仅获取后页元数据
        self.assertNotIn('1[uid]', search.call_args.args[0])  # 本项目自订：不重复检查前页

    def test_unrequested_titles_are_not_reported_as_failure(self):
        with patch.object(m, '_esearch', self.search):
            text = run(m.links, '1', to='pubmed', term='tooth[tiab]')  # 本项目自订：未请求标题
        self.assertIn('未请求标题', text)
        self.assertNotIn('⚠️ 标题', text)
        self.assertNotIn('日期未知', text)

    def test_stopword_warning_keeps_valid_membership(self):
        d = {**response('tooth[tiab] AND (1[UID] OR 2[UID] OR 3[UID])', '1', ['2']),  # 本项目自订：确定命中的交集
             'warninglist': {'phrasesignored': ['of']}}
        with patch.object(m, '_esearch', AsyncMock(return_value=d)):
            data = self.call(term='of tooth[tiab]')  # 只丢回执明确提示的 of，不把额外缺失的 effect 当停用词。
        rows = {r['pmid']: r for r in data['candidate_details']}
        self.assertTrue(rows['2']['in_term'])  # 本项目自订：停用词提示不抹掉有效匹配
        self.assertFalse(rows['3']['in_term'])  # 本项目自订：确定未匹配
        self.assertIn('停用词', '\n'.join(data['warnings']))

    def test_compact_once_per_candidate_and_query_across_sources(self):
        term = 'tooth[tiab]'
        with patch.object(m, '_esearch', self.search):
            raw = run(m.links, '1 4', to='pubmed', term=term, with_titles=True, compact=True)  # 本项目自订：共享候选的两个种子
        page = json.loads(raw)
        self.assertEqual(page['match_scope']['term'], term)
        self.assertEqual([e['querytranslation'] for e in page['executions']], [self.search_calls[0][0]])  # 本项目自订：实际交集只列一次，不按候选重复
        self.assertEqual(page['schema'], 'pubmed.links.compact.v1')  # 本项目自订：独立格式标识
        self.assertTrue({'edges', 'candidate_pmids', 'match_checks'}.isdisjoint(page))
        self.assertTrue(all('target_ids' not in s for s in page['sources']))
        self.assertCountEqual([r['pmid'] for r in page['targets']], self.targets)
        for row in page['targets']:
            self.assertEqual(row['sources'], ['1', '4'])  # 本项目自订：实际显示的两个来源
            self.assertEqual(row['in_term'], row['pmid'] == '2')  # 本项目自订：唯一命中
            self.assertEqual(row['title'], 'Title ' + row['pmid'])
            self.assertEqual(row['languages'], ['eng'])
        self.assertEqual(len(self.search_calls), 1)  # 本项目自订：共享候选只交集检查一批
        self.summary.assert_awaited_once()

    def test_compact_page_and_full_file_use_same_checked_window(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, '_esearch', AsyncMock(side_effect=m.NcbiError('fixture outage'))):
            target = Path(tmp) / 'links.json'
            page = self.call(compact=True, term='tooth', out_path=str(target), offset=1, limit=1)  # 本项目自订：只检查中间项
            full = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(page['content_schema'], full['schema'])
        self.assertEqual(full['candidate_pmids'], self.targets)
        self.assertEqual([r['pmid'] for r in page['targets']], self.targets[1:2])  # 本项目自订：当前页
        row, = page['targets']
        self.assertIsNone(row['in_term'])
        self.assertEqual(row['match_status'], 'error')
        self.assertIn('fixture outage', ' '.join(page['warnings']))
        self.assertTrue(full['match_checks'])
        self.assertEqual(page['sources'][0]['total'], len(self.targets))  # 本项目自订：分页前计数

    def test_match_summary_precedes_truncated_titles_and_deduplicates(self):
        with patch.object(m, '_esearch', self.search), patch.object(m._tools_module, '_NEIGHBOR_TEXT_CAP', 1):  # 本项目自订：仅展示一个详情
            text = run(m.links, '1 4', to='pubmed', term='tooth')  # 本项目自订：两个种子共享同批候选
            data = self.call(term='tooth', compact=True)
        self.assertIn('本页去重候选 3 篇：命中 1，未命中 2', text)  # 本项目自订：汇总覆盖完整窗口
        self.assertLess(text.index('本页去重候选'), text.index('｜未命中当前式'))
        self.assertIn('本页共有 3 个去重候选，文本仅展示前 1 个候选详情', text)  # 本项目自订：仅文本预览截断
        self.assertIn('with_titles=True, compact=True', text)
        self.assertEqual(data['match_summary'], dict(window_total=3, inside=1, outside=2, unknown=0, unchecked=0, outside_window=0))  # 本项目自订：去重后的夹具统计

    def test_failed_unchecked_and_off_page_counts_survive_file_and_preview(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, '_esearch', AsyncMock(side_effect=m.NcbiError('fixture'))), \
                patch.object(m._tools_module, '_NEIGHBOR_ENRICH_CAP', 1):  # 本项目自订：首项检查失败，次项超预算
            path = Path(tmp) / 'all.json'
            preview = self.call(term='tooth', compact=True, offset=1, limit=2, out_path=str(path))  # 本项目自订：第一页候选不在本次窗口内
            stored = json.loads(path.read_text(encoding='utf-8'))
            empty = self.call(term='tooth', limit=0)  # 本项目自订：空窗口仍有其他候选
        expected = dict(window_total=2, inside=0, outside=0, unknown=1, unchecked=1, outside_window=1)  # 本项目自订：故障/未查/窗外独立计数
        self.assertEqual(preview['match_summary'], expected)
        self.assertEqual(stored['match_summary'], expected)
        self.assertEqual(empty['match_summary']['outside_window'], len(self.targets))
        self.assertEqual(empty['match_summary']['window_total'], 0)  # 本项目自订：空页
        self.assertNotIn('match_summary', self.call(with_titles=True))

    def test_long_translation_stays_in_structured_fields(self):
        executed = 'tooth[tiab] AND (' + ' OR '.join(f'{i}[UID]' for i in range(100)) + ')'  # 本项目自订：执行式超过旧截断长度
        result = {**response(executed, '1', ['2']), 'warninglist': {'phrasesignored': ['of']}}  # 本项目自订：保留停用词警告
        with patch.object(m, '_esearch', AsyncMock(return_value=result)):
            check, = self.call(term='tooth')['match_checks']
            recall = json.loads(run(m.check_recall, 'tooth', '2', as_json=True))  # 本项目自订：已知集夹具
            diff = json.loads(run(m.diff, 'a', 'b', as_json=True))
        for notes in (check['warnings'], recall['warnings'], diff['warnings_a'], diff['warnings_b']):
            warnings = '\n'.join(notes)
            self.assertNotIn('完整解析用 as_json', warnings)
            self.assertIn('停用词', warnings)
        self.assertEqual(check['querytranslation'], executed)
        self.assertEqual(recall['querytranslations'], [executed])
        self.assertEqual((diff['querytranslation_a'], diff['querytranslation_b']), (executed, executed))

    def test_compact_non_publication_and_empty_page(self):
        payload = {'linksets': [{'ids': ['1'], 'linksetdbs': [{'linkname': 'pubmed_pmc', 'links': ['42']}]}]}  # 本项目自订：PMC 目标
        with patch.object(m, '_get_async', AsyncMock(return_value=json.dumps(payload))):
            page = json.loads(run(m.links, '1', compact=True))  # 本项目自订：默认 PMC
            empty = json.loads(run(m.links, '1', compact=True, limit=0))  # 本项目自订：空窗口
        self.assertEqual(page['targets'], [{'id': '42', 'sources': ['1']}])  # 本项目自订：其他库的号不标成 PMID
        self.assertFalse(empty['targets'])
        self.assertEqual(empty['sources'][0]['total'], 1)  # 本项目自订：空窗口保留源计数
        self.summary.assert_not_awaited()

    def test_old_call_adds_no_enrichment_request(self):
        mock = AsyncMock(side_effect=AssertionError('must not call'))
        with patch.object(m, '_esearch', mock):
            data = self.call()
        self.assertNotIn('candidate_details', data)
        self.summary.assert_not_awaited()
        mock.assert_not_awaited()

    def test_invalid_combination_fails_before_network(self):
        for kwargs in ({'to': 'pmc', 'term': 'tooth'}, {'to': 'gene', 'with_titles': True},
                       {'to': 'pubmed', 'mindate': '2020'},  # 本项目自订：无 term 却带日期
                       {'to': 'pubmed', 'term': 'tooth', 'mindate': '2024', 'maxdate': '2020'}):  # 本项目自订：倒挂日期
            mock = AsyncMock(side_effect=AssertionError('must not call'))
            with patch.object(m, '_pmids_exist', mock), self.assertRaises(m.NcbiError):
                run(m.links, '1', **kwargs)  # 本项目自订：源 PMID
            mock.assert_not_awaited()

    def test_sdk_exposes_options_and_text_is_readable(self):
        schemas = {t.name: getattr(t, 'inputSchema', None) or getattr(t, 'input_schema') for t in run(m.mcp.list_tools)}
        self.assertEqual(schemas['search']['properties']['diagnose']['default'], 'auto')
        self.assertEqual(schemas['count']['properties']['diagnose']['default'], 'off')
        self.assertIn('term', schemas['links']['properties'])
        compact = schemas['links']['properties']['compact']
        self.assertIs(compact['default'], False)
        self.assertIn('精简 JSON', compact['description'])
        with patch.object(m, '_esearch', self.search):
            text = run(m.links, '1', to='pubmed', term='tooth[tiab]', with_titles=True)  # 本项目自订：源 PMID
        self.assertIn('未命中当前式', text)
        self.assertIn('Title 3', text)  # 本项目自订：候选标题
        self.assertIn('种子自身', text)
        self.assertNotIn('不能自动判定研究空白', text)
        self.assertIn('追引未比对改式新增', next(t.description for t in run(m.mcp.list_tools) if t.name == 'links'))


class LowHitScope(unittest.TestCase):
    def test_parent_scope_loss_marks_unreliable_and_skips_children(self):
        query = 'bone[tiab] AND root[tiab]'
        mock = AsyncMock(return_value=response('bone[tiab] AND root[tiab]', '0'))  # 本项目自订：执行式丢了字段/日期
        with patch.object(m, '_esearch', mock):
            data = json.loads(run(m.search, query, as_json=True, field='Title', mindate='2020', maxdate='2024'))
        self.assertEqual(mock.await_count, 1)  # 本项目自订：范围无法核实就不产生子请求
        self.assertEqual(data['diagnostics']['status'], 'unreliable_query')
        joined = '\n'.join(data['diagnostics']['warnings'] + data['next_steps'])
        self.assertIn('不能核实所有主题词均按 Title 执行', joined)
        self.assertIn('外加绝对日期不是执行式各分支的必需条件', joined)
        self.assertEqual(data['diagnostics']['rows'], [])  # 本项目自订：不把无法核实的低命中继续放宽

    def test_child_scope_loss_has_no_reliable_delta(self):
        query = 'bone[tiab] AND root[tiab]'
        parent_qt = ('bone[Title] AND root[Title] '
                     'AND 2020/01/01:2024/12/31[Date - Publication]')  # 本项目自订：父查询带回字段和日期
        child_qt = 'root[Title] OR bone[Title]'  # 本项目自订：子查询丢了日期限制

        async def fake(term, *args, **kwargs):
            if term == query:
                return response(parent_qt, '0')
            return response(child_qt, '5')

        with patch.object(m, '_esearch', fake):
            data = json.loads(run(m.search, query, as_json=True, field='Title',
                                  mindate='2020', maxdate='2024'))
        self.assertEqual(data['diagnostics']['status'], 'partial')
        self.assertEqual(len(data['diagnostics']['rows']), 2)
        for row in data['diagnostics']['rows']:
            self.assertEqual(row['status'], 'unreliable_query')
            self.assertIsNone(row['delta'])  # 本项目自订：范围丢失时不给可比较的差值
            self.assertEqual(row['count'], 5)  # 本项目自订：计数保留，但只作为观测
            self.assertTrue(any('日期' in note for note in row['warnings']))
        joined = '\n'.join(data['next_steps'])
        self.assertIn('不能按原式比较', joined)


class ProtectedBlocks(unittest.TestCase):
    def test_term_internal_date_and_type_are_not_offered(self):
        query = 'bone[tiab] AND root[tiab] AND 2020:2024[dp] AND review[pt]'
        calls = []

        async def fake(term, *args, **kwargs):
            calls.append(term)
            return response(term, '0' if term == query else '9')

        with patch.object(m, '_esearch', fake):
            data = json.loads(run(m.search, query, as_json=True))
        self.assertEqual(data['diagnostics']['status'], 'complete')
        removed = [row['removed_block'] for row in data['diagnostics']['rows']]
        self.assertEqual(removed, ['bone[tiab]', 'root[tiab]'])  # 本项目自订：只放宽概念块
        self.assertEqual(data['diagnostics']['protected_blocks'], ['2020:2024[dp]', 'review[pt]'])
        joined = '\n'.join(data['next_steps'])
        self.assertIn('受保护块（含日期/文献类型限制', joined)
        self.assertIn('其余字段、日期和排除条件保持不变', joined)
        self.assertNotIn('去掉 2020:2024[dp]', joined)
        self.assertNotIn('去掉 review[pt]', joined)

    def test_all_blocks_protected_is_unsupported_without_child_requests(self):
        query = '2020:2024[dp] AND review[pt]'
        mock = AsyncMock(return_value=response(query, '0'))
        with patch.object(m, '_esearch', mock):
            data = json.loads(run(m.search, query, as_json=True))
        self.assertEqual(mock.await_count, 1)
        self.assertEqual(data['diagnostics']['status'], 'unsupported')
        self.assertEqual(data['diagnostics']['rows'], [])
        joined = '\n'.join(data['next_steps'])
        self.assertIn('可拆分的块都含日期或文献类型限制', joined)


class SearchEndVsWindow(unittest.TestCase):
    def test_end_page_text_and_json_flags(self):
        d = {'count': '5', 'idlist': ['4', '5'], 'querytranslation': 'x[All Fields]',
             '_term': 'x', 'retstart': '3', '_request_params': {}}
        with patch.object(m, '_esearch', AsyncMock(return_value=d)):
            text = run(m.search, 'x', detail='ids', retstart=3, retmax=2, diagnose='off')
            data = json.loads(run(m.search, 'x', detail='ids', retstart=3, retmax=2, diagnose='off', as_json=True))
        self.assertIn('已到末页', text)
        self.assertNotIn('不是末页', text)
        self.assertTrue(data['end_page'])
        self.assertFalse(data['window_exceeded'])
        self.assertIsNone(data['next_retstart'])

    def test_window_exceeded_is_not_reported_as_end_page(self):
        ids = [str(i) for i in range(9991, 10000)]  # 本项目自订：窗口末端 9 条
        d = {'count': '12000', 'idlist': ids, 'querytranslation': 'x[All Fields]',
             '_term': 'x', 'retstart': '9990', '_request_params': {}}
        with patch.object(m, '_esearch', AsyncMock(return_value=d)):
            text = run(m.search, 'x', detail='ids', retstart=9990, retmax=10, diagnose='off')
            data = json.loads(run(m.search, 'x', detail='ids', retstart=9990, retmax=10,
                                  diagnose='off', as_json=True))
        self.assertNotIn('已到末页', text)
        self.assertIn('第 9999 条以后的【永远翻不到】', text)
        self.assertTrue(data['window_exceeded'])
        self.assertFalse(data['end_page'])
        self.assertIsNone(data['next_retstart'])


class MeshExactOtherType(unittest.TestCase):
    @staticmethod
    def fake(records, count):
        async def get(path, params, timeout):
            if path == 'esearch.fcgi':
                ids = list(records)[:params['retmax']]
                return json.dumps({'esearchresult': {'count': str(count), 'idlist': ids,
                                                     'querytranslation': 'q[All Fields]'}})
            ids = params['id'].split(',')
            return json.dumps({'result': {'uids': ids, **{
                i: records.get(i, {'uid': i, 'ds_meshterms': ['Related ' + i]}) for i in ids}}})
        return get

    QUAL = {'uid': '1', 'ds_meshui': 'Q1', 'ds_meshterms': ['Target'],
            'ds_recordtype': 'qualifier'}  # 本项目自订：精确匹配但非描述符
    DESC = {'uid': '2', 'ds_meshui': 'D1', 'ds_meshterms': ['Other'],
            'ds_recordtype': 'descriptor'}

    def test_exact_other_type_with_remaining_suggests_retmax(self):
        with patch.object(m, '_get_async', self.fake({'1': self.QUAL, '2': self.DESC}, 3)):
            text = run(m.mesh, 'Target', retmax=5)
        self.assertIn('字面精确匹配，但类型是「副主题词 [sh]」而非描述符', text)
        self.assertIn('可增大 retmax（上限 20）', text)
        self.assertNotIn('中无字面精确匹配', text)

    def test_exact_other_type_at_cap_does_not_suggest_retmax(self):
        records = {'1': self.QUAL}
        for i in range(10, 30):  # 本项目自订：填充到超过上限
            records[str(i)] = {'uid': str(i), 'ds_meshterms': ['Other ' + str(i)],
                               'ds_recordtype': 'descriptor'}
        with patch.object(m, '_get_async', self.fake(records, len(records))):
            text = run(m.mesh, 'Target', retmax=50)
        self.assertIn('字面精确匹配，但类型是「副主题词 [sh]」而非描述符', text)
        self.assertIn('已到上限 20', text)
        self.assertNotIn('可增大 retmax', text)

    def test_exact_other_type_without_remaining_has_no_hint(self):
        with patch.object(m, '_get_async', self.fake({'1': self.QUAL, '2': self.DESC}, 2)):
            text = run(m.mesh, 'Target', retmax=5)
        self.assertNotIn('字面精确匹配，但类型是', text)


class NeighborPagination(unittest.TestCase):
    def setUp(self):
        self.targets = ['1', '2', '3']  # 本项目自订：两个来源共享的候选夹具

        async def elink(path, params, timeout):
            return json.dumps({'linksets': [{'ids': [params['id']], 'linksetdbs': [
                {'linkname': 'pubmed_pubmed', 'links': self.targets}]}]})

        async def uid_echo(term, **kwargs):
            batch = re.findall(r'(\d+)\[UID\]', term, re.I)
            return response(term, str(len(batch)), batch)

        async def summary(ids, **kw):
            return [(p, {'title': 'Title ' + p, 'pubdate': 'fixture-date', 'lang': ['eng']}) for p in ids]

        for name, value in (('_get_async', elink), ('_pmids_exist', AsyncMock(return_value={'1': 'present', '4': 'present'})),
                            ('_esummary_dicts', summary), ('_esearch', uid_echo)):
            patcher = patch.object(m, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_multi_source_window_over_cap_replays_at_same_offset(self):
        with patch.object(m._tools_module, '_NEIGHBOR_ENRICH_CAP', 2):
            page = json.loads(run(m.links, '1 4', to='pubmed', term='tooth[tiab]', as_json=True))
            compact = json.loads(run(m.links, '1 4', to='pubmed', term='tooth[tiab]', compact=True))
        pagination = page['pagination']
        self.assertEqual(pagination['enrichment_cap'], 2)
        self.assertEqual(pagination['window_total'], 3)
        self.assertEqual(pagination['window_checked'], 2)
        self.assertTrue(pagination['exceeded'])
        self.assertIsNone(pagination['next_offset'])  # 本项目自订：不推进，先重放本页
        self.assertEqual(pagination['safe_per_source_limit'], 1)  # 本项目自订：2 // 2 个来源
        joined = '\n'.join(page['warnings'])
        self.assertIn('安全重放：offset=0, limit=1', joined)
        self.assertIn('不要直接用 offset+limit 前进', joined)
        self.assertIn('该页检查完后再用 offset=1, limit=1', joined)
        self.assertEqual(compact['safe_per_source_limit'], 1)
        self.assertIsNone(compact['next_offset'])
        self.assertEqual(compact['window_total'], 3)

    def test_bounded_replay_provides_next_offset(self):
        with patch.object(m._tools_module, '_NEIGHBOR_ENRICH_CAP', 2):
            page = json.loads(run(m.links, '1 4', to='pubmed', term='tooth[tiab]',
                                  as_json=True, offset=0, limit=1))
        pagination = page['pagination']
        self.assertFalse(pagination['exceeded'])
        self.assertEqual(pagination['window_total'], 1)  # 本项目自订：两个来源的首项相同
        self.assertEqual(pagination['next_offset'], 1)
        self.assertIn('下一页 offset=1, limit=1', '\n'.join(page['warnings']))

    def test_relative_window_drift_marks_affected_matches_unknown(self):
        windows = iter(['2026/10/21:2026/11/19[Date - Publication]',
                        '2026/10/22:2026/11/20[Date - Publication]'])  # 本项目自订：两批窗口不同

        async def drifting(term, **kwargs):
            batch = re.findall(r'(\d+)\[UID\]', term, re.I)
            return response(term + ' AND ' + next(windows), str(len(batch)), batch)

        with patch.object(m, 'GOLD_CHUNK', 2), patch.object(m, '_esearch', drifting):
            data = json.loads(run(m.links, '1', to='pubmed', term='tooth[tiab]', reldate=30, as_json=True))
        self.assertEqual(data['match_summary']['unknown'], len(self.targets))
        self.assertEqual(data['match_summary']['inside'] + data['match_summary']['outside'], 0)
        self.assertTrue(all(row['in_term'] is None for row in data['candidate_details']))
        self.assertIn('相对日期窗口不一致', '\n'.join(data['warnings']))


if __name__ == '__main__':
    unittest.main()

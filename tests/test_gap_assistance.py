"""检索辅助回归；全部 PMID、计数、日期和预算替身均为本项目自订测试输入，不是真实文献证据。"""
import importlib.util
import json
import os
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
        with patch.object(m, '_esearch', self.fake), patch.dict(os.environ, {'PUBMED_AUDIT_DIR': ''}):
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
        with patch.object(m, '_esearch', self.search):
            data = self.call(term='tooth[tiab]', with_titles=True, **scope)
        rows = {r['pmid']: r for r in data['candidate_details']}
        self.assertTrue(rows['2']['in_term'])  # 本项目自订：夹具命中
        self.assertFalse(rows['3']['in_term'])  # 本项目自订：夹具不命中
        self.assertEqual(rows['3']['title'], 'Title 3')  # 本项目自订：标题保留
        self.assertTrue(rows['1']['is_seed'])  # 本项目自订：种子明确标记
        self.assertEqual(data['candidate_pmids'], self.targets)
        self.assertEqual(data['outside_term_pmids'], ['1', '3'])  # 本项目自订：未命中按原名次
        self.assertEqual({k: self.search_calls[0][1][k] for k in scope}, scope)  # 本项目自订：首批参数
        self.assertEqual(data['match_scope']['datetype'], 'edat')

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
        with patch.object(m, '_esearch', AsyncMock(return_value=response('tooth[tiab]', '0'))):  # 本项目自订：分页候选未命中
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
            data = self.call(term='effect of tooth[tiab]')
        rows = {r['pmid']: r for r in data['candidate_details']}
        self.assertTrue(rows['2']['in_term'])  # 本项目自订：停用词提示不抹掉有效匹配
        self.assertFalse(rows['3']['in_term'])  # 本项目自订：确定未匹配
        self.assertIn('停用词', '\n'.join(data['warnings']))

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
        with patch.object(m, '_esearch', self.search):
            text = run(m.links, '1', to='pubmed', term='tooth[tiab]', with_titles=True)  # 本项目自订：源 PMID
        self.assertIn('未命中当前式', text)
        self.assertIn('Title 3', text)  # 本项目自订：候选标题
        self.assertIn('种子自身', text)
        self.assertIn('相似或被引用不等于符合研究问题', text)


if __name__ == '__main__':
    unittest.main()

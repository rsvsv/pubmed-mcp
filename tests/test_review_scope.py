"""复查范围回归。查询、日期、PMID 和响应均为本项目自订测试夹具。"""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import anyio

spec = importlib.util.spec_from_file_location('review_scope', Path(__file__).resolve().parents[1] / 'pubmed_mcp.py')  # 本项目自订：源码目录
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

SCOPE = dict(field='ti', mindate='2020/01', maxdate='2024', reldate='30', datetype='edat')  # 本项目自订：交集范围夹具
NORMAL = dict(SCOPE, field='Title', mindate='2020-01', reldate=30)  # 本项目自订：规范参数夹具


def run(tool, *args, **kwargs):
    async def call():
        return await tool(*args, **kwargs)
    return anyio.run(call)


class ReviewScope(unittest.TestCase):
    def test_recall_sends_same_scope_to_every_wire_request(self):
        calls = []
        async def wire(path, params, timeout):
            self.assertEqual(path, 'esearch.fcgi')
            calls.append(dict(params))
            hit = '11' if '11[uid]' in params['term'] else '22'  # 本项目自订：每批唯一测试 PMID
            return json.dumps({'esearchresult': {'count': '1', 'idlist': [hit], 'querytranslation': params['term']}})  # 本项目自订：单项响应
        with patch.object(m, 'GOLD_CHUNK', 1), patch.object(m, '_get_async', wire):  # 本项目自订：强制分批
            data = json.loads(run(m.check_recall, 'fixture', '11 22', True, **SCOPE))  # 本项目自订：种子
        self.assertEqual(data['retrieved'], ['11', '22'])  # 本项目自订：仅验证参数已下发，不代表回显证实范围
        self.assertEqual(data['unknown'], data['retrieved'])
        self.assertEqual(data['found'], [])
        self.assertEqual(data['scope'], NORMAL)
        self.assertEqual(len(calls), 2)  # 本项目自订：期望批数
        for request in calls:
            self.assertEqual({k: request[k] for k in NORMAL}, NORMAL)

    def test_only_bad_recall_batch_is_unknown(self):
        pages = [{'count':'1', 'idlist':[pid], 'querytranslation':qt} for pid, qt in (  # 本项目自订：每批仅命中首项
            ('11', 'tooth[Title] AND (11[UID] OR 12[UID]) AND 2024/01/01:2024/12/31[Date - Publication]'),  # 本项目自订：回显日期范围
            ('21', 'tooth[Title] AND (21[UID] OR 22[UID])'))]  # 本项目自订：丢日期，不论返回与否均未知
        with patch.object(m, 'GOLD_CHUNK', 2), patch.object(m, '_esearch', AsyncMock(side_effect=pages)), patch.object(m, '_esummary_dicts', AsyncMock(return_value=[])) as meta:  # 本项目自订：两项一批
            result = json.loads(run(m.check_recall, 'tooth', '11 12 21 22', True, field='Title', mindate='2024', maxdate='2024'))  # 本项目自订：全年范围
        self.assertEqual(result['retrieved'], ['11','21'])  # 本项目自订：原始返回
        self.assertEqual(result['found'], ['11'])  # 本项目自订：可靠批的命中
        self.assertEqual(result['unknown'], ['21','22'])  # 本项目自订：失真批的实取和未取均保留
        self.assertEqual([mi['pmid'] for mi in result['missing']], ['12'])  # 本项目自订：仅可靠批的未命中
        meta.assert_awaited_once_with(['12'], include_unavailable=True)  # 本项目自订：未知不是未命中
        controls = {item['omit']: item['arguments'] for item in result['suggested_controls']}
        self.assertEqual(set(controls), {'外加绝对日期', '外加字段'})
        original = dict(term=result['term'], pmids='11 12 21 22', as_json=True, **result['scope'])  # 本项目自订：同批已知集
        self.assertEqual(controls['外加绝对日期'], {**original, 'mindate': '', 'maxdate': ''})
        self.assertEqual(controls['外加字段'], {**original, 'field': ''})

    def test_batch_uids_are_a_required_complete_disjunction(self):
        a, b = '11', '22'  # 本项目自订：实取首号，次号未返回
        full = f'({a}[UID] OR "{b}"[PMID])'
        subject = f'(tooth[Title] OR {b}[UID])'
        for qt, valid in [(f'{subject} AND {full}', True), (f'{subject} AND {a}[UID]', False),
                          (f'{subject} OR {full}', False), (f'{subject} NOT {full}', False),
                          (f'{subject} AND ({a}[UID] AND {b}[UID])', False),
                          (f'{subject} AND ({a}[UID] OR {b}[Title])', False),
                          (f'{subject} AND {a}[UID] AND "{b}[UID]"[Title]', False),
                          (f'({subject} AND {full}) OR tooth[Title]', False)]:
            response = {'count':'1', 'idlist':[a], 'querytranslation':qt}  # 本项目自订：响应编号及计数不随回显变化
            with self.subTest(qt=qt), patch.object(m, '_esearch', AsyncMock(return_value=response)), patch.object(m, '_esummary_dicts', AsyncMock(return_value=[])) as meta:
                data = json.loads(run(m.check_recall, subject, f'{a} {b}', True))
                self.assertEqual(data['retrieved'], [a])
                self.assertEqual(data['found'], [a] if valid else [])
                self.assertEqual([r['pmid'] for r in data['missing']], [b] if valid else [])
                self.assertEqual(data['unknown'], [] if valid else [a,b])
                if not valid:
                    meta.assert_not_awaited()

    def test_original_uid_group_cannot_stand_in_for_tool_group(self):
        gold = '11 22'  # 本项目自订：原式也包含完整已知集
        subject = '(11[UID] OR 22[UID])'  # 本项目自订：两处编号条件必须各自完整
        for clause, valid in [(subject, True), ('11[UID]', False), ('', False),  # 本项目自订：完整、只剩一个、全部消失
                              ('(11[UID] AND 22[UID])', False)]:  # 本项目自订：编号数相同，OR 却被换成 AND
            qt = f'{subject} AND {clause}' if clause else subject
            with self.subTest(qt=qt), patch.object(m, '_esearch', AsyncMock(return_value={'count':'0','querytranslation':qt})), patch.object(m, '_esummary_dicts', AsyncMock(return_value=[])):  # 本项目自订：模拟空交集
                data = json.loads(run(m.check_recall, subject, gold, True))
                self.assertEqual(data['unknown'], [] if valid else gold.split())

    def test_recall_invalid_subfield_or_ambiguous_relative_axis(self):
        pid = '11'  # 本项目自订：已知集种子
        pub = '2020/01/01:2020/12/31[Date - Publication]'  # 本项目自订：原式发表窗口
        entry = '2024/01/01:2024/01/31[Date - Entry]'  # 本项目自订：外加入库窗口
        for term, qt, scope, valid in [('"a b"[au:~3]', '"a b"[Author]', {}, False),  # 本项目自订：不支持邻近的字段
                                       ('tooth AND 2020[dp]', f'tooth AND {pub}', {'reldate':30,'datetype':'edat'}, True),  # 本项目自订：两个日期轴
                                       ('tooth AND 2024[edat]', 'tooth', {'reldate':30,'datetype':'edat'}, False)]:  # 本项目自订：同轴无法归属
            qt += f' AND {pid}[UID]' + (' AND ' + entry if scope else '')
            with self.subTest(term=term), patch.object(m, '_esearch', AsyncMock(return_value={'count':'1','idlist':[pid],'querytranslation':qt})):  # 本项目自订：实取一条
                data = json.loads(run(m.check_recall, term, pid, True, **scope))
                self.assertEqual(data['found'], [pid] if valid else [])
                self.assertEqual(data['unknown'], [] if valid else [pid])

    def test_diff_both_sides_and_every_page_use_scope(self):
        pages = []
        async def wire(path, params, timeout):
            self.assertEqual(path, 'esearch.fcgi')
            pages.append((params['term'], params['retstart']))
            self.assertEqual({k: params[k] for k in NORMAL}, NORMAL)
            ids = {'left': ['11', '22'], 'right': ['22', '33']}[params['term']]  # 本项目自订：两个结果集
            return json.dumps({'esearchresult': {'count': str(len(ids)), 'idlist': [ids[params['retstart']]], 'querytranslation': params['term']}})
        with patch.object(m, 'RETMAX_CAP', 1), patch.object(m, '_get_async', wire):  # 本项目自订：强制两页
            data = json.loads(run(m.diff, 'left', 'right', as_json=True, **SCOPE))
        self.assertEqual(data['scope'], NORMAL)
        self.assertEqual(data['added'], ['33'])  # 本项目自订：期望新增
        self.assertEqual(data['removed'], ['11'])  # 本项目自订：期望移出
        self.assertCountEqual(pages, [('left', 0), ('left', 1), ('right', 0), ('right', 1)])  # 本项目自订：两侧全部页面

    def test_diff_checks_all_pages_before_labelling_added(self):
        for scenario in ('clean', 'stopword', 'degraded', 'missing_translation', 'changed_translation', 'duplicate'):
            def page(term, retstart=0, **kwargs):  # 本项目自订：分页替身
                ids = ['11', '22'] if term == 'left' else ['22', '33']  # 本项目自订：原式与新式名单
                d = {'count': str(len(ids)), 'idlist': [ids[retstart]], 'querytranslation': term, '_term': term}
                if term == 'right' and retstart:
                    if scenario == 'stopword': d['warninglist'] = {'phrasesignored': ['of']}
                    if scenario == 'degraded': d['errorlist'] = {'phrasesnotfound': ['fragment']}
                    if scenario == 'missing_translation': d['querytranslation'] = ''
                    if scenario == 'changed_translation': d['querytranslation'] = 'different'
                    if scenario == 'duplicate': d['idlist'] = ids[:1]  # 本项目自订：重复首页编号
                return d
            with self.subTest(scenario=scenario), patch.object(m, '_esearch', AsyncMock(side_effect=page)), patch.object(m, '_esummary_dicts', AsyncMock(return_value=[])):
                data = json.loads(run(m.diff, 'left', 'right', as_json=True, preview=True))
                reviewed = scenario in ('clean', 'stopword')
                self.assertEqual(data['provenance']['comparison'], 'compared' if reviewed else 'needs_review')
                if scenario != 'duplicate':
                    self.assertEqual(data['preview']['added']['missing_pmids'], data['added'])
                    self.assertEqual(data['added'], ['33'])  # 本项目自订：缺题名仍保留实取差集
                text = run(m.diff, 'left', 'right', preview=True)
                self.assertIn('当次仅 b 有标题预览' if reviewed else '实取仅 b 有标题预览', text)
                if scenario == 'changed_translation':
                    changed, = data['provenance']['review_pages']
                    self.assertEqual(changed['querytranslation'], 'different')
                    self.assertIn('term_b', changed['stage'])
                    self.assertIn('different', text)

    def test_embedded_window_does_not_verify_external_dates(self):
        qt = 'tooth[Title] AND 2020/01/01:2020/12/31[Date - Publication]'  # 本项目自订：回显仅含已有窗口
        page = {'count': '0', 'idlist': [], 'querytranslation': qt}  # 本项目自订：空集合也须验证范围
        with patch.object(m, '_esearch', AsyncMock(return_value=page)):
            for lo, status in [('2020', 'compared'), ('2024', 'needs_review')]:  # 本项目自订：匹配与被遮蔽的外加窗口
                result = json.loads(run(m.diff, 'tooth AND 2020[dp]', 'tooth', as_json=True, mindate=lo, maxdate=lo))  # 本项目自订：测试原式
                self.assertEqual(result['provenance']['comparison'], status)

    def test_date_must_constrain_every_branch_and_field_every_subject(self):
        window = '2024/01/01:2024/12/31[Date - Publication]'  # 本项目自订：绝对日期回显
        for qt, expected in [(f'tooth[Title] OR {window}', False), (f'tooth[Title] NOT {window}', False),
                             (f'(tooth[Title] OR jaw[Title]) AND {window}', True),
                             (f'(tooth[Title] OR jaw[All Fields]) AND {window}', False),
                             (f'"{window}"[Title]', False)]:
            with self.subTest(qt=qt), patch.object(m, '_esearch', AsyncMock(return_value={'count':'0', 'querytranslation':qt})):
                report = json.loads(run(m.diff, 'tooth AND 2024[dp]', 'jaw AND 2024[dp]', field='ti', mindate='2024', maxdate='2024', as_json=True))  # 本项目自订：同范围比较
                self.assertEqual(report['provenance']['comparison'], 'compared' if expected else 'needs_review')

    def test_relative_scope_compares_actual_server_days(self):
        windows = ['2024/11/01:2024/12/01', '2024/11/02:2024/12/02']  # 本项目自订：服务器换日前后窗口
        for cross_day in (False, True):
            def page(term, **kw):
                win = windows[bool(cross_day and term == 'right')]
                return {'count':'0', 'querytranslation':f'{term}[Title] AND {win}[Date - Entry]'}  # 本项目自订：完整空页
            with self.subTest(cross_day=cross_day), patch.object(m, '_esearch', AsyncMock(side_effect=page)):
                origin = json.loads(run(m.diff, 'left', 'right', as_json=True, datetype='edat', reldate=30))['provenance']  # 本项目自订：相对天数
                self.assertEqual(origin['comparison'], 'needs_review' if cross_day else 'compared')
                if cross_day: self.assertIn('窗口不一致', str(origin['reasons']))

    def test_identical_queries_and_lowercase_boolean_degrade(self):
        for left, right, qt in [('tooth', 'tooth', 'tooth[Title]'),
                                ('miniscrew[Title] and stability[Title]', 'stability', '"miniscrew"[Title] AND "and stability"[Title]')]:
            pages = [{'count':'1', 'idlist':[pid], 'querytranslation':qt if term == left else term}  # 本项目自订：两次各命中一个不同编号
                     for term, pid in ((left, '11'), (right, '22'))]  # 本项目自订：响应编号
            with self.subTest(left=left), patch.object(m, '_esearch', AsyncMock(side_effect=pages)), patch.object(m, '_audit') as audit:
                data = json.loads(run(m.diff, left, right, as_json=True))
                self.assertEqual(data['provenance']['comparison'], 'needs_review')
                self.assertEqual(audit.call_args.kwargs['provenance'], data['provenance'])
                self.assertTrue(data['added'])

    def test_distinct_input_but_same_execution_is_not_query_addition(self):
        def page(term, **kwargs):
            return {'querytranslation':'tooth[Title]', 'count':'1', 'idlist':['11' if term == 'a' else '22']}  # 本项目自订：相同执行式、不同名单
        with patch.object(m, '_esearch', AsyncMock(side_effect=page)):
            data = json.loads(run(m.diff, 'a', 'b', as_json=True))
        self.assertEqual(data['provenance']['comparison'], 'needs_review')
        self.assertTrue(data['provenance']['reasons'])

    def test_invalid_scope_fails_before_requests(self):
        for tool, args in ((m.diff, ('a', 'b')), (m.check_recall, ('a', '11'))):  # 本项目自订：种子
            for bad in ({'field': 'bogus'}, {'reldate': -1}, {'datetype': 'bad'}, {'mindate':'2025', 'maxdate':'2020'}):  # 本项目自订：非法范围
                with self.subTest(tool=tool.__name__, bad=bad), patch.object(m, '_get_async', AsyncMock()) as net:
                    with self.assertRaises(m.NcbiError):
                        run(tool, *args, **bad)
                    net.assert_not_awaited()

    def test_only_seed_and_date_left_or_no_translation(self):
        for qt, flags in [('11[UID] AND 2024/01/01:2024/12/31[Date - Publication]', (True, False)), ('', (False, True))]:  # 本项目自订：原词消失与无回显
            result = {'count': '1', 'idlist': ['11'], 'querytranslation': qt}  # 本项目自订：原始交集响应
            with patch.object(m, '_esearch', AsyncMock(return_value=result)):
                data = json.loads(run(m.check_recall, '牙科', '11', True, mindate='2024', maxdate='2024'))  # 本项目自订：限定发表年份
            self.assertEqual((data['term_ignored'], data['term_degraded']), flags)

    def test_numeric_pmid_field_is_a_legitimate_term(self):
        qt = '11[UID] AND 11[UID] AND 2024/01/01:2024/12/31[Date - Entry]'  # 本项目自订：真实词项本来就是编号
        with patch.object(m, '_esearch', AsyncMock(return_value={'count':'1', 'idlist':['11'], 'querytranslation':qt})):  # 本项目自订：响应
            data = json.loads(run(m.check_recall, '11', '11', True, field='PMID', mindate='2024', datetype='edat'))  # 本项目自订：字段与日期
        self.assertFalse(data['term_ignored'])

    def test_scope_in_text_and_mcp_schema(self):
        for tool, args in ((m.diff, ('a', 'b')), (m.check_recall, ('a', '11'))):  # 本项目自订：种子
            empty = {'count':'0', 'idlist':[], 'querytranslation':'a'}  # 本项目自订：空响应
            with patch.object(m, '_esearch', AsyncMock(return_value=empty)), patch.object(m, '_esummary_dicts', AsyncMock(return_value=[])):
                text = run(tool, *args, **SCOPE)
                self.assertIn('入库日期 2020-01 至 2024 与 近 30 天', text)  # 本项目自订：实际范围展示
                self.assertIn('字段 Title', text)
                self.assertIn('日期未另限', run(tool, *args, datetype='edat'))
        for tool in run(m.mcp.list_tools):
            if tool.name in ('diff', 'check_recall'):
                schema = getattr(tool, 'inputSchema', None) or tool.input_schema
                self.assertTrue(set(NORMAL) <= schema['properties'].keys())


if __name__ == '__main__':
    unittest.main()

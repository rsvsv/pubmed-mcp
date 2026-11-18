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
        self.assertEqual(data['found'], ['11', '22'])  # 本项目自订：期望找回
        self.assertEqual(data['scope'], NORMAL)
        self.assertEqual(len(calls), 2)  # 本项目自订：期望批数
        for request in calls:
            self.assertEqual({k: request[k] for k in NORMAL}, NORMAL)

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

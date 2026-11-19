"""links 完整来源模型/JSON/分页/文件出口的离线回归。

所有 NCBI 响应用本文件内的合成夹具（本项目自订测试输入），不联网；
PMID、链接号、计数均为测试数据，不代表任何真实文献关系。
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import anyio

spec = importlib.util.spec_from_file_location(
    "links_full_subject", Path(__file__).resolve().parents[1] / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


def elink_body(pid, linksetdbs):
    return json.dumps({"linksets": [{"ids": [pid], "linksetdbs": linksetdbs}]})


def static_elink(payload):
    async def fake(path, params, timeout):
        assert path == "elink.fcgi"
        return json.dumps(payload)
    return fake


class SDKOutput(unittest.TestCase):
    def test_full_file_via_sdk(self):
        async def call():
            with tempfile.TemporaryDirectory() as folder:
                target = Path(folder)/'links.json'
                body={'linksets':[{'ids':['1'],'linksetdbs':[{'linkname':'pubmed_pubmed_refs','links':['2','3']}]}]}
                with patch.object(m,'_pmids_exist',AsyncMock(return_value={'1':'present'})), patch.object(m,'_get_async',static_elink(body)):
                    result=await m.mcp.call_tool('links',{'pmids':'1','to':'refs','out_path':str(target),'limit':1})
                self.assertIn('sha256',str(result))
                self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['candidate_pmids'],['2','3'])
        anyio.run(call)


class TextCompatibility(unittest.TestCase):
    def test_direct_render_and_pmc_prefix_unchanged(self):
        body = {"linksets": [{"ids": ["1"], "linksetdbs": [
            {"linkname": "pubmed_pmc", "links": ["123", "124"]}]}]}
        with patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present"})), \
                patch.object(m, "_get_async", static_elink(body)):
            text = run(m.links, "1")
        self.assertIn("PMID 1  pubmed_pmc=PMC123,PMC124", text)
        self.assertNotIn("说明：", text)

    def test_citation_cap_and_gloss_unchanged(self):
        async def fake(path, params, timeout):
            return elink_body(params["id"], [
                {"linkname": params["linkname"], "links": ["10", "11", "12"]}])
        with patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present"})), \
                patch.object(m, "SNOW_SINGLE_CAP", 2), patch.object(m, "_get_async", fake):
            for direction in ("refs", "citedin"):
                text = run(m.links, "1", to=direction)
                label = '参考文献' if direction == 'refs' else '施引文献'
                self.assertIn(f'{label} 3 篇；目标 PMID：10,11…', text)  # 本项目自订：总数与展示编号分栏

    def test_missing_metadata_stays_unknown_in_text(self):
        async def fake(path, params, timeout):
            if path == "esummary.fcgi":
                return json.dumps({"result": {"uids": []}})
            return json.dumps({"linksets": [{"ids": ["1"]}]})
        with patch.object(m, "_get_async", fake):
            text = run(m.links, "1")
        self.assertIn("元数据缺失", text)
        self.assertNotIn("无直接对应记录)", text)

    def test_no_linkset_is_not_no_relation_in_text(self):
        with patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present"})), \
                patch.object(m, "_get_async", static_elink({"linksets": []})):
            with self.assertRaises(m.NcbiError) as caught:
                run(m.links, '1')  # 本项目自订：唯一来源也无法确认
        self.assertIn('没返回这个号的 linkset', str(caught.exception))
        self.assertIn('不是', str(caught.exception))

    def test_citation_ownership_failure_raises(self):
        for sets in ([], [{"ids": ["9"]}], [{"ids": ["1", "9"]}]):
            with self.subTest(sets=sets), \
                    patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present"})), \
                    patch.object(m, "_get_async", static_elink({"linksets": sets})):
                with self.assertRaisesRegex(m.NcbiError, "失败"):
                    run(m.links, "1", to="refs")

    def test_single_citation_id_is_not_a_count(self):
        body = {'linksets': [{'ids': ['1'], 'linksetdbs': [  # 本项目自订：合成来源
            {'linkname': 'pubmed_pubmed_citedin', 'links': ['3']}]}]}  # 本项目自订：目标号与篇数不同
        with patch.object(m, '_pmids_exist', AsyncMock(return_value={'1': 'present'})), \
                patch.object(m, '_get_async', static_elink(body)):
            text = run(m.links, '1', to='citedin')  # 本项目自订：合成来源
        self.assertIn('施引文献 1 篇；目标 PMID：3', text)  # 本项目自订：一篇、编号为三

    def test_consecutive_calls_distinguish_zero_error_and_empty_page(self):
        positive, empty, failed, unknown = '11', '22', '33', '44'  # 本项目自订：四个测试源
        states = dict.fromkeys((positive, empty, failed), 'present') | {unknown: 'unknown'}
        async def fake(path, params, timeout):
            pid = params['id']
            if pid == failed:
                raise m.NcbiError('fixture timeout')
            groups = [{'linkname': params['linkname'], 'links': ['101', '202']}] if pid == positive else []  # 本项目自订：分页目标
            return elink_body(pid, groups)
        with patch.object(m, '_pmids_exist', AsyncMock(return_value=states)), patch.object(m, '_get_async', fake):
            for options in ({}, {'as_json': True}, {'compact': True}):
                def call(ids, **kw):
                    return run(m.links, ids, to='citedin', **options, **kw)
                def notices(raw):
                    return '\n'.join(json.loads(raw)['warnings']) if options else raw
                for _ in ('first', 'repeat'):
                    good = call(positive)
                    self.assertNotIn('放宽式', good)
                    self.assertNotIn('无人引用', good)
                    if options:
                        self.assertEqual(json.loads(good)['warnings'], [])
                        self.assertEqual(json.loads(good)['provenance']['comparison'], 'not_compared')
                raw_mixed = call(' '.join((empty, failed, unknown)))
                mixed = notices(raw_mixed)
                if not options:
                    self.assertEqual(mixed.count('PMID ' + failed), 1)  # 本项目自订：仅在失败来源行写原因
                    rows = {p: next(line for line in mixed.splitlines() if line.startswith('PMID ' + p + ' '))
                            for p in (empty, failed, unknown)}
                    self.assertIn('无人引用', rows[empty])
                    self.assertIn('请求失败', rows[failed])
                    self.assertIn('存在性', rows[unknown])
                else:
                    sources = {r['source']: r for r in json.loads(raw_mixed)['sources']}
                    self.assertEqual(sources[empty]['relation_status'], 'ok')
                    self.assertEqual(sources[failed]['relation_status'], 'error')
                    self.assertEqual(sources[unknown]['source_status'], 'unknown')
                    self.assertIn('PMID ' + empty + '：PubMed 未录入', mixed)
                for expected in (empty, failed, unknown, '无人引用', '请求失败', '存在性'):
                    self.assertIn(expected, mixed)
                self.assertEqual(mixed.count('无人引用'), 1)  # 本项目自订：只归属确认为零的来源
                for pid in (unknown, positive):
                    page = call(pid, offset=2, limit=1)  # 本项目自订：超过末页或来源未知
                    self.assertNotIn('无人引用', page)
                    self.assertNotIn('下一页 offset=', page)
                    if pid == positive and not options:
                        self.assertIn('分页前有 2 个目标；本页未展示编号（offset=2, limit=1），不是零关联', page)  # 本项目自订：页空、总数非零
                first = call(positive, limit=1)  # 本项目自订：首页
                if options:
                    data = json.loads(first)
                    self.assertEqual(data.get('pagination', data)['next_offset'], 1)  # 本项目自订：后续页偏移
                else:
                    self.assertIn('下一页 offset=1, limit=1', first)  # 本项目自订：每源翻页
                self.assertIn('无人引用', notices(call(empty)))
                self.assertNotIn('无人引用', notices(call(positive)))

    def test_zero_citation_text_kept(self):
        with patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present"})), \
                patch.object(m, "_get_async", static_elink({"linksets": [{"ids": ["1"]}]})):
            text = run(m.links, "1", to="refs")
            cited = run(m.links, '1', to='citedin')  # 本项目自订：同一无关联响应
        self.assertIn("不等于原文无引文", text)
        self.assertEqual(cited.count('无人引用'), 1)  # 本项目自订：单来源警示就地保留一次
        self.assertNotIn('方括号', cited)
        self.assertNotIn('放宽式', cited)


class JsonModel(unittest.TestCase):
    def test_pmc_schema_source_shape_and_empty_candidates(self):
        body = {"linksets": [{"ids": ["1"], "linksetdbs": [
            {"linkname": "pubmed_pmc", "links": ["123", "124"]}]}]}
        with patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present"})), \
                patch.object(m, "_get_async", static_elink(body)):
            data = json.loads(run(m.links, "1", as_json=True))
        self.assertEqual(data["schema"], "pubmed.links.v1")
        self.assertEqual(data["candidate_pmids"], [])
        self.assertEqual(set(data["sources"][0]),
                         {'source', 'source_status', 'relation', 'relation_status', 'target_ids', 'page_positions',
                          'error', 'fetched_at', 'linknames', 'other_relations'})
        self.assertEqual(data["sources"][0]["source"], "1")
        self.assertEqual(data["sources"][0]["relation"], "pmc")
        self.assertEqual(data["sources"][0]["target_ids"], ["123", "124"])
        self.assertTrue(data["sources"][0]["fetched_at"])
        self.assertEqual(data["edges"], [
            {"source": "1", "relation": "pmc", "target": "123"},
            {"source": "1", "relation": "pmc", "target": "124"}])

    def test_pubmed_candidates_dedupe_across_sources(self):
        async def fake(path, params, timeout):
            links = {"1": ["10", "11"], "2": ["11", "20"]}[params["id"]]
            return elink_body(params["id"], [{"linkname": "pubmed_pubmed", "links": links}])
        with patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present", "2": "present"})), \
                patch.object(m, "_get_async", fake):
            data = json.loads(run(m.links, "1 2", to="pubmed", as_json=True))
        self.assertEqual(data["candidate_pmids"], ["10", "11", "20"])
        self.assertEqual(len(data["edges"]), 4)

    def test_per_source_pagination_and_counts_before_pagination(self):
        async def fake(path, params, timeout):
            links = {"1": ["10", "11", "12"], "2": ["20", "21"]}[params["id"]]
            return elink_body(params["id"], [{"linkname": "pubmed_pmc", "links": links}])
        with patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present", "2": "present"})), \
                patch.object(m, "_get_async", fake):
            data = json.loads(run(m.links, "1 2", as_json=True, offset=1, limit=1))
            text = run(m.links, '1 2', offset=1, limit=1)  # 本项目自订：每源只看第二项
            compact = json.loads(run(m.links, '1 2', compact=True, offset=2, limit=1))  # 本项目自订：第二个源已耗尽
        self.assertIn('来源列表第 2 至 2 项（共 3 个）：PMC11', text)  # 本项目自订：第一源第二项
        self.assertNotIn('PMC10', text)  # 本项目自订：不能重复首页
        self.assertEqual(compact['sources'][1]['page_positions'], {'first': None, 'last': None, 'returned': 0})  # 本项目自订：空页无名次
        self.assertEqual([s["target_ids"] for s in data["sources"]], [["11"], ["21"]])
        self.assertEqual(data["counts"]["per_source"], {"1": 3, "2": 2})
        self.assertEqual(data["counts"]["targets"], 5)
        self.assertEqual(data["counts"]["targets_returned"], 2)
        self.assertTrue(data["pagination"]["per_source"])

    def test_default_json_without_limit_is_full(self):
        links = ["10", "11", "12", "13", "14", "15", "16", "17", "18"]
        with patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present"})), \
                patch.object(m, "_get_async", static_elink(
                    {"linksets": [{"ids": ["1"], "linksetdbs": [
                        {"linkname": "pubmed_pmc", "links": links}]}]})):
            data = json.loads(run(m.links, "1", as_json=True))
        self.assertEqual(data["sources"][0]["target_ids"], links)
        self.assertEqual(data["counts"]["targets_returned"], len(links))

    def test_unknown_and_no_relation_are_distinct(self):
        async def fake(path, params, timeout):
            return json.dumps({"linksets": [{"ids": [params["id"]]}]})
        with patch.object(m, "_pmids_exist", AsyncMock(
                return_value={"1": "unknown", "2": "present"})), \
                patch.object(m, "_get_async", fake):
            data = json.loads(run(m.links, "1 2", as_json=True))
        by_source = {s["source"]: s for s in data["sources"]}
        self.assertEqual(by_source["1"]["source_status"], "unknown")
        self.assertEqual(by_source["2"]["source_status"], "present")
        self.assertEqual(by_source["1"]["target_ids"], [])
        self.assertEqual(by_source["2"]["target_ids"], [])

    def test_partial_error_retained_and_all_failed_raises(self):
        async def partial(path, params, timeout):
            if params["id"] == "1":
                return elink_body("1", [{"linkname": "pubmed_pmc", "links": ["123"]}])
            raise m.NcbiError("upstream boom")
        with patch.object(m, "_pmids_exist", AsyncMock(
                return_value={"1": "present", "2": "present"})), \
                patch.object(m, "_get_async", partial):
            data = json.loads(run(m.links, "1 2", as_json=True))
        self.assertEqual(len(data["sources"]), 2)
        self.assertEqual(data["sources"][0]["target_ids"], ["123"])
        self.assertIn("upstream boom", data["sources"][1]["error"])
        self.assertEqual(data["counts"]["sources_with_errors"], 1)
        with patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present"})), \
                patch.object(m, "_get_async", AsyncMock(side_effect=m.NcbiError("down"))):
            with self.assertRaisesRegex(m.NcbiError, "失败"):
                run(m.links, "1")

    def test_foreign_linkset_not_attributed(self):
        body = {"linksets": [{"ids": ["9"], "linksetdbs": [
            {"linkname": "pubmed_pmc", "links": ["999"]}]}]}
        with patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present"})), \
                patch.object(m, "_get_async", static_elink(body)):
            with self.assertRaisesRegex(m.NcbiError, "归属于请求号"):
                run(m.links, "1", as_json=True)

    def test_invalid_pubmed_target_rejected(self):
        body = {"linksets": [{"ids": ["1"], "linksetdbs": [
            {"linkname": "pubmed_pubmed", "links": ["not-a-pmid"]}]}]}
        with patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present"})), \
                patch.object(m, "_get_async", static_elink(body)):
            with self.assertRaisesRegex(m.NcbiError, "非法目标 ID"):
                run(m.links, "1", to="pubmed", as_json=True)

    def test_negative_offset_or_limit_rejected_before_network(self):
        for kwargs in ({"offset": -1}, {"limit": -2}):
            with self.subTest(kwargs=kwargs), \
                    patch.object(m, "_pmids_exist", AsyncMock(side_effect=AssertionError("must not call"))), \
                    patch.object(m, "_get_async", AsyncMock(side_effect=AssertionError("must not call"))):
                with self.assertRaises(m.NcbiError):
                    run(m.links, "1", **kwargs)

    def test_out_path_writes_full_model_and_returns_summary(self):
        body = {"linksets": [{"ids": ["1"], "linksetdbs": [
            {"linkname": "pubmed_pmc", "links": ["123", "124"]}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "links.json")
            with patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present"})), \
                    patch.object(m, "_get_async", static_elink(body)):
                summary = json.loads(run(m.links, "1", as_json=True, out_path=path,
                                         offset=1, limit=1))
            raw = Path(path).read_text(encoding="utf-8")
            full = json.loads(raw)
        self.assertEqual(summary["schema"], "pubmed.links.file.v1")
        self.assertEqual(summary["content_schema"], full["schema"])
        self.assertEqual(summary["path"], os.path.abspath(path))
        self.assertEqual(summary["bytes"], len(raw.encode("utf-8")))
        self.assertEqual(summary["sha256"],
                         hashlib.sha256(raw.encode("utf-8")).hexdigest())
        self.assertEqual(full["sources"][0]["target_ids"], ["123", "124"])
        self.assertEqual(full["counts"]["targets"], 2)

    def test_out_path_no_clobber_and_checked_before_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "exists.json")
            Path(path).write_text("old", encoding="utf-8")
            with patch.object(m, "_pmids_exist",
                              AsyncMock(side_effect=AssertionError("must not call"))), \
                    patch.object(m, "_get_async",
                                 AsyncMock(side_effect=AssertionError("must not call"))):
                with self.assertRaises(m.NcbiError):
                    run(m.links, "1", out_path=path)


if __name__ == "__main__":
    unittest.main()
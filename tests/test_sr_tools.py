"""Systematic-review tool regressions (check_recall / mesh / diff / export / audit).
All fixture numbers, PMIDs, MeSH UIDs and dates are 本项目自订测试输入, not literature evidence.
Run: python -I -m unittest discover -s <repo>/tests -v
"""
import contextlib
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

import anyio

spec = importlib.util.spec_from_file_location("sr_subject", Path(__file__).resolve().parents[1] / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


def esr(count, ids, qt='"tooth"[All Fields]', term="tooth", retstart=0, **extra):
    """本项目自订：构造 esearch 响应形状（键名与真实 NCBI 一致，见探针记录）。"""
    return {"_term": term, "count": str(count), "idlist": list(ids), "retstart": str(retstart),
            "querytranslation": qt, **extra}


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def path(self, name):
        return os.path.join(self.dir, name)


class CheckRecall(unittest.TestCase):
    def setUp(self):
        self.esearch = AsyncMock(return_value=esr(2, ["1", "2"], term="(tooth) AND (1[uid] OR 2[uid])"))
        self.esummary = AsyncMock(return_value=[])
        for name, mock in (("_esearch", self.esearch), ("_esummary_dicts", self.esummary)):
            p = patch.object(m, name, mock)
            p.start()
            self.addCleanup(p.stop)

    def test_all_found_skips_metadata(self):
        self.esummary.side_effect = AssertionError("no missing items, must not fetch metadata")
        text = run(m.check_recall, "tooth", "1,2")
        self.assertIn("检回 2 个，未检回 0 个", text)
        self.assertIn("不代表整体查全率", text)
        self.esummary.assert_not_awaited()

    def test_partial_missing_present_title(self):
        self.esearch.return_value = esr(2, ["1", "3"], term="(tooth) AND (1[uid] OR 2[uid] OR 3[uid])")
        self.esummary.return_value = [("2", {"title": "Missing paper title"})]
        text = run(m.check_recall, "tooth", "1 2 3")
        self.assertIn("检回 2 个，未检回 1 个", text)
        self.assertIn("PMID 2（记录存在，本次未检回）Missing paper title", text)
        data = json.loads(run(m.check_recall, "tooth", "1 2 3", as_json=True))
        self.assertEqual(data["found"], ["1", "3"])
        self.assertEqual(data["missing"], [{"pmid": "2", "state": "present", "title": "Missing paper title"}])

    def test_missing_states_unavailable_and_unknown(self):
        self.esearch.return_value = esr(0, [], term="(tooth) AND (1[uid] OR 2[uid])")
        self.esummary.return_value = [("1", {"error": "cannot get document summary"})]
        data = json.loads(run(m.check_recall, "tooth", "1,2", as_json=True))
        states = {mi["pmid"]: mi["state"] for mi in data["missing"]}
        self.assertEqual(states, {"1": "unavailable", "2": "unknown"})
        text = run(m.check_recall, "tooth", "1,2")
        self.assertIn("存在性未确认", text)

    def test_metadata_failure_keeps_determination(self):
        self.esearch.return_value = esr(1, ["1"], term="(tooth) AND (1[uid] OR 2[uid])")
        self.esummary.side_effect = m.NcbiError("simulated esummary outage")
        text = run(m.check_recall, "tooth", "1,2")
        self.assertIn("检回 1 个，未检回 1 个", text)
        self.assertIn("缺失项标题获取失败", text)
        self.assertIn("simulated esummary outage", text)

    def test_gold_cap_and_anomaly_fail_before_or_after_network(self):
        with self.assertRaisesRegex(m.NcbiError, "最多 200"):
            run(m.check_recall, "tooth", " ".join(str(i) for i in range(1, 202)))
        self.esearch.assert_not_awaited()
        self.esearch.return_value = esr(5, ["1", "2", "3", "4", "5"], term="(tooth) AND (1[uid] OR 2[uid])")
        with self.assertRaisesRegex(m.NcbiError, "交集结果异常"):
            run(m.check_recall, "tooth", "1,2")

    def test_dedup_note_and_warning_passthrough(self):
        self.esearch.return_value = esr(2, ["1", "2"], term="(tooth) AND (1[uid] OR 2[uid])",
                                        errorlist={"phrasesnotfound": ["zzzqqx"]})
        text = run(m.check_recall, "tooth", "1,1,2")
        self.assertIn("原始输入 3 个，去重后 2 个", text)
        self.assertIn("phrasesnotfound", text)

    def test_sdk_schema_and_call(self):
        tools = run(m.mcp.list_tools)
        self.assertIn("check_recall", [t.name for t in tools])
        run(m.mcp.call_tool, "check_recall", {"term": "tooth", "pmids": "1,2"})

    def test_gold_over_chunk_batches(self):
        # 60 个金标准 → 2 批（每批 50）；fake 按批返回偶数号，验证合并与请求数。
        def fake(term, retmax=20, **kw):
            uids = re.findall(r"(\d+)\[uid\]", term)
            found = [u for u in uids if int(u) % 2 == 0]
            qt = '"tooth"[All Fields] AND (' + " OR ".join(f"{u}[UID]" for u in uids) + ")"
            return esr(len(found), found, qt=qt, term=term)
        self.esearch.side_effect = fake
        gold = " ".join(str(i) for i in range(1, 61))
        data = json.loads(run(m.check_recall, "tooth", gold, as_json=True))
        self.assertEqual(self.esearch.await_count, 2)
        self.assertEqual(data["found"], [str(i) for i in range(2, 61, 2)])
        self.assertEqual(data["gold_count"], 60)
        self.assertFalse(data["term_ignored"])
        self.assertIn("分 2 批", run(m.check_recall, "tooth", gold))

    def test_term_ignored_cjk_false_positive(self):
        # term 被 NCBI 忽略：执行式只剩 uid OR 子句，found 全中但须显著告警〔实测 A6 症状〕。
        self.esearch.return_value = esr(3, ["1", "2", "3"], qt="1[UID] OR 2[UID] OR 3[UID]",
                                        term="(牙根吸收) AND (1[uid] OR 2[uid] OR 3[uid])")
        data = json.loads(run(m.check_recall, "牙根吸收", "1,2,3", as_json=True))
        self.assertTrue(data["term_ignored"])
        self.assertEqual(data["found"], ["1", "2", "3"])
        self.assertTrue(any("未参与实际检索" in w for w in data["warnings"]))
        self.assertIn("found 不可信", run(m.check_recall, "牙根吸收", "1,2,3"))

    def test_normal_term_not_flagged_ignored(self):
        data = json.loads(run(m.check_recall, "tooth", "1,2", as_json=True))
        self.assertFalse(data["term_ignored"])
        self.assertNotIn("found 不可信", run(m.check_recall, "tooth", "1,2"))


class Mesh(unittest.TestCase):
    def setUp(self):
        self.get = AsyncMock()
        p = patch.object(m, "_get_async", self.get)
        p.start()
        self.addCleanup(p.stop)
        self.desc = {"uid": "68014091", "ds_meshui": "D014091",
                     "ds_meshterms": ["Tooth Resorption", "Resorptions, Tooth", "Resorption, Tooth"],
                     "ds_scopenote": "Resorption of calcified dental tissue.",
                     "ds_yearintroduced": "1965",
                     "ds_idxlinks": [{"treenum": "C07.793.901", "children": ["68012391"]}]}
        self.child = {"uid": "68012391", "ds_meshterms": ["Root Resorption"]}

    def responses(self, child_error=False):
        esearch = {"esearchresult": {"count": "1", "idlist": ["68014091"], "querytranslation": '"tooth resorption"[MeSH Terms]'}}
        summary = {"result": {"uids": ["68014091"], "68014091": self.desc}}
        kids = {"result": {"uids": ["68012391"], "68012391": self.child}}
        seq = [json.dumps(esearch), json.dumps(summary)]
        seq.append(m.NcbiError("child lookup failed") if child_error else json.dumps(kids))
        self.get.side_effect = seq

    def test_descriptor_entry_terms_tree_children(self):
        self.responses()
        text = run(m.mesh, "tooth resorption")
        for needle in ("D014091", "Tooth Resorption", "Resorptions, Tooth", "C07.793.901",
                       "Root Resorption (UID 68012391)", "Resorption of calcified dental tissue.", "1965"):
            self.assertIn(needle, text)
        self.responses()
        data = json.loads(run(m.mesh, "tooth resorption", as_json=True))
        de = data["descriptors"][0]
        self.assertEqual(de["children"], [{"uid": "68012391", "name": "Root Resorption"}])
        self.assertEqual(de["entry_terms"], self.desc["ds_meshterms"])

    def test_no_descriptor_is_not_no_literature(self):
        self.get.side_effect = [json.dumps({"esearchresult": {"count": "0", "idlist": [], "querytranslation": ""}})]
        text = run(m.mesh, "zzzqqx")
        self.assertIn("无描述符不等于主题无文献", text)
        self.assertEqual(self.get.await_count, 1)

    def test_child_failure_degrades_to_uid(self):
        self.responses(child_error=True)
        text = run(m.mesh, "tooth resorption")
        self.assertIn("下位词名称获取失败", text)
        self.assertIn("UID 68012391", text)
        self.assertIn("D014091", text)

    def test_retmax_validation_and_clamp(self):
        with self.assertRaisesRegex(m.NcbiError, "正数"):
            run(m.mesh, "tooth", retmax=0)
        self.get.assert_not_awaited()
        self.responses()
        text = run(m.mesh, "tooth resorption", retmax=50)
        self.assertIn("已夹到 20", text)
        self.assertEqual(self.get.await_args_list[0].args[1]["retmax"], 20)

    def test_uid_mismatch_and_empty_term(self):
        with self.assertRaisesRegex(m.NcbiError, "term 不能为空"):
            run(m.mesh, "  ")
        self.get.side_effect = [
            json.dumps({"esearchresult": {"count": "1", "idlist": ["68014091"], "querytranslation": "x"}}),
            json.dumps({"result": {"uids": ["999"], "999": self.desc}})]
        with self.assertRaisesRegex(m.NcbiError, "UID 列表与请求不符"):
            run(m.mesh, "tooth resorption")


class Diff(unittest.TestCase):
    def setUp(self):
        self.esearch = AsyncMock()
        p = patch.object(m, "_esearch", self.esearch)
        p.start()
        self.addCleanup(p.stop)

    def two_queries(self, a_ids, b_ids, a_count=None, b_count=None):
        a_count = len(a_ids) if a_count is None else a_count
        b_count = len(b_ids) if b_count is None else b_count
        def fake(term, retmax=20, **kw):
            if term == "a":
                return esr(a_count, a_ids[:retmax], term="a", retstart=kw.get("retstart", 0))
            return esr(b_count, b_ids[:retmax], term="b", retstart=kw.get("retstart", 0))
        self.esearch.side_effect = fake

    def test_added_removed_common(self):
        self.two_queries(["1", "2", "3"], ["2", "3", "4"])
        text = run(m.diff, "a", "b")
        self.assertIn("新增 1 条", text)
        self.assertIn("丢失 1 条", text)
        self.assertIn("共同 2 条", text)
        self.assertIn("不判断相关性", text)
        data = json.loads(run(m.diff, "a", "b", as_json=True))
        self.assertEqual(data["added"], ["4"])
        self.assertEqual(data["removed"], ["1"])
        self.assertEqual(data["common_count"], 2)

    def test_identical_queries_zero_diff(self):
        self.two_queries(["1", "2"], ["1", "2"])
        data = json.loads(run(m.diff, "a", "b", as_json=True))
        self.assertEqual((data["added_count"], data["removed_count"]), (0, 0))

    def test_window_exceeded_advises_export(self):
        self.two_queries(["1"], ["2"], a_count=10000)
        with self.assertRaisesRegex(m.NcbiError, "export"):
            run(m.diff, "a", "b")

    def test_max_show_zero_and_negative(self):
        self.two_queries(["1", "2", "3"], ["2", "3", "4"])
        text = run(m.diff, "a", "b", max_show=0)
        self.assertNotIn("新增（展示", text)
        with self.assertRaisesRegex(m.NcbiError, "不能为负"):
            run(m.diff, "a", "b", max_show=-1)

    def test_paging_drift_is_reported(self):
        self.two_queries(["1", "2", "2"], ["2", "3", "4"], a_count=3)
        text = run(m.diff, "a", "b")
        self.assertIn("重复 PMID", text)


class Export(TempDir):
    def setUp(self):
        super().setUp()
        self.esearch = AsyncMock()
        self.esummary = AsyncMock(return_value=[])
        self.get = AsyncMock()
        for name, mock in (("_esearch", self.esearch), ("_esummary_dicts", self.esummary), ("_get_async", self.get)):
            p = patch.object(m, name, mock)
            p.start()
            self.addCleanup(p.stop)

    def small_fake(self, ids, end_count=None):
        state = {"counts": 0}
        def fake(term, retmax=20, **kw):
            if retmax == 0:
                state["counts"] += 1
                c = len(ids) if end_count is None or state["counts"] == 1 else end_count
                return esr(c, [], term=term)
            return esr(len(ids), ids[kw.get("retstart", 0):kw.get("retstart", 0) + retmax],
                       term=term, retstart=kw.get("retstart", 0))
        self.esearch.side_effect = fake

    def test_pmids_small_writes_file_and_sha(self):
        self.small_fake(["1", "2", "3"])
        p = self.path("out.txt")
        text = run(m.export, "tooth", p)
        self.assertIn("导出完成", text)
        self.assertIn("去重后 3 个", text)
        self.assertEqual(Path(p).read_text(encoding="utf-8"), "1\n2\n3\n")
        import hashlib
        self.assertIn(hashlib.sha256(b"1\n2\n3\n").hexdigest(), text)
        data = json.loads(run(m.export, "tooth", self.path("out2.txt"), as_json=True))
        self.assertEqual(data["unique"], 3)
        self.assertEqual(data["slices"], 1)

    def test_empty_result_writes_empty_file(self):
        self.small_fake([])
        p = self.path("empty.txt")
        run(m.export, "tooth", p)
        self.assertEqual(Path(p).read_text(encoding="utf-8"), "")

    def test_existing_file_and_bad_format_fail_before_network(self):
        p = self.path("exists.txt")
        Path(p).write_text("old", encoding="utf-8")
        with self.assertRaisesRegex(m.NcbiError, "文件已存在"):
            run(m.export, "tooth", p)
        with self.assertRaisesRegex(m.NcbiError, "format"):
            run(m.export, "tooth", self.path("x.txt"), format="bogus")
        self.esearch.assert_not_awaited()

    def test_end_count_drift_reported(self):
        self.small_fake(["1", "2", "3"], end_count=4)
        text = run(m.export, "tooth", self.path("drift.txt"))
        self.assertIn("结束重数 4 ≠ 起始 3", text)

    def test_csv_rows_and_missing_note(self):
        self.small_fake(["1", "2"])
        self.esummary.return_value = [("1", {"title": "Title one", "pubdate": "2026", "source": "J",
                                             "authors": [{"name": "A"}], "pubtype": ["Journal Article"],
                                             "articleids": [{"idtype": "doi", "value": "10.x/y"}]})]
        p = self.path("out.csv")
        text = run(m.export, "tooth", p, format="csv")
        self.assertIn("元数据缺失/不可用", text)
        with open(p, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0][0], "pmid")
        self.assertEqual(rows[1][0], "1")
        self.assertEqual(rows[1][2], "Title one")
        self.assertEqual(rows[2][0], "2")
        self.assertEqual(rows[2][-1], "元数据缺失")

    def test_medline_writes_and_validates(self):
        self.small_fake(["1", "2"])
        self.get.return_value = "PMID- 1\nTI  - Title one\n\nPMID- 2\nTI  - Title two\n"
        p = self.path("out.nbib")
        run(m.export, "tooth", p, format="medline")
        body = Path(p).read_text(encoding="utf-8")
        self.assertIn("PMID- 1", body)
        self.assertIn("PMID- 2", body)
        self.get.return_value = "<!DOCTYPE html><html>error</html>"
        with self.assertRaisesRegex(m.NcbiError, "HTML"):
            run(m.export, "tooth", self.path("bad.nbib"), format="medline")
        self.get.return_value = "PMID- 999\nTI  - Wrong\n"
        with self.assertRaisesRegex(m.NcbiError, "与请求不符"):
            run(m.export, "tooth", self.path("bad2.nbib"), format="medline")

    def test_failure_cleans_part_file(self):
        self.small_fake(["1", "2"])
        self.esummary.side_effect = m.NcbiError("metadata outage")
        p = self.path("fail.csv")
        with self.assertRaisesRegex(m.NcbiError, "metadata outage"):
            run(m.export, "tooth", p, format="csv")
        self.assertFalse(os.path.exists(p))
        self.assertFalse(os.path.exists(p + ".part"))

    def test_slicing_over_window(self):
        # 本项目自订测试数据：6000 条落在 2019、4001 条落在 2020，合计 10001 > 9999 窗口。
        records = {str(i): "2019-06-01" for i in range(1, 6001)}
        records.update({str(i): "2020-06-01" for i in range(6001, 10002)})
        def fake(term, retmax=20, **kw):
            lo = kw.get("mindate") or "0001-01-01"
            hi = kw.get("maxdate") or "2999-12-31"
            matching = [p for p, d in records.items() if lo <= d <= hi]
            if retmax == 0:
                return esr(len(matching), [], term=term)
            rs = kw.get("retstart", 0)
            return esr(len(matching), matching[rs:rs + retmax], term=term, retstart=rs)
        self.esearch.side_effect = fake
        p = self.path("big.txt")
        data = json.loads(run(m.export, "tooth", p, as_json=True))
        self.assertEqual(data["unique"], 10001)
        self.assertGreaterEqual(data["slices"], 2)
        self.assertEqual(data["count_start"], 10001)
        self.assertFalse(any("≠" in w for w in data["warnings"]))
        self.assertEqual(len(Path(p).read_text(encoding="utf-8").splitlines()), 10001)

    def test_slice_sum_mismatch_reported(self):
        records = {str(i): "2019-06-01" for i in range(1, 6001)}
        records.update({str(i): "2020-06-01" for i in range(6001, 10002)})
        def fake(term, retmax=20, **kw):
            lo = kw.get("mindate") or "0001-01-01"
            hi = kw.get("maxdate") or "2999-12-31"
            matching = [p for p, d in records.items() if lo <= d <= hi]
            if retmax == 0:
                # 无日期限定的总数比切片和多 1（模拟无发表日期的记录）〔本项目自订测试输入〕
                phantom = 1 if not (kw.get("mindate") or kw.get("maxdate")) else 0
                return esr(len(matching) + phantom, [], term=term)
            rs = kw.get("retstart", 0)
            return esr(len(matching), matching[rs:rs + retmax], term=term, retstart=rs)
        self.esearch.side_effect = fake
        data = json.loads(run(m.export, "tooth", self.path("mismatch.txt"), as_json=True))
        self.assertTrue(any("切片计数和" in w for w in data["warnings"]))
        self.assertTrue(any("实际取回" in w for w in data["warnings"]))

    def test_export_max_cap(self):
        self.esearch.return_value = esr(m.EXPORT_MAX + 1, [])
        with self.assertRaisesRegex(m.NcbiError, "安全上限"):
            run(m.export, "tooth", self.path("toobig.txt"))

    def test_sdk_schema_and_invalid_format(self):
        tools = run(m.mcp.list_tools)
        tool = next(t for t in tools if t.name == "export")
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema")
        self.assertEqual(schema["properties"]["format"]["enum"], ["pmids", "csv", "medline"])
        with self.assertRaises(Exception):
            run(m.mcp.call_tool, "export", {"term": "tooth", "path": "x.txt", "format": "invalid"})
        self.esearch.assert_not_awaited()


class Audit(TempDir):
    def setUp(self):
        super().setUp()
        self.esearch = AsyncMock(return_value=esr(7, []))
        p = patch.object(m, "_esearch", self.esearch)
        p.start()
        self.addCleanup(p.stop)

    def test_off_by_default_and_on_appends_jsonl(self):
        with patch.dict(os.environ, {"PUBMED_AUDIT_DIR": ""}):
            run(m.count, "tooth")
        log = self.path("searches.jsonl")
        self.assertFalse(os.path.exists(log))
        with patch.dict(os.environ, {"PUBMED_AUDIT_DIR": self.dir}):
            run(m.count, "tooth")
            run(m.search, "tooth", retmax=0)
        lines = Path(log).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        rec0, rec1 = json.loads(lines[0]), json.loads(lines[1])
        self.assertEqual(rec0["event"], "count")
        self.assertEqual(rec0["count"], 7)
        self.assertEqual(rec1["event"], "search")
        for rec in (rec0, rec1):
            self.assertIn("ts", rec)
            self.assertIn("version", rec)
            self.assertEqual(rec["term"], "tooth")

    def test_write_failure_does_not_break_tool(self):
        blocker = self.path("blocker")
        Path(blocker).write_text("i am a file, not a dir", encoding="utf-8")
        err = io.StringIO()
        with patch.dict(os.environ, {"PUBMED_AUDIT_DIR": blocker}):
            with contextlib.redirect_stderr(err):
                text = run(m.count, "tooth")
        self.assertIn("命中 7 条", text)
        self.assertIn("检索留痕写入失败", err.getvalue())


if __name__ == "__main__":
    unittest.main()

"""导出可靠性回归；所有 PMID、计数、日期和夹具值均为本项目自订测试输入。"""
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import anyio

ROOT = Path(__file__).resolve().parents[1]  # 本项目自订：仓库路径层级
spec = importlib.util.spec_from_file_location("integrity_subject", ROOT / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def es(ids, count=None):
    return {"_term": "tooth", "count": str(len(ids) if count is None else count),
            "idlist": ids, "querytranslation": '"tooth"[All Fields]'}


class ExportIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return str(Path(self.tmp.name) / name)

    def export(self, name, fmt="pmids"):
        async def call():
            return json.loads(await m.export("tooth", self.path(name), format=fmt, as_json=True))
        return anyio.run(call)

    def test_pmids_counts_and_audit_match(self):
        ids = ["1", "2"]  # 本项目自订：请求夹具
        with patch.object(m, "_esearch", AsyncMock(return_value=es(ids))), patch.object(m, "_audit") as audit:
            result = self.export("ids.txt")
        self.assertEqual(result["written_records"], len(ids))
        self.assertEqual(Path(self.path("ids.txt")).read_text().splitlines(), ids)
        self.assertEqual(result["missing_pmids"], [])
        self.assertEqual(result["integrity_status"], "no_discrepancy_detected")
        self.assertEqual(result["integrity_issues"], [])
        for key in ("written_records", "missing_pmids", "integrity_status", "integrity_issues"):
            self.assertEqual(audit.call_args.kwargs[key], result[key])

    def test_empty_csv_has_header_and_accurate_warning(self):
        with patch.object(m, "_esearch", AsyncMock(return_value=es([]))), \
                patch.object(m, "_esummary_dicts", AsyncMock(return_value=[])):
            result = self.export("empty.csv", "csv")
        with open(self.path("empty.csv"), encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 1)  # 本项目自订：只有表头
        self.assertEqual(result["written_records"], 0)  # 本项目自订：不含表头
        self.assertTrue(any("仅含表头" in w for w in result["warnings"]))
        self.assertFalse(any("已写出空文件" in w for w in result["warnings"]))

    def test_csv_placeholder_rows_and_missing_metadata(self):
        ids = ["1", "2", "3"]  # 本项目自订：正常、缺失、不可用
        summaries = [(ids[0], {"title": "Title", "pubtype": []}),  # 本项目自订：首个正常题录
                     (ids[-1], {"error": "unavailable"})]  # 本项目自订：末个不可用
        with patch.object(m, "_esearch", AsyncMock(return_value=es(ids))), \
                patch.object(m, "_esummary_dicts", AsyncMock(return_value=summaries)):
            result = self.export("partial.csv", "csv")
        with open(self.path("partial.csv"), encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), result["written_records"])
        self.assertEqual(result["written_records"], len(ids))
        self.assertEqual(result["missing_pmids"], ids[1:])  # 本项目自订：后两个号缺元数据
        self.assertEqual(result["integrity_status"], "missing_records")

    def test_ris_missing_list_is_not_truncated(self):
        ids = [str(i) for i in range(1, 14)]  # 本项目自订：超过文本警告展示阈值的夹具
        records = [{"pmid": ids[0], "title": "Title", "authors": [], "journal": {},  # 本项目自订：只返回首条
                    "pubdate": "", "lang": [], "ids": {}, "abstract": [], "mesh": [], "pt": [], "comments": []}]
        with patch.object(m, "_esearch", AsyncMock(return_value=es(ids))), \
                patch.object(m, "_efetch_xml_all", AsyncMock(return_value=records)):
            result = self.export("partial.ris", "ris")
        self.assertEqual(result["written_records"], len(records))
        self.assertEqual(result["missing_pmids"], ids[1:])  # 本项目自订：完整缺失名单
        self.assertEqual(Path(self.path("partial.ris")).read_text().count("ER  -"), len(records))
        self.assertEqual(result["integrity_status"], "missing_records")
        self.assertIn("missing_records", result["integrity_issues"])

    def test_all_ris_records_missing_keeps_id_and_written_counts_distinct(self):
        ids = ["1"]  # 本项目自订：检回号但题录全部缺失
        with patch.object(m, "_esearch", AsyncMock(return_value=es(ids))), \
                patch.object(m, "_efetch_xml_all", AsyncMock(return_value=[])):
            result = self.export("missing.ris", "ris")
        self.assertEqual(result["unique"], len(ids))
        self.assertEqual(result["written_records"], 0)  # 本项目自订：没有实际题录
        self.assertEqual(Path(self.path("missing.ris")).read_bytes(), b"")
        self.assertEqual(result["missing_pmids"], ids)
        self.assertEqual(result["integrity_status"], "missing_records")

    def test_medline_missing_record_count(self):
        ids = ["1", "2"]  # 本项目自订：题录夹具
        payload = "PMID- 1\nTI  - Title\n"  # 本项目自订：只返回首条
        with patch.object(m, "_esearch", AsyncMock(return_value=es(ids))), \
                patch.object(m, "_get_async", AsyncMock(return_value=payload)):
            result = self.export("partial.nbib", "medline")
        self.assertEqual(result["written_records"], 1)  # 本项目自订：实际题录数
        self.assertEqual(result["missing_pmids"], ids[1:])  # 本项目自订：缺少末条
        self.assertEqual(result["integrity_status"], "missing_records")

    def test_count_change_requires_review(self):
        ids = ["1"]  # 本项目自订：取回集合
        replies = [es(ids), es(ids), es(ids, count=2)]  # 本项目自订：结束计数变化
        with patch.object(m, "_esearch", AsyncMock(side_effect=replies)):
            result = self.export("drift.txt")
        self.assertEqual(result["integrity_status"], "needs_review")
        self.assertIn("count_changed", result["integrity_issues"])
        self.assertEqual(result["missing_pmids"], [])

    def test_overlap_requires_review_without_claiming_missing_records(self):
        ids = ["1", "2"]  # 本项目自订：跨片重复夹具
        with patch.object(m, "WINDOW", 1), patch.object(m, "_esearch", AsyncMock(return_value=es(ids))), \
                patch.object(m, "_export_slice", AsyncMock(return_value=(ids + ids, 2, 4))):  # 本项目自订：窗口、片数与重叠计数
            result = self.export("overlap.txt")
        self.assertEqual(result["written_records"], len(ids))
        self.assertEqual(result["integrity_status"], "needs_review")
        self.assertEqual(result["missing_pmids"], [])
        self.assertIn("slice_overlap_or_count_mismatch", result["integrity_issues"])
        self.assertNotIn("id_count_mismatch", result["integrity_issues"])

    def test_id_count_mismatch_and_pagination_drift(self):
        ids = ["1", "2"]  # 本项目自订：预期集合
        with patch.object(m, "_esearch", AsyncMock(return_value=es(ids))), \
                patch.object(m, "_ids_in_window", AsyncMock(return_value=(ids[:1], len(ids), es(ids), "drift"))):  # 本项目自订：少取首条以外记录
            result = self.export("short.txt")
        self.assertEqual(result["integrity_status"], "needs_review")
        self.assertIn("id_count_mismatch", result["integrity_issues"])
        self.assertIn("pagination_drift", result["integrity_issues"])
        self.assertEqual(result["missing_pmids"], [])  # 未检回号未知，不能编造名单。

    def test_full_missing_lists_with_drift_and_audit_across_formats(self):
        ids = [str(i) for i in range(1, 14)]  # 本项目自订：缺失名单超过文本展示上限
        for fmt in ("csv", "ris", "medline"):
            with self.subTest(format=fmt), \
                    patch.object(m, "_esearch", AsyncMock(side_effect=[es(ids), es(ids), es(ids, count=14)])), \
                    patch.object(m, "_esummary_dicts", AsyncMock(return_value=[])), \
                    patch.object(m, "_efetch_xml_all", AsyncMock(return_value=[])), \
                    patch.object(m, "_get_async", AsyncMock(return_value="1. ")), \
                    patch.object(m, "_audit") as audit:  # 上述计数与占位正文均为本项目自订夹具
                result = self.export("combined." + fmt, fmt)
                self.assertEqual(result["missing_pmids"], ids)
                self.assertEqual(result["integrity_status"], "missing_records")
                self.assertIn("count_changed", result["integrity_issues"])
                self.assertIn("missing_records", result["integrity_issues"])
                expected = len(ids) if fmt == "csv" else 0  # 本项目自订：CSV 保留占位行
                self.assertEqual(result["written_records"], expected)
                warning = next(w for w in result["warnings"] if "没取到记录" in w or "元数据缺失/不可用" in w)
                self.assertTrue(warning.endswith(", ".join(ids[:10]) + "…"))  # 本项目自订：文本展示上限
                for key in ("written_records", "missing_pmids", "integrity_status", "integrity_issues"):
                    self.assertEqual(audit.call_args.kwargs[key], result[key])

    def test_context_restored_on_success_error_and_cancellation(self):
        async def call():
            outer = []
            token = m._REQ_CTR.set(outer)
            try:
                with patch.object(m, "_esearch", AsyncMock(return_value=es([]))):
                    await m.export("tooth", self.path("ok.txt"))
                self.assertIs(m._REQ_CTR.get(), outer)
                with patch.object(m, "_esearch", AsyncMock(side_effect=m.NcbiError("network failure"))):
                    with self.assertRaisesRegex(m.NcbiError, "network failure"):
                        await m.export("tooth", self.path("error.txt"))
                self.assertIs(m._REQ_CTR.get(), outer)
                async def cancelled(*args, **kwargs):
                    scope.cancel()
                    await anyio.lowlevel.checkpoint()
                with anyio.CancelScope() as scope:
                    with patch.object(m, "_esearch", cancelled):
                        await m.export("tooth", self.path("cancelled.txt"))
                self.assertIs(m._REQ_CTR.get(), outer)
                self.assertEqual(outer, [])
            finally:
                m._REQ_CTR.reset(token)
        anyio.run(call)


if __name__ == "__main__":
    unittest.main()

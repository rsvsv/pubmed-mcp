"""检索可靠性离线回归；PMID、日期、计数均为本项目自订测试夹具。

真实 _esearch 只接收模拟传输响应；默认传输禁止联网。
导出文件仅写入 TemporaryDirectory，审计默认禁用并在需要时捕获调用。
"""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import anyio

ROOT = Path(__file__).resolve().parents[1]  # 本项目自订：测试文件到仓库根目录的层级
spec = importlib.util.spec_from_file_location("retrieval_reliability_subject", ROOT / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


def response(count, ids, marker="", **extra):
    data = {"count": str(count), "idlist": list(ids), "querytranslation": "tooth", **extra}
    if marker:
        data["warninglist"] = {"outputmessages": [marker]}
    return data


def wire(data):
    return json.dumps({"esearchresult": data})


class RetrievalReliability(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for blocker in (
            patch.dict(os.environ, {"PUBMED_AUDIT_DIR": ""}),
            patch.object(m, "_get_async", AsyncMock(side_effect=AssertionError("unexpected transport"))),
            patch.object(m, "_get", side_effect=AssertionError("network forbidden")),
        ):
            blocker.start()
            self.addCleanup(blocker.stop)

    def search_payload(self, payload, **kwargs):
        with patch.object(m, "_get_async", AsyncMock(return_value=json.dumps(payload))):
            return run(m._esearch, "tooth", **kwargs)

    def export_responses(self, replies, name="ids.txt", as_json=True, metadata=False):
        transport = AsyncMock(side_effect=[wire(item) for item in replies])
        with patch.object(m, "_get_async", transport), patch.object(m, "_audit") as audit:
            output = run(m.export, "tooth", str(self.root / name), as_json=as_json, metadata=metadata)
        self.assertEqual(transport.await_count, len(replies))
        return (json.loads(output) if as_json else output), audit

    def test_malformed_envelopes_raise_ncbi_error(self):
        for payload in ([], None, {}, {"esearchresult": None}, {"esearchresult": []}, {"esearchresult": "bad"}):
            with self.subTest(payload=payload), self.assertRaises(m.NcbiError):
                self.search_payload(payload)

    def test_idlist_requires_unique_canonical_string_pmids(self):
        malformed = (
            None, "12", {"1": True}, [1], [True], [None], [[]],  # 本项目自订：非列表或非字符串元素
            [""], ["0"], ["01"], ["-1"], ["+1"], [" 1"], ["1 "],  # 本项目自订：非规范 PMID
            ["１"], ["١"], ["1\n"], ["1", "1"],  # 本项目自订：Unicode、换行与页内重复
        )
        for ids in malformed:
            data = {"count": "2", "idlist": ids, "querytranslation": "tooth"}  # 本项目自订：足够容纳重复夹具的计数
            with self.subTest(ids=ids), self.assertRaises(m.NcbiError):
                self.search_payload({"esearchresult": data})

    def test_missing_idlist_in_positive_window_is_rejected(self):
        with self.assertRaises(m.NcbiError):
            self.search_payload({"esearchresult": {"count": "1", "querytranslation": "tooth"}})  # 本项目自订：正命中但缺页数据

    def test_bad_counts_raise_ncbi_error_not_raw_conversion_errors(self):
        for value in (None, True, -1, 1.5, "-1", "1.0", "²", "１", "", "9" * 5000):  # 本项目自订：非整数及超长转换夹具
            with self.subTest(value=repr(value)[:40]), self.assertRaises(m.NcbiError):  # 本项目自订：子测试标签截断长度
                self.search_payload({"esearchresult": {"count": value, "idlist": []}})

    def test_count_and_requested_window_control_page_length(self):
        cases = [
            (response(1, []), {}),  # 本项目自订：正命中空页
            (response(3, ["1"]), {"retmax": 2}),  # 本项目自订：短页
            (response(3, ["1", "2", "3"]), {"retmax": 2}),  # 本项目自订：超过请求页长
            (response(1, ["1", "2"]), {}),  # 本项目自订：超过响应计数
            (response(0, ["1"]), {}),  # 本项目自订：零计数却含 PMID
            (response(2, ["1"]), {"retmax": 0}),  # 本项目自订：计数请求夹带 PMID
            (response(2, ["1"]), {"retstart": 3}),  # 本项目自订：越尾窗口却含 PMID
        ]
        for data, kwargs in cases:
            with self.subTest(data=data, kwargs=kwargs), self.assertRaises(m.NcbiError):
                self.search_payload({"esearchresult": data}, **kwargs)

    def test_legitimate_empty_and_partial_last_pages_remain_legal(self):
        cases = [
            (response(3, []), {"retmax": 0}),  # 本项目自订：只计数
            (response(0, []), {}),  # 本项目自订：无命中
            ({"count": "0"}, {}),  # 本项目自订：兼容旧零命中夹具省略 idlist
            (response(2, [], retstart="3", retmax="0"), {"retstart": 3}),  # 本项目自订：合法越尾、服务端回显空页长度
            (response(2, [], retstart="2", retmax="0"), {"retstart": 2}),  # 本项目自订：刚好到末尾
            (response(2, ["2"], retstart="1", retmax="1"), {"retstart": 1, "retmax": 4}),  # 本项目自订：末页按实际长度回显
            (response(2, ["2"], retstart="1", retmax="4"), {"retstart": 1, "retmax": 4}),  # 本项目自订：末页按请求上限回显
            (response(2, ["1", "2"]), {"retmax": 2}),  # 本项目自订：兼容省略分页回显的模拟响应
        ]
        for data, kwargs in cases:
            with self.subTest(data=data, kwargs=kwargs):
                result = self.search_payload({"esearchresult": data}, **kwargs)
                self.assertEqual(result.get("idlist", []), data.get("idlist", []))

    def test_present_pagination_echoes_must_match_request(self):
        echoes = (
            {"retstart": "1"}, {"retstart": True}, {"retstart": "-1"},  # 本项目自订：错误首页偏移及类型
            {"retmax": "0"}, {"retmax": "7"}, {"retmax": "²"},  # 本项目自订：错误页长及非 ASCII 整数
        )
        for extra in echoes:
            data = response(2, ["1", "2"], **extra)  # 本项目自订：完整页
            with self.subTest(extra=extra), self.assertRaises(m.NcbiError):
                self.search_payload({"esearchresult": data}, retmax=2)  # 本项目自订：请求页长

    def test_effective_cap_and_window_edge_are_validated(self):
        with patch.object(m, "RETMAX_CAP", 2), patch.object(m, "WINDOW", 3):  # 本项目自订：缩小页长和检索窗口
            data = response(9, ["3"], retstart="2", retmax="1")  # 本项目自订：窗口边界只容纳末条
            result = self.search_payload({"esearchresult": data}, retstart=2, retmax=99)  # 本项目自订：请求超出工具页长上限
            self.assertEqual(result["_request_params"]["retmax"], 2)  # 本项目自订：有效请求页长
            with self.assertRaises(m.NcbiError):
                self.search_payload({"esearchresult": response(9, ["2", "3"])}, retstart=2, retmax=99)  # 本项目自订：超过剩余窗口

    def test_positive_empty_first_page_does_not_trigger_another_request(self):
        fake = AsyncMock(return_value=response(1, []))  # 本项目自订：绕过 _esearch 的旧式 mock 也不能静默修补空首页
        with patch.object(m, "_esearch", fake), self.assertRaises(m.NcbiError):
            run(m._ids_in_window, "tooth")
        fake.assert_awaited_once()

    def test_all_page_warnings_and_transient_counts_reach_json_metadata_audit(self):
        replies = [
            response(5, [], "initial-probe"),  # 本项目自订：起始计数
            response(5, ["1", "2"], "first-page"),  # 本项目自订：首页
            response(6, ["3", "4"], "middle-page"),  # 本项目自订：中途计数上升
            response(5, ["5"], "last-page"),  # 本项目自订：末页计数恢复
            response(5, [], "final-probe"),  # 本项目自订：结束计数与起始相同
        ]
        with patch.object(m, "RETMAX_CAP", 2):  # 本项目自订：强制产生分页
            result, audit = self.export_responses(replies, metadata=True)
        self.assertEqual(result["integrity_status"], "needs_review")
        self.assertIn("count_changed", result["integrity_issues"])
        self.assertIn("pagination_drift", result["integrity_issues"])
        self.assertEqual(result["count_start"], result["count_end"])
        self.assertEqual(result["unique"], result["count_start"])
        self.assertEqual([check["count"] for check in result["retrieval_checks"]], [5, 5, 6, 5, 5])  # 本项目自订：完整观测序列
        for marker in ("initial-probe", "first-page", "middle-page", "last-page", "final-probe"):
            self.assertTrue(any(marker in text for text in result["warnings"]), marker)
        self.assertTrue(any("5 → 6" in text for text in result["warnings"]))  # 本项目自订：上升观测
        self.assertTrue(any("6 → 5" in text for text in result["warnings"]))  # 本项目自订：恢复观测不能覆盖上升证据
        self.assertEqual((self.root / "ids.txt").read_text(encoding="utf-8").splitlines(), ["1", "2", "3", "4", "5"])  # 本项目自订：完整导出集合
        metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
        for key in ("warnings", "retrieval_checks", "integrity_issues"):
            self.assertEqual(metadata[key], result[key])
            self.assertEqual(audit.call_args.kwargs[key], result[key])

    def test_initial_probe_to_first_page_change_is_retained(self):
        replies = [
            response(2, [], "initial"),  # 本项目自订：最初计数
            response(1, ["1"], "first"),  # 本项目自订：取号前减少
            response(2, [], "final"),  # 本项目自订：结束时恢复
        ]
        result, _ = self.export_responses(replies)
        self.assertIn("count_changed", result["integrity_issues"])
        self.assertTrue(any("计数探测→取号首页" in text for text in result["warnings"]))

    def test_slice_probe_changes_do_not_cancel_each_other(self):
        replies = [
            response(3, [], "initial"),  # 本项目自订：起始计数
            response(3, [], "root-probe"),  # 本项目自订：需递归拆片的根计数
            response(1, [], "left-probe"),  # 本项目自订：左片计数探测
            response(2, ["1", "2"], "left-page"),  # 本项目自订：左片取号时增加
            response(2, [], "right-probe"),  # 本项目自订：右片计数探测
            response(1, ["3"], "right-page"),  # 本项目自订：右片取号时减少
            response(3, [], "final"),  # 本项目自订：全局首尾及累计计数一致
        ]
        with patch.object(m, "WINDOW", 2), patch.object(m, "RETMAX_CAP", 2), \
                patch.object(m, "NO_BEGIN", "2020-01-01"), patch.object(m, "NO_END", "2020-01-02"):  # 本项目自订：两日切片范围及窗口
            result, _ = self.export_responses(replies)
        self.assertEqual(result["unique"], result["count_start"])
        self.assertEqual(result["count_start"], result["count_end"])
        self.assertEqual(result["integrity_status"], "needs_review")
        self.assertIn("count_changed", result["integrity_issues"])
        self.assertEqual([check["count"] for check in result["retrieval_checks"]], [3, 3, 1, 2, 2, 1, 3])  # 本项目自订：不可抵消的完整计数序列
        for marker in ("initial", "root-probe", "left-probe", "left-page", "right-probe", "right-page", "final"):
            self.assertTrue(any(marker in text for text in result["warnings"]), marker)

    def test_root_slice_probe_change_is_retained(self):
        replies = [
            response(3, [], "initial"),  # 本项目自订：起始计数超窗口
            response(2, [], "root-probe"),  # 本项目自订：根切片探测缩小至窗口内
            response(2, ["1", "2"], "root-page"),  # 本项目自订：根切片取号
            response(3, [], "final"),  # 本项目自订：结束计数恢复
        ]
        with patch.object(m, "WINDOW", 2):  # 本项目自订：强制进入切片分支
            result, _ = self.export_responses(replies)
        self.assertIn("count_changed", result["integrity_issues"])
        self.assertTrue(any("起始总数→根切片" in text for text in result["warnings"]))

    def test_empty_slice_and_parent_checks_are_not_discarded(self):
        replies = [
            response(3, [], "initial"),  # 本项目自订：起始计数
            response(3, [], "root-probe"),  # 本项目自订：内部节点
            response(0, [], "empty-left-probe"),  # 本项目自订：空叶片也包含证据
            response(2, [], "right-probe"),  # 本项目自订：非空叶片
            response(2, ["1", "2"], "right-page"),  # 本项目自订：取号页
            response(3, [], "final"),  # 本项目自订：全局计数不变
        ]
        with patch.object(m, "WINDOW", 2), \
                patch.object(m, "NO_BEGIN", "2020-01-01"), patch.object(m, "NO_END", "2020-01-02"):  # 本项目自订：两日范围及窗口
            result, _ = self.export_responses(replies)
        self.assertTrue(any("empty-left-probe" in text for text in result["warnings"]))
        self.assertTrue(any("子片计数和" in text for text in result["warnings"]))
        self.assertEqual([check["count"] for check in result["retrieval_checks"]], [3, 3, 0, 2, 2, 3])  # 本项目自订：包括空叶片和内部节点
        self.assertEqual(result["integrity_status"], "needs_review")

    def test_text_export_keeps_first_and_final_warnings(self):
        replies = [
            response(1, [], "initial-warning"),  # 本项目自订：起始探测
            response(1, ["1"], "page-warning"),  # 本项目自订：取号页
            response(1, [], "final-warning"),  # 本项目自订：结束探测
        ]
        text, _ = self.export_responses(replies, as_json=False)
        for marker in ("initial-warning", "page-warning", "final-warning"):
            self.assertIn(marker, text)

    def test_diff_later_page_warnings_reach_text_json_and_audit(self):
        async def transport(endpoint, params, timeout):
            start = int(params.get("retstart", 0))  # 本项目自订：首页偏移
            ids = ["1", "2"] if not start else ["3"]  # 本项目自订：分页 PMID
            return wire(response(3, ids, "later-warning" if start else ""))  # 本项目自订：总数

        for json_mode in (False, True):
            with patch.object(m, "RETMAX_CAP", 2), patch.object(m, "_get_async", transport), patch.object(m, "_audit") as audit:  # 本项目自订：分页大小
                result = run(m.diff, "tooth", "root", as_json=json_mode)
            self.assertIn("later-warning", result)
            for key in ("warnings_a", "warnings_b"):
                self.assertTrue(any("later-warning" in w for w in audit.call_args.kwargs[key]))
                if json_mode:
                    self.assertTrue(any("later-warning" in w for w in json.loads(result)[key]))

    def test_same_warning_on_multiple_pages_keeps_each_occurrence(self):
        replies = [
            response(1, [], "repeated-warning"),  # 本项目自订：起始探测
            response(1, ["1"], "repeated-warning"),  # 本项目自订：取号页
            response(1, [], "repeated-warning"),  # 本项目自订：结束探测
        ]
        result, _ = self.export_responses(replies)
        occurrences = [text for text in result["warnings"] if "repeated-warning" in text]
        self.assertEqual(len(occurrences), len(replies))
        self.assertEqual(len(set(occurrences)), len(replies))
        self.assertEqual(result["integrity_status"], "no_discrepancy_detected")


if __name__ == "__main__":
    unittest.main()

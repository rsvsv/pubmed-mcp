"""Output mode regressions. All fixture numbers/IDs are 本项目自订测试输入."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch, AsyncMock

import anyio

spec = importlib.util.spec_from_file_location("modes_subject", Path(__file__).resolve().parents[1] / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


class SearchModes(unittest.TestCase):
    def setUp(self):
        self.search = {"_term": "tooth", "count": "2", "idlist": ["22", "11"], "retstart": "0", "querytranslation": '"tooth"[All Fields]'}
        self.records = [(pid, {"title": f"Complete title {pid}", "pubdate": "2026", "source": "Journal", "volume": "9", "pages": "1-3", "authors": [{"name": "Author A"}], "articleids": [{"idtype": "doi", "value": "10.test/example"}], "pubtype": ["Journal Article", "Retracted Publication", "Published Erratum"]}) for pid in self.search["idlist"]]
        self.esearch = AsyncMock(return_value=self.search)
        self.esummary = AsyncMock(return_value=self.records)
        for name, mock in (("_esearch", self.esearch), ("_esummary_dicts", self.esummary)):
            patcher = patch.object(m, name, mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_default_matches_explicit_full(self):
        for json_mode in (False, True):
            self.assertEqual(run(m.search, "tooth", as_json=json_mode), run(m.search, "tooth", as_json=json_mode, detail="full"))
        data = json.loads(run(m.search, "tooth", as_json=True))
        self.assertNotIn("detail", data)
        self.assertEqual(data["records"][0], m._record_parts(*self.records[0]))

    def test_brief_keeps_titles_and_all_types(self):
        full = json.loads(run(m.search, "tooth", as_json=True))
        brief = json.loads(run(m.search, "tooth", as_json=True, detail="brief"))
        for a, b in zip(full["records"], brief["records"]):
            self.assertEqual(b, {k: a[k] for k in ("pmid", "pubdate", "title", "pubtype")})
        self.assertEqual(brief["returned"], full["returned"])
        self.assertTrue(brief["metadata_requested"])
        text = run(m.search, "tooth", detail="brief")
        self.assertIn("Retracted Publication/Published Erratum", text)
        self.assertIn("Complete title 22", text)
        self.assertNotIn("Author A", text)
        self.assertIn("省略不表示来源缺失", text)

    def test_ids_skips_metadata_preserves_order_and_counts(self):
        self.esummary.side_effect = AssertionError("ids must not fetch metadata")
        data = json.loads(run(m.search, "tooth", as_json=True, detail="ids"))
        self.assertEqual(data["records"], [{"pmid": "22"}, {"pmid": "11"}])
        self.assertEqual(data["returned"], 2)
        self.assertIsNone(data["esummary_missing"])
        self.assertFalse(data["metadata_requested"])
        self.assertIn("未检查撤稿", "\n".join(data["warnings"]))
        text = run(m.search, "tooth", detail="ids")
        self.assertTrue(text.endswith("22\n11"))
        self.esummary.assert_not_awaited()

    def test_all_modes_preserve_query_and_window_warnings(self):
        self.search.update(count="10001", retstart="9998", idlist=["22"], errorlist={"phrasesnotfound": ["zzzqqx"]})
        for detail in ("full", "brief", "ids"):
            for json_mode in (False, True):
                text = run(m.search, "tooth", detail=detail, as_json=json_mode)
                self.assertIn("phrasesnotfound", text)
                self.assertIn("9999", text)
                if json_mode:
                    data = json.loads(text)
                    self.assertEqual(data["querytranslation"], self.search["querytranslation"])
                    self.assertTrue(data["window_warning"])
                else:
                    self.assertIn("NCBI 实际执行", text)

    def test_brief_missing_metadata_is_not_hidden(self):
        self.esummary.return_value = self.records[:1]
        data = json.loads(run(m.search, "tooth", detail="brief", as_json=True))
        self.assertEqual(data["returned"], 1)
        self.assertEqual(data["esummary_missing"], 1)
        self.assertTrue(any("esummary" in w for w in data["warnings"]))
        self.assertIn("本次给 1 条", run(m.search, "tooth", detail="brief"))

    def test_zero_count_count_only_and_out_of_range_all_modes(self):
        cases = [({"count": "0", "idlist": []}, "未匹配记录"),
                 ({"count": "2", "idlist": [], "_retmax": 0}, "只要计数"),
                 ({"count": "2", "idlist": [], "retstart": "2"}, "越过了命中总数")]
        for extra, expected in cases:
            for detail in ("full", "brief", "ids"):
                self.esearch.return_value = {**self.search, **extra}
                self.esummary.return_value = []
                for json_mode in (False, True):
                    text = run(m.search, "tooth", detail=detail, as_json=json_mode)
                    self.assertIn(expected, text)
                    if json_mode:
                        self.assertEqual(json.loads(text)["returned"], 0)

    def test_invalid_detail_fails_before_network_direct_and_sdk(self):
        for value in ("", "summary", None, True):
            with self.subTest(value=value), self.assertRaisesRegex(m.NcbiError, "detail 只支持"):
                run(m.search, "tooth", detail=value)
            # SDK 层由 pydantic 先拦（英文原文，含字段名 detail 与允许值）。
            with self.subTest(sdk=value), self.assertRaisesRegex(m._SdkToolError, "detail"):
                run(m.mcp.call_tool, "search", {"term": "tooth", "detail": value})
        self.esearch.assert_not_awaited()

    def test_sdk_schema_and_modes(self):
        tools = run(m.mcp.list_tools)
        tool = next(t for t in tools if t.name == "search")
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema")
        self.assertEqual(schema["properties"]["detail"]["enum"], ["full", "brief", "ids"])
        self.assertEqual(schema["properties"]["detail"]["default"], "full")
        for detail in ("full", "brief", "ids"):
            run(m.mcp.call_tool, "search", {"term": "tooth", "detail": detail})

    def test_metadata_failure_still_raises_for_brief(self):
        self.esummary.side_effect = m.NcbiError("simulated metadata failure")
        with self.assertRaisesRegex(m.NcbiError, "metadata failure"):
            run(m.search, "tooth", detail="brief")


if __name__ == "__main__":
    unittest.main()

"""1.4.5 regressions for an external review of 1.4.4: unknown tool arguments rejected at the SDK wrapper,
thousands-separator PMIDs with spaces, fetch_abstracts cap, short-key masking, citation without volume,
ptag without display separator, export querytranslation line, MEDLINE placeholder, and a real MeSH
supplemental-record fixture (tests/fixtures/mesh_scr_biodentine.json, 实测 2026-11-13 NCBI responses).
Other literals are 本项目自订测试输入.
Run: python -I -m unittest discover -s <repo>/tests -v
"""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

import anyio

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("r145_subject", ROOT / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


def schema_of(tool):
    return getattr(tool, "inputSchema", None) or getattr(tool, "input_schema")


class UnknownArguments(unittest.TestCase):
    def test_every_tool_schema_forbids_extra(self):
        self.assertEqual(m._EXTRA_FORBID, "")
        tools = run(m.mcp.list_tools)
        self.assertEqual(len(tools), 8)
        for t in tools:
            with self.subTest(tool=t.name):
                self.assertIs(schema_of(t).get("additionalProperties"), False)

    def test_misspelled_argument_rejected_through_wrapper(self):
        # 走 SDK 包装层（与 --probe 的包装层断言同一通路）：拼错的 formt 必须报错并点名，且不发请求、不写文件。
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.pmids")
            with patch.object(m, "_esearch", AsyncMock(side_effect=AssertionError("network"))):
                with self.assertRaisesRegex(m._SdkToolError, "formt"):
                    run(m.mcp.call_tool, "export", {"term": "x", "path": p, "formt": "csv"})
            self.assertFalse(os.path.exists(p))
        for name, args in (("search", {"term": "x", "retmx": 5}), ("fetch_abstracts", {"pmids": "1", "field": "title"}),
                           ("count", {"term": "x", "extra": 1})):
            with self.subTest(tool=name), self.assertRaisesRegex(m._SdkToolError, list(args)[-1]):
                run(m.mcp.call_tool, name, args)


class Pmids(unittest.TestCase):
    def test_thousands_with_spaces_rejected(self):
        for s in ("1,234, 567", "1,234,567", " 12,345 , 9"):
            with self.subTest(s=s), self.assertRaisesRegex(m.NcbiError, "千位分隔符"):
                m._parse_pmids(s)
        self.assertEqual(m._parse_pmids("1,2345, 678")[0], ["1", "2345", "678"])   # 非三位分组：按列表读
        self.assertEqual(m._parse_pmids("12345, 678901")[0], ["12345", "678901"])

    def test_fetch_cap_before_network(self):
        ids = " ".join(str(i) for i in range(1, m.FETCH_MAX + 2))
        with patch.object(m, "_get_async", AsyncMock(side_effect=AssertionError("network"))):
            with self.assertRaisesRegex(m.NcbiError, f"最多 {m.FETCH_MAX}"):
                run(m.fetch_abstracts, ids)


class SmallBugs(unittest.TestCase):
    def test_short_key_masks_only_url_value(self):
        with patch.object(m, "api_key", return_value="f"):
            out = m._mask("efetch failed: GET ...?db=pubmed&api_key=f&id=1 (fail)")
        self.assertIn("api_key=***&id=1 (fail)", out)
        self.assertTrue(out.startswith("efetch failed"))
        with patch.object(m, "api_key", return_value="a1" * 18):   # 长 key 仍全局替换
            self.assertEqual(m._mask("x " + "a1" * 18 + " y"), "x *** y")

    def test_citation_without_volume(self):
        self.assertEqual(m._record_parts("1", {"pages": "123-30"})["citation"], "123-30")
        self.assertEqual(m._record_parts("1", {"volume": "9", "issue": "2", "pages": "1-3"})["citation"], "9(2):1-3")

    def test_ptag_has_no_separator_but_text_does(self):
        r = {"title": "t", "pubtype": ["Journal Article", "Review", "Retracted Publication"]}
        self.assertEqual(m._record_parts("1", r)["ptag"], "Review/Retracted Publication")
        self.assertIn("｜Review/Retracted Publication", m._fmt_record(r, "1"))
        self.assertEqual(m._fmt_record({"title": "t", "pubtype": ["Journal Article"]}, "1").splitlines()[0], "PMID 1｜?｜?")

    def test_int_has_no_unused_default(self):
        with self.assertRaises(TypeError):
            m._int("5", "retmax", 20)
        with self.assertRaisesRegex(m.NcbiError, "retmax"):
            m._int("", "retmax")
        self.assertEqual(m._int("5", "retmax", lo=0, hi=3), 3)

    def test_export_querytranslation_line(self):
        for qt, expect in (("", None), ("x" * 700, "截断")):
            with self.subTest(n=len(qt)), tempfile.TemporaryDirectory() as d:
                es = AsyncMock(return_value={"_term": "x", "count": "1", "idlist": ["1"], "retstart": "0",
                                             "querytranslation": qt})
                with patch.object(m, "_esearch", es):
                    text = run(m.export, "x", os.path.join(d, "a.pmids"))
                lines = [ln for ln in text.splitlines() if ln.startswith("NCBI 实际执行：")]
                if expect is None:
                    self.assertEqual(lines, [])
                else:
                    self.assertEqual(len(lines), 1)
                    self.assertIn(expect, lines[0])
                    self.assertLess(len(lines[0]), 700)

    def test_medline_placeholder_not_written(self):
        good = "PMID- 1\nTI  - t\n"
        async def fake(path, params, timeout, tries=4):
            return good if "1" in params["id"].split(",") else "1. "
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.nbib")
            ids = ["1"] + [str(i) for i in range(1000, 1000 + m.EFETCH_CHUNK)]   # 第二批全无效
            with patch.object(m, "_get_async", fake):
                n, miss = run(m._export_medline, p, ids)
            body = Path(p).read_text(encoding="utf-8")
        self.assertEqual(n, 1)
        self.assertEqual(body, good + "\n")
        self.assertNotIn("1. ", body)


class MeshRealFixture(unittest.TestCase):
    def test_supplemental_record_from_captured_responses(self):
        cap = json.loads((ROOT / "tests" / "fixtures" / "mesh_scr_biodentine.json").read_text(encoding="utf-8"))
        by_key = {(c["path"], c["params"].get("id") or c["params"].get("term")): c["body"] for c in cap["calls"]}

        async def fake(path, params, timeout, tries=4):
            return json.dumps(by_key[(path, params.get("id") or params.get("term"))])
        with patch.object(m, "_get_async", fake):
            data = json.loads(run(m.mesh, "Biodentine", as_json=True))
            text = run(m.mesh, "Biodentine")
        d0 = data["descriptors"][0]
        self.assertEqual((d0["ui"], d0["record_type"]), ("C506393", "supplemental-record"))
        self.assertEqual(d0["treenums"], [])
        self.assertEqual(d0["parents"], [])
        self.assertEqual([x["name"] for x in d0["mapped_to"]], ["Calcium Compounds", "Silicates"])
        self.assertIn("其中 2 条是补充概念", text)
        self.assertNotIn("@", text)


if __name__ == "__main__":
    unittest.main()

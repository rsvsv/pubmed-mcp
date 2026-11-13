"""1.4.2 fetch_abstracts structured mode / out_path / size hint regressions.
Fixtures tests/fixtures/efetch_sample.xml and efetch_sample_abstract.txt are 实测 2026-11-13 NCBI efetch
responses, trimmed (reference lists, affiliations, investigators removed) — see docs/abstract-slim.md.
Other literals are 本项目自订测试输入.
Run: python -I -m unittest discover -s <repo>/tests -v
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

import anyio

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("abs_subject", ROOT / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

FIX = ROOT / "tests" / "fixtures"
XML = (FIX / "efetch_sample.xml").read_text(encoding="utf-8")
TXT = (FIX / "efetch_sample_abstract.txt").read_text(encoding="utf-8")
ALL_IDS = ["41421800", "42759866", "28722854", "40272285", "33301246", "39606989", "42458410", "40934212"]
EMPTY_SET = ('<?xml version="1.0" ?>\n<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2025//EN" '
             '"https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_250101.dtd">\n<PubmedArticleSet></PubmedArticleSet>')


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


def subset(ids, body=XML):
    """按请求号裁剪 fixture，模拟 NCBI 只回所请求的记录。"""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(body)
    for node in list(root):
        if node.findtext(".//PMID") not in ids:
            root.remove(node)
    return ET.tostring(root, encoding="unicode")


def xml_fake(body=None):
    calls = []

    async def fake(path, params, timeout, tries=4):
        calls.append(params)
        assert params["rettype"] == "xml", params
        return body if body is not None else subset(params["id"].split(","))
    fake.calls = calls
    return fake


class Parse(unittest.TestCase):
    def setUp(self):
        with patch.object(m, "_get_async", xml_fake()):
            self.recs = {r["pmid"]: r for r in run(m._efetch_xml_all, ALL_IDS)}

    def test_all_records_parsed_in_request_order(self):
        with patch.object(m, "_get_async", xml_fake()):
            order = [r["pmid"] for r in run(m._efetch_xml_all, list(reversed(ALL_IDS)))]
        self.assertEqual(order, list(reversed(ALL_IDS)))

    def test_structured_sections(self):
        labels = [s["label"] for s in self.recs["41421800"]["abstract"]]
        self.assertEqual(labels, ["PURPOSE", "METHODS", "RESULTS", "CONCLUSIONS"])

    def test_unstructured_and_missing_abstract(self):
        self.assertEqual([s["label"] for s in self.recs["40272285"]["abstract"]], [""])
        self.assertEqual(self.recs["42759866"]["abstract"], [])

    def test_book(self):
        b = self.recs["28722854"]
        self.assertEqual(b["source"], "book")
        self.assertEqual(b["journal"]["book"], "StatPearls")
        self.assertTrue(b["title"])

    def test_retraction_without_pmid_kept_as_null(self):
        cc = {c["type"]: c["pmid"] for c in self.recs["40272285"]["comments"]}
        self.assertIn("RetractionIn", cc)
        self.assertIsNone(cc["RetractionIn"])
        self.assertEqual(cc["ExpressionOfConcernIn"], "40268367")
        self.assertIn("RetractionIn", [c["type"] for c in self.recs["40934212"]["comments"]])

    def test_collective_author_and_language(self):
        self.assertIn({"collective": "C4591001 Clinical Trial Group"}, self.recs["33301246"]["authors"])
        self.assertEqual(self.recs["39606989"]["lang"], ["chi"])
        self.assertTrue(self.recs["39606989"]["other_abstracts"])

    def test_article_number_when_no_pages(self):
        j = self.recs["42458410"]["journal"]
        self.assertEqual((j["pages"], j["article_number"]), ("", "1960"))

    def test_xml_abstract_matches_ncbi_text_byte_for_byte(self):
        # 同批文本档逐字比对（去空白）：解析层不得改动摘要文字。
        flat = re.sub(r"\s+", "", TXT)
        for pmid in ("41421800", "40272285", "39606989", "42458410"):
            for s in self.recs[pmid]["abstract"]:
                with self.subTest(pmid=pmid, label=s["label"]):
                    self.assertIn(re.sub(r"\s+", "", s["text"]), flat)


class Failures(unittest.TestCase):
    def test_html_parse_root_and_foreign_ids_fail_closed(self):
        for body, word in (("<!DOCTYPE html><html>gateway</html>", "HTML"), ("<PubmedArticleSet><Pub", "无法解析"),
                           ("<eSearchResult/>", "根元素"), (XML, "不符")):
            with self.subTest(word=word), patch.object(m, "_get_async", xml_fake(body)):
                with self.assertRaisesRegex(m.NcbiError, word):
                    run(m._efetch_xml_all, ["41421800"] if word != "不符" else ["1"])

    def test_empty_set_is_missing_not_error(self):
        with patch.object(m, "_get_async", xml_fake(EMPTY_SET)):
            out = json.loads(run(m.fetch_abstracts, "9999999999", as_json=True))
        self.assertEqual((out["returned"], out["missing"]), (0, ["9999999999"]))

    def test_bad_fields_fail_before_network(self):
        with patch.object(m, "_get_async", AsyncMock(side_effect=AssertionError("network"))):
            with self.assertRaisesRegex(m.NcbiError, "fields 不认识"):
                run(m.fetch_abstracts, "1", fields="title,affiliation")

    def test_existing_out_path_fails_before_network(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.txt")
            Path(p).write_text("old", encoding="utf-8")
            with patch.object(m, "_get_async", AsyncMock(side_effect=AssertionError("network"))):
                with self.assertRaisesRegex(m.NcbiError, "不覆盖"):
                    run(m.fetch_abstracts, "1", out_path=p)
            self.assertEqual(Path(p).read_text(encoding="utf-8"), "old")


class Tool(unittest.TestCase):
    def test_default_path_unchanged_and_uses_text(self):
        async def fake(path, params, timeout, tries=4):
            self.assertEqual(params["rettype"], "abstract")
            return TXT
        with patch.object(m, "_get_async", fake):
            out = run(m.fetch_abstracts, "41421800,40272285,39606989,42458410")
        self.assertEqual(out, "要 4 条 / 实取 4 条（分 1 批）\n\n" + TXT)

    def test_fields_projection_json(self):
        with patch.object(m, "_get_async", xml_fake()):
            out = json.loads(run(m.fetch_abstracts, ",".join(ALL_IDS), fields="title, abstract", as_json=True))
        self.assertEqual(out["fields"], ["title", "abstract"])
        self.assertEqual(set(out["records"][0]), {"pmid", "title", "abstract", "has_abstract"})
        self.assertEqual(out["no_abstract"], ["42759866"])
        book = [r for r in out["records"] if r["pmid"] == "28722854"][0]
        self.assertEqual(book["source"], "book")

    def test_default_json_fields_and_compact_authors(self):
        with patch.object(m, "_get_async", xml_fake()):
            out = json.loads(run(m.fetch_abstracts, "33301246", as_json=True))
        self.assertEqual(out["fields"], list(m.ABSTRACT_DEFAULT_FIELDS))
        self.assertIn("C4591001 Clinical Trial Group", out["records"][0]["authors"])
        self.assertTrue(all(isinstance(a, str) for a in out["records"][0]["authors"]))

    def test_compact_text_has_no_affiliations_and_flags_retraction(self):
        with patch.object(m, "_get_async", xml_fake()):
            out = run(m.fetch_abstracts, ",".join(ALL_IDS), fields="all")
        self.assertNotIn("Author information", out)
        self.assertIn("RetractionIn", out)
        self.assertIn("（此记录无摘要；有记录不等于有摘要）", out)
        self.assertIn("[PURPOSE]", out)
        self.assertIn("1960", out)   # 无页码 BMC 的文章号

    def test_out_path_writes_full_body_and_returns_summary(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "abs.json")
            with patch.object(m, "_get_async", xml_fake()):
                summary = json.loads(run(m.fetch_abstracts, ",".join(ALL_IDS), as_json=True, out_path=p))
            body = Path(p).read_bytes()
            import hashlib
            self.assertEqual(summary["sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual(len(json.loads(body)["records"]), 8)
            self.assertEqual(os.listdir(os.path.dirname(p)), ["abs.json"])   # 无 .part 残留

    def test_large_default_output_gets_hint_line(self):
        big = "1. x\n\nPMID: 41421800\n" + "a" * (m.FETCH_WARN_CHARS + 10)
        with patch.object(m, "_get_async", AsyncMock(return_value=big)):
            out = run(m.fetch_abstracts, "41421800")
        self.assertTrue(out.startswith("要 1 条 / 实取 1 条（分 1 批）\n\n" + big))
        self.assertIn("out_path", out.splitlines()[-1])

    def test_schema_new_params_optional(self):
        tool = [t for t in run(m.mcp.list_tools) if t.name == "fetch_abstracts"][0]
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema")   # mcp 1.x / 2.x 字段名
        props = schema["properties"]
        self.assertEqual(schema.get("required"), ["pmids"])
        self.assertEqual((props["fields"]["default"], props["as_json"]["default"], props["out_path"]["default"]),
                         ("", False, ""))


if __name__ == "__main__":
    unittest.main()

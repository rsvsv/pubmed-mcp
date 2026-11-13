"""1.4.4 small-bug regressions (audit P3): check_recall empty / uid-only term, MeSH supplemental records,
mesh retmax hint at cap, export zero explanation, per-call request counter, fetch_abstracts placeholder
fragment, links pubmed_pmc_refs gloss.
Behaviours were reproduced live 实测 2026-11-13 (see docs/p3-fixes.md); literals below are 本项目自订测试输入.
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
spec = importlib.util.spec_from_file_location("p3_subject", ROOT / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


def es(count, ids, qt):
    return {"_term": "x", "count": str(count), "idlist": ids, "retstart": "0", "querytranslation": qt}


class CheckRecall(unittest.TestCase):
    def test_empty_term_rejected_before_network(self):
        for term in ("", "   "):
            with self.subTest(term=term), patch.object(m, "_esearch", AsyncMock(side_effect=AssertionError("net"))):
                with self.assertRaisesRegex(m.NcbiError, "term 不能为空"):
                    run(m.check_recall, term, "42988646")

    def test_uid_only_term_not_reported_ignored(self):
        d = es(1, ["42988646"], "42988646[UID] AND 42988646[UID]")
        for term in ("42988646[uid]", "(42988646[uid] OR 1[pmid])"):
            with self.subTest(term=term), patch.object(m, "_esearch", AsyncMock(return_value=dict(d))):
                out = json.loads(run(m.check_recall, term, "42988646", as_json=True))
            self.assertFalse(out["term_ignored"])
            self.assertFalse(any("忽略" in w for w in out["warnings"]))

    def test_ignored_hint_mentions_chinese_only_for_cjk(self):
        d = es(1, ["1"], "1[UID]")
        with patch.object(m, "_esearch", AsyncMock(return_value=dict(d))):
            cjk = json.loads(run(m.check_recall, "牙根吸收", "1", as_json=True))
            latin = json.loads(run(m.check_recall, "zzz", "1", as_json=True))
        self.assertTrue(cjk["term_ignored"] and latin["term_ignored"])
        self.assertTrue(any("常见于中文词" in w for w in cjk["warnings"]))
        self.assertFalse(any("常见于中文词" in w for w in latin["warnings"]))


def mesh_fake(records, count=None, names=None):
    """records: {uid: esummary record}；names: 关联词 uid → 名称。"""
    names = names or {}

    async def fake(path, params, timeout, tries=4):
        if path == "esearch.fcgi":
            ids = list(records)[:params["retmax"]]
            return json.dumps({"esearchresult": {"count": str(count if count is not None else len(records)),
                                                 "idlist": ids, "querytranslation": "x[All Fields]"}})
        ids = params["id"].split(",")
        if all(i in records for i in ids):
            return json.dumps({"result": {"uids": ids, **{i: records[i] for i in ids}}})
        return json.dumps({"result": {"uids": ids, **{i: {"uid": i, "ds_meshterms": [names.get(i, "N" + i)]}
                                                       for i in ids}}})
    return fake


SCR = {"uid": "2", "ds_meshui": "C506393", "ds_meshterms": ["tricalcium silicate", "biodentine"],
       "ds_recordtype": "supplemental-record", "ds_headingmappedtolist": ["68017610", "68017640"],
       "ds_idxlinks": [{"parent": 1000048, "treenum": "@152523", "children": []}]}
DESC = {"uid": "68014071", "ds_meshui": "D014071", "ds_meshterms": ["Tooth Abnormalities"],
        "ds_recordtype": "descriptor", "ds_idxlinks": [{"parent": 68018640, "treenum": "C07.650.800", "children": []}]}


class Mesh(unittest.TestCase):
    def test_supplemental_record_marked_and_mapped(self):
        names = {"68017610": "Calcium Compounds", "68017640": "Silicates", "68018640": "Mouth Abnormalities"}
        with patch.object(m, "_get_async", mesh_fake({"2": SCR, "68014071": DESC}, names=names)):
            text = run(m.mesh, "biodentine")
            data = json.loads(run(m.mesh, "biodentine", as_json=True))
        self.assertIn("其中 1 条是补充概念", text)
        self.assertIn("[nm]", text)
        self.assertIn("映射主题词：Calcium Compounds (UID 68017610); Silicates (UID 68017640)", text)
        self.assertNotIn("@152523", text)
        self.assertNotIn("1000048", text)
        scr = [d for d in data["descriptors"] if d["ui"] == "C506393"][0]
        self.assertEqual((scr["treenums"], scr["parents"]), ([], []))
        self.assertEqual([x["name"] for x in scr["mapped_to"]], ["Calcium Compounds", "Silicates"])
        desc = [d for d in data["descriptors"] if d["ui"] == "D014071"][0]
        self.assertNotIn("mapped_to", desc)
        self.assertEqual(desc["treenums"], ["C07.650.800"])

    def test_descriptor_only_header_unchanged(self):
        with patch.object(m, "_get_async", mesh_fake({"68014071": DESC})):
            text = run(m.mesh, "Tooth Abnormalities")
        self.assertEqual(text.splitlines()[1], "MeSH 命中 1 个描述符，本次给 1 个")

    def test_retmax_hint_not_impossible_at_cap(self):
        recs = {str(i): {"uid": str(i), "ds_meshterms": [f"Other {i}"]} for i in range(1, 41)}
        with patch.object(m, "_get_async", mesh_fake(recs, count=56)):
            at_cap = run(m.mesh, "resorption", retmax=50)
            below = run(m.mesh, "resorption", retmax=5)
        self.assertNotIn("可增大 retmax", at_cap)
        self.assertIn("已到上限", at_cap)
        self.assertIn("可增大 retmax（上限 20）", below)


class Export(unittest.TestCase):
    def test_zero_hits_explained(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "z.pmids")
            with patch.object(m, "_esearch", AsyncMock(return_value=es(0, [], '"zzz"[All Fields]'))):
                text = run(m.export, "zzz", p)
            self.assertEqual(Path(p).read_bytes(), b"")
        self.assertIn("命中 0 条，已写出空文件", text)
        self.assertIn("不证明", text)

    def test_request_count_is_per_call_under_concurrency(self):
        def fake_get(path, params, timeout, tries=4, *, before_attempt=None, wait=None):
            if before_attempt:
                before_attempt()
            return json.dumps({"esearchresult": {"count": "0", "idlist": [], "retstart": "0",
                                                 "querytranslation": "x[All Fields]"}})
        res = {}
        with tempfile.TemporaryDirectory() as d, patch.object(m, "_get", fake_get), \
                patch.object(m, "_pace", AsyncMock(return_value=None)):
            async def main():
                async def ex():
                    res["n"] = json.loads(await m.export("x", os.path.join(d, "a.pmids"), as_json=True))["requests"]
                async with anyio.create_task_group() as tg:
                    tg.start_soon(ex)
                    for _ in range(5):
                        tg.start_soon(m.count, "y")
            anyio.run(main)
        self.assertEqual(res["n"], 3)   # 起始计数 + 窗口取号 + 结束重数；并发的 5 次 count 不算进来


class FetchAndLinks(unittest.TestCase):
    def test_all_invalid_ids_leave_no_fragment(self):
        with patch.object(m, "_get_async", AsyncMock(return_value="1. ")):
            out = run(m.fetch_abstracts, "9999999998 9999999999")
        self.assertEqual(out.splitlines()[0], "要 2 条 / 实取 0 条（分 1 批）")
        self.assertNotIn("1. ", out)
        self.assertFalse(out.endswith("\n"))

    def test_pmc_refs_glossed(self):
        async def fake(path, *a, **k):
            if path == "esummary.fcgi":
                return json.dumps({"result": {"uids": ["1"], "1": {"uid": "1", "title": "t"}}})
            return json.dumps({"linksets": [{"ids": ["1"], "linksetdbs": [
                {"linkname": "pubmed_pmc_refs", "links": ["13689599", "13587028"]}]}]})
        with patch.object(m, "_get_async", fake):
            text = run(m.links, "1")
        self.assertIn("pubmed_pmc_refs:2 条（PMC 中引用本文的全文，不是参考文献）", text)


if __name__ == "__main__":
    unittest.main()

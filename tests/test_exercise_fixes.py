"""正畸演练回归。所有编号、计数、日期与响应均为本项目自订测试夹具，非文献证据。"""
import importlib.util
import inspect
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import anyio

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("exercise_subject", ROOT / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


def esr(ids=(), qt='"tooth"[All Fields]', **extra):
    return {"count": str(len(ids)), "idlist": list(ids), "querytranslation": qt,
            "_term": "tooth", "retstart": "0", **extra}


class Recall(unittest.TestCase):
    def call(self, term, ds):
        with patch.object(m, "_esearch", AsyncMock(return_value=ds)), patch.object(m, "_esummary_dicts", AsyncMock(return_value=[])):
            return json.loads(run(m.check_recall, term, "1", as_json=True))

    def test_partial_cjk_and_raw_parentheses(self):
        for term in ("tooth 深度学习", "(tooth OR implant"):
            with self.subTest(term=term):
                data = self.call(term, esr(["1"]))
                self.assertTrue(data["term_degraded"])
                self.assertFalse(data["term_ignored"])

    def test_empty_translation_is_unknown_not_ignored(self):
        data = self.call("tooth", esr(["1"], qt=""))
        self.assertFalse(data["term_ignored"])
        self.assertTrue(data["term_degraded"])

    def test_later_batch_vanishes_or_loses_cjk(self):
        for qt, ignored in (("2[UID]", True), ('"tooth"[All Fields]', False)):
            with self.subTest(qt=qt), patch.object(m, "GOLD_CHUNK", 1), patch.object(m, "_esearch", AsyncMock(side_effect=[
                esr(["1"], qt='"tooth 深度学习"[All Fields] AND 1[UID]'), esr(["2"], qt=qt)
            ])):
                data = json.loads(run(m.check_recall, "tooth 深度学习", "1 2", as_json=True))
                self.assertEqual(data["term_ignored"], ignored)
                self.assertTrue(data["term_ignored"] or data["term_degraded"])
                self.assertEqual(len(data["querytranslations"]), 2)

    def test_valid_apostrophe_subheadings_and_spelling(self):
        for term, qt in (("Crohn’s disease", '"crohn disease"[MeSH Terms]'),
                         ("neoplasms/de", '"neoplasms/drug effects"[MeSH Terms]'),
                         ("orthopaedic", '"orthopedic"[All Fields]')):
            with self.subTest(term=term):
                self.assertFalse(self.call(term, esr(["1"], qt=qt))["term_degraded"])

    def test_lenticular_brackets_only_external_fusion_breaking(self):
        self.assertFalse(any(b for _, b, _ in m._syntax_findings("【orthodontics】")))
        for term in ("orthodontics【tiab】", "tooth【resorption】", "【tooth】resorption"):
            self.assertTrue(any(b for _, b, _ in m._syntax_findings(term)))

    def test_fullwidth_field_after_other_group_and_at_start(self):
        for term in ("tooth（pain） AND implant（tiab）", "（tiab）", "【pain】 OR 【tiab】"):
            with self.subTest(term=term):
                self.assertTrue(any(b for _, b, _ in m._syntax_findings(term)))

    def test_all_tools_remain_async_and_wrapper_errors(self):
        tools = run(m.mcp.list_tools)
        self.assertTrue(all(inspect.iscoroutinefunction(getattr(m, t.name)) for t in tools))
        cases = [("mesh", {"term": ""}, "不能为空"),
                 ("diff", {"term_a": "x", "term_b": "y", "max_show": -1}, "负"),
                 ("export", {"term": "x", "path": ""}, "不能为空"),
                 ("check_recall", {"term": "x", "pmids": "abc"}, "不是 PMID")]
        with patch.object(m, "_get_async", AsyncMock(side_effect=AssertionError("network forbidden"))):
            for name, args, needle in cases:
                try:
                    out = str(run(m.mcp.call_tool, name, args))
                except Exception as e:
                    out = str(e)
                self.assertIn(needle, out)


class Mesh(unittest.TestCase):
    def test_parent_previous_indexing_exact_order_and_chunking(self):
        wanted = []
        async def fake(endpoint, params, timeout):
            if endpoint == "esearch.fcgi":
                return json.dumps({"esearchresult": {"count": "2", "idlist": ["1", "2"], "querytranslation": "target"}})
            ids = params["id"].split(",")
            if ids == ["1", "2"]:
                return json.dumps({"result": {"uids": ids,
                    "1": {"uid": "1", "ds_meshterms": ["Other"]},
                    "2": {"uid": "2", "ds_meshterms": ["Target"], "ds_previousindexing": ["Old term"],
                          "ds_idxlinks": [{"parent": "3", "children": ["4", "5"]}]}}})
            wanted.extend(ids)
            return json.dumps({"result": {"uids": ids, **{i: {"uid": i, "ds_meshterms": ["Name " + i]} for i in ids}}})
        with patch.object(m, "ESUMMARY_CHUNK", 2), patch.object(m, "_get_async", fake):
            data = json.loads(run(m.mesh, "target", as_json=True))
        first = data["descriptors"][0]
        self.assertEqual(first["uid"], "2")
        self.assertEqual(first["previous_indexing"], ["Old term"])
        self.assertEqual(first["parents"], [{"uid": "3", "name": "Name 3"}])
        self.assertEqual(set(wanted), {"3", "4", "5"})

    def test_mesh_search_envelope_and_ids_rejected(self):
        for payload in ({}, {"esearchresult": []}, {"esearchresult": {"count": "1", "idlist": []}},
                        {"esearchresult": {"count": "1", "idlist": ["1", "1"]}}):
            with self.subTest(payload=payload), patch.object(m, "_get_async", AsyncMock(return_value=json.dumps(payload))):
                with self.assertRaises(m.NcbiError):
                    run(m.mesh, "x")

    def test_summary_missing_or_malformed_rejected(self):
        for result in ([], {"uids": []}, {"uids": ["1"], "1": {"uid": "9"}},
                       {"uids": ["1"], "1": {"uid": "1", "error": "cannot get document summary"}}):
            with self.subTest(result=result), patch.object(m, "_get_async", AsyncMock(side_effect=[
                json.dumps({"esearchresult": {"count": "1", "idlist": ["1"], "querytranslation": "x"}}),
                json.dumps({"result": result})
            ])):
                with self.assertRaises(m.NcbiError):
                    run(m.mesh, "x")

    def test_failed_related_batch_does_not_skip_next(self):
        responses = [
            {"esearchresult": {"count": "1", "idlist": ["1"], "querytranslation": "x"}},
            {"result": {"uids": ["1"], "1": {"uid": "1", "ds_meshterms": ["X"], "ds_idxlinks": [{"children": ["2", "3"]}]}}},
            {"result": {"uids": ["2"], "2": {"uid": "2", "error": "unavailable"}}},
            {"result": {"uids": ["3"], "3": {"uid": "3", "ds_meshterms": ["Third"]}}},
        ]
        with patch.object(m, "ESUMMARY_CHUNK", 1), patch.object(m, "_get_async", AsyncMock(side_effect=[json.dumps(r) for r in responses])):
            data = json.loads(run(m.mesh, "x", as_json=True))
        self.assertEqual(data["descriptors"][0]["children"], [{"uid": "2", "name": ""}, {"uid": "3", "name": "Third"}])
        self.assertTrue(any("其他批继续" in w for w in data["warnings"]))

    def test_bad_related_uid_keeps_unresolved_name(self):
        responses = [
            {"esearchresult": {"count": "1", "idlist": ["1"], "querytranslation": "x"}},
            {"result": {"uids": ["1"], "1": {"uid": "1", "ds_meshterms": ["X"], "ds_idxlinks": [{"children": ["2"]}]}}},
            {"result": {"uids": ["2"], "2": {"uid": "99", "ds_meshterms": ["Wrong"]}}},
        ]
        with patch.object(m, "_get_async", AsyncMock(side_effect=[json.dumps(r) for r in responses])):
            data = json.loads(run(m.mesh, "x", as_json=True))
        self.assertEqual(data["descriptors"][0]["children"], [{"uid": "2", "name": ""}])
        self.assertTrue(any("名称获取失败" in w for w in data["warnings"]))


class CitationAndAudit(unittest.TestCase):
    def test_citation_direction_and_truncation(self):
        async def fake(endpoint, params, timeout):
            self.assertEqual(params["db"], "pubmed")
            return json.dumps({"linksets": [{"ids": [params["id"]], "linksetdbs": [
                {"linkname": params["linkname"], "links": ["10", "11", "12"]}]}]})
        with patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present"})), patch.object(m, "SNOW_SINGLE_CAP", 2), patch.object(m, "_get_async", fake):
            for direction in ("refs", "citedin"):
                text = run(m.links, "1", to=direction)
                self.assertIn("pubmed_pubmed_" + direction + "=10,11…共 3", text)

    def test_citation_missing_or_wrong_linkset_is_failure(self):
        for sets in ([], [{"ids": ["9"]}], [{"ids": ["1", "9"]}]):
            with self.subTest(sets=sets), patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present"})), patch.object(m, "_get_async", AsyncMock(return_value=json.dumps({"linksets": sets}))):
                with self.assertRaisesRegex(m.NcbiError, "失败"):
                    run(m.links, "1", to="refs")
        with patch.object(m, "_pmids_exist", AsyncMock(return_value={"1": "present"})), patch.object(m, "_get_async", AsyncMock(return_value=json.dumps({"linksets": [{"ids": ["1"]}]}))):
            self.assertIn("不等于原文无引文", run(m.links, "1", to="refs"))

    def test_audit_raw_vs_executed_parameters(self):
        response = json.dumps({"esearchresult": esr([])})
        with patch.object(m, "_get_async", AsyncMock(return_value=response)), patch.object(m, "_audit") as audit:
            run(m.search, "tooth", field="Title", mindate="2020", retmax="2000", sort="pub_date", as_json=True)
        fields = audit.call_args.kwargs
        self.assertEqual(fields["input_params"]["retmax"], "2000")
        self.assertEqual(fields["request_params"]["retmax"], 1000)
        self.assertEqual(fields["request_params"]["maxdate"], m.NO_END)
        self.assertNotIn("api_key", fields["request_params"])

    def test_audit_parameters_and_missing_metadata(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PUBMED_AUDIT_DIR": tmp}), patch.object(m, "_esearch", AsyncMock(return_value=esr(["1", "2"]))), patch.object(m, "_esummary_dicts", AsyncMock(return_value=[("1", {"title": "X"})])):
            for json_mode in (True, False):
                run(m.search, "tooth", field="Title", mindate="2020", maxdate="2021", retmax=2, sort="pub_date", as_json=json_mode)
            rows = [json.loads(s) for s in Path(tmp, "searches.jsonl").read_text(encoding="utf-8").splitlines()]
        for r in rows:
            self.assertEqual(r["returned"], 1)
            self.assertEqual(r["field"], "Title")
            self.assertEqual(r["mindate"], "2020")
            self.assertEqual(r["retmax"], 2)
            self.assertTrue(any("esummary" in w for w in r["warnings"]))

    def test_metadata_failure_logged_not_zero_hits(self):
        with patch.object(m, "_esearch", AsyncMock(return_value=esr(["1"]))), patch.object(m, "_esummary_dicts", AsyncMock(side_effect=m.NcbiError("outage"))), patch.object(m, "_audit") as audit:
            with self.assertRaisesRegex(m.NcbiError, "outage"):
                run(m.search, "tooth")
            self.assertEqual(audit.call_args.kwargs["error"], "esummary_failed")
            self.assertEqual(audit.call_args.kwargs["count"], 1)

    def test_version_matches_project_not_metadata(self):
        import importlib.metadata
        with patch.object(importlib.metadata, "distribution", side_effect=AssertionError("stale metadata must not be read")):
            self.assertEqual(m._tool_version(), m.__version__)
        self.assertIn('version = "' + m.__version__ + '"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))


class Export(unittest.TestCase):
    def test_adjacent_days_can_split_and_single_day_cannot(self):
        async def search(term, retmax=0, mindate="", maxdate="", **kw):
            ids = ["1", "2"] if mindate != maxdate else (["1"] if mindate.endswith("01") else ["2"])
            return {**esr(ids if retmax else []), "count": str(len(ids))}
        with patch.object(m, "WINDOW", 1), patch.object(m, "_esearch", search):
            ids, slices, count = run(m._export_slice, "x", "2020-01-01", "2020-01-02", {"drift": []})
            self.assertEqual(ids, ["1", "2"])
            self.assertEqual(slices, 2)
            self.assertEqual(count, 2)
        with patch.object(m, "WINDOW", 1), patch.object(m, "_esearch", AsyncMock(return_value=esr(["1", "2"]))):
            with self.assertRaises(m.NcbiError):
                run(m._export_slice, "x", "2020-01-01", "2020-01-01", {"drift": []})

    def test_same_path_concurrent_writer_cannot_clobber(self):
        async def scenario(path):
            arrived, temps, outcomes = [], [], []
            gate = anyio.Event()
            async def csv_writer(tmp, ids):
                temps.append(tmp)
                Path(tmp).write_text(ids[0], encoding="utf-8")
                arrived.append(True)
                if len(arrived) == 2:
                    gate.set()
                await gate.wait()
                return 1, []
            async def search(term, **kw):
                return esr([term])
            async def invoke(term):
                try:
                    await m.export(term, path, format="csv")
                    outcomes.append((term, True))
                except m.NcbiError as e:
                    self.assertIn("已存在", str(e))
                    outcomes.append((term, False))
            with patch.object(m, "_export_csv", csv_writer), patch.object(m, "_esearch", search):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(invoke, "1")
                    tg.start_soon(invoke, "2")
            self.assertEqual(len(set(temps)), 2)
            winners = [term for term, ok in outcomes if ok]
            self.assertEqual(len(winners), 1)
            self.assertEqual(Path(path).read_text(encoding="utf-8"), winners[0])
        with tempfile.TemporaryDirectory() as tmp:
            run(scenario, str(Path(tmp, "same.csv")))
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["same.csv"])

    def test_unsupported_publish_does_not_replace(self):
        operation = "rename" if os.name == "nt" else "link"
        with tempfile.TemporaryDirectory() as tmp, patch.object(m.os, operation, side_effect=OSError("unsupported")), patch.object(m.os, "replace") as replace, patch.object(m, "_esearch", AsyncMock(return_value=esr(["1"]))):
            with self.assertRaisesRegex(m.NcbiError, "安全发布"):
                run(m.export, "tooth", str(Path(tmp, "out.txt")))
            self.assertFalse(list(Path(tmp).iterdir()))
            replace.assert_not_called()

    def test_export_failure_removes_unique_temp(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, "_esearch", AsyncMock(return_value=esr(["1"]))), patch.object(m, "_export_csv", AsyncMock(side_effect=OSError("disk error"))):
            with self.assertRaisesRegex(m.NcbiError, "disk error"):
                run(m.export, "tooth", str(Path(tmp, "out.csv")), format="csv")
            self.assertFalse(list(Path(tmp).iterdir()))


if __name__ == "__main__":
    unittest.main()

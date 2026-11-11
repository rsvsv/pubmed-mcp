"""Boundary fixtures are synthetic unless explicitly labeled captured tool output.
Numbers, IDs, rates and wait durations below are 本项目自订测试输入/口径.
"""
import importlib.util
import io
import json
from pathlib import Path
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch
import anyio

spec = importlib.util.spec_from_file_location("boundary_subject", Path(__file__).resolve().parents[1] / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


class ResponseBoundary(unittest.TestCase):
    def test_bad_summary_envelopes_raise(self):
        for payload in ({}, {"result": {}}, {"result": {"uids": "1"}}):
            async def fake(*a, **k):
                return json.dumps(payload)
            with patch.object(m, "_get_async", fake), self.assertRaises(m.NcbiError):
                run(m._esummary_dicts, ["1"])

    def test_summary_error_shell_not_counted_as_record(self):
        async def fake(*a, **k):
            return json.dumps({"result": {"uids": ["1"], "1": {"uid": "1", "error": "cannot get document summary"}}})
        with patch.object(m, "_get_async", fake):
            self.assertEqual(run(m._esummary_dicts, ["1"]), [])
            self.assertEqual(run(m._pmids_exist, ["1"]), {"1": "unavailable"})

    def test_summary_backend_failure_raises(self):
        async def fake(*a, **k):
            return json.dumps({"result": {"uids": ["1"], "1": {"error": "backend unavailable"}}})
        with patch.object(m, "_get_async", fake), self.assertRaisesRegex(m.NcbiError, "backend unavailable"):
            run(m._esummary_dicts, ["1"])

    def test_summary_uid_conflict_raises(self):
        async def fake(*a, **k):
            return json.dumps({"result": {"uids": ["1"], "1": {"uid": "2", "title": "Wrong article"}}})
        with patch.object(m, "_get_async", fake), self.assertRaisesRegex(m.NcbiError, "PMID"):
            run(m._esummary_dicts, ["1"])

    def test_partial_summary_states(self):
        async def fake(*a, **k):
            return json.dumps({"result": {"uids": ["1", "2", "3"], "1": {"title": "real title"}, "2": {"error": "cannot get document summary"}}})
        with patch.object(m, "_get_async", fake):
            self.assertEqual(run(m._pmids_exist, ["1", "2", "3", "4"]), {"1": "present", "2": "unavailable", "3": "unknown", "4": "unknown"})

    def test_valid_link_survives_missing_metadata(self):
        async def fake(path, *a, **k):
            if path == "esummary.fcgi":
                return json.dumps({"result": {"uids": []}})
            return json.dumps({"linksets": [{"ids": ["1"], "linksetdbs": [{"linkname": "pubmed_pmc", "links": ["123"]}]}]})
        with patch.object(m, "_get_async", fake):
            text = run(m.links, "1")
        self.assertIn("PMC123", text)
        self.assertIn("未确认", text)
        self.assertNotIn("没有这个号", text)

    def test_no_link_and_missing_metadata_remains_unknown(self):
        async def fake(path, *a, **k):
            return json.dumps({"result": {"uids": []}} if path == "esummary.fcgi" else {"linksets": [{"ids": ["1"]}]})
        with patch.object(m, "_get_async", fake):
            text = run(m.links, "1")
        self.assertIn("存在性无法确认", text)
        self.assertNotIn("无直接对应记录)", text)

    def test_unknown_abstract_payloads_raise(self):
        for body in ("upstream temporarily unavailable", "<?xml version='1.0'?><ERROR>failure</ERROR>", "", '{"error":"failure"}'):
            async def fake(*a, **k):
                return body
            with self.subTest(body=body), patch.object(m, "_get_async", fake), self.assertRaises(m.NcbiError):
                run(m.fetch_abstracts, "1")

    def test_known_empty_placeholder_is_not_failure(self):
        # Shape captured directly from NCBI for an invalid PMID: '1. '.
        async def fake(*a, **k):
            return "1. "
        with patch.object(m, "_get_async", fake):
            text = run(m.fetch_abstracts, "9999999999")
        self.assertIn("实取 0 条", text)

    def test_real_record_without_abstract_preserved(self):
        async def fake(*a, **k):
            return "1. Journal citation\n\nPMID: 1\n"
        with patch.object(m, "_get_async", fake):
            self.assertIn("实取 1 条", run(m.fetch_abstracts, "1"))

    def test_unexpected_and_duplicate_pmids_rejected(self):
        for body in ("PMID: 2\n", "PMID: 1\nPMID: 1\n"):
            async def fake(*a, **k):
                return body
            with patch.object(m, "_get_async", fake), self.assertRaises(m.NcbiError):
                run(m.fetch_abstracts, "1")


class QueryBoundary(unittest.TestCase):
    def test_empty_translation_does_not_prove_empty_conditions(self):
        d = {"_term": "牙根吸收", "querytranslation": "", "count": "0"}
        text = "\n".join(m._note(d)) + m._zero_explanation(d, 0)
        self.assertIn("对应英文词重查", text)
        self.assertNotIn("只索引英文", text)
        self.assertNotIn("没解析出检索条件", text)
        self.assertIn("未返回实际执行式", text)

    def test_missing_phrase_does_not_imply_dropped(self):
        d = {"_term": "miniscrew[Title] AND zzzqqx", "count": "0", "querytranslation": "miniscrew[Title] AND zzzqqx", "errorlist": {"phrasesnotfound": ["zzzqqx"]}}
        text = "\n".join(m._note(d))
        self.assertIn("phrasesnotfound", text)
        self.assertNotIn("已【丢弃】", text)
        self.assertIn("解析有警告", m._zero_explanation(d, 0))

    def test_json_missing_phrase_has_accurate_name_and_deprecated_alias(self):
        async def fake(*a, **k):
            return {"_term": "tooth AND zzzqqx", "count": "0", "idlist": [], "querytranslation": "tooth AND zzzqqx", "errorlist": {"phrasesnotfound": ["zzzqqx"]}}
        with patch.object(m, "_esearch", fake):
            data = json.loads(run(m.search, "tooth AND zzzqqx", as_json=True))
        self.assertEqual(data["phrases_not_found"], ["zzzqqx"])
        self.assertEqual(data["dropped_phrases"], data["phrases_not_found"])
        self.assertIn("不表示", data["deprecated_fields"]["dropped_phrases"])

    def test_field_is_checked_even_with_missing_phrase(self):
        d = {"_term": "x[Title] AND zzzqqx", "querytranslation": '"x"[Title]', "_field_applied": "Journal", "errorlist": {"phrasesnotfound": ["zzzqqx"]}}
        text = "\n".join(m._note(d))
        self.assertNotIn("field 只作用", text)
        self.assertIn("term 内的标签", text)

    def test_paging_precedes_missing_terms(self):
        d = {"_term": "zzzqqx", "_retmax": 20, "retstart": "2", "querytranslation": "2020/01/01:2020/12/31[Date - Publication]", "errorlist": {"phrasesnotfound": ["zzzqqx"]}}
        self.assertIn("越过了命中总数", m._zero_explanation(d, 2))
        self.assertIn("不等于所有执行条件为空", m._zero_explanation(d, 0))

    def test_sdk_rejects_boolean_float_and_decimal_string(self):
        async def scenario():
            async def no_network(*a, **k):
                self.fail("invalid parameter reached network")
            with patch.object(m, "_get_async", no_network):
                for tool, param in (("count", "reldate"), ("search", "reldate"), ("search", "retmax"), ("search", "retstart")):
                    for value in (True, False, 1.0, 1.9, "1.0"):
                        with self.subTest(tool=tool, param=param, value=value):
                            with self.assertRaises(Exception) as exc:
                                await m.mcp.call_tool(tool, {"term": "tooth", param: value})
                            self.assertIn(param, str(exc.exception))
        run(scenario)

    def test_sdk_preserves_integer_string_compatibility(self):
        async def scenario():
            captured = []
            async def fake(path, q, *a, **k):
                captured.append(q)
                return json.dumps({"esearchresult": {"count": "0", "idlist": [], "retstart": "0", "querytranslation": "tooth"}})
            with patch.object(m, "_get_async", fake):
                await m.mcp.call_tool("count", {"term": "tooth", "reldate": "12"})
            self.assertEqual(captured[0]["reldate"], 12)
        run(scenario)


class TransportBoundary(unittest.TestCase):
    def test_every_retry_is_paced(self):
        async def scenario():
            events = []
            async def pace():
                events.append("pace")
            def request(*a, **k):
                events.append("request")
                raise urllib.error.HTTPError("https://example.invalid", 429, "quota", {"Retry-After": "0"}, io.BytesIO(b"quota"))
            with patch.object(m, "_pace", pace), patch.object(m.urllib.request, "urlopen", request):
                with self.assertRaises(m.NcbiError):
                    await m._get_async("test", {}, 1)
            self.assertEqual(events, ["pace", "request"] * 4)
        run(scenario)

    def test_cancelled_wait_does_not_reserve_slot(self):
        async def scenario():
            pacer = m._Pacer(10)
            await pacer.wait()
            previous = pacer._next
            with anyio.move_on_after(0):
                await pacer.wait()
            self.assertEqual(pacer._next, previous)
        run(scenario)

    def test_cancellation_stops_future_retries(self):
        async def scenario():
            started, release = threading.Event(), threading.Event()
            calls = []
            def request(*a, **k):
                calls.append(1)
                started.set()
                if not release.wait(2):
                    raise RuntimeError("test release timeout")
                raise urllib.error.URLError("simulated failure")
            async def worker(scope):
                with scope:
                    await m._get_async("test", {}, 1)
            with patch.object(m.urllib.request, "urlopen", request), patch.object(m, "_PACER", m._Pacer(1000)):
                scope = anyio.CancelScope()
                async with anyio.create_task_group() as tg:
                    tg.start_soon(worker, scope)
                    try:
                        with anyio.fail_after(2):
                            while not started.is_set():
                                await anyio.sleep(0.001)
                        scope.cancel()
                    finally:
                        release.set()
            self.assertEqual(len(calls), 1)
        run(scenario)


if __name__ == "__main__":
    unittest.main()

"""Offline regressions: synthetic fixtures, plus captured NCBI responses.
All literals are test inputs/expectations (本项目自订测试口径), not literature evidence.
Run: python -I -m unittest discover -s <repo>/tests -v
"""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import io
import urllib.error

import anyio

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pubmed_subject", ROOT / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


class QueryRegression(unittest.TestCase):
    def test_lowercase_and_warns_only_when_changed(self):
        d = {"_term": "miniscrew[Title] and stability[Title]", "querytranslation": '"miniscrew"[Title] AND "and stability"[Title]'}
        self.assertIn("小写 and", "\n".join(m._note(d)))
        d["_term"] = '"and stability"[Title]'
        self.assertNotIn("小写 and", "\n".join(m._note(d)))

    def test_mixed_cjk(self):
        d = {"_term": "牙根吸收 resorption", "querytranslation": '"resorption"[All Fields]'}
        self.assertIn("牙根吸收", "\n".join(m._query_warnings(d)))
        d["querytranslation"] = '"牙根吸收" AND "resorption"'
        self.assertFalse(m._query_warnings(d))

    def test_malformed_syntax_and_zero(self):
        for term in ('NOT stability[Title]', '(tooth OR implant', '"tooth', 'tooth)('):
            with self.subTest(term=term):
                d = {"_term": term, "querytranslation": '"tooth"[All Fields]'}
                self.assertTrue(m._query_warnings(d))
                self.assertIn("解析有警告", m._zero_explanation(d, 0))

    def test_quoted_syntax_is_not_operator(self):
        for term in ('"NOT stability"[Title]', '"(tooth)"[Title]', '"and stability"[Title]', '(tooth OR implant) AND stability',
                     '"root resorption"[tiab] AND laser', 'laser AND "root resorption"[tiab]',
                     '"dental implant" AND osseointegration'):  # 本项目自订回归输入；覆盖 issue 的原例
            with self.subTest(term=term):
                self.assertFalse(m._query_warnings({"_term": term, "querytranslation": term}))

    def test_parenthesized_phrase_or_block_not_boolean_start(self):
        # 以「(短语 OR 短语)」开头不应误报「以布尔算子开头」〔实测：正畸演练 export 误报，mask 把短语抹成空格所致〕。
        term = '("root resorption"[tiab] OR "apical root resorption"[tiab]) AND orthodontic[tiab]'
        warns = m._query_warnings({"_term": term, "querytranslation": term})
        self.assertFalse(any("以布尔算子开头" in w for w in warns))
        # 真正以算子开头（含括号包裹）仍须告警
        for bad in ("OR cancer", "(OR cancer)", "AND tooth"):
            with self.subTest(bad=bad):
                self.assertTrue(any("以布尔算子开头" in w
                                    for w in m._query_warnings({"_term": bad, "querytranslation": bad})))

    def test_outputmessages_marked(self):
        d = {"_term": "x", "querytranslation": "x", "warninglist": {"outputmessages": ["NOT", "("]}}
        self.assertTrue(any(line.startswith("⚠️ NCBI 解析警告") for line in m._note(d)))
        self.assertIn("解析有警告", m._zero_explanation(d, 0))

    def test_zero_claim_is_query_limited(self):
        text = m._zero_explanation({"_term": "x", "querytranslation": "x"}, 0)
        self.assertIn("检索式未匹配", text)
        self.assertNotIn("确实没有匹配这篇", text)

    def test_date_boolean_semantics(self):
        a = '2019/01/01:2019/12/31[Date - Publication]'
        b = '2035/01/01:2035/12/31[Date - Publication]'
        cases = [
            (f'x AND {a} AND {b}', True),
            (f'x AND ({a} OR {b})', False),
            (f'x AND {a} NOT {b}', False),
            (f'({a} AND {b}) OR x', False),
            (f'(x OR y) AND {a} AND {b}', True),
            (f'({a} OR x) AND {a} AND {b}', True),
            (f'({a} AND {b}) OR ({b} AND {a})', True),
            (f'"OR NOT"[Title] AND {a} AND {b}', True),
        ]
        for qt, expected in cases:
            with self.subTest(qt=qt):
                d = {"_term": qt, "querytranslation": qt, "_windows": m._windows(qt)}
                self.assertEqual(any("不相交" in line for line in m._note(d)), expected)
                self.assertEqual("不相交" in m._zero_explanation(d, 0), expected)

    def test_captured_responses(self):
        captured = json.loads((ROOT / "tests" / "fixtures" / "audit_queries.json").read_text(encoding="utf-8"))
        for row in captured:
            self.assertNotIn("error", row)
        by_term = {row["term"]: row["raw"] for row in captured}
        for term in ('NOT stability[Title]', '牙根吸收 resorption', 'miniscrew[Title] and stability[Title]'):
            self.assertTrue(m._query_warnings(by_term[term]))
        self.assertFalse(any("不相交" in line for line in m._note(by_term['miniscrew[Title] AND (2019[dp] OR 2035[dp])'])))
        self.assertTrue(any("不相交" in line for line in m._note(by_term['miniscrew[Title] AND 2019[dp] AND 2035[dp]'])))
        phrase = by_term['"zzzqqx nonexistent term"']
        self.assertTrue(any("不是原短语" in line for line in m._note(phrase)))
        self.assertTrue(any("zzzqqx" in line and "找不到" in line for line in m._note(phrase)))


class InfrastructureRegression(unittest.TestCase):
    def test_nested_errors(self):
        for payload in ({"eutilsresult": {"ERROR": "upstream failed"}}, {"error": "upstream failed"}, {"header": {"error": "upstream failed"}}):
            with self.assertRaisesRegex(m.NcbiError, "upstream failed"):
                m._safe_json(json.dumps(payload), "elink")

    def test_invalid_json_shapes(self):
        for text in ('[]', 'null', '"x"', '<html>failure</html>'):
            with self.assertRaises(m.NcbiError):
                m._safe_json(text, "elink")

    def test_numeric_inputs(self):
        for value in (1.9, float("inf"), float("nan"), True, False):
            with self.subTest(value=value), self.assertRaises(m.NcbiError):
                m._int(value, "retmax")
        self.assertEqual(m._int("12", "retmax"), 12)
        self.assertEqual(m._int(0, "retmax"), 0)

    def test_retry_unicode(self):
        self.assertIsNone(m._retry_after({"Retry-After": "²"}))
        self.assertEqual(m._retry_after({"Retry-After": "0"}), 0)
        self.assertEqual(m._retry_after({"Retry-After": "12"}), 12)

    def test_huge_retry_after_fails_without_sleep(self):
        error = urllib.error.HTTPError("https://example.invalid", 429, "limited", {"Retry-After": "999999999999999"}, io.BytesIO(b"limited"))
        with patch.object(m.urllib.request, "urlopen", side_effect=error) as request, patch.object(m.time, "sleep") as sleep, patch.object(m, "api_key", return_value=""):
            with self.assertRaisesRegex(m.NcbiError, "Retry-After"):
                m._get("elink.fcgi", {}, 1)
            sleep.assert_not_called()
            self.assertEqual(request.call_count, 1)

    def test_zero_retry_after_is_respected(self):
        error = urllib.error.HTTPError("https://example.invalid", 429, "limited", {"Retry-After": "0"}, io.BytesIO(b"limited"))
        with patch.object(m.urllib.request, "urlopen", side_effect=error), patch.object(m.time, "sleep") as sleep, patch.object(m, "api_key", return_value=""):
            with self.assertRaises(m.NcbiError):
                m._get("elink.fcgi", {}, 1, tries=2)
            sleep.assert_called_once_with(0.0)

    def test_masks_url_encoded_key(self):
        with patch.object(m, "api_key", return_value="fake+key/part"):
            for value in ("fake+key/part", "fake%2Bkey%2Fpart", "fake%2bkey%2fpart"):
                self.assertEqual(m._mask("url?api_key=" + value), "url?api_key=***")

    def test_error_bodies_redacted(self):
        with patch.object(m, "api_key", return_value="fake-secret-key"):
            for body in ('<html>fake-secret-key</html>', '{"eutilsresult":{"ERROR":"fake-secret-key"}}'):
                with self.assertRaises(m.NcbiError) as ctx:
                    m._safe_json(body, "test")
                self.assertNotIn("fake-secret-key", str(ctx.exception))


class OutputRegression(unittest.TestCase):
    def test_publication_types_not_truncated(self):
        result = m._fmt_record({"pubtype": ["Journal Article", "Review", "Retracted Publication"], "title": "test"}, "1")
        self.assertIn("Retracted Publication", result)
        self.assertNotIn("Journal Article", result)

    def test_invalid_mode_no_network(self):
        async def fail(*args, **kwargs):
            self.fail("Network should not run")
        with patch.object(m, "_get_async", fail):
            with self.assertRaisesRegex(m.NcbiError, "as_json"):
                run(m.search, "tooth", as_json="garbage")

    def test_missing_metadata_and_no_json_query_duplication(self):
        qt = '"tooth"[All Fields]'
        async def esearch(*args, **kwargs):
            return {"_term": "tooth", "count": "2", "idlist": ["1", "2"], "retstart": "0", "querytranslation": qt}
        async def summary(*args):
            return [("1", {"title": "test"})]
        with patch.object(m, "_esearch", esearch), patch.object(m, "_esummary_dicts", summary):
            text = run(m.search, "tooth")
            data = json.loads(run(m.search, "tooth", as_json=True))
        self.assertIn("本次给 1 条", text)
        self.assertEqual(data["returned"], 1)
        self.assertEqual(data["querytranslation"], qt)
        self.assertTrue(any("esummary" in w for w in data["warnings"]))
        self.assertFalse(any(w.startswith("NCBI 实际执行：") for w in data["warnings"]))

    def test_mcp_descriptions_match_functions(self):
        tools = run(m.mcp.list_tools)
        for tool in tools:
            self.assertEqual(tool.description.strip(), getattr(m, tool.name).__doc__.strip())
        self.assertLess(sum(len(t.description) for t in tools), 2600)  # 本项目自订描述体积回归上限（1.4.0 新增 4 工具后由 2000 上调；2026-11-11 实测合计 2304 字符）


if __name__ == "__main__":
    unittest.main()

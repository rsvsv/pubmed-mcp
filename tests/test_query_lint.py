"""1.4.1 term lint regressions: half-width [Tag] whitelist, subfields, leading wildcard, AND NOT,
group tags, date ranges, mixed AND/OR, and count's zero-result explanation.
Expected behaviours come from 实测 2026-11-13 esearch probes (see docs/query-lint.md);
all literals below are 本项目自订测试输入, not literature evidence.
Run: python -I -m unittest discover -s <repo>/tests -v
"""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch, AsyncMock

import anyio

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lint_subject", ROOT / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


def codes(term):
    return [c for c, _, _ in m._syntax_findings(term)]


def lint(term):
    return {c: (b, t) for c, b, t in m._term_lint(term)}


# 实测被 NCBI 识别的写法（执行式带对应字段）—— 一条都不许报。
VALID = [
    "miniscrew[tiab]", "miniscrew[TIAB]", "miniscrew[Title/Abstract]", "miniscrew [tiab]", "miniscrew[ tiab ]",
    '"root resorption"[tiab:~3]', '"root resorption"[Title/Abstract:~3]', '"root resorption"[ti:~3]',
    "Root Resorption[mh:noexp]", "Root Resorption[MeSH Terms:noexp]", "Root Resorption[mh: noexp]",
    "review[pt:noexp]", "diagnosis[sh:noexp]", "Root Resorption/diagnosis[mh]", "Root Resorption/etiology[majr]",
    "english[la]", "english[lang]", "english[Language]", "Smith J[au]", "Smith J[1au]", "Smith J[lastau]",
    "review[pt]", "(review)[pt]", "(miniscrew)[tiab]", '("root resorption")[tiab]', "(miniscrew OR aligner)[All Fields]",
    '"last 5 years"[dp]', "2020[dp]", "2020/03[dp]", "2020:2024[dp]", "2020/01/01:2024/12/31[dp]", "2020 : 2024[dp]",
    "2020/01:2020/06[dp]", "2020[edat]", "2020/01/01:2020/12/31[crdt]", "2024[pdat]", "2020:2024[Date - Publication]",
    "free full text[sb]", '"free full text"[sb]', "10.1016/j.ajodo.2023.01.001[doi]", "hasabstract",
    "deep learning[ot]", "deep learning[keyword]", "Nat Genet[ta]", "8610224[jid]", "J Clin Med[jour]", "Lancet[journal]",
    "miniscrew[tw]", "miniscrew[Text Word]", "miniscrew[cois]", "miniscrew[Other Term]", "123I[nm]", "[123I]iodide",
    "Cone-Beam Computed Tomography[mh]", "humans[mh] NOT animals[mh]", "a NOT b", '"AND NOT"[tiab]',
    "screw*[tiab]", '"*screw"[tiab]', '"tooth[tiabb]"', "(a OR b) AND (c OR d)", "a AND b AND c", "a OR b OR c",
    "((a OR b) AND c) OR d", '("root resorption"[tiab] OR "apical root resorption"[tiab]) AND orthodontic[tiab]',
    "(orthodontic root resorption) AND (CBCT) AND 2020:2026[dp]",
    '"tooth movement"[tiab] AND ("deep learning"[tiab] OR "machine learning"[tiab])',
    "miniscrew*[tiab] AND (root resorption[tiab] OR tooth resorption[tiab])",
]


class Whitelist(unittest.TestCase):
    def test_valid_queries_raise_nothing_new(self):
        for term in VALID:
            with self.subTest(term=term):
                self.assertEqual(m._term_lint(term), [])

    def test_typo_tag_breaking_with_suggestion(self):
        f = lint("miniscrew[tiabb]")
        self.assertTrue(f["term_tag_unknown"][0])
        self.assertIn("可能想写 [tiab]", f["term_tag_unknown"][1])

    def test_known_invalid_aliases_flagged(self):
        # 实测 2026-11-13：这些写法执行式与不写标签逐字相同。
        for tag, hint in (("ab", "[tiab]"), ("abstract", "[tiab]"), ("keywords", "[ot]"), ("kw", "[ot]"),
                          ("mesh term", "[mh]"), ("date of publication", "[dp]"), ("jt", "[ta]"),
                          ("nlm unique id", "[jid]")):
            with self.subTest(tag=tag):
                b, text = lint(f"x[{tag}]")["term_tag_unknown"]
                self.assertTrue(b)
                self.assertIn(hint, text)

    def test_soft_aliases_not_breaking(self):
        for tag in ("pmc", "pmcid", "issn"):
            with self.subTest(tag=tag):
                b, _ = lint(f"PMC1[{tag}]")["term_tag_ignored"]
                self.assertFalse(b)

    def test_internal_spacing_is_flagged(self):
        # 实测：[Title / Abstract]、[MeSH  Terms] 被静默忽略。
        for term in ("miniscrew[Title / Abstract]", "miniscrew[MeSH  Terms]", "miniscrew[Title /Abstract]"):
            with self.subTest(term=term):
                self.assertIn("term_tag_unknown", lint(term))

    def test_multiple_unknown_merge_into_one_line(self):
        f = m._term_lint("a[tiabb] AND b[ab]")
        self.assertEqual([c for c, _, _ in f], ["term_tag_unknown"])

    def test_subfields(self):
        self.assertIn("term_tag_subfield", lint("miniscrew[ti:noexp]"))
        self.assertIn("term_tag_subfield", lint('"a b"[au:~3]'))
        self.assertIn("term_tag_subfield", lint("x[mh:noex]"))
        self.assertFalse(lint("x[mh:noex]")["term_tag_subfield"][0])

    def test_unquoted_proximity(self):
        b, _ = lint("root resorption[tiab:~3]")["proximity_unquoted"]
        self.assertTrue(b)
        self.assertNotIn("proximity_unquoted", lint("miniscrew[tiab:~3]"))  # 单词：无可邻近，NCBI 丢 :~N 但结果同 [tiab]


class Structure(unittest.TestCase):
    def test_leading_wildcard(self):
        for term in ("*screw[tiab]", "orthodontic*[tiab] AND *screw[tiab]", "(*screw OR x)"):
            with self.subTest(term=term):
                self.assertTrue(lint(term)["leading_wildcard"][0])
        for term in ("screw*", '"*screw"', "a * b", "a*b", "5*HT[tiab]"):
            with self.subTest(term=term):
                self.assertNotIn("leading_wildcard", lint(term))

    def test_and_not_or_not(self):
        for term in ("root resorption[mh] AND NOT orthodontics[mh]", "a OR NOT b", "a AND  NOT b"):
            with self.subTest(term=term):
                self.assertTrue(lint(term)["and_not"][0])
        for term in ("a NOT b", '"AND NOT"[tiab]', "a and not b"):
            with self.subTest(term=term):
                self.assertNotIn("and_not", lint(term))

    def test_group_tag(self):
        for term in ('(miniscrew OR "mini-implant")[tiab]', "(miniscrew aligner)[tiab]", "((a OR b))[mh]"):
            with self.subTest(term=term):
                self.assertTrue(lint(term)["group_tag"][0])
        for term in ("(miniscrew)[tiab]", "(a OR b)[All Fields]", "(a OR b) AND c[tiab]", "(a OR b)[tiabb]"):
            with self.subTest(term=term):
                self.assertNotIn("group_tag", lint(term))
        self.assertIn("term_tag_unknown", lint("(a OR b)[tiabb]"))   # 只报未知标签，不重复报组

    def test_unbalanced_parens_skip_structure_checks(self):
        f = codes("(a OR b AND c")
        self.assertIn("paren", f)
        self.assertNotIn("mixed_boolean", f)

    def test_date_ranges(self):
        for term in ("miniscrew AND 2020-2024[dp]", "miniscrew AND 2020 – 2024[edat]", "x AND 2020-2024[Date - Publication]"):
            with self.subTest(term=term):
                self.assertTrue(lint(term)["date_range_hyphen"][0])
        for term in ("miniscrew AND 2024:2020[dp]", "x[ti] AND 2020/06:2020/01[dp]", "x AND 2024/06:2020/01[dp]"):
            with self.subTest(term=term):
                self.assertTrue(lint(term)["date_range_reverse"][0])
        self.assertEqual(m._term_lint("2020-01-01[dp]"), [])
        self.assertEqual(m._term_lint("2020-2024[tiab]"), [])

    def test_date_tag_with_non_date_value(self):
        for term in ("miniscrew[dp]", "last 5 years[dp]"):
            with self.subTest(term=term):
                self.assertTrue(lint(term)["date_tag_value"][0])

    def test_mixed_boolean_nonbreaking(self):
        for term in ("aligner AND CBCT OR miniscrew", "orthodontics[mh] AND review[pt] OR meta-analysis[pt]",
                     "(a AND b OR c) AND d"):
            with self.subTest(term=term):
                self.assertFalse(lint(term)["mixed_boolean"][0])


class Propagation(unittest.TestCase):
    def test_note_and_check_recall_degraded(self):
        d = {"_term": "miniscrew[tiabb]", "querytranslation": '"miniscrew"[All Fields] OR "miniscrews"[All Fields]'}
        self.assertTrue(any("不认" in x for x in m._note(d)))
        lines, degraded = m._recall_warnings_and_degraded("miniscrew[tiabb]", [dict(d, count="3")])
        self.assertTrue(degraded)
        lines, degraded = m._recall_warnings_and_degraded("a AND b OR c", [{"_term": "a AND b OR c",
                                                                             "querytranslation": "(a AND b) OR c", "count": "3"}])
        self.assertFalse(degraded)
        self.assertTrue(any("从左到右" in x for x in lines))

    def test_zero_explanation_mentions_parse_warning(self):
        d = {"_term": "miniscrew AND 2020-2024[dp]", "querytranslation": "miniscrew AND 2020-2024[dp]",
             "count": "0", "warninglist": {"outputmessages": ["2020-2024", "No items found."]}}
        self.assertIn("解析有警告", m._zero_explanation(d, 0))


class CountZero(unittest.TestCase):
    def call(self, d):
        with patch.object(m, "_esearch", AsyncMock(return_value=d)):
            return run(m.count, d["_term"])

    def test_count_zero_has_explanation(self):
        out = self.call({"_term": "zzz", "count": "0", "querytranslation": '"zzz"[All Fields]'})
        self.assertTrue(out.splitlines()[-1].startswith("=> "))
        self.assertIn("不证明", out)

    def test_count_positive_unchanged(self):
        d = {"_term": "tooth", "count": "7", "querytranslation": '"tooth"[All Fields]'}
        out = self.call(d)
        self.assertEqual(out, "查询：tooth\n命中 7 条\nNCBI 实际执行：\"tooth\"[All Fields]")


if __name__ == "__main__":
    unittest.main()

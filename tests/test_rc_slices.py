"""跨切片主题执行核对回归；所有 PMID、计数、日期与回显串均为本项目自订测试输入。
真实回显形状对照：tests/fixtures/audit_queries.json 第 200/232 行（`2019/01/01:2019/12/31[Date - Publication]`）；
单日窗口无冒号后半截（`2026/11/08[Date - Publication]`，见 pubmed_query.py 的 _windows 说明）。
"""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import anyio

ROOT = Path(__file__).resolve().parents[1]  # 本项目自订：测试文件到仓库根目录的层级
m_spec = importlib.util.spec_from_file_location("rc_slices_subject", ROOT / "pubmed_mcp.py")
m = importlib.util.module_from_spec(m_spec)
m_spec.loader.exec_module(m)
export_spec = importlib.util.spec_from_file_location("rc_slices_export", ROOT / "pubmed_export.py")
export_module = importlib.util.module_from_spec(export_spec)
export_spec.loader.exec_module(export_module)

PUB = "Date - Publication"
TOPIC = '"tooth"[Title] AND "resorption"[Title]'  # 本项目自订：健康主题布尔结构夹具


def wire_date(lo, hi):
    """本项目自订：按真实回显形状拼日期窗口；单日不写冒号后半截。"""
    if lo == hi:
        return f"{lo.replace('-', '/')}[{PUB}]"
    return f"{lo.replace('-', '/')}:{hi.replace('-', '/')}[{PUB}]"


def compose(topic, lo="", hi=""):
    if not lo:
        return topic
    return f"({topic}) AND {wire_date(lo, hi)}"


def reply(ids, count, translation):
    return {"_term": "tooth", "count": str(count), "idlist": list(ids),
            "querytranslation": translation}


def search_fixture(seen, *, wrong_leaf=False):
    """按日期片返回本项目自订计数与 PMID；wrong_leaf 让 2020-01-01 叶片执行成别的主题。"""
    async def search(term, **kw):
        lo, hi = kw.get("mindate") or "", kw.get("maxdate") or ""
        retmax = kw.get("retmax") or 0
        topic, ids, count = TOPIC, [], 0
        if not lo:
            count = 2  # 本项目自订：起始/结束计数
        elif (lo, hi) == ("2020-01-01", "2020-01-04"):
            count = 2  # 本项目自订：根片计数，需递归
        elif (lo, hi) == ("2020-01-01", "2020-01-02"):
            count = 2  # 本项目自订：左内部片计数，需递归
        elif (lo, hi) == ("2020-01-03", "2020-01-04"):
            count = 0  # 本项目自订：右空片计数
        elif (lo, hi) == ("2020-01-01", "2020-01-01"):
            count, ids = 1, ["1"]  # 本项目自订：第一叶片
            if wrong_leaf:
                topic, ids = '"jaw"[Title]', ["999"]  # 本项目自订：完全错误的主题与 PMID
        elif (lo, hi) == ("2020-01-02", "2020-01-02"):
            count, ids = 1, ["2"]  # 本项目自订：第二叶片
        else:
            raise m.NcbiError(f"未预期的夹具切片：{lo}→{hi}")
        translation = compose(topic, lo, hi)
        seen.append({"lo": lo, "hi": hi, "retmax": retmax, "translation": translation})
        return reply(ids if retmax else [], count, translation)
    return search


class SliceTopicStructureHelper(unittest.TestCase):
    """纯函数口径：只允许去掉切片新加的必需日期合取；原有日期一律保留。"""

    def check(self, baseline, probe, lo, hi, datetype="pdat"):
        return export_module._slice_topic_structure_reasons(baseline, probe, lo, hi, datetype)

    def test_accepts_extra_grouping_and_and_association(self):
        baseline = '"tooth"[Title] AND ("a"[Title] OR "b"[Title])'  # 本项目自订：主题夹具
        probe = (f'((("tooth"[Title]))) AND (("a"[Title] OR "b"[Title])) '
                 f'AND {wire_date("2020-01-01", "2020-12-31")}')  # 本项目自订：同日期的额外括号
        self.assertEqual(self.check(baseline, probe, "2020-01-01", "2020-12-31"), [])

    def test_accepts_and_child_wrapped_slice_date(self):
        baseline = '"tooth"[Title] AND "a"[Title]'  # 本项目自订：主题夹具
        probe = f'"tooth"[Title] AND ("a"[Title] AND {wire_date("2020-01-01", "2020-12-31")})'
        self.assertEqual(self.check(baseline, probe, "2020-01-01", "2020-12-31"), [])

    def test_preserves_original_same_date_with_and_without_dedup(self):
        window = wire_date("2020-01-01", "2020-12-31")  # 本项目自订：与切片相同的原日期
        baseline = f'"tooth"[Title] AND {window}'
        self.assertEqual(self.check(baseline, baseline, "2020-01-01", "2020-12-31"), [])  # 本项目自订：回显去重
        doubled = f'"tooth"[Title] AND {window} AND {window}'  # 本项目自订：回显未去重
        self.assertEqual(self.check(baseline, doubled, "2020-01-01", "2020-12-31"), [])
        self.assertEqual(self.check(doubled, f'{doubled} AND {window}', "2020-01-01", "2020-12-31"), [])

    def test_preserves_dates_beneath_or_and_not(self):
        window = wire_date("2020-01-01", "2020-12-31")  # 本项目自订：与切片相同的日期
        or_base = f'"tooth"[Title] AND ("a"[Title] OR {window})'
        or_probe = f'({or_base}) AND {window}'
        self.assertEqual(self.check(or_base, or_probe, "2020-01-01", "2020-12-31"), [])
        not_base = f'"tooth"[Title] NOT {window}'
        not_probe = f'({not_base}) AND {window}'
        self.assertEqual(self.check(not_base, not_probe, "2020-01-01", "2020-12-31"), [])

    def test_single_day_and_sentinel_windows(self):
        self.assertEqual(self.check('"tooth"[Title]',
                                    f'"tooth"[Title] AND {wire_date("2020-01-01", "2020-01-01")}',
                                    "2020-01-01", "2020-01-01"), [])
        self.assertEqual(self.check('"tooth"[Title]',
                                    f'"tooth"[Title] AND {wire_date("0001-01-01", "2999-12-31")}',
                                    "0001-01-01", "2999-12-31"), [])

    def test_rejects_wrong_topic_and_missing_slice_date(self):
        changed = f'"jaw"[Title] AND {wire_date("2020-01-01", "2020-12-31")}'
        self.assertTrue(self.check('"tooth"[Title]', changed, "2020-01-01", "2020-12-31"))
        self.assertTrue(self.check('"tooth"[Title]', '"tooth"[Title]', "2020-01-01", "2020-12-31"))
        under_or = f'"tooth"[Title] AND ("a"[Title] OR {wire_date("2020-01-01", "2020-12-31")})'
        self.assertTrue(self.check('"tooth"[Title]', under_or, "2020-01-01", "2020-12-31"))

    def test_rejects_lost_original_date_and_ambiguous_duplicates(self):
        window = wire_date("2020-01-01", "2020-12-31")  # 本项目自订：切片窗口
        other = wire_date("2019-01-01", "2019-12-31")  # 本项目自订：原日期条件
        self.assertTrue(self.check(f'"tooth"[Title] AND {other}',
                                   f'"tooth"[Title] AND {window}', "2020-01-01", "2020-12-31"))
        self.assertTrue(self.check(f'"tooth"[Title] AND {window} AND {window}',
                                   f'"tooth"[Title] AND {window}', "2020-01-01", "2020-12-31"))
        self.assertTrue(self.check('"tooth"[Title]',
                                   f'"tooth"[Title] AND {window} AND {window}',
                                   "2020-01-01", "2020-12-31"))

    def test_ambiguous_echo_cannot_pass_silently(self):
        for probe in ('"tooth"[Title] AND (2020/01/01:2020/12/31[Date - Publication]',  # 本项目自订：缺右括号
                      '"tooth"[Title] AND 2020/01/01:2020/12/31[Date - Publication])',  # 本项目自订：多余右括号
                      '"tooth"[Title] OR "a"[Title] AND 2020/01/01:2020/12/31[Date - Publication]'):  # 本项目自订：同层混用
            with self.subTest(probe=probe):
                self.assertTrue(self.check('"tooth"[Title]', probe, "2020-01-01", "2020-12-31"))
        self.assertTrue(self.check("", '"tooth"[Title] AND 2020/01/01:2020/12/31[Date - Publication]',
                                   "2020-01-01", "2020-12-31"))


class PublicExportSlices(unittest.TestCase):
    """公开 export 口径：完整错误的主题仍保留实取名单，只降级完整性。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"PUBMED_AUDIT_DIR": ""})
        env.start()
        self.addCleanup(env.stop)

    def run_export(self, name, *, wrong_leaf=False):
        seen = []
        async def call():
            with patch.object(m, "_esearch", search_fixture(seen, wrong_leaf=wrong_leaf)), \
                    patch.object(m, "WINDOW", 1), \
                    patch.object(m, "NO_BEGIN", "2020-01-01"), \
                    patch.object(m, "NO_END", "2020-01-04"):  # 本项目自订：两日窗口、四日切片范围
                return json.loads(await m.export("tooth", str(self.root / name), as_json=True)), seen
        return anyio.run(call)

    def test_healthy_root_internal_empty_and_leaf_slices(self):
        result, seen = self.run_export("healthy.txt")
        self.assertEqual(result["integrity_status"], "no_discrepancy_detected")
        self.assertEqual(result["integrity_issues"], [])
        self.assertFalse(any("主题结构" in w for w in result["warnings"]))
        stages = [check["stage"] for check in result["retrieval_checks"]]
        for span in ("2020-01-01→2020-01-04", "2020-01-01→2020-01-02",
                     "2020-01-03→2020-01-04", "2020-01-01→2020-01-01"):
            self.assertTrue(any(span in stage and "计数探测" in stage for stage in stages), span)
        self.assertEqual((self.root / "healthy.txt").read_text(encoding="utf-8").split(), ["1", "2"])  # 本项目自订：完整集合
        self.assertTrue(all(s["translation"].endswith("Date - Publication]") for s in seen if s["lo"]))  # 本项目自订：真实回显形状

    def test_complete_wrong_topic_leaf_keeps_ids_and_degrades_integrity(self):
        result, seen = self.run_export("wrong.txt", wrong_leaf=True)
        self.assertEqual(result["integrity_status"], "needs_review")
        self.assertIn("slice_topic_unverified", result["integrity_issues"])
        self.assertEqual(result["count_start"], 2)  # 本项目自订：起始总数
        self.assertEqual(result["unique"], 2)  # 本项目自订：去重后仍保留两个实取号
        self.assertEqual((self.root / "wrong.txt").read_text(encoding="utf-8").split(), ["999", "2"])  # 本项目自订：错误主题的号不被丢弃
        warning = next(w for w in result["warnings"] if "主题结构与基线" in w)
        self.assertIn("2020-01-01→2020-01-01", warning)
        leaf = next(c for c in result["retrieval_checks"]
                    if "2020-01-01→2020-01-01" in c["stage"] and "计数探测" in c["stage"])
        self.assertEqual(leaf["querytranslation"], '("jaw"[Title]) AND 2020/01/01[Date - Publication]')
        self.assertTrue(leaf["topic_reasons"])


if __name__ == "__main__":
    unittest.main()

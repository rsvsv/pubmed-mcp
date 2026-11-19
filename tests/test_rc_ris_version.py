"""RIS 检查点格式版本隔离的回归测试。

本模块出现的 PMID、批大小、版本号与记录内容均为本项目自订测试输入／夹具，
不是文献数据、NCBI 实测值或真实作业数据。批大小固定为 2，只为稳定构造
「一批已完成、下一批待抓」的部分检查点。

运行：python -I tests/test_rc_ris_version.py，或 pytest tests/test_rc_ris_version.py
测试直接加载仓库源码，覆盖新建与恢复路径的实际版本校验。
"""
import csv
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import anyio

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("new_subject", ROOT / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


def write_text(path, text):
    Path(path).write_text(text, encoding="utf-8")
    return str(path)


async def fake_csv(path, ids):
    # 夹具：表头与本项目 _PMIDS_CSV_HEADER 对齐；title 只用于区分批文件内容。
    rows = [list(m._PMIDS_CSV_HEADER)]
    for p in ids:
        rows.append([p, "", "fixture", "", "", "", "", "", "", ""])
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)
    return len(ids), []


async def fake_ris(path, ids):
    # 夹具：完整 TY/AN/ER 题录块；故意逆序写出，以覆盖恢复时的重排校验。
    got = list(reversed(ids))
    blocks = [f"TY  - JOUR\nAN  - {p}\nER  - " for p in got]
    Path(path).write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")
    return len(got), []


class RISVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        # 夹具：统一批大小 2，让 4 个 PMID 正好两批。
        sizes = patch.object(m, "_EXPORT_BATCH_SIZE",
                             {"pmids": 2, "csv": 2, "ris": 2, "medline": 2})
        sizes.start()
        self.addCleanup(sizes.stop)

    def p(self, name):
        return os.path.join(self.d, name)

    def source(self, ids):
        return write_text(self.p("ids.txt"), " ".join(ids) + "\n")

    def manifest(self, job):
        return json.loads(Path(job, "manifest.json").read_text(encoding="utf-8"))

    def write_manifest(self, job, doc):
        Path(job, "manifest.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def state(self, job, batch_id=0):
        path = Path(job, m._EXPORT_STATE_DIR, f"batch-{batch_id:05d}.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def write_state(self, job, doc, batch_id=0):
        path = Path(job, m._EXPORT_STATE_DIR, f"batch-{batch_id:05d}.json")
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def partial(self, fmt, ids, writer):
        """跑到第 2 批时失败，留下第 1 批 done、第 2 批 pending 的检查点。"""
        src, out, job = self.source(ids), self.p("out." + fmt), self.p("job")
        calls = []

        async def flaky(path, batch_ids):
            calls.append(list(batch_ids))
            if len(calls) == 2:
                raise m.NcbiError("fixture failure: partial checkpoint")
            return await writer(path, batch_ids)

        attr = {"csv": "_export_csv", "ris": "_export_ris"}[fmt]
        with patch.object(m, attr, flaky):
            with self.assertRaisesRegex(m.NcbiError, "fixture failure"):
                run(m._export_pmids, src, out, fmt, job)
        return src, out, job, calls

    def test_new_ris_checkpoint_stamps_current_version_and_resumes_partially(self):
        ids = ["1", "2", "3", "4"]
        src, out, job, calls = self.partial("ris", ids, fake_ris)
        self.assertEqual(calls, [["1", "2"], ["3", "4"]])  # 夹具：第 2 批失败前已请求

        expected = m._EXPORT_RIS_FORMAT_VERSION
        man = self.manifest(job)
        self.assertEqual(man["format_version"], expected)
        self.assertEqual(man["formatter"], {"format": "ris", "format_version": expected})
        self.assertEqual(man["batches"][0]["status"], "done")
        self.assertEqual(man["batches"][1]["status"], "pending")
        # 元数据也必须带上按格式区分的版本，旧 RIS 状态文件才不会与它绑定。
        state = self.state(job, 0)
        self.assertEqual(state["format_version"], expected)
        self.assertEqual(state["format"], "ris")

        resumed_calls = []

        async def good(path, batch_ids):
            resumed_calls.append(list(batch_ids))
            return await fake_ris(path, batch_ids)

        with patch.object(m, "_export_ris", good):
            r = run(m._resume_export, job)
        self.assertTrue(r["resumed"])
        self.assertEqual(resumed_calls, [["3", "4"]])  # 已完成的第 1 批不重抓
        self.assertEqual(self.manifest(job)["phase"], "published")
        text = Path(out).read_text(encoding="utf-8")
        self.assertEqual([x for x in text.splitlines() if x.startswith("AN  - ")],
                         ["AN  - 1", "AN  - 2", "AN  - 3", "AN  - 4"])

    def test_old_ris_checkpoint_is_rejected_before_mixing_formats(self):
        src, out, job, _ = self.partial("ris", ["1", "2", "3", "4"], fake_ris)
        man = self.manifest(job)
        done_batch = Path(job, *man["batches"][0]["file"].split("/"))
        before = done_batch.read_bytes()

        # 旧 RIS 检查点：格式化器版本仍是旧的 2（本项目自订旧版本号）。
        man["format_version"] = 2
        man["formatter"]["format_version"] = 2
        self.write_manifest(job, man)

        fetched = []

        async def boom(path, batch_ids):
            fetched.append(list(batch_ids))
            raise AssertionError("旧 RIS 检查点被拒绝前不应抓取或混写")

        with patch.object(m, "_export_ris", boom):
            with self.assertRaises(m.NcbiError) as ctx:
                run(m._resume_export, job)
        message = str(ctx.exception)
        self.assertIn("ris", message.lower())
        self.assertIn("不兼容", message)
        self.assertIn("重新建作业", message)
        self.assertEqual(fetched, [])  # 拒绝发生在任何批次抓取之前
        self.assertEqual(done_batch.read_bytes(), before)  # 旧批文件保持原样
        self.assertFalse(Path(out).exists())  # 没有发布混格式的最终文件

    def test_compatible_old_non_ris_checkpoint_still_resumes(self):
        ids = ["1", "2", "3", "4"]
        src, out, job, calls = self.partial("csv", ids, fake_csv)
        expected = m._EXPORT_FORMAT_VERSION
        man = self.manifest(job)
        self.assertEqual(man["formatter"]["format_version"], expected)

        # 模拟旧的非 RIS 检查点：顶层与格式化器版本沿用兼容的 2；同时让整份清单
        # 尚未吸收第 1 批的完成状态，改为从旧增量状态文件恢复。
        man["format_version"] = expected
        man["formatter"]["format_version"] = expected
        man["batches"][0].update({"status": "pending", "sha256": None, "count": None,
                                  "written_records": None, "missing_pmids": [],
                                  "fetched_at": None})
        self.write_manifest(job, man)
        state = self.state(job, 0)
        self.assertEqual(state["format_version"], expected)
        state["format_version"] = 2  # 本项目自订：旧非 RIS 状态文件版本
        self.write_state(job, state, 0)

        resumed_calls = []

        async def good(path, batch_ids):
            resumed_calls.append(list(batch_ids))
            return await fake_csv(path, batch_ids)

        with patch.object(m, "_export_csv", good):
            r = run(m._resume_export, job)
        self.assertTrue(r["resumed"])
        self.assertEqual(resumed_calls, [["3", "4"]])  # 第 1 批由兼容的旧状态恢复
        self.assertEqual(self.manifest(job)["phase"], "published")
        rows = list(csv.reader(Path(out).read_text(encoding="utf-8-sig").splitlines()))
        self.assertEqual([row[0] for row in rows[1:]], ids)

    def test_missing_old_ris_state_is_not_reused_under_new_version(self):
        # 新版本 RIS 作业的元数据只认当前版本：把状态文件伪造成旧版本时，
        # 该批必须退回 pending 重抓，而不是把旧摘要格式混进新文件。
        src, out, job, _ = self.partial("ris", ["1", "2", "3", "4"], fake_ris)
        man = self.manifest(job)
        man["batches"][0].update({"status": "pending", "sha256": None, "count": None,
                                  "written_records": None, "missing_pmids": [],
                                  "fetched_at": None})
        self.write_manifest(job, man)
        state = self.state(job, 0)
        state["format_version"] = 2  # 本项目自订：旧 RIS 状态版本
        self.write_state(job, state, 0)

        resumed_calls = []

        async def good(path, batch_ids):
            resumed_calls.append(list(batch_ids))
            return await fake_ris(path, batch_ids)

        with patch.object(m, "_export_ris", good):
            run(m._resume_export, job)
        self.assertEqual(resumed_calls, [["1", "2"], ["3", "4"]])  # 旧状态未被复用
        text = Path(out).read_text(encoding="utf-8")
        self.assertEqual(text.count("AN  - "), 4)


if __name__ == "__main__":
    unittest.main()
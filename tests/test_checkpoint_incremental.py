"""逐批增量状态持久化的回归测试。

本模块出现的所有 PMID、批大小、批次数、字节上限与预期值，都是本项目自订测试夹具
（project-defined fixture），不是文献数据、NCBI 实测值或真实作业数据。其中的大小
阈值只用来发现「每批重写整份清单」这类复杂度回退，不代表线上作业的实际规模。

运行：python -I tests/test_checkpoint_incremental.py
测试直接加载仓库源码，覆盖真实的作业执行、恢复与取消路径。
"""
import csv
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
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


def csv_rows(ids, title):
    # 夹具：10 列与 _PMIDS_CSV_HEADER 对齐；title 用于制造不同的批文件内容。
    return [[p, "", title, "", "", "", "", "", "", ""] for p in ids]


def rewrite_batch_csv(path, ids, title):
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f, lineterminator="\r\n").writerows(csv_rows(ids, title))


async def fake_csv(path, ids, title="fixture"):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(list(m._PMIDS_CSV_HEADER))
        w.writerows(csv_rows(ids, title))
    return len(ids), []


class WriteRecorder:
    """记录 _write_json_atomic 的路径、线程和序列化大小，然后真正写入。"""

    def __init__(self, real):
        self.real = real
        self.main = threading.get_ident()
        self.calls = []

    def __call__(self, path, obj):
        self.calls.append({
            "path": str(path),
            "thread": threading.get_ident(),
            "bytes": len(json.dumps(obj, ensure_ascii=False, indent=2)),
            "phase": obj.get("phase") if isinstance(obj, dict) else None,
        })
        return self.real(path, obj)


def store_recorder(calls):
    real = m._store_batch

    async def rec(jobdir, manifest, batch):
        calls.append(batch["batch_id"])
        return await real(jobdir, manifest, batch)

    return rec


class Base(unittest.TestCase):
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
        path = self.p("ids.txt")
        Path(path).write_text(" ".join(ids) + ("\n" if ids else ""), encoding="utf-8")
        return path

    def manifest(self, job):
        return json.loads(Path(job, "manifest.json").read_text(encoding="utf-8"))

    def write_manifest(self, job, doc):
        Path(job, "manifest.json").write_text(json.dumps(doc, ensure_ascii=False),
                                              encoding="utf-8")

    def reset_batches(self, man):
        for b in man["batches"]:
            b.update({"status": "pending", "sha256": None, "count": None,
                      "written_records": None, "missing_pmids": [], "fetched_at": None})

    def failed_job(self, ids, fail_on=2):
        """跑到第 fail_on 批时抛错，留下可恢复作业；夹具错误文本固定。"""
        src, out, job = self.source(ids), self.p("out.txt"), self.p("job")
        real = m._store_batch
        calls = []

        async def flaky(jobdir, manifest, batch):
            calls.append(batch["batch_id"])
            if len(calls) == fail_on:
                raise m.NcbiError("fixture failure")
            return await real(jobdir, manifest, batch)

        with patch.object(m, "_store_batch", flaky):
            with self.assertRaisesRegex(m.NcbiError, "fixture failure"):
                run(m._export_pmids, src, out, "pmids", job)
        return src, out, job


class PersistenceBoundsTests(Base):
    def test_manifest_snapshots_constant_and_state_files_bounded(self):
        # 夹具：20 个 PMID、批大小 2 → 10 批；阈值只作回归护栏。
        ids = [str(i) for i in range(1, 21)]
        src, out, job = self.source(ids), self.p("out.txt"), self.p("job")
        rec = WriteRecorder(m._write_json_atomic)
        with patch.object(m, "_write_json_atomic", rec):
            r = run(m._export_pmids, src, out, "pmids", job)

        manifest_calls = [c for c in rec.calls
                          if os.path.basename(c["path"]) == "manifest.json"]
        state_calls = [c for c in rec.calls
                       if Path(c["path"]).parent.name == m._EXPORT_STATE_DIR]
        self.assertEqual(len(manifest_calls), 3)      # 夹具：新建 + 开始 + 成功
        self.assertEqual(len(state_calls), 10)        # 夹具：每批一次
        self.assertEqual(
            len(list(Path(job, m._EXPORT_STATE_DIR).glob("batch-*.json"))), 10)
        self.assertTrue(all(c["thread"] != rec.main for c in rec.calls))
        self.assertLessEqual(max(c["bytes"] for c in state_calls), 1500)  # 夹具上限
        self.assertGreater(max(c["bytes"] for c in manifest_calls),
                           max(c["bytes"] for c in state_calls))
        for c in state_calls:
            doc = json.loads(Path(c["path"]).read_text(encoding="utf-8"))
            self.assertEqual(doc["schema"], m._EXPORT_STATE_SCHEMA)
            self.assertNotIn("pmids", doc)
            self.assertNotIn("batches", doc)
        self.assertEqual(Path(out).read_text(encoding="utf-8").splitlines(), ids)
        self.assertEqual(self.manifest(job)["phase"], "published")
        self.assertIsInstance(self.manifest(job)["state_epoch"], str)

    def test_resume_loads_manifest_and_writes_snapshots_off_loop(self):
        src, out, job = self.failed_job(["1", "2", "3", "4"])
        real_load = m._load_job_manifest
        load_threads = []

        def load(jobdir):
            load_threads.append(threading.get_ident())
            return real_load(jobdir)

        rec = WriteRecorder(m._write_json_atomic)
        with patch.object(m, "_load_job_manifest", load), \
                patch.object(m, "_write_json_atomic", rec):
            r = run(m._resume_export, job)
        self.assertTrue(r["resumed"])
        self.assertTrue(load_threads)
        self.assertTrue(all(t != rec.main for t in load_threads))
        self.assertTrue(rec.calls)
        self.assertTrue(all(c["thread"] != rec.main for c in rec.calls))


class IncrementalResumeTests(Base):
    def test_resume_adopts_valid_state_without_refetch(self):
        src, out, job = self.failed_job(["1", "2", "3", "4"])
        man = self.manifest(job)
        self.assertEqual([b["status"] for b in man["batches"]], ["done", "pending"])
        self.reset_batches(man)                    # 模拟崩溃：清单仍是开始快照
        man["last_error"] = None
        self.write_manifest(job, man)

        stores = []
        with patch.object(m, "_store_batch", store_recorder(stores)):
            r = run(m._resume_export, job)
        self.assertEqual(stores, [1])              # 只抓缺失的第 1 批
        self.assertTrue(r["resumed"])
        self.assertEqual(Path(out).read_text(encoding="utf-8").splitlines(),
                         ["1", "2", "3", "4"])

    def test_corrupt_state_only_refetches_that_batch(self):
        ids = ["1", "2", "3", "4", "5", "6"]
        src, out, job = self.failed_job(ids, fail_on=3)
        man = self.manifest(job)
        self.assertEqual([b["status"] for b in man["batches"]],
                         ["done", "done", "pending"])
        self.reset_batches(man)
        self.write_manifest(job, man)
        Path(job, m._EXPORT_STATE_DIR, "batch-00001.json").write_text(
            "{broken", encoding="utf-8")

        stores = []
        with patch.object(m, "_store_batch", store_recorder(stores)):
            r = run(m._resume_export, job)
        self.assertEqual(stores, [1, 2])           # 0 用状态，1 损坏重抓，2 本就没做
        self.assertEqual(Path(out).read_text(encoding="utf-8").splitlines(), ids)

    def test_state_path_rejects_resolved_alias(self):
        # 本项目自订夹具：模拟路径解析穿过符号链接，不依赖 Windows 建链权限。
        job = self.p("job")
        with patch.object(m, "_job_path", return_value=self.p("other.json")):
            with self.assertRaisesRegex(m.NcbiError, "符号链接"):
                m._batch_state_path(job, 0)
        self.assertEqual(m._batch_state_path(job, 0),
                         os.path.realpath(os.path.join(job, "state", "batch-00000.json")))

    def test_deeply_nested_state_is_refetched(self):
        # 本项目自订夹具：畸形嵌套状态不得阻断整个作业恢复。
        src, out, job = self.failed_job(["1", "2", "3", "4"])
        man = self.manifest(job)
        self.reset_batches(man)
        self.write_manifest(job, man)
        Path(job, "state", "batch-00000.json").write_text("[" * 2000, encoding="utf-8")
        stores = []
        with patch.object(m, "_store_batch", store_recorder(stores)):
            run(m._resume_export, job)
        self.assertEqual(stores, [0, 1])
        self.assertTrue(Path(out).is_file())

    def test_state_write_failure_falls_back_to_manifest(self):
        # 本项目自订夹具：小状态写失败，普通异常的完整快照仍能恢复。
        real = m._write_json_atomic
        def flaky(path, obj):
            if Path(path).parent.name == m._EXPORT_STATE_DIR:
                raise OSError("fixture state failure")
            return real(path, obj)
        with patch.object(m, "_write_json_atomic", flaky):
            src, out, job = self.failed_job(["1", "2", "3", "4"])
        self.assertTrue(any("增量状态写入失败" in w for w in self.manifest(job)["warnings"]))
        stores = []
        with patch.object(m, "_store_batch", store_recorder(stores)):
            run(m._resume_export, job)
        self.assertEqual(stores, [1])

    def test_state_is_bound_to_fixed_plan_epoch(self):
        src, out, job = self.failed_job(["1", "2", "3", "4"])
        man = self.manifest(job)
        self.reset_batches(man)
        self.write_manifest(job, man)
        state_path = Path(job, m._EXPORT_STATE_DIR, "batch-00000.json")
        doc = json.loads(state_path.read_text(encoding="utf-8"))
        doc["state_epoch"] = "fixture-other-epoch"
        state_path.write_text(json.dumps(doc), encoding="utf-8")

        stores = []
        with patch.object(m, "_store_batch", store_recorder(stores)):
            r = run(m._resume_export, job)
        self.assertEqual(stores, [0, 1])           # 世代不符 → 两批都重抓
        self.assertTrue(Path(out).is_file())

    def test_stale_state_cannot_override_newer_manifest_snapshot(self):
        ids = ["1", "2", "3", "4"]
        src, out, job = self.source(ids), self.p("out.csv"), self.p("job")
        with patch.object(m, "_export_csv", fake_csv):
            run(m._export_pmids, src, out, "csv", job)
        man = self.manifest(job)
        Path(out).unlink()

        batch0 = Path(job, *man["batches"][0]["file"].split("/"))
        rewrite_batch_csv(batch0, man["batches"][0]["pmids"], "newer-snapshot")
        b = man["batches"][0]
        b["sha256"] = m._sha256_file(batch0)       # 整份快照记录了更新的批文件
        b["count"] = len(b["pmids"])
        b["written_records"] = len(b["pmids"])
        b["missing_pmids"] = []
        man["phase"] = "metadata"
        man["output"] = {"path": man["output"]["path"], "sha256": None,
                         "bytes": None, "published_at": None}
        self.write_manifest(job, man)

        state_path = Path(job, m._EXPORT_STATE_DIR, "batch-00000.json")
        stale = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotEqual(stale["sha256"], b["sha256"])

        stores = []
        with patch.object(m, "_store_batch", store_recorder(stores)):
            r = run(m._resume_export, job)
        self.assertEqual(stores, [])               # 旧状态不得触发重抓
        self.assertTrue(Path(out).is_file())
        refreshed = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(refreshed["sha256"], b["sha256"])
        self.assertEqual(self.manifest(job)["phase"], "published")

    def test_failure_snapshot_records_done_batch_and_state(self):
        src, out, job = self.failed_job(["1", "2", "3", "4"])
        man = self.manifest(job)
        self.assertEqual(man["phase"], "metadata")
        self.assertEqual([b["status"] for b in man["batches"]], ["done", "pending"])
        self.assertIn("fixture failure", man["last_error"])
        self.assertTrue(Path(job, m._EXPORT_STATE_DIR, "batch-00000.json").is_file())
        self.assertFalse(Path(job, m._EXPORT_STATE_DIR, "batch-00001.json").exists())
        self.assertFalse(Path(job, ".resume.lock").exists())

    def test_resume_accepts_manifest_without_state_epoch(self):
        # 兼容旧作业：没有 state_epoch 的整份清单仍可恢复。
        ids = ["1", "2", "3", "4"]
        src, out, job = self.source(ids), self.p("out.txt"), self.p("job")
        run(m._export_pmids, src, out, "pmids", job)
        man = self.manifest(job)
        Path(out).unlink()
        man.pop("state_epoch", None)
        man["phase"] = "metadata"
        man["output"] = {"path": man["output"]["path"], "sha256": None,
                         "bytes": None, "published_at": None}
        self.write_manifest(job, man)

        stores = []
        with patch.object(m, "_store_batch", store_recorder(stores)):
            r = run(m._resume_export, job)
        self.assertEqual(stores, [])
        self.assertTrue(Path(out).is_file())
        self.assertTrue(r["resumed"])


class CancellationAndPublishTests(Base):
    def test_cancellation_preserves_completed_batch_state_and_lock_release(self):
        ids = ["1", "2", "3", "4"]
        src, out, job = self.source(ids), self.p("out.txt"), self.p("job")
        real_save = m._save_batch_state
        saved = []

        async def call():
            async def cancelling(jobdir, manifest, batch):
                ok = await real_save(jobdir, manifest, batch)
                if not saved:
                    saved.append(batch["batch_id"])
                    scope.cancel()
                    await anyio.lowlevel.checkpoint()
                return ok

            with patch.object(m, "_save_batch_state", cancelling):
                with anyio.CancelScope() as scope:
                    await m._export_pmids(src, out, "pmids", job)

        anyio.run(call)
        self.assertEqual(saved, [0])
        self.assertFalse(Path(out).exists())
        self.assertFalse(Path(job, ".resume.lock").exists())
        man = self.manifest(job)
        self.assertEqual(man["phase"], "metadata")
        self.assertEqual([b["status"] for b in man["batches"]], ["pending", "pending"])
        self.assertTrue(Path(job, m._EXPORT_STATE_DIR, "batch-00000.json").is_file())
        self.assertFalse(Path(job, m._EXPORT_STATE_DIR, "batch-00001.json").exists())

        stores = []
        with patch.object(m, "_store_batch", store_recorder(stores)):
            r = run(m._resume_export, job)
        self.assertEqual(stores, [1])              # 第 0 批直接复用状态
        self.assertTrue(r["resumed"])
        self.assertEqual(Path(out).read_text(encoding="utf-8").splitlines(), ids)

    def test_publication_success_after_manifest_write_failure_is_truthful(self):
        ids = ["1", "2", "3", "4"]
        src, out, job = self.source(ids), self.p("out.txt"), self.p("job")
        real = m._write_json_atomic

        def flaky(path, obj):
            if isinstance(obj, dict) and obj.get("phase") == "published":
                raise OSError("fixture disk failure")
            return real(path, obj)

        with patch.object(m, "_write_json_atomic", flaky):
            r = run(m._export_pmids, src, out, "pmids", job)
        self.assertTrue(Path(out).is_file())
        self.assertEqual(Path(out).read_text(encoding="utf-8").splitlines(), ids)
        self.assertEqual(r["manifest_status"], "failed")
        self.assertEqual(r["sha256"], m._sha256_file(out))
        self.assertTrue(any("数据已发布" in w for w in r["warnings"]))
        self.assertEqual(
            len(list(Path(job, m._EXPORT_STATE_DIR).glob("batch-*.json"))), 2)
        self.assertEqual(self.manifest(job)["phase"], "metadata")


if __name__ == "__main__":
    unittest.main(verbosity=2)
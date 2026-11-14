"""PMID 清单解析、离线差集与可续跑题录导出的回归测试。

所有 PMID、批大小、计数与格式内容均为本项目自订测试输入，不是文献或 NCBI 实测数据。
运行：python -I tests/test_pmid_files.py
测试直接加载仓库源码，覆盖实际集成的清单与续跑入口。
"""
import csv
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

import anyio

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("new_subject", ROOT / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


def write(path, text, bom=False):
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    Path(path).write_bytes(data)
    return str(path)


async def fake_csv(path, ids, missing_metadata=()):
    missing = set(missing_metadata)
    rows = [list(m._PMIDS_CSV_HEADER)]
    for p in ids:
        row = [p] + [""] * 9
        if p in missing:
            row[-1] = "元数据缺失"
        rows.append(row)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)
    return len(ids) - len(missing), [p for p in ids if p in missing]


async def fake_ris(path, ids, reverse=True, missing=()):
    miss = set(missing)
    order = list(reversed(ids)) if reverse else list(ids)
    got = [p for p in order if p not in miss]
    blocks = [f"TY  - JOUR\nAN  - {p}\nER  - " for p in got]
    Path(path).write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")
    return len(got), [p for p in ids if p not in set(got)]


async def fake_medline(path, ids, reverse=True, missing=()):
    miss = set(missing)
    order = list(reversed(ids)) if reverse else list(ids)
    got = [p for p in order if p not in miss]
    blocks = [f"PMID- {p}\nTI  - Title" for p in got]
    Path(path).write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")
    return len(got), [p for p in ids if p not in set(got)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name

    def p(self, name):
        return os.path.join(self.d, name)

    def source(self, ids, name="ids.txt", sep=" "):
        return write(self.p(name), sep.join(ids) + ("\n" if ids else ""))

    def manifest(self, job):
        return json.loads(Path(job, "manifest.json").read_text(encoding="utf-8"))


class ParseTests(Base):
    def test_text_accepts_bom_blank_comma_and_normalizes(self):
        src = write(self.p("ids.txt"), " 123, 0456 \n\n789\n", bom=True)
        r = m._read_pmid_file(src)
        self.assertEqual(r["ids"], ["123", "456", "789"])
        self.assertEqual(r["raw_count"], 3)
        self.assertEqual(r["schema"], m._PMID_TEXT_SCHEMA)
        self.assertEqual(len(r["sha256"]), 64)
        self.assertEqual(r["path"], os.path.abspath(src))

    def test_empty_file(self):
        r = m._read_pmid_file(write(self.p("empty.txt"), ""))
        self.assertEqual(r["ids"], [])
        self.assertEqual(r["raw_count"], 0)

    def test_grouped_thousands_reports_line(self):
        src = write(self.p("bad.txt"), "111\n1,234,567\n")
        with self.assertRaisesRegex(m.NcbiError, "第 2 行.*千位分隔符"):
            m._read_pmid_file(src)

    def test_bad_token_reports_line(self):
        src = write(self.p("bad2.txt"), "111\nPMC123\n")
        with self.assertRaisesRegex(m.NcbiError, "第 2 行"):
            m._read_pmid_file(src)

    def test_json_schemas_candidate_only(self):
        for schema in ("pubmed.links.v1", "pubmed.diff.v1", "pubmed.snowball.v1"):
            src = write(self.p(schema + ".json"), json.dumps(
                {"schema": schema, "candidate_pmids": ["001", 2, "2"], "edges": [9, 9]}))
            r = m._read_pmid_file(src)
            self.assertEqual(r["ids"], ["1", "2"])
            self.assertEqual(r["raw_count"], 3)
            self.assertEqual(r["schema"], schema)

    def test_candidate_schemas_cannot_stand_in_for_complete_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            for schema in ('pubmed.links.v1','pubmed.snowball.v1'):
                p=Path(folder)/'candidates.json'
                p.write_text(json.dumps({'schema':schema,'candidate_pmids':['3']}),encoding='utf-8')
                with self.assertRaisesRegex(m.NcbiError,'完整'):
                    m._read_pmid_file(str(p),baseline=True)

    def test_paginated_link_candidates_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'page.json'
            p.write_text(json.dumps({'schema':'pubmed.links.v1','candidate_pmids':['1'],
                                    'pagination':{'applies_to':'returned_json','offset':1,'limit':1}}), encoding='utf-8')
            with self.assertRaisesRegex(m.NcbiError, '分页'):
                m._read_pmid_file(str(p))

    def test_json_unknown_schema(self):
        src = write(self.p("x.json"), json.dumps({"schema": "other", "candidate_pmids": ["1"]}))
        with self.assertRaisesRegex(m.NcbiError, "未知的 PMID 清单 schema"):
            m._read_pmid_file(src)

    def test_json_missing_candidate(self):
        src = write(self.p("x.json"), json.dumps({"schema": "pubmed.links.v1"}))
        with self.assertRaisesRegex(m.NcbiError, "缺少 candidate_pmids"):
            m._read_pmid_file(src)

    def test_json_bad_candidate_index(self):
        src = write(self.p("x.json"), json.dumps(
            {"schema": "pubmed.links.v1", "candidate_pmids": ["1", "PMC2"]}))
        with self.assertRaisesRegex(m.NcbiError, "第 1 个元素"):
            m._read_pmid_file(src)

    def test_baseline_rejects_diff(self):
        src = write(self.p("x.json"), json.dumps({"schema": "pubmed.diff.v1", "candidate_pmids": ["1"]}))
        m._read_pmid_file(src)
        with self.assertRaisesRegex(m.NcbiError, "baseline"):
            m._read_pmid_file(src, baseline=True)

    def test_export_max(self):
        src = write(self.p("ids.txt"), "1 2 3\n")
        with patch.object(m, "EXPORT_MAX", 2):
            with self.assertRaisesRegex(m.NcbiError, "安全上限"):
                m._read_pmid_file(src)


class DiffTests(Base):
    def test_diff_order_counts_bindings(self):
        b = write(self.p("b.txt"), "1 2 3 3\n")
        c = write(self.p("c.txt"), "3 4 2 5\n")
        out = self.p("diff.json")
        r = m._diff_files(b, c, out)
        doc = json.loads(Path(out).read_text(encoding="utf-8"))
        self.assertEqual(doc["schema"], "pubmed.diff.v1")
        self.assertEqual(doc["candidate_pmids"], ["4", "5"])
        self.assertEqual(doc["removed_pmids"], ["1"])
        self.assertEqual(doc["common_pmids"], ["2", "3"])
        self.assertEqual(doc["counts"]["baseline_unique"], 3)
        self.assertEqual(doc["counts"]["current_unique"], 4)
        self.assertEqual(doc["inputs"]["baseline"]["raw_count"], 4)
        self.assertEqual(len(doc["inputs"]["current"]["sha256"]), 64)
        self.assertEqual(r["candidate_pmids"], 2)
        self.assertEqual(doc["added_count"], 2)
        self.assertEqual(r["interpretation"], doc["interpretation"])
        self.assertIn("不代表文献被数据库删除", r["interpretation"])
        self.assertIn("补检入库区间", r["interpretation"])

    def test_diff_empty(self):
        b = write(self.p("b.txt"), "")
        c = write(self.p("c.txt"), "")
        out = self.p("d.json")
        m._diff_files(b, c, out)
        doc = json.loads(Path(out).read_text(encoding="utf-8"))
        self.assertEqual(doc["candidate_pmids"], [])
        self.assertEqual(doc["removed_pmids"], [])
        self.assertEqual(doc["common_pmids"], [])

    def test_diff_no_clobber_and_not_input(self):
        b = write(self.p("b.txt"), "1\n")
        c = write(self.p("c.txt"), "2\n")
        with self.assertRaisesRegex(m.NcbiError, "同路径"):
            m._diff_files(b, c, b)
        existing = write(self.p("exists.json"), "{}")
        with self.assertRaisesRegex(m.NcbiError, "已存在"):
            m._diff_files(b, c, existing)

    def test_diff_baseline_diff_rejected(self):
        b = write(self.p("b.json"), json.dumps({"schema": "pubmed.diff.v1", "candidate_pmids": ["1"]}))
        c = write(self.p("c.txt"), "1\n")
        with self.assertRaisesRegex(m.NcbiError, "baseline"):
            m._diff_files(b, c, self.p("d.json"))


class ExportTests(Base):
    def setUp(self):
        super().setUp()
        self.sizes = patch.object(m, "_EXPORT_BATCH_SIZE",
                                  {"pmids": 2, "csv": 2, "ris": 2, "medline": 2})
        self.sizes.start()
        self.addCleanup(self.sizes.stop)

    def test_pmids_order_batches_and_manifest(self):
        src = self.source(["3", "1", "2", "1"])
        out, job = self.p("out.txt"), self.p("job")
        r = run(m._export_pmids, src, out, "pmids", job)
        self.assertEqual(Path(out).read_text(encoding="utf-8").splitlines(), ["3", "1", "2"])
        self.assertEqual(r["count"], 3)
        self.assertEqual(r["written_records"], 3)
        self.assertTrue(r["resumable"])
        man = self.manifest(job)
        self.assertEqual(man["phase"], "published")
        self.assertEqual(man["input"]["unique_count"], 3)
        self.assertEqual(man["input"]["raw_count"], 4)
        self.assertEqual(man["source_query_status"], "missing")
        self.assertEqual(len(man["batches"]), 2)
        self.assertTrue(all(b["status"] == "done" and len(b["sha256"]) == 64 for b in man["batches"]))
        self.assertEqual(man["output"]["sha256"], r["sha256"])

    def test_csv_single_bom_header_and_order(self):
        ids = ["1", "2", "3", "4", "5"]
        src = self.source(ids)
        out, job = self.p("out.csv"), self.p("job")
        with patch.object(m, "_export_csv", fake_csv):
            r = run(m._export_pmids, src, out, "csv", job)
        raw = Path(out).read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.count(b"\xef\xbb\xbf"), 1)
        rows = list(csv.reader(Path(out).read_text(encoding="utf-8-sig").splitlines()))
        self.assertEqual(rows[0], list(m._PMIDS_CSV_HEADER))
        self.assertEqual([row[0] for row in rows[1:]], ids)
        self.assertEqual(len(rows), 6)
        self.assertEqual(r["written_records"], 5)
        man = self.manifest(job)
        for b in man["batches"]:
            batch_path = Path(job, *b["file"].split("/"))
            self.assertFalse(batch_path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_csv_missing_metadata_rows_and_list(self):
        src = self.source(["1", "2", "3", "4"])
        out = self.p("out.csv")
        async def csv_missing(path, ids):
            return await fake_csv(path, ids, {"2", "4"})
        with patch.object(m, "_export_csv", csv_missing):
            r = run(m._export_pmids, src, out, "csv", self.p("job"))
        self.assertEqual(r["missing_pmids"], ["2", "4"])
        self.assertEqual(r["written_records"], 4)
        rows = list(csv.reader(Path(out).read_text(encoding="utf-8-sig").splitlines()))
        self.assertEqual([row[0] for row in rows[1:]], ["1", "2", "3", "4"])

    def test_ris_reorders_and_reports_missing(self):
        src = self.source(["1", "2", "3", "4"])
        out, job = self.p("out.ris"), self.p("job")
        async def ris_missing(path, ids):
            return await fake_ris(path, ids, missing={"2"})
        with patch.object(m, "_export_ris", ris_missing):
            r = run(m._export_pmids, src, out, "ris", job)
        text = Path(out).read_text(encoding="utf-8")
        self.assertEqual([x for x in text.splitlines() if x.startswith("AN  - ")], ["AN  - 1", "AN  - 3", "AN  - 4"])
        self.assertEqual(r["missing_pmids"], ["2"])
        self.assertEqual(r["written_records"], 3)
        man = self.manifest(job)
        self.assertEqual(man["batches"][0]["missing_pmids"], ["2"])
        self.assertEqual(man["batches"][1]["missing_pmids"], [])

    def test_medline_reorders_and_reports_missing(self):
        src = self.source(["1", "2", "3", "4"])
        out, job = self.p("out.nbib"), self.p("job")
        async def medline_missing(path, ids):
            return await fake_medline(path, ids, missing={"3"})
        with patch.object(m, "_export_medline", medline_missing):
            r = run(m._export_pmids, src, out, "medline", job)
        text = Path(out).read_text(encoding="utf-8")
        self.assertEqual([x for x in text.splitlines() if x.startswith("PMID- ")], ["PMID- 1", "PMID- 2", "PMID- 4"])
        self.assertEqual(r["missing_pmids"], ["3"])
        self.assertEqual(r["written_records"], 3)

    def test_empty_csv_header_only(self):
        src = self.source([])
        out, job = self.p("empty.csv"), self.p("job")
        boom = AsyncMock(side_effect=AssertionError("writer must not be called for empty input"))
        with patch.object(m, "_export_csv", boom):
            r = run(m._export_pmids, src, out, "csv", job)
        raw = Path(out).read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.count(b"\xef\xbb\xbf"), 1)
        rows = list(csv.reader(Path(out).read_text(encoding="utf-8-sig").splitlines()))
        self.assertEqual(rows, [list(m._PMIDS_CSV_HEADER)])
        self.assertEqual((r["count"], r["written_records"], r["missing_pmids"]), (0, 0, []))
        boom.assert_not_awaited()

    def test_no_checkpoint_temp_dir_removed(self):
        src = self.source(["1", "2"])
        outdir = self.p("outdir")
        os.makedirs(outdir)
        with patch.object(m, "_export_csv", fake_csv):
            r = run(m._export_pmids, src, os.path.join(outdir, "o.csv"), "csv", "")
        self.assertFalse(r["resumable"])
        self.assertIsNone(r["jobdir"])
        self.assertEqual(sorted(os.listdir(outdir)), ["o.csv"])

    def test_export_never_overwrites_and_rejects_same_path(self):
        src = self.source(["1"])
        out = write(self.p("keep.txt"), "KEEP")
        with self.assertRaisesRegex(m.NcbiError, "已存在"):
            run(m._export_pmids, src, out, "pmids", "")
        self.assertEqual(Path(out).read_text(encoding="utf-8"), "KEEP")
        with self.assertRaisesRegex(m.NcbiError, "同路径"):
            run(m._export_pmids, src, src, "pmids", "")

    def _failed_job(self, fmt="csv", ids=("1", "2", "3", "4"), fail_on=2, writer=None):
        src, out, job = self.source(ids), self.p("out." + fmt), self.p("job")
        calls = []

        async def flaky(path, batch_ids):
            calls.append(list(batch_ids))
            if len(calls) == fail_on:
                raise m.NcbiError("simulated network failure")
            if writer:
                return await writer(path, batch_ids)
            return await fake_csv(path, batch_ids)

        with patch.object(m, "_export_csv", flaky):
            with self.assertRaisesRegex(m.NcbiError, "simulated network failure"):
                run(m._export_pmids, src, out, fmt, job)
        return src, out, job, calls

    def test_csv_direct_and_checkpoint_bytes_match(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);src=root/'ids.txt';src.write_text('1 2 3')
            async def summary(ids, **kwargs):
                return [(pid,{'uid':pid,'title':'fixture','authors':[]}) for pid in ids]
            with patch.object(m,'_esummary_dicts',summary):
                run(m._export_csv,str(root/'direct.csv'),['1','2','3'])
                run(m._export_pmids,str(src),str(root/'checkpoint.csv'),'csv',str(root/'job'))
            self.assertEqual((root/'direct.csv').read_bytes(),(root/'checkpoint.csv').read_bytes())

    def test_reserved_or_duplicate_batch_path_is_rejected(self):
        src,out,job,calls=self._failed_job()
        original=self.manifest(job)
        for rel in ('manifest.json', original['batches'][0]['file']):
            altered=json.loads(json.dumps(original))
            altered['batches'][1]['file']=rel
            altered['batches'][1]['path']=rel
            Path(job,'manifest.json').write_text(json.dumps(altered),encoding='utf-8')
            with self.assertRaisesRegex(m.NcbiError,'固定批次|共用'):
                run(m._resume_export,job)
            self.assertFalse(Path(out).exists())

    def test_published_output_survives_manifest_update_failure(self):
        src, out, job, calls = self._failed_job()
        real = m._write_json_atomic
        def fail_after_publish(path, manifest):
            if manifest.get('phase') == 'published':
                raise OSError('fixture disk failure')
            return real(path, manifest)
        with patch.object(m, '_write_json_atomic', fail_after_publish), patch.object(m, '_export_csv', fake_csv):
            result = run(m._resume_export, job)
        self.assertTrue(Path(out).is_file())
        self.assertEqual(result['manifest_status'], 'failed')
        self.assertEqual(result['sha256'], m._sha256_file(out))
        self.assertTrue(any('数据已发布' in item for item in result['warnings']))

    def test_resume_after_fetch_failure(self):
        src, out, job, calls = self._failed_job()
        self.assertTrue(os.path.isdir(job))
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.exists(os.path.join(job, ".resume.lock")))
        man = self.manifest(job)
        self.assertEqual(man["phase"], "metadata")
        self.assertEqual(man["batches"][0]["status"], "done")
        self.assertEqual(man["batches"][1]["status"], "pending")
        self.assertIn("simulated", man["last_error"])
        with patch.object(m, "_export_csv", fake_csv):
            r = run(m._resume_export, job)
        self.assertTrue(r["resumed"])
        rows = list(csv.reader(Path(out).read_text(encoding="utf-8-sig").splitlines()))
        self.assertEqual([x[0] for x in rows[1:]], ["1", "2", "3", "4"])
        self.assertEqual(self.manifest(job)["phase"], "published")

    def test_resume_corrupt_batch_refetches(self):
        src, out, job, calls = self._failed_job()
        man = self.manifest(job)
        corrupt = Path(job, *man["batches"][0]["file"].split("/"))
        corrupt.write_bytes(b"garbage\n")
        refetch_calls = []

        async def good(path, ids):
            refetch_calls.append(list(ids))
            return await fake_csv(path, ids)

        with patch.object(m, "_export_csv", good):
            r = run(m._resume_export, job)
        self.assertEqual(refetch_calls[0], ["1", "2"])
        self.assertEqual(len(refetch_calls), 2)
        rows = list(csv.reader(Path(out).read_text(encoding="utf-8-sig").splitlines()))
        self.assertEqual([x[0] for x in rows[1:]], ["1", "2", "3", "4"])
        self.assertTrue(any("校验失败" in w for w in self.manifest(job)["warnings"]))

    def test_batch_order_and_ris_terminator_are_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)/'batch.ris'
            p.write_text('TY  - JOUR\nAN  - 1\n', encoding='utf-8')
            with self.assertRaisesRegex(m.NcbiError, '完整题录'):
                m._inspect_batch_file(str(p), 'ris', ['1'])
            p.write_text('TY  - JOUR\nAN  - 2\nER  - \n\nTY  - JOUR\nAN  - 1\nER  - \n', encoding='utf-8')
            batch={'file':'batch.ris','path':'batch.ris','pmids':['1','2'], 'sha256':m._sha256_file(str(p)),
                   'written_records':2,'count':2,'missing_pmids':[]}
            with self.assertRaisesRegex(m.NcbiError, '顺序'):
                m._verify_done_batch(folder, {'formatter':{'format':'ris'}}, batch)

    def test_resume_rejects_traversal(self):
        src, out, job, calls = self._failed_job()
        man = self.manifest(job)
        man["batches"][0]["file"] = "../escape.txt"
        man["batches"][0]["path"] = "../escape.txt"
        Path(job, "manifest.json").write_text(json.dumps(man), encoding="utf-8")
        with self.assertRaisesRegex(m.NcbiError, "越出作业目录"):
            run(m._resume_export, job)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(job), "escape.txt")))
        self.assertFalse(os.path.exists(os.path.join(job, ".resume.lock")))

    def test_resume_concurrent_lock(self):
        src, out, job, calls = self._failed_job()
        lock = os.path.join(job, ".resume.lock")
        Path(lock).write_text("other\n", encoding="utf-8")
        with self.assertRaisesRegex(m.NcbiError, "另一个进程"):
            run(m._resume_export, job)
        os.unlink(lock)

    def test_resume_input_changed(self):
        src, out, job, calls = self._failed_job()
        write(src, "9 8 7 6\n")
        with self.assertRaisesRegex(m.NcbiError, "输入文件已变化"):
            run(m._resume_export, job)

    def test_resume_missing_input_proceeds_with_warning(self):
        src, out, job, calls = self._failed_job()
        os.unlink(src)
        with patch.object(m, "_export_csv", fake_csv):
            r = run(m._resume_export, job)
        self.assertTrue(any("input_source_missing" in w for w in r["warnings"]))
        self.assertTrue(os.path.exists(out))

    def test_resume_version_and_format_mismatch(self):
        src, out, job, calls = self._failed_job()
        original = self.manifest(job)
        cases = [("tool_version", "0.0.0"), ("job_version", 999),
                 ("schema", "other"), ("formatter", {"format": "xml", "format_version": 1}),
                 ("formatter", {"format": "csv", "format_version": 999})]
        for key, value in cases:
            man = json.loads(json.dumps(original))
            man[key] = value
            Path(job, "manifest.json").write_text(json.dumps(man), encoding="utf-8")
            with self.subTest(key=key, value=value):
                with self.assertRaises(m.NcbiError):
                    run(m._resume_export, job)
        Path(job, "manifest.json").write_text(json.dumps(original), encoding="utf-8")

    def test_resume_existing_output_never_overwritten(self):
        src, out, job, calls = self._failed_job()
        Path(out).write_bytes(b"KEEP")
        with self.assertRaisesRegex(m.NcbiError, "已存在"):
            run(m._resume_export, job)
        self.assertEqual(Path(out).read_bytes(), b"KEEP")

    def test_resume_only_metadata_phase(self):
        src, out, job, calls = self._failed_job()
        man = self.manifest(job)
        man["phase"] = "published"
        Path(job, "manifest.json").write_text(json.dumps(man), encoding="utf-8")
        with self.assertRaisesRegex(m.NcbiError, "metadata"):
            run(m._resume_export, job)

    def test_checkpoint_dir_cannot_be_output(self):
        src = self.source(["1"])
        same = self.p("same")
        with self.assertRaisesRegex(m.NcbiError, "同路径"):
            run(m._export_pmids, src, same, "pmids", same)

    def test_export_accepts_candidate_json_source(self):
        src = write(self.p("cand.json"), json.dumps(
            {"schema": "pubmed.links.v1", "candidate_pmids": ["2", "1"]}))
        out = self.p("fromjson.txt")
        r = run(m._export_pmids, src, out, "pmids", self.p("job"))
        self.assertEqual(Path(out).read_text(encoding="utf-8").splitlines(), ["2", "1"])
        self.assertEqual(self.manifest(self.p("job"))["input"]["schema"], "pubmed.links.v1")

    def test_resume_missing_batch_file_refetches(self):
        src, out, job, calls = self._failed_job()
        man = self.manifest(job)
        batch_path = Path(job, *man["batches"][0]["file"].split("/"))
        batch_path.unlink()
        with patch.object(m, "_export_csv", fake_csv):
            r = run(m._resume_export, job)
        self.assertTrue(os.path.exists(out))
        self.assertTrue(any("校验失败" in w for w in self.manifest(job)["warnings"]))

    def test_cancellation_leaves_resumable_job(self):
        src, out, job = self.source(["1", "2", "3"]), self.p("out.csv"), self.p("job")

        async def call():
            async def cancelling(path, ids):
                scope.cancel()
                await anyio.lowlevel.checkpoint()

            with patch.object(m, "_export_csv", cancelling):
                with anyio.CancelScope() as scope:
                    await m._export_pmids(src, out, "csv", job)

        anyio.run(call)
        self.assertTrue(os.path.isdir(job))
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.exists(os.path.join(job, ".resume.lock")))
        self.assertEqual(self.manifest(job)["phase"], "metadata")
        with patch.object(m, "_export_csv", fake_csv):
            r = run(m._resume_export, job)
        self.assertTrue(os.path.exists(out))
        self.assertTrue(r["resumed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
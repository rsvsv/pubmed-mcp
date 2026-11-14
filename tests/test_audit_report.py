"""审计与离线报告回归；所有数字、日期和 PMID 均为本项目自订测试夹具。"""
import contextlib
import csv
import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import anyio

ROOT = Path(__file__).resolve().parents[1]  # 本项目自订：仓库层级
spec = importlib.util.spec_from_file_location("audit_subject", ROOT / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def es():
    return {"_term": "tooth", "count": "0", "idlist": [], "querytranslation": '"tooth"[All Fields]'}


class AuditReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        env = patch.dict(os.environ, {"PUBMED_AUDIT_DIR": str(self.dir / "logs")})
        env.start()
        self.addCleanup(env.stop)

    def log(self):
        return [json.loads(line) for line in (self.dir / "logs" / m._audit_filename()).read_text(encoding="utf-8").splitlines()]

    def fixture(self, name, records):
        path = self.dir / name
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
        return str(path)

    def test_timestamps_parameters_and_status_through_sdk(self):
        async def call():
            with patch.object(m, "_get_async", AsyncMock(return_value=json.dumps({"esearchresult": es()}))):
                out = await m.mcp.call_tool("search", {"term": "tooth", "retmax": 0, "as_json": True})  # 本项目自订：只查计数
                self.assertIn("written", str(out))
                self.assertIsNone(m._AUDIT_CONTEXT.get())
        anyio.run(call)
        rec, = self.log()
        self.assertEqual(rec["input_params"]["term"], "tooth")
        self.assertEqual(rec["request_params"]["retmax"], 0)  # 本项目自订：原始请求限制
        self.assertEqual(rec["audit_schema"], m._AUDIT_SCHEMA)
        start, end = (datetime.datetime.fromisoformat(rec[k]) for k in ("started_at", "finished_at"))
        self.assertIsNotNone(start.utcoffset())
        self.assertGreaterEqual(end, start)

    def test_disabled_and_failed_status(self):
        async def call():
            with patch.object(m, "_esearch", AsyncMock(return_value=es())):
                with patch.dict(os.environ, {"PUBMED_AUDIT_DIR": ""}):
                    data = json.loads(await m.search("tooth", retmax=0, as_json=True))  # 本项目自订：计数模式
                    self.assertEqual(data["audit_status"], "disabled")
                    original = await m.count("tooth")
                blocker = self.dir / "blocker"
                blocker.write_text("file")
                with patch.dict(os.environ, {"PUBMED_AUDIT_DIR": str(blocker)}), contextlib.redirect_stderr(io.StringIO()):
                    text = await m.count("tooth")
                    data = json.loads(await m.search("tooth", retmax=0, as_json=True))  # 本项目自订：计数模式
                self.assertTrue(text.startswith(original))
                self.assertIn("留痕写入失败", text)
                self.assertEqual(data["audit_status"], "failed")
        anyio.run(call)

    def test_export_completion_after_publish_and_failure_record(self):
        published = []
        real = m._publish_no_clobber
        def publish(*args):
            real(*args)
            published.append(m._utc_now())
        async def call():
            with patch.object(m, "_esearch", AsyncMock(return_value=es())), patch.object(m, "_publish_no_clobber", publish):
                result = json.loads(await m.export("tooth", str(self.dir / "out.txt"), as_json=True))
                self.assertEqual(result["audit_status"], "written")
            with patch.object(m, "_esearch", AsyncMock(side_effect=m.NcbiError("outage"))):
                with self.assertRaisesRegex(m.NcbiError, "outage"):
                    await m.count("tooth")
        anyio.run(call)
        success, failed = self.log()
        self.assertGreaterEqual(success["finished_at"], published[0])  # 本项目自订：首个发布时点
        self.assertEqual(success["sha256"], m._sha256_file(str(self.dir / "out.txt")))
        self.assertEqual(success["written_records"], 0)  # 本项目自订：空导出
        self.assertEqual(failed["outcome"], "failed")
        self.assertEqual(failed["failure_type"], "NcbiError")

    def test_concurrent_and_nested_contexts(self):
        @m._audited
        async def parent():
            await m.count("tooth")
            return "parent done"
        async def call():
            with patch.object(m, "_esearch", AsyncMock(return_value=es())):
                async with anyio.create_task_group() as tg:
                    for _ in range(5):  # 本项目自订：并发任务数量
                        tg.start_soon(m.count, "tooth")
                await parent()
            self.assertIsNone(m._AUDIT_CONTEXT.get())
        anyio.run(call)
        rows = self.log()
        self.assertEqual(len(rows), 7)  # 本项目自订：并发调用加嵌套父子
        self.assertEqual(len({r["event_id"] for r in rows}), len(rows))
        self.assertEqual(rows[-1]["run_id"], rows[-2]["run_id"])  # 本项目自订：父子共享运行标识
        self.assertNotEqual(rows[-1]["event_id"], rows[-2]["event_id"])  # 本项目自订：父子事件独立

    def test_cancellation_is_logged_and_context_restored(self):
        async def call():
            async def cancel(*args, **kwargs):
                scope.cancel()
                await anyio.lowlevel.checkpoint()
            with anyio.CancelScope() as scope:
                with patch.object(m, "_esearch", cancel):
                    await m.count("tooth")
            self.assertIsNone(m._AUDIT_CONTEXT.get())
        anyio.run(call)
        rec, = self.log()
        self.assertEqual(rec["outcome"], "cancelled")

    def test_sensitive_request_fields_removed(self):
        async def call():
            @m._audited
            async def probe():
                with patch.object(m, "_get", return_value="{}"):
                    await m._get_async("esearch.fcgi", {"term": "tooth", "api_key": "SECRET", "email": "PRIVATE"}, 1)  # 本项目自订：假请求
                m._audit("probe", request_url="SECRET", nested={"api_key": "SECRET", "email": "PRIVATE"})
                return "done"
            await probe()
        anyio.run(call)
        text = json.dumps(self.log())
        self.assertNotIn("SECRET", text)
        self.assertNotIn("PRIVATE", text)
        self.assertIn("esearch.fcgi", text)

    def test_old_logs_mark_unknown_and_do_not_sum(self):
        record = {"event": "count", "term": "tooth", "count": 7, "ts": "2026-01-01T00:00:00"}  # 本项目自订：旧日志
        source = self.fixture("old.jsonl", [record, record])
        out = str(self.dir / "report.csv")
        summary = m._audit_report([source, source], out)
        self.assertEqual(summary["records"], 2)  # 本项目自订：相同查询的两个旧事件保留
        with open(out, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["started_at"], m._AUDIT_UNKNOWN)  # 本项目自订：不把旧时间猜成起始
        self.assertEqual(rows[0]["source_sha256"], m._sha256_file(source))
        self.assertEqual(json.loads(rows[0]["counts"])["count"], record["count"])

    def test_event_dedup_and_conflict(self):
        rec = {"event": "count", "event_id": "same", "count": 7}  # 本项目自订：重复事件
        source = self.fixture("first.jsonl", [rec])
        copy = self.fixture("copy.jsonl", [rec])
        self.assertEqual(m._audit_report([source, copy], str(self.dir / "ok.md"))["records"], 1)  # 本项目自订：去重结果
        changed = self.fixture("changed.jsonl", [{**rec, "count": 8}])  # 本项目自订：冲突内容
        with self.assertRaisesRegex(m.NcbiError, "内容冲突"):
            m._audit_report([source, changed], str(self.dir / "bad.md"), skip_invalid=True)
        self.assertFalse((self.dir / "bad.md").exists())

    def test_invalid_lines_require_explicit_partial_report(self):
        path = self.dir / "bad.jsonl"
        path.write_bytes(b'{"event":"count"}\n{bad}\n\xff\n{"event":"count","count":NaN}\n')
        with self.assertRaisesRegex(m.NcbiError, ":2"):
            m._audit_report([str(path)], str(self.dir / "strict.md"))
        self.assertFalse((self.dir / "strict.md").exists())
        summary = m._audit_report([str(path)], str(self.dir / "partial.md"), skip_invalid=True)
        self.assertEqual(summary["skipped"], 3)  # 本项目自订：损坏 JSON、编码和非有限值
        self.assertFalse(summary["input_complete"])
        self.assertIn("输入不完整", (self.dir / "partial.md").read_text(encoding="utf-8"))

    def test_schema_and_duplicate_keys_rejected(self):
        source = self.fixture("future.jsonl", [{"event": "count", "audit_schema": "unknown"}])
        with self.assertRaisesRegex(m.NcbiError, "audit_schema"):
            m._audit_report([source], str(self.dir / "future.md"))
        Path(source).write_text('{"event":"count","event":"search"}', encoding="utf-8")
        with self.assertRaisesRegex(m.NcbiError, "重复键"):
            m._audit_report([source], str(self.dir / "duplicate.md"))

    def test_diff_report_preserves_both_queries_warnings_and_sources(self):
        rec = {"event": "diff", "event_id": "same", "term_a": "old", "term_b": "new",
               "querytranslation_a": "old[All Fields]", "querytranslation_b": "new[All Fields]",
               "warnings_a": ["a warning"], "warnings_b": ["b warning"]}
        a = self.fixture("a.jsonl", [rec])
        b = self.fixture("b.jsonl", [rec])
        out = str(self.dir / "diff.csv")
        m._audit_report([a, b], out)
        with open(out, encoding="utf-8-sig", newline="") as f:
            row, = list(csv.DictReader(f))
        self.assertEqual(json.loads(row["querytranslation"])["querytranslation_b"], rec["querytranslation_b"])
        self.assertEqual(json.loads(row["warnings"])["warnings_a"], rec["warnings_a"])
        self.assertEqual({r["source"] for r in json.loads(row["source_locations"])}, {a, b})

    def test_invalid_unicode_and_large_exponent_are_reported_by_line(self):
        source = self.dir / "unicode.jsonl"
        source.write_bytes(b'{"event":"count","term":"\\ud800"}\n{"event":"count","count":1e999}\n')  # 本项目自订：非法 Unicode 与溢出值
        result = m._audit_report([str(source)], str(self.dir / "unicode.md"), skip_invalid=True)
        self.assertEqual(result["skipped"], 2)  # 本项目自订：无效行数量
        self.assertEqual(result["records"], 0)  # 本项目自订：不接纳损坏记录

    def test_input_output_and_existing_file_are_protected(self):
        source = self.fixture("same.csv", [{"event": "count"}])
        before = Path(source).read_bytes()
        with self.assertRaisesRegex(m.NcbiError, "文件已存在"):
            m._audit_report([source], source)
        self.assertEqual(Path(source).read_bytes(), before)
        with self.assertRaisesRegex(m.NcbiError, "仅支持"):
            m._audit_report([source], str(self.dir / "bad.txt"))

    def test_report_escapes_markdown_and_csv_formulas(self):
        source = self.fixture("escape.jsonl", [{"event": "count", "term": "=HYPERLINK(x)", "warnings": ["<img>|\n"]}])
        m._audit_report([source], str(self.dir / "escaped.md"))
        text = (self.dir / "escaped.md").read_text(encoding="utf-8")
        self.assertNotIn("<img>", text)
        self.assertIn("&lt;img&gt;", text)
        m._audit_report([source], str(self.dir / "escaped.csv"))
        with open(self.dir / "escaped.csv", encoding="utf-8-sig", newline="") as f:
            row = next(csv.DictReader(f))
        self.assertTrue(row["query"].startswith("'="))

    def test_text_abstract_file_audit_has_published_artifact(self):
        target = self.dir / "abstracts.txt"
        async def call():
            with patch.object(m, "_efetch_all", AsyncMock(return_value=("fixture", ["1"]))):  # 本项目自订：模拟 PMID
                await m.fetch_abstracts("1", out_path=str(target))  # 本项目自订：模拟 PMID
        anyio.run(call)
        rec, = self.log()
        self.assertEqual(rec["path"], str(target))
        self.assertEqual(rec["sha256"], m._sha256_file(str(target)))
        self.assertEqual(rec["bytes"], target.stat().st_size)

    def test_report_keeps_tool_specific_counts(self):
        fixtures = [{"event": "diff", "common": 4}, {"event": "mesh", "descriptors": 2},
                    {"event": "links", "direct_total": 5, "errors": 1}]  # 本项目自订：各工具计数夹具
        source = self.fixture("counts.jsonl", fixtures)
        output = self.dir / "counts.csv"
        m._audit_report([source], str(output))
        with output.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        for row, fixture in zip(rows, fixtures):
            self.assertEqual(json.loads(row["counts"]), {k: v for k, v in fixture.items() if k != "event"})

    def test_report_cli_options_can_precede_command(self):
        source = self.fixture("order.jsonl", [{"event": "count"}])
        for name, arguments in (("out", ["--out", str(self.dir / "out.md"), "--audit-report", source]),
                                ("skip", ["--skip-invalid", "--audit-report", source, "--out", str(self.dir / "skip.md")])):
            with self.subTest(order=name), contextlib.redirect_stdout(io.StringIO()) as output:
                m.main(arguments)
                self.assertTrue(json.loads(output.getvalue())["input_complete"])

    def test_cli_runs_offline_and_invalid_input_fails(self):
        source = self.fixture("cli.jsonl", [{"event": "count", "term": "tooth", "count": 0}])  # 本项目自订：零命中
        result = subprocess.run([sys.executable, "-I", str(ROOT / "pubmed_mcp.py"), "--audit-report", source,
                                 "--out", str(self.dir / "cli.md")], capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)  # 本项目自订：成功退出
        self.assertTrue(json.loads(result.stdout)["input_complete"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.main(["--audit-report", source, "--out", str(self.dir / "unused.md"), "--typo"])


if __name__ == "__main__":
    unittest.main()

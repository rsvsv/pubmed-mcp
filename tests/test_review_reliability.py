"""Reliability regression tests, isolated and offline.

All identifiers, counts, timestamps and thresholds in this module are
本项目自订测试输入/断言口径, not observations about real PubMed records.
Run with python -I -B -m unittest discover -s <repo>/tests -p test_review_reliability.py -v.
"""
import contextlib
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import anyio

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review_reliability_subject", ROOT / "pubmed_review.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
m = r.m


def article(pid):
    return {"pmid": pid, "title": "Fixture " + pid,
            "abstract": [{"label": "RESULTS", "text": "Fixture abstract"}],
            "authors": [{"last": "Fixture", "fore": "Author", "initials": "A"}],
            "journal": {"title": "Fixture journal"}, "pubdate": "2020",  # 本项目自订测试输入
            "ids": {}, "lang": ["eng"], "mesh": [], "pt": [], "comments": []}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")  # 本项目自订：测试 JSON 缩进


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and p.name != ".review.lock"}


class Reliability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.out = self.base / "delivery"
        self.repair = self.base / "delivery.repair"
        self.plan_path = self.base / "plan.json"
        self.ids = ["1", "2"]  # 本项目自订测试 PMID
        self.plan = {"question": "Fixture question", "eligibility": "Fixture eligibility",
                     "term": "fixture[tiab]", "strategy_status": "approved",
                     "strategy_review_note": "Fixture approval, not a research decision",
                     "known_relevant": [{"pmid": "1", "source": "independent fixture"}],  # 本项目自订测试 PMID
                     "other_databases": "Fixture scope", "citation_tracking": "Fixture scope"}
        save(self.plan_path, self.plan)
        self.fetch_calls = []
        self.omit = set()

        async def esearch(term, **kwargs):
            if "[uid]" in term:
                ids = [p for p in self.ids if p + "[uid]" in term]
            else:
                ids = list(self.ids)
            return {"count": str(len(ids)), "idlist": ids if kwargs.get("retmax") else [],
                    "querytranslation": term, "_term": term}

        async def fetch(ids):
            self.fetch_calls.append(list(ids))
            return [article(pid) for pid in ids if pid not in self.omit]

        for p in (patch.object(m, "_esearch", esearch),
                  patch.object(m, "_efetch_xml_all", fetch),
                  patch.object(m, "_get_async", AsyncMock(side_effect=AssertionError("unexpected network"))),
                  patch.dict(os.environ, {"PUBMED_AUDIT_DIR": ""})):
            p.start()
            self.addCleanup(p.stop)

    def execute(self):
        return anyio.run(r.run, str(self.plan_path), str(self.out))

    def resume(self, root=None):
        return anyio.run(r.run, None, None, str(root or self.out))

    def missing_delivery(self):
        self.omit = {"2"}  # 本项目自订测试 PMID
        self.assertEqual(self.execute()["handoff_status"], "needs_execution_repair")
        self.omit.clear()
        self.fetch_calls.clear()

    def rewrite_state(self, state):
        save(self.out / "state.json", state)
        delivery = r.read_json(self.out / "delivery.json")
        delivery["files"]["state.json"] = r.digest(self.out / "state.json")
        save(self.out / "delivery.json", delivery)

    def rebind_file(self, relative, stage):
        sha = r.digest(self.out / relative)
        state = r.read_json(self.out / "state.json")
        state["stages"][stage]["files"][relative] = sha
        self.rewrite_state(state)
        delivery = r.read_json(self.out / "delivery.json")
        delivery["files"][relative] = sha
        save(self.out / "delivery.json", delivery)

    def test_completed_resume_fetches_only_missing_and_preserves_original(self):
        self.missing_delivery()
        before = snapshot(self.out)
        with patch.object(m, "_esearch", AsyncMock(side_effect=AssertionError("must keep fixed search"))):
            result = self.resume()
        self.assertEqual(result["handoff_status"], "ready_for_screening")
        self.assertEqual(Path(result["directory"]), self.repair)
        self.assertEqual(self.fetch_calls, [["2"]])  # 本项目自订：只补缺失 PMID
        self.assertEqual(snapshot(self.out), before)
        self.assertEqual(snapshot(self.repair / "original"), before)
        self.assertEqual((self.repair / "all-pmids.txt").read_bytes(), before["all-pmids.txt"])
        repaired = r.read_json(self.repair / "records" / "batch-00000.json")  # 本项目自订：既有批次名
        original = r.read_json(self.out / "records" / "batch-00000.json")  # 本项目自订：既有批次名
        self.assertEqual(repaired["records"][0], original["records"][0])  # 本项目自订：首条为原先成功题录
        self.assertEqual(r.verify(self.repair)["status"], "verified")

    def test_cli_resume_returns_repaired_directory_and_success(self):
        self.missing_delivery()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = r.main(["--resume-review", str(self.out)])
        self.assertEqual(code, 0)  # 本项目自订：成功退出码
        data = json.loads(output.getvalue())
        self.assertEqual(data["handoff_status"], "ready_for_screening")
        self.assertEqual(Path(data["directory"]), self.repair)
        self.assertEqual(self.fetch_calls, [["2"]])  # 本项目自订测试 PMID

    def test_interrupted_repair_resumes_from_original_or_repair_path(self):
        self.missing_delivery()
        before = snapshot(self.out)
        with patch.object(m, "_efetch_xml_all", AsyncMock(side_effect=m.NcbiError("fixture interruption"))):
            with self.assertRaises(m.NcbiError):
                self.resume()
        self.assertTrue((self.repair / "state.json").exists())
        self.assertFalse((self.repair / "delivery.json").exists())
        self.assertEqual(self.resume(self.repair)["handoff_status"], "ready_for_screening")
        self.assertEqual(self.fetch_calls, [["2"]])  # 本项目自订测试 PMID
        self.assertEqual(snapshot(self.out), before)
        self.fetch_calls.clear()
        self.assertEqual(self.resume()["handoff_status"], "ready_for_screening")
        self.assertEqual(self.fetch_calls, [])

    def test_repair_initialization_can_resume_after_copy_failure(self):
        self.missing_delivery()
        before = snapshot(self.out)
        copy = r.copy_verified
        failed = False

        def interrupt(source, target, sha):
            nonlocal failed
            if target.name == "plan.json" and not failed:
                failed = True
                raise OSError("fixture copy interruption")
            return copy(source, target, sha)

        with patch.object(r, "copy_verified", interrupt), self.assertRaises(OSError):
            self.resume()
        self.assertTrue((self.repair / "repair-init.json").exists())
        self.assertFalse((self.repair / "state.json").exists())
        self.assertEqual(self.resume(self.repair)["handoff_status"], "ready_for_screening")
        self.assertEqual(snapshot(self.out), before)

    def test_state_commit_failure_preserves_orphan_and_retries(self):
        self.missing_delivery()
        atomic = m._write_json_atomic
        orphan = {}

        def interrupt(path, data):
            if Path(path) == self.repair / "state.json" and "records-00000" in data["stages"]:  # 本项目自订：首批编号
                batch = self.repair / "records" / "batch-00000.json"  # 本项目自订：首批文件
                orphan["bytes"] = batch.read_bytes()
                raise OSError("fixture state commit interruption")
            return atomic(path, data)

        with patch.object(m, "_write_json_atomic", interrupt), self.assertRaises(OSError):
            self.resume()
        self.fetch_calls.clear()
        self.assertEqual(self.resume()["handoff_status"], "ready_for_screening")
        archived = list((self.repair / "uncommitted").rglob("batch-*.json"))
        self.assertEqual(len(archived), 1)  # 本项目自订：仅一个未提交批次
        self.assertEqual(archived[0].read_bytes(), orphan["bytes"])  # 本项目自订：首个归档
        self.assertEqual(self.fetch_calls, [["2"]])  # 本项目自订测试 PMID

    def test_committed_repair_batch_is_not_refetched_after_later_failure(self):
        self.missing_delivery()
        with patch.object(r, "render_screening", side_effect=OSError("fixture rendering interruption")):
            with self.assertRaises(OSError):
                self.resume()
        self.fetch_calls.clear()
        self.assertEqual(self.resume()["handoff_status"], "ready_for_screening")
        self.assertEqual(self.fetch_calls, [])

    def test_empty_uncommitted_screening_is_archived_and_resumed(self):
        self.missing_delivery()
        def interrupt(root, ids):
            (root / "screening.csv").write_bytes(b"")
            raise OSError("fixture interrupted before header")
        with patch.object(r, "render_screening", interrupt), self.assertRaises(OSError):
            self.resume()
        self.assertEqual(self.resume()["handoff_status"], "ready_for_screening")
        archived = list((self.repair / "uncommitted").rglob("screening.csv"))
        self.assertEqual([p.read_bytes() for p in archived], [b""])

    def test_report_display_columns_must_match_original_log(self):
        self.execute()
        path = self.out / "search-log.csv"
        original = path.read_bytes()
        for column in ("query", "counts", "event", "source", "source_line", "source_locations"):
            with self.subTest(column=column):
                path.write_bytes(original)
                with path.open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    fields, rows = reader.fieldnames, list(reader)
                rows[0][column] = "wrong fixture"  # 本项目自订：修改首行展示值
                with path.open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
                state = r.read_json(self.out / "state.json")
                state["stages"]["report"]["result"].update(sha256=r.digest(path), bytes=path.stat().st_size)
                self.rewrite_state(state)
                self.rebind_file("search-log.csv", "report")
                with self.assertRaisesRegex(ValueError, "审计报告"):
                    r.verify(self.out)

    def test_original_manual_screening_changes_are_never_overwritten(self):
        self.missing_delivery()
        path = self.out / "screening.csv"
        path.write_text(path.read_text(encoding="utf-8-sig").replace("待筛选", "include"), encoding="utf-8-sig")
        before = snapshot(self.out)
        with self.assertRaisesRegex(ValueError, "文件缺失或已改变"):
            self.resume()
        self.assertEqual(snapshot(self.out), before)
        self.assertFalse(self.repair.exists())
        self.assertEqual(self.fetch_calls, [])

    def test_uncommitted_manual_screening_changes_are_never_moved(self):
        self.missing_delivery()
        atomic = m._write_json_atomic

        def interrupt(path, data):
            if Path(path) == self.repair / "state.json" and "screening" in data["stages"]:
                raise OSError("fixture screening commit interruption")
            return atomic(path, data)

        with patch.object(m, "_write_json_atomic", interrupt), self.assertRaises(OSError):
            self.resume()
        path = self.repair / "screening.csv"
        path.write_text(path.read_text(encoding="utf-8-sig").replace("待筛选", "include"), encoding="utf-8-sig")
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "人工修改"):
            self.resume()
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.repair / "uncommitted").exists())

    def test_preexisting_unowned_repair_directory_is_not_adopted(self):
        self.missing_delivery()
        self.repair.mkdir()
        marker = self.repair / "human-work.txt"
        marker.write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "拒绝接管"):
            self.resume()
        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertFalse((self.repair / "repair-init.json").exists())

    def test_bad_tail_does_not_hide_intact_events_and_stays_unresolved(self):
        with patch.object(m, "_efetch_xml_all", AsyncMock(side_effect=m.NcbiError("fixture interruption"))):
            with self.assertRaises(m.NcbiError):
                self.execute()
        log = next((self.out / "audit").glob("*.jsonl"))
        with log.open("ab") as stream:
            stream.write(b'{"unfinished":\xff')
        damaged = log.read_bytes()
        result = self.resume()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["handoff_status"], "needs_execution_repair")
        self.assertTrue(result["audit_diagnostics"])
        self.assertEqual(log.read_bytes(), damaged)
        codes = {item["code"] for item in r.read_json(self.out / "delivery.json")["issues"]}
        self.assertIn("audit_corruption", codes)
        with (self.out / "search-log.csv").open(encoding="utf-8-sig", newline="") as stream:
            self.assertIn("INVALID_INPUT", [row["event"] for row in csv.DictReader(stream)])
        self.fetch_calls.clear()
        self.assertEqual(self.resume()["handoff_status"], "needs_execution_repair")
        self.assertEqual(self.fetch_calls, [])

    def test_missing_duplicate_and_changed_target_are_not_excused_by_bad_tail(self):
        self.execute()
        state = r.read_json(self.out / "state.json")
        receipt = state["stages"]["search"]["result"]
        event_id = receipt["audit_event_id"]
        log = next((self.out / "audit").glob("*.jsonl"))
        records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        event = next(rec for rec in records if rec.get("event_id") == event_id)
        original_sha = state["stages"]["search"]["audit_event_sha256"]
        cases = {"missing": [rec for rec in records if rec.get("event_id") != event_id],
                 "duplicate": records + [event],
                 "invalid_target": records + [{"event_id": event_id}],
                 "changed": [{**rec, "term": "changed fixture"} if rec.get("event_id") == event_id else rec
                             for rec in records]}
        for name, values in cases.items():
            with self.subTest(case=name):
                log.write_bytes(("\n".join(json.dumps(rec) for rec in values) + '\n{"broken":').encode())
                index = r.AuditIndex(self.out)
                self.assertTrue(index.diagnostics)
                if name == "changed":
                    self.assertNotEqual(r.audit_event(self.out, receipt, index), original_sha)
                else:
                    with self.assertRaisesRegex(ValueError, "审计事件缺失或重复"):
                        r.audit_event(self.out, receipt, index)

    def test_all_mandatory_stages_are_derived_even_when_manifest_is_rehashed(self):
        self.execute()
        state = r.read_json(self.out / "state.json")
        for name in ("search", "recall-00000", "records-00000", "screening", "report"):  # 本项目自订：首批编号
            with self.subTest(stage=name):
                altered = json.loads(json.dumps(state))
                del altered["stages"][name]
                self.rewrite_state(altered)
                with self.assertRaisesRegex(ValueError, "必需阶段集合"):
                    r.verify(self.out)
        self.rewrite_state(state)
        self.assertEqual(r.verify(self.out)["status"], "verified")

    def test_stage_files_and_audits_must_be_in_delivery_inventory(self):
        self.execute()
        delivery = r.read_json(self.out / "delivery.json")
        audit_name = next(name for name in delivery["files"] if name.startswith("audit/"))
        for relative in ("records/batch-00000.json", "recall/recall-00000.json", audit_name):  # 本项目自订：首批文件名
            with self.subTest(file=relative):
                altered = json.loads(json.dumps(delivery))
                del altered["files"][relative]
                save(self.out / "delivery.json", altered)
                with self.assertRaisesRegex(ValueError, "清单"):
                    r.verify(self.out)
        save(self.out / "delivery.json", delivery)

    def test_receipts_must_agree_with_actual_records_and_screening(self):
        self.execute()
        state = r.read_json(self.out / "state.json")
        for name in ("records-00000", "screening"):  # 本项目自订：首批编号
            with self.subTest(stage=name):
                altered = json.loads(json.dumps(state))
                altered["stages"][name]["result"]["missing_pmids"] = ["2"]  # 本项目自订：伪造缺失 PMID
                self.rewrite_state(altered)
                with self.assertRaisesRegex(ValueError, "回执"):
                    r.verify(self.out)
        self.rewrite_state(state)

    def test_recall_receipt_cannot_describe_another_known_set(self):
        self.execute()
        name = "recall-00000"  # 本项目自订：首批编号
        state = r.read_json(self.out / "state.json")
        state["stages"][name]["result"]["found"] = ["2"]  # 本项目自订：不属于已知集的测试 PMID
        save(self.out / "recall" / (name + ".json"), {**state["stages"][name]["result"], "evidence": r.EVIDENCE})
        self.rewrite_state(state)
        self.rebind_file("recall/" + name + ".json", name)
        with self.assertRaisesRegex(ValueError, "方案批次"):
            r.verify(self.out)

    def test_rehashed_screening_bibliography_changes_are_detected(self):
        self.execute()
        path = self.out / "screening.csv"
        path.write_text(path.read_text(encoding="utf-8-sig").replace("Fixture 1", "Changed title"), encoding="utf-8-sig")  # 本项目自订测试标题
        self.rebind_file("screening.csv", "screening")
        with self.assertRaisesRegex(ValueError, "筛选表与题录"):
            r.verify(self.out)

    def test_incomplete_delivery_rejects_changed_or_missing_module_fingerprint(self):
        with patch.object(m, '_efetch_xml_all', AsyncMock(side_effect=m.NcbiError('fixture interruption'))):
            with self.assertRaises(m.NcbiError):
                self.execute()
        original = r.read_json(self.out / 'state.json')
        self.assertEqual(original['implementation_sha256'], m._implementation_fingerprint())
        for name in m._runtime.RUNTIME_FILES:
            state = json.loads(json.dumps(original))
            state['implementation_sha256'][name] = 'different-implementation'
            save(self.out / 'state.json', state)
            before = snapshot(self.out)
            with self.subTest(module=name), self.assertRaisesRegex(ValueError, '实现已变化'):
                self.resume()
            self.assertEqual(snapshot(self.out), before)
        state = json.loads(json.dumps(original))
        del state['implementation_sha256']
        save(self.out / 'state.json', state)
        with self.assertRaisesRegex(ValueError, '实现已变化'):
            self.resume()
        save(self.out / 'state.json', original)
        self.assertEqual(self.resume()['handoff_status'], 'ready_for_screening')

    def test_repair_records_current_modules_and_checks_interrupted_repair(self):
        self.missing_delivery()
        source = snapshot(self.out)
        with patch.object(m, '_efetch_xml_all', AsyncMock(side_effect=m.NcbiError('fixture interruption'))):
            with self.assertRaises(m.NcbiError):
                self.resume()
        state = r.read_json(self.repair / 'state.json')
        self.assertEqual(state['implementation_sha256'], m._implementation_fingerprint())
        state['implementation_sha256']['pubmed_records.py'] = 'different-formatter'
        save(self.repair / 'state.json', state)
        before = snapshot(self.repair)
        with self.assertRaisesRegex(ValueError, '实现已变化'):
            self.resume(self.repair)
        self.assertEqual(snapshot(self.repair), before)
        self.assertEqual(snapshot(self.out), source)

    def test_old_complete_delivery_and_moved_directory_still_verify(self):
        self.execute()
        state = r.read_json(self.out / "state.json")
        state["runner_sha256"] = "previous-runner-fixture"
        state["source_sha256"] = "previous-core-fixture"
        state.pop("implementation_sha256", None)
        self.rewrite_state(state)
        moved = self.base / "moved delivery"
        self.out.rename(moved)
        self.assertEqual(r.verify(moved)["handoff_status"], "ready_for_screening")
        self.fetch_calls.clear()
        self.assertEqual(self.resume(moved)["handoff_status"], "ready_for_screening")
        self.assertEqual(self.fetch_calls, [])

    def test_multibatch_mandatory_stages_and_successful_batch_reuse(self):
        self.plan["known_relevant"].append({"pmid": "2", "source": "independent second fixture"})  # 本项目自订测试 PMID
        save(self.plan_path, self.plan)
        with patch.object(r, "BATCH_SIZE", 1), patch.object(m, "GOLD_MAX", 1):  # 本项目自订：每批一个输入
            self.missing_delivery()
            result = self.resume()
            self.assertEqual(result["handoff_status"], "ready_for_screening")
            self.assertEqual(self.fetch_calls, [["2"]])  # 本项目自订：成功批次不重取
            stages = r.read_json(self.repair / "state.json")["stages"]
            self.assertEqual(set(stages), {"search", "recall-00000", "recall-00001",  # 本项目自订批次编号
                                          "records-00000", "records-00001", "screening", "report"})  # 本项目自订批次编号

    def test_empty_export_has_no_invented_record_or_recall_stages(self):
        self.ids = []
        self.plan["known_relevant"] = []
        save(self.plan_path, self.plan)
        result = self.execute()
        self.assertEqual(result["handoff_status"], "needs_methodology_review")
        self.assertEqual(set(r.read_json(self.out / "state.json")["stages"]), {"search", "screening", "report"})
        self.assertEqual(r.verify(self.out)["status"], "verified")

    def test_verify_builds_one_audit_index_for_an_ordinary_delivery(self):
        self.execute()
        with patch.object(r, "AuditIndex", wraps=r.AuditIndex) as indexed:
            self.assertEqual(r.verify(self.out)["status"], "verified")
        indexed.assert_called_once()

    def test_historical_audit_failure_stays_visible_after_records_are_repaired(self):
        writer = m._write_audit

        def fail_batch(event, fields, **kwargs):
            if event == "retrieve_batch":
                return {"status": "failed", "error": "fixture lost audit"}
            return writer(event, fields, **kwargs)

        self.omit = {"2"}  # 本项目自订测试 PMID
        with patch.object(m, "_write_audit", fail_batch):
            self.assertEqual(self.execute()["handoff_status"], "needs_execution_repair")
        self.omit.clear()
        result = self.resume()
        self.assertEqual(result["metrics"]["missing_pmids"], [])
        self.assertEqual(result["handoff_status"], "needs_execution_repair")
        issues = r.read_json(self.repair / "delivery.json")["issues"]
        self.assertIn("source_audit_incomplete", {item["code"] for item in issues})


if __name__ == "__main__":
    unittest.main()

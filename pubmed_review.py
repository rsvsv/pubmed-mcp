"""PubMed review handoff CLI. Schema versions, batch sizes and exit codes are project policy.

Runs in a dedicated CLI process: its audit directory belongs to this review run.
Numeric values in JSON artifacts are tool observations unless marked project policy.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

import anyio
# 始终加载同一快照的相邻服务实现，避免源码入口误用环境里的旧安装包。
import importlib.util
spec = importlib.util.spec_from_file_location("review_pubmed_core", Path(__file__).with_name("pubmed_mcp.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
services = m.review_services()

SCHEMA = "pubmed.review.v1"  # 本项目自订：交付协议版本
BATCH_SIZE = 50  # 本项目自订：单批题录规模，沿用 efetch 分批规模
UNKNOWN = "⚠️【待确认】"
EVIDENCE = "实测/工具输出：本次 PubMed 检索与 efetch XML 解析；不证明全文内容、相关性或整体查全率"
COLUMNS = ("pmid", "title", "abstract", "journal", "pubdate", "doi", "publication_types",
           "record_status", "abstract_status", "reviewer_decision", "exclusion_reason",
           "reviewer", "source_record", "evidence")
PLAN_FIELDS = {"question", "eligibility", "term", "strategy_status", "strategy_review_note",
               "datetype", "mindate", "maxdate", "known_relevant", "other_databases",
               "citation_tracking", "purpose"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    services.write_new_file(str(path), json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def digest(path):
    return services.sha256_file(str(path))


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) - PLAN_FIELDS:
        raise ValueError("方案必须是 JSON 对象，且不能包含未知字段")
    for key in ("question", "eligibility", "term"):
        if not isinstance(plan.get(key), str) or not plan[key].strip() or "待确认" in plan[key]:
            raise ValueError("方案缺少非空文本：" + key)
    for key in PLAN_FIELDS - {"known_relevant"}:
        if key in plan and not isinstance(plan[key], str):
            raise ValueError("方案字段必须是文本：" + key)
    if plan.get("strategy_status", "draft") not in ("draft", "approved"):
        raise ValueError("strategy_status 只支持 draft / approved")
    if plan.get("strategy_status") == "approved" and not plan.get("strategy_review_note", "").strip():
        raise ValueError("批准策略必须注明实际复核者或已有批准依据，不能由程序代填")
    services.check_datetype(plan.get("datetype", "pdat"))
    lo, hi = plan.get("mindate", ""), plan.get("maxdate", "")
    if bool(lo) != bool(hi):
        raise ValueError("日期限制必须同时填写起止日期，或都留空")
    if lo:
        services.check_date(lo, "mindate")
        services.check_date(hi, "maxdate")
        if services.expand_date(lo) > services.expand_date(hi, True):
            raise ValueError("日期区间倒挂")
    known = plan.get("known_relevant", [])
    if not isinstance(known, list):
        raise ValueError("known_relevant 必须是 PMID 与来源组成的数组")
    seen = set()
    for item in known:
        if not isinstance(item, dict) or set(item) != {"pmid", "source"}:
            raise ValueError("每条已知文献必须且只能包含 pmid、source")
        if not isinstance(item["pmid"], str) or not re.fullmatch(r"[1-9][0-9]*", item["pmid"]):
            raise ValueError("已知文献 PMID 必须是无前导零的数字文本")
        if item["pmid"] in seen:
            raise ValueError("已知文献 PMID 重复")
        if not isinstance(item["source"], str) or not item["source"].strip():
            raise ValueError("已知文献必须注明独立来源；不得把本次命中倒充已知集")
        seen.add(item["pmid"])
    return plan


def local_path(root, relative):
    p = Path(relative)
    if p.is_absolute() or ".." in p.parts or not p.parts or ":" in relative or "\\" in relative:
        raise ValueError("交付清单含非相对路径")
    path = root / p
    if path.resolve() != root.resolve() / p:
        raise ValueError("交付文件不能通过链接别名指向其它文件")
    return path


def check_files(root, files):
    if not isinstance(files, dict) or not files:
        raise ValueError("文件清单为空或损坏")
    for name, sha in files.items():
        path = local_path(root, name)
        if not path.is_file() or digest(path) != sha:
            raise ValueError("文件缺失或已改变：" + name)


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class AuditIndex:
    """按文件缓存解析结果；坏行保留诊断，不能充当可验证事件。"""
    def __init__(self, root):
        self.root = Path(root)
        self.cache = {}
        self.refresh()

    def refresh(self):
        current = {}
        for path in sorted((self.root / "audit").glob("*.jsonl")):
            name = path.relative_to(self.root).as_posix()
            path = local_path(self.root, name)
            stat = path.stat()
            stamp = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            cached = self.cache.get(name)
            if cached is not None and cached[0] == stamp:
                current[name] = cached
                continue
            payload = path.read_bytes()
            source_sha = hashlib.sha256(payload).hexdigest()
            events, diagnostics, records = [], [], []
            def reject_constant(value):
                raise ValueError("JSON 不允许 " + value)
            def unique_keys(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj:
                        raise ValueError("JSON 包含重复键")
                    obj[key] = value
                return obj
            for line, raw in enumerate(payload.splitlines(), start=1):  # 本项目自订：源行号从首行计起
                try:
                    text = raw.decode("utf-8-sig" if line == 1 else "utf-8")  # 本项目自订：仅首行允许 BOM
                    if not text.strip():
                        continue
                    rec = json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_keys)
                    services.validate_audit_record(rec)
                    canonical = canonical_json(rec)
                    sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                    event_id = rec.get("event_id")
                    if event_id:
                        events.append((event_id, sha))
                    records.append((event_id, line, canonical))
                except (ValueError, TypeError, OverflowError, RecursionError) as exc:
                    diagnostics.append({"source": name, "source_line": line,
                                        "source_sha256": source_sha,
                                        "raw_line_sha256": hashlib.sha256(raw).hexdigest(),
                                        "error": type(exc).__name__ + ": " + str(exc)})
                    # 截断行或重复键中仍可辨认的 event_id 也占位；不能隐藏目标的坏副本。
                    for token in re.findall(r'"event_id"\s*:\s*("(?:[^"\\]|\\.)*")',
                                            raw.decode("utf-8", errors="replace")):
                        try:
                            event_id = json.loads(token)
                        except ValueError:
                            continue
                        if event_id:
                            events.append((event_id, None))
            current[name] = (stamp, events, diagnostics, records)
        self.cache = current
        self.events, self.diagnostics, self.report_records = {}, [], {}
        for name, (_, events, diagnostics, records) in current.items():
            self.diagnostics.extend(diagnostics)
            for event_id, sha in events:
                self.events.setdefault(event_id, []).append(sha)
            for event_id, line, canonical in records:
                key = ("event", event_id) if event_id else ("legacy", name, line)
                previous = self.report_records.get(key)
                if previous is not None and previous != canonical:
                    raise ValueError("审计 event_id 内容冲突：" + str(event_id))
                self.report_records[key] = canonical
        return self


def audit_event(root, result, index=None):
    if result.get("audit_status") != "written":
        return None
    index = index if index is not None else AuditIndex(root)
    event_id = result.get("audit_event_id")
    matches = index.events.get(event_id, [])
    if not event_id or len(matches) != 1 or matches[0] is None:  # 本项目自订：目标事件须唯一且可解析
        raise ValueError("审计事件缺失或重复，或目标记录无效，不能复用阶段：" + str(event_id))
    return matches[0]


def execution_issues(root, state, index):
    issues = []
    if index.diagnostics:
        issues.append({"code": "audit_corruption", "kind": "execution",
                       "detail": "原始审计字节已保留；以下行不能作为已核实事件，尚未修复："
                                 + canonical_json(index.diagnostics)})
    if state.get("repair_source"):
        prior = read_json(root / "original" / "delivery.json")
        if any(i["code"] in {"audit_incomplete", "source_audit_incomplete"} for i in prior["issues"]):
            issues.append({"code": "source_audit_incomplete", "kind": "execution",
                           "detail": "原交付存在未成功留痕的步骤；补取题录不能补证历史事件，见 original/delivery.json"})
    return issues


def acquire_lock(root):
    # 内核锁在进程退出时自动释放；保留锁文件，避免删除重建引起竞争。
    stream = (root / ".review.lock").open("a+b")
    try:
        if stream.tell() == 0:  # 本项目自订：锁定首个字节
            stream.write(b"L")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)  # 本项目自订：锁定首个字节
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        stream.close()
        raise
    return stream


def actionable_warnings(warnings):
    # 执行式与分批说明也位于 warnings 字段；它们本身不构成待解决问题。
    return [w for w in warnings if not w.startswith(("NCBI 实际执行：", "（金标准 ", "（原始输入 "))]


def csv_safe(value):
    value = str(value).replace("\x00", r"\u0000")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r")) else value


@services.audited
async def retrieve_batch(pmids, out):
    records = await services.fetch_records(pmids)
    returned = [r["pmid"] for r in records]
    if len(set(returned)) != len(returned) or not set(returned).issubset(pmids):
        raise ValueError("题录返回重复或不属于本批的 PMID")
    data = {"requested_pmids": pmids, "records": records,
            "missing_pmids": [p for p in pmids if p not in returned],
            "retrieved_at": services.utc_now(), "evidence": EVIDENCE}
    write_json(out, data)
    result = {"path": str(out), "sha256": digest(out), "missing_pmids": data["missing_pmids"]}
    services.audit("review_records", **result)
    return result


@services.audited
async def retrieve_repair_batch(pmids, out, previous):
    prior = read_json(previous)
    if prior["requested_pmids"] != pmids:
        raise ValueError("修复批次与原固定名单不同")
    wanted = prior["missing_pmids"]
    records = await services.fetch_records(wanted)
    returned = [r["pmid"] for r in records]
    if len(set(returned)) != len(returned) or not set(returned).issubset(wanted):
        raise ValueError("补取题录返回重复或不属于缺失名单的 PMID")
    by_id = {r["pmid"]: r for r in prior["records"]}
    by_id.update({r["pmid"]: r for r in records})
    data = {"requested_pmids": pmids, "records": [by_id[p] for p in pmids if p in by_id],
            "missing_pmids": [p for p in pmids if p not in by_id],
            "retrieved_at": services.utc_now(), "evidence": EVIDENCE,
            "repair_requested_pmids": wanted, "previous_batch_sha256": digest(previous)}
    write_json(out, data)
    result = {"path": str(out), "sha256": digest(out), "missing_pmids": data["missing_pmids"]}
    services.audit("review_records_repair", **result, repair_requested_pmids=wanted,
             previous_batch_sha256=data["previous_batch_sha256"])
    return result


def record_batches(root, ids):
    for start in range(0, len(ids), BATCH_SIZE):
        name = f"records/batch-{start // BATCH_SIZE:05d}.json"  # 本项目自订：固定批次文件名
        data = read_json(local_path(root, name))
        expected = ids[start:start + BATCH_SIZE]
        recs = data["records"]
        got = [r["pmid"] for r in recs]
        if (data["requested_pmids"] != expected or len(set(got)) != len(got)
                or not set(got).issubset(expected)
                or data["missing_pmids"] != [p for p in expected if p not in got]):
            raise ValueError("题录批次与固定名单不一致：" + name)
        yield name, data


def screening_row(name, pid, rec):
    row = {key: "" for key in COLUMNS}
    row.update(pmid=pid, reviewer_decision="待筛选", source_record=name + "#PMID=" + pid,
               evidence=EVIDENCE, record_status="retrieved" if rec else "missing",
               abstract_status="unknown")
    if rec is None:
        row.update(title=UNKNOWN, abstract=UNKNOWN)
    else:
        abstract = "\n".join((s["label"] + ": " if s["label"] else "") + s["text"] for s in rec["abstract"])
        row.update(title=rec["title"], abstract=abstract or "⚠️ PubMed 本次记录未提供摘要；待全文核对",
                   journal=rec["journal"].get("title", rec["journal"].get("book", "")),
                   pubdate=rec["pubdate"], doi=rec["ids"].get("doi", ""),
                   publication_types="; ".join(rec["pt"]),
                   abstract_status="available" if abstract else "not_provided")
    return {k: csv_safe(v) for k, v in row.items()}


def screening_materials(root, ids):
    for name, data in record_batches(root, ids):
        by_id = {r["pmid"]: r for r in data["records"]}
        for pid in data["requested_pmids"]:
            rec = by_id.get(pid)
            yield screening_row(name, pid, rec), services.ris_record(rec) + "\n\n" if rec else ""


def render_screening(root, ids):
    missing, no_abstract = [], []
    csv_path, ris_path = root / "screening.csv", root / "references.ris"
    # 两个派生产物使用同一次抓取的题录；任一未完成时不产生交付完成标识。
    with csv_path.open("x", encoding="utf-8-sig", newline="") as stream, ris_path.open("x", encoding="utf-8") as ris:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        for row, block in screening_materials(root, ids):
            if row["record_status"] == "missing":
                missing.append(row["pmid"])
            elif row["abstract_status"] == "not_provided":
                no_abstract.append(row["pmid"])
            writer.writerow(row)
            ris.write(block)
    return {"missing_pmids": missing, "no_abstract_pmids": no_abstract, "evidence": EVIDENCE}


def assess(plan, export, recalls, batch_results, screening):
    issues = []
    def issue(code, kind, detail):
        issues.append({"code": code, "kind": kind, "detail": detail})
    if export.get("integrity_status") != "no_discrepancy_detected":
        issue("search_integrity", "execution", "检索计数或分页存在差异，见 search-result.json")
    if export.get("metadata_status") != "written":
        issue("search_metadata", "execution", "检索伴随说明未成功写入")
    if export.get("warnings"):
        issue("query_warnings", "methodology", "须逐条核对 search-result.json 的警告与执行式")
    if not export.get("querytranslation"):
        issue("translation_missing", "methodology", "NCBI 实际执行式缺失")
    if export.get("unique") == 0:
        issue("zero_results", "methodology", "零命中须复核策略，不能据此认定没有研究")
    if plan.get("strategy_status", "draft") != "approved":
        issue("strategy_draft", "methodology", "检索策略尚未由研究者复核批准")
    if not plan.get("known_relevant"):
        issue("known_set_missing", "methodology", "未提供独立来源的已知文献集，尚未验证已知文献找回")
    for result in recalls:
        if result.get("missing") or result.get("term_ignored") or result.get("term_degraded"):
            issue("recall_unresolved", "methodology", "已知集未找回或查询失真，见 recall 目录；未找回不直接证明缺词")
        if actionable_warnings(result.get("warnings", [])):
            issue("recall_warnings", "methodology", "已知集检验包含警告，见 recall 目录")
    for key in ("other_databases", "citation_tracking"):
        if not plan.get(key, "").strip() or "待确认" in plan.get(key, ""):
            issue(key, "methodology", "检索范围决策尚未说明：" + key)
    if screening.get("known_missing_from_export"):
        issue("known_not_in_export", "methodology", "已知文献不在本次固定交付名单，不能用后续实时查询的找回替代；见 state.json 的 screening 回执")
    if screening["missing_pmids"]:
        issue("missing_records", "execution", "缺失题录保留为待筛选行，不能自动排除")
    for result in [export, *recalls, *batch_results]:
        if result.get("audit_status") != "written":
            issue("audit_incomplete", "execution", "存在未成功留痕的步骤，见对应阶段回执")
    return issues


def required_stages(plan, ids):
    paths = {"search": {"all-pmids.txt", "all-pmids.txt.meta.json", "search-result.json"}}
    for start in range(0, len(plan.get("known_relevant", [])), services.recall_limit):
        name = f"recall-{start // services.recall_limit:05d}"  # 本项目自订：沿用既有回执编号
        paths[name] = {"recall/" + name + ".json"}
    for start in range(0, len(ids), BATCH_SIZE):
        name = f"records-{start // BATCH_SIZE:05d}"  # 本项目自订：沿用既有回执编号
        paths[name] = {f"records/batch-{start // BATCH_SIZE:05d}.json"}  # 本项目自订：固定批次名
    paths.update(screening={"screening.csv", "references.ris"}, report={"search-log.csv"})
    return paths


def repair_origin(root, state):
    info = state.get("repair_source")
    if info is None:
        return None
    original = local_path(root, "original")
    check_files(original, info["files"])
    if digest(original / "delivery.json") != info["delivery_sha256"]:
        raise ValueError("原交付快照已变化")
    for name in ("plan.json", "all-pmids.txt"):
        if digest(root / name) != digest(original / name):
            raise ValueError("修复不能改变方案或固定 PMID 名单：" + name)
    return original


def verify_report_rows(root, report, rows):
    """重新派生展示列；只允许整体搬移改变来源目录前缀。"""
    original_base = str(report.get("path", "")).replace("\\", "/").rsplit("/", 1)[0]  # 本项目自订：报告原目录
    current_base = str(root).replace("\\", "/")

    def source_name(value):
        value = value.replace("\\", "/")
        for base in (original_base, current_base):
            prefix = base.rstrip("/") + "/audit/"
            if value.startswith(prefix):
                name = value[len(prefix):]
                if "/" not in name and name not in ("", ".", ".."):
                    return "audit/" + name
        raise ValueError("审计报告来源路径与交付目录不一致")

    def normalized(row):
        row = dict(row)
        row["source"] = source_name(row["source"])
        if row["source_locations"] != services.unknown:
            locations = json.loads(row["source_locations"])
            for location in locations:
                location["source"] = source_name(location["source"])
            row["source_locations"] = canonical_json(locations)
        return row

    with tempfile.TemporaryDirectory(prefix="pubmed-report-verify-") as folder:
        path = Path(folder) / "expected.csv"
        services.audit_report([str(p) for p in sorted((root / "audit").glob("*.jsonl"))],
                        str(path), skip_invalid=True)
        with path.open(encoding="utf-8-sig", newline="") as stream:
            expected = list(csv.DictReader(stream))
    try:
        matches = [normalized(row) for row in rows] == [normalized(row) for row in expected]
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("审计报告展示列格式或来源无效：" + str(exc)) from exc
    if not matches:
        raise ValueError("审计报告展示列与原始日志不一致")


def verify(root):
    root = Path(root).resolve()
    delivery = read_json(root / "delivery.json")
    if delivery.get("schema") != SCHEMA:
        raise ValueError("交付协议不支持")
    required = {"plan.json", "state.json", "all-pmids.txt", "all-pmids.txt.meta.json",
                "search-result.json", "screening.csv", "references.ris", "search-log.csv", "HANDOFF.md"}
    if not required.issubset(delivery["files"]):
        raise ValueError("交付清单缺少必需文件")
    check_files(root, delivery["files"])
    plan = validate_plan(read_json(root / "plan.json"))
    state = read_json(root / "state.json")
    if state.get("schema") != SCHEMA or state.get("plan_sha256") != digest(root / "plan.json"):
        raise ValueError("状态协议或方案绑定不一致")
    listing = services.read_pmid_file(str(root / "all-pmids.txt"))
    ids = listing["ids"]
    if listing["raw_count"] != len(ids):
        raise ValueError("固定 PMID 清单存在重复")
    stages = state["stages"]
    contracts = required_stages(plan, ids)
    if set(stages) != set(contracts):
        raise ValueError("必需阶段集合不一致；缺少：" + str(sorted(set(contracts) - set(stages)))
                         + "；多出：" + str(sorted(set(stages) - set(contracts))))
    index = AuditIndex(root)
    if not index.cache or not set(index.cache).issubset(delivery["files"]):
        raise ValueError("交付清单缺少审计原始文件")
    for name, paths in contracts.items():
        entry = stages[name]
        if set(entry["files"]) != paths:
            raise ValueError("阶段文件集合不一致：" + name)
        if any(delivery["files"].get(p) != entry["files"][p] for p in paths):
            raise ValueError("交付清单与阶段文件绑定不一致：" + name)
        check_files(root, entry["files"])
        if audit_event(root, entry["result"], index) != entry.get("audit_event_sha256"):
            raise ValueError("阶段审计事件已变化：" + name)
    exported = stages["search"]["result"]
    if read_json(root / "search-result.json") != {**exported, "evidence": EVIDENCE}:
        raise ValueError("检索回执与文件不一致")
    query = plan["term"]
    datetype = plan.get("datetype", "pdat")
    if plan.get("mindate"):
        lo = services.expand_date(services.check_date(plan["mindate"], "mindate"))
        hi = services.expand_date(services.check_date(plan["maxdate"], "maxdate"), True)
        query = f"({query}) AND ({lo}:{hi}[{datetype}])"
    expected_search = {"term": plan["term"], "effective_query": query, "datetype": datetype,
                       "format": "pmids", "unique": len(ids), "written_records": len(ids),
                       "sha256": digest(root / "all-pmids.txt")}
    if any(exported.get(k) != v for k, v in expected_search.items()):
        raise ValueError("检索回执与方案或固定名单不一致")
    meta = read_json(root / "all-pmids.txt.meta.json")
    if meta.get("schema") != "pubmed.export.v1" or any(meta.get(k) != v for k, v in expected_search.items()):
        raise ValueError("检索伴随说明与方案或固定名单不一致")
    for key in ("integrity_status", "integrity_issues", "count_start", "count_end", "querytranslation", "missing_pmids"):
        if meta.get(key) != exported.get(key):
            raise ValueError("检索伴随说明与回执不一致：" + key)
    if exported.get("audit_status") == "written" and meta.get("event_id") != exported.get("audit_event_id"):
        raise ValueError("检索伴随说明的审计事件绑定不一致")
    if exported.get("integrity_status") == "no_discrepancy_detected" and (
            exported.get("count_start") != len(ids) or exported.get("count_end") != len(ids)
            or exported.get("integrity_issues") or exported.get("missing_pmids")):
        raise ValueError("检索完整性状态与回执计数不一致")
    known = [item["pmid"] for item in plan.get("known_relevant", [])]
    recalls = []
    for start in range(0, len(known), services.recall_limit):
        name = f"recall-{start // services.recall_limit:05d}"  # 本项目自订：沿用回执编号
        result = stages[name]["result"]
        if read_json(root / "recall" / (name + ".json")) != {**result, "evidence": EVIDENCE}:
            raise ValueError("已知集回执与文件不一致：" + name)
        expected_ids = known[start:start + services.recall_limit]
        found = result["found"]
        absent = [item["pmid"] for item in result["missing"]]
        if (result.get("term") != query or result.get("gold_count") != len(expected_ids)
                or found != [p for p in expected_ids if p in set(found)]
                or absent != [p for p in expected_ids if p not in set(found)]):
            raise ValueError("已知集回执与方案批次不一致：" + name)
        recalls.append(result)
    records, batches = {}, []
    for name, data in record_batches(root, ids):
        stage_name = "records-" + Path(name).stem.removeprefix("batch-")
        result = stages[stage_name]["result"]
        if result.get("sha256") != digest(root / name) or result.get("missing_pmids") != data["missing_pmids"]:
            raise ValueError("题录回执与实际批次不一致：" + name)
        records.update({rec["pmid"]: rec for rec in data["records"]})
        batches.append(result)
    original = repair_origin(root, state)
    if original is not None:
        verify(original)
        for _, data in record_batches(original, ids):
            if any(records.get(rec["pmid"]) != rec for rec in data["records"]):
                raise ValueError("补取不能替换原先已取得的题录")
    materials = list(screening_materials(root, ids))
    expected_rows = [row for row, _ in materials]
    with (root / "screening.csv").open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        if reader.fieldnames != list(COLUMNS) or rows != expected_rows:
            raise ValueError("筛选表与题录不一致或已有人工修改；请保留工作副本，拒绝覆盖")
    if (root / "references.ris").read_text(encoding="utf-8") != "".join(block for _, block in materials):
        raise ValueError("RIS 与实际取回题录不一致")
    missing = [row["pmid"] for row in expected_rows if row["record_status"] == "missing"]
    no_ab = [row["pmid"] for row in expected_rows if row["abstract_status"] == "not_provided"]
    metrics = {"candidate_pmids": len(ids), "retrieved_records": len(records),
               "missing_pmids": missing, "no_abstract_pmids": no_ab}
    if metrics != delivery["metrics"]:
        raise ValueError("交付汇总与原始材料不一致")
    screened = {"missing_pmids": missing, "no_abstract_pmids": no_ab, "evidence": EVIDENCE,
                "known_missing_from_export": [p for p in known if p not in set(ids)]}
    if stages["screening"]["result"] != screened:
        raise ValueError("筛选回执与实际题录或已知集不一致")
    report = stages["report"]["result"]
    with (root / "search-log.csv").open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        report_rows = list(reader)
        if reader.fieldnames != list(services.report_columns):
            raise ValueError("审计报告列不完整")
    verify_report_rows(root, report, report_rows)
    invalid = [row for row in report_rows if row["event"] == "INVALID_INPUT"]
    reported = [canonical_json(json.loads(row["record"])) for row in report_rows if row["event"] != "INVALID_INPUT"]
    if (report.get("sha256") != digest(root / "search-log.csv")
            or report.get("bytes") != (root / "search-log.csv").stat().st_size
            or report.get("records") != len(index.report_records)
            or report.get("skipped") != len(index.diagnostics)
            or report.get("input_complete") != (not index.diagnostics)
            or len(invalid) != len(index.diagnostics)
            or sorted(reported) != sorted(index.report_records.values())):
        raise ValueError("审计报告与原始日志或回执不一致")
    issues = assess(plan, exported, recalls, batches, screened) + execution_issues(root, state, index)
    if issues != delivery["issues"]:
        raise ValueError("交付问题清单与证据不一致")
    expected = "needs_execution_repair" if any(i["kind"] == "execution" for i in issues) else (
        "needs_methodology_review" if issues else "ready_for_screening")
    if delivery["status"] != expected:
        raise ValueError("交付状态与待解决事项不一致")
    return {"status": "verified", "handoff_status": expected, "metrics": metrics,
            "directory": str(root), "audit_diagnostics": index.diagnostics,
            "evidence": "实测/工具输出：文件哈希、完整名单、必需阶段、题录及派生产物一致性核对；不证明来源真实性或查全，审计坏行仍是未解决缺口"}


def copy_verified(source, target, expected_sha):
    """原子发布新副本；初始化中断后只接受与原始字节一致的已有副本。"""
    payload = source.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_sha:
        raise ValueError("复制期间原文件已变化：" + str(source))
    if target.exists():
        if digest(target) != expected_sha:
            raise ValueError("已有修复副本不同，拒绝覆盖：" + str(target))
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".part", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        services.publish_no_clobber(temporary, str(target))
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def prepare_repair(source):
    source = Path(source).resolve()
    source_lock = acquire_lock(source)
    try:
        checked = verify(source)
        if not checked["metrics"]["missing_pmids"]:
            raise ValueError("原交付没有可自动补取的缺失题录")
        target = source.with_name(source.name + ".repair")
        if target.resolve() != target:
            raise ValueError("修复目录不能通过链接指向其它目录")
        target.mkdir(exist_ok=True)
        target_lock = acquire_lock(target)
        try:
            files = {}
            for path in sorted(source.rglob("*")):
                name = path.relative_to(source).as_posix()
                local_path(source, name)
                if path.is_file() and name != ".review.lock":
                    files[name] = digest(path)
            info = {"schema": SCHEMA, "source": str(source),
                    "delivery_sha256": digest(source / "delivery.json"), "files": files}
            marker = local_path(target, "repair-init.json")
            if marker.exists():
                if read_json(marker) != info:
                    raise ValueError("修复目录属于不同或已变化的原交付")
            else:
                if any(p.name != ".review.lock" for p in target.iterdir()):
                    raise ValueError("修复目录非空且无初始化标识，拒绝接管")
                write_json(marker, info)
            if (target / "state.json").exists():
                return target
            for name, sha in files.items():
                copy_verified(local_path(source, name), local_path(target, "original/" + name), sha)
            old_state = read_json(source / "state.json")
            kept = {name: entry for name, entry in old_state["stages"].items()
                    if name == "search" or name.startswith("recall-")
                    or (name.startswith("records-") and not entry["result"]["missing_pmids"])}
            copy_verified(source / "plan.json", local_path(target, "plan.json"), files["plan.json"])
            for entry in kept.values():
                for name, sha in entry["files"].items():
                    copy_verified(local_path(source, name), local_path(target, name), sha)
            for path in sorted((source / "audit").glob("*.jsonl")):
                name = path.relative_to(source).as_posix()
                copy_verified(path, local_path(target, "audit/source-" + path.name), files[name])
            state = {"schema": SCHEMA, "plan_sha256": digest(target / "plan.json"), "stages": kept,
                     "started_at": services.utc_now(), "source_sha256": digest(Path(services.source_path)),
                     "runner_sha256": digest(Path(__file__)), "implementation_sha256": services.implementation_fingerprint(),
                     "evidence": EVIDENCE,
                     "repair_source": info}
            services.write_json_atomic(str(target / "state.json"), state)
            return target
        finally:
            target_lock.close()
    finally:
        source_lock.close()


def preserve_uncommitted(root, name, paths):
    screening = root / "screening.csv"
    if "screening.csv" in paths and screening.exists() and screening.stat().st_size:
        with screening.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != list(COLUMNS):
                raise ValueError("未提交筛选表格式不明，拒绝移动或覆盖")
            for row in reader:
                if (row.get("reviewer_decision") != "待筛选" or row.get("exclusion_reason") != ""
                        or row.get("reviewer") != "" or None in row):
                    raise ValueError("未提交筛选表含人工修改或不完整行，拒绝移动或覆盖")
    archive = "uncommitted/" + name + "-" + uuid.uuid4().hex
    for relative in paths:
        path = local_path(root, relative)
        if path.exists():
            saved = local_path(root, archive + "/" + relative)
            saved.parent.mkdir(parents=True, exist_ok=True)
            path.rename(saved)
    print("保留未提交阶段产物：" + archive + "；将重新执行 " + name, file=sys.stderr, flush=True)


async def run(plan_path=None, out_dir=None, resume=None):
    if resume:
        root = Path(resume).resolve()
        if (root / "delivery.json").exists():
            checked = verify(root)
            if checked["metrics"]["missing_pmids"]:
                repaired = prepare_repair(root)
                return await run(resume=str(repaired))
            return checked
        if not (root / "state.json").exists() and (root / "repair-init.json").exists():
            info = read_json(root / "repair-init.json")
            source = Path(info["source"]).resolve()
            if source.with_name(source.name + ".repair") != root:
                raise ValueError("修复初始化标识与目录位置不一致")
            prepare_repair(source)
        state = read_json(root / "state.json")
        if state.get("schema") != SCHEMA:
            raise ValueError("恢复状态协议不支持")
        if digest(root / "plan.json") != state["plan_sha256"]:
            raise ValueError("方案已修改，不能混用旧检索结果；请新建交付目录")
        plan = validate_plan(read_json(root / "plan.json"))
    else:
        plan = validate_plan(read_json(plan_path))
        root = Path(out_dir).resolve()
        root.mkdir(parents=True, exist_ok=False)
        write_json(root / "plan.json", plan)
        state = {"schema": SCHEMA, "plan_sha256": digest(root / "plan.json"), "stages": {},
                 "started_at": services.utc_now(), "source_sha256": digest(Path(services.source_path)),
                 "runner_sha256": digest(Path(__file__)), "implementation_sha256": services.implementation_fingerprint(),
                 "evidence": EVIDENCE}
        services.write_json_atomic(str(root / "state.json"), state)
    if (state["source_sha256"] != digest(Path(services.source_path))
            or state["runner_sha256"] != digest(Path(__file__))
            or state.get("implementation_sha256") != services.implementation_fingerprint()):
        raise ValueError("实现已变化，请用原安装包恢复，或新建任务；不能混用格式化规则")
    lock = root / ".review.lock"
    lock_stream = acquire_lock(root)
    # A dedicated process owns this directory; restore configuration on every exit.
    with lock_stream, services.audit_session(root / "audit"):
        for part in ("audit", "records", "recall"):
            (root / part).mkdir(exist_ok=True)
        repair_origin(root, state)
        index = AuditIndex(root)
        if index.diagnostics:
            print("审计原始字节保留，以下问题尚未解决：" + canonical_json(index.diagnostics),
                  file=sys.stderr, flush=True)
        for entry in state["stages"].values():
            check_files(root, entry["files"])
            if audit_event(root, entry["result"], index) != entry.get("audit_event_sha256"):
                raise ValueError("原阶段审计事件已变化，拒绝继续")
        async def stage(name, paths, action):
            if name in state["stages"]:
                return state["stages"][name]["result"]
            if any((root / p).exists() for p in paths):
                if not state.get("repair_source"):
                    raise ValueError("发现未提交阶段文件，拒绝覆盖；保留此目录并新建任务：" + name)
                preserve_uncommitted(root, name, paths)
            print("执行：" + name, file=sys.stderr, flush=True)
            result = await action()
            index.refresh()
            files = {p: digest(root / p) for p in paths}
            state["stages"][name] = {"files": files, "result": result,
                                      "audit_event_sha256": audit_event(root, result, index)}
            services.write_json_atomic(str(root / "state.json"), state)
            return result

        async def search():
            result = json.loads(await services.export(plan["term"], str(root / "all-pmids.txt"),
                as_json=True, metadata=True, datetype=plan.get("datetype", "pdat"),
                mindate=plan.get("mindate", ""), maxdate=plan.get("maxdate", "")))
            write_json(root / "search-result.json", {**result, "evidence": EVIDENCE})
            if result.get("metadata_status") != "written":
                raise ValueError("名单已保留，但伴随说明失败；不能标为完成")
            return result
        exported = await stage("search", ["all-pmids.txt", "all-pmids.txt.meta.json", "search-result.json"], search)
        ids = services.read_pmid_file(str(root / "all-pmids.txt"))["ids"]
        known = [item["pmid"] for item in plan.get("known_relevant", [])]
        recalls = []
        for start in range(0, len(known), services.recall_limit):
            name = f"recall-{start // services.recall_limit:05d}"  # 本项目自订：回执编号
            path = "recall/" + name + ".json"
            async def recall():
                result = json.loads(await services.check_recall(exported["effective_query"], " ".join(known[start:start + services.recall_limit]), as_json=True))
                write_json(root / path, {**result, "evidence": EVIDENCE})
                return result
            recalls.append(await stage(name, [path], recall))
        batches = []
        for start in range(0, len(ids), BATCH_SIZE):
            name = f"records-{start // BATCH_SIZE:05d}"  # 本项目自订：回执编号
            path = f"records/batch-{start // BATCH_SIZE:05d}.json"  # 本项目自订：固定批次文件名
            async def fetch():
                if state.get("repair_source"):
                    return await retrieve_repair_batch(ids[start:start + BATCH_SIZE], str(root / path),
                                                       str(local_path(root, "original/" + path)))
                return await retrieve_batch(ids[start:start + BATCH_SIZE], str(root / path))
            batches.append(await stage(name, [path], fetch))
        async def screening():
            result = await anyio.to_thread.run_sync(render_screening, root, ids)
            result["known_missing_from_export"] = [pid for pid in known if pid not in set(ids)]
            return result
        screened = await stage("screening", ["screening.csv", "references.ris"], screening)
        async def audit_report():
            logs = sorted(str(p) for p in (root / "audit").glob("*.jsonl"))
            if not logs:
                raise ValueError("没有检索日志，不能完成交付")
            # skip_invalid 会生成显式 INVALID_INPUT 行；原始日志不修改，问题继续阻断就绪状态。
            return await anyio.to_thread.run_sync(
                lambda: services.audit_report(logs, str(root / "search-log.csv"), skip_invalid=True))
        await stage("report", ["search-log.csv"], audit_report)
        issues = assess(plan, exported, recalls, batches, screened) + execution_issues(root, state, index)
        status = "needs_execution_repair" if any(i["kind"] == "execution" for i in issues) else (
            "needs_methodology_review" if issues else "ready_for_screening")
        metrics = {"candidate_pmids": len(ids), "retrieved_records": len(ids) - len(screened["missing_pmids"]),
                   **{k: screened[k] for k in ("missing_pmids", "no_abstract_pmids")}}
        label = {"needs_execution_repair": "执行尚有缺口", "needs_methodology_review": "技术产物已生成，方法学待复核",
                 "ready_for_screening": "检索交付检查通过，可进入人工筛选"}[status]
        text = ["# 综述检索交付", "", "完成条件：delivery.json 存在且 --verify-review 核验成功；否则本目录尚未完成交付。", "",
                "条件满足后的状态：" + label, "", "研究问题：" + plan["question"],
                "", "范围：PubMed 检索与候选题录交付；不代表完成系统综述。", "",
                "候选 PMID：" + str(len(ids)) + "〔实测/来源：all-pmids.txt 非空唯一行；不是纳入数〕", "",
                "## 从哪里开始", "", "- 在 screening.csv 的工作副本中筛选；原表每条均为待筛选，缺摘要不自动排除。",
                "- references.ris 用于文献管理软件导入；实际导入兼容性需在所用软件核对。",
                "- all-pmids.txt 是本次完整基线；补检时保留并用名单差集比较，不用新增名单替换它。",
                "- plan.json 保存研究问题、标准与范围决策；search-result.json 保存实际执行式和警告。",
                "- records 保存带来源说明的题录快照；audit 与 search-log.csv 保存实际请求及过程。",
                "- delivery.json 汇总问题及文件哈希；核验命令仅证明内部一致性，不证明总体查全。", "",
                "## 待解决事项", ""]
        text += ["- " + i["kind"] + " / " + i["detail"] for i in issues] or ["- 本次自动交付检查未发现阻断项；仍需研究者进行筛选和学术判断。"]
        text += ["", "## 责任边界", "", "程序负责名单、题录、来源记录和文件一致性；研究者负责策略批准、多库范围、全文核对、纳入排除及论文结论。",
                 "已知集找回不能代表总体查全；PubMed 题录不替代全文；未提供摘要不等于文献不可用。", ""]
        # 若汇总发布被中断，保留原有文件，不猜测其完成状态。
        handoff = "\n".join(text)
        if (root / "HANDOFF.md").exists():
            if (root / "HANDOFF.md").read_text(encoding="utf-8") != handoff:
                raise ValueError("已有交接说明与本次证据不同，拒绝覆盖")
        else:
            services.write_new_file(str(root / "HANDOFF.md"), handoff)
        inventory = {}
        for path in sorted(root.rglob("*")):
            if path.is_file() and path != lock:
                rel = path.relative_to(root).as_posix()
                local_path(root, rel)
                inventory[rel] = digest(path)
        delivery = {"schema": SCHEMA, "status": status, "metrics": metrics, "issues": issues,
                    "files": inventory, "completed_at": services.utc_now(), "evidence": EVIDENCE,
                    "policy": "状态与验收规则为本项目自订；哈希不是签名，不能证明来源真实性"}
        write_json(root / "delivery.json", delivery)
        return verify(root)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="生成或核验可追溯的 PubMed 综述检索交付目录")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--review-plan")
    group.add_argument("--resume-review")
    group.add_argument("--verify-review")
    parser.add_argument("--out-dir")
    args = parser.parse_args(argv)
    if bool(args.review_plan) != bool(args.out_dir):
        parser.error("--review-plan 必须配 --out-dir；恢复和核验不接受 --out-dir")
    try:
        if args.verify_review:
            result = verify(args.verify_review)
        else:
            result = anyio.run(run, args.review_plan, args.out_dir, args.resume_review)
        print(json.dumps(result, ensure_ascii=False))
        if result["handoff_status"] == "needs_execution_repair":
            return 2  # 本项目自订：已有材料但执行不完整
        return 0  # 本项目自订：命令成功；学术待确认事项仍须查看 handoff_status
    except (services.error_type, OSError, ValueError, KeyError, TypeError) as exc:
        print(services.mask(str(exc)), file=sys.stderr)
        return 1  # 本项目自订：执行或文件核验失败


if __name__ == "__main__":
    raise SystemExit(main())

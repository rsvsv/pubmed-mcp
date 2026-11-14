"""Offline audit validation and report generation, without a service import.

File publication, redaction and artifact verification are explicit call dependencies.
"""
import csv
import datetime
import hashlib
import io
import json
import os


# ---------- 离线检索记录表 ----------
_AUDIT_UNKNOWN = "⚠️【待确认】"
_REPORT_COLUMNS = ("source", "source_sha256", "source_line", "source_locations", "event_id", "run_id", "database", "platform",
                   "event", "outcome", "started_at", "finished_at", "legacy_ts", "query", "querytranslation",
                   "input_params", "request_params", "counts", "path", "sha256", "integrity_status",
                   "missing_pmids", "warnings", "file_verification", "record", "evidence")


def _report_value(value):
    if value is None or value == "":
        return _AUDIT_UNKNOWN
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    # 空字符使用可见转义，兼容旧版 CSV 写入器；record 中保留 JSON 原值。
    return str(value).replace("\x00", "\\u0000")


def validate_audit_record(rec, audit_schema):
    if not isinstance(rec, dict) or not isinstance(rec.get("event"), str) or not rec["event"].strip():
        raise ValueError("记录必须是含非空 event 字符串的对象")
    if "audit_schema" in rec:
        if rec["audit_schema"] != audit_schema:
            raise ValueError("不支持的 audit_schema")
        for key in ("event_id", "run_id", "started_at", "finished_at"):
            if not isinstance(rec.get(key), str) or not rec[key].strip():
                raise ValueError("缺少有效 " + key)
        for key in ("started_at", "finished_at"):
            dt = datetime.datetime.fromisoformat(rec[key].replace("Z", "+00:00"))
            if dt.utcoffset() is None:
                raise ValueError(key + " 缺少时区")
        if datetime.datetime.fromisoformat(rec["finished_at"].replace("Z", "+00:00")) < datetime.datetime.fromisoformat(rec["started_at"].replace("Z", "+00:00")):
            raise ValueError("结束时间早于开始时间")
    if "event_id" in rec and (not isinstance(rec["event_id"], str) or not rec["event_id"].strip()):
        raise ValueError("event_id 必须为非空字符串")


def audit_report(inputs, out, *, skip_invalid=False, verify_files=False,
                 check_out_path, error_type, mask, validate_record,
                 verify_artifact, write_new_file):
    """读取明确给出的日志生成记录表；不联网、不相加命中数、不猜测缺失证据。"""
    output = check_out_path(out)
    if not output:
        raise error_type("报告输出路径不能为空")
    ext = os.path.splitext(output)[1].lower()
    if ext not in (".md", ".csv"):
        raise error_type("报告输出仅支持 .md 或 .csv")
    if not inputs:
        raise error_type("至少提供一个审计日志")
    seen_files, seen_events, records, issues = set(), {}, [], []
    locations = {}

    def invalid(source, line, message, digest):
        if not skip_invalid:
            raise error_type(f"审计记录无效：{source}:{line}：{message}；未生成报告")
        issues.append({"source": source, "source_line": line, "source_sha256": digest, "error": message})

    def reject_constant(value):
        raise ValueError("JSON 不允许 " + value)

    def unique_keys(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("JSON 包含重复键")
            obj[key] = value
        return obj

    for raw_path in inputs:
        path = os.path.abspath(os.path.expanduser(raw_path))
        try:
            with open(path, "rb") as f:
                stat = os.fstat(f.fileno())
                identity = (stat.st_dev, stat.st_ino) if stat.st_ino else os.path.normcase(os.path.realpath(path))
                if identity in seen_files:
                    continue
                seen_files.add(identity)
                payload = f.read()
        except OSError as e:
            raise error_type("无法读取审计日志：" + mask(f"{path}: {e}")) from e
        digest = hashlib.sha256(payload).hexdigest()
        for line, raw in enumerate(payload.splitlines(), start=1):  # 本项目自订：源行号从首行计起
            try:
                text = raw.decode("utf-8-sig" if line == 1 else "utf-8")  # 本项目自订：仅文件首行允许 BOM
                if not text.strip():
                    continue
                rec = json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_keys)
                validate_record(rec)
                canonical = json.dumps(rec, ensure_ascii=False, sort_keys=True, allow_nan=False)
                canonical.encode("utf-8")  # 拒绝 JSON 转义中的孤立代理字符。
            except (ValueError, TypeError, OverflowError, RecursionError) as e:
                invalid(path, line, type(e).__name__ + ": " + str(e), digest)
                continue
            event_id = rec.get("event_id")
            location_key = ("event", event_id) if event_id else ("legacy", path, line)
            locations.setdefault(location_key, []).append({"source": path, "sha256": digest, "line": line})
            if event_id and event_id in seen_events:
                previous, location = seen_events[event_id]
                if previous != canonical:
                    # 冲突绝不允许 skip-invalid 任取一条。
                    raise error_type(f"event_id 内容冲突：{location} 与 {path}:{line}；未生成报告")
                continue
            if event_id:
                seen_events[event_id] = (canonical, f"{path}:{line}")
            records.append((path, digest, line, rec))
    rows = []
    for path, digest, line, rec in records:
        query = rec.get("term")
        if query is None and isinstance(rec.get("input_params"), dict):
            query = rec["input_params"].get("term")
        if query is None and ("term_a" in rec or "term_b" in rec):
            query = {k: rec[k] for k in ("term_a", "term_b") if k in rec}
        counts = {k: rec[k] for k in ("count", "count_start", "count_end", "count_a", "count_b", "returned",
                  "requested", "unique", "written_records", "gold", "found", "missing", "added", "removed",
                  "common", "descriptors", "direct_total", "errors") if k in rec}
        translation = rec.get("querytranslation", rec.get("querytranslations"))
        if translation is None and ("querytranslation_a" in rec or "querytranslation_b" in rec):
            translation = {k: rec[k] for k in ("querytranslation_a", "querytranslation_b") if k in rec}
        key = ("event", rec["event_id"]) if rec.get("event_id") else ("legacy", path, line)
        event = rec["event"].lstrip("_")
        params = rec.get("input_params")
        if not isinstance(params, dict):
            params = {}
        requests = rec.get("logical_requests")
        local = event == "diff_files" or (
            event in ("export_pmids", "resume_export") and rec.get("format", params.get("format")) == "pmids")
        if local:
            database, platform = "不适用（本地文件）", "本地文件处理"
        elif requests:
            database = "MeSH" if event == "mesh" else "PubMed"
            platform = "NCBI E-utilities"
        else:
            database, platform = _AUDIT_UNKNOWN, _AUDIT_UNKNOWN
        if event == "links" and requests:
            database += " → " + str(rec.get("to", _AUDIT_UNKNOWN))
        row = {"source": path, "source_sha256": digest, "source_line": line,
               "source_locations": locations[key], "database": database,
               "platform": platform, "legacy_ts": rec.get("ts"),
               "query": query, "querytranslation": translation,
               "input_params": rec.get("input_params"),
               "request_params": rec.get("logical_requests", rec.get("requests") if isinstance(rec.get("requests"), list)
                                         else rec.get("request_params")),
               "counts": counts if counts else None, "record": rec,
               "evidence": "来源：审计日志该文件/行；未重新联网核验，不代表纳入数或查全率"}
        for key in ("event_id", "run_id", "event", "outcome", "started_at", "finished_at", "path", "sha256",
                    "integrity_status", "missing_pmids", "warnings"):
            row[key] = rec.get(key)
        if row["warnings"] is None and ("warnings_a" in rec or "warnings_b" in rec):
            row["warnings"] = {k: rec[k] for k in ("warnings_a", "warnings_b") if k in rec}
        row["file_verification"] = "not_requested"
        if verify_files and isinstance(rec.get("path"), str) and rec["path"]:
            row["file_verification"] = verify_artifact(rec)
        rows.append({k: _report_value(row.get(k)) for k in _REPORT_COLUMNS})
    for issue in issues:
        row = {k: _AUDIT_UNKNOWN for k in _REPORT_COLUMNS}
        row.update({k: str(v) for k, v in issue.items() if k in row})
        row.update(event="INVALID_INPUT", outcome="skipped", warnings=issue["error"],
                   evidence="输入不完整：此来源行未解析，不能作为检索记录")
        rows.append(row)
    if ext == ".csv":
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=_REPORT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            # 防止日志内容被表格软件解释成公式；原始内容仍可回查日志。
            writer.writerow({k: ("'" + v if v.lstrip().startswith(("=", "+", "-", "@")) or v.startswith(("\t", "\r")) else v)
                             for k, v in row.items()})
        body = "﻿" + stream.getvalue()
    else:
        def cell(value):
            return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;").replace("\r", " ").replace("\n", "<br>")
        body = ("# 检索记录表\n\n" + ("⚠️ 输入不完整：无效行已跳过，详见 INVALID_INPUT 行。\n\n" if issues else "")
                + "每行对应日志事件；不汇总命中数为纳入数。旧字段缺失记为待确认；不是完整 PRISMA-S 合规报告。\n\n"
                + "| " + " | ".join(_REPORT_COLUMNS) + " |\n"
                + "| " + " | ".join("---" for _ in _REPORT_COLUMNS) + " |\n"
                + "\n".join("| " + " | ".join(cell(row[k]) for k in _REPORT_COLUMNS) + " |" for row in rows) + "\n")
    path, nbytes, sha = write_new_file(output, body)
    return {"path": path, "records": len(records), "skipped": len(issues), "input_complete": not issues,
            "bytes": nbytes, "sha256": sha}

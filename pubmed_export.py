"""export implementation; no core or MCP registration import.
Dependencies are an explicit, per-instance, late-bound contract.
Historical evidence and policy comments are retained from the source.
"""
import functools
import csv
import datetime
import hashlib
import io
import json
import os
import re
import sys
import uuid
from typing import Literal
import anyio
from pydantic import StrictInt, StrictStr

DEPENDENCIES = ('EFETCH_CHUNK', 'ESUMMARY_CHUNK', 'EXPORT_MAX', 'NO_BEGIN', 'NO_END', 'NcbiError', 'WINDOW', '_AUDIT_CONTEXT', '_EXPORT_BATCH_EXT', '_EXPORT_BATCH_SIZE', '_EXPORT_FORMATS', '_EXPORT_FORMAT_VERSION', '_EXPORT_JOB_SCHEMA', '_EXPORT_JOB_VERSION', '_EXPORT_RESULT_SCHEMA', '_EXPORT_STATE_DIR', '_EXPORT_STATE_SCHEMA', '_PMIDS_CSV_HEADER', '_REQ_CTR', '__version__', '_acquire_job_lock', '_add_job_warning', '_as_bool', '_audit', '_audit_clean', '_batch_file_path', '_batch_rel', '_batch_state_binding', '_batch_state_path', '_batch_state_payload', '_check_date', '_check_datetype', '_check_job_output', '_check_out_path', '_check_reldate', '_collect_window_checks', '_concat_batches_to_file', '_count_of', '_date_mid', '_efetch_xml_all', '_esearch', '_esummary_dicts', '_execute_export_job', '_expand', '_export_csv', '_export_csv_batch', '_export_job_batches', '_export_medline', '_export_medline_batch', '_export_metadata', '_export_ris', '_export_ris_batch', '_export_run', '_export_slice', '_fixed_job_path', '_freeze_export_query', '_get_async', '_ids_in_window', '_inspect_batch_file', '_job_path', '_load_batch_state', '_load_job_manifest', '_mask', '_new_job_manifest', '_next_day', '_normalize_writer_output', '_paths_same', '_pmid_binding', '_pmid_ids_sha256', '_publish_export_file', '_publish_no_clobber', '_read_pmid_file', '_record_count_change', '_record_parts', '_release_job_lock', '_remove_batch_state', '_required_windows', '_reset_batch_for_refetch', '_retrieval_check', '_revalidate_job_input', '_ris_block', '_save_batch_state', '_sha256_file', '_store_batch', '_stream_export', '_tool_version', '_utc_now', '_validate_job_manifest', '_verify_done_batch', '_verify_export', '_warn_bits', '_write_json_atomic', '_write_new_file', '_write_text', '_zero_explanation')


class Implementation:
    def __init__(self, dependencies):
        self.dependencies = dependencies

    def _date_mid(self, lo, hi):
        a = datetime.date.fromisoformat(lo)
        b = datetime.date.fromisoformat(hi)
        return (a + (b - a) / 2).isoformat()


    def _next_day(self, d):
        return (datetime.date.fromisoformat(d) + datetime.timedelta(days=1)).isoformat()


    async def _export_slice(self, term, lo, hi, state, *, expected_count=None):
        """按发表日期递归二分到 WINDOW 内；返回 (ids, 片数, 切片计数和)。
        每次计数探测（含空片、内部节点）和取号分页都保留检查记录。"""
        context = f"export 切片 {lo}→{hi}："
        d = await self.dependencies._esearch(term, retmax=0, mindate=lo, maxdate=hi)  # 本项目自订：只探测计数
        c = self.dependencies._count_of(d)
        state.setdefault("checks", []).append(self.dependencies._retrieval_check(d, context + "计数探测"))
        if expected_count is not None:
            self.dependencies._record_count_change(state, expected_count, c, context + "起始总数→根切片：")
        if c == 0:  # 本项目自订：空片无需取号
            return [], 0, 0  # 本项目自订：空片返回计数口径
        if c <= self.dependencies.WINDOW:
            ids, c2, first, drift = await self.dependencies._ids_in_window(term, mindate=lo, maxdate=hi, context=context)
            self.dependencies._collect_window_checks(state, first, c, c2, context)
            if drift:
                state.setdefault("drift", []).append(drift)
            return ids, 1, c  # 本项目自订：一个非空叶片，保留探测时计数
        mid = self.dependencies._date_mid(lo, hi)
        if lo >= hi or mid < lo or mid >= hi:
            raise self.dependencies.NcbiError(f"export：区间 {lo}→{hi} 仍有 {c} 条超过 {self.dependencies.WINDOW} 窗口且无法再二分；"
                            f"请缩小查询（加字段/日期限定）后重试。")
        lids, ln, lc = await self.dependencies._export_slice(term, lo, mid, state)
        rids, rn, rc = await self.dependencies._export_slice(term, self.dependencies._next_day(mid), hi, state)
        if lc + rc != c:
            state.setdefault("drift", []).append(
                f"⚠️ {context}子片计数和 {lc + rc} ≠ 父片计数 {c}；"
                f"可能有跨日期重复或检索期间更新，请复核。")
        return lids + rids, ln + rn, lc + rc


    async def _export_csv_batch(self, path, ids):
        """核心题录字段写 CSV〔字段本项目自订〕；utf-8-sig 便于 Excel 直接打开。
        元数据缺失/不可用的行在 note 列标明，不静默丢弃。返回 (记录数, 问题号列表)。"""
        recs = dict(await self.dependencies._esummary_dicts(ids, include_unavailable=True))
        problems = []
        rows = [list(self.dependencies._PMIDS_CSV_HEADER)]
        for p in ids:
            r = recs.get(p)
            if r is None:
                rows.append([p, "", "", "", "", "", "", "", "", "元数据缺失"])
                problems.append(p)
                continue
            if r.get("error"):
                rows.append([p, "", "", "", "", "", "", "", "", "esummary 不可用：" + str(r.get("error"))[:80]])
                problems.append(p)
                continue
            parts = self.dependencies._record_parts(p, r)
            rows.append([parts["pmid"], parts["pubdate"], parts["title"], parts["journal"],
                         parts["authors"], parts["author_count"], parts["doi"], parts["pmc"],
                         "/".join(parts["pubtype"]), ""])

        def write():
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                csv.writer(f).writerows(rows)
        await anyio.to_thread.run_sync(write)     # 〔1.4.5〕大文件写盘不占事件循环（契约 #9 原只覆盖网络）
        return len(ids) - len(problems), problems


    async def _export_medline_batch(self, path, ids):
        """efetch MEDLINE 格式（.nbib）分批写文件。按 ^PMID- 行校验归属（MEDLINE 排版用 PMID-，
        与摘要端的 PMID: 不同）。返回 (PMID- 行数, 缺记录的号)。"""
        chunks = [ids[i:i + self.dependencies.EFETCH_CHUNK] for i in range(0, len(ids), self.dependencies.EFETCH_CHUNK)]
        out = [None] * len(chunks)
        errs = []

        async def one(i, c):
            try:
                text = await self.dependencies._get_async("efetch.fcgi",
                                        {"db": "pubmed", "id": ",".join(c), "rettype": "medline", "retmode": "text"},
                                        min(60, 20 + 0.4 * len(c)))
                head = text.lstrip()[:200].lower()
                if head.startswith("<!doctype") or head.startswith("<html"):
                    raise self.dependencies.NcbiError("efetch 回的是 HTML 页面（HTTP 200 也可能是故障页），不是 MEDLINE 正文。原文开头："
                                    + self.dependencies._mask(text.lstrip()[:200]))
                got = re.findall(r"^PMID- (\d+)", text, re.M)
                if len(got) != len(set(got)) or not set(got).issubset(c):
                    raise self.dependencies.NcbiError("efetch MEDLINE 返回的 PMID 与请求不符或重复，不能作为有效导出")
                if not got and not re.fullmatch(r"\s*(?:\d+\.\s*)+", text):
                    raise self.dependencies.NcbiError("efetch 返回了无法识别的 MEDLINE 正文；原文开头：" + self.dependencies._mask(text.lstrip()[:200]))
                blocks = re.split(r"(?m)(?=^PMID- \d+)", text)
                by_id = {}
                for block in blocks:
                    match = re.match(r"PMID- (\d+)", block)
                    if match:
                        by_id[match[1]] = block.strip()
                ordered = [pid for pid in c if pid in by_id]
                out[i] = text if ordered == got else "\n\n".join(by_id[pid] for pid in ordered) + ("\n" if ordered else "")
            except self.dependencies.NcbiError as e:
                errs.append(e)

        async with anyio.create_task_group() as tg:
            for i, c in enumerate(chunks):
                tg.start_soon(one, i, c)
        if errs:
            raise errs[0]
        # 全无效号的批只回「1. 」占位（与 fetch_abstracts 同一现象），不写进 .nbib。
        text = "\n\n".join(t for t in out if t and re.search(r"^PMID- \d+", t, re.M))
        await anyio.to_thread.run_sync(self.dependencies._write_text, path, text + ("\n" if text else ""))
        got = re.findall(r"^PMID- (\d+)", text, re.M)
        missing = [p for p in ids if p not in set(got)]
        return len(got), missing


    async def _export_ris_batch(self, path, ids):
        """efetch XML 分批解析后写 RIS。返回 (记录数, 缺记录的号)。"""
        recs = await self.dependencies._efetch_xml_all(ids)
        by_id = {r["pmid"]: r for r in recs}
        if len(by_id) != len(recs) or not set(by_id).issubset(ids):
            raise self.dependencies.NcbiError("RIS 返回记录归属错误或重复")
        recs = [by_id[pid] for pid in ids if pid in by_id]
        await anyio.to_thread.run_sync(self.dependencies._write_text, path, "\n\n".join(self.dependencies._ris_block(r) for r in recs) + ("\n" if recs else ""))
        got = {r["pmid"] for r in recs}
        return len(recs), [p for p in ids if p not in got]


    async def _stream_export(self, path, ids, format):
        """固定并发上限，按输入顺序合并批文件；内存不保留全部题录。"""
        import tempfile
        import shutil
        writer = {"csv": self.dependencies._export_csv_batch, "ris": self.dependencies._export_ris_batch, "medline": self.dependencies._export_medline_batch}[format]
        size = self.dependencies.ESUMMARY_CHUNK if format == "csv" else self.dependencies.EFETCH_CHUNK
        width = 3  # 本项目自订：同时抓取批次数；全局网络限速仍由 _get_async 执行
        if len(ids) <= size:
            return await writer(path, ids)
        problems, completed = [], 0
        with tempfile.TemporaryDirectory(prefix="pubmed-batches-", dir=os.path.dirname(os.path.abspath(path))) as folder:
            with open(path, "wb"):
                pass
            first = True
            for base in range(0, len(ids), size * width):
                batches = [ids[i:i + size] for i in range(base, min(len(ids), base + size * width), size)]
                outputs = [os.path.join(folder, str(i)) for i in range(len(batches))]
                results, errors = [None] * len(batches), []
                async def one(i):
                    try:
                        results[i] = await writer(outputs[i], batches[i])
                    except Exception as exc:
                        errors.append(exc)
                async with anyio.create_task_group() as tg:
                    for i in range(len(batches)):
                        tg.start_soon(one, i)
                if errors:
                    raise errors[0]
                def append():
                    nonlocal first
                    with open(path, "ab") as target:
                        for source in outputs:
                            if not os.path.getsize(source):
                                os.unlink(source)
                                continue
                            with open(source, "rb") as stream:
                                if format == "csv" and not first:
                                    stream.readline()  # 批头含 BOM，最终只保留首批表头
                                elif format != "csv" and not first:
                                    target.write(b"\n")
                                shutil.copyfileobj(stream, target)
                            first = False
                            os.unlink(source)
                await anyio.to_thread.run_sync(append)
                for count, missing in results:
                    completed += count
                    problems.extend(missing)
        return completed, problems


    async def _export_csv(self, path, ids):
        return await self.dependencies._stream_export(path, ids, "csv")


    async def _export_ris(self, path, ids):
        return await self.dependencies._stream_export(path, ids, "ris")


    async def _export_medline(self, path, ids):
        return await self.dependencies._stream_export(path, ids, "medline")


    def _publish_no_clobber(self, tmp, p):
        """原子发布且不覆盖。无可用原语时失败，不退回 exists + replace。"""
        try:
            if os.name == "nt":
                os.rename(tmp, p)  # Windows rename 在目标存在时失败；POSIX rename 会覆盖。
            else:
                os.link(tmp, p)  # 同目录硬链接：目标存在时原子失败。
        except FileExistsError as e:
            raise self.dependencies.NcbiError(f"文件已存在：{p}。本工具不覆盖已有文件，请换个路径或先移走旧文件。") from e
        except OSError as e:
            raise self.dependencies.NcbiError(f"无法安全发布导出文件（{p}）：{e}；请使用支持原子不覆盖发布的本地文件系统。") from e


    async def _freeze_export_query(self, term, datetype, mindate, maxdate, reldate):
        """固定用户日期条件；内部拆片不得覆盖此条件。相对区间只认独立 NCBI 回显。"""
        self.dependencies._check_datetype(datetype)
        if not isinstance(term, str) or not term.strip():
            raise self.dependencies.NcbiError("term 不能为空")
        lo, hi = self.dependencies._check_date(mindate, "mindate"), self.dependencies._check_date(maxdate, "maxdate")
        rel = self.dependencies._check_reldate(reldate)
        query = term
        if lo or hi:
            lo, hi = self.dependencies._expand(lo or self.dependencies.NO_BEGIN), self.dependencies._expand(hi or self.dependencies.NO_END, True)
            if lo > hi:
                raise self.dependencies.NcbiError("日期区间倒挂：mindate 晚于 maxdate")
            query = f'({query}) AND ({lo}:{hi}[{datetype}])'
        if rel:
            probe = await self.dependencies._esearch("all[sb]", retmax=0, reldate=rel, datetype=datetype)
            windows = list(dict.fromkeys(self.dependencies._required_windows(probe)))
            label = "发表日期[pdat]" if datetype == "pdat" else "入库日期[edat]"
            if len(windows) != 1 or windows[0][2] != label or any(self.dependencies._warn_bits(probe)):
                raise self.dependencies.NcbiError("无法从 NCBI 回显核实相对日期窗口；请改用 mindate/maxdate 显式区间导出")
            a, b, _ = windows[0]
            self.dependencies._check_date(a, "NCBI mindate")
            self.dependencies._check_date(b, "NCBI maxdate")
            if a > b:
                raise self.dependencies.NcbiError("NCBI 相对日期回显区间倒挂；请改用显式区间")
            query = f'({query}) AND ({a}:{b}[{datetype}])'
        return query


    async def _export_metadata(self, path, record, enabled):
        if not enabled:
            return {"metadata_status": "not_requested"}
        context = self.dependencies._AUDIT_CONTEXT.get() or {}
        meta = {"schema": "pubmed.export.v1", "version": self.dependencies._tool_version(),
                "run_id": context.get("run_id") or uuid.uuid4().hex,
                "event_id": context.get("event_id") or uuid.uuid4().hex,
                **record, "completed_at": self.dependencies._utc_now(),
                "evidence": "来源：本次工具检索与文件写入记录；哈希不证明查全"}
        try:
            p, _, _ = await anyio.to_thread.run_sync(
                self.dependencies._write_new_file, path + ".meta.json", json.dumps(self.dependencies._audit_clean(meta), ensure_ascii=False, allow_nan=False))
            return {"metadata_status": "written", "metadata_path": p}
        except Exception as exc:
            return {"metadata_status": "failed", "metadata_error": self.dependencies._mask(str(exc))}


    def _verify_export(self, path, meta_path=""):
        """离线核验明确指定的数据与伴随文件，只证明字节绑定。"""
        data = os.path.abspath(os.path.expanduser(path))
        meta = os.path.abspath(os.path.expanduser(meta_path)) if meta_path else data + ".meta.json"
        result = {"path": data, "metadata_path": meta,
                  "scope": "仅核验文件哈希，不证明检索完整性"}
        if not os.path.isfile(data):
            return {**result, "status": "missing_data"}
        if not os.path.isfile(meta):
            return {**result, "status": "missing_meta"}
        try:
            with open(meta, encoding="utf-8-sig") as f:
                rec = json.load(f)
            if (not isinstance(rec, dict) or rec.get("schema") != "pubmed.export.v1"
                    or not isinstance(rec.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", rec["sha256"])):
                raise ValueError("不支持或损坏的导出元数据")
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            return {**result, "status": "invalid_meta", "error": self.dependencies._mask(str(exc))}
        try:
            actual = self.dependencies._sha256_file(data)
        except FileNotFoundError as exc:
            return {**result, "status": "missing_data", "error": self.dependencies._mask(str(exc))}
        except OSError as exc:
            return {**result, "status": "unreadable_data", "error": self.dependencies._mask(str(exc))}
        return {**result, "status": "verified" if actual == rec["sha256"] else "hash_mismatch",
                "expected_sha256": rec["sha256"], "actual_sha256": actual}


    def _verify_audit_artifact(self, rec):
        """分别核对当前文件对和日志事件绑定；不把另一轮文件冒充本事件产物。"""
        meta_path = rec.get("metadata_path", "")
        if not isinstance(meta_path, str):
            result = {"status": "invalid_meta", "error": "日志 metadata_path 不是字符串"}
        else:
            result = self.dependencies._verify_export(rec["path"], meta_path)
        expected = rec.get("sha256")
        result["metadata_verification_status"] = result["status"]
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            result["event_binding_status"] = "missing_or_invalid_hash"
            if result["status"] == "verified":
                result["status"] = "event_hash_unavailable"
            return result
        result["event_expected_sha256"] = expected
        actual = result.get("actual_sha256")
        if actual is None:
            try:
                actual = self.dependencies._sha256_file(rec["path"])
                result["actual_sha256"] = actual
            except (OSError, ValueError) as exc:
                result["event_binding_status"] = "not_verified"
                result["event_binding_error"] = self.dependencies._mask(str(exc))
                return result
        if actual != expected:
            result["event_binding_status"] = "hash_mismatch"
            result["status"] = "event_hash_mismatch"
        else:
            result["event_binding_status"] = "verified"
        return result


    async def export(self, term: str, path: str, format: Literal["pmids", "csv", "medline", "ris"] = "pmids",
                     as_json: bool | str = False, metadata: bool | str = False,
                     datetype: Literal["pdat", "edat"] = "pdat", mindate: str = "", maxdate: str = "",
                     reldate: StrictInt | StrictStr = 0) -> str:
        """取全某查询的 PMID 并写文件；超 9999 窗口自动按发表日期二分切片。
        format: pmids / csv（核心题录，不含摘要）/ medline（.nbib）/ ris（全作者、摘要、文章号，供文献管理软件导入）。
        只回摘要（总数/片数/去重/路径/sha256），不回记录本身。path 显式给出且不得已存在（不覆盖）。
        切片计数与总数核对，不一致如实报告；检索期间数据库可能更新，结果以导出时点为准。
        自订上限 10 万条；导入兼容性须实测。metadata 开启伴随说明；日期类型 pdat/edat，后者超窗拒绝。
        """
        if format not in ("pmids", "csv", "medline", "ris"):
            raise self.dependencies.NcbiError("format 只支持 pmids / csv / medline / ris")
        json_mode = self.dependencies._as_bool(as_json, "as_json")
        raw_p = os.path.expanduser((path or "").strip())
        if not raw_p:
            raise self.dependencies.NcbiError("path 不能为空；请给明确的输出文件路径。")
        p = os.path.abspath(raw_p)
        if os.path.isdir(p):
            raise self.dependencies.NcbiError(f"path 是已存在的目录而非文件路径：{p}")
        if os.path.exists(p):
            raise self.dependencies.NcbiError(f"文件已存在：{p}。本工具不覆盖已有文件，请换个路径或先移走旧文件。")
        meta_enabled = self.dependencies._as_bool(metadata, "metadata")
        self.dependencies._check_datetype(datetype)
        if meta_enabled:
            self.dependencies._check_out_path(p + ".meta.json")
        ctr = []
        token = self.dependencies._REQ_CTR.set(ctr)
        try:
            query = await self.dependencies._freeze_export_query(term, datetype, mindate, maxdate, reldate)
            return await self.dependencies._export_run(term, p, format, json_mode, ctr, query=query,
                                     metadata=meta_enabled, datetype=datetype)
        finally:
            self.dependencies._REQ_CTR.reset(token)


    async def _export_run(self, term, p, format, json_mode, ctr, *, query=None, metadata=False, datetype="pdat"):
        """执行导出；请求计数上下文由公开入口管理，成功、异常与取消均恢复。"""
        query = term if query is None else query
        t0 = datetime.datetime.now().isoformat(timespec="seconds")
        d0 = await self.dependencies._esearch(query, retmax=0)
        total = self.dependencies._count_of(d0)
        qt = (d0.get("querytranslation") or "").strip()
        warn = []  # 全部响应提示在写出结果前统一汇总，保留阶段和重复出现的证据。
        if total > self.dependencies.EXPORT_MAX:
            raise self.dependencies.NcbiError(f"查询命中 {total} 条，超过 export 本项目自订安全上限 {self.dependencies.EXPORT_MAX}；"
                            f"请缩小查询（加字段/日期限定）后重试。")
        if datetype == "edat" and total > self.dependencies.WINDOW:
            raise self.dependencies.NcbiError("edat 超窗拆片尚未经联网集合验证；请给定更小入库日期区间，或明确选择 pdat 拆片；未给日期时 edat 不用于筛选，仍不自动改换拆片类型")
        state = {"drift": [], "checks": [self.dependencies._retrieval_check(d0, "export 起始计数：")], "count_changed": False}
        if total <= self.dependencies.WINDOW:
            ids, cnt, first, drift = await self.dependencies._ids_in_window(query, context="export：")
            n_slices, slice_sum = 1, cnt  # 原文/来源：现有单窗口导出的片数口径
            self.dependencies._collect_window_checks(state, first, total, cnt, "export：")
            if drift:
                state["drift"].append(drift)
        else:
            ids, n_slices, slice_sum = await self.dependencies._export_slice(
                query, self.dependencies.NO_BEGIN, self.dependencies.NO_END, state, expected_count=total)
        raw_n = len(ids)
        ids = list(dict.fromkeys(ids))
        d_end = await self.dependencies._esearch(query, retmax=0)
        total_end = self.dependencies._count_of(d_end)
        state["checks"].append(self.dependencies._retrieval_check(d_end, "export 结束计数："))
        t1 = datetime.datetime.now().isoformat(timespec="seconds")
        mismatch = []
        if slice_sum != total:
            mismatch.append(f"⚠️ 切片计数和 {slice_sum} ≠ 起始总数 {total}：可能有记录带多个发表日期，"
                            f"或检索期间数据库更新；已按 PMID 去重。")
        if total_end != total:
            mismatch.append(f"⚠️ 结束重数 {total_end} ≠ 起始 {total}：检索期间数据库有更新，结果以导出时点为准。")
        if raw_n != len(ids):
            mismatch.append(f"⚠️ 切片间有重复：取回 {raw_n} 个、去重后 {len(ids)} 个。")
        if len(ids) != total:
            mismatch.append(f"⚠️ 实际取回 {len(ids)} 个 ≠ 命中数 {total}：请复核（数据库可能更新，"
                            f"或无发表日期的记录未被日期切片覆盖）。")
        parent = os.path.dirname(p)
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            raise self.dependencies.NcbiError(f"无法创建导出父目录（{parent}）：{e}") from e
        import tempfile
        try:
            fd, tmp = tempfile.mkstemp(prefix=os.path.basename(p) + ".", suffix=".part", dir=parent)
            os.close(fd)
        except OSError as e:
            raise self.dependencies.NcbiError(f"无法创建导出临时文件（{parent}）：{e}") from e
        coverage = ""
        written_records = len(ids)  # PMID 行或 CSV 数据行；CSV 缺元数据仍写占位行。
        missing_pmids = []
        try:
            if format == "pmids":
                await anyio.to_thread.run_sync(self.dependencies._write_text, tmp, "\n".join(ids) + ("\n" if ids else ""))
            elif format == "csv":
                ok, problems = await self.dependencies._export_csv(tmp, ids)
                missing_pmids = problems
                if problems:
                    coverage = (f"⚠️ CSV 有 {len(problems)} 个号元数据缺失/不可用，已在 note 列标明（不静默丢弃）："
                                + ", ".join(problems[:10]) + ("…" if len(problems) > 10 else ""))
            elif format == "ris":
                n_rec, miss = await self.dependencies._export_ris(tmp, ids)
                written_records, missing_pmids = n_rec, miss
                if miss:
                    coverage = (f"⚠️ RIS 写入 {n_rec} 条，{len(miss)} 个号没取到记录："
                                + ", ".join(miss[:10]) + ("…" if len(miss) > 10 else ""))
            else:
                n_rec, miss = await self.dependencies._export_medline(tmp, ids)
                written_records, missing_pmids = n_rec, miss
                if miss:
                    coverage = (f"⚠️ MEDLINE 正文有 {n_rec} 条 PMID- 行，{len(miss)} 个号没取到记录："
                                + ", ".join(miss[:10]) + ("…" if len(miss) > 10 else ""))
            sha = await anyio.to_thread.run_sync(self.dependencies._sha256_file, tmp)
            await anyio.to_thread.run_sync(self.dependencies._publish_no_clobber, tmp, p)
        except OSError as e:
            raise self.dependencies.NcbiError(f"导出文件写入失败（{p}）：{e}") from e
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"⚠️ 导出临时文件清理失败（{tmp}）：{e}", file=sys.stderr)
        nreq = len(ctr)
        # 状态仅表示计数/题录核对结果，不证明检索或文件查全〔本项目自订口径〕。
        integrity_issues = []
        if missing_pmids:
            integrity_issues.append("missing_records")
        if len(ids) != total:
            integrity_issues.append("id_count_mismatch")
        if total_end != total or state["count_changed"]:
            integrity_issues.append("count_changed")
        if state["drift"]:
            integrity_issues.append("pagination_drift")
        if slice_sum != total or raw_n != len(ids):
            integrity_issues.append("slice_overlap_or_count_mismatch")
        integrity_status = ("missing_records" if missing_pmids else
                            "needs_review" if integrity_issues else "no_discrepancy_detected")
        result_details = {"written_records": written_records, "missing_pmids": missing_pmids,
                          "integrity_status": integrity_status, "integrity_issues": integrity_issues}
        if total == 0 and not ids:
            # CSV 即使无数据行也保留表头；其余格式写空文件。
            empty_note = "已写出仅含表头的 CSV 文件。" if format == "csv" else "已写出空文件。"
            mismatch.append("⚠️ 命中 0 条，" + empty_note + self.dependencies._zero_explanation(d0, 0))
        warn += [text for check in state["checks"] for text in check["warnings"]]
        warn += mismatch + state["drift"] + ([coverage] if coverage else [])
        if datetype == "edat" and query == term:
            warn.append("⚠️ 未给日期区间或相对天数：datetype=edat 未用于日期筛选")
        record = dict(term=term, effective_query=query, datetype=datetype,
                                datetype_applied=query != term, path=p, format=format,
                                count_start=total, count_end=total_end, slices=n_slices, fetched=raw_n,
                                unique=len(ids), sha256=sha, started=t0, ended=t1, requests=nreq,
                                querytranslation=qt, warnings=warn, retrieval_checks=state["checks"], **result_details)
        meta_status = await self.dependencies._export_metadata(p, record, metadata)
        if meta_status["metadata_status"] == "failed":
            warn.append("⚠️ 数据已导出，但伴随元数据写入失败：" + meta_status["metadata_error"])
        self.dependencies._audit("export", **record, **meta_status)
        if json_mode:
            return json.dumps({**record, **meta_status}, ensure_ascii=False)
        # 〔1.4.5〕执行式为空时不印空行（空执行式的告警已在 warn 里）；超长按 _note 同一规则截 600 字。
        qt_line = ([] if not qt else [f"NCBI 实际执行：{qt[:600]}…（截断；完整执行式见 as_json 的 querytranslation）"]
                   if len(qt) > 600 else [f"NCBI 实际执行：{qt}"])
        head = [f"导出完成：{p}", f"查询：{term}"] + qt_line + [
                f"命中 {total} 条；切片 {n_slices} 片；取回 {raw_n} 个 PMID，去重后 {len(ids)} 个；格式 {format}",
                f"时间 {t0} → {t1}；本次约 {nreq} 次 HTTP 请求（含重试，本项目自订口径）",
                f"sha256 {sha}"]
        head += warn
        if meta_status["metadata_status"] == "written":
            head.append("伴随元数据：" + meta_status["metadata_path"])
        head.append("⚠️ 结果以导出时点为准；筛选工具导入兼容性以对方文档为准。")
        return "\n".join(head)


    def _check_job_output(self, jobdir, out_path):
        """最终产物放在作业目录之外，避免与内部文件及后续状态文件冲突。"""
        root = os.path.normcase(os.path.realpath(jobdir))
        target = os.path.normcase(os.path.realpath(out_path))
        try:
            inside = os.path.commonpath([root, target]) == root
        except ValueError:
            inside = False  # 不同盘符不可能属于同一目录。
        if inside:
            raise self.dependencies.NcbiError("最终输出不能与作业目录同路径或位于作业目录内；请使用目录外的新路径。")


    def _export_job_batches(self, ids, size):
        return [list(ids[i:i + size]) for i in range(0, len(ids), size)]


    def _job_path(self, jobdir, rel):
        """把 manifest 里的相对路径限制在作业目录；绝对路径、盘符、..、符号链接越界全部拒绝。"""
        import os
        import re

        if not isinstance(rel, str) or not rel.strip():
            raise self.dependencies.NcbiError("作业清单缺少批文件相对路径")
        s = rel.strip().replace("\\", "/")
        if s.startswith("/") or s.startswith("//") or re.match(r"^[A-Za-z]:", s):
            raise self.dependencies.NcbiError(f"作业清单里的批文件路径必须是作业目录内的相对路径：{rel!r}")
        norm = os.path.normpath(s)
        if ".." in s.split("/") or norm in (".", "..") or norm.startswith(".." + os.sep) or norm.startswith("../"):
            raise self.dependencies.NcbiError(f"作业清单里的批文件路径越出作业目录：{rel!r}")
        root = os.path.realpath(jobdir)
        try:
            p = os.path.realpath(os.path.join(root, norm))
            common = os.path.commonpath([root, p])
        except (OSError, ValueError) as e:
            raise self.dependencies.NcbiError(f"作业清单里的批文件路径无法解析：{rel!r}（{e}）") from e
        if os.path.normcase(common) != os.path.normcase(root):
            raise self.dependencies.NcbiError(f"作业清单里的批文件路径越出作业目录：{rel!r}")
        return p


    def _acquire_job_lock(self, jobdir):
        import os
        lock = os.path.join(jobdir, ".resume.lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as e:
            raise self.dependencies.NcbiError(
                f"作业正在被另一个进程处理（锁文件已存在：{lock}）。"
                f"若确认没有进程在跑，请人工检查并删除该锁文件后再恢复。") from e
        except OSError as e:
            raise self.dependencies.NcbiError(f"无法创建作业锁（{lock}）：{e}") from e
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(f"pid={os.getpid()} acquired_at={self.dependencies._utc_now()}\n")
        except OSError as e:
            try:
                os.unlink(lock)
            except OSError:
                pass
            raise self.dependencies.NcbiError(f"无法写入作业锁（{lock}）：{e}") from e
        return lock


    def _release_job_lock(self, lock):
        import os
        try:
            os.unlink(lock)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"⚠️ 作业锁清理失败（{lock}）：{e}", file=sys.stderr)


    def _add_job_warning(self, manifest, text):
        w = manifest.setdefault("warnings", [])
        if text not in w:
            w.append(text)


    def _load_job_manifest(self, jobdir):
        import json
        import os

        p = os.path.join(jobdir, "manifest.json")
        if not os.path.isfile(p):
            raise self.dependencies.NcbiError(f"作业目录里没有 manifest.json：{jobdir}")
        try:
            with open(p, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            raise self.dependencies.NcbiError(f"作业清单无法读取或不是有效 JSON（{p}）：{e}") from e
        if not isinstance(doc, dict):
            raise self.dependencies.NcbiError(f"作业清单顶层必须是对象：{p}")
        return doc


    def _validate_job_manifest(self, manifest, jobdir, *, expected_format=None, expected_out=None,
                               require_metadata_phase=False):
        import os
        import re

        def fail(msg):
            raise self.dependencies.NcbiError("作业清单不可用：" + msg)

        if manifest.get("schema") != self.dependencies._EXPORT_JOB_SCHEMA:
            fail(f"schema 不是 {self.dependencies._EXPORT_JOB_SCHEMA}（实际 {manifest.get('schema')!r}）")
        if manifest.get("job_version") != self.dependencies._EXPORT_JOB_VERSION:
            fail(f"作业版本 {manifest.get('job_version')!r} 与本工具支持的 {self.dependencies._EXPORT_JOB_VERSION} 不兼容")
        if manifest.get("tool_version") != self.dependencies.__version__:
            fail(f"作业由工具版本 {manifest.get('tool_version')!r} 创建，本工具是 {self.dependencies.__version__}；版本不兼容，拒绝恢复")
        fm = manifest.get("formatter")
        if not isinstance(fm, dict) or fm.get("format") not in self.dependencies._EXPORT_FORMATS:
            fail("formatter.format 缺失或不是支持格式")
        fmt = fm.get("format")
        if fm.get("format_version") != self.dependencies._EXPORT_FORMAT_VERSION:
            fail(f"格式化器版本 {fm.get('format_version')!r} 与本工具支持的 {self.dependencies._EXPORT_FORMAT_VERSION} 不兼容")
        if manifest.get("format") not in (None, fmt):
            fail(f"顶层 format={manifest.get('format')!r} 与 formatter.format={fmt!r} 不一致")
        if manifest.get("format_version") not in (None, self.dependencies._EXPORT_FORMAT_VERSION):
            fail(f"顶层 format_version={manifest.get('format_version')!r} 与本工具支持的 "
                 f"{self.dependencies._EXPORT_FORMAT_VERSION} 不兼容")
        if expected_format is not None and fmt != expected_format:
            fail(f"作业格式是 {fmt!r}，与请求的 {expected_format!r} 不一致")
        if require_metadata_phase and manifest.get("phase") != "metadata":
            fail(f"只有 metadata 阶段可恢复；当前 phase={manifest.get('phase')!r}")

        ids = manifest.get("pmids")
        if not isinstance(ids, list) or not all(isinstance(x, str) and re.fullmatch(r"[0-9]+", x) for x in ids):
            fail("pmids 必须是纯数字字符串数组")
        if len(ids) != len(set(ids)):
            fail("pmids 有重复号，固定清单应已去重")
        if len(ids) > self.dependencies.EXPORT_MAX:
            fail(f"pmids 有 {len(ids)} 个，超过安全上限 {self.dependencies.EXPORT_MAX}")
        if manifest.get("count") != len(ids):
            fail(f"count={manifest.get('count')!r} 与 pmids 长度 {len(ids)} 不一致")
        if manifest.get("pmids_sha256") != self.dependencies._pmid_ids_sha256(ids):
            fail("pmids_sha256 与固定 PMID 清单不一致")

        size = manifest.get("batch_size")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size > 10000:
            fail(f"batch_size={size!r} 非法")
        chunks = self.dependencies._export_job_batches(ids, size)
        batches = manifest.get("batches")
        if not isinstance(batches, list) or len(batches) != len(chunks):
            fail(f"batches 数量 {len(batches) if isinstance(batches, list) else '非数组'} "
                 f"与固定清单应有批次数 {len(chunks)} 不一致")
        batch_paths = set()
        for i, (batch, chunk) in enumerate(zip(batches, chunks)):
            if not isinstance(batch, dict):
                fail(f"batches[{i}] 不是对象")
            if batch.get("batch_id") != i:
                fail(f"batches[{i}].batch_id={batch.get('batch_id')!r} 与顺序不一致")
            if batch.get("pmids") != chunk:
                fail(f"batches[{i}].pmids 与固定清单切片不一致")
            if batch.get("pmids_sha256") != self.dependencies._pmid_ids_sha256(chunk):
                fail(f"batches[{i}].pmids_sha256 不一致")
            if batch.get("file") is not None:
                self.dependencies._job_path(jobdir, batch["file"])
            if batch.get("path") is not None:
                self.dependencies._job_path(jobdir, batch["path"])
            rel = self.dependencies._batch_rel(batch)
            canonical_path = os.path.normcase(self.dependencies._job_path(jobdir, rel))
            expected_rel = f"batches/batch-{i:05d}.{self.dependencies._EXPORT_BATCH_EXT[fmt]}"
            if canonical_path != os.path.normcase(self.dependencies._fixed_job_path(jobdir, expected_rel)):
                fail("批文件路径不符合固定批次计划")
            if canonical_path in batch_paths:
                fail("不同批次不能共用同一批文件")
            batch_paths.add(canonical_path)
            batch["file"] = rel
            batch["path"] = rel
            if batch.get("status") not in ("pending", "done"):
                fail(f"batches[{i}].status={batch.get('status')!r} 非法")

        inp = manifest.get("input")
        if not isinstance(inp, dict) or not isinstance(inp.get("path"), str) or not inp["path"]:
            fail("input.path 缺失")
        if not os.path.isabs(inp["path"]):
            fail("input.path 必须是绝对路径")
        for key in ("sha256", "schema"):
            if not isinstance(inp.get(key), str) or not inp[key]:
                fail(f"input.{key} 缺失")
        if isinstance(inp.get("raw_count"), bool) or not isinstance(inp.get("raw_count"), int):
            fail("input.raw_count 缺失或不是整数")
        if inp.get("pmids_sha256") != manifest.get("pmids_sha256"):
            fail("input.pmids_sha256 与 pmids_sha256 不一致")

        out = manifest.get("output")
        if not isinstance(out, dict) or not isinstance(out.get("path"), str) or not out["path"]:
            fail("output.path 缺失")
        if not os.path.isabs(out["path"]):
            fail("output.path 必须是绝对路径")
        out_path = os.path.abspath(out["path"])
        self.dependencies._check_job_output(jobdir, out_path)
        if require_metadata_phase and os.path.exists(out_path):
            fail(f"最终输出文件已存在，绝不覆盖：{out_path}")
        if expected_out is not None and not self.dependencies._paths_same(out_path, expected_out):
            fail(f"output.path={out_path} 与请求输出 {expected_out} 不一致")
        return fmt, out_path


    def _new_job_manifest(self, parsed, out_path, fmt, batch_size):
        import os

        ids = list(parsed["ids"])
        chunks = self.dependencies._export_job_batches(ids, batch_size)
        ext = self.dependencies._EXPORT_BATCH_EXT[fmt]
        return {
            "schema": self.dependencies._EXPORT_JOB_SCHEMA,
            "job_version": self.dependencies._EXPORT_JOB_VERSION,
            "tool_version": self.dependencies.__version__,
            "format": fmt,
            "format_version": self.dependencies._EXPORT_FORMAT_VERSION,
            "formatter": {"format": fmt, "format_version": self.dependencies._EXPORT_FORMAT_VERSION},
            "created_at": self.dependencies._utc_now(),
            "updated_at": self.dependencies._utc_now(),
            "phase": "metadata",
            "input": self.dependencies._pmid_binding(parsed),
            "source_query": None,
            "source_query_status": "missing",
            "evidence": "来源：输入清单及本次题录抓取/文件写入记录；批次规模为本项目自订，固定名单不冻结题录内容",
            "pmids": ids,
            "pmids_sha256": self.dependencies._pmid_ids_sha256(ids),
            "state_epoch": uuid.uuid4().hex,   # 本项目自订：绑定同一固定计划的增量状态世代
            "count": len(ids),
            "batch_size": batch_size,
            "output": {"path": os.path.abspath(out_path), "sha256": None, "bytes": None,
                       "published_at": None},
            "batches": [
                {"batch_id": i, "pmids": list(chunk), "pmids_sha256": self.dependencies._pmid_ids_sha256(chunk),
                 "file": f"batches/batch-{i:05d}.{ext}",
                 "path": f"batches/batch-{i:05d}.{ext}", "status": "pending",
                 "sha256": None, "count": None, "requested_count": len(chunk),
                 "written_records": None, "missing_pmids": [], "fetched_at": None}
                for i, chunk in enumerate(chunks)
            ],
            "last_error": None,
            "warnings": [],
        }


    def _batch_rel(self, batch):
        a, b = batch.get("file"), batch.get("path")
        if a is None:
            a = b
        if a is None:
            raise self.dependencies.NcbiError("作业清单缺少批文件相对路径")
        if b is not None and a != b:
            raise self.dependencies.NcbiError(f"作业清单的批文件路径 file={a!r} 与 path={b!r} 不一致")
        return a


    def _inspect_batch_file(self, path, fmt, ids):
        """校验批文件归属并返回 (实际记录数, 缺失号, 实际顺序号)。"""
        import csv
        import os
        import re

        if not os.path.isfile(path):
            raise self.dependencies.NcbiError(f"批文件不存在：{path}")
        if fmt == "pmids":
            try:
                with open(path, encoding="utf-8") as f:
                    got = [ln.strip() for ln in f if ln.strip()]
            except (OSError, UnicodeDecodeError) as e:
                raise self.dependencies.NcbiError(f"批文件无法按 UTF-8 文本读取（{path}）：{e}") from e
            if got != list(ids):
                raise self.dependencies.NcbiError(f"pmids 批文件内容与固定清单不一致（{path}）")
            return len(got), [], got
        if fmt == "csv":
            try:
                with open(path, encoding="utf-8", newline="") as f:
                    rows = list(csv.reader(f))
            except (OSError, UnicodeDecodeError, csv.Error) as e:
                raise self.dependencies.NcbiError(f"CSV 批文件无法解析（{path}）：{e}") from e
            got = [r[0] if r else "" for r in rows]
            if got != list(ids):
                raise self.dependencies.NcbiError(f"CSV 批文件的 pmid 行与固定清单不一致（{path}）")
            if any(len(r) != len(self.dependencies._PMIDS_CSV_HEADER) for r in rows):
                raise self.dependencies.NcbiError(f"CSV 批文件列数与 writer 表头不一致（{path}）")
            missing = [r[0] for r in rows if r and r[-1]]
            return len(rows), missing, got
        if fmt == "ris":
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError) as e:
                raise self.dependencies.NcbiError(f"RIS 批文件无法按 UTF-8 读取（{path}）：{e}") from e
            blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
            got = []
            for b in blocks:
                if not b.startswith("TY  - ") or not re.search(r"(?m)^ER  -\s*$", b):
                    raise self.dependencies.NcbiError(f"RIS 批文件不是完整题录块（{path}）")
                found = re.findall(r"^AN  - (\d+)\s*$", b, re.M)
                if len(found) != 1:
                    raise self.dependencies.NcbiError(f"RIS 批文件记录缺少唯一的 AN 行（{path}）")
                got.append(found[0])
            if len(got) != len(set(got)):
                raise self.dependencies.NcbiError(f"RIS 批文件有重复 PMID（{path}）")
            if not set(got).issubset(set(ids)):
                raise self.dependencies.NcbiError(f"RIS 批文件包含不属于本批的 PMID（{path}）")
            got_set = set(got)
            return len(got), [p for p in ids if p not in got_set], got
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as e:
            raise self.dependencies.NcbiError(f"MEDLINE 批文件无法按 UTF-8 读取（{path}）：{e}") from e
        blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
        got = []
        for b in blocks:
            found = re.findall(r"^PMID- (\d+)\s*$", b, re.M)
            if len(found) != 1:
                raise self.dependencies.NcbiError(f"MEDLINE 批文件记录缺少唯一的 PMID- 行（{path}）")
            got.append(found[0])
        if len(got) != len(set(got)):
            raise self.dependencies.NcbiError(f"MEDLINE 批文件有重复 PMID（{path}）")
        if not set(got).issubset(set(ids)):
            raise self.dependencies.NcbiError(f"MEDLINE 批文件包含不属于本批的 PMID（{path}）")
        got_set = set(got)
        return len(got), [p for p in ids if p not in got_set], got


    def _normalize_writer_output(self, raw_path, fmt, ids, writer_written, writer_missing):
        """把 writer 临时输出规范成批文件正文；RIS/MEDLINE 按固定 PMID 顺序重排。

        CSV 去掉 writer 自己写的表头，只留数据行（最终文件只写一次表头）。
        返回 (正文, 实际记录数, 缺失号)。
        """
        import csv
        import io
        import re

        ids = list(ids)
        missing = [str(x) for x in (writer_missing or [])]
        if fmt == "pmids":
            with open(raw_path, encoding="utf-8") as f:
                text = f.read()
            got = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if got != ids:
                raise self.dependencies.NcbiError("pmids writer 返回的顺序或集合与固定清单不一致")
            if missing:
                raise self.dependencies.NcbiError("pmids writer 不应报告缺失记录")
            return text, len(got), []
        if fmt == "csv":
            with open(raw_path, "r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            if not rows:
                raise self.dependencies.NcbiError("CSV writer 没有写出表头")
            header, data = rows[0], rows[1:]
            if tuple(header) != self.dependencies._PMIDS_CSV_HEADER:
                raise self.dependencies.NcbiError("CSV writer 表头与本工具预期的表头不一致；请同步更新 _PMIDS_CSV_HEADER")
            got = [r[0] if r else "" for r in data]
            if got != ids:
                raise self.dependencies.NcbiError("CSV writer 的 pmid 行顺序或集合与固定清单不一致")
            if any(len(r) != len(header) for r in data):
                raise self.dependencies.NcbiError("CSV writer 返回的数据行列数不一致")
            if not set(missing).issubset(set(ids)):
                raise self.dependencies.NcbiError("CSV writer 报告的缺失名单含不属于本批的 PMID")
            actual_missing = [r[0] for r in data if r and r[-1]]
            missing_set = set(actual_missing)
            if missing_set != set(missing):
                raise self.dependencies.NcbiError("CSV writer 报告的缺失名单与 note 列不符")
            out = io.StringIO(newline="")
            csv.writer(out, lineterminator="\r\n").writerows(data)
            return out.getvalue(), len(data), [p for p in ids if p in missing_set]
        with open(raw_path, encoding="utf-8") as f:
            text = f.read()
        blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
        by = {}
        pat = r"^AN  - (\d+)\s*$" if fmt == "ris" else r"^PMID- (\d+)\s*$"
        label = "AN" if fmt == "ris" else "PMID-"
        for b in blocks:
            if fmt == "ris" and (not b.startswith("TY  - ") or not re.search(r"(?m)^ER  -\s*$", b)):
                raise self.dependencies.NcbiError("RIS writer 返回了缺少 TY/ER 的不完整题录块")
            found = re.findall(pat, b, re.M)
            if len(found) != 1:
                raise self.dependencies.NcbiError(f"{fmt} writer 返回了缺少唯一 {label} 行的记录")
            p = found[0]
            if p in by:
                raise self.dependencies.NcbiError(f"{fmt} writer 返回了重复 PMID：{p}")
            if p not in ids:
                raise self.dependencies.NcbiError(f"{fmt} writer 返回了不属于本批的 PMID：{p}")
            by[p] = b
        got = [p for p in ids if p in by]
        derived_missing = [p for p in ids if p not in by]
        if set(derived_missing) != set(missing):
            raise self.dependencies.NcbiError(f"{fmt} writer 报告的缺失名单与正文不符")
        if isinstance(writer_written, int) and writer_written != len(got):
            raise self.dependencies.NcbiError(f"{fmt} writer 报告的记录数与正文不符")
        ordered = "\n\n".join(by[p] for p in got)
        return (ordered + ("\n" if ordered else "")), len(got), derived_missing


    async def _store_batch(self, jobdir, manifest, batch):
        import os
        import tempfile

        fmt = manifest["formatter"]["format"]
        ids = batch["pmids"]
        self.dependencies._batch_file_path(jobdir, fmt, batch)
        batches_dir = os.path.join(jobdir, "batches")
        os.makedirs(batches_dir, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=f".batch-{batch['batch_id']}-", suffix=".raw", dir=batches_dir)
        os.close(fd)
        norm = None
        try:
            if fmt == "pmids":
                await anyio.to_thread.run_sync(self.dependencies._write_text, raw, "\n".join(ids) + ("\n" if ids else ""))
                written, missing = len(ids), []
            elif fmt == "csv":
                written, missing = await self.dependencies._export_csv(raw, ids)
            elif fmt == "ris":
                written, missing = await self.dependencies._export_ris(raw, ids)
            else:
                written, missing = await self.dependencies._export_medline(raw, ids)
            body, actual_written, actual_missing = await anyio.to_thread.run_sync(
                self.dependencies._normalize_writer_output, raw, fmt, ids, written, missing)
            fd2, norm = tempfile.mkstemp(prefix=f".batch-{batch['batch_id']}-", suffix=".part",
                                         dir=batches_dir)
            os.close(fd2)
            await anyio.to_thread.run_sync(self.dependencies._write_text, norm, body)
            batch_path = self.dependencies._batch_file_path(jobdir, fmt, batch)
            os.makedirs(os.path.dirname(batch_path), exist_ok=True)
            await anyio.to_thread.run_sync(os.replace, norm, batch_path)
            norm = None
            sha = await anyio.to_thread.run_sync(self.dependencies._sha256_file, batch_path)
        finally:
            for tmp in (raw, norm):
                if tmp:
                    try:
                        os.unlink(tmp)
                    except FileNotFoundError:
                        pass
                    except OSError as e:
                        print(f"⚠️ 批文件临时文件清理失败（{tmp}）：{e}", file=sys.stderr)
        batch.update({
            "status": "done", "sha256": sha, "count": actual_written,
            "requested_count": len(ids), "written_records": actual_written,
            "missing_pmids": list(actual_missing), "fetched_at": self.dependencies._utc_now(),
        })


    def _verify_done_batch(self, jobdir, manifest, batch):
        import os

        fmt = manifest["formatter"]["format"]
        p = self.dependencies._job_path(jobdir, self.dependencies._batch_rel(batch))
        if not os.path.isfile(p):
            raise self.dependencies.NcbiError(f"批文件不存在：{batch['file']}")
        if batch.get("sha256") != self.dependencies._sha256_file(p):
            raise self.dependencies.NcbiError(f"批文件哈希不符：{batch['file']}")
        written, missing, got = self.dependencies._inspect_batch_file(p, fmt, batch["pmids"])
        if batch.get("written_records") != written or batch.get("count") != written:
            raise self.dependencies.NcbiError(f"批文件记录数与作业清单不一致：{batch['file']}")
        recorded = batch.get("missing_pmids")
        if not isinstance(recorded, list):
            raise self.dependencies.NcbiError(f"批文件缺失名单不是数组：{batch['file']}")
        if list(recorded) != list(missing):
            raise self.dependencies.NcbiError(f"批文件缺失名单与正文不一致：{batch['file']}")
        got_set = set(got)
        if got != [pid for pid in batch["pmids"] if pid in got_set]:
            raise self.dependencies.NcbiError(f"批文件顺序与固定清单不一致：{batch['file']}")
        return p


    def _fixed_job_path(self, jobdir, relative):
        """固定内部文件不允许通过符号链接别名指向其它文件。"""
        path = self.dependencies._job_path(jobdir, relative)
        expected = os.path.join(os.path.realpath(jobdir), *relative.split("/"))
        if os.path.normcase(path) != os.path.normcase(expected):
            raise self.dependencies.NcbiError("固定作业路径不能通过符号链接指向其它文件")
        return path


    def _batch_file_path(self, jobdir, fmt, batch):
        relative = f"batches/batch-{batch['batch_id']:05d}.{self.dependencies._EXPORT_BATCH_EXT[fmt]}"  # 本项目自订：固定批文件名
        path = self.dependencies._fixed_job_path(jobdir, relative)
        if os.path.normcase(self.dependencies._job_path(jobdir, self.dependencies._batch_rel(batch))) != os.path.normcase(path):
            raise self.dependencies.NcbiError("批文件路径不符合固定批次计划")
        return path


    def _batch_state_path(self, jobdir, batch_id):
        """由固定计划生成路径，拒绝符号链接别名。"""
        relative = f"{self.dependencies._EXPORT_STATE_DIR}/batch-{int(batch_id):05d}.json"  # 本项目自订：批状态文件名
        return self.dependencies._fixed_job_path(jobdir, relative)


    def _batch_state_binding(self, manifest, batch):
        """写入和读取共用的固定计划绑定字段。"""
        return {
            "schema": self.dependencies._EXPORT_STATE_SCHEMA,
            "job_schema": manifest.get("schema"),
            "job_version": manifest.get("job_version"),
            "tool_version": manifest.get("tool_version"),
            "format": manifest["formatter"]["format"],
            "format_version": manifest["formatter"].get("format_version"),
            "state_epoch": manifest.get("state_epoch"),
            "pmids_sha256": manifest.get("pmids_sha256"),
            "batch_id": batch.get("batch_id"),
            "batch_pmids_sha256": self.dependencies._pmid_ids_sha256(batch["pmids"]),
            "file": self.dependencies._batch_rel(batch),
            "status": "done",
        }


    def _batch_state_payload(self, manifest, batch):
        """单批增量状态；体积只随本批 PMID 数增长，与总批次数无关。"""
        return {
            **self.dependencies._batch_state_binding(manifest, batch),
            "sha256": batch.get("sha256"),
            "count": batch.get("count"),
            "requested_count": batch.get("requested_count"),
            "written_records": batch.get("written_records"),
            "missing_pmids": list(batch.get("missing_pmids") or []),
            "fetched_at": batch.get("fetched_at"),
            "saved_at": self.dependencies._utc_now(),
        }


    async def _save_batch_state(self, jobdir, manifest, batch):
        """原子写入单批状态（线程内 fsync）；失败只记警告，由开始/结束/失败快照兜底。"""
        payload = self.dependencies._batch_state_payload(manifest, batch)
        try:
            await anyio.to_thread.run_sync(
                self.dependencies._write_json_atomic, self.dependencies._batch_state_path(jobdir, batch["batch_id"]), payload)
        except Exception as e:
            self.dependencies._add_job_warning(
                manifest,
                f"批次 {batch['batch_id']} 增量状态写入失败，将在整份清单快照时补齐："
                f"{self.dependencies._mask(str(e))[:300]}")
            return False
        return True


    def _remove_batch_state(self, jobdir, batch_id):
        """删除单批状态；删除失败不影响重抓，下次恢复仍会校验状态内容。"""
        import os

        try:
            os.unlink(self.dependencies._batch_state_path(jobdir, batch_id))
        except FileNotFoundError:
            pass
        except (OSError, self.dependencies.NcbiError) as e:
            print(f"⚠️ 批次增量状态清理失败（批次 {batch_id}）：{self.dependencies._mask(str(e))}",
                  file=sys.stderr)


    def _load_batch_state(self, jobdir, manifest, batch):
        """读取并校验单批状态，返回可并入清单的字段；任何不可信情况返回 None。

        状态必须与固定批次计划逐项绑定（job/format/epoch/全量 pmids 哈希/批号/批清单
        哈希/固定路径）。损坏、缺字段或不匹配都只让该批重抓，不牵动其它批。调用方仅在
        整份清单仍标记该批 pending 时使用它；清单已 done 的批次以清单为准，避免旧状态
        覆盖更新的整份快照。
        """
        import json
        import re

        try:
            with open(self.dependencies._batch_state_path(jobdir, batch["batch_id"]), encoding="utf-8") as f:
                doc = json.load(f)
        except self.dependencies.NcbiError as exc:
            self.dependencies._add_job_warning(manifest, "不复用不可信增量状态，将重抓该批：" + self.dependencies._mask(str(exc)))
            return None
        except (OSError, ValueError, RecursionError):
            return None
        if not isinstance(doc, dict):
            return None
        for key, want in self.dependencies._batch_state_binding(manifest, batch).items():
            if doc.get(key) != want:
                return None
        sha, count, written = doc.get("sha256"), doc.get("count"), doc.get("written_records")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            return None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
        if type(written) is not int or written != count:
            return None
        if type(doc.get("requested_count")) is not int or doc["requested_count"] != len(batch["pmids"]):
            return None
        missing = doc.get("missing_pmids")
        if not isinstance(missing, list) or not all(isinstance(x, str) for x in missing):
            return None
        if not set(missing).issubset(set(batch["pmids"])):
            return None
        fetched_at = doc.get("fetched_at")
        if fetched_at is not None and not isinstance(fetched_at, str):
            return None
        return {"status": "done", "sha256": sha, "count": count,
                "requested_count": len(batch["pmids"]), "written_records": written,
                "missing_pmids": list(missing), "fetched_at": fetched_at}


    def _reset_batch_for_refetch(self, batch):
        """把单批退回 pending；其它批的完成状态保持不变。"""
        batch.update({"status": "pending", "sha256": None, "count": None,
                      "written_records": None, "missing_pmids": [], "fetched_at": None})


    def _concat_batches_to_file(self, jobdir, manifest, dst, fmt):
        """按序合并批文件，同时计算最终文件摘要，避免发布前整文件重读。"""

        paths = []
        for b in manifest["batches"]:
            if b.get("status") != "done":
                raise self.dependencies.NcbiError(f"批次 {b.get('batch_id')} 尚未完成，不能发布最终文件")
            p = self.dependencies._batch_file_path(jobdir, fmt, b)
            if not os.path.isfile(p):
                raise self.dependencies.NcbiError(f"批次文件不存在，不能发布：{b['file']}")
            paths.append((p, b))
        digest = hashlib.sha256()
        with open(dst, "wb") as w:
            def write(data):
                w.write(data)
                digest.update(data)

            if fmt == "csv":
                write(b"\xef\xbb\xbf")
                write((",".join(self.dependencies._PMIDS_CSV_HEADER) + "\r\n").encode("utf-8"))
            first = True
            for p, batch in paths:
                batch_digest = hashlib.sha256()
                has_data = False
                with open(p, "rb") as r:
                    while chunk := r.read(1 << 20):  # 本项目自订：沿用原合并缓冲大小
                        if not has_data and fmt in ("ris", "medline") and not first:
                            write(b"\n")
                        has_data = True
                        batch_digest.update(chunk)
                        write(chunk)
                if batch_digest.hexdigest() != batch.get("sha256"):
                    raise self.dependencies.NcbiError(f"合并时批文件哈希不符，拒绝发布：{batch['file']}")
                if has_data:
                    first = False
        return digest.hexdigest()


    async def _publish_export_file(self, jobdir, manifest, out_path, fmt):
        import os
        import tempfile

        parent = os.path.dirname(out_path)
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            raise self.dependencies.NcbiError(f"无法创建导出父目录（{parent}）：{e}") from e
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(out_path) + ".", suffix=".part", dir=parent)
        os.close(fd)
        try:
            sha = await anyio.to_thread.run_sync(self.dependencies._concat_batches_to_file, jobdir, manifest, tmp, fmt)
            nbytes = os.path.getsize(tmp)
            await anyio.to_thread.run_sync(self.dependencies._publish_no_clobber, tmp, out_path)
        except OSError as e:
            raise self.dependencies.NcbiError(f"导出文件写入失败（{out_path}）：{e}") from e
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"⚠️ 导出临时文件清理失败（{tmp}）：{e}", file=sys.stderr)
        return out_path, nbytes, sha


    def _revalidate_job_input(self, manifest):
        import os

        inp = manifest["input"]
        p = inp["path"]
        if not os.path.exists(p):
            return ("input_source_missing：原输入文件已不存在；使用作业清单中的固定 PMID 清单继续，"
                    "输入哈希无法重新核对。")
        parsed = self.dependencies._read_pmid_file(p)
        if parsed["sha256"] != inp["sha256"]:
            raise self.dependencies.NcbiError(
                f"作业的输入文件已变化，拒绝恢复：{p}（清单 sha256 {inp['sha256']}，"
                f"当前 {parsed['sha256']}）。固定 PMID 清单不能和新输入混用；请重新建作业。")
        if self.dependencies._pmid_ids_sha256(parsed["ids"]) != manifest["pmids_sha256"]:
            raise self.dependencies.NcbiError(f"输入文件重新解析出的 PMID 集合与作业固定清单不一致：{p}")
        return None


    async def _execute_export_job(self, jobdir, manifest, manifest_path, out_path, fmt, *, resumed, resumable):
        import copy
        import os

        if manifest["formatter"]["format"] != fmt:
            raise self.dependencies.NcbiError("作业格式与请求格式不一致")
        manifest["last_error"] = None

        async def snapshot():
            """整份清单快照：开始/成功/失败各一次，绝不逐批重写。"""
            manifest["updated_at"] = self.dependencies._utc_now()
            await anyio.to_thread.run_sync(
                lambda: self.dependencies._write_json_atomic(manifest_path, copy.deepcopy(manifest)))

        try:
            await snapshot()
            for batch in manifest["batches"]:
                from_manifest = batch.get("status") == "done"
                if not from_manifest:
                    state = await anyio.to_thread.run_sync(self.dependencies._load_batch_state, jobdir, manifest, batch)
                    if state is not None:
                        batch.update(state)
                if batch.get("status") == "done":
                    try:
                        await anyio.to_thread.run_sync(self.dependencies._verify_done_batch, jobdir, manifest, batch)
                    except self.dependencies.NcbiError as e:
                        self.dependencies._reset_batch_for_refetch(batch)
                        await anyio.to_thread.run_sync(self.dependencies._remove_batch_state, jobdir, batch["batch_id"])
                        self.dependencies._add_job_warning(manifest, f"批次 {batch['batch_id']} 校验失败，已安排重抓："
                                                   f"{self.dependencies._mask(str(e))[:300]}")
                    else:
                        if from_manifest:
                            # 以整份清单为准，刷新可能陈旧的状态文件。
                            await self.dependencies._save_batch_state(jobdir, manifest, batch)
                        continue
                await self.dependencies._store_batch(jobdir, manifest, batch)
                await self.dependencies._save_batch_state(jobdir, manifest, batch)

            written_records = sum(int(b.get("written_records") or 0) for b in manifest["batches"])
            missing = list(dict.fromkeys(p for b in manifest["batches"] for p in b.get("missing_pmids", [])))
            output_path, nbytes, sha = await self.dependencies._publish_export_file(jobdir, manifest, out_path, fmt)
            manifest["phase"] = "published"
            manifest["output"] = {"path": output_path, "sha256": sha, "bytes": nbytes,
                                  "published_at": self.dependencies._utc_now()}
            manifest_status = "written"
            try:
                await snapshot()
            except Exception as exc:
                manifest_status = "failed"
                self.dependencies._add_job_warning(manifest, "数据已发布，但作业完成状态写入失败：" + self.dependencies._mask(str(exc)))
            result = {
                "schema": self.dependencies._EXPORT_RESULT_SCHEMA, "format": fmt, "path": output_path,
                "count": len(manifest["pmids"]), "written_records": written_records,
                "missing_pmids": missing, "sha256": sha, "bytes": nbytes,
                "batches": len(manifest["batches"]),
                "jobdir": jobdir if resumable else None, "resumable": bool(resumable),
                "resumed": bool(resumed), "input": manifest["input"],
                "integrity_status": "missing_records" if missing else "no_discrepancy_detected",
                "integrity_issues": ["missing_records"] if missing else [],
                "manifest_status": manifest_status,
                "warnings": list(manifest.get("warnings") or []),
            }
            self.dependencies._audit("export_pmids", **result)
            return result
        except BaseException as e:
            if isinstance(e, Exception) and isinstance(manifest, dict):
                manifest["last_error"] = self.dependencies._mask(str(e))[:500]
                try:
                    await snapshot()
                except Exception:
                    pass
            raise


    async def _export_pmids(self, source, out, format="pmids", checkpoint_dir=""):
        """从已持久化的 PMID 清单导出题录；checkpoint_dir 非空时留下可恢复作业目录。"""
        import functools
        import os
        import shutil
        import tempfile

        if format not in self.dependencies._EXPORT_FORMATS:
            raise self.dependencies.NcbiError("format 只支持 pmids / csv / medline / ris")
        src_s, out_s = str(source or "").strip(), str(out or "").strip()
        if not src_s:
            raise self.dependencies.NcbiError("source 不能为空；请给 PMID 清单文件路径。")
        if not out_s:
            raise self.dependencies.NcbiError("out 不能为空；请给最终输出文件路径。")
        src_path = os.path.abspath(os.path.expanduser(src_s))
        out_path = os.path.abspath(os.path.expanduser(out_s))
        if self.dependencies._paths_same(src_path, out_path):
            raise self.dependencies.NcbiError("输出不能与输入清单同路径；本工具不覆盖输入文件。")
        out_path = self.dependencies._check_out_path(out_path)
        ckpt_s = str(checkpoint_dir or "").strip()
        ckpt_abs = os.path.abspath(os.path.expanduser(ckpt_s)) if ckpt_s else ""
        if ckpt_abs:
            self.dependencies._check_job_output(ckpt_abs, out_path)
        if ckpt_abs and os.path.exists(ckpt_abs):
            raise self.dependencies.NcbiError(f"作业目录已存在：{ckpt_abs}。新建作业必须使用尚不存在的目录；"
                            f"恢复已有作业请用 _resume_export(jobdir)。")

        parsed = await anyio.to_thread.run_sync(functools.partial(self.dependencies._read_pmid_file, src_path))
        batch_size = int(self.dependencies._EXPORT_BATCH_SIZE[format])
        temp_job = False
        created_job = False
        manifest_written = False
        jobdir = None
        lock = None
        try:
            if ckpt_abs:
                jobdir = ckpt_abs
                try:
                    os.makedirs(jobdir, exist_ok=False)
                except FileExistsError as e:
                    raise self.dependencies.NcbiError(f"作业目录已存在：{jobdir}。请用 _resume_export(jobdir) 恢复。") from e
                except OSError as e:
                    raise self.dependencies.NcbiError(f"无法创建作业目录（{jobdir}）：{e}") from e
                created_job = True
            else:
                parent = os.path.dirname(out_path)
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError as e:
                    raise self.dependencies.NcbiError(f"无法创建导出父目录（{parent}）：{e}") from e
                jobdir = tempfile.mkdtemp(prefix=os.path.basename(out_path) + ".job.", dir=parent)
                temp_job = True
            os.makedirs(os.path.join(jobdir, "batches"), exist_ok=True)
            lock = self.dependencies._acquire_job_lock(jobdir)
            manifest = self.dependencies._new_job_manifest(parsed, out_path, format, batch_size)
            self.dependencies._validate_job_manifest(manifest, jobdir, expected_format=format,
                                   expected_out=out_path, require_metadata_phase=True)
            manifest_path = os.path.join(jobdir, "manifest.json")
            await anyio.to_thread.run_sync(self.dependencies._write_json_atomic, manifest_path, manifest)
            manifest_written = True
            return await self.dependencies._execute_export_job(jobdir, manifest, manifest_path, out_path, format,
                                             resumed=False, resumable=bool(ckpt_abs))
        except BaseException:
            if created_job and not manifest_written and jobdir:
                shutil.rmtree(jobdir, ignore_errors=True)
            raise
        finally:
            if lock:
                self.dependencies._release_job_lock(lock)
            if temp_job and jobdir:
                shutil.rmtree(jobdir, ignore_errors=True)


    async def _resume_export(self, jobdir):
        """恢复 metadata 阶段的题录作业；锁定作业目录，拒绝并发恢复。"""
        import os

        job_s = str(jobdir or "").strip()
        if not job_s:
            raise self.dependencies.NcbiError("jobdir 不能为空")
        jobdir = os.path.abspath(os.path.expanduser(job_s))
        if not os.path.isdir(jobdir):
            raise self.dependencies.NcbiError(f"作业目录不存在：{jobdir}")
        lock = self.dependencies._acquire_job_lock(jobdir)
        try:
            manifest = await anyio.to_thread.run_sync(self.dependencies._load_job_manifest, jobdir)
            fmt, out_path = self.dependencies._validate_job_manifest(manifest, jobdir, require_metadata_phase=True)
            manifest_path = os.path.join(jobdir, "manifest.json")
            warning = await anyio.to_thread.run_sync(self.dependencies._revalidate_job_input, manifest)
            if warning:
                self.dependencies._add_job_warning(manifest, warning)
                await anyio.to_thread.run_sync(self.dependencies._write_json_atomic, manifest_path, manifest)
            return await self.dependencies._execute_export_job(jobdir, manifest, manifest_path, out_path, fmt,
                                             resumed=True, resumable=True)
        finally:
            self.dependencies._release_job_lock(lock)

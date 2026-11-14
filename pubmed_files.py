"""files implementation; no core or MCP registration import.
Dependencies are an explicit, per-instance, late-bound contract.
Historical evidence and policy comments are retained from the source.
"""
import hashlib
import json
import os
import re
import sys

DEPENDENCIES = ('EXPORT_MAX', 'NcbiError', '_CANDIDATE_SCHEMAS', '_DIFF_SCHEMA', '_PMID_TEXT_SCHEMA', '_audit', '_check_out_path', '_parse_pmids', '_paths_same', '_pmid_binding', '_pmid_ids_sha256', '_publish_no_clobber', '_read_pmid_file', '_utc_now', '_write_json_new', '_write_new_file')


class Implementation:
    def __init__(self, dependencies):
        self.dependencies = dependencies

    def _write_text(self, path, text):
        """UTF-8、LF 换行写文本。同步函数，调用方用 anyio.to_thread 卸载。"""
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


    def _sha256_file(self, path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()


    def _write_new_file(self, path, body):
        """原子写新文件且不覆盖（同 export）。返回 (绝对路径, 字节数, sha256)。"""
        import tempfile
        p = os.path.abspath(os.path.expanduser(path))
        parent = os.path.dirname(p)
        try:
            os.makedirs(parent, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=os.path.basename(p) + ".", suffix=".part", dir=parent)
            os.close(fd)   # Windows：不关 fd 时 rename 报 WinError 32〔审查实测 2026-11-12〕
        except OSError as e:
            raise self.dependencies.NcbiError(f"无法创建输出文件（{parent}）：{e}") from e
        data = body.encode("utf-8")
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            self.dependencies._publish_no_clobber(tmp, p)
        except OSError as e:
            raise self.dependencies.NcbiError(f"输出文件写入失败（{p}）：{e}") from e
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"⚠️ 临时文件清理失败（{tmp}）：{e}", file=sys.stderr)
        return p, len(data), hashlib.sha256(data).hexdigest()


    def _check_out_path(self, out_path):
        s = (out_path or "").strip()
        if not s:
            return ""
        p = os.path.abspath(os.path.expanduser(s))
        if os.path.isdir(p):
            raise self.dependencies.NcbiError(f"out_path 是已存在的目录而非文件路径：{p}")
        if os.path.exists(p):
            raise self.dependencies.NcbiError(f"文件已存在：{p}。本工具不覆盖已有文件，请换个路径或先移走旧文件。")
        return p


    def _pmid_ids_sha256(self, ids):
        import hashlib
        return hashlib.sha256("\n".join(str(x) for x in ids).encode("utf-8")).hexdigest()


    def _pmid_binding(self, parsed):
        return {"path": parsed["path"], "sha256": parsed["sha256"], "schema": parsed["schema"],
                "raw_count": parsed["raw_count"], "unique_count": len(parsed["ids"]),
                "pmids_sha256": self.dependencies._pmid_ids_sha256(parsed["ids"])}


    def _paths_same(self, a, b):
        import os
        try:
            return os.path.samefile(a, b)
        except OSError:
            return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


    def _read_pmid_file(self, path, *, baseline=False):
        """读取 PMID 清单；严格逐行解析文本，或只读取已知 schema JSON 的 candidate_pmids。

        返回 {ids, raw_count, path, sha256, schema}。ids 保序去重并规范成裸十进制；
        raw_count 是去重前的输入个数（文本按 token 计，JSON 按 candidate_pmids 元素计）。
        本项目自订：文本 schema 为 pubmed.pmids.text.v1；JSON 只认 links/diff/snowball 三个 schema。
        文本每一行单独交给 _parse_pmids，所以歧义千位分隔与非法 token 能报出行号；空文件合法。
        baseline=True 时拒绝 diff schema：它的 candidate_pmids 只代表新增集合，不是完整旧集合。
        唯一 ID 数超过 EXPORT_MAX 直接报错，不截断。
        """
        import hashlib
        import json
        import os
        import re

        s = str(path or "").strip()
        if not s:
            raise self.dependencies.NcbiError("PMID 清单路径不能为空")
        p = os.path.abspath(os.path.expanduser(s))
        if os.path.isdir(p):
            raise self.dependencies.NcbiError(f"PMID 清单路径是目录而不是文件：{p}")
        try:
            with open(p, "rb") as f:
                raw = f.read()
        except OSError as e:
            raise self.dependencies.NcbiError(f"无法读取 PMID 清单（{p}）：{e}") from e
        sha = hashlib.sha256(raw).hexdigest()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise self.dependencies.NcbiError(f"PMID 清单不是 UTF-8 文本（{p}）：{e}") from e

        is_json = p.lower().endswith(".json") or text.lstrip().startswith("{")
        if is_json:
            try:
                doc = json.loads(text)
            except ValueError as e:
                raise self.dependencies.NcbiError(f"PMID 清单 JSON 无法解析（{p}）：{e}") from e
            if not isinstance(doc, dict):
                raise self.dependencies.NcbiError(f"PMID 清单 JSON 顶层必须是对象（{p}）")
            if doc.get("schema") == "pubmed.links.v1":
                pagination = doc.get("pagination") or {}
                if not isinstance(pagination, dict):
                    raise self.dependencies.NcbiError(f"引用候选 pagination 字段无效：{p}")
                if (pagination.get("applies_to") == "returned_json"
                        and (pagination.get("offset") or pagination.get("limit") is not None)):
                    raise self.dependencies.NcbiError("不能把分页后的引用 JSON 当完整清单；请用 links(out_path=...) 保存全量候选")
            schema = doc.get("schema")
            if schema not in self.dependencies._CANDIDATE_SCHEMAS:
                raise self.dependencies.NcbiError(
                    f"未知的 PMID 清单 schema：{schema!r}（{p}）。只接受 "
                    + " / ".join(sorted(self.dependencies._CANDIDATE_SCHEMAS))
                    + "，且只读取其中的 candidate_pmids；不从任意 JSON 递归提取数字。")
            if baseline and schema in (self.dependencies._DIFF_SCHEMA, "pubmed.snowball.v1", "pubmed.links.v1"):
                raise self.dependencies.NcbiError(
                    f"不能把 {schema} 当作 baseline：其中的 candidate_pmids 只代表新增或引用候选集合，"
                    f"不是完整的旧检索集合。请改用完整导出清单。")
            if "candidate_pmids" not in doc:
                raise self.dependencies.NcbiError(f"JSON 清单缺少 candidate_pmids 字段：{p}")
            cand = doc.get("candidate_pmids")
            if not isinstance(cand, list):
                raise self.dependencies.NcbiError(f"candidate_pmids 必须是数组，不能是 {type(cand).__name__}：{p}")
            ids, bad = [], []
            for i, v in enumerate(cand):
                if isinstance(v, bool) or not isinstance(v, (str, int)):
                    bad.append((i, v))
                    continue
                t = str(v).strip()
                if not re.fullmatch(r"[0-9]+", t):
                    bad.append((i, v))
                    continue
                try:
                    ids.append(str(int(t)))
                except ValueError as e:
                    raise self.dependencies.NcbiError(f"candidate_pmids[{i}] 数字过长，无法规范：{str(v)[:40]}") from e
            if bad:
                i, v = bad[0]
                raise self.dependencies.NcbiError(
                    f"candidate_pmids 第 {i} 个元素不是纯数字 PMID：{v!r}（{p}）。"
                    f"只接受裸十进制 ID，不接受 PMC 号、逗号或其它数据库 ID。")
            unique = list(dict.fromkeys(ids))
            if len(unique) > self.dependencies.EXPORT_MAX:
                raise self.dependencies.NcbiError(
                    f"PMID 清单有 {len(unique)} 个唯一号，超过本项目自订安全上限 {self.dependencies.EXPORT_MAX}（{p}）；"
                    f"请拆分文件后分批导出。")
            return {"ids": unique, "raw_count": len(cand), "path": p, "sha256": sha, "schema": schema}

        ids, raw_count = [], 0
        for line_no, line in enumerate(text.splitlines(), 1):
            t = line.strip()
            if not t:
                continue
            try:
                line_ids, n = self.dependencies._parse_pmids(t)
            except self.dependencies.NcbiError as e:
                raise self.dependencies.NcbiError(f"{p} 第 {line_no} 行：{e}") from e
            raw_count += n
            ids.extend(line_ids)
        unique = list(dict.fromkeys(ids))
        if len(unique) > self.dependencies.EXPORT_MAX:
            raise self.dependencies.NcbiError(
                f"PMID 清单有 {len(unique)} 个唯一号，超过本项目自订安全上限 {self.dependencies.EXPORT_MAX}（{p}）；"
                f"请拆分文件后分批导出。")
        return {"ids": unique, "raw_count": raw_count, "path": p, "sha256": sha,
                "schema": self.dependencies._PMID_TEXT_SCHEMA}


    def _write_json_new(self, path, obj, *, indent=2):  # 本项目自订：默认缩进保持现有文件格式
        """原子写新 JSON 文件且不覆盖；indent=None 保留紧凑输出且不加末尾换行。"""
        body = json.dumps(obj, ensure_ascii=False, indent=indent)
        if indent is not None:
            body += "\n"
        return self.dependencies._write_new_file(path, body)


    def _write_json_atomic(self, path, obj):
        """覆盖式原子更新作业清单：同目录临时文件 + os.replace。"""
        import json
        import os
        import tempfile

        p = os.path.abspath(path)
        parent = os.path.dirname(p)
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(p) + ".", suffix=".tmp", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except OSError as e:
            raise self.dependencies.NcbiError(f"无法原子更新作业清单（{p}）：{e}") from e
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"⚠️ 作业清单临时文件清理失败（{tmp}）：{e}", file=sys.stderr)


    def _diff_files(self, baseline, current, out):
        """离线集合差；输出 schema=pubmed.diff.v1，candidate_pmids 只装新增集合。

        新增按 current 首次出现顺序；removed/common 按 baseline 顺序。输出含完整列表、
        计数、两份输入的 path/sha256/schema/raw_count/unique_count/pmids_sha256。
        不覆盖输入或已有文件；baseline 若为 diff schema 直接拒绝。
        """
        import os

        b_s, c_s, o_s = (str(baseline or "").strip(), str(current or "").strip(), str(out or "").strip())
        if not b_s:
            raise self.dependencies.NcbiError("baseline 路径不能为空")
        if not c_s:
            raise self.dependencies.NcbiError("current 路径不能为空")
        if not o_s:
            raise self.dependencies.NcbiError("差集输出路径不能为空")
        b_abs, c_abs = os.path.abspath(os.path.expanduser(b_s)), os.path.abspath(os.path.expanduser(c_s))
        out_abs = os.path.abspath(os.path.expanduser(o_s))
        if self.dependencies._paths_same(out_abs, b_abs) or self.dependencies._paths_same(out_abs, c_abs):
            raise self.dependencies.NcbiError("差集输出不能与任一输入文件同路径；请换个新路径。")
        out_abs = self.dependencies._check_out_path(out_abs)
        b = self.dependencies._read_pmid_file(b_abs, baseline=True)
        c = self.dependencies._read_pmid_file(c_abs)
        bset, cset = set(b["ids"]), set(c["ids"])
        added = [p for p in c["ids"] if p not in bset]
        removed = [p for p in b["ids"] if p not in cset]
        common = [p for p in b["ids"] if p in cset]
        doc = {
            "schema": self.dependencies._DIFF_SCHEMA,
            "generated_at": self.dependencies._utc_now(),
            "candidate_pmids": added,
            "removed_pmids": removed,
            "common_pmids": common,
            "added_count": len(added), "removed_count": len(removed), "common_count": len(common),
            "counts": {
                "baseline_raw": b["raw_count"], "baseline_unique": len(b["ids"]),
                "current_raw": c["raw_count"], "current_unique": len(c["ids"]),
                "candidate": len(added), "added": len(added),
                "removed": len(removed), "common": len(common),
            },
            "inputs": {"baseline": self.dependencies._pmid_binding(b), "current": self.dependencies._pmid_binding(c)},
            "warnings": [],
        }
        doc["interpretation"] = ("新增是当前名单有、基线没有；未再出现（removed）是基线有、当前名单没有，"
                                 "不代表文献被数据库删除。若当前名单仅覆盖补检入库区间，区间外旧文献也会列入 removed。"
                                 "保留完整基线，不能用补检名单或新增名单替换；新增仍需阅读题名摘要判断相关性。")
        doc["evidence"] = "来源：输入清单及其哈希；仅做集合运算，不判断相关性或数据库变化原因"
        path, nbytes, sha = self.dependencies._write_json_new(out_abs, doc)
        self.dependencies._audit("diff_files", path=path, sha256=sha, added=len(added), removed=len(removed), common=len(common), inputs=doc["inputs"])
        return {"schema": self.dependencies._DIFF_SCHEMA, "path": path, "bytes": nbytes, "sha256": sha,
                "counts": doc["counts"], "candidate_pmids": len(added),
                "removed_pmids": len(removed), "common_pmids": len(common),
                "interpretation": doc["interpretation"]}

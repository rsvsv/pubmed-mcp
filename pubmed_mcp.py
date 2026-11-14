#!/usr/bin/env python
"""自建 PubMed MCP 服务：裸打 NCBI eutils，带 api_key、超时、退避、限速，失败绝不静默。

依赖版本（实测 2026-11-08，本机 importlib.metadata）：mcp 1.29.0 / anyio 4.14.2 / pydantic 2.13.4
  本文件把工具写成 async def 并把网络调用挪进线程，依据是 mcp SDK 的实现细节：
  mcp/server/fastmcp/utilities/func_metadata.py 里非 async 分支是裸 `return fn(**kwargs)`，
  没有 threadpool —— 也就是 sync 工具会占住事件循环，一次 40s 的 efetch 能把整个 server 冻住。
  SDK 升级后该分支若改成 threadpool，本文件的 async 写法依然正确，不必回退。
  〔实测 2026-11-10，这句话被 mcp 2.3.0 触发了：上面那段「裸 return fn(**kwargs)、没有 threadpool」
  是 1.x 的行为；2.x 里该文件挪到 mcp/server/mcpserver/utilities/func_metadata.py，非 async 分支变成
  `return await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))`。所以那句「不必回退」
  现在有了证据：本文件的 async 写法在两代 SDK 下都不是占住事件循环的那一方。〕

为什么自己写（对比 pubmedmcp 0.1.4 + pubmedclient 0.2.0）——【已不可就地复核】：
  〔实测于本文件初版；2026-11-08 复核：本机 pip show 两个包均 not found、`where pubmedmcp` 无结果、
   无 pipx，所以下面 4 条当时是看过源码得出的，现在无法重跑验证。别当事实引用，要用就重装再看。〕
  - 那两个包不读 api_key，等于自降为 3 次/秒；这里带 key（10 次/秒）
  - 那个包不设 timeout，卡住就干等；这里 esearch/esummary 25s / efetch 40s 起
  - 那个包出错时把异常打成空字符串，"查不到"和"没查到"分不清；
    这里 0 命中会明说「请求成功、真的没有匹配」，请求失败会把 NCBI 原文带回来
  - 429/5xx 按 Retry-After 退避重试，最多 4 次

2026-11-08 重写要点（每条都有实测或源码依据，见 --selftest / --probe）：
  1. ⚠️ 机制改读 querytranslation/translationset。依据：实测 esearch retmode=json 的
     esearchresult 有 count/retmax/retstart/idlist/translationset/querytranslation/errorlist
     （〔订正 2026-11-10〕原文写的是 `restart`，实测键名是 **retstart**，逐字打印过四个词的返回）。
     ⚠️ 原文那句「warninglist 根本不存在，那 6 行是死代码」【已作废，2026-11-10 实测】：
     warninglist 真实存在，三个子键 phrasesignored / quotedphrasesnotfound / outputmessages，
     停用词与「整个引号短语查不到」这两类信息【只在】这里，errorlist 里没有。
     当年的判断大概来自只看了有命中数的查询；0 条的查询才常见 warninglist。
     现在 _note 会读它（见 _warn_bits）。
  2. esummary 分批。依据：实测 300 个 id 拼一个 GET 是 200/取回 300；400 个 id（载荷 3599 字符）
     直接 HTTP 414；改 POST 另撞 NCBI 硬闸「Maximum number of UIDs is 500 for JSON format output」。
  3. fetch_abstracts 报「要几条 / 实取几条 / 缺哪些」，不再拿请求数冒充返回数。
     依据：实测不存在的 PMID 9999999999 会返回「取回 1 条」+ 空壳；改为按 efetch 正文里的
     ^PMID: 行计数，这是 NCBI 排版每条必有的一行。
  4. field 走白名单。依据：实测 field=Title→7 / Tiab→28 / Journal→22 / Author→0 都生效，
     而 field=ZZZBOGUSFIELD→82 与不传 field 完全一致且无任何 ⚠️（拼错一个字母=限定被静默吃掉）；
     field=MeSH Terms、Publication Type 则让 esearch 不返回 count 字段（见第 5 条）。
  5. count 字段缺失/非数字一律抛 NcbiError，不再印「命中 ? 条」。
  6. 网络调用出事件循环 + 按有无 key 限速（本机有 key，档位 10 次/秒〔实测〕；
     key 的任何片段都不写进源码——发布这份文件等于公开发布它）。

用法：
    python pubmed_mcp.py                       # 当 MCP stdio 服务（Claude 用这个）
    python pubmed_mcp.py --selftest 牙根吸收     # 不走 MCP，直接打印结果，自检用
    python pubmed_mcp.py --check-key            # 只报告 key/限速档位，不发查询
    python pubmed_mcp.py --probe                # 跑一组断言，回归用
    #   **断言数与请求数都由 --probe 自己在末行报**（末行形状：「N 通过 / M 失败 —— 本轮 K 次 HTTP
    #   请求」），所以这里不写死任何数字——写死过一次就注定过期：121 那条注释写下当天就变 133 了
    #   〔实测 2026-11-10，补丁 20–22 各加了几条〕。量级供参考：88 次/121 条、71 次/98 条〔实测〕。
    #   旧注释写「约 10 次请求」是按节数拍的，差了 7-8 倍；按 10 次估限速会估错一个量级——
    #   一轮探针要按【几十秒到几分钟】算。

key 读取顺序：环境变量 NCBI_API_KEY → 文件 ~/.ncbi_api_key → 都没有就走匿名（3 次/秒）
依赖：Python 标准库 + mcp(FastMCP)；anyio 随 mcp 一起装。
"""

import functools
import calendar
import csv
import datetime
import hashlib
import io
import inspect                              # 只用来验「装饰后还是协程函数」这条静态事实
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Literal

import anyio
from pydantic import StrictInt, StrictStr

# Load adjacent implementation modules even under python -I file execution.
import importlib.util as _importlib_util
_runtime_spec = _importlib_util.spec_from_file_location(
    "pubmed_runtime", os.path.join(os.path.dirname(__file__), "pubmed_runtime.py"))
_runtime = _importlib_util.module_from_spec(_runtime_spec)
_runtime_spec.loader.exec_module(_runtime)
_query = _runtime.load_sibling("pubmed_query")
_records = _runtime.load_sibling("pubmed_records")
_report = _runtime.load_sibling("pubmed_report")
_audit_module = _runtime.load_sibling("pubmed_audit")
_runtime_context = _audit_module.RuntimeContext()
_implementation_fingerprint = _runtime.implementation_fingerprint

_transport_module = _runtime.load_sibling('pubmed_transport')
def _transport_set__KEY(value):
    global _KEY
    _KEY = value
def _transport_set__KEY_SOURCE(value):
    global _KEY_SOURCE
    _KEY_SOURCE = value
def _transport_set__NREQ(value):
    global _NREQ
    _NREQ = value
def _transport_set__PACER(value):
    global _PACER
    _PACER = value
_transport_impl = _transport_module.Implementation(_runtime.Dependencies(
    _transport_module.DEPENDENCIES, {
        'BASE': lambda: BASE,
        'EMAIL': lambda: EMAIL,
        'NcbiError': lambda: NcbiError,
        'TOOL': lambda: TOOL,
        '_AUDIT_CONTEXT': lambda: _AUDIT_CONTEXT,
        '_KEY': lambda: _KEY,
        '_KEY_SOURCE': lambda: _KEY_SOURCE,
        '_NREQ': lambda: _NREQ,
        '_PACER': lambda: _PACER,
        '_PACER_INIT': lambda: _PACER_INIT,
        '_Pacer': lambda: _Pacer,
        '_REQ_CTR': lambda: _REQ_CTR,
        '_audit_clean': lambda: _audit_clean,
        '_decode_key_bytes': lambda: _decode_key_bytes,
        '_get': lambda: _get,
        '_mask': lambda: _mask,
        '_pace': lambda: _pace,
        '_retry_after': lambda: _retry_after,
        '_thread_before_attempt': lambda: _thread_before_attempt,
        '_thread_wait': lambda: _thread_wait,
        'api_key': lambda: api_key,
    }, {'_KEY': _transport_set__KEY, '_KEY_SOURCE': _transport_set__KEY_SOURCE, '_NREQ': _transport_set__NREQ, '_PACER': _transport_set__PACER}))

_files_module = _runtime.load_sibling('pubmed_files')
_files_impl = _files_module.Implementation(_runtime.Dependencies(
    _files_module.DEPENDENCIES, {
        'EXPORT_MAX': lambda: EXPORT_MAX,
        'NcbiError': lambda: NcbiError,
        '_CANDIDATE_SCHEMAS': lambda: _CANDIDATE_SCHEMAS,
        '_DIFF_SCHEMA': lambda: _DIFF_SCHEMA,
        '_PMID_TEXT_SCHEMA': lambda: _PMID_TEXT_SCHEMA,
        '_audit': lambda: _audit,
        '_check_out_path': lambda: _check_out_path,
        '_parse_pmids': lambda: _parse_pmids,
        '_paths_same': lambda: _paths_same,
        '_pmid_binding': lambda: _pmid_binding,
        '_pmid_ids_sha256': lambda: _pmid_ids_sha256,
        '_publish_no_clobber': lambda: _publish_no_clobber,
        '_read_pmid_file': lambda: _read_pmid_file,
        '_utc_now': lambda: _utc_now,
        '_write_json_new': lambda: _write_json_new,
        '_write_new_file': lambda: _write_new_file,
    }, {}))

_export_module = _runtime.load_sibling('pubmed_export')
_export_impl = _export_module.Implementation(_runtime.Dependencies(
    _export_module.DEPENDENCIES, {
        'EFETCH_CHUNK': lambda: EFETCH_CHUNK,
        'ESUMMARY_CHUNK': lambda: ESUMMARY_CHUNK,
        'EXPORT_MAX': lambda: EXPORT_MAX,
        'NO_BEGIN': lambda: NO_BEGIN,
        'NO_END': lambda: NO_END,
        'NcbiError': lambda: NcbiError,
        'WINDOW': lambda: WINDOW,
        '_AUDIT_CONTEXT': lambda: _AUDIT_CONTEXT,
        '_EXPORT_BATCH_EXT': lambda: _EXPORT_BATCH_EXT,
        '_EXPORT_BATCH_SIZE': lambda: _EXPORT_BATCH_SIZE,
        '_EXPORT_FORMATS': lambda: _EXPORT_FORMATS,
        '_EXPORT_FORMAT_VERSION': lambda: _EXPORT_FORMAT_VERSION,
        '_EXPORT_JOB_SCHEMA': lambda: _EXPORT_JOB_SCHEMA,
        '_EXPORT_JOB_VERSION': lambda: _EXPORT_JOB_VERSION,
        '_EXPORT_RESULT_SCHEMA': lambda: _EXPORT_RESULT_SCHEMA,
        '_EXPORT_STATE_DIR': lambda: _EXPORT_STATE_DIR,
        '_EXPORT_STATE_SCHEMA': lambda: _EXPORT_STATE_SCHEMA,
        '_PMIDS_CSV_HEADER': lambda: _PMIDS_CSV_HEADER,
        '_REQ_CTR': lambda: _REQ_CTR,
        '__version__': lambda: __version__,
        '_acquire_job_lock': lambda: _acquire_job_lock,
        '_add_job_warning': lambda: _add_job_warning,
        '_as_bool': lambda: _as_bool,
        '_audit': lambda: _audit,
        '_audit_clean': lambda: _audit_clean,
        '_batch_file_path': lambda: _batch_file_path,
        '_batch_rel': lambda: _batch_rel,
        '_batch_state_binding': lambda: _batch_state_binding,
        '_batch_state_path': lambda: _batch_state_path,
        '_batch_state_payload': lambda: _batch_state_payload,
        '_check_date': lambda: _check_date,
        '_check_datetype': lambda: _check_datetype,
        '_check_job_output': lambda: _check_job_output,
        '_check_out_path': lambda: _check_out_path,
        '_check_reldate': lambda: _check_reldate,
        '_collect_window_checks': lambda: _collect_window_checks,
        '_concat_batches_to_file': lambda: _concat_batches_to_file,
        '_count_of': lambda: _count_of,
        '_date_mid': lambda: _date_mid,
        '_efetch_xml_all': lambda: _efetch_xml_all,
        '_esearch': lambda: _esearch,
        '_esummary_dicts': lambda: _esummary_dicts,
        '_execute_export_job': lambda: _execute_export_job,
        '_expand': lambda: _expand,
        '_export_csv': lambda: _export_csv,
        '_export_csv_batch': lambda: _export_csv_batch,
        '_export_job_batches': lambda: _export_job_batches,
        '_export_medline': lambda: _export_medline,
        '_export_medline_batch': lambda: _export_medline_batch,
        '_export_metadata': lambda: _export_metadata,
        '_export_ris': lambda: _export_ris,
        '_export_ris_batch': lambda: _export_ris_batch,
        '_export_run': lambda: _export_run,
        '_export_slice': lambda: _export_slice,
        '_fixed_job_path': lambda: _fixed_job_path,
        '_freeze_export_query': lambda: _freeze_export_query,
        '_get_async': lambda: _get_async,
        '_ids_in_window': lambda: _ids_in_window,
        '_inspect_batch_file': lambda: _inspect_batch_file,
        '_job_path': lambda: _job_path,
        '_load_batch_state': lambda: _load_batch_state,
        '_load_job_manifest': lambda: _load_job_manifest,
        '_mask': lambda: _mask,
        '_new_job_manifest': lambda: _new_job_manifest,
        '_next_day': lambda: _next_day,
        '_normalize_writer_output': lambda: _normalize_writer_output,
        '_paths_same': lambda: _paths_same,
        '_pmid_binding': lambda: _pmid_binding,
        '_pmid_ids_sha256': lambda: _pmid_ids_sha256,
        '_publish_export_file': lambda: _publish_export_file,
        '_publish_no_clobber': lambda: _publish_no_clobber,
        '_read_pmid_file': lambda: _read_pmid_file,
        '_record_count_change': lambda: _record_count_change,
        '_record_parts': lambda: _record_parts,
        '_release_job_lock': lambda: _release_job_lock,
        '_remove_batch_state': lambda: _remove_batch_state,
        '_required_windows': lambda: _required_windows,
        '_reset_batch_for_refetch': lambda: _reset_batch_for_refetch,
        '_retrieval_check': lambda: _retrieval_check,
        '_revalidate_job_input': lambda: _revalidate_job_input,
        '_ris_block': lambda: _ris_block,
        '_save_batch_state': lambda: _save_batch_state,
        '_sha256_file': lambda: _sha256_file,
        '_store_batch': lambda: _store_batch,
        '_stream_export': lambda: _stream_export,
        '_tool_version': lambda: _tool_version,
        '_utc_now': lambda: _utc_now,
        '_validate_job_manifest': lambda: _validate_job_manifest,
        '_verify_done_batch': lambda: _verify_done_batch,
        '_verify_export': lambda: _verify_export,
        '_warn_bits': lambda: _warn_bits,
        '_write_json_atomic': lambda: _write_json_atomic,
        '_write_new_file': lambda: _write_new_file,
        '_write_text': lambda: _write_text,
        '_zero_explanation': lambda: _zero_explanation,
    }, {}))

_diagnostics_module = _runtime.load_sibling('pubmed_diagnostics')
def _diagnostics_set__KEY(value):
    global _KEY
    _KEY = value
def _diagnostics_set__get_async(value):
    global _get_async
    _get_async = value
def _diagnostics_set__pmids_exist(value):
    global _pmids_exist
    _pmids_exist = value
_diagnostics_impl = _diagnostics_module.Implementation(_runtime.Dependencies(
    _diagnostics_module.DEPENDENCIES, {
        'EMAIL': lambda: EMAIL,
        'FastMCP': lambda: FastMCP,
        'GOLD_MAX': lambda: GOLD_MAX,
        'NO_BEGIN': lambda: NO_BEGIN,
        'NO_END': lambda: NO_END,
        'NcbiError': lambda: NcbiError,
        'PAGE_TERM': lambda: PAGE_TERM,
        'SORT_REAL': lambda: SORT_REAL,
        '_EXTRA_FORBID': lambda: _EXTRA_FORBID,
        '_KEY': lambda: _KEY,
        '_KEY_SOURCE': lambda: _KEY_SOURCE,
        '_NREQ': lambda: _NREQ,
        '_all_dropped': lambda: _all_dropped,
        '_as_bool': lambda: _as_bool,
        '_check_date': lambda: _check_date,
        '_check_db': lambda: _check_db,
        '_check_field': lambda: _check_field,
        '_decode_key_bytes': lambda: _decode_key_bytes,
        '_esearch': lambda: _esearch,
        '_get_async': lambda: _get_async,
        '_key_shape': lambda: _key_shape,
        '_ln': lambda: _ln,
        '_mask': lambda: _mask,
        '_note': lambda: _note,
        '_num_of': lambda: _num_of,
        '_parse_pmids': lambda: _parse_pmids,
        '_pmids_exist': lambda: _pmids_exist,
        '_raises_enc': lambda: _raises_enc,
        '_row': lambda: _row,
        '_safe_json': lambda: _safe_json,
        '_warn_bits': lambda: _warn_bits,
        '_windows': lambda: _windows,
        '_zero_explanation': lambda: _zero_explanation,
        'api_key': lambda: api_key,
        'check_recall': lambda: check_recall,
        'count': lambda: count,
        'diff': lambda: diff,
        'export': lambda: export,
        'fetch_abstracts': lambda: fetch_abstracts,
        'links': lambda: links,
        'main': lambda: main,
        'mcp': lambda: mcp,
        'mesh': lambda: mesh,
        'search': lambda: search,
    }, {'_KEY': _diagnostics_set__KEY, '_get_async': _diagnostics_set__get_async, '_pmids_exist': _diagnostics_set__pmids_exist}))

_tools_module = _runtime.load_sibling('pubmed_tools')
_tools_impl = _tools_module.Implementation(_runtime.Dependencies(
    _tools_module.DEPENDENCIES, {
        'ABSTRACT_DEFAULT_FIELDS': lambda: ABSTRACT_DEFAULT_FIELDS,
        'ABSTRACT_FIELDS': lambda: ABSTRACT_FIELDS,
        'DB_WHITELIST': lambda: DB_WHITELIST,
        'DIFF_SHOW_CAP': lambda: DIFF_SHOW_CAP,
        'EFETCH_CHUNK': lambda: EFETCH_CHUNK,
        'ESUMMARY_CHUNK': lambda: ESUMMARY_CHUNK,
        'EXPORT_MAX': lambda: EXPORT_MAX,
        'FETCH_MAX': lambda: FETCH_MAX,
        'FETCH_WARN_CHARS': lambda: FETCH_WARN_CHARS,
        'FIELD_NO_COUNT': lambda: FIELD_NO_COUNT,
        'FIELD_SWALLOWED': lambda: FIELD_SWALLOWED,
        'FIELD_WHITELIST': lambda: FIELD_WHITELIST,
        'GOLD_CHUNK': lambda: GOLD_CHUNK,
        'GOLD_MAX': lambda: GOLD_MAX,
        'MESH_CHILD_SHOW': lambda: MESH_CHILD_SHOW,
        'MESH_PARENT_SHOW': lambda: MESH_PARENT_SHOW,
        'MESH_RETMAX_CAP': lambda: MESH_RETMAX_CAP,
        'NO_BEGIN': lambda: NO_BEGIN,
        'NO_END': lambda: NO_END,
        'NcbiError': lambda: NcbiError,
        'RELDATE_MAX': lambda: RELDATE_MAX,
        'RETMAX_CAP': lambda: RETMAX_CAP,
        'RETSTART_MAX': lambda: RETSTART_MAX,
        'SNOW_BATCH_CAP': lambda: SNOW_BATCH_CAP,
        'SNOW_SINGLE_CAP': lambda: SNOW_SINGLE_CAP,
        'SORT_IGNORED': lambda: SORT_IGNORED,
        'SORT_WHITELIST': lambda: SORT_WHITELIST,
        'WINDOW': lambda: WINDOW,
        '_AUDIT_UNKNOWN': lambda: _AUDIT_UNKNOWN,
        '_CJK': lambda: _CJK,
        '_DATE_RE': lambda: _DATE_RE,
        '_as_bool': lambda: _as_bool,
        '_as_list': lambda: _as_list,
        '_audit': lambda: _audit,
        '_brief_record': lambda: _brief_record,
        '_check_date': lambda: _check_date,
        '_check_datetype': lambda: _check_datetype,
        '_check_db': lambda: _check_db,
        '_check_field': lambda: _check_field,
        '_check_out_path': lambda: _check_out_path,
        '_check_reldate': lambda: _check_reldate,
        '_check_sort': lambda: _check_sort,
        '_count_of': lambda: _count_of,
        '_efetch_abstracts': lambda: _efetch_abstracts,
        '_efetch_all': lambda: _efetch_all,
        '_efetch_xml_all': lambda: _efetch_xml_all,
        '_efetch_xml_chunk': lambda: _efetch_xml_chunk,
        '_esearch': lambda: _esearch,
        '_esummary_dicts': lambda: _esummary_dicts,
        '_expand': lambda: _expand,
        '_fmt_brief_record': lambda: _fmt_brief_record,
        '_fmt_compact': lambda: _fmt_compact,
        '_fmt_record': lambda: _fmt_record,
        '_get_async': lambda: _get_async,
        '_ids_in_window': lambda: _ids_in_window,
        '_ids_map': lambda: _ids_map,
        '_int': lambda: _int,
        '_mask': lambda: _mask,
        '_note': lambda: _note,
        '_parse_fields': lambda: _parse_fields,
        '_parse_pmids': lambda: _parse_pmids,
        '_pmids_exist': lambda: _pmids_exist,
        '_project': lambda: _project,
        '_read_pmid_file': lambda: _read_pmid_file,
        '_recall_warnings_and_degraded': lambda: _recall_warnings_and_degraded,
        '_record_count_change': lambda: _record_count_change,
        '_record_parts': lambda: _record_parts,
        '_retrieval_check': lambda: _retrieval_check,
        '_safe_json': lambda: _safe_json,
        '_sort_note': lambda: _sort_note,
        '_term_vanished': lambda: _term_vanished,
        '_utc_now': lambda: _utc_now,
        '_validate_esearch_page': lambda: _validate_esearch_page,
        '_warn_bits': lambda: _warn_bits,
        '_windows': lambda: _windows,
        '_write_json_new': lambda: _write_json_new,
        '_write_new_file': lambda: _write_new_file,
        '_xml_record': lambda: _xml_record,
        '_zero_explanation': lambda: _zero_explanation,
        'links': lambda: links,
    }, {}))

# ---------- SDK 兼容层（放在 NcbiError 之前，因为它的基类要从 SDK 取） ----------
# 〔实测 2026-11-10，本会话两条通路各跑过完整 --probe〕
#   mcp 1.x：  mcp.server.fastmcp.FastMCP → 1.29.0（本机）/ 1.30.0（干净 venv）
#   mcp 2.x：  该模块名【不存在】，改名 mcp.server.mcpserver.MCPServer → 2.3.0
# 本文件对 SDK 只用到三处，两代签名都兼容：`FastMCP(名字)`（v2 第一参数仍是 name）、
# `@mcp.tool()` 裸装饰、`mcp.run()` 默认 stdio。逐字签名见 docs/history/mcp-sdk-v2.md。
# 〔加这层之前，`mcp>=1.2` 会让别人第一次安装就 ModuleNotFoundError：作者本机是 1.29.0，
#  在自己机器上永远复现不出来。别把「我机器能跑」当「装得上」的证据。〕
#
# 第二个名字 ToolError【不是可选的装饰】。mcp 2.3.0 的 mcp/server/mcpserver/tools/base.py 逐字：
#     except (ToolError, ResourceError) as exc:
#         raise ToolError(f"Error executing tool {self.name}: {exc}") from exc     # 原文带上
#     except Exception as exc:
#         raise UnexpectedToolError(f"Error executing tool {self.name}") from exc   # 原文留在服务器
# 本文件的失败说明【全部】写在异常文本里（NCBI 原文、改写建议、实测出处），所以它必须走上面那条
# 分支。〔实测 2026-11-10 A/B：同一个 sort="journal" 拦截，1.30.0 客户端拿到 323 字符，
#  未修前的 2.3.0 只拿到 27 字符 ——「Error executing tool search」。〕
# 唯一一处写区间的文本，两处报错都引用它。下界不是随手写的：〔实测 2026-11-11，干净 venv 配
# pydantic 2.14.3〕mcp 1.2.0 与 1.21.0 都在 import 阶段炸（cannot import name 'eval_type_backport'
# from 'pydantic._internal._typing_extra'），1.26.0 才 IMPORT_OK。写 >=1.2 等于承诺一个跑不起来的下界。
__version__ = "1.4.5"         # 本项目自订发布版本（与 pyproject.toml 的 project.version 一致）
_SDK_RANGE = "mcp>=1.26,<3"


def _mcp_ver():
    try:
        from importlib.metadata import version
        return "版本 " + version("mcp")
    except Exception:
        return "版本未知"


try:
    from mcp.server.fastmcp import FastMCP                        # noqa: E402  mcp 1.x
    from mcp.server.fastmcp.exceptions import ToolError as _SdkToolError
except ModuleNotFoundError:                                       # v2 抛的就是 ModuleNotFoundError
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP      # noqa: E402  mcp 2.x
        from mcp.server.mcpserver.exceptions import ToolError as _SdkToolError
    except ModuleNotFoundError as _sdk_err:
        raise ImportError(
            "拿不到 MCP 服务类。本文件认 mcp 1.x（mcp.server.fastmcp）和 2.x"
            "（mcp.server.mcpserver.MCPServer），两个都没找到说明你装的 mcp 不在这两代里。"
            "先跑 `pip install -U \"%s\"` 再试；还不行就把这条报错和 `pip show mcp` 一起贴到 "
            "https://github.com/rsvsv/pubmed-mcp/issues（这里不会带上你的 API key）。" % _SDK_RANGE
        ) from _sdk_err
except ImportError as _sdk_imp_err:
    # 「在区间内但太老」或依赖冲突时炸的是普通 ImportError，上面那条友好报错根本不会触发。
    # 〔本会话实测过 mcp 1.2/1.21/1.26 配新 pydantic 的 import 结果，见 docs/history/mcp-sdk-v2.md〕
    raise ImportError(
        "mcp 装了但导入失败（%s）。这一般是版本太老或与 pydantic 冲突，不是本文件的问题。"
        "先跑 `pip install -U \"%s\"`；还不行请把这条报错和 `pip show mcp pydantic` 一起贴到 "
        "https://github.com/rsvsv/pubmed-mcp/issues（这里不会带上你的 API key）。"
        "底层错误：%s" % (_mcp_ver(), _SDK_RANGE, str(_sdk_imp_err)[:200])
    ) from _sdk_imp_err

# 多余/拼错的参数必须被拒，不能静默丢掉。〔实测 2026-11-13，1.4.4：export(..., formt="csv") 不报错，
# 按默认 pmids 照写文件——正是本工具主承诺里的「参数被吞掉」。〕根因：两代 SDK 都用 pydantic 按函数签名
# 建参数模型，基类 ArgModelBase 未设 extra，pydantic 默认 extra="ignore"。这里在 @mcp.tool() 注册【之前】
# 改基类配置，之后建的参数模型都继承 extra="forbid"，tools/list 的 schema 也随之带 additionalProperties:false。
# 取舍：会往 arguments 里塞额外字段的客户端将被拒——拒掉比静默吞掉好（README 版本要求一节有说明）。
# 拿不到基类时不静默跳过：_EXTRA_FORBID 留空因，--check-key 打印，--probe 记 FAIL。
_EXTRA_FORBID = ""
try:
    try:
        from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase as _ArgModelBase     # 1.x
    except ModuleNotFoundError:
        from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase as _ArgModelBase   # 2.x
    _ArgModelBase.model_config = {**_ArgModelBase.model_config, "extra": "forbid"}
except Exception as _xf_err:
    _EXTRA_FORBID = f"本代 SDK（{_mcp_ver()}）无法禁止多余参数：{type(_xf_err).__name__}: {_xf_err}"
    print("⚠️ " + _EXTRA_FORBID + "——拼错的参数名会被静默忽略。", file=sys.stderr)

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TOOL = "claude-pubmed-mcp"
EMAIL = os.environ.get("NCBI_EMAIL", "")

ESUMMARY_CHUNK = 100          # 实测 300 可用、400 报 414；留余量
EFETCH_CHUNK = 50             # 实测未做批量-耗时曲线，200/批 + 40s 超时偏险（推算），降到 50
RETMAX_CAP = 1000             # esearch 允许；esummary 由本文件分批，所以不再是 300 那道墙

# 实测过「生效」的放在前面；后面的属于 NCBI 常见标签名，没逐个验证过（未核实），
# 真被 NCBI 吞掉时会由第 5 条的 count 缺失守卫抛错，不会静默。
FIELD_WHITELIST = {
    "title": "Title", "ti": "Title",
    "tiab": "Title/Abstract", "title/abstract": "Title/Abstract",
    # 〔实测 2026-11-08〕`Nat Genet[ta]` 与 `Nat Genet[journal]` 都是 10297 条、qt 都回显 [Journal]，
    # 而 `[tiab]` 是 170 条 —— 旧版把 "ta" 映射成 Title/Abstract，等于把 PubMed 的刊名标签
    # 偷偷换成另一个字段。要标题/摘要请写 tiab。
    "journal": "Journal", "ta": "Journal", "jour": "Journal",
    "author": "Author", "au": "Author",
    "all fields": "All Fields", "keywords": "Keywords",
    "lang": "Language", "language": "Language",
    "affiliation": "Affiliation", "ad": "Affiliation",
    "pmid": "PMID", "pmcid": "PMC Number", "mesh terms": "MeSH Terms",
    "mesh": "MeSH Terms", "publication type": "Publication Type",
    "ptyp": "Publication Type", "corporated author": "Corporate Author",
    "isbn": "ISBN", "issn": "ISSN", "grant": "Grant",
}


class NcbiError(_SdkToolError, RuntimeError):
    """请求层面的失败。和「PubMed 里没有这篇文章」严格区分开。

    基类里那个 SDK 的 ToolError 是**功能性的，不是装饰**〔实测 2026-11-10，本会话 A/B 复现〕：
    mcp 2.x 只把 `ToolError` 分支的异常原文发给调用方，普通 Exception 的理由被留在服务器
    stderr、客户端只看到一句 `Error executing tool <name>`。本文件的 NCBI 原文、改写建议、
    实测出处全在异常文本里，不继承它等于把「失败绝不静默」这条承诺在新用户默认的 2.x 通路
    上关掉。mcp 1.x 两分支都带原文，所以这是单向受益、不改变 1.x 的行为。
    RuntimeError 仍留在基类里，是为了保住外面 `except RuntimeError` 一类的旧写法。"""


_KEY = None
_KEY_SOURCE = ""      # 供 --check-key 说清 key 到底从哪来（环境变量 / 哪个文件 / 无）


_decode_key_bytes = _transport_impl._decode_key_bytes


api_key = _transport_impl.api_key


_key_shape = _transport_impl._key_shape


_mask = _transport_impl._mask


_safe_json = _transport_impl._safe_json


# ---------- 限速 ----------
_Pacer = _transport_module._Pacer


_PACER = None
_PACER_INIT = threading.Lock()


_pace = _transport_impl._pace


# ---------- HTTP ----------
_retry_after = _transport_impl._retry_after


_NREQ = 0          # 本轮发出去多少次 urlopen（含退避重试的每一次）
# 单次工具调用自己的请求计数（export 的「本次约 N 次 HTTP 请求」用）。旧版拿全局 _NREQ 前后差值，
# 同进程并发的其它工具调用会被算进来〔实测 2026-11-13：0 条 export 与 count/fetch 并发时报 6 次，单独跑 3 次〕。
# 值是可变 list：同一调用内 task group 派生的子任务继承同一对象，各线程 append 在 GIL 下原子。
_REQ_CTR = _runtime_context.requests


_get = _transport_impl._get


_thread_before_attempt = _transport_impl._thread_before_attempt


_thread_wait = _transport_impl._thread_wait


_get_async = _transport_impl._get_async


# ---------- eutils ----------
_as_bool = _tools_impl._as_bool


_int = _tools_impl._int


# 〔已作废 2026-11-10：这一行原写「实测：NCBI 只有这两种排序是真生效的」——假的。
#  当时只测了 pub_date / relevance / 一个乱写的 journal_quality，没测过 author。〕
# 实测 2026-11-10（两个词各测一遍：orthodontics、tooth resorption，retmax=5/25 都看了 idlist）：
#   真会换顺序的三个值：pub_date、relevance、author（Author / AUTHOR 同样生效，大小写不敏感）
#   与【不传 sort】逐字同序的：journal、Journal、relevancy、best match、keyword、journal_quality
#     —— journal 那档不只比了 idlist，还取了前 12 条的 esummary.source 看是不是字典序：不是，
#        所以「同序」不是巧合，是 NCBI 真的没排。判断生效与否不能看 HTTP 状态码（全都 200）。
SORT_REAL = {"pub_date": "pub_date", "relevance": "relevance", "author": "author"}
# PubMed 网页上的「Best Match」在 esearch 的 sort 参数里没有对应值：字面传 best match 会被 NCBI
# 忽略（＝默认顺序），而 relevance 是另一种【真实存在】的顺序。本工具仍把前者映射到后者，
# 但这个改写必须在输出里说出来 —— 见 _sort_note。
SORT_ALIAS = {"best match": "relevance"}
SORT_WHITELIST = dict(SORT_REAL, **SORT_ALIAS)
SORT_IGNORED = ("journal", "Journal", "relevancy", "keyword")   # 实测不生效，所以不放进来


_check_sort = _tools_impl._check_sort


_DATE_RE = re.compile(r"^\d{4}([/-]\d{1,2}){0,2}$")

# 〔实测 2026-11-08〕field= 传这三个值时 NCBI 整条忽略限定：count 与 querytranslation
# 与不传 field 时【逐字相同】（miniscrew 三种都回 2144、qt 里根本没有 [Keywords]/[ISSN]/[PMCID]）。
# 而 field=MeSH Terms / Publication Type 更糟：esearch 干脆不返回 count。
# 这五个与其静默给一个「不限定」的数，不如直接拦下来告诉调用方该写 term 里的标签。
FIELD_SWALLOWED = {"Keywords", "PMC Number", "ISSN"}
FIELD_NO_COUNT = {"MeSH Terms", "Publication Type"}


_check_date = _tools_impl._check_date


_expand = _tools_impl._expand


_today = _tools_impl._today


# 哨兵日期。〔实测 2026-11-09〕这两个值 NCBI 都照常接受并原样回显在 querytranslation 里：
#   "tooth" + 0001-01-01:1800-01-01 → 12 条（旧版补 1809-01-01 会把这 12 条静默切掉）
#   "dental implant" + 2026-09-10:2999-12-31 → 807 条（补「今天」2026-11-09 只有 763 条）
# 「不限某一边」就该发一个不可能生效的界，而不是发「今天/收录起点」——后者是个真边界。
NO_BEGIN = "0001-01-01"
NO_END = "2999-12-31"
RELDATE_MAX = 36500          # ≈100 年；再大的 reldate 没有实际意义，且旧版会抛裸 OverflowError
RETSTART_MAX = 9998          # 实测 NCBI："'retstart' cannot be larger than 9998"


# Compatibility names; implementation belongs to pubmed_query.py.
_WIN_RE = _query._WIN_RE
_WIN_LABEL = _query._WIN_LABEL
_windows = _query._windows
_same_label_pairs = _query._same_label_pairs


_check_reldate = _tools_impl._check_reldate


_check_datetype = _tools_impl._check_datetype


_validate_esearch_page = _tools_impl._validate_esearch_page


_esearch = _tools_impl._esearch


_sort_note = _tools_impl._sort_note


_check_field = _tools_impl._check_field


_count_of = _tools_impl._count_of


# Compatibility names; implementation belongs to pubmed_query.py.
_CJK = _query._CJK
_warn_bits = _query._warn_bits
_FW_TAG_RE = _query._FW_TAG_RE
_syntax_findings = _query._syntax_findings
_TAG_OK = _query._TAG_OK
_TAG_BAD = _query._TAG_BAD
_NOEXP_BASES = _query._NOEXP_BASES
_PROX_BASES = _query._PROX_BASES
_DATE_TAGS = _query._DATE_TAGS
_MESH_TAGS = _query._MESH_TAGS
_TAG_SPAN_RE = _query._TAG_SPAN_RE
_tag_norm = _query._tag_norm
_tag_shape = _query._tag_shape
_date_tuple = _query._date_tuple
_term_lint = _query._term_lint
_query_warnings = _query._query_warnings
_required_window_group = _query._required_window_group
_required_windows = _query._required_windows
_note = _query._note
_phrase_gap = _query._phrase_gap
_BOOL_WORDS = _query._BOOL_WORDS
_zero_explanation = _query._zero_explanation
_all_dropped = _query._all_dropped
_as_list = _query._as_list


_parse_pmids = _tools_impl._parse_pmids


_esummary_dicts = _tools_impl._esummary_dicts


_ids_map = _tools_impl._ids_map


_record_parts = _tools_impl._record_parts


_brief_record = _tools_impl._brief_record


_fmt_brief_record = _tools_impl._fmt_brief_record


_fmt_record = _tools_impl._fmt_record


_efetch_abstracts = _tools_impl._efetch_abstracts


_efetch_all = _tools_impl._efetch_all


# ---------- 结构化摘要（1.4.2）：efetch XML 解析 ----------
# 〔实测 2026-11-13，efetch rettype=xml 14 条混批：Cochrane、中文刊、团体作者、撤稿/撤稿声明、
#   StatPearls 与 GeneReviews 书、无摘要 Letter、无页码 BMC〕形状要点：
#   - 根 PubmedArticleSet，子元素 PubmedArticle / PubmedBookArticle（书的树形完全不同）；
#   - 无效号不报错，只是少一条（id=9999999999 → 空 <PubmedArticleSet></PubmedArticleSet>）；
#   - 结构化摘要 = 多个 <AbstractText Label=…>；非结构化 = 一个不带 Label 的 AbstractText；
#   - 摘要里有 <i>/<b>/<sup>/<sub> 内联标签；NCBI 的文本档把它们直接去标签拼接（Na+/K+ 写成 Na+/K+），
#     这里同样只取文字、不另加符号，保证与文本档摘要逐字可比；
#   - 撤稿关联在 CommentsCorrections（RefType=RetractionIn 等），PMID 可能缺（只给 DOI）；
#   - 中文刊另有 OtherAbstract Language=chi（Publisher 版本）。
# 解析失败一律报错（fail closed），不退回文本档冒充。
ABSTRACT_FIELDS = ("title", "abstract", "authors", "journal", "pubdate", "ids", "pt", "lang", "mesh",
                   "comments", "coi", "other_abstracts")
ABSTRACT_DEFAULT_FIELDS = ("title", "abstract", "authors", "journal", "pubdate", "ids", "pt", "lang", "comments")
FETCH_MAX = 2000              # 本项目自订：fetch_abstracts 单次 PMID 上限（2000 号 = 40 批；更多请分次或用 export）
FETCH_WARN_CHARS = 60000      # 本项目自订：默认文本档超过此字符数时追加「可能超出客户端结果上限」提示；
#                               客户端真实上限未核实〔审查子代理观察：60 篇约 15.7 万字符被拒，20 篇正常〕


# Compatibility names; implementation belongs to pubmed_records.py.
_xml_text = _records._xml_text


_parse_fields = _tools_impl._parse_fields


# Compatibility names; implementation belongs to pubmed_records.py.
_xml_sections = _records._xml_sections
_xml_authors = _records._xml_authors
_xml_pubdate = _records._xml_pubdate
_xml_ids = _records._xml_ids
_xml_record = _records._xml_record


_efetch_xml_chunk = _tools_impl._efetch_xml_chunk


_efetch_xml_all = _tools_impl._efetch_xml_all


# Compatibility names; implementation belongs to pubmed_records.py.
_project = _records._project
_fmt_authors = _records._fmt_authors
_fmt_compact = _records._fmt_compact


_write_text = _files_impl._write_text


_sha256_file = _files_impl._sha256_file


_write_new_file = _files_impl._write_new_file


_check_out_path = _files_impl._check_out_path


# ---------- 审计调用上下文 ----------
_AUDIT_CONTEXT = _runtime_context.audit
_AUDIT_LOCK = _runtime_context.lock
_AUDIT_SESSION = _runtime_context.session
_AUDIT_SCHEMA = "pubmed.audit.v1"  # 本项目自订：审计格式标识


def _utc_now():
    return _audit_module.utc_now()


def _audit_clean(value):
    return _audit_module.clean(value, mask=_mask)


def _audit_filename():
    return _audit_module.filename(_AUDIT_SESSION)


def _audit_dependencies():
    # Read replaceable operations at invocation time, including after decoration.
    return dict(context_var=_AUDIT_CONTEXT, schema=_AUDIT_SCHEMA,
                now=lambda: _utc_now(),
                write=lambda *args, **kwargs: _write_audit(*args, **kwargs),
                error_type=NcbiError)


def _audited_sync(fn):
    return _audit_module.audited_sync(fn, **_audit_dependencies())


def _audited(fn):
    return _audit_module.audited(fn, **_audit_dependencies())


# ---------- MCP 工具 ----------
# SDK 兼容层在文件顶部（`import anyio` 之后）——因为 NcbiError 的基类 `_SdkToolError` 要从那里取。
#
# 【已作废 2026-11-10，本会话 A/B 实测推翻】这里原先写「v2 真正改的是【客户端侧】：字段名换
# snake_case、serverInfo.version 变空串、结果多了 structuredContent、protocolVersion 成
# 2025-11-25 —— 本文件一处都不读，所以不受影响」。
# 错在漏看服务端：2.x 还会把【非 ToolError 异常的文本】从发给调用方的结果里剥掉，只留
# `Error executing tool <name>`。同一个 sort="journal" 拦截，1.30.0 客户端拿到 323 字符，
# 未修的 2.3.0 只拿到 27 字符 —— 打的正是本文件「失败绝不静默」这条主承诺。
# 原句留着不删：「我不读那个字段，所以那个改动与我无关」这种推理本身会再犯第二次。
# 修法见顶部兼容层的第二个名字 `_SdkToolError`，以及 NcbiError 的基类。
mcp = FastMCP("PubMed")


search = mcp.tool()(_audited(_tools_impl.search))


count = mcp.tool()(_audited(_tools_impl.count))


fetch_abstracts = mcp.tool()(_audited(_tools_impl.fetch_abstracts))


DB_WHITELIST = {"pmc", "nucleotide", "gene", "pubmed", "citedin", "refs"}


_check_db = _tools_impl._check_db


_pmids_exist = _tools_impl._pmids_exist


SNOW_SINGLE_CAP = 200        # 本项目自订：links(to=citedin/refs) 单 PMID 调用最多展示关联号数
SNOW_BATCH_CAP = 50          # 本项目自订：links(to=citedin/refs) 多 PMID 批量调用每号最多展示数，防 50×200 撑爆输出


links = mcp.tool()(_audited(_tools_impl.links))

# ---------- 系统综述助手（1.4.0 新增） ----------
# 设计依据与实测前提见 docs/history/systematic-review-plan.md（2026-11-11 探针 P1–P3）：
#   - MeSH 库可经 esearch/esummary db=mesh 查，ds_meshterms 给入口词、ds_idxlinks 给树号与子词〔实测 P1〕
#   - 已知集交集 `term AND (pmid[uid] OR …)` 一次 esearch 即可判定找回〔实测 P2〕
#   - 9999 检索窗口对 history 服务器同样生效，esummary/efetch 从 history 取 retstart=10000 均被拒〔实测 P3〕
#     → 取全只能按日期切片，不能靠 history 绕过。
# 分工〔本项目自订〕：工具只做机械可复现的事（检索、集合运算、导出、留痕）；
# 相关性判断、纳入排除由人与 LLM 按预设标准做，工具不声称查全率/查准率。

WINDOW = 9999                # PubMed esearch 检索窗口〔实测：retstart=9998 可取、9999 起被拒〕
EXPORT_MAX = 100000          # 本项目自订：export 安全上限，避免过宽查询跑成小时级
GOLD_MAX = 200               # 本项目自订：check_recall 一次最多金标准 PMID 数
GOLD_CHUNK = 50              # 本项目自订：每批交集的金标准数。〔实测 gold=200 一次交集撞 HTTP 414、gold=100 通过〕取 50 留 URL 长度余量
MESH_RETMAX_CAP = 20         # 本项目自订：mesh 一次最多描述符数
MESH_CHILD_SHOW = 10         # 本项目自订：每个描述符最多展示的下位词数
MESH_PARENT_SHOW = 5         # 本项目自订：每个描述符最多展示的上位词数
DIFF_SHOW_CAP = 200          # 本项目自订：diff 文本档每侧最多展示 PMID 数


def _tool_version():
    # 运行中的源码为版本依据，避免同机旧 distribution / egg-info 冒充当前代码版本。
    return __version__


def _write_audit(event, fields, *, enabled=True):
    return _audit_module.write_audit(
        event, fields, enabled=enabled, clean_fields=_audit_clean, now=_utc_now,
        version=_tool_version, lock=_AUDIT_LOCK, filename=_audit_filename, mask=_mask)


def _audit(event, **fields):
    return _audit_module.audit(event, context_var=_AUDIT_CONTEXT, write=_write_audit, fields=fields)


_retrieval_check = _tools_impl._retrieval_check


_record_count_change = _tools_impl._record_count_change


_collect_window_checks = _tools_impl._collect_window_checks


_ids_in_window = _tools_impl._ids_in_window


# Compatibility names; implementation belongs to pubmed_query.py.
_term_vanished = _query._term_vanished
_DEGRADING_OUTMSG_RE = _query._DEGRADING_OUTMSG_RE
_recall_warnings_and_degraded = _query._recall_warnings_and_degraded


check_recall = mcp.tool()(_audited(_tools_impl.check_recall))


mesh = mcp.tool()(_audited(_tools_impl.mesh))


diff = mcp.tool()(_audited(_tools_impl.diff))


_date_mid = _export_impl._date_mid


_next_day = _export_impl._next_day


_export_slice = _export_impl._export_slice


_export_csv_batch = _export_impl._export_csv_batch


_export_medline_batch = _export_impl._export_medline_batch


# Compatibility names; implementation belongs to pubmed_records.py.
_MONTHS = _records._MONTHS
_ris_date = _records._ris_date
_ris_block = _records._ris_block


_export_ris_batch = _export_impl._export_ris_batch


_stream_export = _export_impl._stream_export


_export_csv = _export_impl._export_csv


_export_ris = _export_impl._export_ris


_export_medline = _export_impl._export_medline


_publish_no_clobber = _export_impl._publish_no_clobber


_freeze_export_query = _export_impl._freeze_export_query


_export_metadata = _export_impl._export_metadata


_verify_export = _export_impl._verify_export


_verify_audit_artifact = _export_impl._verify_audit_artifact


export = mcp.tool()(_audited(_export_impl.export))


_export_run = _export_impl._export_run


# ---------- 自检 ----------
_selftest = _diagnostics_impl._selftest


_check = _diagnostics_impl._check


PAGE_TERM = "\"micro-implant immediate loading\""


_ln = _diagnostics_impl._ln


_row = _diagnostics_impl._row


_num_of = _diagnostics_impl._num_of


_raises_enc = _diagnostics_impl._raises_enc


_probe = _diagnostics_impl._probe


_check_key = _diagnostics_impl._check_key


# ---------- 离线检索记录表：兼容入口与当前实例依赖 ----------
_AUDIT_UNKNOWN = _report._AUDIT_UNKNOWN
_REPORT_COLUMNS = _report._REPORT_COLUMNS
_report_value = _report._report_value


def _validate_audit_record(rec):
    return _report.validate_audit_record(rec, _AUDIT_SCHEMA)


def _audit_report(inputs, out, *, skip_invalid=False, verify_files=False):
    return _report.audit_report(
        inputs, out, skip_invalid=skip_invalid, verify_files=verify_files,
        check_out_path=_check_out_path, error_type=NcbiError, mask=_mask,
        validate_record=_validate_audit_record, verify_artifact=_verify_audit_artifact,
        write_new_file=_write_new_file)


_snowball = _audited(_tools_impl._snowball)


# ---------- PMID 清单 / 离线差集 / 可续跑题录导出（第 5 批 B/C、第 4 批 D） ----------
# 本项目自订：以下 schema 名、作业字段与结果字段都是本文件新定义的，不是 MCP 或 PubMed 的协议字段。
_PMID_TEXT_SCHEMA = "pubmed.pmids.text.v1"
_DIFF_SCHEMA = "pubmed.diff.v1"
_CANDIDATE_SCHEMAS = frozenset({"pubmed.links.v1", "pubmed.diff.v1", "pubmed.snowball.v1"})
_EXPORT_JOB_SCHEMA = "pubmed.export.job.v1"
_EXPORT_JOB_VERSION = 1
_EXPORT_FORMAT_VERSION = 2  # 本项目自订：CSV 恢复产物统一已发布 CRLF 规则
_EXPORT_RESULT_SCHEMA = "pubmed.export.pmids.v1"
_EXPORT_FORMATS = ("pmids", "csv", "medline", "ris")
_EXPORT_BATCH_SIZE = {"pmids": 1000, "csv": ESUMMARY_CHUNK, "ris": EFETCH_CHUNK, "medline": EFETCH_CHUNK}
_EXPORT_BATCH_EXT = {"pmids": "txt", "csv": "csv", "ris": "ris", "medline": "nbib"}
_EXPORT_STATE_SCHEMA = "pubmed.export.state.v1"  # 本项目自订：逐批增量状态文件格式
_EXPORT_STATE_DIR = "state"                      # 本项目自订：作业目录内的增量状态子目录
_PMIDS_CSV_HEADER = ("pmid", "pubdate", "title", "journal", "authors", "author_count",
                     "doi", "pmc", "pubtype", "note")


_pmid_ids_sha256 = _files_impl._pmid_ids_sha256


_pmid_binding = _files_impl._pmid_binding


_paths_same = _files_impl._paths_same


_read_pmid_file = _files_impl._read_pmid_file


_write_json_new = _files_impl._write_json_new


_write_json_atomic = _files_impl._write_json_atomic


_diff_files = _audited(_files_impl._diff_files)


_check_job_output = _export_impl._check_job_output


_export_job_batches = _export_impl._export_job_batches


_job_path = _export_impl._job_path


_acquire_job_lock = _export_impl._acquire_job_lock


_release_job_lock = _export_impl._release_job_lock


_add_job_warning = _export_impl._add_job_warning


_load_job_manifest = _export_impl._load_job_manifest


_validate_job_manifest = _export_impl._validate_job_manifest


_new_job_manifest = _export_impl._new_job_manifest


_batch_rel = _export_impl._batch_rel


_inspect_batch_file = _export_impl._inspect_batch_file


_normalize_writer_output = _export_impl._normalize_writer_output


_store_batch = _export_impl._store_batch


_verify_done_batch = _export_impl._verify_done_batch


_fixed_job_path = _export_impl._fixed_job_path


_batch_file_path = _export_impl._batch_file_path


_batch_state_path = _export_impl._batch_state_path


_batch_state_binding = _export_impl._batch_state_binding


_batch_state_payload = _export_impl._batch_state_payload


_save_batch_state = _export_impl._save_batch_state


_remove_batch_state = _export_impl._remove_batch_state


_load_batch_state = _export_impl._load_batch_state


_reset_batch_for_refetch = _export_impl._reset_batch_for_refetch


_concat_batches_to_file = _export_impl._concat_batches_to_file


_publish_export_file = _export_impl._publish_export_file


_revalidate_job_input = _export_impl._revalidate_job_input


_execute_export_job = _export_impl._execute_export_job


_export_pmids = _audited(_export_impl._export_pmids)


_resume_export = _audited(_export_impl._resume_export)


def review_services():
    """Build a review adapter over this instance, with late-bound callbacks."""
    service_module = _runtime.load_sibling("pubmed_services")
    def set_session(value):
        global _AUDIT_SESSION
        _AUDIT_SESSION = value
    return service_module.ReviewServices({
        'write_new_file': lambda: _write_new_file,
        'sha256_file': lambda: _sha256_file,
        'check_datetype': lambda: _check_datetype,
        'check_date': lambda: _check_date,
        'expand_date': lambda: _expand,
        'validate_audit_record': lambda: _validate_audit_record,
        'audited': lambda: _audited,
        'fetch_records': lambda: _efetch_xml_all,
        'utc_now': lambda: _utc_now,
        'audit': lambda: _audit,
        'ris_record': lambda: _ris_block,
        'audit_report': lambda: _audit_report,
        'read_pmid_file': lambda: _read_pmid_file,
        'publish_no_clobber': lambda: _publish_no_clobber,
        'write_json_atomic': lambda: _write_json_atomic,
        'implementation_fingerprint': lambda: _implementation_fingerprint,
        'export': lambda: export,
        'check_recall': lambda: check_recall,
        'mask': lambda: _mask,
    }, {
        'recall_limit': lambda: GOLD_MAX,
        'unknown': lambda: _AUDIT_UNKNOWN,
        'report_columns': lambda: _REPORT_COLUMNS,
        'source_path': lambda: __file__,
        'error_type': lambda: NcbiError,
    }, lambda: _AUDIT_SESSION, set_session)

def main(argv=None):
    """Keep the historical entrypoint; argument parsing lives in the sibling CLI module."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        mcp.run()  # stdio: SDK owns stream encoding; no CLI output or imports.
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    # Explicit sibling loading keeps python -I file execution independent of cwd
    # and prevents picking a different installed snapshot through sys.path.
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "pubmed_cli.py")
    if not os.path.isfile(path):
        print("缺少相邻 pubmed_cli.py；请完整安装或复制全部运行模块：" + "、".join(_runtime.RUNTIME_FILES), file=sys.stderr)
        raise SystemExit(1)  # 本项目自订：安装不完整退出状态
    spec = importlib.util.spec_from_file_location("pubmed_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    operations = {
        "export_pmids": _export_pmids, "resume_export": _resume_export,
        "snowball": _snowball, "diff_files": _diff_files,
        "verify_export": _verify_export, "audit_report": _audit_report,
        "selftest": _selftest, "probe": _probe, "concurrent": _check,
        "check_key": _check_key, "version": _tool_version,
        "mask": _mask, "error_type": NcbiError,
    }
    return cli.run(args, operations, os.path.join(os.path.dirname(__file__), "pubmed_review.py"))


if __name__ == "__main__":
    main()

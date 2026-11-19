"""tools implementation; no core or MCP registration import.
Dependencies are an explicit, per-instance, late-bound contract.
Historical evidence and policy comments are retained from the source.
"""
import functools
import datetime
import hashlib
import json
import os
import re
from typing import Annotated, Literal
import anyio
from pydantic import Field, StrictInt, StrictStr

DEPENDENCIES = ('ABSTRACT_DEFAULT_FIELDS', 'ABSTRACT_FIELDS', 'DB_WHITELIST', 'DIFF_SHOW_CAP', 'EFETCH_CHUNK', 'ESUMMARY_CHUNK', 'EXPORT_MAX', 'FETCH_MAX', 'FETCH_WARN_CHARS', 'FIELD_NO_COUNT', 'FIELD_SWALLOWED', 'FIELD_WHITELIST', 'GOLD_CHUNK', 'GOLD_MAX', 'MESH_CHILD_SHOW', 'MESH_PARENT_SHOW', 'MESH_RETMAX_CAP', 'NO_BEGIN', 'NO_END', 'NcbiError', 'RELDATE_MAX', 'RETMAX_CAP', 'RETSTART_MAX', 'SNOW_BATCH_CAP', 'SNOW_SINGLE_CAP', 'SORT_IGNORED', 'SORT_WHITELIST', 'WINDOW', '_AUDIT_UNKNOWN', '_CJK', '_DATE_RE', '_abstract_material', '_as_bool', '_as_list', '_audit', '_brief_record', '_check_date', '_check_datetype', '_check_db', '_check_field', '_check_out_path', '_check_reldate', '_check_sort', '_count_of', '_efetch_abstracts', '_efetch_all', '_efetch_xml_all', '_efetch_xml_chunk', '_esearch', '_esummary_dicts', '_expand', '_fmt_brief_record', '_fmt_compact', '_fmt_record', '_get_async', '_ids_in_window', '_ids_map', '_int', '_mask', '_note', '_parse_fields', '_parse_pmids', '_pmids_exist', '_project', '_read_pmid_file', '_recall_warnings_and_degraded', '_relaxation_plan', '_record_count_change', '_record_parts', '_retrieval_check', '_safe_json', '_scope_evidence', '_sort_note', '_term_vanished', '_utc_now', '_validate_esearch_page', '_warn_bits', '_windows', '_write_json_new', '_write_new_file', '_xml_record', '_zero_explanation', 'links')


_SEARCH_SNAPSHOT_MAX_BYTES = 32 * 1024 * 1024  # 本项目自订：快照读写共用字节上限，超限不截断
_DIAGNOSTIC_BLOCK_CAP = 6  # 本项目自订：一次诊断最多额外计数请求，不递归触发
_NEIGHBOR_ENRICH_CAP = 200  # 本项目自订：每次近邻标题/命中检查预算，其余显式标未检查
_NEIGHBOR_TEXT_CAP = 20  # 本项目自订：文本候选展示上限，完整状态用 JSON/文件查看


# 本项目自订：term 内部写成 [dp]/[edat]/[pt] 的日期或文献类型条件属于检索范围本身；
# 放宽时删掉整块会连带删掉这些条件，不能提供，也不能在提示里声称「保持日期/类型」。
_PROTECTED_FILTER_TAGS = frozenset({
    'dp', 'pdat', 'date', 'publication date', 'date - publication',
    'edat', 'entry date', 'date - entry',
    'crdt', 'create date', 'date - create',
    'dcom', 'completion date', 'date - completion',
    'lr', 'modification date', 'date - modification',
    'mhda', 'mesh date', 'date - mesh', 'epdat', 'ppdat',
    'pt', 'ptyp', 'publication type',
})
_PROTECTED_TAG_RE = re.compile(r'\[([^\[\]\n]{1,40})\](?![A-Za-z0-9])')

_MESH_TYPE_LABELS = {'descriptor': '描述符 [mh]', 'qualifier': '副主题词 [sh]',
                      'pharmacological-action': '药理作用 [pa]', 'supplemental-record': '补充概念 [nm]'}


def _protected_filter_tags(text):
    """返回块内出现的受保护标签（日期/文献类型）；引号内的文字不算标签。"""
    masked = re.sub(r'"[^"\n]*"', lambda m: 'x' * len(m[0]), str(text or ''))
    tags = {re.sub(r'\s+', ' ', m[1].partition(':')[0]).strip().lower() for m in _PROTECTED_TAG_RE.finditer(masked)}
    return tags & _PROTECTED_FILTER_TAGS


def _mesh_kind_label(record_type):
    return _MESH_TYPE_LABELS.get(record_type, '其他类型：' + (record_type or '⚠️【待确认】'))


# 本项目自订：一批题录共用来源说明，不逐 PMID 重复，不继承上一调用的检索状态。
_SEARCH_ORIGIN = {"source": "search", "comparison": "not_compared", "note": "本次检索"}
_LINKS_ORIGIN = {"source": "links", "comparison": "not_compared", "note": "追引／近邻候选"}
_EMPTY_LINK_PAGE = '本页未取编号；请给正 limit 后从同一 offset 重试。'
_TITLE_FAILURE_PREFIX = '⚠️ 缺失项标题获取失败'
_ZERO_CITATION_CAUTION = {'citedin': '0 条不等于无人引用', 'refs': '0 条不等于原文无引文'}  # 本项目口径：仅说明未录入关系


def _uid_intersection(term, batch):
    return f'({term}) AND (' + ' OR '.join(f'{p}[uid]' for p in batch) + ')'


def _window_match_counts(window, details, all_pmids):
    """Count unique candidates before sorting or truncating the text preview."""
    counts = dict(window_total=len(window), inside=0, outside=0, unknown=0, unchecked=0,
                  outside_window=len(all_pmids) - len(window))  # 本项目自订：按去重后的窗口分类，从零计数
    for pid in window:
        row = details[pid]
        if row['in_term'] is True:
            key = 'inside'
        elif row['in_term'] is False:
            key = 'outside'
        elif row['match_status'] == 'not_checked':
            key = 'unchecked'
        else:
            key = 'unknown'
        counts[key] += 1  # 本项目自订：每个候选只计一次
    return counts


def _neighbor_executions(checks):
    return [{'status': check['status'], 'querytranslation': check.get('querytranslation', ''),
             **({'error': check['error']} if 'error' in check else {})} for check in checks]


def _compact_links(page):
    """Project one displayed page. Membership and metadata come only from the full model."""
    owners = {}
    positions = {}
    sources = []
    counts = page['counts']
    need_positions = len(page['sources']) > 1 or 'candidate_details' in page  # 本项目口径：多源合并或题名/命中排序后标出原位置
    for source in page['sources']:
        pid = source['source']
        entry = {k: source[k] for k in ('source', 'source_status', 'relation_status')}
        entry['total'] = counts['per_source'][pid]
        entry['page_positions'] = source['page_positions']
        if source['error']:
            entry['error'] = source['error']
        sources.append(entry)
        for rank, target in enumerate(source['target_ids'], start=entry['page_positions']['first'] or 1):  # 本项目口径：按源的绝对位置
            owners.setdefault(target, []).append(pid)
            if need_positions:
                positions.setdefault(target, {})[pid] = rank

    rows = []
    if 'candidate_details' in page:
        for candidate in page['candidate_details']:
            row = {'pmid': candidate['pmid'], 'sources': owners[candidate['pmid']]}
            if candidate['is_seed']:
                row['is_seed'] = True
            if candidate['match_status'] != 'not_requested':
                row.update(in_term=candidate['in_term'], match_status=candidate['match_status'])
            if candidate['metadata_status'] != 'not_requested':
                row['metadata_status'] = candidate['metadata_status']
                if candidate['metadata_status'] == 'available':
                    row['languages'] = candidate['languages']
                if candidate['title']:
                    row['title'] = candidate['title']
                if candidate['pubdate']:
                    row['pubdate'] = candidate['pubdate']
            rows.append(row)
    else:
        id_key = 'pmid' if page['to'] in ('pubmed', 'refs', 'citedin') else 'id'
        seeds = set(page['pmids']) if id_key == 'pmid' else set()
        rows = [{id_key: target, 'sources': origins, **({'is_seed': True} if target in seeds else {})}
                for target, origins in owners.items()]

    if positions:
        for row in rows:
            row['source_positions'] = positions[row.get('pmid', row.get('id'))]

    result = {'schema': 'pubmed.links.compact.v1', 'to': page['to'], 'sources': sources,  # 本项目自订：精简响应格式标识
              'offset': page['pagination']['offset'], 'limit': page['pagination']['limit'],
              'targets': rows, 'warnings': list(dict.fromkeys(page['warnings']))}
    for key in ('next_offset', 'safe_per_source_limit', 'enrichment_cap', 'window_total', 'window_checked'):
        if key in page['pagination']:  # 仅在 term/with_titles 的分页预算生效时出现
            result[key] = page['pagination'][key]
    if 'provenance' in page:
        result['provenance'] = page['provenance']
    if page.get('match_scope', {}).get('term'):
        result['executions'] = _neighbor_executions(page['match_checks'])
        result['match_summary'] = page['match_summary']
        result['match_scope'] = {k: v for k, v in page['match_scope'].items() if k not in ('offset', 'limit')}
    return result


class Implementation:
    def __init__(self, dependencies):
        self.dependencies = dependencies

    def _as_bool(self, v, name):
        """布尔参数容忍字符串："True"/1/"yes"/"on" 都收，认不出来就报是哪个参数。

        〔实测 2026-11-10，本机 mcp + pydantic 2.13.4，订正旧写法〕这个函数的**存在理由**不是
        「pydantic 会拒字符串」：工具签名写的就是 `as_json: bool | str = False`，
        `list_tools()` 里那条 schema 是 `{"anyOf":[{"type":"boolean"},{"type":"string"}]}`，
        所以字符串在 pydantic 那关【根本不会被拦】，拦截发生在这一行。
        旧 docstring 写「实测 pydantic 的严格 bool 会直接回一句 expected boolean, received string」
        两处都对不上：① 本机默认是 lax 模式，`TypeAdapter(bool).validate_python("True")` 返回 True，
        不抛（只有 `strict=True` 才抛，而抛出来的话也是 "Input should be a valid boolean"，
        不是那句英文〔实测 2026-11-10〕）；② 「有 MCP 客户端把 true 发成 "True"」这个观察
        没在本机复现过〔未核实〕——它是当初写这条的理由，不是量出来的事实。"""
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("true", "1", "yes", "y", "on", ""):
                return s != ""
            if s in ("false", "0", "no", "n", "off"):
                return False
        raise self.dependencies.NcbiError(f'{name} 要布尔，收到 {v!r}（可写 true/false，也容忍 "True"/"1"/"yes"）')


    def _int(self, v, name, *, lo=None, hi=None):
        """数字参数统一收口：报错要说清是哪个参数，别抛裸 ValueError('invalid literal for int()')。
        〔1.4.5〕删掉了从未被读过的 default 参数：空串照样报错（不替用户编一个默认值），lo/hi 改为仅限关键字。"""
        if isinstance(v, bool) or not isinstance(v, (int, str)):
            raise self.dependencies.NcbiError(f"{name} 要整数，收到 {v!r}；不接受布尔或浮点数")
        try:
            n = int(v)
        except (TypeError, ValueError, OverflowError):
            raise self.dependencies.NcbiError(f"{name} 要整数，收到 {v!r}")
        if lo is not None:
            n = max(lo, n)
        if hi is not None:
            n = min(hi, n)
        return n


    def _check_sort(self, sort):
        s = (sort or "").strip()
        if not s:
            return ""
        canon = self.dependencies.SORT_WHITELIST.get(s.lower())
        if canon is None:
            raise self.dependencies.NcbiError(
                f"sort={sort!r} 不是本工具接受的排序值。实测 NCBI 对认不出的 sort 是【静默接受】的："
                f'sort="journal_quality" 返回的顺序与不传 sort 完全一致，看着像排过了其实没有。'
                f'可用值：pub_date / relevance / author。'
                f'（{"/".join(self.dependencies.SORT_IGNORED)} 这几个 NCBI 文档里出现过的值，实测顺序与不传时【逐字相同】，'
                f'所以不放进白名单，免得你以为排过了。）'
                f'（PubMed 没有「按期刊好坏排序」这个概念，所以这里不假装能排；'
                f'要那种排序得自己接外部期刊指标数据源——本仓库不含这类脚本。）')
        return canon


    def _check_date(self, v, name):
        """拦两种：写法不对（格式正则）、写法对但日历上不存在这一天。
        后者是这轮实测新发现的：mindate=2023-02-29（2023 年没有 2 月 29 日）NCBI 【不报错、
        errorlist 空、也不做词翻译】，整条查询直接 0 命中，而输出里那行「日期条件」看着完全正常。
        2020-02-29（真闰日）同参数是 413 条，所以这不是「那年没文献」，是我塞了个不存在的日子。
        同理想办法：13 月、2 月 30 日、00 月一律日历判掉；年份超出收录范围（1000/2099）NCBI 照常工作，不拦。"""
        s = str(v or "").strip()
        if not s:
            return ""
        if not self.dependencies._DATE_RE.match(s):
            raise self.dependencies.NcbiError(f"{name}={v!r} 不是 NCBI 认的日期写法（用 2020、2020/01、2020-01-01；"
                            f"要「近 N 天」请用 reldate）")
        # 〔实测 2026-11-11〕`\d` 匹配的是所有 Unicode 十进制数字：'٢٠٢٠-٠١-٠١'（阿拉伯-印度数字）
        # 能过正则，但原样下发会被 NCBI 丢掉 —— count 0、querytranslation 里 mindate 整段消失，
        # 而输出仍印着「日期条件」。这里用 int() 归一成 ASCII（int('٢٠٢٠')==2020，全角数字同理），
        # 绝不把用户的数字原样发走。
        p = [str(int(x)).zfill(2) for x in s.replace("/", "-").split("-")]
        p[0] = p[0].zfill(4)
        try:
            datetime.date(int(p[0]), int(p[1]) if len(p) > 1 else 1, int(p[2]) if len(p) > 2 else 1)
        except ValueError:
            raise self.dependencies.NcbiError(f"{name}={v!r} 格式像日期但日历上没有这一天 —— 实测这种值 NCBI 不报错、"
                            f"不返回 errorlist，整条查询静默变成 0 命中（例如 2023-02-29：2023 年没有 2 月 29 日）。"
                            f"要 2023 年就写 2023-01-01。")
        return "-".join(p)


    def _today(self):
        """本机日历的今天。〔状态 2026-11-10：日期逻辑已经不用它了〕
        reldate 改为原样下发、单边日期改为补哨兵（NO_BEGIN/NO_END）之后，本工具不再需要本机日历；
        留着是因为「本机日历 ≠ NCBI 服务器日历」这件事本身就是一次实测记录（见 _windows 的 docstring），
        而且这里不是 git 仓库，删掉就找不回来了。
        ⚠️ 别把它再拿来当日期边界用：〔实测 2026-11-09〕本机比服务器领先一天时，
        用它当上界会静默切掉边界那一天的文献（dental implant 770 → 763，少的 7 条＝那一天）。"""
        return datetime.date.today().strftime("%Y-%m-%d")


    def _check_reldate(self, v):
        """reldate 只收 0 或 1..RELDATE_MAX。旧写法 `_int(reldate,"reldate",0,0)` 上界不管、
        下界被夹成 0，两条都是静默的：〔实测 2026-11-09〕
          reldate=1000000000 → datetime 抛裸 OverflowError（不是 NcbiError，而且请求已经发出去了才炸）；
          reldate=-30        → 夹成 0 ＝不限日期，313615 条照回，一个字都没提醒。"""
        n = self.dependencies._int(v, "reldate")
        if n < 0:
            raise self.dependencies.NcbiError(f"reldate={v!r} 是负数。旧版会把它静默夹成 0（＝完全不限日期），"
                            f"所以这里直接拦：要「最近 N 天」给正数，要「某年以前」请用 maxdate。")
        if n > self.dependencies.RELDATE_MAX:
            raise self.dependencies.NcbiError(f"reldate={n} 太大（上限 {self.dependencies.RELDATE_MAX} 天 ≈ {self.dependencies.RELDATE_MAX // 365} 年）。"
                            f"旧写法这种值会在算日期时抛 OverflowError；要「某某年以来」请直接用 mindate。")
        return n


    def _check_datetype(self, value):
        if value not in ("pdat", "edat"):
            raise self.dependencies.NcbiError("datetype 只支持 pdat（发表日期）或 edat（入库日期）")
        return value


    def _validate_esearch_page(self, d, retstart, retmax):
        """按实际请求校验响应；分页回显可省略，但不能代替请求参数。"""
        if not isinstance(d, dict):
            raise self.dependencies.NcbiError("esearch 缺少对象形式的 esearchresult")
        count = self.dependencies._count_of(d)
        expected = min(retmax, max(0, count - retstart), max(0, self.dependencies.WINDOW - retstart))  # 本项目自订：请求窗口校验口径
        # 兼容旧计数/零命中夹具省略 idlist；显式 null 或非空窗口缺字段仍报错。
        ids = d.get("idlist", [] if expected == 0 else None)  # 本项目自订：仅空窗口允许省略
        if not isinstance(ids, list):
            raise self.dependencies.NcbiError("esearch idlist 必须是 PMID 字符串列表")
        if any(not isinstance(pid, str) or re.fullmatch(r"[1-9][0-9]*", pid) is None for pid in ids):  # 本项目自订：规范 PMID 字符串校验规则
            raise self.dependencies.NcbiError("esearch idlist 包含非规范 PMID（须为无前导零的正整数 ASCII 字符串）")
        if len(set(ids)) != len(ids):
            raise self.dependencies.NcbiError("esearch idlist 包含重复 PMID")
        if len(ids) != expected:
            raise self.dependencies.NcbiError(f"esearch idlist 与请求窗口/count 不一致：retstart={retstart}，"
                            f"retmax={retmax}，count={count}，预期 {expected} 个，实际 {len(ids)} 个")
        # retmax 可回显请求上限或实际页长；越尾时实际页长为空。
        for key, allowed in (("retstart", {retstart}), ("retmax", {retmax, expected})):
            if key not in d:
                continue
            value = d[key]
            if (isinstance(value, bool) or not isinstance(value, (str, int))
                    or re.fullmatch(r"[0-9]+", str(value)) is None):  # 本项目自订：非负 ASCII 整数回显
                raise self.dependencies.NcbiError(f"esearch {key} 回显不是非负整数")
            try:
                echoed = int(value)
            except ValueError as exc:
                raise self.dependencies.NcbiError(f"esearch {key} 回显无法解析") from exc
            if echoed not in allowed:
                raise self.dependencies.NcbiError(f"esearch {key} 回显与请求窗口不一致：{value!r}")
        if "querytranslation" in d and not isinstance(d["querytranslation"], str):
            raise self.dependencies.NcbiError("esearch querytranslation 不是字符串")
        return d


    async def _esearch(self, term, retmax=20, sort="", field="", mindate="", maxdate="", reldate=0, retstart=0, datetype="pdat"):  # 原文/来源：现有 _esearch 参数默认值保持不变
        datetype = self.dependencies._check_datetype(datetype)
        date_label = "发表日期" if datetype == "pdat" else "入库日期"
        if not (term or "").strip():
            raise self.dependencies.NcbiError("term 不能为空（空 term 会被 NCBI 当特殊输入处理，count 字段直接消失）")
        f = self.dependencies._check_field(field)
        srt = self.dependencies._check_sort(sort)
        try:
            _rs_req = int(str(retstart).strip())
        except (TypeError, ValueError):
            _rs_req = None                      # 不是数字就交给 _int 报人话
        if _rs_req is not None and _rs_req < 0:
            # 〔实测 2026-11-11〕旧写法 `_int(retstart, ..., 0)` 把负数夹成 0＝第一页，输出只印
            # retstart=0，调用方以为拿到了 -5 起始的窗口。与上面 retmax 负数同口径：直接报。
            raise self.dependencies.NcbiError(f"retstart={retstart!r} 是负数，本工具不替它编一个意思（旧版会静默夹成 0＝第一页）。"
                            f"要第一页就写 0；翻页请给非负整数，上限 {self.dependencies.RETSTART_MAX}。")
        rs = self.dependencies._int(retstart, "retstart", lo=0)
        if rs > self.dependencies.RETSTART_MAX:
            raise self.dependencies.NcbiError(f"retstart={rs} 超出 PubMed esearch 的检索窗口。〔实测 2026-11-09〕"
                            f"retstart=9998 还取得到东西，9999 和 10000 都被 NCBI 拒绝，原文是 "
                            f"「'retstart' cannot be larger than 9998 … ESearch can only retrieve the "
                            f"first 9,999 records」。esearch 最多只能取回前 9999 条，"
                            f"要覆盖更多请按年份或字段把查询切小、分批取。")
        # 〔实测 2026-11-10〕旧写法把两件事都静默办了：
        #   `retmax=5000` 被夹到 1000，输出只写「本次给 1000 条」——看着像 NCBI 只给了这么多；
        #   `retmax=-3` 被 lo=0/1 夹掉，等于替用户把负数解释成「就要 0/1 条」。
        #   负数没有合理含义，直接报；超上限还夹（这是 NCBI 的硬墙），但必须在输出里说是本工具夹的。
        try:
            _rm_req = int(str(retmax).strip())
        except (TypeError, ValueError):
            _rm_req = None                      # 不是数字就交给 _int 报人话
        if _rm_req is not None and _rm_req < 0:
            raise self.dependencies.NcbiError(f"retmax={retmax!r} 是负数，本工具不替它编一个意思。"
                            f"要几条就写正数（上限 {self.dependencies.RETMAX_CAP}）；只想知道命中数就用 count()。")
        _rm = self.dependencies._int(retmax, "retmax", lo=0, hi=self.dependencies.RETMAX_CAP)
        _rm_note = ("" if _rm_req is None or _rm_req <= self.dependencies.RETMAX_CAP else
                    f"retmax={_rm_req} 超过本工具上限，已夹到 {_rm}。"
                    f"〔实测 2026-11-10〕上限 {self.dependencies.RETMAX_CAP} 是【本工具自订】的（NCBI 那边 esearch 允许更大），"
                    f"要一次多拿就把 {self.dependencies.RETMAX_CAP} 当参数改，或者按年份/字段切小查询分批取。")
        q = {"db": "pubmed", "term": term, "retmax": _rm,
             "retmode": "json", "retstart": rs}
        if srt:
            q["sort"] = srt
        if f:
            q["field"] = f
        # 〔实测 2026-11-08〕旧版写 `if mindate and maxdate:`，只给一端时不下发日期。我原以为是这里的锅，
        # 改成照单发之后数还是没变，直接打 eutils 裸参数才看清真相：
        #   不限                      → 795
        #   mindate=2020-01-01（带 datetype=pdat）→ 795   ← NCBI 自己忽略只给一端的 mindate
        #   maxdate=2020-12-31                    → 795   ← 只给一端同理
        #   mindate+maxdate 成对                   → 419 / 47
        #   reldate=600                            → 126   ← 这个能单独生效
        # 所以"只给一端"根本不可能生效，必须替使用者把另一端补成一个真边界，并把补了什么写进输出。
        lo, hi = self.dependencies._check_date(mindate, "mindate"), self.dependencies._check_date(maxdate, "maxdate")
        if lo and hi and self.dependencies._expand(hi, True) < self.dependencies._expand(lo):
            raise self.dependencies.NcbiError(f"maxdate={maxdate!r} 的末尾早于 mindate={mindate!r}，这个区间是空的")
        filled = []
        rel = self.dependencies._check_reldate(reldate)
        # reldate 一律【原样下发】给 NCBI，本工具不做日历换算。理由〔实测 2026-11-09〕：
        #   原来的交集分支把 NCBI 实际执行的 2026-09-09:2026-11-08 换成本机日历的 2026-09-10:2026-11-09，
        #   于是 "dental implant" 的 reldate=60 单给 = 770 条，再配一个根本不该改变结果的
        #   mindate=2020-01-01 就变成 763 条 —— 少的 7 条正好是 2026-09-09 那一天的量（我单独量过：7 条），
        #   上界那天 2026-11-09 是 0 条所以看不出来。同一个词、同一个参数，因为多给一个无关边界就换数字。
        #   原样下发后 reldate=60 + 2020-01-01:2999-12-31 → 770，与单给 reldate 逐字相同。
        # 保留的两条实测事实：
        #   ① reldate 与 mindate/maxdate 同时给 = NCBI 做【两段 AND】（qt 里会回显两段），不是取交集显示；
        #   ② reldate 随 datetype 换字段：不带 datetype 按 [Date - Entry]（365 天 → 82 条），
        #      配 datetype=pdat 按 [Date - Publication]（同段 → 86 条），所以固定带 pdat。
        # 空交集不再由本机预判（本机预判本身就是这个 bug 的来源），改成事后从 qt 的窗口里判，见 _note。
        if rel:
            q.update(reldate=rel, datetype=datetype)
        if lo or hi:
            if lo and not hi:
                hi = self.dependencies.NO_END
                filled.append(f"maxdate 由本工具补成 {self.dependencies.NO_END}＝不限上界（补成「今天」会出事：本机日历与 "
                              f"NCBI 服务器日历差一天时，边界日那一天的文献被静默切掉）")
            if hi and not lo:
                lo = self.dependencies.NO_BEGIN
                filled.append(f"mindate 由本工具补成 {self.dependencies.NO_BEGIN}＝不限下界（旧版补 1809-01-01 是错的："
                              f"实测 PubMed 里确有早于 1809 的题录，例如 1790 年发表的文章）")
            # 〔实测 2026-11-10〕补完哨兵还得再比一次：只给 `mindate=3000-01-01` 时，
            #   上界被本工具补成 NO_END=2999-12-31 → 区间倒挂成 3000-01-01 → 2999-12-31，
            #   NCBI 对这种倒挂区间不报错、【静默回 0 条】，而 search 末尾会写
            #   「PubMed 里确实没有匹配这篇」——那是拿本工具自己造的空区间当结论。
            # 〔实测 2026-11-10 第 5 轮探针抓到〕这里【不能】逐字比字符串：
            #   mindate="2021-01-01" + maxdate="2021" 时 "2021-01-01" > "2021" 为真，
            #   于是把一对完全合法的参数拦成了「倒挂」——NCBI 自己把 2021 摊成 2021-12-31。
            #   本文件 467 行早就用 _expand() 处理过这件事，我加守卫时没用它。
            #   下界按【当年 1 月 1 日】摊开、上界按【当年 12 月 31 日】摊开，再比。
            if self.dependencies._expand(lo) > self.dependencies._expand(hi, True):
                raise self.dependencies.NcbiError(
                    f"mindate={lo}（摊开 {lo}→{self.dependencies._expand(lo)}）晚于本工具补出来的上界 {hi}"
                    f"（摊开 {self.dependencies._expand(hi, True)}，NO_END 哨兵），区间是倒挂的。"
                    f"〔实测 2026-11-10〕这种区间 NCBI 不报错、静默回 0 条，"
                    f"那个 0 条既不是「主题没文献」也不是「NCBI 有这个界」，是参数本身没意义。"
                    f"确实要查未来年份请连 maxdate 一起给（比如 maxdate=3050-01-01，"
                    f"但注意 PubMed 的收录不会到那一年）。")
            q.update(mindate=lo, maxdate=hi, datetype=datetype)
        payload = self.dependencies._safe_json(await self.dependencies._get_async("esearch.fcgi", q, 25), "esearch")  # 原文/来源：现有 _esearch 请求超时保持不变
        page = payload.get('esearchresult')
        try:
            d = self.dependencies._validate_esearch_page(page, rs, _rm)
        except self.dependencies.NcbiError as exc:
            exc.rejected_page = ({k: page[k] for k in ('count', 'idlist', 'querytranslation') if k in page}
                                 if isinstance(page, dict) else {})
            raise
        d.setdefault('retstart', str(rs))  # 无上游回显时沿用已校验的实际请求偏移，供快照、文本和审计使用。
        d["_term"] = term
        d["_request_params"] = {k: v for k, v in q.items() if k not in ("api_key", "email")}
        # 〔实测 2026-11-11〕留给 _zero_explanation 判断「用户是不是只要计数」；**不能**改读 NCBI
        # 回显的 retmax（越页时回显会变成 "0"）。完整实测与原因只写在 _zero_explanation 那一支里，
        # 避免同一段证据两处各写一遍。这个字段只给解释用，不改下发参数。
        d["_retmax"] = _rm
        d["_retmax_note"] = _rm_note
        d["_sort_note"] = self.dependencies._sort_note(sort, srt)
        d["_field_applied"] = f
        d["_rel"] = rel
        wins = self.dependencies._windows(d.get("querytranslation"))
        d["_windows"] = wins
        segs = "；".join(f"{a} → {b}（{lab}）" for a, b, lab in wins)
        if lo or hi or rel:
            bounds = (self.dependencies._expand(lo), self.dependencies._expand(hi, True)) if lo else None
            reasons, _ = self.dependencies._scope_evidence(term, d.get('querytranslation') or '', '', bounds, rel, datetype)
            requested = ([f'{lo} → {hi}'] if bounds else []) + ([f'近 {rel} 天（reldate）'] if rel else [])
            parts = ['请求：' + ' 与 '.join(requested) + f'（datetype={datetype}＝{date_label}）', *filled]
            if reasons:
                parts.append('请求日期范围未核实：' + '；'.join(reasons))
            else:
                parts.append('回显核实：' + ('两段 AND' if bounds and rel else '请求的日期限制是必需条件'))
            if rel:
                parts.append('回显中出现的窗口：' + (segs or 'NCBI 没回显窗口'))
            d['_daterange'] = '；'.join(parts)
        else:
            d["_daterange"] = ("未给日期区间或相对天数：datetype=edat 未用于日期筛选" if datetype == "edat" else "")
        return d


    def _sort_note(self, sort, canon):
        """用户写的 sort 字面值与实际下发的值不一致时，返回一句必须打印的话；否则返回空串。

        为什么单独提：〔实测 2026-11-10〕`sort="best match"` 传上去 NCBI 是【忽略】的（顺序与不传 sort
        逐字相同），而本工具映射成的 `relevance` 是另一种真实顺序。两者给出的是不同的前 5 条，
        所以「我把你当 best match 处理成了 relevance」必须写清楚，不能让用户以为拿到了 PubMed 网页
        那个 Best Match。（PubMed 网页的 Best Match 在 esearch 的 sort 参数里没有对应值——
        〔本项目口径：从实测推的，NCBI 没有一份逐字的对照说明，别拿这句去论证 relevance 就等于 Best Match〕）"""
        raw = (sort or "").strip().lower()
        if not raw or not canon or raw == canon:
            return ""
        return (f'sort={sort!r} 由本工具改写成 sort={canon!r} 下发。〔实测 2026-11-10〕'
                f'字面 "{raw}" 传上去 NCBI 是【忽略】的（顺序与不传 sort 逐字相同），'
                f'而 {canon!r} 是另一种真实顺序 —— 你拿到的是后者。')


    def _check_field(self, field):
        f = (field or "").strip()
        if not f:
            return ""
        canon = self.dependencies.FIELD_WHITELIST.get(f.lower())
        if canon in self.dependencies.FIELD_SWALLOWED:
            raise self.dependencies.NcbiError(f'field="{field}" 会被 NCBI 整条忽略（实测 field=keywords / pmcid / issn 三种'
                            f'给的 count 与 querytranslation 与【不传 field】逐字相同，等于没限定）。'
                            f'要按这些维度查，请把标签写进 term，例如 term=\'"Cone Beam Computed Tomography"[MeSH Terms]\'、'
                            f'或按刊名查用 [journal]；白名单里其余值是真生效的。')
        if canon in self.dependencies.FIELD_NO_COUNT:
            raise self.dependencies.NcbiError(f'field="{field}"（={canon}）实测会让 esearch 不返回 count，本工具没法给出命中数；'
                            f'请把标签写进 term，如 term=\'miniscrew[MeSH Terms]\'（那样 count 正常返回）。')
        if canon is None:
            raise self.dependencies.NcbiError(
                f"field={field!r} 不在白名单里。NCBI 对认不出的 field 是【静默忽略】的"
                f"（实测 field=ZZZBOGUSFIELD 的命中数与不传 field 完全一致，⚠️ 也不报），"
                f"所以这里直接拦掉。可用值："
                + ", ".join(sorted(set(self.dependencies.FIELD_WHITELIST.values()) - self.dependencies.FIELD_SWALLOWED - self.dependencies.FIELD_NO_COUNT)))
        return canon


    def _count_of(self, d):
        """count 缺失或不是数字就抛错。实测 field=MeSH Terms / Publication Type、空 term 三种情况
        都会让 esearch 不返回 count —— 旧版把它印成「命中 ? 条」，等于把无意义结果放进数字槽。"""
        c = d.get("count") if isinstance(d, dict) else None
        if (isinstance(c, bool) or not isinstance(c, (str, int))
                or re.fullmatch(r"[0-9]+", str(c)) is None):  # 本项目自订：非负 ASCII 整数计数校验
            raise self.dependencies.NcbiError("esearch 没返回可用的 count；NCBI 原文：" + json.dumps(d, ensure_ascii=False)[:300])  # 原文/来源：现有 _count_of 诊断截断长度
        try:
            return int(c)
        except ValueError as exc:
            raise self.dependencies.NcbiError("esearch count 无法解析") from exc


    def _parse_pmids(self, pmids):
        """接受 逗号/分号/空格/换行 分隔，容忍 "PMID 12345"、"pmid: 12345"；返回 (规范 id 列表, 原始个数)。

        两条实测教训（2026-11-08，都是把这条路径修对的依据）：
          - 旧版先按空白切分、再拿 `(?i:pmid)?[:#.]?(\\d+)` 去匹配，于是 "PMID 12345" 被切成
            ["PMID","12345"]，"PMID" 匹配不上 → 抛错，而那句错文又让调用方「写成 PMID 12345」，
            自己打自己〔实测：`_parse_pmids('PMID 12345')` 旧版报「这些不是 PMID…或写成 PMID 12345」〕。
            从 PubMed 网页复制粘贴出来就是这个形状，所以必须先把前缀摘掉再切分。
          - 补零的号 NCBI 认、正文也照常返回，但 efetch 里的 `PMID:` 行是不补零的
            〔实测：`fetch_abstracts('000037427694')` 同一屏出现「实取 1 条」和「NCBI 没有返回…000037427694」〕。
            所以这里统一 `str(int(...))` 规范化，比对时两边都是裸号。
        旧版 `p.lstrip('PM').isdigit()` 那类凑数写法会把混进来的非数字静默丢掉 —— 那正是
        「要 3 条实取 2 条」这类谎的源头，别改回去。
        """
        # 仅识别官方文章 URL；不访问链接，也不从任意 URL 猜编号。
        def article_id(match):
            suffix = match.group(2) or ""
            if any(separator in suffix for separator in ";,，、"):
                raise self.dependencies.NcbiError(
                    "PubMed URL 的查询参数或片段含名单分隔符，无法判断是否另有 PMID；"
                    "请去掉 URL 的 ?/# 后缀，或将参数中的分隔符编码，再用空格／换行分隔链接与编号。")
            return match.group(1)

        s = re.sub(
            r"(?<![^\s;,，、])https?://pubmed\.ncbi\.nlm\.nih\.gov/([0-9]+)/?([?#][^\s]*)?(?=$|[\s;,，、])",
            article_id, pmids or "", flags=re.I)
        s = re.sub(r"(?i)\bpmids?\b\s*[:#]?\s*", " ", s)
        # 〔实测 2026-11-10 抓到〕旧写法直接按逗号切，'1,234,567' 变成 ['1','234','567'] ——
        #   三个都是【真实存在】的 PMID，于是「从 Excel 复制一列带千位分隔符的号」
        #   会安静地抓到三篇不相干的文献，而且条数看起来完全正常。
        # 这里不猜：这种形状既可能是「一个号写成了 1,234,567」也可能是「1 和 234 和 567 三个号」，
        #   两种读法给出不同文献，替用户选就是本工具一直在禁止的那种事（把猜测写成事实），所以直接拦。
        # 〔实测 2026-11-11〕旧守卫与实际切分用的不是同一套分隔符：'1，234，567'（全角逗号）与
        #   '1,234,567,'（行尾逗号）都能绕过，直接被切成 1/234/567 三个【真实存在】的 PMID。
        #   现在整串（去掉首尾分隔符）与逐个 token 都按同一套判据看一遍，全角逗号也按千位分隔符处理。
        whole = s.strip().strip(";，、, \t\r\n")
        amb = [whole] if re.fullmatch(r"\d{1,3}(?:[,，]\d{3})+", whole) else []
        # 〔实测 2026-11-13，1.4.4〕'1,234, 567' 切出 token '1,234,'——行尾逗号让 fullmatch 落空，被当成 1/234/567
        #   三个号放行。逐 token 判之前先剥掉首尾 ASCII 逗号。
        amb += [t.strip(",") for t in re.split(r"[;，、\s]+", s)
                if t and re.fullmatch(r"\d{1,3}(?:,\d{3})+", t.strip(","))]
        if amb:
            raise self.dependencies.NcbiError(
                f"这些串看着像【带千位分隔符的数字】，没法判断是一个号还是好几个号：{amb[:3]}。"
                f"〔实测 2026-11-10〕旧版会把 '1,234,567' 切成 1 / 234 / 567 三个【真实存在】的 PMID，"
                f"于是抓回三篇不相干的文献，条数还看着正常。"
                f"两种意思请分开写：一个号就去掉逗号写 1234567；好几个号就用空格或分号隔开"
                f"（如 '12345 678 900'），别用逗号紧跟三位数字的写法。")
        toks = [t for t in re.split(r"[;,，、\s]+", s) if t]
        if not toks:
            raise self.dependencies.NcbiError("pmids 为空")
        ids, bad = [], []
        for t in toks:
            m = re.fullmatch(r"[:#.，、]?(\d+)", t.strip())
            if m:
                digits = m.group(1)
                try:
                    normalized = str(int(digits))
                    if normalized == "0":  # 本项目口径：PMID 仅接受正整数
                        bad.append(t)
                    else:
                        ids.append(normalized)
                except ValueError:
                    # 〔实测 2026-11-11，Python 3.13〕4301 位纯数字在 int() 处抛裸 ValueError
                    # （3.11+ 的 4300 位上限），不是 NcbiError；走 mcp 2.x 时原文会被剥成一句
                    # 「Error executing tool ...」。这里换成能读懂的报错。
                    raise self.dependencies.NcbiError(f"这一串有 {len(digits)} 位数字，超出 Python 的整数解析上限（4300 位）；"
                                    f"PMID 不会有这么长，多半是粘错了内容，请核对后再试。")
            else:
                bad.append(t)
        if bad:
            raise self.dependencies.NcbiError(f"这些不是 PMID（要纯数字，也可写成 PMID 12345）：{bad[:5]}"
                            + ("；PMC 号不能当 PMID 用，先转成 PMID" if any(b.upper().startswith("PMC") for b in bad) else ""))
        seen, uniq = set(), []
        for p in ids:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq, len(ids)


    async def _esummary_dicts(self, ids, *, include_unavailable=False):
        """分批取 esummary，返回 [(uid, 记录), ...]。
        uid 一律用 NCBI 自己给的 result.uids，不拿入参顺序去 zip —— 顺序不保证一致。"""
        ids = list(ids)
        chunks = [ids[i:i + self.dependencies.ESUMMARY_CHUNK] for i in range(0, len(ids), self.dependencies.ESUMMARY_CHUNK)]
        out = [None] * len(chunks)

        async def one(i, c):
            try:
                d = self.dependencies._safe_json(await self.dependencies._get_async("esummary.fcgi",
                                                {"db": "pubmed", "id": ",".join(c), "retmode": "json"}, 25),
                               "esummary")
                res = d.get("result")
                if not isinstance(res, dict) or not isinstance(res.get("uids"), list):
                    raise self.dependencies.NcbiError("esummary 缺少合法的 result/uids，无法判断记录是否存在；请重试")
                uids = res["uids"]
                if any(not isinstance(u, str) or u not in c for u in uids) or len(set(uids)) != len(uids):
                    raise self.dependencies.NcbiError("esummary 返回的 PMID 列表与请求不符，不能作为有效元数据")
                records = []
                for uid in uids:
                    rec = res.get(uid)
                    if rec is None:
                        continue    # 单条缺失保持未知，不凭空造记录
                    if not isinstance(rec, dict):
                        raise self.dependencies.NcbiError(f"esummary 的 PMID {uid} 元数据结构异常")
                    if "uid" in rec and rec["uid"] != uid:
                        raise self.dependencies.NcbiError(f"esummary 的 PMID {uid} 与记录内部 uid 不一致，不能作为有效元数据")
                    if rec.get("error") and rec["error"] != "cannot get document summary":
                        raise self.dependencies.NcbiError(self.dependencies._mask(f"esummary 的 PMID {uid} 请求失败：{rec['error']}"))
                    if include_unavailable or (not rec.get("error") and rec.get("title")):
                        records.append((uid, rec))
                out[i] = records
            except self.dependencies.NcbiError as e:
                errs.append(e)      # 不让它出组：ExceptionGroup 的 str() 里没有 NCBI 原文
                return

        errs = []
        async with anyio.create_task_group() as tg:
            for i, c in enumerate(chunks):
                tg.start_soon(one, i, c)
        if errs:
            # 〔实测 2026-11-08〕旧版不接，任何一片失败调用方只看到
            # 「unhandled errors in a TaskGroup (1 sub-exception)」——NCBI 那句原文被吞，
            # 而文件头承诺「请求失败会把 NCBI 原文带回来」。宁可整条报错，也不返回缺片的表。
            raise errs[0]
        # esummary 的返回顺序不保证等于 esearch 排序；按 UID 关联后恢复请求顺序。
        by_id = {uid: rec for part in out if part for uid, rec in part}
        return [(uid, by_id[uid]) for uid in ids if uid in by_id]


    def _ids_map(self, r):
        return {a.get("idtype"): a.get("value") for a in (r.get("articleids") or []) if isinstance(a, dict)}


    def _record_parts(self, uid, r):
        """DOI / PMC 一律从 articleids 取。
        旧版先遍历 elocationid 找 {'doctype':'doi'} —— 实测这个键是字符串（"doi: 10.1259/..."），
        isinstance(el, dict) 永假，那段是死代码，删。"""
        ids = self.dependencies._ids_map(r)
        src = r.get("source") or r.get("fulljournalname") or "?"
        vol, issue, pages = r.get("volume") or "", r.get("issue") or "", r.get("pages") or ""
        cite = ""
        if vol:
            cite = f" {vol}" + (f"({issue})" if issue else "") + (f":{pages}" if pages else "")
        elif pages:
            cite = f" {pages}"    # 〔1.4.5〕无卷号时旧版拼成 ':123-30'，冒号前没有东西
        # authorlist 兜底到 authors 是必需的：实测 esummary 对 37427694 不返回 authorlist，
        # 作者全走 authors；重构时别把 `or r.get("authors")` 当冗余删掉。
        auth = r.get("authorlist") or r.get("authors") or []
        names = ", ".join((a.get("name") or "").strip() for a in auth[:3] if isinstance(a, dict))
        total = len(auth)
        # authors 只放名字，不粘「，共 N 人」：as_json 的卖点是「给脚本用」，粘在最后一个作者名后面
        # 会让脚本切出假名字、数逗号数错人数。人数由 author_count 单独给；文本输出的人话后缀在
        # _fmt_record 里加（2026-11-11 改，旧版的 authors 字段两种输出共用同一段拼串）。
        ptypes = [p for p in self.dependencies._as_list(r.get("pubtype") or []) if isinstance(p, str)]
        # 不截掉撤稿、勘误或试验类型。〔1.4.5〕ptag 不再带展示分隔符「｜」：旧版把它漏进 as_json（'｜Review'），
        # 分隔符改由 _fmt_record 拼文本时加。
        ptag = "/".join(p for p in ptypes if p != "Journal Article")
        return {
            "pmid": uid,
            "pubdate": r.get("pubdate") or "?",
            "epubdate": r.get("epubdate") or "",
            "journal": src,
            "citation": cite.strip(),
            "title": r.get("title") or "(无标题)",
            "authors": names,
            "author_count": total,
            "doi": ids.get("doi") or "",
            "pmc": ids.get("pmc") or "",
            "pubtype": ptypes,
            "ptag": ptag,
        }


    def _brief_record(self, uid, r):
        """初筛字段；文献类型完整保留，省略字段不代表来源缺失。

        字段契约保持四字段；仅当刊期年与 Epub 年都已知且不同时，额外附可选 epubdate。
        同年或任一未知时仍是四字段，旧快照/调用方不受影响。"""
        p = self.dependencies._record_parts(uid, r)
        out = {k: p[k] for k in ("pmid", "pubdate", "title", "pubtype")}
        if self._epub_cross_year(p):
            out["epubdate"] = p["epubdate"]
        return out


    @staticmethod
    def _epub_cross_year(p):
        """刊期年与 Epub 年都已知且不同时为真；任一缺失不推断。"""
        yp = re.match(r"\s*(\d{4})\b", str(p.get("pubdate") or ""))
        ye = re.match(r"\s*(\d{4})\b", str(p.get("epubdate") or ""))
        return bool(yp and ye and yp.group(1) != ye.group(1))


    @staticmethod
    def _record_date_text(p):
        """题录日期文本：刊期年与 Epub 年不同时并列，其它情况只给刊期。

        只从能解析出四位年份的字符串判断；任一缺失就不并列，也不补造「同年/跨年」。
        〔工具输出锚点 SRC-MH〕同一 PMID 限 2007–2009[pdat] 却显示 2010 Mar，
        原排版另有 Epub 2009 Nov 3 —— 只按列表年份手删会删掉真实命中。
        字段契约不变：这里只改文本头部，不动 pubdate/epubdate 的 JSON 投影。"""
        pub = str(p.get("pubdate") or "")
        epub = str(p.get("epubdate") or "")
        if Implementation._epub_cross_year(p):
            return f"{pub}｜Epub {epub}"
        return pub


    def _fmt_brief_record(self, r, uid):
        # 文本 brief 另取 epubdate 判跨年；JSON brief 走 _brief_record（跨年时自带可选 epubdate）。
        p = self.dependencies._record_parts(uid, r)
        types = "/".join(p["pubtype"])
        return f"PMID {p['pmid']}｜{self._record_date_text(p)}" + (f"｜{types}" if types else "") + f"\n  {p['title']}"


    def _fmt_record(self, r, uid):
        p = self.dependencies._record_parts(uid, r)
        head = f"PMID {p['pmid']}｜{self._record_date_text(p)}｜{p['journal']}{(' ' + p['citation']) if p['citation'] else ''}{('｜' + p['ptag']) if p['ptag'] else ''}"
        tail = []
        if p["doi"]:
            tail.append("doi:" + p["doi"])
        if p["pmc"]:
            tail.append("pmc:" + p["pmc"])
        auth_txt = p["authors"] or "(无作者信息)"
        if p["authors"] and p["author_count"] > 3:
            auth_txt += f"，共 {p['author_count']} 人"    # 旧版「等 N 人」会被读成「另外 N 人」
        return (f"{head}\n  {p['title']}\n  {auth_txt}"
                + (("｜" + "｜".join(tail)) if tail else ""))


    async def _efetch_abstracts(self, pmids):
        timeout = min(60, 20 + 0.4 * len(pmids))
        text = await self.dependencies._get_async("efetch.fcgi", {"db": "pubmed", "id": ",".join(pmids),
                                                "rettype": "abstract", "retmode": "text"}, timeout)
        # 〔实测 2026-11-11〕efetch 是全文唯一的文本端点，没有 _safe_json 那道 200+HTML 守卫：
        # 把 _get_async 换成返回 '<!DOCTYPE html><html>…' 后，旧版把整段 HTML 当摘要拼进正文并报
        # 「实取 0 条」——那是故障页，不是「这些 PMID 没有摘要」。README 行为契约第 1 条要求它算失败。
        head = text.lstrip()[:200].lower()
        if head.startswith("<!doctype") or head.startswith("<html"):
            raise self.dependencies.NcbiError("efetch 回的是 HTML 页面（HTTP 200 也可能是故障页/网关页），不是摘要正文 —— "
                            "这【不是】「这些 PMID 没有摘要」。原文开头：" + self.dependencies._mask(text.lstrip()[:200]))
        got = re.findall(r"^PMID: (\d+)", text, re.M)
        if len(got) != len(set(got)) or not set(got).issubset(pmids):
            raise self.dependencies.NcbiError("efetch 返回的 PMID 与请求不符或重复，不能作为有效摘要")
        # 无效号的空占位形状已在线核对；其它无 PMID 正文不能解释为文献不存在。
        if not got and not re.fullmatch(r"\s*(?:\d+\.\s*)+", text):
            raise self.dependencies.NcbiError("efetch 返回了无法识别的正文，无法判断记录是否存在；原文开头：" +
                            self.dependencies._mask(text.lstrip()[:200]))
        return text


    async def _efetch_all(self, pmids):
        """分批并发取正文，同时按 efetch 文本里的 ^PMID: 行统计到底取回了哪几条。"""
        chunks = [pmids[i:i + self.dependencies.EFETCH_CHUNK] for i in range(0, len(pmids), self.dependencies.EFETCH_CHUNK)]
        out = [None] * len(chunks)

        async def one(i, c):
            try:
                out[i] = await self.dependencies._efetch_abstracts(c)
            except self.dependencies.NcbiError as e:
                errs.append(e)     # 出组会被包成 ExceptionGroup，原文丢失（见 _esummary_dicts 同处注释）
                return

        errs = []
        async with anyio.create_task_group() as tg:
            for i, c in enumerate(chunks):
                tg.start_soon(one, i, c)
        if errs:
            raise errs[0]
        # 全是无效号的批只回「1. 」占位〔实测 2026-11-13：9999999998,9999999999 → '1. '〕，旧版把它拼进正文成了碎片。
        text = "\n\n".join(p for p in out if p and re.search(r"^PMID: \d+", p, re.M))
        got = re.findall(r"^PMID: (\d+)", text, re.M)     # NCBI 排版每条末尾都有这行（实测）
        return text, got


    def _parse_fields(self, fields):
        raw = [f.strip().lower() for f in str(fields or "").replace("，", ",").split(",")]
        raw = [f for f in raw if f]
        if not raw:
            return list(self.dependencies.ABSTRACT_DEFAULT_FIELDS)
        if raw == ["all"]:
            return list(self.dependencies.ABSTRACT_FIELDS)
        bad = [f for f in raw if f not in self.dependencies.ABSTRACT_FIELDS]
        if bad:
            raise self.dependencies.NcbiError(f"fields 不认识：{', '.join(bad)}。可用：{', '.join(self.dependencies.ABSTRACT_FIELDS)}，或 all；"
                            f"不传则为 {','.join(self.dependencies.ABSTRACT_DEFAULT_FIELDS)}。")
        return list(dict.fromkeys(raw))


    async def _efetch_xml_chunk(self, pmids):
        import xml.etree.ElementTree as ET
        timeout = min(60, 20 + 0.4 * len(pmids))
        text = await self.dependencies._get_async("efetch.fcgi", {"db": "pubmed", "id": ",".join(pmids),
                                                "rettype": "xml", "retmode": "xml"}, timeout)
        head = text.lstrip()[:200].lower()
        if head.startswith("<!doctype html") or head.startswith("<html"):
            raise self.dependencies.NcbiError("efetch 回的是 HTML 页面（HTTP 200 也可能是故障页/网关页），不是 XML 记录 —— "
                            "这【不是】「这些 PMID 没有摘要」。原文开头：" + self.dependencies._mask(text.lstrip()[:200]))
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            raise self.dependencies.NcbiError(f"efetch XML 无法解析（{e}），不能作为有效摘要；原文开头：" + self.dependencies._mask(text.lstrip()[:200])) from e
        if root.tag != "PubmedArticleSet":
            raise self.dependencies.NcbiError(f"efetch XML 根元素是 <{root.tag}> 而不是 PubmedArticleSet；原文开头：" +
                            self.dependencies._mask(text.lstrip()[:200]))
        recs = []
        for node in root:
            if node.tag in ("PubmedArticle", "PubmedBookArticle"):
                try:
                    recs.append(self.dependencies._xml_record(node))
                except AttributeError as e:
                    raise self.dependencies.NcbiError(f"efetch XML 记录缺少必需结构（{node.tag}），无法解析：{e}") from e
            elif node.tag != "DeleteCitation":
                raise self.dependencies.NcbiError(f"efetch XML 出现未识别的记录类型 <{node.tag}>，不冒充为已解析")
        got = [r["pmid"] for r in recs]
        if len(got) != len(set(got)) or not set(got).issubset(pmids):
            raise self.dependencies.NcbiError("efetch 返回的 PMID 与请求不符或重复，不能作为有效摘要")
        return recs


    async def _efetch_xml_all(self, pmids):
        chunks = [pmids[i:i + self.dependencies.EFETCH_CHUNK] for i in range(0, len(pmids), self.dependencies.EFETCH_CHUNK)]
        out = [None] * len(chunks)
        errs = []

        async def one(i, c):
            try:
                out[i] = await self.dependencies._efetch_xml_chunk(c)
            except self.dependencies.NcbiError as e:
                errs.append(e)

        async with anyio.create_task_group() as tg:
            for i, c in enumerate(chunks):
                tg.start_soon(one, i, c)
        if errs:
            raise errs[0]
        by_id = {r["pmid"]: r for part in out for r in (part or [])}
        return [by_id[p] for p in pmids if p in by_id]


    @staticmethod
    def _display_fingerprint(record):
        return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    def _read_search_snapshot(self, path):
        """Explicit local comparison input; no hidden session or query exclusion."""
        p = os.path.abspath(os.path.expanduser(path))
        try:
            with open(p, "rb") as stream:
                raw = stream.read(_SEARCH_SNAPSHOT_MAX_BYTES + 1)  # 本项目自订：多读一个字节检测超限
            if len(raw) > _SEARCH_SNAPSHOT_MAX_BYTES:
                raise ValueError("快照过大，请开启新的展示批次")
            doc = json.loads(raw.decode("utf-8"))
            if not isinstance(doc, dict) or doc.get("schema") != "pubmed.search.snapshot.v1":  # 本项目自订：快照协议
                raise ValueError("只接受 search(out_path=...) 生成的快照")
            history = doc.get("display_history")
            if not isinstance(history, dict) or len(history) > self.dependencies.EXPORT_MAX:
                raise ValueError("展示记录无效或过大")
            for pid, entry in history.items():
                if (not re.fullmatch(r"[1-9][0-9]*", pid) or not isinstance(entry, dict)
                        or set(entry) != {"fingerprint", "detail"}
                        or not isinstance(entry["fingerprint"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", entry["fingerprint"])
                        or entry["detail"] not in ("brief", "full", "ids")):
                    raise ValueError("展示记录字段无效")
            if not isinstance(doc.get("result"), dict) or not isinstance(doc["result"].get("records"), list):
                raise ValueError("快照缺少当前页题录")
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            raise self.dependencies.NcbiError(f"无法读取检索快照：{p}：{exc}") from exc
        return history, {"path": p, "sha256": hashlib.sha256(raw).hexdigest()}

    async def _save_search_snapshot(self, data, pairs, detail, outp, previous, max_show):
        """Keep the entire requested page; limit only repeat presentation, never retrieval."""
        history, previous_binding = previous
        records = ([{"pmid": pid} for pid in data["search_pmids"]] if detail == "ids" else
                   [self.dependencies._record_parts(pid, record) for pid, record in pairs])
        warnings = list(data["warnings"])
        if detail != "ids":
            warnings.extend(self._integrity_warnings([
                {"pmid": r["pmid"], "pt": r["pubtype"]} for r in records]))
        data = {**data, "warnings": list(dict.fromkeys(warnings))}
        eligible, unchanged, fingerprints, refreshed = [], [], {}, set()
        for record in records:
            pid = record["pmid"]
            fingerprint = self._display_fingerprint(record)
            fingerprints[pid] = fingerprint
            old = history.get(pid)
            enough_detail = old and (old["detail"] == detail or (old["detail"] == "full" and detail == "brief"))
            if old and (detail == "ids" or (enough_detail and old["fingerprint"] == fingerprint)):
                unchanged.append(pid)
            else:
                eligible.append(pid)
                # A changed citation can differ only in fields omitted by brief.
                # Present that full version before treating its fingerprint as shown.
                if old and old["detail"] != "ids" and detail != "ids" and old["fingerprint"] != fingerprint:
                    refreshed.add(pid)
        shown = eligible[:max_show]
        deferred = eligible[max_show:]
        refreshed_shown = [pid for pid in shown if pid in refreshed]
        updated = dict(history)
        for pid in shown:
            updated[pid] = {"fingerprint": fingerprints[pid], "detail": "full" if pid in refreshed else detail}
        if len(updated) > self.dependencies.EXPORT_MAX:
            raise self.dependencies.NcbiError("展示记录达到上限，请不带 previous_path 开启新批次；不自动丢弃历史记录")
        display = {"displayed_pmids": shown, "unchanged_pmids": unchanged, "deferred_pmids": deferred}
        snapshot = {
            "schema": "pubmed.search.snapshot.v1",  # 本项目自订：当前请求页快照，不是全量基线
            "source_type": "工具输出/当前请求页；计数为工具记录，展示限制与协议为本项目口径",
            "scope": "requested_page", "saved_at": self.dependencies._utc_now(),
            "source_note": "保存本页全部标准化题录及查询信息；不是 NCBI 原始响应，不含摘要；保存或展示均不等于已筛选。",
            "result": {**data, "records": records}, "requested_detail": detail,
            "stored_detail": "ids" if detail == "ids" else "full",
            "previous": previous_binding, "display": display, "display_history": updated,
        }
        body = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"  # 本项目自订：缩进便于局部读取
        if len(body.encode("utf-8")) > _SEARCH_SNAPSHOT_MAX_BYTES:
            raise self.dependencies.NcbiError(
                "快照超过读写大小上限，未写入文件；请减小 retmax 分页获取，"
                "或不带 previous_path 开启新批次。max_show 不会减小保存的完整页；不截断题录或丢弃历史。")
        p, size, sha = await anyio.to_thread.run_sync(self.dependencies._write_new_file, outp, body)
        shown_set = set(shown)
        full_by_id = {r["pmid"]: r for r in records}
        projected = [full_by_id[r["pmid"]] if r["pmid"] in refreshed else r
                     for r in data["records"] if r["pmid"] in shown_set]
        # The full page retains aliases and translationset; the compact reply avoids duplicating them.
        summary = {k: v for k, v in data.items() if k not in
                   ("records", "search_pmids", "translationset", "dropped_phrases", "deprecated_fields")}
        summary.update(path=p, bytes=size, sha256=sha, scope="requested_page",
                       displayed=len(shown), unchanged_count=len(unchanged), deferred_count=len(deferred),
                       deferred_pmids=deferred, refreshed_pmids=refreshed_shown, records=projected,
                       display_note=(f"总命中 {data['count']} 条，本页已保存 {len(records)} 条"
                                     f"{' PMID' if detail == 'ids' else '题录'}，本次展示 {len(shown)} 条，"
                                     f"另有 {len(deferred)} 条待展示。"
                                     + (f"此前展示且未变 {len(unchanged)} 条，不重复展示。" if unchanged else "")
                                     + "保存或展示不等于已筛选。"))
        self.dependencies._audit("search", path=p, bytes=size, sha256=sha, previous=previous_binding,
                                 displayed=len(shown), unchanged_count=len(unchanged), deferred_count=len(deferred),
                                 warnings=data["warnings"])
        return json.dumps(summary, ensure_ascii=False)

    def _missing_citations(self, pmids):
        """Keep every ID; offer a valid first fetch even when the page exceeds its limit."""
        batch = pmids[:self.dependencies.FETCH_MAX]
        args = {'pmids': ' '.join(batch), 'fields': 'title,abstract', 'as_json': True}
        hint = '补取题名/摘要：fetch_abstracts(' + ', '.join(f'{k}={v!r}' for k, v in args.items()) + ')'
        if len(pmids) > len(batch):
            hint += f'；剩余 {len(pmids) - len(batch)} 个按相同方式分批补取'
        return {'tool': 'fetch_abstracts', 'arguments': args}, hint

    @staticmethod
    def _query_heading(term, d):
        """文本只回显实际执行式一次且不截断；原输入仍在调用参数、JSON 和审计中。"""
        executed = str(d.get('querytranslation') or '').strip()
        return 'NCBI 实际执行：' + executed if executed else '输入查询（执行式未返回）：' + term

    def _append_zero_note(self, lines, d, count):
        explanation = self.dependencies._zero_explanation(d, count)
        if '⚠️ ' + explanation not in lines:
            lines.append('=> ' + explanation)

    def _diagnostic_options(self, diagnose, low_hit_threshold):
        if diagnose not in ('auto', 'always', 'off'):
            raise self.dependencies.NcbiError('diagnose 只支持 auto / always / off')
        threshold = self.dependencies._int(low_hit_threshold, 'low_hit_threshold')
        if threshold < 0:  # 本项目自订：阈值非负，不静默修正
            raise self.dependencies.NcbiError('low_hit_threshold 不能为负数')
        return threshold

    async def _low_hit_help(self, term, d, diagnose, threshold, scope, *, first_page=True):
        """诊断附属于原始结果；子查询故障不冒充零命中，不改原查询，也不选最大增幅。

        低命中状态要按【实际执行】的字段/日期范围判断：父查询与每个子查询都用
        _check_execution_scope（_review_scope 同口径归一）核对执行式，范围无法核实或解析失真时
        不把计数当低命中证据，delta 一律留空。term 内部写成 [dp]/[edat]/[pt] 的日期或文献类型
        条件属于检索范围，删除会改变范围，不列入可移除块。"""
        total = self.dependencies._count_of(d)
        scope = self._normalized_scope(scope)
        active = diagnose == 'always' or (diagnose == 'auto' and total <= threshold and first_page)
        report = {'status': 'off' if diagnose == 'off' else 'not_triggered', 'rows': [],
                  'scope': scope, 'warnings': [], 'request_budget': _DIAGNOSTIC_BLOCK_CAP,
                  'evidence': '来源：本次工具计数；不证明相关性、漏检原因或研究空白'}
        lines = []
        plans, reason = self.dependencies._relaxation_plan(term) if active else ([], '')
        protected = []
        if active and plans:
            kept = []
            for plan in plans:
                tags = _protected_filter_tags(plan['removed_block'])
                if tags:
                    protected.append(plan['removed_block'])
                else:
                    kept.append(plan)
            plans = kept
            if protected:
                report['protected_blocks'] = list(dict.fromkeys(protected))
        if active:
            _, degraded = self.dependencies._recall_warnings_and_degraded(term, [d])
            scope_errors, parent_relative = self._check_execution_scope(term, d, scope)
            if degraded or scope_errors:
                report['status'] = 'unreliable_query'
                report['warnings'].extend('⚠️ ' + message for message in scope_errors)
                lines.append('诊断暂缓：原查询存在解析警告或字段/日期范围无法核实，请先修正检索式；'
                             '不把无法核实的低命中当成原式计数。')
            elif not plans:
                report['status'] = 'unsupported'
                if protected:
                    lines.append('诊断未执行：可拆分的块都含日期或文献类型限制；删除会改变检索范围，不提供该放宽。')
                else:
                    lines.append('诊断未执行：' + reason)
            else:
                report['status'] = 'complete' if len(plans) <= _DIAGNOSTIC_BLOCK_CAP else 'partial'
                if protected:
                    lines.append('受保护块（含日期/文献类型限制，删除会改变范围，不列入放宽）：'
                                 + '；'.join(dict.fromkeys(protected)))
                if len(plans) > _DIAGNOSTIC_BLOCK_CAP:
                    report['warnings'].append(f'诊断预算仅检查原顺序前 {_DIAGNOSTIC_BLOCK_CAP} 个可移除块；其余未检查。')
                lines.append('逐块放宽（其余字段、日期和排除条件保持不变；按原顺序，不按增幅推荐删词）：')
                for plan in plans[:_DIAGNOSTIC_BLOCK_CAP]:
                    row = {**plan, 'count': None, 'delta': None, 'status': 'unknown'}
                    try:
                        child = await self.dependencies._esearch(plan['term'], retmax=0, **scope)
                        observed = self.dependencies._count_of(child)
                        warnings, degraded = self.dependencies._recall_warnings_and_degraded(plan['term'], [child])
                        child_errors, child_relative = self._check_execution_scope(plan['term'], child, scope)
                        if parent_relative and child_relative and parent_relative != child_relative:
                            child_errors.append('原查询与放宽查询的实际相对日期窗口不一致')
                        reliable = not degraded and not child_errors
                        row.update(count=observed, status='ok' if reliable else 'unreliable_query',
                                   delta=observed - total if reliable else None,
                                   querytranslation=child.get('querytranslation') or '',
                                   request_params=child.get('_request_params') or {},
                                   warnings=warnings + ['⚠️ ' + message for message in child_errors])
                        suffix = '' if reliable else '（解析或范围警告，不能按原式比较）'
                        lines.append(f"  去掉 {plan['removed_block']} → {observed} 条{suffix}")
                        if not reliable:
                            report['status'] = 'partial'
                            lines.extend(warnings)
                            lines.extend('⚠️ ' + message for message in child_errors)
                        else:
                            lines.extend(note for note in warnings if note.startswith('⚠️'))
                    except self.dependencies.NcbiError as exc:
                        row['error'] = self.dependencies._mask(str(exc))
                        report['status'] = 'partial'
                        lines.append(f"  去掉 {plan['removed_block']} → 无法判断：{row['error']}")
                    report['rows'].append(row)
                    if plan['method_only']:
                        lines.append('  方法统称提示：若方法不是研究问题本身，可考虑放宽此块并阅读候选；'
                                     '若研究的就是该方法，请保留。未触发此提示不代表方法词已覆盖充分。')
                lines.append('这些计数只说明条件限制程度，不证明哪个块有错；用 diff 比较选定的原式与放宽式，并保留相同限制。')
            lines.extend(report['warnings'])
        if first_page and total <= threshold and (diagnose != 'off' or total == 0):  # 本项目自订：轻量 count 正命中保持原输出
            if not active:
                lines.append('下一步：diagnose="auto" 逐块试查，不改原式。')
            lines.append('复核：已知集用 check_recall；近邻用 links(to="pubmed", term=当前式, '
                         'with_titles=True)，沿用字段/日期限制，读摘要确认相关性。')
            if not os.environ.get('PUBMED_AUDIT_DIR', '').strip():
                lines.append('查新见 literature-search；可设置 PUBMED_AUDIT_DIR 留痕（当前未启用）。')
        if active or lines:
            self.dependencies._audit('query_diagnostics', term=term, diagnostics=report, next_steps=lines)
        return report, lines

    async def search(self, term: str, retmax: StrictInt | StrictStr = 20, sort: str = "", field: str = "",
                     mindate: str = "", maxdate: str = "", reldate: StrictInt | StrictStr = 0,
                     retstart: StrictInt | StrictStr = 0,
                     as_json: bool | str = False,
                     detail: Literal["full", "brief", "ids"] = "brief", datetype: Literal["pdat", "edat"] = "pdat",
                     out_path: str = "", previous_path: str = "", max_show: StrictInt | StrictStr = 20,  # 本项目自订：保留展示默认值
                     diagnose: Literal["auto", "always", "off"] = "auto",
                     low_hit_threshold: StrictInt | StrictStr = 10) -> str:  # 本项目自订：低命中阈值
        """PubMed 检索；计数用 count。英文词、短语[tiab]、大写 AND/OR/NOT、[MeSH Terms]/[Publication Type]。
        field: Title、Title/Abstract、Journal、Author、Affiliation、Language、Grant、PMID。
        sort: pub_date/relevance/author（非质量）；datetype: pdat发表/edat入库；日期年/月/日，reldate近天数与区间取交集。
        retmax≤1000，retstart零起≤9998，限前9999条〔工具口径〕；空页非取全。
        brief题名/日期/全部类型；full加引用；ids不取元数据/撤稿。JSON含执行式、分页、缺失号。
        少看用retmax；仅out_path可设max_show/previous_path，跳过已展示未变者；deferred_pmids未读。
        丢词/改写计数非原式；零条非无文献；命中非短语存在；省略非缺失，PMC非可下载。
        diagnose: auto首页低命中逐块计数，always强制，off不加请求；增幅非相关性。
        本页不代表取全；是否仅新式命中尚未比较。同范围比 diff，已知集 check_recall，近邻 links，查新见 literature-search。
        """
        # 〔实测 2026-11-10〕这里【不能】给 _int 传 lo：传了就把负数当场夹平，
        #    _esearch 里那句「retmax 是负数，本工具不替它编一个意思」根本轮不到（-3 变 0、静默出结果）。
        #   上限同理留给 _esearch，那里既夹也说明。
        if detail not in ("full", "brief", "ids"):
            raise self.dependencies.NcbiError("detail 只支持 full / brief / ids")
        threshold = self._diagnostic_options(diagnose, low_hit_threshold)
        json_mode = self.dependencies._as_bool(as_json, "as_json")    # 非法模式在联网前报错
        outp = self.dependencies._check_out_path(out_path)
        show = self.dependencies._int(max_show, "max_show")
        if not 0 <= show <= self.dependencies.RETMAX_CAP:  # 本项目自订：展示范围与单页上限一致
            raise self.dependencies.NcbiError("max_show 超出展示范围")
        if not outp and (previous_path or show != 20):  # 本项目自订：不落盘时维持旧返回契约
            raise self.dependencies.NcbiError("previous_path / max_show 需要 out_path；只想少看几条，请去掉这两个参数，改用 retmax 限制本页条数。")
        previous = await anyio.to_thread.run_sync(self._read_search_snapshot, previous_path) if previous_path else ({}, None)
        json_mode = json_mode or bool(outp)
        d = await self.dependencies._esearch(term, self.dependencies._int(retmax, "retmax"), sort, field, mindate, maxdate, reldate, retstart, datetype=datetype)
        ids = d.get("idlist") or []
        count = self.dependencies._count_of(d)
        diagnostics, guidance = await self._low_hit_help(
            term, d, diagnose, threshold,
            {'field': field, 'mindate': mindate, 'maxdate': maxdate, 'reldate': reldate, 'datetype': datetype},
            first_page=self.dependencies._int(retstart, 'retstart') == 0)  # 本项目自订：首页才自动触发
        # 〔实测 2026-11-09 更正〕检索窗口的真实边界（旧注释有一条不复现，跟着它写的分支也就走不到）：
        #   retstart=9990 retmax=10 → 只回 9 条；retstart=9998 retmax=2 → 只回 1 条；
        #   retstart=9999 与 10000 → NCBI 直接拒绝（「'retstart' cannot be larger than 9998 …
        #   can only retrieve the first 9,999 records」）。旧注释写的是「retstart=9999 → 只回 0 条」，
        #   这句不成立，所以越界值现在由 _esearch 提前拦下并给人话，而不是在这里解释。
        win = int(d.get("retstart") or 0) + len(ids)
        win_warn = (f"⚠️ 到这一页已经贴着 PubMed 的检索窗口上限：esearch 只能取回【前 9999 条】"
                    f"（实测 retstart=9998 还取得到、9999 起直接被拒），这条查询共 {count} 条，"
                    f"第 9999 条以后的【永远翻不到】。别把「这一页短」读成「已取回全部」"
                    f"——要覆盖全量请按年份或字段把查询切小、分批取。"
                    if count > 9999 and win >= 9999 else "")
        brief_note = '说明：brief 省略作者、期刊、卷页、DOI/PMC；省略不表示来源缺失，全部文献类型保留。'
        mode_note = ('⚠️ ids 仅返回 esearch 的 PMID，未获取元数据、未检查撤稿等文献类型；返回数按 PMID 计。'
                     if detail == 'ids' else '')
        applied_sort = (d.get("_request_params") or {}).get("sort") or "default"
        page_end = int(d.get("retstart") or 0) + len(ids)
        has_more = page_end < count
        next_start = page_end if ids and has_more and page_end <= self.dependencies.RETSTART_MAX else None
        audit_params = {
            "input_params": {"field": field, "sort": sort, "mindate": mindate, "maxdate": maxdate,
                             "reldate": reldate, "retmax": retmax, "retstart": retstart, "as_json": as_json},
            "request_params": d.get("_request_params") or {},
            "field": d.get("_field_applied") or field or "",
            "sort": sort or "",
            "mindate": mindate or "",
            "maxdate": maxdate or "",
            "reldate": d.get("_rel") if "_rel" in d else self.dependencies._int(reldate, "reldate"),
            "retmax": d.get("_retmax") if "_retmax" in d else self.dependencies._int(retmax, "retmax"),
        }
        if json_mode:
            try:
                pairs = await self.dependencies._esummary_dicts(ids) if (ids and detail != "ids") else []
            except self.dependencies.NcbiError as e:
                self.dependencies._audit("search", term=term, count=count, returned=0, detail=detail,
                       retstart=int(d.get("retstart") or 0), **audit_params,
                       querytranslation=(d.get("querytranslation") or "").strip(),
                       warnings=self.dependencies._note(d, include_translation=False) + [f"⚠️ esummary 元数据获取失败：{type(e).__name__}"],
                       error="esummary_failed")
                raise
            # 〔实测 2026-11-09〕旧版的 as_json 分支把「窗口到顶」⚠️ 和 0 条解释整个丢掉了，
            # 而 docstring 又让脚本改用 as_json —— 走 JSON 的人恰恰拿不到「这批没取全」这句最需要的话。
            # 现在两支都进 JSON（warnings 里也放一份，脚本只看 warnings 也不会漏）。
            # 完整 querytranslation 已是独立字段，不在 warnings 再复制同一长串。
            warn = self.dependencies._note(d, include_translation=False) + guidance
            warn += [win_warn] if win_warn else []
            if detail == 'brief' and pairs:
                warn.append(brief_note)
            elif mode_note:
                warn.append(mode_note)
            returned_ids = {uid for uid, _ in pairs}
            missing_pmids = [pid for pid in ids if pid not in returned_ids] if detail != "ids" else []
            if missing_pmids:
                recovery, hint = self._missing_citations(missing_pmids)
                warn.append('⚠️ esummary 本次未返回可展示题录。题录缺失 PMID：' + ', '.join(missing_pmids) + '。' + hint)
            self.dependencies._audit("search", term=term, count=count,
                   returned=len(ids) if detail == "ids" else len(pairs), detail=detail,
                   retstart=int(d.get("retstart") or 0), **audit_params,
                   querytranslation=(d.get("querytranslation") or "").strip(), warnings=warn)
            result = {
                "term": term, "querytranslation": (d.get("querytranslation") or "").strip(),
                "translationset": d.get("translationset") or [],
                "field": d.get("_field_applied", ""),
                "count": count, "retstart": int(d.get("retstart") or 0),
                "sort": applied_sort, "has_more": has_more, "next_retstart": next_start,
                # 末页与「还有命中但检索窗口翻不到」是两件事，文本和 JSON 分开说。
                "end_page": bool(ids) and not has_more,
                "window_exceeded": bool(ids) and has_more and next_start is None,
                "provenance": _SEARCH_ORIGIN,
                "search_pmids": ids,
                "page_pmid_count": len(ids),
                "page_citation_count": None if detail == 'ids' else len(pairs),
                "missing_pmids": missing_pmids,
                **({'metadata_recovery': recovery} if missing_pmids else {}),
                "returned": len(ids) if detail == "ids" else len(pairs),
                "esummary_missing": None if detail == "ids" else max(0, len(ids) - len(pairs)),
                **({"detail": detail, "metadata_requested": bool(ids) and detail != "ids"} if detail != "full" else {}),
                "phrases_not_found": self.dependencies._as_list((d.get("errorlist") or {}).get("phrasesnotfound") or []),
                # 兼容旧调用方保留别名；原「已丢弃」解释已作废，见 deprecated_fields。
                "dropped_phrases": self.dependencies._as_list((d.get("errorlist") or {}).get("phrasesnotfound") or []),
                "deprecated_fields": {"dropped_phrases": "弃用别名，请读 phrases_not_found；不表示这些词已被丢弃"},
                "daterange": d.get("_daterange") or "",
                "executed_windows": d.get("_windows") or [],
                "window_warning": win_warn,
                "zero_note": ("=> " + self.dependencies._zero_explanation(d, count)) if not ids else "",
                "records": ([{"pmid": uid} for uid in ids] if detail == "ids" else
                            [(self.dependencies._brief_record if detail == "brief" else self.dependencies._record_parts)(uid, r) for uid, r in pairs]),
                "warnings": warn,
                "diagnostics": diagnostics,
                "next_steps": guidance,
            }
            if outp:
                return await self._save_search_snapshot(result, pairs, detail, outp, previous, show)
            return json.dumps(result, ensure_ascii=False)
        head = [self._query_heading(term, d), f"本次检索命中 {count} 条，本页取得 {len(ids)} 个 PMID（retstart={d.get('retstart')}）",
                f"排序：{applied_sort}" + ("（NCBI 默认顺序，未指定排序）" if applied_sort == "default" else "")]
        if next_start is not None:
            head.append(f"下一页 retstart={next_start}；本次仅返回当前页，未筛完全部命中。")
        elif ids and has_more:
            # 还有命中，但没有下一页：只可能是撞上 9999 条检索窗口（retstart 上限 9998）。
            if not win_warn:
                head.append("⚠️ 本次不是末页：已到 PubMed 检索窗口末端，剩余命中无法用 retstart 继续翻取；"
                            "请按年份或字段拆分查询。")
        elif ids:
            head.append(f"已到末页：本页末个 PMID 是检索结果的第 {page_end} 个；翻到末页不等于已读完全部题录。")
        notes = self.dependencies._note(d, include_translation=False) + guidance
        head += notes
        if win_warn:
            head.append(win_warn)
        if mode_note:
            head.append(mode_note)
        base_warn = notes + ([win_warn] if win_warn else []) + ([mode_note] if mode_note else [])
        if not ids:
            self.dependencies._audit("search", term=term, count=count, returned=0, detail=detail,
                   retstart=int(d.get("retstart") or 0), **audit_params,
                   querytranslation=(d.get("querytranslation") or "").strip(), warnings=base_warn)
            self._append_zero_note(head, d, count)
            return "\n".join(head)
        if detail == "ids":
            self.dependencies._audit("search", term=term, count=count, returned=len(ids), detail=detail,
                   retstart=int(d.get("retstart") or 0), **audit_params,
                   querytranslation=(d.get("querytranslation") or "").strip(), warnings=base_warn)
            return "\n".join(head) + "\n\n" + "\n".join(ids)
        try:
            pairs = await self.dependencies._esummary_dicts(ids)
        except self.dependencies.NcbiError as e:
            self.dependencies._audit("search", term=term, count=count, returned=0, detail=detail,
                   retstart=int(d.get("retstart") or 0), **audit_params,
                   querytranslation=(d.get("querytranslation") or "").strip(),
                   warnings=base_warn + [f"⚠️ esummary 元数据获取失败：{type(e).__name__}"],
                   error="esummary_failed")
            raise
        if detail == 'brief' and pairs:
            head.append(brief_note)
            base_warn.append(brief_note)
        returned_ids = {uid for uid, _ in pairs}
        missing_pmids = [pid for pid in ids if pid not in returned_ids]
        if missing_pmids:
            _, hint = self._missing_citations(missing_pmids)
            miss_msg = '⚠️ esummary 本次未返回可展示题录。题录缺失 PMID：' + ', '.join(missing_pmids) + '。' + hint
            head.append(miss_msg)
            base_warn.append(miss_msg)
        self.dependencies._audit("search", term=term, count=count, returned=len(pairs), detail=detail,
               retstart=int(d.get("retstart") or 0), **audit_params,
               querytranslation=(d.get("querytranslation") or "").strip(), warnings=base_warn)
        head[1] += f"；展示 {len(pairs)} 条题录"
        head.append("")
        formatter = self.dependencies._fmt_brief_record if detail == "brief" else self.dependencies._fmt_record
        return "\n".join(head) + "\n" + "\n".join(formatter(r, uid) for uid, r in pairs)


    async def count(self, term: str, field: str = "", mindate: str = "", maxdate: str = "",
                    reldate: StrictInt | StrictStr = 0, datetype: Literal["pdat", "edat"] = "pdat",  # 本项目自订：保留不限相对日期默认值
                    diagnose: Literal["auto", "always", "off"] = "off",
                    low_hit_threshold: StrictInt | StrictStr = 10) -> str:  # 本项目自订：低命中阈值
        """只取命中数；需题录直接 search。英文词、大写 AND/OR/NOT；字段/日期同 search，pdat发表/edat入库。
        返回实际执行式及警告；丢词/改写计数非原式，零条非无文献，命中非短语存在（须读题录/摘要）。完整解析用 search(retmax=0,as_json=True)。
        diagnose默认off；auto/always逐块计数，保留字段/日期/排除条件，增幅非相关性。
        查新见 literature-search；已知集 check_recall，近邻 links，查询对比 diff。
        """
        threshold = self._diagnostic_options(diagnose, low_hit_threshold)
        d = await self.dependencies._esearch(term, retmax=0, field=field, mindate=mindate, maxdate=maxdate, reldate=reldate, datetype=datetype)
        n = self.dependencies._count_of(d)
        _, guidance = await self._low_hit_help(term, d, diagnose, threshold,
            {'field': field, 'mindate': mindate, 'maxdate': maxdate, 'reldate': reldate, 'datetype': datetype})
        notes = self.dependencies._note(d, include_translation=False) + guidance
        self.dependencies._audit("count", term=term, count=n,
               input_params={"field": field, "mindate": mindate, "maxdate": maxdate, "reldate": reldate},
               request_params=d.get("_request_params") or {},
               field=d.get("_field_applied") or field or "",
               mindate=mindate or "", maxdate=maxdate or "",
               reldate=d.get("_rel") if "_rel" in d else self.dependencies._int(reldate, "reldate"),
               querytranslation=(d.get("querytranslation") or "").strip(), warnings=notes)
        # 0 条与 search 同口径给出「是哪一种 0」（1.4.1 补；旧版 count 只印执行式，0 的语义要靠调用方猜）。
        lines = [self._query_heading(term, d), f"命中 {n} 条", *notes]
        if n == 0:
            self._append_zero_note(lines, d, n)
        return "\n".join(lines)


    @staticmethod
    def _integrity_warnings(records):
        """字段裁剪不能隐去来源已报告的撤稿、关注声明或勘误关联。"""
        notices = {"RetractionIn", "RetractionOf", "ExpressionOfConcernIn", "ExpressionOfConcernFor", "ErratumIn", "ErratumFor"}
        types = {"Retracted Publication", "Retraction of Publication", "Expression of Concern", "Published Erratum"}
        warnings = []
        for rec in records:
            flags = list(dict.fromkeys(
                [p for p in rec.get("pt", []) if p in types]
                + [c["type"] + (" PMID " + c["pmid"] if c.get("pmid") else "")
                   for c in rec.get("comments", []) if c.get("type") in notices]))
            if flags:
                warnings.append(f"⚠️ PMID {rec['pmid']} 来源标记：{'；'.join(flags)}。请核对关联声明；勘误不等于撤稿。")
        return warnings


    async def fetch_abstracts(self, pmids: str, fields: str = "", as_json: bool | str = False,
                              out_path: str = "") -> str:
        """按逗号/空格分隔的 PMID 取 NCBI 原排版摘要；保留结构化段落、作者信息和声明。

        也接受官方 PubMed 文章 URL（仅提取编号，不访问链接）；自动去重、归一补零号、分批。先筛题录再取所需摘要。
        返回请求数、实际记录数和缺失号；有记录不等于有摘要，社论可能只有引文。
        网络失败报错，不冒充无记录。文献数值引用仍须核对原文位置。
        fields/as_json 改走 XML 精简（无作者单位）：title,abstract,authors,journal,pubdate,ids,pt,lang,
        mesh,comments,coi,other_abstracts 或 all。精简字段仍保留撤稿/关注声明/勘误 warnings；未见标记不证明无问题。
        abstract_requested 区分未请求摘要；no_abstract 指已返回记录缺摘要。out_path 写新文件，回路径、哈希及上述质量摘要。
        """
        uniq, n_in = self.dependencies._parse_pmids(pmids)
        if len(uniq) > self.dependencies.FETCH_MAX:
            # 〔1.4.5〕旧版无上限：5000 号＝100 批，全部正文留在内存里，超长提示要等拼完才出。
            raise self.dependencies.NcbiError(f"fetch_abstracts 一次最多 {self.dependencies.FETCH_MAX} 个 PMID（本项目自订上限），收到 {len(uniq)} 个"
                            f"（约 {(len(uniq) + self.dependencies.EFETCH_CHUNK - 1) // self.dependencies.EFETCH_CHUNK} 批）。请分次取；只要题录可用 "
                            f'export(format="ris" 或 "medline") 落盘。')
        json_mode = self.dependencies._as_bool(as_json, "as_json")
        structured = bool(json_mode or str(fields or "").strip())
        flds = self.dependencies._parse_fields(fields) if structured else []
        outp = self.dependencies._check_out_path(out_path)
        batches = (len(uniq) + self.dependencies.EFETCH_CHUNK - 1) // self.dependencies.EFETCH_CHUNK
        if structured:
            recs = await self.dependencies._efetch_xml_all(uniq)
            got = [r["pmid"] for r in recs]
        else:
            text, got = await self.dependencies._efetch_all(uniq)
        missing = [p for p in uniq if p not in set(got)]
        head = [f"要 {len(uniq)} 条 / 实取 {len(got)} 条（分 {batches} 批）"]
        if not structured:
            head.append('实取为记录数，不是摘要数。原排版未逐篇核验有无摘要；要逐篇标明缺摘要，用 fields="title,abstract"。')
        if n_in != len(uniq):
            head.append(f"（原始输入 {n_in} 个，去掉重复后 {len(uniq)} 个）")
        if missing:
            # 「或该文本来就没有摘要」这半句是假的，删掉：missing 是按 efetch 正文里有没有 ^PMID: 行算的，
            # 而没摘要的社论照样回记录、照样算实取〔实测 PMID 42759866：要 1 条 / 实取 1 条，无 ⚠️〕。
            # 走到这里只剩一种解释：这些号 NCBI 没给回任何记录（号不存在、已撤销，或 db 里查不到）。
            where = "efetch XML 里没有对应的记录" if structured else "efetch 正文里没有对应的 PMID 行"
            head.append(f"⚠️ NCBI 没返回这些号的记录（{where}）——"
                        "注意【不是】「这篇没有摘要」：实测没有摘要的社论照样返回记录、照样算实取。"
                        "这里点名的一律是取不到条目本身：" + ", ".join(missing))
        self.dependencies._audit("fetch_abstracts", pmids=uniq, requested=len(uniq), returned=len(got), missing=missing,
               **({"fields": flds, "as_json": json_mode} if structured else {}),
               **({"out_path": outp} if outp else {}))
        quality = {}
        if structured:
            statuses = [(r['pmid'], self.dependencies._abstract_material(r)[0]) for r in recs] if 'abstract' in flds else []
            no_ab = [p for p, status in statuses if status == 'not_provided']
            other_only = [p for p, status in statuses if status == 'other_abstract_only']
            integrity_warnings = self._integrity_warnings(recs)
            quality = {"abstract_requested": "abstract" in flds, "no_abstract": no_ab, "other_abstract_only": other_only,
                       "warnings": integrity_warnings}
            if json_mode:
                body = json.dumps({"requested": len(uniq), "returned": len(got), "missing": missing,
                                   "batches": batches, "fields": flds, **quality,
                                   "records": [self.dependencies._project(r, flds) for r in recs],
                                   "source_note": "efetch XML 解析；内联格式（上下标/斜体）已去标签，数值引用须核对原文"},
                                  ensure_ascii=False)
            else:
                body = "\n".join(head + integrity_warnings) + "\n\n" + "\n\n".join(self.dependencies._fmt_compact(r, flds) for r in recs)
        else:
            body = "\n".join(head) + ("\n\n" + text if text else "")
        if outp:
            p, nbytes, sha = await anyio.to_thread.run_sync(self.dependencies._write_new_file, outp, body)
            self.dependencies._audit("fetch_abstracts", path=p, bytes=nbytes, sha256=sha)
            summary = {"path": p, "bytes": nbytes, "sha256": sha, "format": "json" if json_mode else "text",
                       "requested": len(uniq), "returned": len(got), "missing": missing, **quality}
            if json_mode:
                return json.dumps(summary, ensure_ascii=False)
            return "\n".join(head + quality.get("warnings", []) + [f"已写入：{p}（UTF-8，{summary['format']}）｜{nbytes} 字节｜sha256 {sha}",
                                     "正文未回传；需要时请读该文件。"])
        if not json_mode and len(body) > self.dependencies.FETCH_WARN_CHARS:
            # 纯新增提示行（不改正文）：大结果常被客户端整条拒收，模型一条摘要也看不到。
            body += (f"\n\n⚠️ 本次输出约 {len(body)} 字符，可能超出客户端单次结果上限（上限未核实，本项目自订提示阈值 "
                     f"{self.dependencies.FETCH_WARN_CHARS}）。如被拒收：改用 out_path 写文件，或用 fields=title,abstract 精简，或分批取。")
        return body


    def _check_db(self, to):
        """〔实测 2026-11-08〕旧版把 to 原样塞进 db 参数、又用 linkname.endswith(to) 分类，大小写一改就出错：
          to='pmc' → PMID 37427694 给 pubmed_pmc=PMC10461258
          to='PMC' → 同一条 PMID 给「(PMC 库无直接对应记录)」—— 把「有全文」说成了「没全文」
          to='pmcid' → NCBI 回 Invalid db name，但异常被 task group 包成 ExceptionGroup，原文被吞
        这个工具存在的理由就是回答「有没有免费全文可下」，所以宁可拦下来，也不给假阴性。"""
        s = (to or "").strip().lower()
        # 〔已作废 2026-11-11，本会话实测推翻〕这句报错原先写「只认库名，不认 'PMC'/'PMCID' 这类写法」。
        # 但下一行本来就 .lower()，实测 links(pmids="37427694", to="PMC") 走的是成功分支，返回
        # 「pubmed_pmc=PMC10461258」〔mcp 1.30.0 与 2.3.0 各一次，走 SDK 包装层〕——
        # 也就是说 'PMC' 是认的，旧句子在它自己那条报错里反证了自己。真正不认的是 'PMCID'（非库名）。
        if s not in self.dependencies.DB_WHITELIST:
            raise self.dependencies.NcbiError(f"to={to!r} 不是本工具支持的库。可用：{', '.join(sorted(self.dependencies.DB_WHITELIST))}"
                            f"（库名大小写不敏感，'PMC' 与 'pmc' 算同一个库；不认 'PMCID' 这种非库名写法）")
        return s


    async def _pmids_exist(self, pids):
        """返回 PMID 到 present / unavailable / unknown 的映射。

        【已作废 2026-11-11】下面旧依据不能推出“未返回就不存在”；只有正常记录证明存在。

        〔实测 2026-11-08〕elink 分不出「号不存在」和「号存在但那个方向零链接」——
        真号 42760028（to=pmc 无链接）与假号 9999999999 返回的 linkset 形状逐字相同
        （都只有 {"dbfrom":"pubmed","ids":[...]}，没有 linksetdbs 键）。
        上一轮我就是拿「linksetdbs 缺失」当「号不存在」的判据，结果把有 PMC deposit 之外的
        真号说成「PubMed 里没有这个号」，存在性判断整个反过来。esummary 能分开这两种：
          真号 → 完整记录（实测 42760028 / 33485718 各 43 个键）
          假号 → 空壳，且带 error="cannot get document summary"（实测 9999999999 只有 2 个键）
        """
        # 已作废 2026-11-11：缺返回/空壳不一律证明不存在。保留每个请求号的独立状态。
        states = {str(pid): "unknown" for pid in pids}
        pairs = await self.dependencies._esummary_dicts(list(states), include_unavailable=True)
        for uid, rec in pairs:
            if rec.get("error") == "cannot get document summary":
                states[uid] = "unavailable"
            elif not rec.get("error") and rec.get("title"):
                states[uid] = "present"
        return states


    async def _enrich_neighbors(self, candidates, term, with_titles, scope, *, check_candidates=None):
        """完整名单与本次检查窗口分开；预算外、故障或失真不冒充未命中。"""
        selected = (candidates if check_candidates is None else check_candidates)[:_NEIGHBOR_ENRICH_CAP]
        details = {pid: {'pmid': pid, 'in_term': None,
                         'match_status': 'not_checked' if term else 'not_requested',
                         'title': None, 'pubdate': None, 'languages': [],
                         'metadata_status': 'not_checked' if with_titles else 'not_requested'}
                   for pid in candidates}
        warnings, checks = [], []
        window = candidates if check_candidates is None else check_candidates
        if len(window) > len(selected):
            warnings.append(f'仅检查本次窗口内前 {_NEIGHBOR_ENRICH_CAP} 篇候选；其余未检查，不能当成未命中。'
                            '若本页没有检查完，不要推进 offset；须用同一 offset、更小的每源 limit 重放（见分页提示）。')
        if term:
            for start in range(0, len(selected), self.dependencies.GOLD_CHUNK):  # 本项目自订：沿用已知集分批预算
                batch = selected[start:start + self.dependencies.GOLD_CHUNK]
                query = _uid_intersection(term, batch)
                check = {'pmids': batch, 'term': query, 'status': 'unknown'}
                try:
                    d = await self.dependencies._esearch(query, retmax=len(batch), **scope)
                    count = self.dependencies._count_of(d)
                    got = d.get('idlist') or []
                    if count != len(got) or len(set(got)) != len(got) or not set(got).issubset(batch):
                        raise self.dependencies.NcbiError('近邻交集返回不完整或包含非本批 PMID')
                    check.update(self._intersection_check(term, d, scope, batch), request_params=d.get('_request_params') or {})
                    valid = check['status'] == 'checked'
                    for pid in batch:
                        details[pid].update(in_term=(pid in got) if valid else None, match_status=check['status'])
                    warnings.extend(note for note in check['warnings'] if note.startswith('⚠️'))
                    if not valid:
                        warnings.append('近邻命中判断无法确认：检索式被忽略、改写或执行式缺失；in_term=null，不当成漏检。')
                except self.dependencies.NcbiError as exc:
                    check['error'] = self.dependencies._mask(str(exc))
                    for pid in batch:
                        details[pid]['match_status'] = 'error'
                    warnings.append('近邻命中检查失败，相关候选为未知：' + check['error'])
                checks.append(check)
        # 相对日期窗口由 NCBI 服务器日历计算；同一操作的各批若回显不同窗口，任何一批的
        # in_term 都不能代表统一范围 —— 在汇总（match_summary）之前把受影响批次记为未知。
        windows = [tuple(check['relative_window']) for check in checks if check.get('relative_window')]
        if len(set(windows)) > 1:
            drift_warning = ('⚠️ 各批实际相对日期窗口不一致，命中判断不能合并为同一范围；'
                             '受影响批次的 in_term 记为未知（null），不当成命中或漏检。')
            for check in checks:
                if check.get('relative_window'):
                    check['status'] = 'unreliable_query'
                    check.setdefault('warnings', []).append(drift_warning)
                    for pid in check.get('pmids', []):
                        if pid in details:
                            details[pid].update(in_term=None, match_status='unreliable_query')
            warnings.append(drift_warning)
        if with_titles and selected:
            try:
                records = dict(await self.dependencies._esummary_dicts(selected, include_unavailable=True))
                for pid in selected:
                    record = records.get(pid) or {}
                    if not record.get('error') and record.get('title'):
                        details[pid].update(title=record['title'], pubdate=record.get('pubdate') or None,
                                            languages=[lang for v in self.dependencies._as_list(record.get('lang') or [])
                                                       if isinstance(v, str) and (lang := v.strip())], metadata_status='available')
                    else:
                        details[pid]['metadata_status'] = 'unavailable' if record.get('error') else 'missing'
                if any(details[p]['metadata_status'] != 'available' for p in selected):
                    warnings.append('部分候选标题元数据缺失；不表示文献不存在，且与命中判断分别报告。')
            except self.dependencies.NcbiError as exc:
                for pid in selected:
                    details[pid]['metadata_status'] = 'error'
                warnings.append('候选标题获取失败，关联名单仍保留：' + self.dependencies._mask(str(exc)))
        return details, list(dict.fromkeys(warnings)), checks

    async def links(self, pmids: str, to: str = "pmc", as_json: bool | str = False,
                    out_path: str = "", offset: StrictInt | StrictStr = 0,  # 本项目自订：保留默认偏移
                    limit: StrictInt | StrictStr | None = None,
                    term: str = "", with_titles: bool | str = False, field: str = "",
                    mindate: str = "", maxdate: str = "", reldate: StrictInt | StrictStr = 0,  # 本项目自订：默认不限相对日期
                    datetype: Literal["pdat", "edat"] = "pdat",
                    compact: Annotated[bool | str, Field(description='精简 JSON，每个目标列一次。out_path 仍存完整记录。')] = False) -> str:
        """关联/追引。pmids逗号/空格分隔，最多50个〔工具口径〕；to: pmc默认/gene/nucleotide/pubmed/citedin/refs。
        pubmed含自身；PMC非下载保证，无关联非无全文；引文仅限已录入关系。来源未知/请求失败不能判零；方括号为其它关联数。
        compact=True精简JSON；out_path存全量并回路径/sha/字节数。offset/limit按源切页和检查窗口，不裁文件。部分失败保留，全失败报错。
        pubmed/refs/citedin可带term，传原式字段/日期；in_term=null为未查/失败/失真，非漏检。
        with_titles附标题/日期/语言；前200篇〔自订预算〕，每源limit≤预算/来源数。次序非引文编号或质量排名；追引未比对改式新增，相关性须读摘要。
        """
        uniq, _ = self.dependencies._parse_pmids(pmids)
        db = self.dependencies._check_db(to)
        term = term.strip()
        titles = self.dependencies._as_bool(with_titles, 'with_titles')
        if (term or titles) and db not in ('pubmed', 'refs', 'citedin'):
            raise self.dependencies.NcbiError('term / with_titles 只用于 PubMed 文献候选（pubmed / refs / citedin）')
        scope = self._review_scope(field, mindate, maxdate, reldate, datetype)
        if not term and (any(scope[k] for k in ('field', 'mindate', 'maxdate', 'reldate')) or datetype != 'pdat'):
            raise self.dependencies.NcbiError('字段/日期限制需要 term；未执行的限制不能假装生效')
        if len(uniq) > 50:
            raise self.dependencies.NcbiError(f"一次最多 50 个 PMID（实测多 id 会被 NCBI 合并、归属丢失，且一个 id 一次请求），"
                            f"收到 {len(uniq)} 个，请分批")
        compact_mode = self.dependencies._as_bool(compact, 'compact')
        json_mode = self.dependencies._as_bool(as_json, 'as_json') or compact_mode
        off = self.dependencies._int(offset, "offset")
        if off < 0:
            raise self.dependencies.NcbiError(f"offset 不能为负数，收到 {off!r}")
        if limit is None:
            lim = None
        else:
            lim = self.dependencies._int(limit, "limit")
            if lim < 0:
                raise self.dependencies.NcbiError(f"limit 不能为负数，收到 {lim!r}；要全部目标请省略 limit")
        # 输出路径在联网前检查：已存在就当场拒绝，不先花掉一轮请求。
        outp = self.dependencies._check_out_path(out_path)
        metadata_warnings = []
        try:
            exist = await self.dependencies._pmids_exist(uniq)
        except self.dependencies.NcbiError as exc:
            exist = {pid: "unknown" for pid in uniq}
            metadata_warnings.append("⚠️ esummary 存在性检查失败，关联查询继续：" + self.dependencies._mask(str(exc)))
        text_mode = not (json_mode or outp)
        out = [None] * len(uniq)
        errs = []
        sources = [None] * len(uniq)
        snow = db in ("citedin", "refs")
        snow_link = "pubmed_pubmed_citedin" if db == "citedin" else ("pubmed_pubmed_refs" if db == "refs" else "")
        target_db = 'pubmed' if snow else db
        allowed_dbs = ('nucleotide', 'nuccore') if db == 'nucleotide' else (target_db,)
        show_cap = (self.dependencies.SNOW_SINGLE_CAP if len(uniq) == 1 else self.dependencies.SNOW_BATCH_CAP) if snow else 8
        candidate_db = db in ("pubmed", "refs", "citedin")
        direct_names = {
            "pmc": ("pubmed_pmc",),
            "gene": ("pubmed_gene",),
            "pubmed": ("pubmed_pubmed",),
            "nucleotide": ("pubmed_nuccore", "pubmed_nuccore_refseq"),
            "citedin": ("pubmed_pubmed_citedin",),
            "refs": ("pubmed_pubmed_refs",),
        }.get(db, ())
        schema = "pubmed.links.v1"

        def _page(ids):
            return ids[off:] if lim is None else ids[off:off + lim]

        def _positions(ids):
            return {'first': off + 1 if ids else None, 'last': off + len(ids) if ids else None,
                    'returned': len(ids)}  # 本项目口径：按各来源返回次序从一计，不是原文编号

        def _link_targets(lsd):
            vals = lsd.get("links")
            if vals is None:
                vals = lsd.get("neighbors")
            if vals is None:
                raise self.dependencies.NcbiError('elink 关系组缺少目标名单，关联未知')
            if not isinstance(vals, list):
                vals = [vals]
            got = []
            for value in vals:
                if isinstance(value, bool) or not isinstance(value, (str, int)):
                    raise self.dependencies.NcbiError(f"elink 返回了无法识别的目标 ID：{value!r}")
                text = str(value).strip()
                if not re.fullmatch(r"[0-9]+", text) or len(text) > 64:  # 本项目自订：ID 长度防护
                    raise self.dependencies.NcbiError("elink 返回了非法目标 ID")
                if not text.strip('0'):  # 本项目口径：NCBI UID 须为正整数
                    raise self.dependencies.NcbiError('elink 目标 ID 不能为零')
                got.append(str(int(text)))
            return got

        async def one(i, pid):
            fetched_at = self.dependencies._utc_now()
            state = exist.get(pid, "unknown")
            entry = {"source": pid, "source_status": state, "relation": db,
                     "target_ids": [], "error": None, "fetched_at": fetched_at,
                     "target_count": 0, "relation_status": "ok", "linknames": [],
                     "other_relations": []}
            direct = []
            try:
                params = {"dbfrom": "pubmed", "db": target_db, "id": pid, "retmode": "json"}
                if snow_link:
                    params["linkname"] = snow_link
                d = self.dependencies._safe_json(await self.dependencies._get_async("elink.fcgi", params, 25), "elink")
                sets = d.get("linksets", [])
                if not isinstance(sets, list):
                    raise self.dependencies.NcbiError("elink 的 linksets 不是列表，不能判断关联关系")
                if snow and (not sets or any(not isinstance(s, dict) for s in sets)):
                    raise self.dependencies.NcbiError("elink 缺少有效 linkset，不能判断引文关系")
                owned = []
                for ls in sets:
                    if not isinstance(ls, dict):
                        raise self.dependencies.NcbiError("elink 的 linkset 结构异常，不能判断关联关系")
                    # 单号发请求不代替回包归属；两种键并存时也不能忽略冲突。
                    src_ids = {str(x) for key in ('id', 'ids') if key in ls
                               for x in self.dependencies._as_list(ls[key])}
                    if src_ids == {pid}:
                        if 'dbfrom' in ls and ls['dbfrom'] != 'pubmed':
                            raise self.dependencies.NcbiError('elink 来源库与请求不符，关联未知')
                        owned.append(ls)
                if (snow or sets) and not owned:
                    raise self.dependencies.NcbiError("elink 未返回归属于请求号的 linkset")
                for ls in owned:
                    error = ls.get("error") or ls.get("ERROR")
                    if error:
                        raise self.dependencies.NcbiError(self.dependencies._mask(f"elink 来源请求失败：{error}"))
                    groups = ls.get('linksetdbs', [])  # 此键缺省是有效空关联；类型错误不是空关联。
                    if not isinstance(groups, list):
                        raise self.dependencies.NcbiError('elink 的 linksetdbs 不是列表，关联未知')
                    for lsd in groups:
                        if not isinstance(lsd, dict):
                            raise self.dependencies.NcbiError("elink 的 linksetdb 结构异常，不能判断关联关系")
                        error = lsd.get("error") or lsd.get("ERROR")
                        if error:
                            raise self.dependencies.NcbiError(self.dependencies._mask(f"elink 关联请求失败：{error}"))
                        name = lsd.get('linkname')
                        if not isinstance(name, str) or not name.strip():
                            raise self.dependencies.NcbiError('elink 未给出有效 linkname，关联方向未知')
                        if snow and name != snow_link:
                            raise self.dependencies.NcbiError('elink 返回的引文方向与请求不符，关联未知')
                        if 'dbto' in lsd and lsd['dbto'] not in allowed_dbs:
                            raise self.dependencies.NcbiError('elink 目标库与请求不符，关联未知')
                        dests = _link_targets(lsd)
                        if not dests:
                            continue
                        if name not in direct_names:
                            gloss = "（PMC 中引用本文的全文，不是参考文献）" if name == "pubmed_pmc_refs" else ""
                            entry["other_relations"].append(
                                {"relation": name, "count": len(dests), "note": gloss})
                        else:
                            direct.append((name, dests))
            except self.dependencies.NcbiError as e:
                errs.append(f"PMID {pid}：{str(e)[:120]}")
                entry["error"] = str(e)
                entry["relation_status"] = "error"
                sources[i] = entry
                if text_mode:
                    out[i] = f"PMID {pid}  ⚠️ 请求失败，关联未知：{str(e)[:120]}"  # 本项目自订：原因预览限长
                    if state != 'present':
                        out[i] += '；题录存在性未确认，请核号'
                return

            seen = set()
            for name, dests in direct:
                if name not in entry["linknames"]:
                    entry["linknames"].append(name)
                for target in dests:
                    if target not in seen:
                        seen.add(target)
                        entry["target_ids"].append(target)
            entry["target_count"] = len(entry["target_ids"])
            if not sets:
                entry["relation_status"] = "no_linkset"
                entry["error"] = "elink 没返回这个号的 linkset，关联未知（不是「没有关联」）"
            sources[i] = entry
            if not text_mode:
                return

            # 默认保留按关联名称的概览；显式分页则与 JSON 使用同一份去重来源名单。
            if not sets:
                out[i] = (f"PMID {pid}  ⚠️ elink 没返回这个号的 linkset —— 请求没给回这个号，"
                          f"这【不是】「没有关联」，先核对 PMID")
                if state != 'present':
                    out[i] += '；题录存在性未确认'
                return
            mine, other = [], []
            displayed_groups = [(snow_link, entry['target_ids'])] if snow and direct else direct
            for name, dests in displayed_groups:
                more_hint = ""
                if len(dests) > show_cap:
                    more_hint = ('…' if snow else f"…共 {len(dests)}") + (
                        f"（多号批量每号限展 {self.dependencies.SNOW_BATCH_CAP}，单号调用可展 {self.dependencies.SNOW_SINGLE_CAP}）"
                        if snow and len(uniq) > 1 else "")
                prefix = '目标 PMID：' if snow else f'{name}='
                mine.append(prefix + ",".join(
                    (x if db != "pmc" else f"PMC{x}") for x in dests[:show_cap]) + more_hint)
            for item in entry["other_relations"]:
                other.append(f"{item['relation']}:{item['count']} 条{item['note']}")
            tail = ("  [" + "; ".join(other) + "]") if other else ""
            if mine and (off or lim is not None):
                page_ids = _page(entry['target_ids'])
                positions = _positions(page_ids)
                if page_ids:
                    where = f"第 {positions['first']} 至 {positions['last']} 项"
                    shown = ','.join(('PMC' if db == 'pmc' else '') + v for v in page_ids[:show_cap])
                    remaining = f'；文本仅列前 {show_cap} 个，整页见 compact=True' if len(page_ids) > show_cap else ''
                    out[i] = f"PMID {pid}  来源列表{where}（共 {entry['target_count']} 个）：{shown}{remaining}" + tail
                else:
                    out[i] = (f"PMID {pid}  分页前有 {entry['target_count']} 个目标；"
                              f"本页未展示编号（offset={off}, limit={lim}），不是零关联。" + tail)
            elif mine:
                count_note = ''
                if snow:
                    label = '施引文献' if db == 'citedin' else '参考文献'
                    count_note = f"{label} {entry['target_count']} 篇；"
                out[i] = f"PMID {pid}  {count_note}" + "  ".join(mine) + tail
            elif state == "unavailable":
                out[i] = (f"PMID {pid}  ⚠️ esummary 未找到此号的文档摘要（cannot get document summary）；"
                          "请核对 PMID 或稍后重试，不能据此判断全文可用性") + tail
            elif state == "unknown":
                out[i] = f"PMID {pid}  ⚠️ 元数据缺失，存在性无法确认；请重试" + tail
            elif snow:
                label = '施引记录' if db == 'citedin' else '参考文献'
                out[i] = f"PMID {pid}  (PubMed 未录入{label}；{_ZERO_CITATION_CAUTION[db]})" + tail
            else:
                out[i] = f"PMID {pid}  ({db} 库无直接对应记录)" + tail
            if mine and state != 'present':
                out[i] += '  ⚠️ esummary 未确认此号，以上关联来自 elink；请复核'

        async with anyio.create_task_group() as tg:
            for i, pid in enumerate(uniq):
                tg.start_soon(one, i, pid)
        source_states = {e['source']: {k: e[k] for k in ('source_status', 'relation_status', 'target_count', 'error')}
                         for e in sources}
        failed = sum(bool(e['error']) for e in sources)
        if failed == len(sources):
            self.dependencies._audit('links', to=db, requested=len(uniq), errors=failed,
                                     pmids=uniq, source_states=source_states, warnings=metadata_warnings)
            first = sources[0]  # 本项目口径：首个输入来源
            exc = self.dependencies.NcbiError(f"elink 全部 {failed} 个来源失败或无法确认；PMID {first['source']}：{first['error']}")
            exc.link_sources = sources
            raise exc

        direct_total = sum(entry['target_count'] for entry in sources)
        if direct_total > self.dependencies.EXPORT_MAX:
            raise self.dependencies.NcbiError("关联目标超过本项目安全上限；请减少源 PMID，未写入截断文件")

        enriched, match_checks = {}, []
        match_summary = {}
        more_targets = lim is not None and lim > 0 and any(e['target_count'] > off + lim for e in sources)
        enrich_page = {'next_offset': off + lim if more_targets else None}
        if term or titles:
            window_candidates = list(dict.fromkeys(target for entry in sources for target in _page(entry['target_ids'])))
            chosen = (list(dict.fromkeys(target for entry in sources for target in entry['target_ids']))
                      if outp else window_candidates)
            enriched, extra_warnings, match_checks = await self._enrich_neighbors(
                chosen, term, titles, scope, check_candidates=window_candidates)
            metadata_warnings.extend(extra_warnings)
            # 每源 offset/limit 切片；去重只会让窗口更小。把「每源 limit」限在 预算/来源数，
            # 才能保证一次调用的去重窗口不超过检查预算，不会留下翻页跳过的未检查候选。
            n_sources = sum(1 for entry in sources if entry['target_ids']) or 1
            safe_limit = max(1, _NEIGHBOR_ENRICH_CAP // n_sources)
            if n_sources > _NEIGHBOR_ENRICH_CAP:
                # 生产上限下不会发生（pmids 最多 50、预算 200）；仍明确拒绝假装能一次查完。
                metadata_warnings.append(
                    f'⚠️ 参与检查的来源数 {n_sources} 超过检查预算 {_NEIGHBOR_ENRICH_CAP}：'
                    f'即使每源 limit=1 也无法在一次调用内检查全部来源，请把 pmids 再拆成更小批次分别调用。')
            window_total = len(window_candidates)
            exceeded = window_total > _NEIGHBOR_ENRICH_CAP
            more_after = lambda step: any(len(entry['target_ids']) > off + step
                                          for entry in sources if entry['target_ids'])
            if exceeded:
                next_offset, replay_limit = None, safe_limit
                metadata_warnings.append(
                    f'⚠️ 本页每源 limit={lim if lim is not None else "全部"}，去重后 {window_total} 个候选超过检查预算 '
                    f'{_NEIGHBOR_ENRICH_CAP}；仅前 {_NEIGHBOR_ENRICH_CAP} 个已检查，其余为 not_checked。'
                    f'不要直接用 offset+limit 前进（会跳过本页未检查候选）。'
                    f'安全重放：offset={off}, limit={safe_limit}（每源上限；{n_sources} 个来源去重后不超过 '
                    f'{_NEIGHBOR_ENRICH_CAP}）。'
                    + (f' 该页检查完后再用 offset={off + safe_limit}, limit={safe_limit}。'
                       if more_after(safe_limit) else ' 该页检查完后就没有更多候选页。'))
            elif lim == 0:
                next_offset, replay_limit = None, 0
                metadata_warnings.append('limit=0 时窗口为空，本页没有检查任何候选、也没有推进位置；'
                                         '要检查请给正 limit（每源上限不超过 预算/来源数）。')
            elif lim is None:
                next_offset, replay_limit = None, None
            else:
                replay_limit = lim
                next_offset = enrich_page['next_offset']
                if next_offset is not None:
                    metadata_warnings.append(
                        f'下一页 offset={next_offset}, limit={lim}（按来源翻页）。')
            enrich_page = {'enrichment_cap': _NEIGHBOR_ENRICH_CAP, 'window_total': window_total,
                           'window_checked': min(window_total, _NEIGHBOR_ENRICH_CAP),
                           'safe_per_source_limit': safe_limit, 'next_offset': next_offset,
                           'replay_limit': replay_limit, 'exceeded': exceeded}
            if term:
                all_pmids = {pid for entry in sources for pid in entry['target_ids']}
                match_summary = _window_match_counts(window_candidates, enriched, all_pmids)
            for pid, detail in enriched.items():
                detail.update(source_count=0, is_seed=pid in uniq, best_rank=None)  # 本项目自订：来源统计初态
            for entry in sources:
                for rank, pid in enumerate(entry['target_ids'], start=1):  # 本项目自订：按原始关联列表名次计
                    if pid not in enriched:
                        continue
                    detail = enriched[pid]
                    detail['source_count'] += 1  # 本项目自订：每个源的 target_ids 已去重
                    if db == 'pubmed':
                        detail['best_rank'] = min(detail['best_rank'] or rank, rank)

        def _candidate_details(cands):
            # 保留 sources/edges/candidate_pmids 的原顺序，只给候选详情另设阅读顺序。
            return sorted((enriched[pid] for pid in cands if pid in enriched), key=lambda item: (
                {False: 0, None: 1, True: 2}[item['in_term']],  # 本项目自订：确认未命中、未知、已命中
                -item['source_count'], item['best_rank'] or float('inf')))

        def _model(displayed):
            src_out, edges, edge_seen = [], [], set()
            cands, cand_seen = [], set()
            all_cands, all_cand_seen = [], set()
            shown_targets = 0
            for entry in sources:
                full_ids = entry["target_ids"]
                ids = _page(full_ids) if displayed else full_ids
                shown_targets += len(ids)
                src_out.append({"source": entry["source"], "source_status": entry["source_status"],
                                "relation": entry["relation"], "target_ids": ids,
                                'linknames': entry['linknames'], 'other_relations': entry['other_relations'],
                                **({'page_positions': _positions(ids)} if displayed else {}),
                                "error": entry["error"], "relation_status": entry["relation_status"],
                                "fetched_at": entry["fetched_at"]})
                for target in ids:
                    key = (entry["source"], entry["relation"], target)
                    if key not in edge_seen:
                        edge_seen.add(key)
                        edges.append({"source": entry["source"], "relation": entry["relation"],
                                      "target": target})
                    if candidate_db and target not in cand_seen:
                        cand_seen.add(target)
                        cands.append(target)
                if candidate_db and displayed:
                    for target in full_ids:
                        if target not in all_cand_seen:
                            all_cand_seen.add(target)
                            all_cands.append(target)
            if not displayed:
                all_cands = cands
            warnings = list(metadata_warnings)
            if errs:
                warnings.append(f"⚠️ {len(errs)} 个请求失败（那是故障，不是「没有链接」）：" + "; ".join(errs[:3]))
            no_link = [e["source"] for e in sources if e["relation_status"] in ("no_linkset", "no_owned_linkset")]
            if no_link:
                warnings.append("⚠️ 这些源的 elink 没有可归属于请求号的 linkset，关联状态未知（不是已确认无关联）："
                                + ", ".join(no_link))
            unverified = [e['source'] for e in sources if e['source_status'] != 'present']
            if unverified:
                warnings.append(f"⚠️ 来源 PMID {', '.join(unverified)} 的题录存在性未确认；请核号、重试。")
            if snow:
                empty_sources = [e['source'] for e in sources if e['relation_status'] == 'ok'
                                 and e['source_status'] == 'present' and not e['target_count']]
                if empty_sources:
                    warnings.append(f"PMID {', '.join(empty_sources)}：PubMed 未录入该方向引文；{_ZERO_CITATION_CAUTION[db]}。")
            if displayed and lim == 0 and not (term or titles):
                warnings.append(_EMPTY_LINK_PAGE)
            details = _candidate_details(cands) if term or titles else []
            return {
                "schema": schema,
                "to": db,
                "pmids": list(uniq),
                "requested": len(uniq),
                "sources": src_out,
                "edges": edges,
                "candidate_pmids": cands,
                **({'provenance': _LINKS_ORIGIN} if candidate_db else {}),
                **({'match_summary': match_summary} if term else {}),
                **({'candidate_details': details,
                    'outside_term_pmids': [r['pmid'] for r in details if r['in_term'] is False],
                    'match_scope': {'term': term, **scope, 'offset': off, 'limit': lim}, 'match_checks': match_checks,
                    'enrichment_cap': _NEIGHBOR_ENRICH_CAP} if term or titles else {}),
                "counts": {
                    "sources": len(uniq),
                    "sources_with_targets": sum(1 for e in sources if e["target_count"]),
                    "sources_with_errors": failed,
                    "targets": direct_total,
                    "targets_returned": shown_targets,
                    "per_source": {e["source"]: e["target_count"] for e in sources},
                    "candidate_pmids": len(cands),
                    "candidate_pmids_total": len(all_cands),
                },
                "pagination": {
                    "offset": off,
                    "limit": lim,
                    "per_source": True,
                    "applies_to": "returned_json" if displayed else "out_path_full_model",
                    "counts_before_pagination": True,
                    **enrich_page,
                },
                "warnings": warnings,
                "source_note": f"pubmed→{db}；按来源统计",
                "evidence": "elink / esummary" + ("；交集执行见 match_checks" if term else ""),
            }

        self.dependencies._audit("links", to=db, pmids=uniq, requested=len(uniq), direct_total=direct_total,
               errors=failed, source_states=source_states,
               warnings=metadata_warnings, show_cap=show_cap, offset=off, limit=lim,
               as_json=json_mode, term=term, with_titles=titles, match_scope=scope, match_checks=match_checks,
               **({'compact': True} if compact_mode else {}), **({'out_path': outp} if outp else {}))
        if compact_mode:
            compact_page = _compact_links(_model(displayed=True))
            if not outp:
                return json.dumps(compact_page, ensure_ascii=False, separators=(',', ':'))
        if outp:
            full = await anyio.to_thread.run_sync(_model, False)
            p, nbytes, sha = await anyio.to_thread.run_sync(
                functools.partial(self.dependencies._write_json_new, outp, full, indent=None))
            self.dependencies._audit("links", path=p, bytes=nbytes, sha256=sha, requested=len(uniq),
                   direct_total=direct_total)
            if compact_mode:
                compact_page.update(path=p, bytes=nbytes, sha256=sha, content_schema=schema)
                return json.dumps(compact_page, ensure_ascii=False, separators=(',', ':'))
            summary = {"schema": "pubmed.links.file.v1", "content_schema": schema, "path": p, "bytes": nbytes, "sha256": sha,
                       "to": db, "requested": len(uniq), "sources": len(uniq),
                       "targets": full["counts"]["targets"], "edges": len(full["edges"]),
                       "candidate_pmids": len(full["candidate_pmids"]),
                       "counts": full["counts"],
                       "source_issues": [{'source': e['source'], **source_states[e['source']]}
                                         for e in sources if e['error'] or e['source_status'] != 'present'],
                       **({'provenance': full['provenance']} if candidate_db else {}),
                       **({'match_summary': match_summary} if term else {}),
                       "pagination": {"offset": off, "limit": lim,
                                      "note": "out_path 保存全量名单和状态；offset/limit 限定本次候选检查窗口，不裁文件",
                                      **enrich_page},
                       "warnings": full["warnings"]}
            return json.dumps(summary, ensure_ascii=False)
        if json_mode:
            return json.dumps(_model(displayed=True), ensure_ascii=False)

        label = {'pubmed': '近邻候选', 'citedin': '施引候选', 'refs': '参考文献候选'}.get(db, '关联')
        head = [f"{label}（pubmed→{db}），{len(uniq)} 个来源 PMID："]
        head.extend(metadata_warnings)
        if not (term or titles) and lim is not None:
            if lim == 0:
                head.append(_EMPTY_LINK_PAGE)
            elif more_targets:
                head.append(f'下一页 offset={off + lim}, limit={lim}（按每个来源各自翻页）。')
        detail_lines = []
        if term or titles:
            if enriched:
                detail_lines.append('候选详情：')
            if term:
                detail_lines.append('命中判断范围：' + json.dumps({'term': term, **scope}, ensure_ascii=False))
                detail_lines.extend('交集实际执行：' + (check['querytranslation'] or '未返回') + '；' + check['status']
                                    for check in _neighbor_executions(match_checks))
                detail_lines.append(('本页去重候选 {window_total} 篇：命中 {inside}，未命中 {outside}，'
                                     '无法判断 {unknown}，未检查 {unchecked}。另有窗口外 {outside_window} 篇。').format(**match_summary))
            rows = _candidate_details(list(enriched))
            for row in rows[:_NEIGHBOR_TEXT_CAP]:
                label = {False: '未命中当前式', True: '命中当前式', None: '无法判断/未检查'}[row['in_term']] if term else '未请求命中判断'
                title = row['title'] or {'not_requested': '未请求标题', 'not_checked': '标题未检查',
                                        'missing': '⚠️ 标题元数据缺失', 'unavailable': '⚠️ 标题元数据不可用',
                                        'error': '⚠️ 标题请求失败'}.get(row['metadata_status'], '⚠️ 标题未返回')
                seed = '；种子自身' if row['is_seed'] else ''
                date = ('｜' + (row['pubdate'] or '日期未返回')) if row['metadata_status'] == 'available' else ''
                language = '｜语言：' + ', '.join(row['languages']) if row['languages'] else ''
                detail_lines.append(f"  PMID {row['pmid']}｜{label}{seed}{date}{language}｜{title}")
            if len(rows) > _NEIGHBOR_TEXT_CAP:
                detail_lines.append(f'本页共有 {len(rows)} 个去重候选，文本仅展示前 {_NEIGHBOR_TEXT_CAP} 个候选详情。'
                                    '要整页可用标题，首次调用即可传 with_titles=True, compact=True；'
                                    '超出检查预算的候选用 offset、limit 分页。')
        return '\n'.join(head + out + detail_lines)


    def _retrieval_check(self, d, stage):
        """保留每次响应的计数、执行式和全部解析提示，并标明检索阶段。"""
        return {"stage": stage, "count": self.dependencies._count_of(d),
                "request_params": dict(d.get("_request_params") or {}),
                "querytranslation": (d.get("querytranslation") or "").strip(),
                "warnings": [f"{stage} {text}" for text in self.dependencies._note(d, include_translation=False)]}


    def _record_count_change(self, state, before, after, context):
        if before != after:
            state["count_changed"] = True
            state.setdefault("drift", []).append(
                f"⚠️ {context}命中数变化：{before} → {after}；请复核。")


    def _collect_window_checks(self, state, first, expected_count, actual_count, context):
        checks = first.get("_retrieval_checks")
        state.setdefault("checks", []).extend(
            checks if checks is not None else [self.dependencies._retrieval_check(first, context)])
        if first.get("_count_changed"):
            state["count_changed"] = True
        self.dependencies._record_count_change(state, expected_count, actual_count, context + "计数探测→取号首页：")


    def _review_scope(self, field, mindate, maxdate, reldate, datetype):
        scope = {'field': self.dependencies._check_field(field),
                 'mindate': self.dependencies._check_date(mindate, 'mindate'),
                 'maxdate': self.dependencies._check_date(maxdate, 'maxdate'),
                 'reldate': self.dependencies._check_reldate(reldate),
                 'datetype': self.dependencies._check_datetype(datetype)}
        if scope['mindate'] or scope['maxdate']:
            start = self.dependencies._expand(scope['mindate'] or self.dependencies.NO_BEGIN)
            end = self.dependencies._expand(scope['maxdate'] or self.dependencies.NO_END, True)
            if start > end:
                raise self.dependencies.NcbiError('日期区间倒挂，请核对 mindate 和 maxdate')
        return scope

    def _normalized_scope(self, scope):
        """把调用参数归一成 _review_scope 同口径；诊断与交集检查共用，避免 "0" 被当真、空串报错。"""
        return {'field': self.dependencies._check_field(scope.get('field') or ''),
                'mindate': self.dependencies._check_date(scope.get('mindate') or '', 'mindate'),
                'maxdate': self.dependencies._check_date(scope.get('maxdate') or '', 'maxdate'),
                'reldate': self.dependencies._check_reldate(scope.get('reldate') or 0),
                'datetype': self.dependencies._check_datetype(scope.get('datetype') or 'pdat')}

    @staticmethod
    def _scope_note(scope):
        field = scope['field'] or '未另限'
        dates = []
        if scope['mindate'] or scope['maxdate']:
            dates.append(f"{scope['mindate'] or '不限起点'} 至 {scope['maxdate'] or '不限终点'}")
        if scope['reldate']:
            dates.append(f"近 {scope['reldate']} 天")
        label = '发表日期' if scope['datetype'] == 'pdat' else '入库日期'
        date_note = label + ' ' + ' 与 '.join(dates) if dates else '日期未另限'
        return f'复查范围（term 外）：字段 {field}，{date_note}'

    def _intersection_term_ignored(self, term, translation, scope):
        # 只判断原式主题是否整个消失：工具外加的本批 UID/日期由 _term_vanished 按标签识别，
        # 不依赖出现位置、括号分组或 [UID]/[PMID] 拼写；原式本身是纯 UID/日期条件时保留。
        return self.dependencies._term_vanished(translation, term=term,
                                                field=scope.get('field') or '')

    def _intersection_check(self, term, page, scope, batch):
        qt = (page.get('querytranslation') or '').strip()
        warnings, degraded = self.dependencies._recall_warnings_and_degraded(term, [page], include_translation=False)
        ignored = self._intersection_term_ignored(term, qt, scope)
        errors, relative = self._check_execution_scope(term, page, scope, batch)
        warnings.extend('⚠️ ' + message for message in errors)
        return {'status': 'unreliable_query' if ignored or degraded or errors else 'checked',
                'querytranslation': qt, 'term_ignored': ignored, 'term_degraded': degraded and not ignored,
                'warnings': warnings, **({'relative_window': relative} if relative else {})}

    def _check_execution_scope(self, term, page, scope, uid_batch=()):
        scope = self._normalized_scope(scope)
        bounds = None
        if scope['mindate'] or scope['maxdate']:
            bounds = (self.dependencies._expand(scope['mindate'] or self.dependencies.NO_BEGIN),
                      self.dependencies._expand(scope['maxdate'] or self.dependencies.NO_END, True))
        return self.dependencies._scope_evidence(term, page.get('querytranslation') or '', scope['field'],
                                                 bounds, scope['reldate'], scope['datetype'], uid_batch, check_subject=True,
                                                 ignored=self.dependencies._warn_bits(page)[0],
                                                 mappings=page.get('translationset') or ())

    async def _ids_in_window(self, term, *, mindate="", maxdate="", context="", field="", reldate=0, datetype="pdat"):  # 本项目自订：缺省不限制相对日期
        """返回 (ids, initial_count, first, drift)，保留现有返回接口。
        first 附带全部分页检查记录；count 为首页计数，不将后续计数覆盖成看似稳定。
        请求大小使用 RETMAX_CAP；生产响应的页内容由 _esearch 统一校验。"""
        params = dict(field=field, mindate=mindate, maxdate=maxdate, reldate=reldate, datetype=datetype,
                      retmax=self.dependencies.RETMAX_CAP)
        first = dict(await self.dependencies._esearch(term, **params))
        count = self.dependencies._count_of(first)
        if count > self.dependencies.WINDOW:
            raise self.dependencies.NcbiError(f"{context}命中 {count} 条，超过 PubMed esearch 的 {self.dependencies.WINDOW} 条检索窗口，"
                            f"无法在窗口内取全。请缩小查询，或改用 export（自动按发表日期切片取全）。")
        def page_check(page, stage):
            check = self.dependencies._retrieval_check(page, stage)
            _, degraded = self.dependencies._recall_warnings_and_degraded(term, [page], include_translation=False)
            reasons, relative = self._check_execution_scope(term, page, params)
            check['query_needs_review'] = degraded or bool(reasons)
            if degraded:
                reasons.append('执行式缺失或解析失真')
            check['review_reasons'] = reasons
            if reldate:
                check['relative_window'] = relative
            return check

        checks = [page_check(first, f"{context}retstart=0")]  # 本项目自订：首页偏移标签
        first["_retrieval_checks"] = checks
        first["_count_changed"] = False
        ids = list(first.get("idlist") or [])
        if count and not ids:
            raise self.dependencies.NcbiError(f"{context}分页中断：正命中数 {count} 的首页为空，请重试。")
        drift_parts = []
        previous_count = count
        while len(ids) < count:
            offset = len(ids)
            page = await self.dependencies._esearch(term, retstart=offset, **params)
            checks.append(page_check(page, f"{context}retstart={offset}"))
            page_count = self.dependencies._count_of(page)
            if page_count != previous_count:
                first["_count_changed"] = True
                drift_parts.append(f"⚠️ {context}retstart={offset} 命中数变化：{previous_count} → {page_count}；"
                                   f"首页命中数 {count}，请复核。")
            previous_count = page_count
            got = page.get("idlist") or []
            if not got:
                raise self.dependencies.NcbiError(f"{context}分页中断：已取 {len(ids)}/{count} 条时 NCBI 返回空页。"
                                f"数据库可能在检索期间更新，请重试。")
            ids.extend(got)
        uniq = list(dict.fromkeys(ids))
        if len(uniq) != len(ids):
            drift_parts.append(f"⚠️ {context}分页出现重复 PMID（取 {len(ids)} 个、去重后 {len(uniq)} 个）——"
                               f"数据库可能在分页期间更新，结果以取回时点为准。")
        if len(uniq) != count:
            drift_parts.append(f"⚠️ {context}实际取回（去重后）{len(uniq)} 个 ≠ NCBI 命中数 {count}；"
                               f"数据库可能在检索期间更新，请复核。")
        return uniq, count, first, "\n".join(drift_parts)


    async def check_recall(self, term: str, pmids: str, as_json: bool | str = False,
                           field: str = '', mindate: str = '', maxdate: str = '',
                           reldate: StrictInt | StrictStr = 0, datetype: Literal['pdat', 'edat'] = 'pdat') -> str:  # 本项目自订：默认不限相对日期
        """已知集找回，非整体查全率。pmids逗号/空格分隔，最多200个、每批50个〔自订预算〕。
        显式传原搜索的字段/日期。逐批核对：retrieved 是实取；found/missing 只计可靠批次，unknown 不算漏检。
        逐块用 count；扩展 links，对比 diff；查新见 literature-search。
        """
        return await self._recall(term, pmids, as_json, field, mindate, maxdate, reldate, datetype)

    async def retry_recall(self, term, pmids, previous_path, *, mindate='', maxdate='', datetype='pdat'):
        """Internal review repair: bind the prior receipt without embedding it in the audit."""
        payload = await anyio.Path(previous_path).read_bytes()
        previous = json.loads(payload)
        self.dependencies._audit('retry_recall', previous_sha256=hashlib.sha256(payload).hexdigest())
        return await self._recall(term, pmids, True, '', mindate, maxdate, 0, datetype, previous)  # 本项目口径：综述仅绑定绝对日期

    async def _recall(self, term, pmids, as_json, field, mindate, maxdate, reldate, datetype, previous=None):
        json_mode = self.dependencies._as_bool(as_json, 'as_json')
        if not str(term or "").strip():
            # 旧版空 term 拼成「() AND (uids)」，NCBI 只执行 uid 部分 → 报「全找回」并误指中文〔实测 2026-11-13〕。
            raise self.dependencies.NcbiError("term 不能为空：空检索式没有可测的找回率。")
        uniq, n_in = self.dependencies._parse_pmids(pmids)
        if len(uniq) > self.dependencies.GOLD_MAX:
            raise self.dependencies.NcbiError(f"check_recall 一次最多 {self.dependencies.GOLD_MAX} 个金标准 PMID（本项目自订上限），"
                            f"收到 {len(uniq)} 个，请分批。")
        scope = self._review_scope(field, mindate, maxdate, reldate, datetype)
        # 分批交集：一次塞 200 个 pmid[uid] 会让 GET URL 超长被 NCBI 414〔实测 A1〕。
        reused, old_missing = {}, {}
        if previous is None:
            chunk = self.dependencies.GOLD_CHUNK
            batches = [uniq[i:i + chunk] for i in range(0, len(uniq), chunk)]  # 本项目口径：按当前批量上限切片
        else:
            batches = [c['pmids'] for c in previous['checks']]
            if previous['term'] != term or previous['scope'] != scope or [p for b in batches for p in b] != uniq:
                raise self.dependencies.NcbiError('恢复的已知集批次与原式、范围或来源名单不一致')
            reused = {tuple(c['pmids']): c for c in previous['checks'] if 'error' not in c}
            old_missing = {item['pmid']: item for item in previous['missing']}
        checks, attempted = [], []
        for batch in batches:
            saved = reused.get(tuple(batch)) if reused else None
            if saved is not None:
                checks.append(dict(saved))
                continue
            gold_q = _uid_intersection(term, batch)
            d = None
            if previous is not None:
                attempted.append(batch)
            try:
                d = await self.dependencies._esearch(gold_q, retmax=len(batch), **scope)
                count = self.dependencies._count_of(d)
                got = d.get('idlist') or []
                if count != len(got) or len(set(got)) != len(got) or not set(got).issubset(batch):
                    raise self.dependencies.NcbiError(f'check_recall 交集结果异常：命中 {count}、返回 {len(got)} 个（本批金标准 {len(batch)} 个）。请复核查询。')
                check = self._intersection_check(term, d, scope, batch)
                checks.append(dict(check, pmids=batch, returned=list(got)))
            except self.dependencies.NcbiError as exc:
                message = self.dependencies._mask(str(exc))
                # 服务器实取名单可含批外号，不能把它混入按已知集统计的 retrieved。
                page = d if d is not None else getattr(exc, 'rejected_page', None)
                received = page is not None  # 空对象也是已收到但被拒的回包。
                payload = page or {}
                ids = payload.get('idlist')
                observed = [p for p in ids if isinstance(p, str) and re.fullmatch(r'[1-9][0-9]*', p)] if isinstance(ids, list) else []
                translation = payload.get('querytranslation')
                label = '交集回包不一致' if received else '交集请求未取得有效回包'
                checks.append(dict(status='unreliable_query', pmids=batch,
                                   returned=list(dict.fromkeys(p for p in observed if p in batch)), error=message,
                                   error_kind='invalid_intersection' if received else 'request_failed',
                                   **({'observed_pmids': observed, 'observed_count': payload.get('count')} if received else {}),
                                   querytranslation=translation.strip() if isinstance(translation, str) else '',
                                   term_ignored=False, term_degraded=False,
                                   warnings=[f'⚠️ {label}，请按原范围重试 PMID {", ".join(batch)}：{message}']))
        if all('error' in c for c in checks):
            self.dependencies._audit('check_recall', checks=checks, querytranslations=[c['querytranslation'] for c in checks],
                                     batch_translations=True, pmids=uniq, unknown=uniq, scope=scope, term=term)
            raise self.dependencies.NcbiError('check_recall 全部批次失败：' + checks[0]['error'])  # 本项目口径：显示首个失败原因
        windows = {tuple(c['relative_window']) for c in checks if c.get('relative_window')}
        if len(windows) > 1:
            for c in checks:
                c['status'] = 'unreliable_query'
                c['warnings'].append('⚠️ 各批实际相对日期窗口不一致，不能合并为同一范围。')
        raw = {p for c in checks for p in c['returned']}
        uncertain = {p for c in checks if c['status'] != 'checked' for p in c['pmids']}
        retrieved = [p for p in uniq if p in raw]
        unknown = [p for p in uniq if p in uncertain]
        found = [p for p in uniq if p in raw and p not in uncertain]
        missing = [p for p in uniq if p not in raw and p not in uncertain]
        miss_info = []
        fresh_missing = [p for p in missing if p not in old_missing]
        if fresh_missing:
            try:
                recs = dict(await self.dependencies._esummary_dicts(fresh_missing, include_unavailable=True))
                for p in fresh_missing:
                    r = recs.get(p)
                    if r is not None and not r.get("error") and r.get("title"):
                        miss_info.append({"pmid": p, "state": "present", "title": r.get("title") or ""})
                    elif r is not None and r.get("error") == "cannot get document summary":
                        miss_info.append({"pmid": p, "state": "unavailable", "title": ""})
                    else:
                        miss_info.append({"pmid": p, "state": "unknown", "title": ""})
            except self.dependencies.NcbiError as e:
                meta_warn = f"{_TITLE_FAILURE_PREFIX}（不影响 found/missing 判定）：{str(e)[:200]}"  # 本项目口径：原因预览限长
                miss_info = [{"pmid": p, "state": "unknown", "title": "", "metadata_warning": meta_warn} for p in fresh_missing]
        if old_missing:
            indexed = {**old_missing, **{item['pmid']: item for item in miss_info}}
            miss_info = [indexed[p] for p in missing]
        caveat = ("只证明这批已知集的找回，不代表整体查全率；金标准集可能有偏（如只取某综述的参考文献）。"
                  "present＝记录存在但未被本次实际执行式检回；unavailable/unknown＝存在性未确认，不能断定是 term 漏了它。")
        translations = [c['querytranslation'] for c in checks]
        qt = translations[0]  # 本项目口径：始终指首批，不借其它批次的执行式
        # term 本身就只由 PMID[uid] 构成时，执行式只剩 uid 是正常结果，不是「被忽略」
        # 〔实测 2026-11-13：term=42988646[uid] → 执行式 42988646[UID] AND 42988646[UID]，旧版误报 term_ignored〕。
        term_ignored = any(c['term_ignored'] for c in checks)
        term_degraded = any(c['term_degraded'] for c in checks)
        warn = [w for c in checks for w in c['warnings']]
        if term_ignored:
            hint = '（常见于中文词，可换英文词重查）' if self.dependencies._CJK.search(term) else ''
            warn.append(f'⚠️ 至少一批原式条件未完整保留{hint}；该批实取 PMID 不代表原式命中。')
        if unknown:
            warn.append('⚠️ 范围或执行式无法核实的批次列为 unknown，不计入检回或未检回。')
        controls = []
        if missing:
            original = dict(term=term, pmids=' '.join(uniq), as_json=True, **scope)
            for label, reset in (('外加绝对日期', {'mindate': '', 'maxdate': ''}),
                                 ('外加相对日期', {'reldate': 0}), ('外加字段', {'field': ''})):  # 本项目口径：每次只改变一个限制
                if any(scope[key] for key in reset):
                    controls.append({'omit': label, 'tool': 'check_recall', 'arguments': {**original, **reset}})
            if controls:
                warn.append('定位未命中原因：每次只去掉一个限制，保留其余条件和同一组 PMID，再比 found/missing/unknown；本次结果不能直接说明是哪项造成的。')
            elif any(item['state'] == 'present' for item in miss_info):
                warn.append('请核对已知题录与原式用词；本次没有外加字段或日期可单独去掉。')
        if any(item['state'] == 'unknown' and 'metadata_warning' not in item for item in old_missing.values()):
            # Older receipts kept title request failures only in the stage warnings.
            warn.extend(w for w in previous['warnings'] if w.startswith(_TITLE_FAILURE_PREFIX))
        warn.extend(item['metadata_warning'] for item in miss_info if 'metadata_warning' in item)
        warn = list(dict.fromkeys(warn))
        if n_in != len(uniq):
            warn.append(f"（原始输入 {n_in} 个，去重后 {len(uniq)} 个）")
        if len(batches) > 1:
            warn.append(f"（金标准 {len(uniq)} 个分 {len(batches)} 批交集判定，每批最多 {self.dependencies.GOLD_CHUNK} 个，避免 URL 超长）")
        self.dependencies._audit('retry_recall' if previous is not None else 'check_recall', term=term, gold=len(uniq), found=len(found), missing=len(missing),
               term_ignored=term_ignored, term_degraded=term_degraded, querytranslation=qt,
               querytranslations=translations, batch_translations=True, scope=scope, pmids=uniq, warnings=warn,
               **({'reused_batches': [list(b) for b in reused], 'retried_batches': attempted} if previous is not None else {}),
               retrieved=retrieved, unknown=unknown, checks=checks)
        if json_mode:
            return json.dumps({"term": term, "scope": scope, "gold_count": len(uniq), "found": found, "missing": miss_info,
                               "retrieved": retrieved, "unknown": unknown, "checks": checks,
                               **({'suggested_controls': controls} if controls else {}),
                               "term_ignored": term_ignored, "term_degraded": term_degraded,
                               "querytranslation": qt, "querytranslations": translations,
                               "warnings": warn, "caveat": caveat}, ensure_ascii=False)
        head = [f"查询：{term}", self._scope_note(scope),
                f"金标准 {len(uniq)} 个 PMID，检回 {len(found)} 个，未检回 {len(missing)} 个，无法判定 {len(unknown)} 个",
                "只核对这批已知文献，不代表整体查全率。"]
        for number, c in enumerate(checks, 1):  # 本项目自订：批次从首批编号
            if 'error' in c:
                if c.get('error_kind') == 'invalid_intersection':
                    head.append(f"第 {number} 批回包不一致，无法判定；实际执行：{c['querytranslation'] or '⚠️【待确认】'}")
                else:
                    head.append(f'第 {number} 批请求失败，无法判定。')
                continue
            status = ''
            if c['status'] != 'checked':
                status = '（原式条件未完整保留，无法判定）' if c['term_ignored'] else '（无法核实原式或范围，无法判定）'
            elif unknown:
                status = '（本批已核实）'
            head.append(f"第 {number} 批{status} NCBI 实际执行：{c['querytranslation'] or '⚠️【待确认】'}")
        head += warn
        if unknown:
            head.append('无法判定：' + ', '.join(unknown))
            head.append('服务器实取（不等于已验证找回）：' + (', '.join(retrieved) or '无'))
        if found:
            head.append("检回：" + ", ".join(found))
        if miss_info:
            head.append("未检回：")
            for mi in miss_info:
                if mi["state"] == "present":
                    head.append(f"  PMID {mi['pmid']}（记录存在，本次未检回）{mi['title']}")
                elif mi["state"] == "unavailable":
                    head.append(f"  PMID {mi['pmid']} ⚠️ esummary 未找到文档摘要，存在性未确认")
                else:
                    head.append(f"  PMID {mi['pmid']} ⚠️ 元数据缺失，存在性未确认")
            if any(mi['state'] != 'present' for mi in miss_info):
                head.append('存在性未确认的 PMID 仍计入未检回，不计入无法判定。请先核号、重查题录，再判断是否漏了文献。')
        return "\n".join(head)


    async def mesh(self, term: str, retmax: StrictInt | StrictStr = 5, as_json: bool | str = False) -> str:
        """查 MeSH 词表。descriptors 仅含描述符（含树号与上下位词）；other_records 给副主题词、药理作用、补充概念等。
        词表以查询时点 NCBI 返回为准；retmax 默认 5、上限 20（本批候选内精确匹配置顶）。
        历史前身词供扩展旧标引词，不保证补齐；下位词（最多展 10 个）供决定是否展开。
        无描述符命中不等于主题无文献；NCBI 报 phrasesnotfound 时告警。
        """
        json_mode = self.dependencies._as_bool(as_json, "as_json")
        if not (term or "").strip():
            raise self.dependencies.NcbiError("term 不能为空")
        n_req = self.dependencies._int(retmax, "retmax")
        if n_req < 1:
            raise self.dependencies.NcbiError(f"retmax={retmax!r} 要正数；查词给正整数（上限 {self.dependencies.MESH_RETMAX_CAP}）。")
        n = min(n_req, self.dependencies.MESH_RETMAX_CAP)
        clamp_note = "" if n_req <= self.dependencies.MESH_RETMAX_CAP else f"retmax={n_req} 超过本项目自订上限，已夹到 {n}。"
        d = self.dependencies._safe_json(await self.dependencies._get_async("esearch.fcgi",
                                        {"db": "mesh", "term": term, "retmax": n, "retmode": "json"}, 25),
                       "esearch(mesh)")
        er = d.get("esearchresult")
        if not isinstance(er, dict):
            raise self.dependencies.NcbiError("mesh esearch 返回结构异常")
        er["_term"] = term
        count = self.dependencies._count_of(er)
        ids = er.get("idlist")
        if (not isinstance(ids, list) or any(not isinstance(u, str) or not u.isdigit() for u in ids)
                or len(set(ids)) != len(ids) or len(ids) > min(count, n) or (count and not ids)):
            raise self.dependencies.NcbiError("mesh esearch 的 UID 列表缺失、异常或与计数不符")
        qt = (er.get("querytranslation") or "").strip()
        warn = self.dependencies._note(er)
        e_er = er.get("errorlist")
        e_er = e_er if isinstance(e_er, dict) else {}
        if self.dependencies._as_list(e_er.get("phrasesnotfound") or []) or self.dependencies._warn_bits(er)[1]:
            warn.append("⚠️ MeSH 检索片段未找到或被改写；这不能证明片段已被丢弃。"
                        "请核对实际执行式，并尝试单数词形或核心词根重查。")
        if clamp_note:
            warn.append("⚠️ " + clamp_note)
        if not ids:
            note = f"MeSH 库未命中「{term}」的描述符。无描述符不等于主题无文献；请用标题/摘要同义词补充检索。"
            self.dependencies._audit("mesh", term=term, count=count, descriptors=0, querytranslation=qt, warnings=warn)
            if json_mode:
                return json.dumps({"term": term, "count": count, "descriptors": [], "other_records": [], "record_order": [],
                                   "querytranslation": qt, "warnings": warn, "note": note}, ensure_ascii=False)
            return "\n".join([f"查询：{term}", note] + warn)
        s = self.dependencies._safe_json(await self.dependencies._get_async("esummary.fcgi",
                                        {"db": "mesh", "id": ",".join(ids), "retmode": "json"}, 25),
                       "esummary(mesh)")
        res = s.get("result")
        if not isinstance(res, dict):
            raise self.dependencies.NcbiError("mesh esummary 返回结构异常")
        uids = res.get("uids")
        if (not isinstance(uids, list) or any(not isinstance(u, str) for u in uids)
                or set(uids) != set(ids) or len(set(uids)) != len(uids)):
            raise self.dependencies.NcbiError("mesh esummary 返回的 UID 列表与请求不符，不能作为有效词表数据")
        descs, child_want, parent_want = [], [], []
        q_norm = term.strip().lower()
        for uid in uids:
            rec = res.get(uid)
            if not isinstance(rec, dict) or rec.get("error") or not rec.get("ds_meshterms"):
                raise self.dependencies.NcbiError(f"mesh esummary 的 UID {uid} 记录结构异常或不可用，不能作为有效词表数据")
            if "uid" in rec and rec["uid"] != uid:
                raise self.dependencies.NcbiError(f"mesh esummary 的 UID {uid} 与记录内部 uid 不一致，不能作为有效词表数据")
            terms = [str(t) for t in self.dependencies._as_list(rec.get("ds_meshterms") or []) if str(t).strip()]
            idx = self.dependencies._as_list(rec.get("ds_idxlinks") or [])
            kids, pars = [], []
            for x in idx:
                if not isinstance(x, dict):
                    continue
                p_uid = x.get("parent")
                if p_uid is not None and str(p_uid).strip() and str(p_uid) != uid and str(p_uid) not in pars:
                    pars.append(str(p_uid))
                for c in self.dependencies._as_list(x.get("children") or []):
                    c = str(c)
                    if c and c not in kids:
                        kids.append(c)
            prev_idx = [str(x).strip() for x in self.dependencies._as_list(rec.get("ds_previousindexing") or []) if str(x).strip()]
            rtype_raw = self.dependencies._as_list(rec.get("ds_recordtype") or [])
            rtype = str(rtype_raw[0]).strip() if rtype_raw and rtype_raw[0] is not None else ""
            supp = rtype == "supplemental-record"
            mapped = []
            if rtype != 'descriptor':
                pars, kids, idx = [], [], []
            if supp:
                # 补充概念记录（SCR，C 开头 UI）不是描述符：树号是 @ 开头的内部号、上位词是虚根 All MeSH Categories，
                # 都不能用；它真正指向的是 ds_headingmappedtolist 里的描述符〔实测 2026-11-13：Biodentine →
                # tricalcium silicate，[nm] 670 条，写成 [mh] 被改写为 "tricalcium" AND "silicates"[MeSH Terms] 907 条〕。
                pars = []
                mapped = [str(x).strip() for x in self.dependencies._as_list(rec.get("ds_headingmappedtolist") or []) if str(x).strip()]
            name = terms[0] if terms else "( unnamed )"
            exact = q_norm in {t.lower() for t in ([name] + terms)}
            descs.append({
                "uid": uid, "ui": rec.get("ds_meshui") or "",
                # ds_meshterms 首项为主题词名〔实测 P1 一例〕；完整词表都在 entry_terms 里，首项异常也不丢信息。
                "name": name,
                "record_type": rtype,
                "exact_match": exact,
                "entry_terms": terms,
                "treenums": [str(x.get("treenum")) for x in idx
                             if isinstance(x, dict) and x.get("treenum") and not str(x.get("treenum")).startswith("@")],
                "parents": pars[:self.dependencies.MESH_PARENT_SHOW], "parents_total": len(pars),
                "children": kids[:self.dependencies.MESH_CHILD_SHOW], "children_total": len(kids),
                "previous_indexing": prev_idx,
                "scope_note": rec.get("ds_scopenote") or "",
                "year_introduced": rec.get("ds_yearintroduced") or "",
                **({"mapped_to": mapped} if supp else {}),
            })
            child_want.extend(kids[:self.dependencies.MESH_CHILD_SHOW])
            parent_want.extend(pars[:self.dependencies.MESH_PARENT_SHOW] + mapped)
        rel_names = {de["uid"]: de["name"] for de in descs if de["name"] != "( unnamed )"}
        child_warns = []
        want = [u for u in dict.fromkeys(child_want + parent_want) if u not in rel_names]
        for i in range(0, len(want), self.dependencies.ESUMMARY_CHUNK):
            chunk = want[i:i + self.dependencies.ESUMMARY_CHUNK]
            try:
                cs = self.dependencies._safe_json(await self.dependencies._get_async("esummary.fcgi",
                                                 {"db": "mesh", "id": ",".join(chunk), "retmode": "json"}, 25),
                                "esummary(mesh 下位词)")
                cres = cs.get("result")
                if not isinstance(cres, dict) or not isinstance(cres.get("uids"), list):
                    raise self.dependencies.NcbiError("mesh 关联词 esummary 返回结构异常")
                returned = cres["uids"]
                if (any(not isinstance(u, str) for u in returned) or set(returned) != set(chunk)
                        or len(set(returned)) != len(returned)):
                    raise self.dependencies.NcbiError("mesh 关联词 UID 列表与请求不符")
                names = {}
                for cu in returned:
                    cr = cres.get(cu)
                    if (not isinstance(cr, dict) or cr.get("error") or not cr.get("ds_meshterms")
                            or ("uid" in cr and str(cr["uid"]) != cu)):
                        raise self.dependencies.NcbiError("mesh 关联词记录缺失或内部 UID 不符")
                    names[cu] = str(self.dependencies._as_list(cr["ds_meshterms"])[0])
                rel_names.update(names)
            except self.dependencies.NcbiError as e:
                child_warns.append(f"⚠️ 下位词名称获取失败（本批上位词亦仅显示 UID；其他批继续）：{str(e)[:200]}")
        warn.extend(dict.fromkeys(child_warns))
        for de in descs:
            de["children"] = [{"uid": c, "name": rel_names.get(c, "")} for c in de["children"]]
            de["parents"] = [{"uid": p, "name": rel_names.get(p, "")} for p in de["parents"]]
            if "mapped_to" in de:
                de["mapped_to"] = [{"uid": p, "name": rel_names.get(p, "")} for p in de["mapped_to"]]
        exact_desc_i = next((i for i, de in enumerate(descs) if de['exact_match'] and de['record_type'] == 'descriptor'), None)
        exact_any_i = next((i for i, de in enumerate(descs) if de['exact_match']), None)
        exact_other_kind = descs[exact_any_i]['record_type'] if exact_any_i is not None else None
        exact_i = exact_desc_i if exact_desc_i is not None else exact_any_i
        moved_from = None
        if exact_i is not None and exact_i > 0:
            picked = descs.pop(exact_i)
            descs.insert(0, picked)
            moved_from = exact_i + 1
        if exact_desc_i is None and count > len(descs):
            if exact_other_kind is not None:
                # 本页有精确匹配、但类型不是描述符：不能把这条当成 [mh] 可用，也不能把它当成
                # 「没有精确匹配」；剩余条目里可能还有精确描述符。上限内外要给不同建议。
                kind = _mesh_kind_label(exact_other_kind)
                if n < self.dependencies.MESH_RETMAX_CAP:
                    warn.append(f"提示：本页有字面精确匹配，但类型是「{kind}」而非描述符；"
                                f"前 {len(descs)} 个中无精确描述符。MeSH 共命中 {count} 条，"
                                f"可增大 retmax（上限 {self.dependencies.MESH_RETMAX_CAP}）查看后续条目是否含精确描述符。")
                else:
                    warn.append(f"提示：本页有字面精确匹配，但类型是「{kind}」而非描述符；"
                                f"前 {len(descs)} 个（已到上限 {self.dependencies.MESH_RETMAX_CAP}）中无精确描述符；"
                                f"后续条目本工具取不到，请换更具体的词重查。")
            elif n < self.dependencies.MESH_RETMAX_CAP:
                warn.append(f"提示：MeSH 共命中 {count} 条记录，本次前 {len(descs)} 个中无字面精确匹配；"
                            f"可增大 retmax（上限 {self.dependencies.MESH_RETMAX_CAP}）查看后续条目。")
            else:
                # 已在上限还叫人「增大 retmax」是做不到的建议〔实测 2026-11-13：resorption retmax=50 → 夹到 20 后仍这么说〕。
                warn.append(f"提示：MeSH 共命中 {count} 条记录，本次前 {len(descs)} 个（已到上限 {self.dependencies.MESH_RETMAX_CAP}）中"
                            f"无字面精确匹配；后续条目本工具取不到，请换更具体的词重查。")
        descriptors = [de for de in descs if de['record_type'] == 'descriptor']
        others = [de for de in descs if de['record_type'] != 'descriptor']
        self.dependencies._audit('mesh', term=term, count=count, descriptors=len(descriptors), other_records=len(others), querytranslation=qt, warnings=warn)
        if json_mode:
            return json.dumps({'term': term, 'count': count, 'descriptors': descriptors, 'other_records': others,
                               'record_order': [de['uid'] for de in descs], 'querytranslation': qt, 'warnings': warn}, ensure_ascii=False)
        n_supp = sum(1 for de in others if 'mapped_to' in de)
        if not others and len(descs) == count:
            head = [f'查询：{term}', f'MeSH 命中 {count} 个描述符，本次给 {len(descs)} 个']
        else:
            suffix = f'，其中 {n_supp} 条是补充概念' if n_supp else ''
            head = [f'查询：{term}', f'MeSH 命中 {count} 条记录，本次给 {len(descs)} 条（描述符 {len(descriptors)} 条{suffix}）']
        head += warn
        for de in descs:
            head.append("")
            badge = ""
            if moved_from and de is descs[0]:
                badge = f" [精确匹配，已由第 {moved_from} 位置顶]"
            elif de["exact_match"] and len(descs) > 1:
                badge = " [精确匹配]"
            kind = _mesh_kind_label(de['record_type'])
            head.append(f"MeSH {de['ui']}｜{de['name']}（{kind}）"
                        + (f"（{de['year_introduced']} 年引入）" if de["year_introduced"] else "")
                        + badge + ("（补充概念记录，非主题词：检索用「名称[nm]」，写成 [mh] 会被 NCBI 改写）"
                                   if "mapped_to" in de else ""))
            head.append("  入口词：" + "; ".join(de["entry_terms"]))
            if de.get("mapped_to"):
                head.append("  映射主题词：" + "; ".join((f"{p['name']} (UID {p['uid']})" if p["name"] else f"UID {p['uid']}")
                                                    for p in de["mapped_to"]))
            if de["treenums"]:
                head.append("  树号：" + "; ".join(de["treenums"]))
            if de["parents"]:
                pshown = "; ".join((f"{p['name']} (UID {p['uid']})" if p["name"] else f"UID {p['uid']}")
                                   for p in de["parents"])
                pmore = f"…共 {de['parents_total']} 个" if de["parents_total"] > len(de["parents"]) else ""
                head.append("  上位词：" + pshown + pmore)
            if de["children"]:
                shown = "; ".join((f"{c['name']} (UID {c['uid']})" if c["name"] else f"UID {c['uid']}")
                                  for c in de["children"])
                more = f"…共 {de['children_total']} 个" if de["children_total"] > len(de["children"]) else ""
                head.append("  下位词：" + shown + more)
            if de["previous_indexing"]:
                head.append("  历史前身词（previous indexing）：" + "; ".join(de["previous_indexing"])
                            + "（检索引入年份之前的文献需补旧主题词）")
            if de["scope_note"]:
                head.append("  范围注：" + de["scope_note"])
        head.append("")
        head.append("说明：词表以查询时点 NCBI 返回为准；MeSH 覆盖不全，无描述符的概念请用标题/摘要同义词补充。")
        return "\n".join(head)


    async def diff(self, term_a: str, term_b: str, max_show: StrictInt | StrictStr = 20,
                   as_json: bool | str = False, preview: bool | str = False, field: str = '',
                   mindate: str = '', maxdate: str = '', reldate: StrictInt | StrictStr = 0,  # 本项目自订：默认不限相对日期
                   datetype: Literal['pdat', 'edat'] = 'pdat') -> str:
        """对比 b 相对 a 增减的 PMID。显式传入原搜索的字段和日期限制，两侧每页共用。
        每侧须在9999条窗口内〔工具口径〕，超窗请缩小或用export。JSON给完整名单。
        max_show限文本展示及可选preview题录数（默认20、上限200〔工具口径〕）。预览按结果顺序。
        看 provenance.comparison：compared 的 added 仅为当次 b 有 a 无；needs_review 保留实取名单，不作改式归因。
        removed非数据库删除，日期变化影响差集。新增须读摘要确认相关性。
        """
        json_mode = self.dependencies._as_bool(as_json, "as_json")
        preview_mode = self.dependencies._as_bool(preview, "preview")
        ms_req = self.dependencies._int(max_show, "max_show")
        if ms_req < 0:
            raise self.dependencies.NcbiError(f"max_show={max_show!r} 不能为负；不想看列表给 0。")
        ms = min(ms_req, self.dependencies.DIFF_SHOW_CAP)
        scope = self._review_scope(field, mindate, maxdate, reldate, datetype)
        sides = [None, None]
        side_errs = [None, None]
        async with anyio.create_task_group() as tg:
            async def fetch_side(idx, q, ctx):
                try:
                    sides[idx] = await self.dependencies._ids_in_window(q, context=ctx, **scope)
                except self.dependencies.NcbiError as e:
                    side_errs[idx] = e
                    tg.cancel_scope.cancel()
            tg.start_soon(fetch_side, 0, term_a, "diff term_a：")
            tg.start_soon(fetch_side, 1, term_b, "diff term_b：")
        for e in side_errs:
            if e is not None:
                raise e
        a_ids, a_count, a_first, a_drift = sides[0]
        b_ids, b_count, b_first, b_drift = sides[1]
        sa, sb = set(a_ids), set(b_ids)
        added = [p for p in b_ids if p not in sa]
        removed = [p for p in a_ids if p not in sb]
        common = len(sa & sb)
        warn_a = self.dependencies._note(a_first, include_translation=not json_mode)
        warn_b = self.dependencies._note(b_first, include_translation=not json_mode)
        for first, warnings in ((a_first, warn_a), (b_first, warn_b)):
            for check in first.get("_retrieval_checks", [])[1:]:  # 本项目自订：首项已有 _note 输出
                warnings.extend(check["warnings"])
        if a_drift:
            warn_a.append(a_drift)
        if b_drift:
            warn_b.append(b_drift)
        qt_a = (a_first.get("querytranslation") or "").strip()
        qt_b = (b_first.get("querytranslation") or "").strip()
        caveat = ("集合差只描述增减，不判断相关性；命中更多不等于更好。未再命中（removed）只是 a 有、b 没有，"
                  "不代表文献被数据库删除；若 b 仅覆盖补检入库区间，区间外旧文献也会列入 removed。"
                  "两次查询间数据库可能更新，结果以取回时点为准。")
        # 先判两份取号名单是否适合归因；标题能否取回不改变 PMID 差集。
        def side_needs_review(first, drift, ids, count, translation):
            return (bool(drift) or len(set(ids)) != count or not translation
                    or not first.get('_retrieval_checks')
                    or any(check.get('query_needs_review', True) or check['querytranslation'] != translation
                           for check in first['_retrieval_checks']))

        review_pages = []
        for first, qt in ((a_first, qt_a), (b_first, qt_b)):
            for page in first.get('_retrieval_checks', []):
                reasons = list(page.get('review_reasons', []))
                if page['querytranslation'] != qt:
                    reasons.append('执行式与同侧首页不同')
                if reasons:
                    review_pages.append({k: page[k] for k in ('stage', 'querytranslation')} | {'reasons': reasons})
        compare_notes = []
        if scope['reldate']:
            def relative_window(first):
                checks = first.get('_retrieval_checks') or []
                return checks[0].get('relative_window') if checks else None
            a_window, b_window = relative_window(a_first), relative_window(b_first)
            if a_window != b_window:
                compare_notes.append(f'两侧相对日期窗口不一致：a={a_window}，b={b_window}')
        if (added or removed) and (term_a.strip() == term_b.strip() or (qt_a and qt_a == qt_b)):
            compare_notes.append('输入或实际执行式相同，但名单不同；不能归因于改式')
        uncertain = (bool(compare_notes) or side_needs_review(a_first, a_drift, a_ids, a_count, qt_a)
                     or side_needs_review(b_first, b_drift, b_ids, b_count, qt_b))
        origin = {"source": "diff", "comparison": "needs_review" if uncertain else "compared",
                  "note": ("来源：实取名单差异；执行式、范围或分页需复核，不能归为改式新增。"
                           if uncertain else "来源：当次两份名单已比对；added 仅表示 b 有、a 无，不证明差异由改式造成。")}
        if compare_notes:
            origin['reasons'] = compare_notes
        if review_pages:
            origin['review_pages'] = review_pages
        previews = {}
        if preview_mode:
            for label, ids, warnings in (("added", added, warn_b), ("removed", removed, warn_a)):
                selected = ids[:ms]
                records, error = [], ""
                if selected:
                    try:
                        pairs = await self.dependencies._esummary_dicts(selected)
                        by_id = {uid: self.dependencies._brief_record(uid, record) for uid, record in pairs}
                        records = [by_id[uid] for uid in selected if uid in by_id]
                    except self.dependencies.NcbiError as exc:
                        error = self.dependencies._mask(str(exc))
                shown_ids = {rec['pmid'] for rec in records}
                missing = [uid for uid in selected if uid not in shown_ids]
                if missing:
                    recovery, hint = self._missing_citations(missing)
                    warnings.append('⚠️ ' + label + ' 标题预览不完整；集合差 PMID 保留。' + hint
                                    + (' 原因：' + error if error else ''))
                previews[label] = {"requested_pmids": selected, "records": records,
                                   **({'metadata_recovery': recovery} if missing else {}),
                                   "missing_pmids": missing, "not_previewed_count": len(ids) - len(selected),
                                   "error": error}
            previews["note"] = "仅按结果顺序预览每侧前 max_show 条，不是随机样本或相关性排名；题名不能代替摘要筛选。"
        self.dependencies._audit("diff", term_a=term_a, term_b=term_b, scope=scope, count_a=a_count, count_b=b_count,
               added=len(added), removed=len(removed), common=common,
               querytranslation_a=qt_a, querytranslation_b=qt_b, provenance=origin,
               warnings_a=warn_a, warnings_b=warn_b)
        if json_mode:
            return json.dumps({"term_a": term_a, "term_b": term_b, "scope": scope, "count_a": a_count, "count_b": b_count,
                               "provenance": origin, "added_count": len(added), "removed_count": len(removed), "common_count": common,
                               "added": added, "removed": removed,
                               "querytranslation_a": qt_a, "querytranslation_b": qt_b,
                               "warnings_a": warn_a, "warnings_b": warn_b, "caveat": caveat,
                               **({"preview": previews} if preview_mode else {})}, ensure_ascii=False)
        head = [self._scope_note(scope), f"term_a：{term_a}", f"  命中 {a_count}，取回 {len(a_ids)}"]
        head += ["  " + w for w in warn_a]
        head += [f"term_b：{term_b}", f"  命中 {b_count}，取回 {len(b_ids)}"]
        head += ["  " + w for w in warn_b]
        head.append('执行式、范围或分页需复核；目前只报告实取差异。复核后也只能确认名单差，不能凭名单归因于改式。' if uncertain
                    else '已比对当次取回的两份名单；差异不证明由改式造成。')
        head.extend('⚠️ ' + reason for reason in compare_notes)
        head.extend(f"⚠️ {page['stage']}：{'；'.join(page['reasons'])}；实际执行：{page['querytranslation'] or '未返回'}" for page in review_pages)
        add_label = "实取仅 b 有" if uncertain else "当次仅 b 有"
        remove_label = "实取仅 a 有" if uncertain else "当次仅 a 有"
        head.append(f"{add_label} {len(added)} 条；{remove_label} {len(removed)} 条；共同 {common} 条")
        if preview_mode:
            head.append(previews["note"])
            for label, title, ids in (("added", add_label, added), ("removed", remove_label, removed)):
                item = previews[label]
                head.append(f"{title}标题预览（请求 {len(item['requested_pmids'])}/{len(ids)}，其余未预览 {item['not_previewed_count']}）")
                head.extend(self.dependencies._fmt_brief_record(rec, rec["pmid"]) for rec in item["records"])
                if item["missing_pmids"]:
                    head.append("⚠️ 预览元数据未取回，PMID 保留：" + ", ".join(item["missing_pmids"]))
        elif ms:
            if added:
                head.append(f"{add_label}（展示 {min(ms, len(added))}/{len(added)}）：" + ", ".join(added[:ms]))
            if removed:
                head.append(f"{remove_label}（展示 {min(ms, len(removed))}/{len(removed)}）：" + ", ".join(removed[:ms]))
        head.append('名单不判断相关性，须读摘要；数据库可能在两次取号之间更新。')
        if removed:
            head.append('仅 a 有不代表文献被数据库删除；若 b 只搜补检日期，区间外的旧文献也会在此列。')
        return "\n".join(head)


    async def _snowball(self, seeds_path, baseline_path, out):
        """单轮 refs/citedin；保存全部关系与扣除材料，可离线复算。"""
        target = self.dependencies._check_out_path(out)
        if not target:
            raise self.dependencies.NcbiError("out 不能为空")
        seeds = await anyio.to_thread.run_sync(self.dependencies._read_pmid_file, seeds_path)
        baseline = await anyio.to_thread.run_sync(
            functools.partial(self.dependencies._read_pmid_file, baseline_path, baseline=True))
        if not seeds["ids"]:
            raise self.dependencies.NcbiError("种子清单为空")
        sources, edges, errors = [], [], []
        child_audit_failures, child_warnings = [], []
        for direction in ("refs", "citedin"):
            for start in range(0, len(seeds["ids"]), 50):  # 本项目自订：沿用 links 单次源上限
                batch = seeds["ids"][start:start + 50]
                try:
                    result = json.loads(await self.dependencies.links(" ".join(batch), to=direction, as_json=True))
                    if result.get("audit_status") == "failed":
                        child_audit_failures.append({"direction": direction, "seeds": batch,
                                                     "error": result.get("audit_error", self.dependencies._AUDIT_UNKNOWN)})
                    child_warnings.extend(result.get("warnings", []))
                    sources.extend(result["sources"])
                    edges.extend(result["edges"])
                    errors.extend({"direction": direction, "seeds": [source["source"]], "error": source["error"]}
                                  for source in result["sources"] if source.get("error"))
                except self.dependencies.NcbiError as exc:
                    audit_failure = getattr(exc, "audit_failure", None)
                    if audit_failure:
                        child_audit_failures.append({"direction": direction, "seeds": batch,
                                                     "error": audit_failure["error"]})
                    message = self.dependencies._mask(str(exc))
                    per_source = getattr(exc, 'link_sources', None)
                    if per_source is not None:
                        sources.extend(per_source)
                        errors.extend({'direction': direction, 'seeds': [s['source']], 'error': s['error']}
                                      for s in per_source)
                    else:
                        errors.append({"direction": direction, "seeds": batch, "error": message})
                        sources.extend({"source": pid, "source_status": "unknown", "relation": direction,
                                        "relation_status": "error", "target_ids": [], "error": message,
                                        "fetched_at": self.dependencies._utc_now()} for pid in batch)
                if len(edges) > self.dependencies.EXPORT_MAX:
                    raise self.dependencies.NcbiError("滚雪球来源边超过本项目安全上限；请减少种子后重跑，未发布候选文件")
        successful = [source for source in sources if not source.get("error")]
        if not successful:
            raise self.dependencies.NcbiError("滚雪球全部来源请求失败，未生成候选文件")
        raw = list(dict.fromkeys(edge["target"] for edge in edges))
        seed_set, old_set = set(seeds["ids"]), set(baseline["ids"])
        removed = [{"pmid": pid, "reasons": (["seed"] if pid in seed_set else []) +
                    (["baseline"] if pid in old_set else [])} for pid in raw if pid in seed_set or pid in old_set]
        candidates = [pid for pid in raw if pid not in seed_set and pid not in old_set]
        titles, metadata_errors = {}, []
        for start in range(0, len(candidates), self.dependencies.ESUMMARY_CHUNK):
            batch = candidates[start:start + self.dependencies.ESUMMARY_CHUNK]
            try:
                pairs = await self.dependencies._esummary_dicts(batch)
                titles.update({pid: rec.get("title", "") for pid, rec in pairs})
            except self.dependencies.NcbiError as exc:
                metadata_errors.append({"pmids": batch, "error": self.dependencies._mask(str(exc))})
        warnings = list(dict.fromkeys(child_warnings))
        if child_audit_failures:
            warnings.append("⚠️ 部分关联调用留痕失败；父事件写入成功不代表子调用日志完整。")
        audit_details = {"child_audit_failures": child_audit_failures, "warnings": warnings}
        result = {**audit_details, "schema": "pubmed.snowball.v1", "candidate_pmids": candidates,
                  "seeds": seeds, "baseline": baseline, "sources": sources, "edges": edges,
                  "before_exclusion": raw, "excluded": removed, "titles": titles,
                  "title_missing": [pid for pid in candidates if not titles.get(pid)],
                  "source_errors": errors, "metadata_errors": metadata_errors,
                  "completed_at": self.dependencies._utc_now(), "count": len(candidates),
                  "evidence": "来源：本次 PubMed 关联与题录响应；只追种子一轮，候选不代表纳入，关系覆盖有限"}
        p, nbytes, sha = await anyio.to_thread.run_sync(
            functools.partial(self.dependencies._write_json_new, target, result, indent=None))
        self.dependencies._audit("snowball", path=p, sha256=sha, count=len(candidates), sources=sources,
               seed_sha256=seeds["sha256"], baseline_sha256=baseline["sha256"], **audit_details)
        return {**audit_details, "path": p, "bytes": nbytes, "sha256": sha, "count": len(candidates),
                "source_errors": errors, "metadata_errors": metadata_errors,
                "warning": "PubMed 引文覆盖有限；只生成待筛候选"}

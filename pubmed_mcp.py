#!/usr/bin/env python
"""自建 PubMed MCP 服务：裸打 NCBI eutils，带 api_key、超时、退避、限速，失败绝不静默。

依赖版本（实测 2026-11-08，本机 importlib.metadata）：mcp 1.29.0 / anyio 4.14.2 / pydantic 2.13.4
  本文件把工具写成 async def 并把网络调用挪进线程，依据是 mcp SDK 的实现细节：
  mcp/server/fastmcp/utilities/func_metadata.py 里非 async 分支是裸 `return fn(**kwargs)`，
  没有 threadpool —— 也就是 sync 工具会占住事件循环，一次 40s 的 efetch 能把整个 server 冻住。
  SDK 升级后该分支若改成 threadpool，本文件的 async 写法依然正确，不必回退。

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
  6. 网络调用出事件循环 + 按有无 key 限速（实测：有 key 走 10 次/秒，无 key 走 3 次/秒）。

用法：
    python pubmed_mcp.py                       # 当 MCP stdio 服务（Claude 用这个）
    python pubmed_mcp.py --selftest 牙根吸收     # 不走 MCP，直接打印结果，自检用
    python pubmed_mcp.py --check-key            # 只报告 key/限速档位，不发查询
    python pubmed_mcp.py --probe                # 跑一组断言，回归用
    #   〔实测 2026-11-10，给 _get_async 包计数器量的〕**121 条断言用 88 次 HTTP 请求**
    #   （同一天 98 条那次是 71 次）；旧注释写「约 10 次请求」是按节数拍的，差了 7-8 倍。
    #   按 10 次估限速会估错一个量级——一轮探针要按【几十秒到几分钟】算。

key 读取顺序：环境变量 NCBI_API_KEY → 文件 ~/.ncbi_api_key → 都没有就走匿名（3 次/秒）
依赖：Python 标准库 + mcp(FastMCP)；anyio 随 mcp 一起装。
"""

import functools
import calendar
import datetime
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import anyio

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


class NcbiError(RuntimeError):
    """请求层面的失败。和「PubMed 里没有这篇文章」严格区分开。"""


_KEY = None


def api_key():
    """缓存一次读取 —— 旧版每个请求都 stat+读文件。"""
    global _KEY
    if _KEY is None:
        k = (os.environ.get("NCBI_API_KEY") or "").strip()
        if not k:
            for p in ("~/.ncbi_api_key", "~/.ncbi_api_key.txt"):
                q = os.path.expanduser(p)
                if os.path.isfile(q):
                    k = io.open(q, encoding="utf-8").read().strip()
                    break
        _KEY = k
    return _KEY


def _mask(s):
    """异常文本里不要带出 api_key。"""
    k = api_key()
    return s.replace(k, k[:4] + "***") if (k and len(k) > 8 and isinstance(s, str)) else s


def _safe_json(text, what):
    """NCBI 故障时可能回 200 + HTML。裸 json.loads 会抛 JSONDecodeError，
    绕过本文件「失败带 NCBI 原文」的承诺，所以包一层。
    顶层 ERROR 键也在这里拦：实测 elink 的 JSON 模式不支持 cmd 参数时回的是
    HTTP 200 + {"linksets": [], "ERROR": "Command \\"links\\" is not supported"} ——
    只看 linksets 为空就会把「我用错了参数」说成「NCBI 里没有对应记录」。"""
    try:
        d = json.loads(text)
    except Exception as e:
        # 〔实测 2026-11-08〕NCBI 的 ERROR 正文里带真换行（retstart 越限时那句
        # "'retstart' cannot be larger than 9998…"），strict=True 会报
        # "Invalid control character at: line 1 column 105"，于是旧版把一条【本来写明了原因】
        # 的响应说成「收到的不是合法 JSON」——错文把调用方往服务端坏了的方向指。
        # 先试 strict=False，能解析就继续往下走，把 NCBI 自己那句话原样递出去。
        try:
            d = json.loads(text, strict=False)
        except Exception:
            raise NcbiError(f"{what} 收到的不是合法 JSON（{type(e).__name__}）；正文前 300 字：{text[:300]!r}")
    if isinstance(d, dict):
        esr = d.get("esearchresult")
        err = d.get("ERROR") or (d.get("header") or {}).get("error")
        if not err and isinstance(esr, dict):
            err = esr.get("ERROR")      # retstart 越限这类错误就藏在这里，而且没有 count
        if err:
            raise NcbiError(f"{what} 被 NCBI 拒绝：{str(err)[:400]}")
    return d


# ---------- 限速 ----------
class _Pacer:
    def __init__(self, rate):
        self.interval = 1.0 / rate
        self._next = 0.0
        self._lock = anyio.Lock()

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval
            delay = slot - now
        if delay > 0:
            await anyio.sleep(delay)


_PACER = None
_PACER_INIT = threading.Lock()


async def _pace():
    global _PACER
    with _PACER_INIT:
        if _PACER is None:
            _PACER = _Pacer(10.0 if api_key() else 3.0)   # 实测本机有 key
    await _PACER.wait()


# ---------- HTTP ----------
def _retry_after(headers):
    ra = headers.get("Retry-After") if headers else None
    if not ra:
        return None
    ra = str(ra).strip()
    if ra.isdigit():
        return float(ra)
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        return max(0.0, (parsedate_to_datetime(ra) - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None                                       # HTTP-date 解析不了就退回固定退避


def _get(path, params, timeout, tries=4):
    """带 key 和退避重试的 GET，返回响应文本。失败抛 NcbiError 并带 NCBI 原文。
    同步函数，只允许在 _get_async 的线程里调用。"""
    q = dict(params)
    q["tool"] = TOOL
    if EMAIL:
        q["email"] = EMAIL
    key = api_key()
    if key:
        q["api_key"] = key
    url = f"{BASE}/{path}?" + urllib.parse.urlencode(q)
    # 失败文本里必须交代本次有没有带 key：〔实测 2026-11-08〕key 写错时 NCBI 回的是
    # HTTP 400 + {"error":"API key invalid"}（拿 'zzz' 和 32 位假 key 各试一次都是这个形状），
    # 不写这句就会让人去查 NCBI 是不是挂了。
    keyhow = (f"本次带上 api_key（前 4 位 {key[:4]}…，长度 {len(key)}）" if key
              else "本次【没带】api_key（走匿名 3 次/秒）")
    last = "没试过"
    backoff = (1, 3, 9)
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": TOOL})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")[:300].strip()
            except Exception:
                body = ""
            last = f"HTTP {e.code}；NCBI 原文：{body or '(空响应体)'}"
            if e.code in (429, 500, 502, 503, 504):
                if attempt == tries - 1:
                    break                                  # 最后一次不白等（旧版会多睡 9s）
                time.sleep(_retry_after(e.headers) or backoff[min(attempt, 2)])
                continue
            raise NcbiError(_mask(f"{path} 失败：{last}（{keyhow}）"))   # 400/414 这种重试没意义
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if attempt == tries - 1:
                break
            time.sleep(backoff[min(attempt, 2)])
    raise NcbiError(_mask(f"{path} 试了 {tries} 次仍失败：{last}（{keyhow}）"))


async def _get_async(path, params, timeout, tries=4):
    await _pace()
    return await anyio.to_thread.run_sync(
        functools.partial(_get, path, params, timeout, tries))


# ---------- eutils ----------
def _as_bool(v, name):
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
    raise NcbiError(f'{name} 要布尔，收到 {v!r}（可写 true/false，也容忍 "True"/"1"/"yes"）')


def _int(v, name, default=0, lo=None, hi=None):
    """数字参数统一收口：报错要说清是哪个参数，别抛裸 ValueError('invalid literal for int()')。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise NcbiError(f"{name} 要整数，收到 {v!r}")
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


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


def _check_sort(sort):
    s = (sort or "").strip()
    if not s:
        return ""
    canon = SORT_WHITELIST.get(s.lower())
    if canon is None:
        raise NcbiError(
            f"sort={sort!r} 不是本工具接受的排序值。实测 NCBI 对认不出的 sort 是【静默接受】的："
            f'sort="journal_quality" 返回的顺序与不传 sort 完全一致，看着像排过了其实没有。'
            f'可用值：pub_date / relevance / author。'
            f'（{"/".join(SORT_IGNORED)} 这几个 NCBI 文档里出现过的值，实测顺序与不传时【逐字相同】，'
            f'所以不放进白名单，免得你以为排过了。）'
            f'要按期刊指标排序请自己接外部期刊指标数据源（PubMed 没有这个概念）。')
    return canon


_DATE_RE = re.compile(r"^\d{4}([/-]\d{1,2}){0,2}$")

# 〔实测 2026-11-08〕field= 传这三个值时 NCBI 整条忽略限定：count 与 querytranslation
# 与不传 field 时【逐字相同】（miniscrew 三种都回 2144、qt 里根本没有 [Keywords]/[ISSN]/[PMCID]）。
# 而 field=MeSH Terms / Publication Type 更糟：esearch 干脆不返回 count。
# 这五个与其静默给一个「不限定」的数，不如直接拦下来告诉调用方该写 term 里的标签。
FIELD_SWALLOWED = {"Keywords", "PMC Number", "ISSN"}
FIELD_NO_COUNT = {"MeSH Terms", "Publication Type"}


def _check_date(v, name):
    """拦两种：写法不对（格式正则）、写法对但日历上不存在这一天。
    后者是这轮实测新发现的：mindate=2023-02-29（2023 年没有 2 月 29 日）NCBI 【不报错、
    errorlist 空、也不做词翻译】，整条查询直接 0 命中，而输出里那行「日期条件」看着完全正常。
    2020-02-29（真闰日）同参数是 413 条，所以这不是「那年没文献」，是我塞了个不存在的日子。
    同理想办法：13 月、2 月 30 日、00 月一律日历判掉；年份超出收录范围（1000/2099）NCBI 照常工作，不拦。"""
    s = str(v or "").strip()
    if not s:
        return ""
    if not _DATE_RE.match(s):
        raise NcbiError(f"{name}={v!r} 不是 NCBI 认的日期写法（用 2020、2020/01、2020-01-01；"
                        f"要「近 N 天」请用 reldate）")
    p = [x.zfill(2) for x in s.replace("/", "-").split("-")]
    try:
        datetime.date(int(p[0]), int(p[1]) if len(p) > 1 else 1, int(p[2]) if len(p) > 2 else 1)
    except ValueError:
        raise NcbiError(f"{name}={v!r} 格式像日期但日历上没有这一天 —— 实测这种值 NCBI 不报错、"
                        f"不返回 errorlist，整条查询静默变成 0 命中（例如 2023-02-29：2023 年没有 2 月 29 日）。"
                        f"要 2023 年就写 2023-01-01。")
    return "-".join(p)


def _expand(v, end=False):
    """把 2021 / 2021-06 摊成真正的起止日。NCBI 自己就是这么展开的
    〔实测 2026-11-08：maxdate=2021 的 qt 尾段是 2021-01-01:2021-12-31〕。"""
    if not v:
        return ""
    p = v.split("-")
    y = int(p[0])
    mo = int(p[1]) if len(p) > 1 else (12 if end else 1)
    d = int(p[2]) if len(p) > 2 else (calendar.monthrange(y, mo)[1] if end else 1)
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _today():
    """本机日历的今天。〔状态 2026-11-10：日期逻辑已经不用它了〕
    reldate 改为原样下发、单边日期改为补哨兵（NO_BEGIN/NO_END）之后，本工具不再需要本机日历；
    留着是因为「本机日历 ≠ NCBI 服务器日历」这件事本身就是一次实测记录（见 _windows 的 docstring），
    而且这里不是 git 仓库，删掉就找不回来了。
    ⚠️ 别把它再拿来当日期边界用：〔实测 2026-11-09〕本机比服务器领先一天时，
    用它当上界会静默切掉边界那一天的文献（dental implant 770 → 763，少的 7 条＝那一天）。"""
    return datetime.date.today().strftime("%Y-%m-%d")


# 哨兵日期。〔实测 2026-11-09〕这两个值 NCBI 都照常接受并原样回显在 querytranslation 里：
#   "tooth" + 0001-01-01:1800-01-01 → 12 条（旧版补 1809-01-01 会把这 12 条静默切掉）
#   "dental implant" + 2026-09-10:2999-12-31 → 807 条（补「今天」2026-11-09 只有 763 条）
# 「不限某一边」就该发一个不可能生效的界，而不是发「今天/收录起点」——后者是个真边界。
NO_BEGIN = "0001-01-01"
NO_END = "2999-12-31"
RELDATE_MAX = 36500          # ≈100 年；再大的 reldate 没有实际意义，且旧版会抛裸 OverflowError
RETSTART_MAX = 9998          # 实测 NCBI："'retstart' cannot be larger than 9998"


_WIN_RE = re.compile(r"(\d{4})[-/](\d{2})[-/](\d{2})(?::(\d{4})[-/](\d{2})[-/](\d{2}))?\[([^\]\[]*)\]")
# NCBI 回显里的日期字段名 → 人话
_WIN_LABEL = {"Date - Publication": "发表日期[pdat]",
              "Date - Entry": "入库日期[edat]",
              "Date - Modification": "修改日期[mdat]"}


def _windows(qt):
    r"""从 NCBI 回显的 querytranslation 里取出真正执行过的日期窗口，返回 [(起, 止, 字段名)]。
    （可能不止一段，因为 reldate 与 mindate/maxdate 是两段 AND。）

    为什么从回显读、不在本机算〔实测 2026-11-09〕：reldate 由 NCBI 按【它的服务器日历】算，
    本机日历可能已领先一天，本机换算出来的「近 N 天」与 NCBI 实际执行的不是同一段。

    回显里的日期分隔符是斜杠（U+002F），不是横杠〔实测 2026-11-10：用 ord() 逐字符量，
    窗口形状是 `2026/09/11:2026/11/10[Date - Publication]`；同一条串里 45（横杠）
    只出现在 `[Date - Publication]` 这类标签里〕。所以正则必须收 `[-/]`，只认横杠的正则
    在真实回显上 findall 得 0 个〔实测：dash-only 对 live qt = 0，对同一串 `[-/]` = 1〕。

    ⚠️ 测量坑（2026-11-10 我自己踩的）：本会话的工具输出/Read 渲染会把斜杠显示成横杠 ——
    同一串用 repr() 与 unicode_escape 印出来看着像横杠（`2026-09-11`），而 ord() 列表与
    「dash-only 正则命中 0」两条都证明它是斜杠。我据此一度认定「斜杠那句旧注释是假的」，
    差点把对的注释改成错的。要分清这两种形状别用肉眼：
        sorted({ord(c) for c in t if c in "-/"})
    或数 `re.findall(r"\d{4}-\d{2}-\d{2}", t)` 的命中数（0 ＝ 串里是斜杠）。

    第三个元素（字段名）为什么要有〔实测 2026-11-10〕：同一条回显里的两段日期可以挂在【不同字段】上
      "2020:2020[edat] AND implant" 配 reldate=60 → qt 回显两段：
        2020/01/01:2020/12/31[Date - Entry] 与 2026/09/11:2026/11/10[Date - Publication]
      一段数入库日、一段数发表日。把这两段的端点比大小再说「两段日期不相交」是拿不相干的两个轴
      比距离，所以判据只在【同一字段】的两段之间比 —— 见 _note 与 _zero_explanation。
    单日窗口也要认〔实测 2026-11-10〕：`2026-11-08[dp]` 的回显只有一天
      （`2026/11/08[Date - Publication]`，冒号与后半截整个不存在）；只认 `A:B[` 形状的正则
      对它返回空列表，于是「NCBI 没回显窗口」那条提醒会被误触发 —— 那正是
      「把没查到的东西写成结论」的形状。单日窗口的起止都填这一天。"""
    out = []
    for m in _WIN_RE.finditer(str(qt or "")):
        g = m.groups()
        lo = f"{g[0]}-{g[1]}-{g[2]}"
        hi = f"{g[3]}-{g[4]}-{g[5]}" if g[3] else lo        # 单日：回显没有后半截
        lab = (g[6] or "").strip()
        out.append((lo, hi, _WIN_LABEL.get(lab, lab or "未知字段")))
    return out


def _same_label_pairs(wins):
    """从 [(起, 止, 字段名)] 里产出【字段名相同】的窗口两两组合。

    为什么要挑：〔实测 2026-11-10〕`"2020:2020[edat] AND implant"` 配 reldate=60 时
      NCBI 回显两段 —— 2020 全年〔入库日期〕 与 近 60 天〔发表日期〕。
    旧写法直接拿 wins[0] 与 wins[1] 比大小，会说「两段日期不相交」，可这两段量的根本不是
    同一个轴（一条文献可以 2020 年入库、多年后改题录，也可以 2026 年发表），
    那句话是无依据的断言。现在只有同字段的两段才可能触发这条判据。
    注：reldate 那一段永远是我们自己强制 datetype=pdat 发出去的（见 _esearch），
    所以「用户 term 里写 [dp] 区间 + reldate」这种真的会互斥的组合仍然判得到。"""
    out = []
    for i in range(len(wins)):
        for j in range(i + 1, len(wins)):
            if wins[i][2] == wins[j][2]:
                out.append((wins[i], wins[j]))
    return out


def _check_reldate(v):
    """reldate 只收 0 或 1..RELDATE_MAX。旧写法 `_int(reldate,"reldate",0,0)` 上界不管、
    下界被夹成 0，两条都是静默的：〔实测 2026-11-09〕
      reldate=1000000000 → datetime 抛裸 OverflowError（不是 NcbiError，而且请求已经发出去了才炸）；
      reldate=-30        → 夹成 0 ＝不限日期，313615 条照回，一个字都没提醒。"""
    n = _int(v, "reldate")
    if n < 0:
        raise NcbiError(f"reldate={v!r} 是负数。旧版会把它静默夹成 0（＝完全不限日期），"
                        f"所以这里直接拦：要「最近 N 天」给正数，要「某年以前」请用 maxdate。")
    if n > RELDATE_MAX:
        raise NcbiError(f"reldate={n} 太大（上限 {RELDATE_MAX} 天 ≈ {RELDATE_MAX // 365} 年）。"
                        f"旧写法这种值会在算日期时抛 OverflowError；要「某某年以来」请直接用 mindate。")
    return n


async def _esearch(term, retmax=20, sort="", field="", mindate="", maxdate="", reldate=0, retstart=0):
    if not (term or "").strip():
        raise NcbiError("term 不能为空（空 term 会被 NCBI 当特殊输入处理，count 字段直接消失）")
    f = _check_field(field)
    srt = _check_sort(sort)
    rs = _int(retstart, "retstart", 0, 0)
    if rs > RETSTART_MAX:
        raise NcbiError(f"retstart={rs} 超出 PubMed esearch 的检索窗口。〔实测 2026-11-09〕"
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
        raise NcbiError(f"retmax={retmax!r} 是负数，本工具不替它编一个意思。"
                        f"要几条就写正数（上限 {RETMAX_CAP}）；只想知道命中数就用 count()。")
    _rm = _int(retmax, "retmax", 0, 0, RETMAX_CAP)
    _rm_note = ("" if _rm_req is None or _rm_req <= RETMAX_CAP else
                f"retmax={_rm_req} 超过本工具上限，已夹到 {_rm}。"
                f"〔实测 2026-11-10〕上限 {RETMAX_CAP} 是【本工具自订】的（NCBI 那边 esearch 允许更大），"
                f"要一次多拿就把 {RETMAX_CAP} 当参数改，或者按年份/字段切小查询分批取。")
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
    lo, hi = _check_date(mindate, "mindate"), _check_date(maxdate, "maxdate")
    if lo and hi and _expand(hi, True) < _expand(lo):
        raise NcbiError(f"maxdate={maxdate!r} 的末尾早于 mindate={mindate!r}，这个区间是空的")
    filled = []
    rel = _check_reldate(reldate)
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
        q.update(reldate=rel, datetype="pdat")
    if lo or hi:
        if lo and not hi:
            hi = NO_END
            filled.append(f"maxdate 由本工具补成 {NO_END}＝不限上界（补成「今天」会出事：本机日历与 "
                          f"NCBI 服务器日历差一天时，边界日那一天的文献被静默切掉）")
        if hi and not lo:
            lo = NO_BEGIN
            filled.append(f"mindate 由本工具补成 {NO_BEGIN}＝不限下界（旧版补 1809-01-01 是错的，"
                          f"实测 tooth 在 0001-01-01:1800-01-01 有 12 条、\"1790\"[dp] 有 45 条，"
                          f"PubMed 里确有早于 1809 的题录）")
        # 〔实测 2026-11-10〕补完哨兵还得再比一次：只给 `mindate=3000-01-01` 时，
        #   上界被本工具补成 NO_END=2999-12-31 → 区间倒挂成 3000-01-01 → 2999-12-31，
        #   NCBI 对这种倒挂区间不报错、【静默回 0 条】，而 search 末尾会写
        #   「PubMed 里确实没有匹配这篇」——那是拿本工具自己造的空区间当结论。
        # 〔实测 2026-11-10 第 5 轮探针抓到〕这里【不能】逐字比字符串：
        #   mindate="2021-01-01" + maxdate="2021" 时 "2021-01-01" > "2021" 为真，
        #   于是把一对完全合法的参数拦成了「倒挂」——NCBI 自己把 2021 摊成 2021-12-31。
        #   本文件 467 行早就用 _expand() 处理过这件事，我加守卫时没用它。
        #   下界按【当年 1 月 1 日】摊开、上界按【当年 12 月 31 日】摊开，再比。
        if _expand(lo) > _expand(hi, True):
            raise NcbiError(
                f"mindate={lo}（摊开 {lo}→{_expand(lo)}）晚于本工具补出来的上界 {hi}"
                f"（摊开 {_expand(hi, True)}，NO_END 哨兵），区间是倒挂的。"
                f"〔实测 2026-11-10〕这种区间 NCBI 不报错、静默回 0 条，"
                f"那个 0 条既不是「主题没文献」也不是「NCBI 有这个界」，是参数本身没意义。"
                f"确实要查未来年份请连 maxdate 一起给（比如 maxdate=3050-01-01，"
                f"但注意 PubMed 的收录不会到那一年）。")
        q.update(mindate=lo, maxdate=hi, datetype="pdat")
    d = _safe_json(await _get_async("esearch.fcgi", q, 25), "esearch").get("esearchresult", {})
    d["_term"] = term
    d["_retmax_note"] = _rm_note
    d["_sort_note"] = _sort_note(sort, srt)
    d["_field_applied"] = f
    d["_rel"] = rel
    wins = _windows(d.get("querytranslation"))
    d["_windows"] = wins
    segs = "；".join(f"{a} → {b}（{lab}）" for a, b, lab in wins)
    if rel and (lo or hi):
        d["_daterange"] = (f"reldate 与你给的区间同时下发，NCBI 按【两段 AND】执行（都按发表日期 pdat；"
                           f"reldate 的窗口由 NCBI 按它自己的服务器日历算，本工具不做日历换算）："
                           f"{segs or 'NCBI 没回显窗口'}"
                           + ("；" + "，".join(filled) if filled else ""))
    elif rel:
        d["_daterange"] = (f"近 {rel} 天（reldate，按【发表日期】datetype=pdat）"
                           + (f"；NCBI 实际执行窗口 {segs}" if segs else "；NCBI 没回显窗口"))
    elif lo or hi:
        d["_daterange"] = (f"{lo} → {hi}（datetype=pdat＝发表日期）"
                           + ("；" + "，".join(filled) if filled else ""))
    else:
        d["_daterange"] = ""
    return d


def _sort_note(sort, canon):
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


def _check_field(field):
    f = (field or "").strip()
    if not f:
        return ""
    canon = FIELD_WHITELIST.get(f.lower())
    if canon in FIELD_SWALLOWED:
        raise NcbiError(f'field="{field}" 会被 NCBI 整条忽略（实测 field=keywords / pmcid / issn 三种'
                        f'给的 count 与 querytranslation 与【不传 field】逐字相同，等于没限定）。'
                        f'要按这些维度查，请把标签写进 term，例如 term=\'"Cone Beam Computed Tomography"[MeSH Terms]\'、'
                        f'或按刊名查用 [journal]；白名单里其余值是真生效的。')
    if canon in FIELD_NO_COUNT:
        raise NcbiError(f'field="{field}"（={canon}）实测会让 esearch 不返回 count，本工具没法给出命中数；'
                        f'请把标签写进 term，如 term=\'miniscrew[MeSH Terms]\'（那样 count 正常返回）。')
    if canon is None:
        raise NcbiError(
            f"field={field!r} 不在白名单里。NCBI 对认不出的 field 是【静默忽略】的"
            f"（实测 field=ZZZBOGUSFIELD 的命中数与不传 field 完全一致，⚠️ 也不报），"
            f"所以这里直接拦掉。可用值：{', '.join(sorted(set(FIELD_WHITELIST.values())))}")
    return canon


def _count_of(d):
    """count 缺失或不是数字就抛错。实测 field=MeSH Terms / Publication Type、空 term 三种情况
    都会让 esearch 不返回 count —— 旧版把它印成「命中 ? 条」，等于把无意义结果放进数字槽。"""
    c = d.get("count")
    if c is None or not str(c).isdigit():
        raise NcbiError("esearch 没返回可用的 count；NCBI 原文：" + json.dumps(d, ensure_ascii=False)[:300])
    return int(c)


_CJK = re.compile(r"[㐀-鿿぀-ヿ]")


def _warn_bits(d):
    """从 NCBI 的 warninglist 里取出「对你的查询动了什么手脚」。实测 2026-11-10 三个子键：

        phrasesignored        —— 停用词（the/and/of…）被 NCBI 扔掉，不参与检索
        quotedphrasesnotfound —— 整个引号短语在索引里找不到（原样带引号回显）
        outputmessages        —— 0 条时 NCBI 的回声 "No items found."

    返回 (ignored, quoted_missing, others)。**这一支以前全文没读过 warninglist**，
    于是 '"zzzqqxnonexistentterm"' 与 'the and of' 两条 0 查询的 _note 是【空的】，
    而 _zero_explanation 兜底那句「PubMed 里确实没有匹配这篇」照印 —— 那是假话。
    """
    w = d.get("warninglist")
    if not isinstance(w, dict):
        return [], [], []
    ig = [str(x).strip() for x in _as_list(w.get("phrasesignored") or []) if str(x).strip()]
    qn = [str(x).strip() for x in _as_list(w.get("quotedphrasesnotfound") or []) if str(x).strip()]
    om = [str(x).strip() for x in _as_list(w.get("outputmessages") or []) if str(x).strip()]
    others = [x for x in om if x.lower() != "no items found."]   # 实测它＝0 条的回声，不重复印
    return ig, qn, others


def _note(d):
    """把 NCBI 对查询动的手脚原样说出来。键名以实测返回的为准。"""
    lines = []
    term = str(d.get("_term") or "")
    qt = (d.get("querytranslation") or "").strip()
    if not qt:
        # 实测：中文查询（"牙根吸收"、"种植钉"）会让 NCBI 返回空的 querytranslation + 0 条。
        # 这个 0 是「NCBI 没解析出任何检索条件」，不是「这个主题没有文献」，两者必须分开说。
        hint = ""
        if _CJK.search(term):
            hint = "（PubMed 只索引英文，中文词基本必然走到这里；先换成英文词，如 牙根吸收 → tooth resorption）"
        lines.append(f"⚠️ NCBI 没解析出任何检索条件（querytranslation 为空）{hint}"
                     f"—— 这个结果【不能】用来说明主题有无文献")
    elif qt.lower() != term.strip().lower():
        # 只印 querytranslation，不再重复印 translationset —— 实测后者是前者的逐概念分解，
        # 两条都印等于同一份信息烧两遍 token（CBCT 那种短语能扩出 200+ 字符）。
        # as_json=True 时 translationset 会作为结构化字段单独给，脚本要用再从那里取。
        if len(qt) > 600:
            lines.append(f"NCBI 实际执行：{qt[:600]}…（截断；完整解析用 as_json=True 取）")
        else:
            lines.append(f"NCBI 实际执行：{qt}")
    e = d.get("errorlist") or {}
    nf = []
    if isinstance(e, dict):
        nf = [str(x) for x in _as_list(e.get("phrasesnotfound") or [])]
        if nf:
            lines.append("⚠️ NCBI 查不到这些片段，已【丢弃】（它们没有参与检索）：" + ", ".join(nf))
        ff = _as_list(e.get("fieldsnotfound") or [])
        if ff:
            lines.append("⚠️ NCBI 查不到这些字段，已【丢弃】：" + ", ".join(map(str, ff)))
    ig, qn, others = _warn_bits(d)
    if ig:
        lines.append("⚠️ NCBI 把这些词当【停用词】直接忽略（它们没参与检索，与上面「已丢弃」是两条"
                     "不同的路）：" + ", ".join(ig))
    if qn:
        try:
            _c = int(str(d.get("count") or "0"))
        except ValueError:
            _c = -1
        head = "⚠️ NCBI 明说这些【整个引号短语】在索引里不存在（warninglist.quotedphrasesnotfound）：" \
               + "，".join(qn)
        if _c > 0:
            # 实测 2026-11-10：'"micro-implant immediate loading"' 有 8 条，但 querytranslation
            #   根本不是这个短语，而是 "micro implant"[All Fields] AND immediate* AND loading*
            #   ——NCBI 把短语换成【按词 AND】再查。所以那 8 条是替代式的数。
            lines.append(head +
                         " —— 它被换成了【按词 AND】执行（上面那条「NCBI 实际执行」就是替代式），"
                         f"所以这 {_c} 条【不是】你写的这个短语的命中数。"
                         "要按短语查就别加引号，或改成 MeSH 词。")
        else:
            lines.append(head +
                         " —— 这条 0 只关于那段字面短语本身，关于它想表达的主题有没有文献"
                         "什么都没说；去掉引号按词查、或换 MeSH 词再问一次。")
    for o in others:
        lines.append(f"NCBI 附言：{o}")
    if d.get("_retmax_note"):
        lines.append("⚠️ " + d["_retmax_note"])
    if d.get("_sort_note"):
        lines.append("⚠️ " + d["_sort_note"])
    if d.get("_daterange"):
        dr = d["_daterange"]
        # 〔实测 2026-11-09〕旧判据里的「取交集」没了：现在 reldate 原样下发、由 NCBI 做两段 AND，
        # 所以该打 ⚠️ 的是「本工具补了边界」和「两段窗口不相交」这两种。
        mark = "⚠️ " if ("由本工具补" in dr or "两段 AND" in dr or "没回显" in dr) else ""
        lines.append(f"{mark}日期条件：{dr}")
    # 〔实测 2026-11-10〕这段判据原来在 `if d.get("_daterange")` 里面，于是
    #   "miniscrew[Title] AND 2019[dp] AND 2035[dp]"（互斥区间全写在 term 里、没传 mindate/reldate）
    # 这种根本不会提醒 —— 而 _daterange 只描述【本工具发出去的】日期参数，窗口本身在 qt 里就有。
    # 现在独立判：只要 qt 回显了同字段且互斥的两段就说出来。
    for (a1, b1, l1), (a2, b2, l2) in _same_label_pairs(d.get("_windows") or []):
        if a1 > b2 or a2 > b1:
            lines.append(f"⚠️ NCBI 实际执行的两段【同字段（{l1}）】日期不相交"
                         f"（{a1}→{b1} 与 {a2}→{b2}）。"
                         f"这种情况下即使主题有大量文献也会是 0 条，别读成「PubMed 里没有匹配」。")
    gap = _phrase_gap(d)
    if gap:
        lines.append("⚠️ 引号短语里有词在 NCBI 的实际执行中找不到：" + "；".join(gap) +
                     " —— 实测引号【内】的词被丢掉时 NCBI 不填 phrasesnotfound，所以这种丢弃不会出现在"
                     "上面那行「已丢弃」里。这个命中数是【剩下的词】的数，"
                     "不能读成「这个短语有 N 篇」（实测 '\"xyzzy tooth resorption\"' 回 11916，"
                     "等于 tooth resorption 的数；该短语真实 0 篇）")
    if d.get("_field_applied"):
        # 〔实测 2026-11-08〕上一版这里断言「NCBI 不在 querytranslation 里回显 field」是假的：
        #   count('tooth resorption', field='author') 的 qt 就是 `tooth[Author]`；
        #   field='ti' → qt 尾段带 [Title]；field='ta' → 带 [Journal]。
        # 但它确实【不保证】回显，而且有两种实测到的「限定根本没生效」：
        #   a) term 里已经写了标签：'miniscrew[Title]' 配 field="Journal" → 808 条，qt 仍是
        #      `"miniscrew"[Title]` —— term 内的标签赢，field 被丢；
        #   b) 某些字段 NCBI 直接吞掉 field 参数（keywords/pmcid/issn，已改由 _check_field 拦下）。
        # 所以这里不再写「已下发」了事，改成给一条可自己核对的判据。
        extra = ""
        fa = str(d["_field_applied"])
        if nf:
            extra = ("\n   ⚠️ 上面那些片段被丢弃后，field 只作用在【剩下的部分】上 —— "
                     "这个数不是「原查询在该字段下的命中数」")
        elif ("[" + fa + "]") in qt:
            # 〔实测 2026-11-09〕判据顺序必须是「先看 qt 有没有，再说别的」：
            #   term='tooth resorption AND 2020:2026[dp]' 配 field=Title 时 qt 是
            #   `"tooth"[Title] AND "resorption"[Title] AND 2020/01/01:2026/12/31[Date - Publication]`
            #   —— field 明明生效，旧写法却因为「term 里有方括号」直接宣布 field 被 NCBI 丢掉，
            #   还教人「请直接改 term 里的标签」，那条建议在这个情况下会把对的查询改坏。
            extra = f"\n   判据：「NCBI 实际执行」里出现了 [{fa}]，这个限定生效了。"
        elif re.search(r"\[[A-Za-z][^\]]{1,28}\]", str(d.get("_term") or "")):
            extra = ("\n   ⚠️ qt 里没有出现这个 field 的标签，而 term 自己写了字段标签 —— "
                     "实测这种情况下 term 内的标签赢、field 被丢"
                     "（'miniscrew[Title]' 配 field=\"Journal\" 仍回 [Title] 的 808 条）。"
                     "要换字段请直接改 term 里的标签，别靠 field 参数。")
        else:
            extra = (f"\n   ⚠️ 「NCBI 实际执行」里没出现 [{fa}]，"
                     f"不能确定限定真的生效；要确认就对比同查询不传 field 的命中数")
        lines.append(f"field={fa}（已下发）" + (f"，判定据见下{extra}" if extra else ""))
    return lines


def _phrase_gap(d):
    """带引号的精确短语里，哪些词在 NCBI 的实际执行里彻底不见了。
    〔实测 2026-11-08，这是今天最后一个漏掉的假存在性判断〕引号内的词查不到时 NCBI
    【不填 phrasesnotfound】，所以「⚠️ 已丢弃」和 _all_dropped 两道保险双双失效：
      count('"xyzzy tooth resorption"') → 11916 条，与 count('tooth resorption') 逐字相同，
      qt 是 "tooth resorption"[MeSH Terms] OR … —— xyzzy 被整段吞掉，一个字的提醒都没有。
      而不带引号写同样的三个词，NCBI 就老老实实填了 phrasesnotfound=[xyzzy]。
    判据只能是「引号里的词有没有出现在 qt 中」。反向也验过不误报：
      '"children"' → qt 就是 "children"[All Fields]（ATD 会展开成 MeSH 词，原词也在 qt 里）；
      '"temporary anchorage device"' → qt 原样保留，0 个缺口。
    """
    qt = (d.get("querytranslation") or "").lower()
    if not qt:
        return []
    gap = []
    for ph in re.findall(r'"([^"]+)"', str(d.get("_term") or "")):
        for w in (x for x in re.split(r"[^0-9A-Za-z]+", ph.lower()) if len(x) >= 3):
            if w not in qt and w not in {str(x).lower() for x in
                                         _as_list((d.get("errorlist") or {}).get("phrasesnotfound") or [])}:
                gap.append(f'"{ph}" 里的 {w}')
    return gap


_BOOL_WORDS = {"and", "or", "not", "with", "near"}     # PubMed 查询里的算子，不是「被检索的词」


def _zero_explanation(d, count):
    """命中 0 条时该说哪一句（不带「=>」前缀，由调用方拼）。
    〔实测 2026-11-09〕这一支里补/删了两件事：
      - 新加「两段日期窗口不相交」：reldate 现在原样下发、由 NCBI 做 AND，本工具不再用本机日历
        提前预判空交集（那个预判本身就是 bug 的来源），所以空交集必须在这里判，
        否则「确实没有匹配这篇」那句假话又回来了；
      - 删掉「撞上 9999 窗口上限」那一支：越界的 retstart 现在由 _esearch 提前拦下并报错，
        那一支永远走不到（旧注释还把它当实测事实写着）。
    """
    if _all_dropped(d):
        # 〔实测 2026-11-10〕旧写法对两种「全丢」说同一句话，而建议只对其中一种成立：
        #   errorlist.phrasesnotfound 全丢  → 多半是 field 选错（tooth resorption[Journal]），
        #                                     「换成 tiab 或去掉 field」才对；
        #   warninglist.phrasesignored 全丢 → 查询里本来就只剩 the/and/of 这种虚词，
        #                                     换字段没用，得换【有实质内容的词】。
        only_stop = bool(_warn_bits(d)[0]) and not (
            isinstance(d.get("errorlist"), dict) and d["errorlist"].get("phrasesnotfound"))
        if only_stop:
            return ("0 条，但这【不能】当成「主题没有文献」：NCBI 把你写的词全当【停用词】忽略了"
                    "（the/and/of 这类虚词，见上 ⚠️），实际生效的检索条件为空。"
                    "换字段没用——要换成语义实词（如 miniscrew、root resorption）再问一次。")
        return ("0 条，但这【不能】当成「主题没有文献」：NCBI 把这条查询里的词全部丢弃了（见上 ⚠️），"
                "实际生效的检索条件为空。换成有索引的字段（如 tiab）或去掉 field 再问一次。")
    for (a1, b1, l1), (a2, b2, l2) in _same_label_pairs(d.get("_windows") or []):
        if a1 > b2 or a2 > b1:
            return (f"0 条的原因是【两段同字段（{l1}）的日期窗口不相交】：NCBI 执行的是 "
                    f"{a1}→{b1} 与 {a2}→{b2}。"
                    f"这不能读成主题没文献 —— 放宽 reldate，或去掉 mindate/maxdate 再问一次。")
    if not (d.get("querytranslation") or "").strip():
        return ("0 条，但这条 0 不能作为「主题没有文献」的依据："
                "NCBI 压根没解析出检索条件（见上）。")
    if count and int(d.get("retstart") or 0) >= count:
        return (f"本次 0 条只是因为 retstart={d.get('retstart')} 越过了命中总数 {count} 条，"
                f"不是「没有匹配」——往回翻页即可。")
    if _warn_bits(d)[1]:
        return ("0 条的原因是 NCBI 说【整个引号短语在索引里不存在】"
                f"（warninglist.quotedphrasesnotfound＝{_warn_bits(d)[1]}）。"
                "这句只关于那段字面短语，关于它想表达的主题有没有文献【什么都没说】——"
                "去掉引号按词查、或换成 MeSH 词再问一次。")
    if _phrase_gap(d):
        return ("0 条，但先看清楚：上面 ⚠️ 说引号短语里有词没进实际执行，"
                "所以这个 0 是「剩下那几个词的组合没有匹配」，不是你写的那个短语的结论。")
    return "0 条。HTTP 请求是成功的，PubMed 里确实没有匹配这篇。"


def _all_dropped(d):
    """整条查询里的词是不是全被 NCBI 丢掉了。丢掉后 querytranslation 只是原词回显，
    实际生效的检索条件为空 —— 这种 0 条绝不能写成「PubMed 里没有匹配」。
    〔实测 2026-11-08〕search('tooth resorption', field='journal')：命中 0、
    phrasesnotfound=[tooth, resorption]，旧输出跟着印「HTTP 请求是成功的，PubMed 里确实没有匹配这篇」。"""
    e = d.get("errorlist")
    # 〔实测 2026-11-10〕丢掉整条查询有【两条】路，旧代码只看第一条：
    #   errorlist.phrasesnotfound（词查不到，如 'miniscrew AND zzzqqx' 里的 zzzqqx）
    #   warninglist.phrasesignored（停用词，如 'the and of' 三个词全被忽略 → 生效条件为空）
    nf = set()
    if isinstance(e, dict):
        nf |= {str(x).strip().lower() for x in _as_list(e.get("phrasesnotfound") or []) if str(x).strip()}
    nf |= {x.lower() for x in _warn_bits(d)[0]}
    if not nf:
        return False
    toks = {w for w in re.split(r"[^0-9A-Za-z一-鿿]+", str(d.get("_term") or "").lower())
            if w and w not in _BOOL_WORDS}
    # 〔实测 2026-11-09〕旧写法没有剔掉布尔算子，于是整条查询只要有 AND/OR 就永远不会判成「全丢」：
    #   search('zzzqqx AND qbvcjk') → 命中 0、⚠️ 已丢弃列出这两个词，
    #   末尾却照样印「HTTP 请求是成功的，PubMed 里确实没有匹配这篇」——那是假的存在性判断。
    if toks and toks <= nf:
        return True
    qt = (d.get("querytranslation") or "").lower()
    return bool(qt) and bool(toks) and not any(w in qt for w in toks)


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _parse_pmids(pmids):
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
    s = re.sub(r"(?i)\bpmids?\b\s*[:#]?\s*", " ", pmids or "")
    # 〔实测 2026-11-10 抓到〕旧写法直接按逗号切，'1,234,567' 变成 ['1','234','567'] ——
    #   三个都是【真实存在】的 PMID，于是「从 Excel 复制一列带千位分隔符的号」
    #   会安静地抓到三篇不相干的文献，而且条数看起来完全正常。
    # 这里不猜：这种形状既可能是「一个号写成了 1,234,567」也可能是「1 和 234 和 567 三个号」，
    #   两种读法给出不同文献，替用户选就是本工具一直在禁止的那种事（把猜测写成事实），所以直接拦。
    amb = [t for t in re.split(r"[;，、\s]+", s) if t and re.fullmatch(r"\d{1,3}(?:,\d{3})+", t)]
    if amb:
        raise NcbiError(
            f"这些串看着像【带千位分隔符的数字】，没法判断是一个号还是好几个号：{amb[:3]}。"
            f"〔实测 2026-11-10〕旧版会把 '1,234,567' 切成 1 / 234 / 567 三个【真实存在】的 PMID，"
            f"于是抓回三篇不相干的文献，条数还看着正常。"
            f"两种意思请分开写：一个号就去掉逗号写 1234567；好几个号就用空格或分号隔开"
            f"（如 '12345 678 900'），别用逗号紧跟三位数字的写法。")
    toks = [t for t in re.split(r"[;,，、\s]+", s) if t]
    if not toks:
        raise NcbiError("pmids 为空")
    ids, bad = [], []
    for t in toks:
        m = re.fullmatch(r"[:#.，、]?(\d+)", t.strip())
        if m:
            ids.append(str(int(m.group(1))))
        else:
            bad.append(t)
    if bad:
        raise NcbiError(f"这些不是 PMID（要纯数字，也可写成 PMID 12345）：{bad[:5]}"
                        + ("；PMC 号不能当 PMID 用，先转成 PMID" if any(b.upper().startswith("PMC") for b in bad) else ""))
    seen, uniq = set(), []
    for p in ids:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq, len(ids)


async def _esummary_dicts(ids):
    """分批取 esummary，返回 [(uid, 记录), ...]。
    uid 一律用 NCBI 自己给的 result.uids，不拿入参顺序去 zip —— 顺序不保证一致。"""
    ids = list(ids)
    chunks = [ids[i:i + ESUMMARY_CHUNK] for i in range(0, len(ids), ESUMMARY_CHUNK)]
    out = [None] * len(chunks)

    async def one(i, c):
        try:
            d = _safe_json(await _get_async("esummary.fcgi",
                                            {"db": "pubmed", "id": ",".join(c), "retmode": "json"}, 25),
                           "esummary")
        except NcbiError as e:
            errs.append(e)      # 不让它出组：ExceptionGroup 的 str() 里没有 NCBI 原文
            return
        res = d.get("result", {})
        uids = res.get("uids", [])
        out[i] = [(u, res.get(u) or {}) for u in uids]

    errs = []
    async with anyio.create_task_group() as tg:
        for i, c in enumerate(chunks):
            tg.start_soon(one, i, c)
    if errs:
        # 〔实测 2026-11-08〕旧版不接，任何一片失败调用方只看到
        # 「unhandled errors in a TaskGroup (1 sub-exception)」——NCBI 那句原文被吞，
        # 而文件头承诺「请求失败会把 NCBI 原文带回来」。宁可整条报错，也不返回缺片的表。
        raise errs[0]
    return [pair for part in out if part for pair in part]


def _ids_map(r):
    return {a.get("idtype"): a.get("value") for a in (r.get("articleids") or []) if isinstance(a, dict)}


def _record_parts(uid, r):
    """DOI / PMC 一律从 articleids 取。
    旧版先遍历 elocationid 找 {'doctype':'doi'} —— 实测这个键是字符串（"doi: 10.1259/..."），
    isinstance(el, dict) 永假，那段是死代码，删。"""
    ids = _ids_map(r)
    src = r.get("source") or r.get("fulljournalname") or "?"
    vol, issue, pages = r.get("volume") or "", r.get("issue") or "", r.get("pages") or ""
    cite = ""
    if vol or pages:
        cite = f" {vol}" + (f"({issue})" if issue else "") + (f":{pages}" if pages else "")
    # authorlist 兜底到 authors 是必需的：实测 esummary 对 37427694 不返回 authorlist，
    # 作者全走 authors；重构时别把 `or r.get("authors")` 当冗余删掉。
    auth = r.get("authorlist") or r.get("authors") or []
    names = ", ".join((a.get("name") or "").strip() for a in auth[:3] if isinstance(a, dict))
    total = len(auth)
    if total > 3:
        names += f"，共 {total} 人"          # 旧版「等 N 人」会被读成「另外 N 人」
    ptypes = [p for p in _as_list(r.get("pubtype") or []) if isinstance(p, str)]
    ptag = ""
    if ptypes and not (len(ptypes) == 1 and ptypes[0] == "Journal Article"):
        ptag = "｜" + "/".join(ptypes[:2])    # 只有非普通论著才占这个 token
    return {
        "pmid": uid,
        "pubdate": r.get("pubdate") or "?",
        "epubdate": r.get("epubdate") or "",
        "journal": src,
        "citation": cite.strip(),
        "title": r.get("title") or "(无标题)",
        "authors": names or "(无作者信息)",
        "author_count": total,
        "doi": ids.get("doi") or "",
        "pmc": ids.get("pmc") or "",
        "pubtype": ptypes,
        "ptag": ptag,
    }


def _fmt_record(r, uid):
    p = _record_parts(uid, r)
    head = f"PMID {p['pmid']}｜{p['pubdate']}｜{p['journal']}{(' ' + p['citation']) if p['citation'] else ''}{p['ptag']}"
    tail = []
    if p["doi"]:
        tail.append("doi:" + p["doi"])
    if p["pmc"]:
        tail.append("pmc:" + p["pmc"] + "（开放获取，可有免费全文）")
    return (f"{head}\n  {p['title']}\n  {p['authors']}"
            + (("｜" + "｜".join(tail)) if tail else ""))


async def _efetch_abstracts(pmids):
    timeout = min(60, 20 + 0.4 * len(pmids))
    return await _get_async("efetch.fcgi", {"db": "pubmed", "id": ",".join(pmids),
                                            "rettype": "abstract", "retmode": "text"}, timeout)


async def _efetch_all(pmids):
    """分批并发取正文，同时按 efetch 文本里的 ^PMID: 行统计到底取回了哪几条。"""
    chunks = [pmids[i:i + EFETCH_CHUNK] for i in range(0, len(pmids), EFETCH_CHUNK)]
    out = [None] * len(chunks)

    async def one(i, c):
        try:
            out[i] = await _efetch_abstracts(c)
        except NcbiError as e:
            errs.append(e)     # 出组会被包成 ExceptionGroup，原文丢失（见 _esummary_dicts 同处注释）
            return

    errs = []
    async with anyio.create_task_group() as tg:
        for i, c in enumerate(chunks):
            tg.start_soon(one, i, c)
    if errs:
        raise errs[0]
    text = "\n\n".join(p for p in out if p)
    got = re.findall(r"^PMID: (\d+)", text, re.M)     # NCBI 排版每条末尾都有这行（实测）
    return text, got


# ---------- MCP 工具 ----------
from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("PubMed")


@mcp.tool()
async def search(term: str, retmax: int = 20, sort: str = "", field: str = "",
                 mindate: str = "", maxdate: str = "", reldate: int = 0, retstart: int = 0,
                 as_json: bool | str = False) -> str:
    """在 PubMed 检索，返回命中数 + 每条的 PMID/日期/期刊卷页/标题/作者/DOI/PMC。

    什么时候用：找文献、核对某主题有多少文章、拿 PMID 列表。
    term 支持 PubMed 语法：字段标签如 "resorption[Title]"、布尔组合 "A AND B"、
    截词 "resorption*"、范围 "2024:2026[dp]"。建议一次只写一个概念，靠字段标签收窄。
    sort 只接受 pub_date / relevance（实测 NCBI 对别的值是静默接受、顺序照旧，所以这里会拦）。
    **PubMed 没有「按期刊好坏排序」这个概念**，要那种排序得自己接外部期刊指标数据源。
    field 整体限定字段，只接受白名单里【实测真生效】的值（Title / Title/Abstract / Journal / Author /
    Affiliation / Language / Grant / ISBN / PMID 等）。实测会被 NCBI 静默吞掉的 keywords / PMC Number /
    ISSN，以及会让 esearch 不返回 count 的 MeSH Terms / Publication Type，现在直接拦下来并教你改写进 term。
    不在名单里的也直接报错——实测 NCBI 对认不出的 field 是静默忽略的，宁拦勿吞。
    mindate/maxdate 请成对给。〔实测 2026-11-08〕只给一端时 NCBI 会【整条忽略】日期条件：
    mindate=2020-01-01 单给得到 795 条，与不限时间逐字相同（我一度以为是我们没下发，裸参数实测才排除掉）。
    所以本工具替你补上另一端，补的是【不可能生效的哨兵值】，不是「今天/收录起点」：
    缺 maxdate 补 2999-12-31（＝不限上界），缺 mindate 补 0001-01-01（＝不限下界），输出里用 ⚠️ 写明补了什么。
    〔实测 2026-11-09〕旧写法那两个「看起来无害」的边界都会静默少算：本机日历与 NCBI 服务器日历
    差一天时，补「今天」把边界日那天的文献切掉（dental implant 770 → 763，少的是 2026-09-09 那 7 条）；
    补 1809-01-01 则切掉更早的题录（实测 tooth 在 0001-01-01:1800-01-01 有 12 条、"1790"[dp] 有 45 条，
    所以「早于 PubMed 收录起点」那句是假的）。
    格式可用 2024-01-01 / 2024/01 / 2024；写法对但日历上不存在的（2023-02-29）会被拦——
    实测那种值 NCBI 不报错、errorlist 也是空的，整条查询静默变 0 命中。
    reldate=365 是「近一年」，本工具【原样下发】给 NCBI 并固定配 datetype=pdat（发表日期口径），
    不做本机日历换算〔实测 2026-11-09：换算过一回，把 NCBI 的 09-09:11-08 换成 09-10:11-09，
    同一个词同一组参数就少算 7 条〕。NCBI 自己单看 reldate 用的是 [Date - Entry]（365 天 → 82 条），
    配 pdat 是 [Date - Publication]（→ 86 条），差 4 篇，所以这里固定带 pdat。
    reldate 只收 0..36500：负数旧版被静默夹成 0（＝不限日期，313615 条照回、毫不提醒），
    1e9 会抛裸 OverflowError，现在都直接报错。
    retmax 上限 1000（内部自动分批，不会再撞 URL 长度墙），翻页用 retstart，
    但实测 PubMed 的 esearch 最多只能取回【前 9999 条】：count 再大也只能翻到 9999，到顶会打 ⚠️。
    as_json=True 时返回结构化 JSON（给脚本用，别正则我的中文文案）。

    怎么读返回：
    - 「NCBI 实际执行：…」这行是改写后的查询，引用命中数时以它为准。实测打错的
      "anchorage devcie" 会被执行成 '"anchorage"[All Fields] OR "anchorages"[All Fields]'，
      命中数暴涨且与主题无关——不打印这行就查不出来。
    - 「⚠️ 已丢弃」列出的词【没有】参与检索，此时命中数是剩下那几个词的，别当原查询用。
      但这条只对**没加引号**的词成立：实测写成 "xyzzy tooth resorption"（带引号）时 NCBI 照样把
      xyzzy 扔掉，却不填 phrasesnotfound —— 那种情况由另一行「⚠️ 引号短语里有词…找不到」负责，
      看到它就说明这个命中数不是你这个短语的数。
    - 日期：给了 reldate 又给了 mindate/maxdate 时，两段条件【都下发】，由 NCBI 做 AND，
      所以「NCBI 实际执行」里会出现两段日期窗口，本工具打印的窗口也是从那里读的（不是本机算的）。
      两段【同字段】的日期不相交时这一条会给 0 条，输出里有专门的「两段同字段的日期窗口不相交」
      说明，不会跟「确实没有匹配这篇」混在一起（〔实测 2026-11-09〕旧版在这里是本机换算＋取交集，
      两种路径给出的命中数不一样，那才是真 bug；旧版还写着「不相交直接报错」，现已改由 NCBI 判、
      由本工具解释）。挂在不同字段上的两段（一段 [Date - Entry]、一段 [Date - Publication]）
      【不】会说成不相交 —— 那是两个不同的轴，比端点没有意义〔实测 2026-11-10：
      "2020:2020[edat] AND implant" 配 reldate=60 的回显就是这种形状〕。
    - "0 条"是请求成功、不是网络故障（故障会以 NcbiError 抛出），但**不能一律读成「确实没有匹配」**。
      〔实测 2026-11-10〕这四种 0 条各有各的原因，返回里都会另起一行说清楚：
        ① 中文词（"牙根吸收"）→ querytranslation 为空 = NCBI 没解析出检索条件，换英文词；
        ② 词全被当停用词（"the and of"）→ warninglist.phrasesignored 列全了词 = 生效条件为空，
          这句 0 跟主题有没有文献无关；
        ③ 整个引号短语不存在（'"zzzqqxnonexistentterm"'）→ warninglist.quotedphrasesnotfound；
        ④ 两段同字段日期互斥 → 见上面「不相交」那条。
      只有这四种都不命中、且 NCBI 解析出了真实检索条件时，「PubMed 里没有匹配」才成立。
    - 单复数（device/devices）在 PubMed 里映射到不同词形，实测差 1 条
      〔已作废 2026-11-10：现在 "temporary anchorage device" 与 "...devices" 都是 795 条、
      querytranslation 逐字相同 —— NCBI 把两边都展开成同一组 OR；当年那个 1 条的差不复现，
      但「用 MeSH 词覆盖两种写法」这条建议本身仍然有效〕。要覆盖两种写法，
      改用 MeSH 词，如 "Temporary Anchorage Devices"[MeSH Terms]。
    """
    # 〔实测 2026-11-10〕这里【不能】给 _int 传 lo：传了就把负数当场夹平，
    #    _esearch 里那句「retmax 是负数，本工具不替它编一个意思」根本轮不到（-3 变 0、静默出结果）。
    #   上限同理留给 _esearch，那里既夹也说明。
    d = await _esearch(term, _int(retmax, "retmax", 20), sort, field, mindate, maxdate, reldate, retstart)
    ids = d.get("idlist") or []
    count = _count_of(d)
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
    if _as_bool(as_json, "as_json"):
        pairs = await _esummary_dicts(ids)
        # 〔实测 2026-11-09〕旧版的 as_json 分支把「窗口到顶」⚠️ 和 0 条解释整个丢掉了，
        # 而 docstring 又让脚本改用 as_json —— 走 JSON 的人恰恰拿不到「这批没取全」这句最需要的话。
        # 现在两支都进 JSON（warnings 里也放一份，脚本只看 warnings 也不会漏）。
        warn = _note(d) + ([win_warn] if win_warn else [])
        return json.dumps({
            "term": term, "querytranslation": (d.get("querytranslation") or "").strip(),
            "translationset": d.get("translationset") or [],
            "field": d.get("_field_applied", ""),
            "count": count, "retstart": int(d.get("retstart") or 0), "returned": len(ids),
            "esummary_missing": max(0, len(ids) - len(pairs)),
            "dropped_phrases": _as_list((d.get("errorlist") or {}).get("phrasesnotfound") or []),
            "daterange": d.get("_daterange") or "",
            "executed_windows": d.get("_windows") or [],
            "window_warning": win_warn,
            "zero_note": ("=> " + _zero_explanation(d, count)) if not ids else "",
            "records": [_record_parts(uid, r) for uid, r in pairs],
            "warnings": warn,
        }, ensure_ascii=False)
    head = [f"查询：{term}", f"命中 {count} 条，本次给 {len(ids)} 条（retstart={d.get('retstart')}）"]
    head += _note(d)
    if win_warn:
        head.append(win_warn)
    if not ids:
        head.append("=> " + _zero_explanation(d, count))
        return "\n".join(head)
    pairs = await _esummary_dicts(ids)
    if len(pairs) != len(ids):
        head.append(f"⚠️ esearch 给了 {len(ids)} 个 id，esummary 只回了 {len(pairs)} 个 —— "
                    f"少的那几条 NCBI 没给元数据，别按 {len(ids)} 条算。")
    head.append("")
    return "\n".join(head) + "\n" + "\n".join([_fmt_record(r, uid) for uid, r in pairs])


@mcp.tool()
async def count(term: str, field: str = "", mindate: str = "", maxdate: str = "",
                reldate: int = 0) -> str:
    """只问「这个主题有多少篇」，不取记录，最省请求。带 ⚠️ 提示查询被改写。

    注意：命中数大得反常时先看 ⚠️ 和「NCBI 实际执行」——实测查不到的词被 NCBI 静默丢弃后
    仍会返回一个非 0 的数（"zzzqqx nonexistent term" → 610 条），0 条反而是少见的情况。
    中文查询词（实测 "牙根吸收" → 0 条 + 空 querytranslation）会得到「NCBI 没解析出任何检索条件」，
    那不等于主题没文献，换英文词再问。
    field 与 search 一样走白名单；count 拿不到数字时抛 NcbiError，不印 "?"。
    """
    d = await _esearch(term, retmax=0, field=field, mindate=mindate, maxdate=maxdate, reldate=reldate)
    return "\n".join([f"查询：{term}", f"命中 {_count_of(d)} 条"] + _note(d))


@mcp.tool()
async def fetch_abstracts(pmids: str) -> str:
    """按 PMID 取摘要原文（NCBI 排版，含 INTRODUCTION/METHODS/RESULTS/CONCLUSIONS）。

    pmids 用逗号分隔，如 "42998065,42997565"。内部每 50 个一批并发，多少个都收。
    引用数值请以这里的原文为准，search 返回的只有标题和 DOI。
    返回头是「要 N 条 / 实取 M 条」两个数——不存在的 PMID 会被点名（实测旧版把请求数当结果数，
    拿 PMID 9999999999 会报「取回 1 条」再加一条空壳）。
    口径注意：这里的「实取」= NCBI 返回了这条**记录**，不等于里面有摘要。社论、letter 本来就没有摘要，
    〔实测 2026-11-08：PMID 42759866 → 「要 1 条 / 实取 1 条」且没有任何 ⚠️，正文里只有引文行〕
    旧版 docstring 说这种会列进「NCBI 未返回」，是假的，已改口。要判断有没有摘要得看正文本身。
    补零的号（如 000037427694）现在由 _parse_pmids 统一成裸号，不会再出现「实取 1 条」与
    「NCBI 没有返回这条」同屏自相矛盾。〔实测 2026-11-08〕
    """
    uniq, n_in = _parse_pmids(pmids)
    text, got = await _efetch_all(uniq)
    missing = [p for p in uniq if p not in set(got)]
    head = [f"要 {len(uniq)} 条 / 实取 {len(got)} 条（分 {(len(uniq) + EFETCH_CHUNK - 1) // EFETCH_CHUNK} 批）"]
    if n_in != len(uniq):
        head.append(f"（原始输入 {n_in} 个，去掉重复后 {len(uniq)} 个）")
    if missing:
        # 「或该文本来就没有摘要」这半句是假的，删掉：missing 是按 efetch 正文里有没有 ^PMID: 行算的，
        # 而没摘要的社论照样回记录、照样算实取〔实测 PMID 42759866：要 1 条 / 实取 1 条，无 ⚠️〕。
        # 走到这里只剩一种解释：这些号 NCBI 没给回任何记录（号不存在、已撤销，或 db 里查不到）。
        head.append("⚠️ NCBI 没返回这些号的记录（efetch 正文里没有对应的 PMID 行）——"
                    "注意【不是】「这篇没有摘要」：实测没有摘要的社论照样返回记录、照样算实取。"
                    "这里点名的一律是取不到条目本身：" + ", ".join(missing))
    return "\n".join(head) + "\n\n" + text


DB_WHITELIST = {"pmc", "nucleotide", "gene", "pubmed"}


def _check_db(to):
    """〔实测 2026-11-08〕旧版把 to 原样塞进 db 参数、又用 linkname.endswith(to) 分类，大小写一改就出错：
      to='pmc' → PMID 37427694 给 pubmed_pmc=PMC10461258
      to='PMC' → 同一条 PMID 给「(PMC 库无直接对应记录)」—— 把「有全文」说成了「没全文」
      to='pmcid' → NCBI 回 Invalid db name，但异常被 task group 包成 ExceptionGroup，原文被吞
    这个工具存在的理由就是回答「有没有免费全文可下」，所以宁可拦下来，也不给假阴性。"""
    s = (to or "").strip().lower()
    if s not in DB_WHITELIST:
        raise NcbiError(f"to={to!r} 不是本工具支持的库。可用：{', '.join(sorted(DB_WHITELIST))}"
                        f"（只认库名，不认 'PMC'/'PMCID' 这类写法）")
    return s


async def _pmids_exist(pids):
    """哪些 PMID 真的存在于 PubMed。返回 set。

    〔实测 2026-11-08〕elink 分不出「号不存在」和「号存在但那个方向零链接」——
    真号 42760028（to=pmc 无链接）与假号 9999999999 返回的 linkset 形状逐字相同
    （都只有 {"dbfrom":"pubmed","ids":[...]}，没有 linksetdbs 键）。
    上一轮我就是拿「linksetdbs 缺失」当「号不存在」的判据，结果把有 PMC deposit 之外的
    真号说成「PubMed 里没有这个号」，存在性判断整个反过来。esummary 能分开这两种：
      真号 → 完整记录（实测 42760028 / 33485718 各 43 个键）
      假号 → 空壳，且带 error="cannot get document summary"（实测 9999999999 只有 2 个键）
    """
    if not pids:
        return set()
    pairs = await _esummary_dicts(list(pids))
    have = set()
    for uid, rec in pairs:
        if isinstance(rec, dict) and not rec.get("error") and len(rec) > 3:
            have.add(str(uid))
    return have


@mcp.tool()
async def links(pmids: str, to: str = "pmc") -> str:
    """查这些 PMID 在别的数据库里的对应记录（elink）。to=pmc 用来判断「有没有免费全文可下」。

    to 可选 pmc / nucleotide / gene / pubmed（to=pubmed 得到「相关文献」）。
    返回每行形如「PMID 37427694  pubmed_pmc=PMC10461258  [pubmed_pmc_refs:4 条]」〔实测 2026-11-09 逐字抄〕：
    等号后是直接对应记录（to=pmc 时就是可以下载的 PMC 号，直接喂给下载脚本），方括号里是 NCBI 给的
    其它方向链接的计数。没有对应记录时写「(pmc 库无直接对应记录)」，号不存在时写
    「⚠️ PubMed 里没有这个号」——这两句是两件事，别混。
    （这句以前举的形状是「全文:PMC…  被引在PMC:6 条」，与今天代码的实际输出不符——
    今天按 db 改了图例，文档没跟着改。到底哪一版对应哪一天我没有逐日记录，〔未核实〕，
    所以这里只说「现在以本段为准」，不要把旧句子当历史事实引用。）

    两条实测出来的约束，别改回去：
    1. elink 的 JSON 模式不支持 cmd 参数（传了会 HTTP 200 + 顶层 ERROR），本工具因此不下发 cmd；
    2. 一次传多个 id 时 NCBI 会把它们【合并进一个 linkset】（ids 数组 + 并集 links），
       归属就没了 —— 所以这里强制一个 PMID 一次请求。代价是 N 次请求，限速 10 次/秒，
       故上限 50 个 PMID。

    〔2026-11-08 晚更正〕这里原来还有第 3 条，写的是「elink 能区分号不存在与没有全文：
    不存在的号返回的 linkset 里没有 linksetdbs 键」。**那条是错的，已作废**：
    真号 42760028（只是没有 PMC 链接）与假号 9999999999 的 linkset 逐字同形，
    拿它当判据会把有全文方向之外的真号说成「PubMed 里没有这个号」——存在性判断反了。
    现在号存在性交给 esummary（假号回空壳且带 error="cannot get document summary"，真号 43 个键），
    所以本工具每次调用会多做一次 esummary。
    """
    uniq, _ = _parse_pmids(pmids)
    db = _check_db(to)
    if len(uniq) > 50:
        raise NcbiError(f"一次最多 50 个 PMID（实测多 id 会被 NCBI 合并、归属丢失，且一个 id 一次请求），"
                        f"收到 {len(uniq)} 个，请分批")
    # 号存在性交给 esummary（理由见 _pmids_exist 的 docstring：elink 那个形状两种情况逐字相同）
    exist = await _pmids_exist(uniq)
    out = [None] * len(uniq)
    errs = []

    async def one(i, pid):
        try:
            d = _safe_json(await _get_async("elink.fcgi",
                                            {"dbfrom": "pubmed", "db": db, "id": pid,
                                             "retmode": "json"}, 25), "elink")
        except NcbiError as e:
            # 单个号失败只废这一行。上一版让它出组：50 行一起没结果，且只剩
            # 「unhandled errors in a TaskGroup (1 sub-exception)」那句没有信息的话。
            errs.append(f"PMID {pid}：{str(e)[:120]}")
            out[i] = f"PMID {pid}  请求失败（原因见表头）"
            return
        sets = d.get("linksets") or []
        if not sets:
            out[i] = (f"PMID {pid}  ⚠️ elink 没返回这个号的 linkset —— 请求没给回这个号，"
                      f"这【不是】「没有全文」，先核对 PMID")
            return
        mine, other = [], []
        for ls in sets:
            srcs = {str(x) for x in _as_list(ls.get("id") or [])} | {str(x) for x in _as_list(ls.get("ids") or [])}
            if srcs and pid not in srcs:
                continue
            for lsd in (ls.get("linksetdbs") or []):      # 注意别把循环变量也叫 db —— 那会盖掉规范化的库名
                dests = [str(x) for x in _as_list(lsd.get("links") or lsd.get("neighbors") or [])]
                if not dests:
                    continue
                name = str(lsd.get("linkname") or db)
                if "_" in name and not name.endswith(db):      # pubmed_pmc_refs / pubmed_pubmed_cites 等
                    other.append(f"{name}:{len(dests)} 条")
                else:
                    mine.append(f"{name}=" + ",".join(
                        (x if db != "pmc" else f"PMC{x}") for x in dests[:8])
                        + (f"…共 {len(dests)}" if len(dests) > 8 else ""))
        tail = ("  [" + "; ".join(other) + "]") if other else ""
        if pid not in exist:
            # 号不存在优先说：它比「有没有链接」更基本，而且 esummary 的判据是可靠的
            out[i] = f"PMID {pid}  ⚠️ PubMed 里没有这个号（esummary 只回空壳，" \
                     f"这【不是】「{db} 库无对应记录」，先核对 PMID）"
        elif mine:
            out[i] = f"PMID {pid}  " + "  ".join(mine) + tail
        else:
            out[i] = f"PMID {pid}  ({db} 库无直接对应记录)" + tail

    async with anyio.create_task_group() as tg:
        for i, pid in enumerate(uniq):
            tg.start_soon(one, i, pid)
    if errs and len(errs) == len(uniq):
        raise NcbiError(f"elink 全部 {len(errs)} 个请求都失败，第一个原因：" + errs[0])
    head = [f"elink pubmed→{db}，{len(uniq)} 个 PMID（每个一次请求，另加一次 esummary 判号是否存在）："]
    if errs:
        head.append(f"⚠️ {len(errs)} 个请求失败（那是故障，不是「没有链接」）：" + "; ".join(errs[:3]))
    # 图例必须跟着 to 变：to=pubmed 时那串「直接对应记录」是相关文献，跟全文没关系，
    # 而固定写「有全文/有数据」会把 104 条邻居读成「这篇有免费全文」。实测 37427694 to=pubmed
    # 走的就是 mine 分支（linkname="pubmed_pubmed" 以 db 结尾），第一个邻居还是这篇自己。
    legend = {"pmc": "直接对应记录＝这篇在 PMC 有免费全文可下；写「(pmc 库无直接对应记录)」= 没有 deposit",
              "gene": "直接对应记录＝NCBI 把这个号关联到了 Gene 条目",
              "nucleotide": "直接对应记录＝NCBI 把这个号关联到了核酸序列条目",
              "pubmed": "直接对应记录＝NCBI 列出的【相关文献】（含这篇自己），与有没有全文无关；"
                        "要判断能不能下载请用 to=pmc"}[db]
    return ("\n".join(head) + "\n" + "\n".join(str(x) for x in out)
            + f"\n说明：{legend}；方括号里的是 NCBI 给出的关联计数，"
              "只说明『有这个方向的链接』，不代表具体条目。"
              f"\n「({db} 库无直接对应记录)」与「PubMed 里没有这个号」是两件事（后者由 esummary 判），"
              "看到后者先回去核对 PMID。")


# ---------- 自检 ----------
def _selftest(term):
    print("key:", (api_key()[:4] + "…") if api_key() else "(无 key，走 3 次/秒)")
    t0 = time.time()
    print(anyio.run(search, term, 3))
    print(f"--- search 耗时 {time.time() - t0:.1f}s ---")
    t0 = time.time()
    print(anyio.run(count, term))
    print(f"--- count 耗时 {time.time() - t0:.1f}s ---")


async def _check(term):
    """并发跑三个查询，验证事件循环没被占住（旧版会串行阻塞）。"""
    t0 = time.time()
    results = [None, None, None]

    async def one(i, coro):
        results[i] = await coro

    async with anyio.create_task_group() as tg:
        tg.start_soon(one, 0, search(term, retmax=2))
        tg.start_soon(one, 1, count(term + "[Title]"))
        tg.start_soon(one, 2, fetch_abstracts("37427694,33485718"))
    wall = time.time() - t0
    serial = sum(len(str(r)) for r in results)
    print(f"3 个工具并发总耗时 {wall:.1f}s（旧版为串行）；返回总字数 {serial}")
    for i, r in enumerate(results):
        print(f"  [{i}] {str(r)[:80]}…")


PAGE_TERM = "\"micro-implant immediate loading\""


def _ln(text, k=1):
    """探针专用：取第 k 行（越界给占位串），永不抛。

    为什么需要：〔实测 2026-11-10 第 5 轮〕`c_yr.splitlines()[1]` 在 c_yr 为空串时 IndexError，
    整轮探针中止、后面的节全没跑 —— attempt() 包住了被测试的调用，包不住描述串。
    探针自己的措辞不是被测对象，它崩了会让「一处没测到」看起来像「全绿」。"""
    ls = (text or "").splitlines()
    return ls[k][:60] if len(ls) > k else "(无输出)"


def _row(text, prefix):
    """探针专用：取第一行以 prefix 开头的；没有就返回空串。

    返回空串而不是哨兵，是为了让依赖它的断言必须显式写 `bool(row) and ...`：
    哨兵串会让 `"不存在的东西" not in row` 这种**否定式断言**白过（探针恒真的第三种形状）。"""
    for x in (text or "").splitlines():
        if x.startswith(prefix):
            return x
    return ""


def _num_of(text):
    """从本工具自己的输出里取「命中 N 条」的 N；取不到算 0。
    探针用它，不用裸 re.search(...).group(1)：调用被 attempt() 兜住时拿到的是空串，
    裸写法会在这一行再抛一次 AttributeError —— 那又变回「崩掉」而不是「记一条 FAIL」。"""
    mm = re.search(r"命中 (\d+)", str(text))
    return int(mm.group(1)) if mm else 0


async def _probe():
    """一组断言，回归用。跑通即说明 P0 那几条修到位了。"""
    ok, fail = 0, 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  PASS  {name}")
        else:
            fail += 1
            print(f"  FAIL  {name}  {detail}")

    async def attempt_json(name, fn, *a, **kw):
        """as_json=True 那条路：先取文本再 json.loads。裸写 json.loads(await ...) 时，
        请求一旦出错，attempt 兜回来的空串会让 json.loads 抛 JSONDecodeError —— 又中止整份探针。"""
        raw = await attempt(name, fn, *a, default="", **kw)
        try:
            return json.loads(raw)
        except Exception as e:
            check(f"{name} 的 as_json 输出能解析成 JSON", False, f"{type(e).__name__}: {str(raw)[:120]}")
            return {}

    async def attempt(name, fn, *a, default="", **kw):
        """跑一个【期望成功】的调用；它抛异常就记一条 FAIL 并返回 default，后面的断言照跑。

        〔实测 2026-11-10〕不加这层之前，--probe 里任何一处意外抛错都会让整份探针中止：
        那次是第 12 节的 retstart 断言，日志显示「28 通过 / 0 失败」却根本没跑到第 13 节 ——
        崩掉的探针看起来像通过，比一条 FAIL 危险得多。期望抛错的调用仍然用 try/except 直写。
        default 用 {} 给那些返回 dict 的调用，免得后续 .get() 再抛一次。"""
        try:
            return await fn(*a, **kw)
        except BaseException as e:
            eg = getattr(e, "exceptions", None)
            real = eg[0] if isinstance(e, BaseExceptionGroup) and eg else e
            check(f"{name} 意外抛错", False, f"{type(real).__name__}: {str(real)[:200]}")
            return default

    num = _num_of        # 〔2026-11-10〕原来定义在第 13 节里，前面几处拿不到，只能裸写 re.search

    print("1) 拼错的词必须在「NCBI 实际执行」里现形")
    s = await attempt("s", search, "anchorage devcie", retmax=1, default="")
    check("querytranslation 已打印", "NCBI 实际执行" in s, s[:200])
    check("丢弃的词被点名", "已【丢弃】" in s, s[:200])

    print("2) 非法 field 必须报错，不能静默退化成全字段")
    try:
        await search("temporary anchorage device", field="ZZZBOGUSFIELD", retmax=1)
        check("field 白名单", False, "没抛异常")
    except NcbiError as e:
        check("field 白名单", "白名单" in str(e), str(e)[:120])

    print("2b) sort 不能静默失效，也不能把真生效的值当乱写的拦掉"
          "（实测：pub_date / relevance / author 会换顺序，其余与不传时同序）")
    try:
        await search("temporary anchorage device", retmax=1, sort="journal_quality")
        check("sort 白名单", False, "没抛异常")
    except NcbiError as e:
        check("sort 白名单", "静默接受" in str(e), str(e)[:120])
    s_ok = await attempt("s_ok", search, "temporary anchorage device", retmax=1, sort="pub_date", default="")
    check("合法 sort 仍能走", "查询：" in s_ok, s_ok[:80])

    print("3) field=MeSH Terms 拿不到 count 时必须抛错，不许印 '?'")
    try:
        await count("temporary anchorage device", field="MeSH Terms")
        check("count 缺失守卫", False, "没抛异常")
    except NcbiError as e:
        check("count 缺失守卫", "count" in str(e), str(e)[:160])

    print("4) 空 term 在 count 里也要拦住（旧版只有 search 拦了）")
    try:
        await count("  ")
        check("空 term 守卫", False, "没抛异常")
    except NcbiError as e:
        check("空 term 守卫", "不能为空" in str(e), str(e)[:120])

    print("5) 不存在的 PMID：报「要 N / 实取 M」并点名缺的，不再拿请求数冒充结果数")
    f = await attempt("f", fetch_abstracts, "37427694,9999999999", default="")
    check("诚实计数", "要 2 条 / 实取 1 条" in f, f[:120])
    check("缺失被点名", "9999999999" in f and "⚠️" in f, f[:200])
    f2 = await attempt("f2", fetch_abstracts, "37427694,37427694, 33485718", default="")
    check("去重不谎报", "实取 2 条" in f2 and "去掉重复" in f2, f2[:160])

    print("6) 大批量 esummary 不再撞 414（旧版 ~350 个 id 拼一个 GET 必挂）")
    t0 = time.time()
    s = await attempt("s", search, "miniscrew*", retmax=400, default="")
    n_rec = len(re.findall(r"^PMID ", s, re.M))
    check("retmax=400 出得来", n_rec >= 350, f"只有 {n_rec} 条 / {time.time() - t0:.1f}s：{s[:120]}")

    print("7) 开放获取标识出现在列表里（省一次 fetch 请求）")
    check("pmc 已输出", "pmc:PMC" in s, "400 条里没有一条 pmc:，可疑")
    check("卷页已输出", re.search(r"\d+\(\d+\)|\d+:e?\d", s) is not None, s[:200])

    print("8) PMID 输入容错：PMC 号不能冒充 PMID")
    try:
        await fetch_abstracts("PMC10461258")
        check("拒收 PMC 号", False, "没抛异常")
    except NcbiError as e:
        check("拒收 PMC 号", "PMC" in str(e), str(e)[:120])

    print("9) elink：多 id 必须各归各，不合并")
    l = await attempt("l", links, "37427694,33485718", default="")
    la = next((x for x in l.split("\n") if x.startswith("PMID 37427694")), "")
    lb = next((x for x in l.split("\n") if x.startswith("PMID 33485718")), "")
    check("有全文的那条认得出", "PMC10461258" in la, l[:200])
    check("归属不串", "PMC10461258" not in lb, lb[:120])
    check("无对应的明说", "无直接对应记录" in lb, lb[:120])

    print("10) 顶层 ERROR 不能被当成「NCBI 里没有」")
    try:
        d = _safe_json(await _get_async("elink.fcgi", {"dbfrom": "pubmed", "db": "pmc", "id": "37427694",
                                                        "retmode": "json", "cmd": "links"}, 25), "elink")
        check("ERROR 守卫", False, "没抛异常，返回：" + json.dumps(d)[:120])
    except NcbiError as e:
        check("ERROR 守卫", "not supported" in str(e), str(e)[:160])

    print("11) 中文查询：0 条必须被说成「NCBI 没解析出来」，不能当成「主题没文献」")
    c = await attempt("c", count, "牙根吸收", default="")
    check("空解析被点名", "没解析出任何检索条件" in c, c[:160])
    check("给了英文对照提示", "只索引英文" in c, c[:160])
    check("不再印空的「NCBI 实际执行」", "NCBI 实际执行" not in c, c[:160])
    s0 = await attempt("s0", search, "种植钉", retmax=1, default="")
    check("search 的 0 条也跟着改口", "不能作为" in s0, s0[:200])

    print("12) 本轮实测复现的 6 类（单边日期被丢、假「确实没匹配」、to 大小写、ExceptionGroup、"
          "PMID 粘贴形状、ta 别名）")
    c_all = await attempt("c_all", count, "temporary anchorage device", default="")
    c_lo = await attempt("c_lo", count, "temporary anchorage device", mindate="2020-01-01", default="")
    n_all = num(c_all)
    n_lo = num(c_lo)
    check("只给一端时日期真的生效", n_lo < n_all, f"只给 mindate 仍是 {n_lo} == 不限时的 {n_all}")
    check("补了哪一端在输出里写明", "日期条件" in c_lo and "由本工具补" in c_lo and "⚠️" in c_lo, c_lo[:240])
    n_rel = num(await attempt("c_rel600", count, "temporary anchorage device", reldate=600))
    check("reldate 单独给也生效", n_rel < n_all, f"reldate=600 得到 {n_rel} == 不限时的 {n_all}")
    try:
        await count("temporary anchorage device", mindate="2020年1月")
        check("乱写的日期被拦", False, "没抛异常")
    except NcbiError as e:
        check("乱写的日期被拦", "不是 NCBI 认的日期写法" in str(e), str(e)[:100])
    try:
        await count("temporary anchorage device", mindate="2021-01-01", maxdate="2020-01-01")
        check("区间给反了被拦", False, "没抛异常")
    except NcbiError as e:
        check("区间给反了被拦", "空的" in str(e), str(e)[:100])
    s_drop = await attempt("s_drop", search, "tooth resorption", retmax=3, field="journal", default="")
    check("词全被丢弃时不再断言「确实没有匹配」", "确实没有匹配这篇" not in s_drop
          and "主题没有文献" in s_drop, s_drop[:260])
    # (a) retstart 超出 NCBI 的检索窗口 → 本工具提前拦，不发请求
    #     〔实测 2026-11-10：这条以前是直接 await，NcbiError 一抛整份探针就断在这儿〕
    try:
        await search("temporary anchorage device", retmax=20, retstart=100000)
        check("retstart 超窗口被提前拦下", False, "没抛异常")
    except NcbiError as e:
        check("retstart 超窗口被提前拦下", "9998" in str(e), str(e)[:120])
    # (b) retstart 在窗口内但越过命中总数 → NCBI 正常回 0 条，必须说成「翻页越界」而不是「没有匹配」
    #     计数现场取，不写死（写死的数字会随 PubMed 更新失效，那是假断言）
    #     〔实测 2026-11-10："micro-implant immediate loading" 当时 8 条，retstart=9 得到
    #      「本次 0 条只是因为 retstart=9 越过了命中总数 8 条」〕
    n_page = _num_of(await attempt("count(PAGE_TERM)", count, PAGE_TERM))
    s_page = await attempt("s_page", search, PAGE_TERM, retmax=5, retstart=n_page + 1)
    check("翻页越过命中总数不说成「没有匹配」",
          n_page > 0 and "越过了命中总数" in s_page and "确实没有匹配" not in s_page,
          f"count={n_page}；{s_page[-200:]}")
    # retmax 的两处静默改动（实测 2026-11-10 修）
    s_big = await attempt("s_big", search, "miniscrew", retmax=5000, default="")
    check("retmax 超上限要说出是【本工具】夹的，不许看着像 NCBI 只给了这么多",
          "本工具上限" in s_big and "retmax=5000" in s_big, s_big[-320:])
    check("夹完确实只给了 1000 条以内（反向：说明不是空话）",
          "本次给 1000 条" in s_big, s_big.splitlines()[1][:60] if s_big else "")
    try:
        await search("miniscrew", retmax=-3)
        check("retmax 负数必须报错，不许静默变成 1 条", False, "没抛异常")
    except NcbiError as e:
        check("retmax 负数必须报错，不许静默变成 1 条", "负数" in str(e), str(e)[:120])
    s_zero = await attempt("s_zero", search, "miniscrew", retmax=0, default="")
    check("retmax=0 是「只要计数」，不该被静默改成 1 条",
          "本次给 0 条" in s_zero and "命中" in s_zero, s_zero[:160])
    check("as_json 的字符串形状仍由 _as_bool 处理（签名是 bool|str，pydantic 那关不拦）",
          _as_bool("True", "as_json") is True and _as_bool("off", "as_json") is False, "")
    l_up = await attempt("l_up", links, "37427694", to="PMC", default="")
    check("to 大小写不再造出假阴性", "PMC10461258" in l_up, l_up[:160])
    try:
        await links("37427694", to="pmcid")
        check("非法库名给人话报错", False, "没抛异常")
    except NcbiError as e:
        check("非法库名给人话报错", "不是本工具支持的库" in str(e), str(e)[:120])
    except BaseException as e:          # 被包成 ExceptionGroup 就是要防的那种
        check("非法库名给人话报错", False, f"{type(e).__name__}: {str(e)[:80]}")
    l_bad = await attempt("l_bad", links, "9999999999", default="")
    check("不存在的号不再和「无全文」同句", "里没有这个号" in l_bad, l_bad[:200])
    check("收得下 PMID 12345 这种粘贴", _parse_pmids("PMID 12345")[0] == ["12345"],
          str(_parse_pmids("PMID 12345")))
    check("收得下 pmid: 前缀", _parse_pmids("pmid: 37427694, 33485718")[0] == ["37427694", "33485718"], "")
    check("补零号规范成裸号", _parse_pmids("000037427694")[0] == ["37427694"], "")
    f_pad = await attempt("f_pad", fetch_abstracts, "000037427694", default="")
    check("补零号不再「实取 1 条」与「没返回」同屏", "实取 1 条" in f_pad and "没有返回" not in f_pad,
          f_pad[:180])
    check("ta 按 PubMed 自己的含义映射成 Journal", _check_field("ta") == "Journal", _check_field("ta"))
    check("tiab 仍是 Title/Abstract", _check_field("tiab") == "Title/Abstract", _check_field("tiab"))

    print("13) 第三轮审查实测到的 9 类（reldate 隐身、空交集假 0、不存在的日历日、年级粒度、"
          "field 被吞、term 内标签压过 field、引号内丢词、9999 窗口、elink 判据反了）")
    T = "temporary anchorage device"
    n_lo2 = num(await attempt("c_lo2", count, T, mindate="2020-01-01"))
    # 〔2026-11-09 改写〕原来这两条断言抓不到任何事：`n_mix < n_lo2` 是嵌套窗口必然成立的恒真式，
    # 而本轮的真 bug 恰恰是「reldate 走两条路径给出两个数」——本机日历换算把边界日切掉，
    # dental implant 从 770 变 763（少的 7 条＝2026-09-09 那一天）。所以判据换成
    # 「加一个不可能生效的宽区间，数必须逐字相同」。
    n_rel_only = num(await attempt("n_rel_only", count, T, reldate=60))
    n_rel_wide = num(await attempt("n_rel_wide", count, T, reldate=60, mindate=NO_BEGIN, maxdate=NO_END))
    check("reldate 加一条不该改变结果的宽区间，命中数必须一模一样",
          n_rel_only == n_rel_wide, f"单给 {n_rel_only} vs 配宽区间 {n_rel_wide}（旧版这里差 7 条）")
    c_mix = await attempt("c_mix", count, T, mindate="2020-01-01", reldate=60, default="")
    check("两段日期同给时必须写清是 NCBI 做两段 AND", "两段 AND" in c_mix and "reldate" in c_mix, c_mix[:300])
    # 〔已作废 2026-11-10〕这里原本是 `f"→ {_today()}" not in c_mix`，那条判据本身是错的：
    #   它只在「本机日历 ≠ NCBI 服务器日历」的那几天才有效，两天一旦相同（今天就是），
    #   代码明明是对的（窗口就是从回显读的）它却报 FAIL —— 一个会随日期自己变红/变绿的断言
    #   不是判据，是噪声。实测：本机＝服务器＝2026-11-10 时旧断言 FAIL，而打印出的
    #   2026-09-11 → 2026-11-10 恰好逐字来自回显。
    # 换成两条都能真正证伪的判据：
    #   (a) 打印出来的每个窗口，把横杠换成斜杠后必须【逐字出现在同一行的 NCBI 回显里】
    #       —— 本机算出来的窗口做不到这一点（NCBI 回显用斜杠，见 _windows 的 docstring）；
    #   (b) 哨兵区间那次必须打印出 2999-12-31 —— 这个值本机日历永远算不出来，
    #       它出现在输出里就证明「窗口是从回显读的」这条链是活的。
    _SL = chr(47)
    _qt_line = (re.search(r"NCBI 实际执行：(.*)", c_mix) or [None, ""])[1]
    _printed = re.findall(r"(\d{4}-\d{2}-\d{2}) → (\d{4}-\d{2}-\d{2})", c_mix)
    check("打印的每个窗口逐字来自 NCBI 回显（横杠换斜杠后能在回显里找到）",
          bool(_printed) and all(a.replace("-", _SL) in _qt_line and b.replace("-", _SL) in _qt_line
                                 for a, b in _printed),
          f"打印 {_printed}｜回显前 160：{_qt_line[:160]}")
    c_senti = await attempt("c_senti", count, T, reldate=60, mindate=NO_BEGIN, maxdate=NO_END, default="")
    check("哨兵上界 2999-12-31 出现在输出里（本机日历算不出这个数）",
          "2999-12-31" in c_senti and "0001-01-01" in c_senti, c_senti[:260])
    d_dis = await attempt("d_dis", count, T, mindate="2000-01-01", maxdate="2024-12-31", reldate=60, default="")
    check("两段窗口不相交时点明（这种情况 0 条不代表主题没文献）",
          "不相交" in d_dis, d_dis[:300])
    s_dis = await attempt("s_dis", search, T, mindate="2000-01-01", maxdate="2024-12-31", reldate=60, retmax=5, default="")
    check("不相交的 0 条不能说成「确实没有匹配这篇」",
          "不相交" in s_dis and "确实没有匹配这篇" not in s_dis, s_dis[-260:])
    d_rel = await attempt("d_rel", _esearch, T, retmax=0, reldate=365, default={})
    check("reldate 单给也按发表日期口径", "[Date - Publication]" in (d_rel.get("querytranslation") or ""),
          (d_rel.get("querytranslation") or "")[-70:])
    for bad_d in ("2023-02-29", "2020-13-01", "2020-02-30", "2020-00-00"):
        try:
            await count(T, mindate=bad_d)
            check(f"日历上不存在的 {bad_d} 被拦", False, "没抛异常（NCBI 对这种值静默给 0 条）")
        except NcbiError as e:
            check(f"日历上不存在的 {bad_d} 被拦", "日历上没有这一天" in str(e), str(e)[:90])
    c_leap = await attempt("c_leap", count, T, mindate="2020-02-29", default="")
    check("真闰日不误伤", num(c_leap) > 0, c_leap[:120])
    c_yr = await attempt("c_yr", count, T, mindate="2021-01-01", maxdate="2021", default="")
    c_yr2 = await attempt("c_yr2", count, T, mindate="2021-01-01", maxdate="2021-12-31", default="")
    # 〔2026-11-09 改写〕旧断言只查「输出里有『命中』两个字」——count() 每一次成功都有这两个字，
    # 恒真。现在比的是「NCBI 把 2021 展开成 2021-12-31」这件事本身：两种写法必须同数。
    # 〔2026-11-10 第 5 轮〕这一条刚抓到过一次真回归：倒挂守卫逐字比字符串，
    # 把合法的 (2021-01-01, 2021) 拦成倒挂 → c_yr 抛错、num("")==0 与 c_yr2 的真数不等 = FAIL。
    # 所以断言拆两条：先确认【没被拦】（旧写法只比数值，被拦时 num 都是 0 会假过），再比同数。
    check("年级 maxdate 配完整 mindate 不许被误判成倒挂（c_yr 必须有命中行）",
          "命中" in c_yr, _ln(c_yr, 0) + " | " + _ln(c_yr2, 0))
    check("年级 maxdate 与写成 12-31 的必须同数",
          "命中" in c_yr and "命中" in c_yr2 and num(c_yr) == num(c_yr2),
          f"{_ln(c_yr)} vs {_ln(c_yr2)}")
    try:
        _ = await count(T, mindate="3000-01-01")
        check("真倒挂（mindate 在 NO_END 之后）仍然要抛，不许被守卫放宽成静默 0 条",
              False, "没抛异常")
    except NcbiError as e:
        check("真倒挂（mindate 在 NO_END 之后）仍然要抛，不许被守卫放宽成静默 0 条",
              "倒挂" in str(e), str(e)[:120])
    for sw in ("keywords", "pmcid", "issn"):
        try:
            await count("miniscrew", field=sw)
            check(f"field={sw}（实测被 NCBI 整条吞掉）不再静默给不限定的数", False, "没抛异常")
        except NcbiError as e:
            check(f"field={sw}（实测被 NCBI 整条吞掉）不再静默给不限定的数", "整条忽略" in str(e), str(e)[:90])
    c_tag = await attempt("c_tag", count, "miniscrew[Title]", field="journal", default="")
    check("term 里写了概念级标签时点破 field 被顶掉", "term 内的标签赢" in c_tag, c_tag[:300])
    # 〔实测 2026-11-09〕反向案例：term 里只有 [dp] 这种日期标签时 field 照样生效，
    # 旧写法却因为「term 里有方括号」宣布 field 被丢，还给出一条会把查询改坏的建议。
    c_dptag = await attempt("c_dptag", count, "tooth resorption AND 2020:2026[dp]", field="Title", default="")
    check("term 里只有日期标签时不能说 field 被丢", "这个限定生效了" in c_dptag
          and "term 内的标签赢" not in c_dptag, c_dptag[:320])
    # 〔实测 2026-11-10〕NCBI 的 warninglist 这一支以前全文没读过：这两条 0 查询
    #   的 _note 是空的，结尾却印「PubMed 里确实没有匹配这篇」——那是把 NCBI 明说的
    #   「短语不存在 / 词全被当停用词」写成「主题没文献」。
    s_stop = await attempt("s_stop", search, "the and of", retmax=1, default="")
    check("词全被当停用词时，不许说「确实没有匹配这篇」",
          "停用词" in s_stop and "确实没有匹配这篇" not in s_stop, s_stop[-320:])
    check("停用词那条要给对建议（换实词，不是换字段）",
          "换成语义实词" in s_stop and "换成有索引的字段" not in s_stop, s_stop[-260:])
    # 〔实测 2026-11-10〕引号短语不存在但 NCBI 换成按词 AND、仍有命中：这一支必须说清
    #   「命中数是替代式的」，不能只说「短语找不到」就完事。
    s_pf = await attempt("s_pf", search, PAGE_TERM, retmax=3, default="")
    check("短语被换成按词 AND 且有命中时，要说清那个数不是短语的数",
          _num_of(s_pf) > 0 and "按词 AND" in s_pf and "不是" in s_pf, s_pf[-300:])
    s_qp = await attempt("s_qp", search, '"zzzqqxnonexistentterm"', retmax=1, default="")
    check("输出里不许抄旧错句当历史说明（第 7 轮就是这里自己踩了自己的断言）",
          "确实没有匹配这篇" not in s_pf and "确实没有匹配这篇" not in s_qp,
          (s_pf[-120:] + " || " + s_qp[-120:])[:240])
    check("整个引号短语查不到时要说出来，且不说成主题没文献",
          "整个引号短语" in s_qp and "确实没有匹配这篇" not in s_qp, s_qp[-320:])
    c_ig = await attempt("c_ig", count, "the and of", default="")
    check("count() 也不能漏掉 warninglist（它和 search 走同一套说明）",
          "停用词" in c_ig, c_ig[:260])
    for messy in ("1,234,567", "PMID 1,234,567", "12,345"):
        try:
            await fetch_abstracts(messy)
            check(f"带千位分隔符的 {messy!r} 必须拦住，不许切成三个真号", False, "没抛异常")
        except NcbiError as e:
            check(f"带千位分隔符的 {messy!r} 必须拦住，不许切成三个真号",
                  "千位分隔符" in str(e), str(e)[:110])
    check("普通逗号列表仍然是多个号（反向：新拦法没把正常用法杀掉）",
          _parse_pmids("12345,678")[0] == ["12345", "678"] and
          _parse_pmids("12345;678")[0] == ["12345", "678"], str(_parse_pmids("12345,678")))
    c_q = await attempt("c_q", count, '"xyzzy tooth resorption"', default="")
    ql = [l for l in c_q.splitlines() if "引号短语里有词" in l]
    # 〔2026-11-09 改写〕旧断言写成 `"xyzzy" in c_q`，而查询式自己就被逐字回显在第一行，恒真。
    # 现在只盯那行 ⚠️ 本身。
    check("引号内丢词必须点名（NCBI 不填 phrasesnotfound）", bool(ql) and "xyzzy" in ql[0], c_q[:300])
    c_q2 = await attempt("c_q2", count, '"cone beam computed tomography"', default="")
    check("正常引号短语不误报", "引号短语里有词" not in c_q2, c_q2[:200])
    s_cap = await attempt("s_cap", search, "orthodontics", retmax=10, retstart=9990, default="")
    check("贴到 9999 检索窗口上限时说明白", "9999" in s_cap and "永远翻不到" in s_cap, s_cap[:300])
    j_cap = await attempt_json("j_cap", search, "orthodontics", retmax=10, retstart=9990, as_json=True)
    # 〔实测 2026-11-09〕旧版的 as_json 把这句整个丢了，而 docstring 又叫脚本用 as_json：
    # 最需要「这批没取全」的人恰好拿不到它。
    check("as_json 也必须带上窗口到顶的警告", "永远翻不到" in (j_cap.get("window_warning") or "")
          + " ".join(j_cap.get("warnings") or []), str(j_cap.get("window_warning"))[:200])
    for rs in (9999, 10000):
        try:
            await search("orthodontics", retmax=5, retstart=rs)
            check(f"retstart={rs} 越限要给人话而不是 NCBI 原文 blob", False, "没抛异常")
        except NcbiError as e:
            check(f"retstart={rs} 越限要给人话而不是 NCBI 原文 blob",
                  "9998" in str(e) and "不是合法 JSON" not in str(e), str(e)[:160])
    s_edge = await attempt("s_edge", search, "orthodontics", retmax=2, retstart=9998, default="")
    check("retstart=9998 是合法边界、不该被自己拦死", "命中" in s_edge, s_edge[:120])
    l_real = await attempt("l_real", links, "42760028", default="")
    row_real = _row(l_real, "PMID ")
    check("links 对真号至少印出一行 PMID 数据（否则下面两条否定式断言无从判起）",
          bool(row_real), (row_real or l_real)[:200])
    # 只查这一行：结尾那段图例本来就要解释「无对应记录」与「号不存在」是两件事，
    # 拿整屏做 not in 会把说明文字当成失效（第一版就是这么写错的，看着像 bug 其实是断言太糙）。
    check("真号没有 PMC 链接时不能说成「号不存在」",
          bool(row_real) and "里没有这个号" not in row_real, row_real[:220])
    check("这种情况写成「库无直接对应记录」",
          bool(row_real) and "无直接对应记录" in row_real, row_real[:220])
    l_to = await attempt("l_to", links, "37427694", to="pubmed", default="")
    row_to = _row(l_to, "PMID ")
    check("to=pubmed 至少印出一行 PMID 数据", bool(row_to), (row_to or l_to)[:200])
    # 〔2026-11-09 改写〕旧断言查的是结尾那段图例，而图例本来就要同时解释两件事，
    # 「相关文献」是无条件印的固定文字 → 恒真。现在只盯这个 PMID 的数据行。
    check("to=pubmed 的数据行不说成「有全文」", bool(row_to) and "全文" not in row_to, row_to[:220])
    check("to=pubmed 给的是 NCBI 的关联链接名", ("pubmed_pubmed" in row_to) or ("无直接对应记录" in row_to),
          row_to[:220])
    # 14) 第四轮：审查代理报出来、我逐条实测复现的几类
    print("14) 第四轮实测（reldate 取值范围、早于 1809 的题录、全词被丢还带布尔算子、链接 docstring 形状）")
    for bad_rel in (-30, 10 ** 9):
        try:
            await count(T, reldate=bad_rel)
            check(f"reldate={bad_rel} 必须报错", False, "没抛异常（旧版：负数被夹成 0＝不限日期；大数抛裸 OverflowError）")
        except NcbiError as e:
            check(f"reldate={bad_rel} 必须报错", True, str(e)[:120])
        except Exception as e:
            check(f"reldate={bad_rel} 必须报错", False, f"{type(e).__name__}（不是 NcbiError）：{str(e)[:90]}")
    c_old = await attempt("c_old", count, "tooth", maxdate="1800-01-01", default="")
    check("早于 1809 的区间不能报 0（旧版补的下界 1809-01-01 会把这段整个切掉）",
          num(c_old) > 0 and "0001-01-01" in c_old, c_old[:300])
    s_old = await attempt("s_old", search, "tooth", maxdate="1800-01-01", retmax=3, default="")
    check("这段真文献不能说成「确实没有匹配这篇」", "确实没有匹配这篇" not in s_old, s_old[-240:])
    s_drop = await attempt("s_drop", search, "zzzqqx AND qbvcjk", retmax=5, default="")
    check("全词被丢＋布尔算子时不许下「确实没有匹配」的结论",
          "全部丢弃" in s_drop and "确实没有匹配这篇" not in s_drop, s_drop[-300:])
    s_keep = await attempt("s_keep", search, "tooth resorption", retmax=1, default="")
    check("有词的查询仍正常（反向：上面那条不是无条件生效）", "命中" in s_keep, s_keep[:120])
    j_norm = await attempt_json("j_norm", search, "miniscrew", retmax=2, as_json=True)
    # 〔已作废 2026-11-10〕原断言只查「键在不在 dict 里」，而这两个键是无条件写的 —— 永远为真；
    #  用的查询（miniscrew, retmax=2）又根本没有日期条件，值为空也算过。
    #  换成【带日期条件的查询 + 值必须非空】，这样日期逻辑一坏它就会红。
    j_dated = await attempt_json("j_dated", search, T, retmax=2, as_json=True, reldate=60)
    check("as_json 在真带日期条件时给出非空的窗口与 daterange",
          bool(j_dated.get("executed_windows")) and bool((j_dated.get("daterange") or "").strip()),
          str({k: j_dated.get(k) for k in ("executed_windows", "daterange")})[:240])
    j_norm = await attempt_json("j_norm", search, "miniscrew", retmax=2, as_json=True)
    check("没有日期条件时 executed_windows 必须真的是空列表（不是漏填）",
          j_norm.get("executed_windows") == [], str(j_norm.get("executed_windows"))[:120])
    s_bm = await attempt("s_bm", search, T, retmax=2, sort="best match")
    check("sort=best match 被改写成 relevance 这件事必须说出来",
          "改写" in s_bm and "relevance" in s_bm, s_bm[:300])
    check("sort=author 现在被接受（实测它真会换顺序）", "author" in SORT_REAL, "")
    ids_plain = (await attempt("ids_plain", _esearch, T, retmax=5, default={})).get("idlist") or []
    ids_author = (await attempt("ids_author", _esearch, T, retmax=5, sort="author", default={})).get("idlist") or []
    check("sort=author 的 idlist 与不传 sort 不同（反向：白名单不是白加）",
          bool(ids_plain) and bool(ids_author) and ids_plain != ids_author,
          f"默认 {ids_plain[:3]} vs author {ids_author[:3]}")
    ids_journal_req = (await attempt("ids_pdat", _esearch, T, retmax=5, sort="pub_date",
                                     default={})).get("idlist") or []
    check("三种真实顺序两两不同（pub_date / author / 默认）",
          len({tuple(ids_plain), tuple(ids_author), tuple(ids_journal_req)}) == 3,
          f"{ids_plain[:2]} | {ids_author[:2]} | {ids_journal_req[:2]}")
    f_mis = await attempt("f_mis", fetch_abstracts, "37427694,9999999999", default="")
    check("缺条目不再被解释成「这篇没摘要」", "没有摘要的社论照样返回记录" in f_mis
          and "9999999999" in f_mis, f_mis[:260])

    print("15) 日期窗口解析（全离线；形状用 chr(47) 拼，因为工具渲染会把斜杠显示成横杠）")
    SL = chr(47)
    q_one = f"2026{SL}11{SL}08[Date - Publication]"
    q_rng = f"2020{SL}01{SL}01:2024{SL}12{SL}31[Date - Publication]"
    q_reld = f"2026{SL}09{SL}11:2026{SL}11{SL}10[Date - Publication]"
    q_edat = f"2020{SL}01{SL}01:2020{SL}12{SL}31[Date - Entry]"
    q_2019 = f"2019{SL}01{SL}01:2019{SL}12{SL}31[Date - Publication]"

    w_rng = _windows(q_rng)
    check("斜杠形状解析得出窗口（NCBI 回显的真实形状）",
          w_rng == [("2020-01-01", "2024-12-31", "发表日期[pdat]")], str(w_rng))
    check("横杠形状也认（容错，别只认一种）",
          _windows("2020-01-01:2024-12-31[Date - Publication]") == w_rng,
          str(_windows("2020-01-01:2024-12-31[Date - Publication]")))
    w_one = _windows(q_one)
    check("单日窗口（回显没有冒号后半截）解析成起＝止",
          w_one == [("2026-11-08", "2026-11-08", "发表日期[pdat]")], str(w_one))
    check("窗口带回日期字段名（edat 与 pdat 分得开）",
          [x[2] for x in _windows(f"a AND {q_edat} AND {q_reld}")] == ["入库日期[edat]", "发表日期[pdat]"],
          str(_windows(f"a AND {q_edat} AND {q_reld}")))
    check("无日期的查询不产窗口", _windows('"miniscrew"[Title]') == [], str(_windows('"miniscrew"[Title]')))
    check("qt 为空/None 不炸", _windows(None) == [] and _windows("") == [], "")

    def _dd(qt, rel=0, dr=""):
        return {"querytranslation": qt, "_term": "t", "_rel": rel, "_daterange": dr,
                "_windows": _windows(qt)}

    def _has_disjoint(d):
        return any("不相交" in x for x in _note(d))

    check("跨字段两段（入库日 vs 发表日）不许说「不相交」",
          not _has_disjoint(_dd(f"a AND {q_edat} AND {q_reld}", 60, "两段 AND")),
          str(_note(_dd(f"a AND {q_edat} AND {q_reld}", 60, "两段 AND")))[:400])
    check("同字段互斥要说「不相交」",
          _has_disjoint(_dd(f"a AND {q_2019} AND {q_reld}", 60, "两段 AND")), "")
    check("互斥全写在 term 里（没传 mindate/reldate）也要说到",
          _has_disjoint(_dd(f"a AND {q_2019} AND 2035{SL}01{SL}01:2035{SL}12{SL}31[Date - Publication]")), "")
    check("单日与区间真互斥时判得出（2024-11-08 vs 近 60 天）",
          _has_disjoint(_dd(f"a AND 2024{SL}11{SL}08[Date - Publication] AND {q_reld}", 60, "两段 AND")), "")
    check("同字段相容时【不】说不相交（反向：判据不是无条件生效）",
          not _has_disjoint(_dd(f"a AND 2026{SL}01{SL}01:2026{SL}12{SL}31[Date - Publication] AND {q_reld}",
                                60, "两段 AND")), "")
    check("只有一段时【不】说不相交（反向）", not _has_disjoint(_dd(f"a AND {q_reld}", 60, "近 60 天")), "")
    _d_dis = _dd(f"a AND {q_2019} AND {q_reld}", 60, "两段 AND")
    check("0 条解释走「不相交」那一支，不说「确实没有匹配这篇」",
          "不相交" in _zero_explanation(_d_dis, 0) and "确实没有匹配这篇" not in _zero_explanation(_d_dis, 0),
          _zero_explanation(_d_dis, 0)[:200])

    print("\n16) warninglist 解析（全离线：自己造 NCBI 的返回形状，不发请求）")
    fake = {"_term": "the and of", "querytranslation": "the and of", "count": "0",
            "warninglist": {"phrasesignored": ["the", "and", "of"],
                            "quotedphrasesnotfound": [], "outputmessages": ["No items found."]}}
    ig2, qn2, ot2 = _warn_bits(fake)
    check("phrasesignored 被取出来", ig2 == ["the", "and", "of"], str(ig2))
    check("outputmessages 里那句 0 条回声不重复印", ot2 == [], str(ot2))
    check("全停用词 → _all_dropped 判 True（旧版这里判 False，于是跟着印假话）",
          _all_dropped(fake) is True, "")
    check("全停用词的 0 条解释不说「确实没有匹配这篇」",
          "确实没有匹配这篇" not in _zero_explanation(fake, 0), _zero_explanation(fake, 0)[:150])
    fake2 = {"_term": '"zzz qq"', "querytranslation": '"zzz qq"', "count": "0",
             "warninglist": {"phrasesignored": [], "outputmessages": ["No items found."],
                             "quotedphrasesnotfound": ['"zzz qq"']}}
    check("quotedphrasesnotfound 走自己那一支，不说成停用词",
          "整个引号短语" in _zero_explanation(fake2, 0), _zero_explanation(fake2, 0)[:150])
    check("warninglist 缺失/不是 dict 时不炸", _warn_bits({"nope": 1}) == ([], [], []), "")
    check("_warn_bits 对 warninglist 是字符串的怪形状也不炸", _warn_bits({"warninglist": "x"}) == ([], [], []), "")
    # 反向：有 warninglist 但【不是】全丢的情形，不许误报成「条件为空」
    fake3 = {"_term": "the miniscrew", "querytranslation": "miniscrew", "count": "700",
             "warninglist": {"phrasesignored": ["the"], "quotedphrasesnotfound": [],
                             "outputmessages": []}}
    check("只丢了一个虚词、还有实词时不许说「生效条件为空」",
          _all_dropped(fake3) is False, str(_note(fake3))[:200])

    print(f"\n{ok} 通过 / {fail} 失败")
    return fail


def _check_key():
    k = api_key()
    print("key:", (k[:4] + "…" + f"（长度 {len(k)}）") if k else "(无)")
    print("限速档位:", "10 次/秒" if k else "3 次/秒")
    print("EMAIL:", EMAIL or "(未设 NCBI_EMAIL，NCBI 的 courtesy 要求建议设上)")
    print("依赖:", end=" ")
    try:
        from importlib.metadata import version
        print(", ".join(f"{p} {version(p)}" for p in ("mcp", "anyio", "pydantic")))
    except Exception as e:
        print("读版本失败", e)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--selftest":
        _selftest(args[1] if len(args) > 1 else "tooth resorption")
    elif args and args[0] == "--probe":
        sys.exit(1 if anyio.run(_probe) else 0)
    elif args and args[0] == "--concurrent":
        anyio.run(_check, args[1] if len(args) > 1 else "tooth resorption")
    elif args and args[0] == "--check-key":
        _check_key()
    else:
        mcp.run()          # stdio

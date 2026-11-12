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
from typing import Literal

import anyio
from pydantic import StrictInt, StrictStr

# ---------- SDK 兼容层（放在 NcbiError 之前，因为它的基类要从 SDK 取） ----------
# 〔实测 2026-11-10，本会话两条通路各跑过完整 --probe〕
#   mcp 1.x：  mcp.server.fastmcp.FastMCP → 1.29.0（本机）/ 1.30.0（干净 venv）
#   mcp 2.x：  该模块名【不存在】，改名 mcp.server.mcpserver.MCPServer → 2.3.0
# 本文件对 SDK 只用到三处，两代签名都兼容：`FastMCP(名字)`（v2 第一参数仍是 name）、
# `@mcp.tool()` 裸装饰、`mcp.run()` 默认 stdio。逐字签名见 docs/mcp-sdk-v2.md。
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
__version__ = "1.4.0"         # 本项目自订发布版本（与 pyproject.toml 的 project.version 一致）
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
    # 〔本会话实测过 mcp 1.2/1.21/1.26 配新 pydantic 的 import 结果，见 docs/mcp-sdk-v2.md〕
    raise ImportError(
        "mcp 装了但导入失败（%s）。这一般是版本太老或与 pydantic 冲突，不是本文件的问题。"
        "先跑 `pip install -U \"%s\"`；还不行请把这条报错和 `pip show mcp pydantic` 一起贴到 "
        "https://github.com/rsvsv/pubmed-mcp/issues（这里不会带上你的 API key）。"
        "底层错误：%s" % (_mcp_ver(), _SDK_RANGE, str(_sdk_imp_err)[:200])
    ) from _sdk_imp_err

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


def _decode_key_bytes(raw):
    """把 key 文件的字节变成字符串，兼容 Windows 上常见的三种写法。

    〔实测 2026-11-10，修 A/B 两条〕旧写法只认「UTF-8 无 BOM」，而别人建这个文件时
    默认拿到的往往不是它：
      - PowerShell 5.1 的 `>` / Out-File 默认写 **UTF-16LE + BOM**（头两字节 FF FE），
        旧代码 io.open(encoding="utf-8") 读它会抛 UnicodeDecodeError（byte 0xff invalid start
        byte），`--check-key` 与首次工具调用一起变成裸 traceback。
      - 记事本「UTF-8（带 BOM）」→ 前三个字节 EF BB BF，decode("utf-8") 后留下 \ufeff；
        `\ufeff` 不是空白，str.strip() 不掉它，于是发出去的 apikey 前多一个不可见字符，
        NCBI 判成无效 key，而错误文本里看不出是编码的锅。
      - 真 UTF-8 无 BOM：照旧。
    所以：先看 BOM 定编码，再兜一次 utf-8-sig，最后 latin-1 保命（宁可让 NCBI 报 key 无效，
    也不要在这行抛 traceback）。BOM 一律 lstrip 掉。
    """
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            text = raw.decode("utf-16")                # PowerShell 默认
        except UnicodeDecodeError:
            # 〔实测 2026-11-11〕截断的奇数长度（如 FF FE 41）会在这一行抛裸 UnicodeDecodeError，
            # 绕过下面承诺的 latin-1 兜底。用 replace 让流程继续，最终由 NCBI 判 key 无效。
            text = raw.decode("utf-16", errors="replace")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw.decode("utf-8-sig")                 # 记事本 UTF-8（带 BOM）
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    return text.lstrip("\ufeff")


def api_key():
    """缓存一次读取 —— 旧版每个请求都 stat+读文件。

    找不到 key 不是错误：走匿名档（3 次/秒），功能不缺，只是慢一档；
    `--check-key` 会把当前档位和 key 文件的编码一起说出来。"""
    global _KEY, _KEY_SOURCE
    if _KEY is None:
        k = (os.environ.get("NCBI_API_KEY") or "").strip().lstrip("\ufeff")
        src = "环境变量 NCBI_API_KEY" if k else ""
        if not k:
            for p in ("~/.ncbi_api_key", "~/.ncbi_api_key.txt"):
                q = os.path.expanduser(p)
                if os.path.isfile(q):
                    try:
                        with open(q, "rb") as key_file:
                            raw = key_file.read()
                    except OSError:
                        continue
                    # 吃掉首尾空白、中间连续空白压成一个空格；key 中间真夹着空格时压不掉，由 _key_shape 报出来
                    cand = " ".join(_decode_key_bytes(raw).split())
                    if not cand:
                        # 〔实测 2026-11-11〕空文件旧版会 break，把后面有效的 .ncbi_api_key.txt 挡住。
                        continue
                    k, src = cand, f"文件 {q}"
                    break
        _KEY, _KEY_SOURCE = k, src
    return _KEY


def _key_shape(k):
    """给 --check-key 用：说出 key 长什么样，但**不打印值本身**。

    只判**判得动的**事：有没有混进空白或说明文字（这一类真会让请求 400，而且报错里看不出来）。
    长度与进制**只描述、不当判据**：〔实测 2026-11-10，本机 --check-key 抓到我自己〕补丁 20 里
    我写过「NCBI 的 key 通常是 32 位十六进制」，结果本机那串比这长、也不是恰好 32 位，于是这句把正常配置
    报成了可疑。「32 位」是顺来的常识，没查 NCBI 页面、也没统计过〔未核实〕，所以现在删掉。
    要以长度为准请回你自己账号页上看的那串。"""
    if not k:
        return "(无 key，走匿名档 3 次/秒)"
    bits = f"长度 {len(k)}"
    bad = sorted({c for c in k if not (c.isalnum() or c in "-_")})
    if bad:
        return f"{bits}，含 {len(bad)} 个非字母数字字符（{bad[:3]}）——多半是文件里混进了别的文字"
    hexish = all(c in "0123456789abcdefABCDEF" for c in k)
    return (f"{bits}，纯十六进制" if hexish else
            f"{bits}，含非十六进制字符（若你账号页上那串是纯十六进制而这份不是，多半抄漏或抄错了字符）")


def _mask(s):
    """异常文本里不要带出 api_key。

    〔发布版改动 2026-11-10；来源：审计 finding privacy-04，本会话已实测复现前两条〕旧写法有三个洞：
      一、掩码完仍留前 4 位；二、`len(k) > 8` 才启用掩码，短 key 整串原文外泄；
      三、只做字面 str.replace，key 以 URL 百分号编码形态出现在报错文本里时替换不到。
    现在无条件替换成定长掩码，一个字符的原文都不留。第三条仍未处理——先解码再替换属于过度设计，
    记在这儿当已知边界，别当成已修好。"""
    k = api_key()
    if not (k and isinstance(s, str)):
        return s
    # 旧注释的「第三条仍未处理」已作废：本轮同时替换 URL 编码形式，见离线回归。
    variants = {k, urllib.parse.quote(k, safe=""), urllib.parse.quote_plus(k)}
    for value in sorted(variants, key=len, reverse=True):
        encoded = re.escape(value)
        if "%" in value:
            encoded = re.sub(r"%([0-9A-Fa-f]{2})", lambda m: "(?i:%" + m[1] + ")", encoded)
        s = re.sub(encoded, "***", s)
    return s


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
            raise NcbiError(_mask(f"{what} 收到的不是合法 JSON（{type(e).__name__}）；正文前 300 字：{text[:300]!r}"))
    if not isinstance(d, dict):
        raise NcbiError(f"{what} 返回的 JSON 顶层不是对象，不能作为成功结果")
    esr = d.get("esearchresult")
    err = d.get("ERROR") or d.get("error")
    for envelope in ("header", "eutilsresult"):
        nested = d.get(envelope)
        if not err and isinstance(nested, dict):
            err = nested.get("ERROR") or nested.get("error")
    if not err and isinstance(esr, dict):
        err = esr.get("ERROR")      # retstart 越限这类错误就藏在这里，而且没有 count
    if err:
        raise NcbiError(_mask(f"{what} 被 NCBI 拒绝：{str(err)[:400]}"))
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
            delay = self._next - now
            if delay > 0:
                await anyio.sleep(delay)
            # 只为真正放行的请求推进时钟；取消等待不会留下预约空洞。
            self._next = time.monotonic() + self.interval


_PACER = None
_PACER_INIT = threading.Lock()


async def _pace():
    global _PACER
    with _PACER_INIT:
        if _PACER is None:
            _PACER = _Pacer(10.0 if api_key() else 3.0)   # 实测：有 key 10 次/秒，无 3 次/秒
    await _PACER.wait()


# ---------- HTTP ----------
def _retry_after(headers):
    ra = headers.get("Retry-After") if headers else None
    if not ra:
        return None
    ra = str(ra).strip()
    if re.fullmatch(r"[0-9]+", ra):
        try:
            return float(ra)
        except (ValueError, OverflowError):
            return None
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        return max(0.0, (parsedate_to_datetime(ra) - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None                                       # HTTP-date 解析不了就退回固定退避


_NREQ = 0          # 本轮发出去多少次 urlopen（含退避重试的每一次）


def _get(path, params, timeout, tries=4, *, before_attempt=None, wait=None):
    """带 key 和退避重试的 GET，返回响应文本。失败抛 NcbiError 并带 NCBI 原文。
    同步函数，只允许在 _get_async 的线程里调用。

    _NREQ 按**每次 urlopen** 计数，不是按工具调用次数：一次 429 退避重试算 4 次。
    多线程下 `+=` 不保证精确〔自订口径〕，它只用来回答「这一轮探针花了多少请求」这个量级问题，
    别拿它当计费依据。"""
    global _NREQ
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
    # 〔实测 2026-11-10 改〕旧写法是「前 4 位 {key[:4]}…」：那是**安装者自己的** key 片段，
    # 而这句话会随错误文本进对话记录。改成只报长度——判「有没有带上 key」用长度就够了，
    # 与补丁 21 之后 _key_shape 的口径一致（那儿也不打印任何 key 内容）。
    keyhow = (f"本次带上 api_key（长度 {len(key)}）" if key
              else "本次【没带】api_key（走匿名 3 次/秒）")
    last = "没试过"
    backoff = (1, 3, 9)
    wait = time.sleep if wait is None else wait
    for attempt in range(tries):
        if before_attempt is not None:
            before_attempt()    # 每次网络尝试都节流；取消检查也在这里执行
        try:
            req = urllib.request.Request(url, headers={"User-Agent": TOOL})
            _NREQ += 1
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
                delay = _retry_after(e.headers)
                # 本项目自订等待上限：过长 Retry-After 直接失败，不提前重试违反服务端要求。
                if delay is not None and delay > 60:
                    raise NcbiError(_mask(f"{path} 失败：{last}；Retry-After={delay:g}s 超过本工具等待上限，请稍后重试"))
                wait(backoff[min(attempt, 2)] if delay is None else delay)
                continue
            raise NcbiError(_mask(f"{path} 失败：{last}（{keyhow}）"))   # 400/414 这种重试没意义
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if attempt == tries - 1:
                break
            wait(backoff[min(attempt, 2)])
    raise NcbiError(_mask(f"{path} 试了 {tries} 次仍失败：{last}（{keyhow}）"))


def _thread_before_attempt():
    anyio.from_thread.check_cancelled()
    anyio.from_thread.run(_pace)
    anyio.from_thread.check_cancelled()


def _thread_wait(delay):
    anyio.from_thread.check_cancelled()
    anyio.from_thread.run(anyio.sleep, delay)
    anyio.from_thread.check_cancelled()


async def _get_async(path, params, timeout, tries=4):
    # 线程中的 urllib 正在读 socket 时仍受 socket timeout 约束；取消后不再发新重试。
    return await anyio.to_thread.run_sync(
        functools.partial(_get, path, params, timeout, tries,
                          before_attempt=_thread_before_attempt, wait=_thread_wait))


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
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        raise NcbiError(f"{name} 要整数，收到 {v!r}；不接受布尔或浮点数")
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
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
            f'（PubMed 没有「按期刊好坏排序」这个概念，所以这里不假装能排；'
            f'要那种排序得自己接外部期刊指标数据源——本仓库不含这类脚本。）')
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
    # 〔实测 2026-11-11〕`\d` 匹配的是所有 Unicode 十进制数字：'٢٠٢٠-٠١-٠١'（阿拉伯-印度数字）
    # 能过正则，但原样下发会被 NCBI 丢掉 —— count 0、querytranslation 里 mindate 整段消失，
    # 而输出仍印着「日期条件」。这里用 int() 归一成 ASCII（int('٢٠٢٠')==2020，全角数字同理），
    # 绝不把用户的数字原样发走。
    p = [str(int(x)).zfill(2) for x in s.replace("/", "-").split("-")]
    p[0] = p[0].zfill(4)
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
    try:
        _rs_req = int(str(retstart).strip())
    except (TypeError, ValueError):
        _rs_req = None                      # 不是数字就交给 _int 报人话
    if _rs_req is not None and _rs_req < 0:
        # 〔实测 2026-11-11〕旧写法 `_int(retstart, ..., 0)` 把负数夹成 0＝第一页，输出只印
        # retstart=0，调用方以为拿到了 -5 起始的窗口。与上面 retmax 负数同口径：直接报。
        raise NcbiError(f"retstart={retstart!r} 是负数，本工具不替它编一个意思（旧版会静默夹成 0＝第一页）。"
                        f"要第一页就写 0；翻页请给非负整数，上限 {RETSTART_MAX}。")
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
            filled.append(f"mindate 由本工具补成 {NO_BEGIN}＝不限下界（旧版补 1809-01-01 是错的："
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
    d["_request_params"] = {k: v for k, v in q.items() if k not in ("api_key", "email")}
    # 〔实测 2026-11-11〕留给 _zero_explanation 判断「用户是不是只要计数」；**不能**改读 NCBI
    # 回显的 retmax（越页时回显会变成 "0"）。完整实测与原因只写在 _zero_explanation 那一支里，
    # 避免同一段证据两处各写一遍。这个字段只给解释用，不改下发参数。
    d["_retmax"] = _rm
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
            f"所以这里直接拦掉。可用值："
            + ", ".join(sorted(set(FIELD_WHITELIST.values()) - FIELD_SWALLOWED - FIELD_NO_COUNT)))
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


_FW_TAG_RE = re.compile(
    r"^\s*(?:tiab|title/abstract|ti|title|ab|mh|mesh(?:\s+terms)?|tw|au|author|"
    r"ta|jour|journal|dp|pdat|edat|pt|ptyp|la|lang|ad|affiliation|uid|pmid|pmc|doi|ot|sh)\s*$",
    re.I,
)


def _syntax_findings(term):
    """独立于 esearch 响应的原始检索式语法检查。返回 [(code, breaking, warning_text), ...]。
    breaking=True 表示极可能导致概念丢失、粘连或分组错乱（供 check_recall 判失真）。
    本项目自订检查口径：不以弯撇号判断引号配对；不单凭 /de、/ad、/su、/ab 判外库语法。
    旧「实测」标记已作废：此前依据来自子代理、未经主会话核验（2026-11-12）。
    """
    term = str(term or "")
    outside = re.sub(r'"[^"\n]*"|\[[^\]]*\]', lambda m: " " * len(m[0]), term)
    ascii_masked = re.sub(r'"[^"\n]*"', lambda m: " " * len(m[0]), term)
    masked = re.sub(r'"[^"\n]*"|“[^“”\n]*”|\[[^\]]*\]', lambda m: " " * len(m[0]), term)
    nodoi = re.sub(r"\b10\.\d{4,9}/\S+|\bhttps?://\S+", lambda m: " " * len(m[0]), masked)
    out = []
    if term.count('"') % 2:
        out.append(("dq_unclosed", True, "⚠️ 双引号未闭合，NCBI 可能忽略引号；请配对后重查。"))
    depth = 0
    bad_parentheses = False
    for c in outside:
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            bad_parentheses |= depth < 0
    if depth or bad_parentheses:
        out.append(("paren", True, "⚠️ 括号不配对，NCBI 可能改变组合顺序；请配对后重查。"))
    # 用原始 term 而非 mask 后的 outside：outside 把引号短语抹成空格，会让
    # `("root resorption"[tiab] OR …)` 变成 `(   OR …)` 而误报「以布尔算子开头」〔实测：正畸演练 export〕。
    # 原始 term 里短语以 " 起头，挡住匹配；真正以算子开头（如 `OR cancer`）仍会命中。
    if re.match(r"^\s*\(*\s*(AND|OR|NOT)\b", term, re.I):
        out.append(("bool_start", True, "⚠️ 查询以布尔算子开头，可能被忽略；请写完整的 A AND/OR/NOT B。"))
    if ascii_masked.count("“") != ascii_masked.count("”"):
        out.append(("fw_quote_unpaired", True,
                    '⚠️ 全角双引号（“”）未配对，NCBI 可能忽略未配对引号并按词拆分；请改用半角 " 并配对。'))
    elif "“" in ascii_masked or "”" in ascii_masked:
        out.append(("fw_quote_norm", False,
                    '⚠️ 含全角双引号（“”）：建议改用半角 "，以便引号配对与短语检查一致。'))
    round_groups = re.findall(r"（([^（）]+)）", masked)
    if any(_FW_TAG_RE.match(g) for g in round_groups):
        out.append(("fw_round_tag", True,
                    "⚠️ 含全角圆括号包裹字段名（如（tiab））：PubMed 不识别为字段限定，会把括号内文字当普通检索词；"
                    "请改用半角方括号 [tiab]。"))
    elif re.search(r"[（）]", masked):
        out.append(("fw_round", False,
                    "⚠️ 含全角圆括号（）：PubMed 不按分组语法解析全角括号；分组请用半角 ()，字段限定请用半角 []。"))
    lenticular_groups = re.findall(r"【([^【】]+)】", masked)
    if any(_FW_TAG_RE.match(g) for g in lenticular_groups) or re.search(r"[0-9A-Za-z_]【|】[0-9A-Za-z_]", masked):
        out.append(("fw_lenticular_fuse", True,
                    "⚠️ 含全角方头括号【】与检索词相连或包裹字段名：PubMed 不识别为字段标签且可能与相邻词粘连；"
                    "请改用半角 [ ]。"))
    elif re.search(r"[【】]", masked):
        out.append(("fw_lenticular", False, "⚠️ 含全角方头括号【】：PubMed 不识别为字段标签；请改用半角 [ ]。"))
    if re.search(r"[［］]", masked):
        out.append(("fw_square", False, "⚠️ 含全角方括号［］：建议改用半角 [ ]，避免字段标签解析差异。"))
    if re.search(r"(?<=[0-9A-Za-z_])[、。](?=[0-9A-Za-z_])", masked):
        out.append(("fw_fuse", True,
                    "⚠️ 含无空格分隔的全角顿号/句号（、。）：NCBI 会将两侧英文词粘连导致词项失效；"
                    "请改用空格配合大写 AND/OR。"))
    elif re.search(r"[、。]", masked):
        out.append(("fw_ideopunct", False, "⚠️ 含全角顿号/句号（、。）：请改用空格配合大写 AND/OR。"))
    if re.search(r"[，；：]", masked):
        out.append(("fw_and_punct", False,
                    "⚠️ 含全角逗号/分号/冒号（，；：）：NCBI 通常按 AND 交集处理；"
                    "若本意为同义词并列请显式写大写 OR，交集请写大写 AND。"))
    foreign = []
    if re.search(r"\b(?:TITLE(?:-ABS)?-KEY|LIMIT-TO)\s*\(", nodoi, re.I):
        foreign.append("Scopus TITLE-ABS-KEY/LIMIT-TO")
    if re.search(r"(?<![A-Za-z0-9_])(?:TS|TI|AB|AU|SO|KW|PY|LA)\s*=", nodoi):
        foreign.append("WoS 字段等号（如 TS=）")
    if (re.search(r"(?<=[A-Za-z])/(?:exp|ti|tw|kw|au|mh)\b", nodoi, re.I)
            or re.search(r"(?<=[A-Za-z0-9])\s*:\s*(?:ti,ab|ab,ti|ti|ab|kw|de|au|ad|tw|mh)\b", nodoi, re.I)
            or re.search(r"(?<![A-Za-z])exp\s+[A-Za-z][\w\- ]*/", nodoi, re.I)):
        foreign.append("Embase /exp 或 :ti,ab 等标签")
    if foreign:
        out.append(("foreign_db", True,
                    "⚠️ 检测到疑似外库检索语法（" + "、".join(foreign) + "）：PubMed 不支持该语法，"
                    "会将其当作普通检索词；请改写为 [MeSH Terms] / [tiab] 等 PubMed 标签。"))
    for seg in re.split(r"\b(?:AND|OR|NOT)\b|[()]", nodoi, flags=re.I):
        toks = seg.split()
        if (len(toks) >= 2 and toks[-1].endswith("*") and not toks[-1].startswith("*")
                and len(toks[-1]) >= 2 and toks[-1][-2].isalnum() and not toks[-2].endswith("*")):
            pair = " ".join(toks[-2:])
            out.append(("wildcard_multi", False,
                        f'⚠️ 未加引号的多词片段「{pair}」末尾带截词符 *：* 只作用于最后一个词且会关闭整段短语的 '
                        f'MeSH 自动映射；若需短语截词请加半角双引号（如 "{pair}"）。'))
            break
    return out


def _query_warnings(d):
    """只提示，不偷偷修写检索式；原始响应样例见审计回归测试。"""
    term = str(d.get("_term") or "")
    qt = str(d.get("querytranslation") or "")
    outside = re.sub(r'"[^"\n]*"|\[[^\]]*\]', lambda m: " " * len(m[0]), term)
    warnings = [text for _, _, text in _syntax_findings(term)]
    if re.search(r"\band\b", outside) and re.search(r'"and\s', qt, re.I):
        warnings.append("⚠️ 小写 and 被实际执行式当作词的一部分；请改为大写 AND 后重查。")
    missing_cjk = sorted(set(re.findall(r"[㐀-鿿぀-ヿ]+", term)) -
                         set(re.findall(r"[㐀-鿿぀-ヿ]+", qt)))
    if qt and missing_cjk:
        warnings.append("⚠️ 实际执行式未保留这些中文/日文片段：" + ", ".join(missing_cjk) +
                        "；命中数不能代表完整原查询，请用对应英文词重查。")
    return warnings


def _required_window_group(tokens, index):
    """解析一个括号组；显式传入 tokens，避免递归闭包保留整个分词列表。"""
    required, op, seen = set(), "AND", False
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == ")":
            return required, index
        if token in ("AND", "OR", "NOT"):
            op = token
            continue
        if token == "(":
            dates, index = _required_window_group(tokens, index)
        else:
            dates = set(_windows(token))
        if not seen:
            required = dates if op != "NOT" else set()
        elif op == "AND":
            required |= dates
        elif op == "OR":
            required &= dates
        seen, op = True, "AND"
    return required, index


def _required_windows(d):
    """提取布尔式每个分支都要求的日期。OR 取交集，AND 取并集，NOT 只保留左边。

    不同日期值可以属于同一记录，因此这里只供提示，不据此提前返回空结果。
    PubMed 布尔组合按从左到右处理；括号单独递归，短语和字段名里的算子不拆。
    """
    qt = str(d.get("querytranslation") or "")
    tokens, start = [], 0
    pattern = r'"[^"\n]*"|\[[^\]]*\]|\b(?:AND|OR|NOT)\b|[()]'
    for match in re.finditer(pattern, qt):
        value = match[0]
        if value.startswith(('"', '[')):
            continue
        fragment = qt[start:match.start()]
        if fragment.strip():
            tokens.append(fragment)
        tokens.append(value)
        start = match.end()
    fragment = qt[start:]
    if fragment.strip():
        tokens.append(fragment)

    try:
        required, _ = _required_window_group(tokens, 0)
    except RecursionError:
        return []    # 极深表达式不作日期因果解释，仍返回实际执行式
    return [window for window in _windows(qt) if window in required]


def _note(d, *, include_translation=True):
    """生成解析提示；JSON 已含完整执行式时可省去其文本行，警告仍保留。"""
    lines = []
    term = str(d.get("_term") or "")
    qt = (d.get("querytranslation") or "").strip()
    if not qt:
        # 实测：中文查询（"牙根吸收"、"种植钉"）会让 NCBI 返回空的 querytranslation + 0 条。
        # 已作废（2026-11-11）：空执行式证明没有任何条件、以及「PubMed 只索引英文」。
        # 作废理由：空字段只说明未返回执行式；既有样例不足以证明索引语言的绝对结论。
        hint = ""
        if _CJK.search(term):
            hint = "（查询含中文，建议先换成对应英文词重查，如 牙根吸收 → tooth resorption）"
        lines.append(f"⚠️ NCBI 未返回实际执行式（querytranslation 为空）{hint}"
                     f"—— 这个结果【不能】用来说明主题有无文献")
    elif include_translation and qt.lower() != term.strip().lower():
        # 只印 querytranslation，不再重复印 translationset —— 实测后者是前者的逐概念分解，
        # 两条都印等于同一份信息烧两遍 token（CBCT 那种短语能扩出 200+ 字符）。
        # as_json=True 时 translationset 会作为结构化字段单独给，脚本要用再从那里取。
        if len(qt) > 600:
            lines.append(f"NCBI 实际执行：{qt[:600]}…（截断；完整解析用 as_json=True 取）")
        else:
            lines.append(f"NCBI 实际执行：{qt}")
    lines.extend(_query_warnings(d))
    e = d.get("errorlist") or {}
    nf = []
    if isinstance(e, dict):
        nf = [str(x) for x in _as_list(e.get("phrasesnotfound") or [])]
        if nf:
            # 已作废 2026-11-11：「phrasesnotfound 就是已丢弃」。在线 AND 反例仍为零命中。
            lines.append("⚠️ NCBI 未找到这些检索片段（phrasesnotfound）：" + ", ".join(nf) +
                         "；不能据此断言它们被丢弃，也不能把计数解释成剩余词的结果。")
        ff = _as_list(e.get("fieldsnotfound") or [])
        if ff:
            lines.append("⚠️ NCBI 未识别这些字段，请核对实际执行式：" + ", ".join(map(str, ff)))
    ig, qn, others = _warn_bits(d)
    if ig:
        lines.append("⚠️ NCBI 把这些词当【停用词】忽略：" + ", ".join(ig))
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
                         f" —— 已改写（可能按词 AND），这 {_c} 条不是原短语的命中数。"
                         "请核对实际执行式；可去掉引号按词查，或使用经核实的 MeSH 词。")
        else:
            lines.append(head +
                         " —— 这条 0 只关于那段字面短语本身，关于它想表达的主题有没有文献"
                         "什么都没说；去掉引号按词查、或换 MeSH 词再问一次。")
    if others:
        lines.append("⚠️ NCBI 解析警告：" + "；".join(others) +
                     "；检索式可能被忽略或改写，请核对实际执行式。")
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
    for (a1, b1, l1), (a2, b2, l2) in _same_label_pairs(_required_windows(d)):
        if a1 > b2 or a2 > b1:
            # 〔实测 2026-11-11〕不相交的两段仍可能返回 >0：一条记录可以同时带有落进两段的
            # 日期。判据必须写成「分段各命中」，不能只看 pubdate：把 PMID 32672908 单独限定进
            # 任一段（"tooth" + 2020 全年 / reldate=365）各命中 1 条；而 esummary 里
            # pubdate=1993、sortpubdate=2026/08/13 00:00 都不在这两段的窗口里 —— 只读 pubdate
            # 会以为「1993 两段都不落」，把这条对的说明判成编的。具体哪个日期值落进哪一段
            # 仍未核实（本工具拿不到那个字段），能复核的是「分段各命中」这组数。
            # 所以这里只说「不相交」这个事实、不替 0 条下结论（那是 _zero_explanation 的活），
            # 也不按 count 分两支 —— 旧写法无条件印「也会是 0 条」，与同一屏的命中数自相矛盾。
            lines.append(f"⚠️ NCBI 实际执行的两段【同字段（{l1}）】日期不相交"
                         f"（{a1}→{b1} 与 {a2}→{b2}）。不相交不等于 0 条 —— 一条记录可以"
                         f"同时带有落进两段的日期；有命中时别把这个命中数当成日期条件生效的证据。")
    gap = _phrase_gap(d)
    if gap:
        lines.append("⚠️ 引号短语里有词在 NCBI 的实际执行中找不到：" + "；".join(gap) +
                     "；不能将替代式的计数当成原短语计数，也不能据此断言原短语为 0 条。")
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
        # 未找到片段并不证明 field 生效；独立检查执行式里的字段标签。
        if ("[" + fa + "]") in qt:
            # 〔实测 2026-11-09〕判据顺序必须是「先看 qt 有没有，再说别的」：
            #   term='tooth resorption AND 2020:2026[dp]' 配 field=Title 时 qt 是
            #   `"tooth"[Title] AND "resorption"[Title] AND 2020/01/01:2026/12/31[Date - Publication]`
            #   —— field 明明生效，旧写法却因为「term 里有方括号」直接宣布 field 被 NCBI 丢掉，
            #   还教人「请直接改 term 里的标签」，那条建议在这个情况下会把对的查询改坏。
            extra = f"\n   判据：「NCBI 实际执行」里出现了 [{fa}]，这个限定生效了。"
        elif re.search(r"\[[A-Za-z][^\]]{1,28}\]", str(d.get("_term") or "")):
            extra = ("\n   ⚠️ qt 里没有出现这个 field 的标签，而 term 自己写了字段标签 —— "
                     "实测这种情况下 term 内的标签赢、field 被丢"
                     "（'miniscrew[Title]' 配 field=\"Journal\" 回的仍是 [Title] 的结果，与 field 无关）。"
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
    """返回 0 条记录时该说哪一句（count 可能 >0：retmax=0 只要计数 / retstart 越页）。
    不带「=>」前缀，由调用方拼。
    〔实测 2026-11-09〕这一支里补/删了两件事：
      - 新加「两段日期窗口不相交」：reldate 现在原样下发、由 NCBI 做 AND，本工具不再用本机日历
        提前预判空交集（那个预判本身就是 bug 的来源），所以空交集必须在这里判，
        否则「确实没有匹配这篇」那句假话又回来了；
      - 删掉「撞上 9999 窗口上限」那一支：越界的 retstart 现在由 _esearch 提前拦下并报错，
        那一支永远走不到（旧注释还把它当实测事实写着）。
    """
    if count and d.get("_retmax") == 0:
        # 〔实测 2026-11-11〕这里读的是【本工具发出去的】retmax，不是 NCBI 回显的 retmax：
        # 越页时 NCBI 会把回显改成 "retmax":"0"（retmax=5&retstart=9 → count=8、idlist 空），
        # 拿回显判会把「翻页越界」误报成「只要计数」。retmax=0 是「只要计数」的合法用法
        # （--probe 也这么验），旧写法落到最后一个分支，把「你没要记录」说成
        # 「PubMed 确实没有匹配这篇」，而同屏还印着命中数 N。这一支必须排在日期/短语判据之前，
        # 否则锅会被甩给别的条件。
        return (f"本次 0 条是因为 retmax=0：你只要计数、没要记录。"
                f"命中数是真实的 {count} 条；要记录请传 retmax>0，只要数字可以用 count()。")
    if count and int(d.get("retstart") or 0) >= count:
        return (f"本次 0 条只是因为 retstart={d.get('retstart')} 越过了命中总数 {count} 条，"
                "不是「没有匹配」——往回翻页即可。")
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
                    "（见上 ⚠️）；是否还有日期等条件请看实际执行式。"
                    "换字段没用——要换成语义实词（如 miniscrew、root resorption）再问一次。")
        return ("0 条，但这【不能】当成「主题没有文献」：查询词全部被报告为未找到或被忽略（见上 ⚠️）。"
                "这不等于所有执行条件为空；核对拼写、字段和日期后再查。")
    for (a1, b1, l1), (a2, b2, l2) in _same_label_pairs(_required_windows(d)):
        if a1 > b2 or a2 > b1:
            return (f"0 条的一个可能原因是【两段同字段（{l1}）的日期窗口不相交】：NCBI 执行的是 "
                    f"{a1}→{b1} 与 {a2}→{b2}。"
                    f"这不能读成主题没文献 —— 放宽 reldate，或去掉 mindate/maxdate 再问一次。")
    if not (d.get("querytranslation") or "").strip():
        return ("0 条，但这条 0 不能作为「主题没有文献」的依据："
                "NCBI 未返回实际执行式，无法据此确认检索条件（见上）。")
    if _warn_bits(d)[1]:
        return ("0 条的原因是 NCBI 说【整个引号短语在索引里不存在】"
                f"（warninglist.quotedphrasesnotfound＝{_warn_bits(d)[1]}）。"
                "这句只关于那段字面短语，关于它想表达的主题有没有文献【什么都没说】——"
                "去掉引号按词查、或换成 MeSH 词再问一次。")
    if _phrase_gap(d):
        return ("0 条，但先看清楚：上面 ⚠️ 说引号短语里有词没进实际执行，"
                "所以这个 0 是「剩下那几个词的组合没有匹配」，不是你写的那个短语的结论。")
    if (_query_warnings(d) or _warn_bits(d)[2] or
            any((d.get("errorlist") or {}).get(k) for k in ("fieldsnotfound", "phrasesnotfound"))):
        return "0 条，但查询解析有警告；修正检索式后再查，不能据此判断主题有无文献。"
    return "0 条：本次实际执行的检索式未匹配记录；这不证明该主题或某篇文献不存在。"


def _all_dropped(d):
    """历史函数名保留；现只判断查询词是否全部被报告未找到/忽略。
    【已作废 2026-11-11】以下旧解释把未找到等同于丢弃、把文本词问题等同于条件为空：
    整条查询里的词是不是全被 NCBI 丢掉了。丢掉后 querytranslation 只是原词回显，
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
    # 〔实测 2026-11-11〕旧写法把 [Title]/[dp] 这类字段标签文字也当成检索词，且兜底用子串匹配
    # querytranslation：search('zzzqqx[Title]') 的 toks 是 {'zzzqqx','title'}，而回显逐字含
    # 'title'，于是「全丢」判不出来，同一屏先说「zzzqqx 已丢弃」、最后又说「确实没有匹配这篇」。
    # 先把方括号标签整段去掉（标签不是检索词），再分词。
    term_wo_tags = re.sub(r"\[[^\]]{1,30}\]", " ", str(d.get("_term") or "")).lower()
    toks = {w for w in re.split(r"[^0-9A-Za-z一-鿿]+", term_wo_tags)
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
    # 〔实测 2026-11-11〕旧守卫与实际切分用的不是同一套分隔符：'1，234，567'（全角逗号）与
    #   '1,234,567,'（行尾逗号）都能绕过，直接被切成 1/234/567 三个【真实存在】的 PMID。
    #   现在整串（去掉首尾分隔符）与逐个 token 都按同一套判据看一遍，全角逗号也按千位分隔符处理。
    whole = s.strip().strip(";，、, \t\r\n")
    amb = [whole] if re.fullmatch(r"\d{1,3}(?:[,，]\d{3})+", whole) else []
    amb += [t for t in re.split(r"[;，、\s]+", s) if t and re.fullmatch(r"\d{1,3}(?:,\d{3})+", t)]
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
            digits = m.group(1)
            try:
                ids.append(str(int(digits)))
            except ValueError:
                # 〔实测 2026-11-11，Python 3.13〕4301 位纯数字在 int() 处抛裸 ValueError
                # （3.11+ 的 4300 位上限），不是 NcbiError；走 mcp 2.x 时原文会被剥成一句
                # 「Error executing tool ...」。这里换成能读懂的报错。
                raise NcbiError(f"这一串有 {len(digits)} 位数字，超出 Python 的整数解析上限（4300 位）；"
                                f"PMID 不会有这么长，多半是粘错了内容，请核对后再试。")
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


async def _esummary_dicts(ids, *, include_unavailable=False):
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
            res = d.get("result")
            if not isinstance(res, dict) or not isinstance(res.get("uids"), list):
                raise NcbiError("esummary 缺少合法的 result/uids，无法判断记录是否存在；请重试")
            uids = res["uids"]
            if any(not isinstance(u, str) or u not in c for u in uids) or len(set(uids)) != len(uids):
                raise NcbiError("esummary 返回的 PMID 列表与请求不符，不能作为有效元数据")
            records = []
            for uid in uids:
                rec = res.get(uid)
                if rec is None:
                    continue    # 单条缺失保持未知，不凭空造记录
                if not isinstance(rec, dict):
                    raise NcbiError(f"esummary 的 PMID {uid} 元数据结构异常")
                if "uid" in rec and rec["uid"] != uid:
                    raise NcbiError(f"esummary 的 PMID {uid} 与记录内部 uid 不一致，不能作为有效元数据")
                if rec.get("error") and rec["error"] != "cannot get document summary":
                    raise NcbiError(_mask(f"esummary 的 PMID {uid} 请求失败：{rec['error']}"))
                if include_unavailable or (not rec.get("error") and rec.get("title")):
                    records.append((uid, rec))
            out[i] = records
        except NcbiError as e:
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
    # authors 只放名字，不粘「，共 N 人」：as_json 的卖点是「给脚本用」，粘在最后一个作者名后面
    # 会让脚本切出假名字、数逗号数错人数。人数由 author_count 单独给；文本输出的人话后缀在
    # _fmt_record 里加（2026-11-11 改，旧版的 authors 字段两种输出共用同一段拼串）。
    ptypes = [p for p in _as_list(r.get("pubtype") or []) if isinstance(p, str)]
    ptag = ""
    specific = [p for p in ptypes if p != "Journal Article"]
    if specific:
        ptag = "｜" + "/".join(specific)    # 不截掉撤稿、勘误或试验类型
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


def _brief_record(uid, r):
    """初筛字段；文献类型完整保留，省略字段不代表来源缺失。"""
    p = _record_parts(uid, r)
    return {k: p[k] for k in ("pmid", "pubdate", "title", "pubtype")}


def _fmt_brief_record(r, uid):
    p = _brief_record(uid, r)
    types = "/".join(p["pubtype"])
    return f"PMID {p['pmid']}｜{p['pubdate']}" + (f"｜{types}" if types else "") + f"\n  {p['title']}"


def _fmt_record(r, uid):
    p = _record_parts(uid, r)
    head = f"PMID {p['pmid']}｜{p['pubdate']}｜{p['journal']}{(' ' + p['citation']) if p['citation'] else ''}{p['ptag']}"
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


async def _efetch_abstracts(pmids):
    timeout = min(60, 20 + 0.4 * len(pmids))
    text = await _get_async("efetch.fcgi", {"db": "pubmed", "id": ",".join(pmids),
                                            "rettype": "abstract", "retmode": "text"}, timeout)
    # 〔实测 2026-11-11〕efetch 是全文唯一的文本端点，没有 _safe_json 那道 200+HTML 守卫：
    # 把 _get_async 换成返回 '<!DOCTYPE html><html>…' 后，旧版把整段 HTML 当摘要拼进正文并报
    # 「实取 0 条」——那是故障页，不是「这些 PMID 没有摘要」。README 行为契约第 1 条要求它算失败。
    head = text.lstrip()[:200].lower()
    if head.startswith("<!doctype") or head.startswith("<html"):
        raise NcbiError("efetch 回的是 HTML 页面（HTTP 200 也可能是故障页/网关页），不是摘要正文 —— "
                        "这【不是】「这些 PMID 没有摘要」。原文开头：" + _mask(text.lstrip()[:200]))
    got = re.findall(r"^PMID: (\d+)", text, re.M)
    if len(got) != len(set(got)) or not set(got).issubset(pmids):
        raise NcbiError("efetch 返回的 PMID 与请求不符或重复，不能作为有效摘要")
    # 无效号的空占位形状已在线核对；其它无 PMID 正文不能解释为文献不存在。
    if not got and not re.fullmatch(r"\s*(?:\d+\.\s*)+", text):
        raise NcbiError("efetch 返回了无法识别的正文，无法判断记录是否存在；原文开头：" +
                        _mask(text.lstrip()[:200]))
    return text


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


@mcp.tool()
async def search(term: str, retmax: StrictInt | StrictStr = 20, sort: str = "", field: str = "",
                 mindate: str = "", maxdate: str = "", reldate: StrictInt | StrictStr = 0,
                 retstart: StrictInt | StrictStr = 0,
                 as_json: bool | str = False,
                 detail: Literal["full", "brief", "ids"] = "full") -> str:
    """检索 PubMed，返回命中数及 PMID、日期、期刊、标题、作者、DOI、PMC、文献类型。

    term 使用英文 PubMed 语法：resorption[Title]、"root resorption"[tiab]、AND/OR/NOT、截词 *。
    布尔算子写大写，引号只包短语，括号须配对。MeSH 请核对实际执行式：写错的带引号词会报
    quotedphrasesnotfound，不带引号可能改写成别的概念；有命中不证明原短语存在。
    sort: pub_date / relevance / author；不能按期刊质量排序。
    field 可用 Title、Title/Abstract、Journal、Author、Affiliation、Language、Grant、PMID 等；
    未支持值会报错。MeSH/文献类型请写进 term 的 [MeSH Terms]/[Publication Type] 标签。
    日期按发表日期；mindate/maxdate 接受 YYYY、YYYY-MM、YYYY-MM-DD 或斜杠写法。
    单端日期自动补另一端并提示。reldate=0 不限，正数为最近天数；和显式日期同时给时做 AND。
    retmax 默认 20，上限 1000；retstart 为零起始偏移，上限 9998，最多可翻前 9999 条。
    只要计数用 count；需要题录直接 search，无需先 count。as_json=True 返回完整解析和结构化记录。
    detail: full 默认完整题录；brief 仅 PMID/日期/标题/全部文献类型；ids 仅 PMID，不取元数据或检查撤稿类型。
    各档保留查询警告；brief 省略字段不表示缺失，ids 的 returned 计 PMID 数、esummary_missing 为 null。
    先读 ⚠️：改写后的命中数不等于原查询命中数；0 条不证明主题无文献。PMC 号不保证立即可下载。
    """
    # 〔实测 2026-11-10〕这里【不能】给 _int 传 lo：传了就把负数当场夹平，
    #    _esearch 里那句「retmax 是负数，本工具不替它编一个意思」根本轮不到（-3 变 0、静默出结果）。
    #   上限同理留给 _esearch，那里既夹也说明。
    if detail not in ("full", "brief", "ids"):
        raise NcbiError("detail 只支持 full / brief / ids")
    json_mode = _as_bool(as_json, "as_json")    # 非法模式在联网前报错
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
    mode_note = {
        "full": "",
        "brief": "说明：brief 省略作者、期刊、卷页、DOI/PMC；省略不表示来源缺失，全部文献类型保留。",
        "ids": "⚠️ ids 仅返回 esearch 的 PMID，未获取元数据、未检查撤稿等文献类型；返回数按 PMID 计。",
    }[detail]
    audit_params = {
        "input_params": {"field": field, "sort": sort, "mindate": mindate, "maxdate": maxdate,
                         "reldate": reldate, "retmax": retmax, "retstart": retstart, "as_json": as_json},
        "request_params": d.get("_request_params") or {},
        "field": d.get("_field_applied") or field or "",
        "sort": sort or "",
        "mindate": mindate or "",
        "maxdate": maxdate or "",
        "reldate": d.get("_rel") if "_rel" in d else _int(reldate, "reldate", 0),
        "retmax": d.get("_retmax") if "_retmax" in d else _int(retmax, "retmax", 20),
    }
    if json_mode:
        try:
            pairs = await _esummary_dicts(ids) if (ids and detail != "ids") else []
        except NcbiError as e:
            _audit("search", term=term, count=count, returned=0, detail=detail,
                   retstart=int(d.get("retstart") or 0), **audit_params,
                   querytranslation=(d.get("querytranslation") or "").strip(),
                   warnings=_note(d, include_translation=False) + [f"⚠️ esummary 元数据获取失败：{type(e).__name__}"],
                   error="esummary_failed")
            raise
        # 〔实测 2026-11-09〕旧版的 as_json 分支把「窗口到顶」⚠️ 和 0 条解释整个丢掉了，
        # 而 docstring 又让脚本改用 as_json —— 走 JSON 的人恰恰拿不到「这批没取全」这句最需要的话。
        # 现在两支都进 JSON（warnings 里也放一份，脚本只看 warnings 也不会漏）。
        # 完整 querytranslation 已是独立字段，不在 warnings 再复制同一长串。
        warn = _note(d, include_translation=False)
        warn += [win_warn] if win_warn else []
        warn += [mode_note] if mode_note else []
        if detail != "ids" and len(pairs) != len(ids):
            # 〔实测 2026-11-11〕文本路径有这条 ⚠️，as_json 旧版却没有：脚本只看 warnings 会
            # 以为 2 个 id 就是 2 条记录。
            warn.append(f"⚠️ esearch 给了 {len(ids)} 个 id，esummary 只回了 {len(pairs)} 个 —— "
                        f"少的那几条 NCBI 没给元数据，别按 {len(ids)} 条算。")
        _audit("search", term=term, count=count,
               returned=len(ids) if detail == "ids" else len(pairs), detail=detail,
               retstart=int(d.get("retstart") or 0), **audit_params,
               querytranslation=(d.get("querytranslation") or "").strip(), warnings=warn)
        return json.dumps({
            "term": term, "querytranslation": (d.get("querytranslation") or "").strip(),
            "translationset": d.get("translationset") or [],
            "field": d.get("_field_applied", ""),
            "count": count, "retstart": int(d.get("retstart") or 0),
            "returned": len(ids) if detail == "ids" else len(pairs),
            "esummary_missing": None if detail == "ids" else max(0, len(ids) - len(pairs)),
            **({"detail": detail, "metadata_requested": detail != "ids"} if detail != "full" else {}),
            "phrases_not_found": _as_list((d.get("errorlist") or {}).get("phrasesnotfound") or []),
            # 兼容旧调用方保留别名；原「已丢弃」解释已作废，见 deprecated_fields。
            "dropped_phrases": _as_list((d.get("errorlist") or {}).get("phrasesnotfound") or []),
            "deprecated_fields": {"dropped_phrases": "弃用别名，请读 phrases_not_found；不表示这些词已被丢弃"},
            "daterange": d.get("_daterange") or "",
            "executed_windows": d.get("_windows") or [],
            "window_warning": win_warn,
            "zero_note": ("=> " + _zero_explanation(d, count)) if not ids else "",
            "records": ([{"pmid": uid} for uid in ids] if detail == "ids" else
                        [(_brief_record if detail == "brief" else _record_parts)(uid, r) for uid, r in pairs]),
            "warnings": warn,
        }, ensure_ascii=False)
    head = [f"查询：{term}", f"命中 {count} 条，本次给 {len(ids)} 条（retstart={d.get('retstart')}）"]
    notes = _note(d)
    head += notes
    if win_warn:
        head.append(win_warn)
    if mode_note:
        head.append(mode_note)
    base_warn = notes + ([win_warn] if win_warn else []) + ([mode_note] if mode_note else [])
    if not ids:
        _audit("search", term=term, count=count, returned=0, detail=detail,
               retstart=int(d.get("retstart") or 0), **audit_params,
               querytranslation=(d.get("querytranslation") or "").strip(), warnings=base_warn)
        head.append("=> " + _zero_explanation(d, count))
        return "\n".join(head)
    if detail == "ids":
        _audit("search", term=term, count=count, returned=len(ids), detail=detail,
               retstart=int(d.get("retstart") or 0), **audit_params,
               querytranslation=(d.get("querytranslation") or "").strip(), warnings=base_warn)
        head[1] = f"命中 {count} 条，本次给 {len(ids)} 个 PMID（retstart={d.get('retstart')}）"
        return "\n".join(head) + "\n\n" + "\n".join(ids)
    try:
        pairs = await _esummary_dicts(ids)
    except NcbiError as e:
        _audit("search", term=term, count=count, returned=0, detail=detail,
               retstart=int(d.get("retstart") or 0), **audit_params,
               querytranslation=(d.get("querytranslation") or "").strip(),
               warnings=base_warn + [f"⚠️ esummary 元数据获取失败：{type(e).__name__}"],
               error="esummary_failed")
        raise
    if len(pairs) != len(ids):
        miss_msg = (f"⚠️ esearch 给了 {len(ids)} 个 id，esummary 只回了 {len(pairs)} 个 —— "
                    f"少的那几条 NCBI 没给元数据，别按 {len(ids)} 条算。")
        head.append(miss_msg)
        base_warn.append(miss_msg)
    _audit("search", term=term, count=count, returned=len(pairs), detail=detail,
           retstart=int(d.get("retstart") or 0), **audit_params,
           querytranslation=(d.get("querytranslation") or "").strip(), warnings=base_warn)
    head[1] = f"命中 {count} 条，本次给 {len(pairs)} 条（retstart={d.get('retstart')}）"
    head.append("")
    formatter = _fmt_brief_record if detail == "brief" else _fmt_record
    return "\n".join(head) + "\n" + "\n".join(formatter(r, uid) for uid, r in pairs)


@mcp.tool()
async def count(term: str, field: str = "", mindate: str = "", maxdate: str = "",
                reldate: StrictInt | StrictStr = 0) -> str:
    """只取 PubMed 命中数，不取题录；需要题录直接用 search，避免重复检索。

    term、field、日期参数规则同 search：英文词，大写 AND/OR/NOT，日期按发表日期。
    返回实际执行式及 ⚠️；丢词/短语改写后的计数不能当原查询计数。0 条不证明主题无文献，
    有命中也不证明原短语存在；确认短语须读取题录或摘要。完整解析可用 search(retmax=0, as_json=True)。
    """
    d = await _esearch(term, retmax=0, field=field, mindate=mindate, maxdate=maxdate, reldate=reldate)
    n = _count_of(d)
    notes = _note(d)
    _audit("count", term=term, count=n,
           input_params={"field": field, "mindate": mindate, "maxdate": maxdate, "reldate": reldate},
           request_params=d.get("_request_params") or {},
           field=d.get("_field_applied") or field or "",
           mindate=mindate or "", maxdate=maxdate or "",
           reldate=d.get("_rel") if "_rel" in d else _int(reldate, "reldate", 0),
           querytranslation=(d.get("querytranslation") or "").strip(), warnings=notes)
    return "\n".join([f"查询：{term}", f"命中 {n} 条"] + notes)


@mcp.tool()
async def fetch_abstracts(pmids: str) -> str:
    """按逗号/空格分隔的 PMID 取 NCBI 原排版摘要；保留结构化段落、作者信息和声明。

    自动去重、归一补零号、分批。建议先筛选题录，再只取需要的摘要以节省输出。
    返回请求数、实际记录数和缺失号；有记录不等于有摘要，社论可能只有引文。
    网络失败报错，不冒充无记录。文献数值引用仍须核对原文位置。
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
    _audit("fetch_abstracts", pmids=uniq, requested=len(uniq), returned=len(got), missing=missing)
    return "\n".join(head) + "\n\n" + text


DB_WHITELIST = {"pmc", "nucleotide", "gene", "pubmed", "citedin", "refs"}


def _check_db(to):
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
    if s not in DB_WHITELIST:
        raise NcbiError(f"to={to!r} 不是本工具支持的库。可用：{', '.join(sorted(DB_WHITELIST))}"
                        f"（库名大小写不敏感，'PMC' 与 'pmc' 算同一个库；不认 'PMCID' 这种非库名写法）")
    return s


async def _pmids_exist(pids):
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
    pairs = await _esummary_dicts(list(states), include_unavailable=True)
    for uid, rec in pairs:
        if rec.get("error") == "cannot get document summary":
            states[uid] = "unavailable"
        elif not rec.get("error") and rec.get("title"):
            states[uid] = "present"
    return states


SNOW_SINGLE_CAP = 200        # 本项目自订：links(to=citedin/refs) 单 PMID 调用最多展示关联号数
SNOW_BATCH_CAP = 50          # 本项目自订：links(to=citedin/refs) 多 PMID 批量调用每号最多展示数，防 50×200 撑爆输出


@mcp.tool()
async def links(pmids: str, to: str = "pmc") -> str:
    """查 PMID 的数据库关联或引文追踪。pmids 用逗号/空格分隔，去重后最多 50 个。

    to: pmc（默认）/ gene / nucleotide / pubmed / citedin（施引）/ refs（参考文献），大小写不敏感。
    每行列直接关联号；方括号是其它关联计数。citedin/refs 供滚雪球追踪（0 条仅限 PubMed 已录入关系，不等于无引文）。
    to=pubmed 是相关文献，含自身；to=pmc 是 PMC 关联，不保证开放许可或此刻可下载。
    无关联、元数据不可用与存在性未知分开报告；esummary 缺失不证明 PMID 不存在。
    search 已给 PMC 时无需重复查询 links。内部逐 PMID 请求以保留归属，失败明确标注。
    """
    uniq, _ = _parse_pmids(pmids)
    db = _check_db(to)
    if len(uniq) > 50:
        raise NcbiError(f"一次最多 50 个 PMID（实测多 id 会被 NCBI 合并、归属丢失，且一个 id 一次请求），"
                        f"收到 {len(uniq)} 个，请分批")
    # 号存在性交给 esummary（理由见 _pmids_exist 的 docstring：elink 那个形状两种情况逐字相同）
    exist = await _pmids_exist(uniq)
    out = [None] * len(uniq)
    counts = [0] * len(uniq)
    errs = []
    snow = db in ("citedin", "refs")
    snow_link = "pubmed_pubmed_citedin" if db == "citedin" else ("pubmed_pubmed_refs" if db == "refs" else "")
    show_cap = (SNOW_SINGLE_CAP if len(uniq) == 1 else SNOW_BATCH_CAP) if snow else 8

    async def one(i, pid):
        params = {"dbfrom": "pubmed", "db": "pubmed" if snow else db, "id": pid, "retmode": "json"}
        if snow_link:
            params["linkname"] = snow_link
        try:
            d = _safe_json(await _get_async("elink.fcgi", params, 25), "elink")
        except NcbiError as e:
            # 单个号失败只废这一行。上一版让它出组：50 行一起没结果，且只剩
            # 「unhandled errors in a TaskGroup (1 sub-exception)」那句没有信息的话。
            errs.append(f"PMID {pid}：{str(e)[:120]}")
            out[i] = f"PMID {pid}  请求失败（原因见表头）"
            return
        sets = d.get("linksets") or []
        if snow and (not isinstance(sets, list) or not sets or any(not isinstance(s, dict) for s in sets)):
            errs.append(f"PMID {pid}：elink 缺少有效 linkset，不能判断引文关系")
            out[i] = f"PMID {pid}  请求失败（原因见表头）"
            return
        if snow:
            sets = [s for s in sets if {str(x) for x in _as_list(s.get("id") or s.get("ids") or [])} == {pid}]
            if not sets:
                errs.append(f"PMID {pid}：elink 未返回归属于请求号的 linkset")
                out[i] = f"PMID {pid}  请求失败（原因见表头）"
                return
        state = exist.get(pid, "unknown")
        if not sets:
            out[i] = (f"PMID {pid}  ⚠️ elink 没返回这个号的 linkset —— 请求没给回这个号，"
                      f"这【不是】「没有全文」，先核对 PMID")
            return
        mine, other = [], []
        # 〔实测 2026-11-11〕nucleotide 的直接链接 linkname 是 pubmed_nuccore /
        # pubmed_nuccore_refseq（dbto=nuccore），永远不以 "nucleotide" 结尾；旧判据把它们全塞进
        # other，输出「(nucleotide 库无直接对应记录)」又在方括号里报出同一条链接。直接链接按库名
        # 显式列出，不再用后缀猜。
        direct_names = {
            "pmc": ("pubmed_pmc",),
            "gene": ("pubmed_gene",),
            "pubmed": ("pubmed_pubmed",),
            "nucleotide": ("pubmed_nuccore", "pubmed_nuccore_refseq"),
            "citedin": ("pubmed_pubmed_citedin",),
            "refs": ("pubmed_pubmed_refs",),
        }.get(db, ())
        for ls in sets:
            srcs = {str(x) for x in _as_list(ls.get("id") or [])} | {str(x) for x in _as_list(ls.get("ids") or [])}
            if srcs and pid not in srcs:
                continue
            for lsd in (ls.get("linksetdbs") or []):      # 注意别把循环变量也叫 db —— 那会盖掉规范化的库名
                dests = [str(x) for x in _as_list(lsd.get("links") or lsd.get("neighbors") or [])]
                if not dests:
                    continue
                name = str(lsd.get("linkname") or db)
                if direct_names:
                    is_direct = name in direct_names
                else:
                    is_direct = not ("_" in name and not name.endswith(db))   # 兜底：pubmed_*_refs 等
                if not is_direct:
                    other.append(f"{name}:{len(dests)} 条")
                else:
                    counts[i] += len(dests)
                    more_hint = ""
                    if len(dests) > show_cap:
                        more_hint = f"…共 {len(dests)}" + (
                            f"（多号批量每号限展 {SNOW_BATCH_CAP}，单号调用可展 {SNOW_SINGLE_CAP}）"
                            if snow and len(uniq) > 1 else "")
                    mine.append(f"{name}=" + ",".join(
                        (x if db != "pmc" else f"PMC{x}") for x in dests[:show_cap]) + more_hint)
        tail = ("  [" + "; ".join(other) + "]") if other else ""
        if mine:
            out[i] = f"PMID {pid}  " + "  ".join(mine) + tail
            if state != "present":
                out[i] += "  ⚠️ esummary 未确认此号，以上关联来自 elink；请复核"
        elif state == "unavailable":
            out[i] = (f"PMID {pid}  ⚠️ esummary 未找到此号的文档摘要（cannot get document summary）；"
                      "请核对 PMID 或稍后重试，不能据此判断全文可用性") + tail
        elif state == "unknown":
            out[i] = f"PMID {pid}  ⚠️ 元数据缺失，存在性无法确认；请重试" + tail
        elif snow:
            zero_msg = ("(PubMed 未录入施引记录；0 条不等于无人引用)"
                        if db == "citedin" else "(PubMed 未录入参考文献；0 条不等于原文无引文)")
            out[i] = f"PMID {pid}  {zero_msg}" + tail
        else:
            out[i] = f"PMID {pid}  ({db} 库无直接对应记录)" + tail

    async with anyio.create_task_group() as tg:
        for i, pid in enumerate(uniq):
            tg.start_soon(one, i, pid)
    if errs and len(errs) == len(uniq):
        raise NcbiError(f"elink 全部 {len(errs)} 个请求都失败，第一个原因：" + errs[0])
    _audit("links", to=db, pmids=uniq, requested=len(uniq), direct_total=sum(counts), errors=len(errs),
           show_cap=show_cap)
    head = [f"elink pubmed→{db}，{len(uniq)} 个 PMID（每个一次请求，另加一次 esummary 判号是否存在）："]
    if errs:
        head.append(f"⚠️ {len(errs)} 个请求失败（那是故障，不是「没有链接」）：" + "; ".join(errs[:3]))
    # 图例必须跟着 to 变：to=pubmed 时那串「直接对应记录」是相关文献，跟全文没关系，
    # 而固定写「有全文/有数据」会把 104 条邻居读成「这篇有免费全文」。实测 37427694 to=pubmed
    # 走的就是 mine 分支（linkname="pubmed_pubmed" 以 db 结尾），第一个邻居还是这篇自己。
    legend = {"pmc": "直接对应记录＝PMC 关联，开放许可/当前可下载性未核实；无关联不代表其它站点无全文",
              "gene": "直接对应记录＝NCBI 把这个号关联到了 Gene 条目",
              "nucleotide": "直接对应记录＝NCBI 把这个号关联到了核酸序列条目",
              "pubmed": "直接对应记录＝NCBI 列出的【相关文献】（含这篇自己），与有没有全文无关；"
                        "要判断能不能下载请用 to=pmc",
              "citedin": "直接对应记录＝PubMed 已录入的【施引文献】PMID（滚雪球向前追踪）；未收录施引不等于无人引用",
              "refs": "直接对应记录＝PubMed 已录入的【参考文献】PMID（滚雪球向后追踪）；早期文献可能未录入引文关系"}[db]
    return ("\n".join(head) + "\n" + "\n".join(str(x) for x in out)
            + f"\n说明：{legend}；方括号为其它关联计数。")


# ---------- 系统综述助手（1.4.0 新增） ----------
# 设计依据与实测前提见 docs/systematic-review-plan.md（2026-11-11 探针 P1–P3）：
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


def _audit(event, **fields):
    """检索留痕（可选）。设 PUBMED_AUDIT_DIR 后，每次检索追加一行 JSONL，供写 PRISMA-S 检索报告。
    默认关闭＝零行为变化。留痕是辅助功能：写失败只报 stderr，不让检索本身失败〔本项目自订〕。"""
    d = os.environ.get("PUBMED_AUDIT_DIR", "").strip()
    if not d:
        return
    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "event": event,
           "version": _tool_version(), **fields}
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "searches.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"⚠️ 检索留痕写入失败（不影响本次结果）：{type(e).__name__}: {e}", file=sys.stderr)


async def _ids_in_window(term, *, mindate="", maxdate="", context=""):
    """在 9999 窗口内分页取全某查询的 PMID（每页 1000）。
    返回 (ids, count, first, drift)：ids 去重保序；count 为 NCBI 命中数；first 为首页 esearch
    响应（带 _term 等，供 _note 出警告）；drift 为分页期间数据库漂移提示（无则空串）。
    count 超窗抛 NcbiError —— 由调用方决定切片（export）还是缩小查询（diff）。"""
    first = await _esearch(term, retmax=1000, mindate=mindate, maxdate=maxdate)
    count = _count_of(first)
    if count > WINDOW:
        raise NcbiError(f"{context}命中 {count} 条，超过 PubMed esearch 的 {WINDOW} 条检索窗口"
                        f"（实测 retstart=9998 可取、9999 起被拒），无法在窗口内取全。"
                        f"请缩小查询，或改用 export（自动按发表日期切片取全）。")
    ids = list(first.get("idlist") or [])
    while len(ids) < count:
        page = await _esearch(term, retmax=1000, mindate=mindate, maxdate=maxdate, retstart=len(ids))
        got = page.get("idlist") or []
        if not got:
            raise NcbiError(f"{context}分页中断：已取 {len(ids)}/{count} 条时 NCBI 返回空页。"
                            f"数据库可能在检索期间更新，请重试。")
        ids.extend(got)
    drift = ""
    uniq = list(dict.fromkeys(ids))
    if len(uniq) != len(ids):
        drift = (f"⚠️ {context}分页出现重复 PMID（取 {len(ids)} 个、去重后 {len(uniq)} 个）——"
                 f"数据库可能在分页期间更新，结果以取回时点为准。")
    if len(uniq) != count:
        drift += (f"⚠️ {context}实际取回（去重后）{len(uniq)} 个 ≠ NCBI 命中数 {count}；"
                  f"数据库可能在检索期间更新，请复核。")
    return uniq, count, first, drift


def _term_vanished(qt):
    """实际执行式里除金标准 PMID（[UID]）与布尔算子/括号外是否还剩内容。
    剩不下＝term 被 NCBI 忽略（常见于中文词，翻译为空），此时 `(term) AND (uids)` 退化成只剩
    uids，found 会假阳性地全中。〔实测 A6：term=牙根吸收 → 执行式仅 uid OR 子句、found 3/3〕
    这是启发式判断〔本项目自订〕：只覆盖「执行式纯由 uid 构成」这一可观测症状。"""
    if not qt or not qt.strip():
        return False  # 缺少执行式不能证明条件被清空。
    s = re.sub(r'"?\d+"?\s*\[UID\]', " ", qt, flags=re.I)
    s = re.sub(r"\b(AND|OR|NOT)\b", " ", s, flags=re.I)
    s = re.sub(r"[()\s]+", " ", s)
    return not s.strip()


_DEGRADING_OUTMSG_RE = re.compile(
    r"^(?:AND|OR|NOT|[()])$|unmatched|ignored|not\s+found|unknown\s+field",
    re.I,
)


def _recall_warnings_and_degraded(raw_term, batch_ds):
    """汇总 check_recall 对原始 term 的语法警告与各批 esearch 响应警告，并判定是否存在部分词失效/失真。
    用原始 raw_term 做语法/中文/短语缺口检查，防止外层 `({term}) AND (uids)` 吞掉括号失衡告警〔实测 2026-11-12〕。"""
    first_d = batch_ds[0]
    qt0 = (first_d.get("querytranslation") or "").strip()
    lines = []
    if not qt0:
        hint = "（查询含中文，建议先换成对应英文词重查，如 牙根吸收 → tooth resorption）" if _CJK.search(raw_term) else ""
        lines.append(f"⚠️ NCBI 未返回实际执行式（querytranslation 为空）{hint}"
                     f"—— 这个结果【不能】用来说明主题有无文献")
    elif qt0.lower() != raw_term.strip().lower():
        if len(qt0) > 600:
            lines.append(f"NCBI 实际执行：{qt0[:600]}…（截断；完整解析用 as_json=True 取）")
        else:
            lines.append(f"NCBI 实际执行：{qt0}")

    seen = set(lines)

    def add(msg):
        if msg and msg not in seen:
            seen.add(msg)
            lines.append(msg)

    raw_findings = _syntax_findings(raw_term)
    for m in _query_warnings({"_term": raw_term, "querytranslation": qt0}):
        add(m)

    degraded = any(breaking or code == "fw_and_punct" for code, breaking, _ in raw_findings)
    missing_cjk = sorted(set(re.findall(r"[㐀-鿿぀-ヿ]+", str(raw_term or ""))) -
                         set(re.findall(r"[㐀-鿿぀-ヿ]+", qt0)))
    if qt0 and missing_cjk:
        degraded = True

    for d_b in batch_ds:
        batch_qt = str(d_b.get("querytranslation") or "")
        original = {**d_b, "_term": raw_term}
        for note in _note(original, include_translation=False):
            add(note)
        if not batch_qt or (_CJK.search(raw_term) and
                set(re.findall(r"[㐀-鿿぀-ヿ]+", raw_term)) - set(re.findall(r"[㐀-鿿぀-ヿ]+", batch_qt))):
            degraded = True
        e = d_b.get("errorlist")
        e = e if isinstance(e, dict) else {}
        nf = [str(x) for x in _as_list(e.get("phrasesnotfound") or []) if str(x).strip()]
        if nf:
            degraded = True
            add("⚠️ NCBI 未找到这些检索片段（phrasesnotfound）：" + ", ".join(nf) +
                "；不能据此断言它们被丢弃，也不能把计数解释成剩余词的结果。")
        ff = [str(x) for x in _as_list(e.get("fieldsnotfound") or []) if str(x).strip()]
        if ff:
            degraded = True
            add("⚠️ NCBI 未识别这些字段，请核对实际执行式：" + ", ".join(ff))
        ig, qn, others = _warn_bits(d_b)
        if ig:
            add("⚠️ NCBI 把这些词当【停用词】忽略：" + ", ".join(ig))
        if qn:
            degraded = True
            add("⚠️ NCBI 明说这些【整个引号短语】在索引里不存在（warninglist.quotedphrasesnotfound）：" +
                "，".join(qn) + " —— 可能已按词拆分改写，请核对实际执行式。")
        if others:
            if any(_DEGRADING_OUTMSG_RE.search(x) for x in others):
                degraded = True
            add("⚠️ NCBI 解析警告：" + "；".join(others) + "；检索式可能被忽略或改写，请核对实际执行式。")
        gap = _phrase_gap({"_term": raw_term, "querytranslation": d_b.get("querytranslation") or "",
                           "errorlist": e})
        if gap:
            degraded = True
            add("⚠️ 引号短语里有词在 NCBI 的实际执行中找不到：" + "；".join(gap) +
                "；不能将替代式的计数当成原短语计数，也不能据此断言原短语为 0 条。")
    return lines, degraded


@mcp.tool()
async def check_recall(term: str, pmids: str, as_json: bool | str = False) -> str:
    """已知文献找回测试：一批已知相关 PMID 里哪些被 term 检回。pmids 逗号/空格分隔，去重后最多 200 个。
    分批 esearch 交集判定 found/missing（每批 50 个，避免 GET URL 超长被 NCBI 414）；缺失项附标题与存在状态。
    只证明这批已知集的找回，不代表整体查全率；金标准集本身可能有偏。
    逐块诊断用 count("概念块 AND pmid[uid]")。查询警告原样透传；term 被忽略或部分词未生效时显著告警。
    """
    json_mode = _as_bool(as_json, "as_json")
    uniq, n_in = _parse_pmids(pmids)
    if len(uniq) > GOLD_MAX:
        raise NcbiError(f"check_recall 一次最多 {GOLD_MAX} 个金标准 PMID（本项目自订上限），"
                        f"收到 {len(uniq)} 个，请分批。")
    # 分批交集：一次塞 200 个 pmid[uid] 会让 GET URL 超长被 NCBI 414〔实测 A1〕。
    batches = [uniq[i:i + GOLD_CHUNK] for i in range(0, len(uniq), GOLD_CHUNK)]
    found_set, batch_ds = set(), []
    for batch in batches:
        gold_q = f"({term}) AND (" + " OR ".join(f"{p}[uid]" for p in batch) + ")"
        d = await _esearch(gold_q, retmax=len(batch))
        count = _count_of(d)
        got = d.get("idlist") or []
        bset = set(batch)
        if count > len(batch) or any(p not in bset for p in got):
            raise NcbiError(f"check_recall 交集结果异常：命中 {count}、返回 {len(got)} 个（本批金标准 {len(batch)} 个）。"
                            f"交集不应超出金标准集，请复核查询。")
        found_set.update(got)
        batch_ds.append(d)
    first_d = batch_ds[0]
    found = [p for p in uniq if p in found_set]
    missing = [p for p in uniq if p not in found_set]
    miss_info, meta_warn = [], ""
    if missing:
        try:
            recs = dict(await _esummary_dicts(missing, include_unavailable=True))
            for p in missing:
                r = recs.get(p)
                if r is not None and not r.get("error") and r.get("title"):
                    miss_info.append({"pmid": p, "state": "present", "title": r.get("title") or ""})
                elif r is not None and r.get("error") == "cannot get document summary":
                    miss_info.append({"pmid": p, "state": "unavailable", "title": ""})
                else:
                    miss_info.append({"pmid": p, "state": "unknown", "title": ""})
        except NcbiError as e:
            meta_warn = f"⚠️ 缺失项标题获取失败（不影响 found/missing 判定）：{str(e)[:200]}"
            miss_info = [{"pmid": p, "state": "unknown", "title": ""} for p in missing]
    caveat = ("只证明这批已知集的找回，不代表整体查全率；金标准集可能有偏（如只取某综述的参考文献）。"
              "present＝记录存在但未被本次实际执行式检回；unavailable/unknown＝存在性未确认，不能断定是 term 漏了它。")
    qt = (first_d.get("querytranslation") or "").strip()
    translations = [(d.get("querytranslation") or "").strip() for d in batch_ds]
    term_ignored = any(_term_vanished(t) for t in translations)
    warn, raw_degraded = _recall_warnings_and_degraded(term, batch_ds)
    term_degraded = (not term_ignored) and raw_degraded
    if term_ignored:
        warn.append("⚠️ term 未参与实际检索：NCBI 忽略了它（常见于中文词，翻译为空），实际执行式只剩金标准 "
                    "PMID 本身。上面的 found【不代表】term 匹配了这些文献——请换英文词或核对实际执行式后重跑。")
    elif term_degraded:
        warn.append("⚠️ term 存在部分概念未生效、词项未找到或语法失真（见上方警告）：多概念检索中若某侧概念被吞，"
                    "found 可能包含仅命中剩余概念的假阳性文献——请修正检索式后重跑。")
    if meta_warn:
        warn.append(meta_warn)
    if n_in != len(uniq):
        warn.append(f"（原始输入 {n_in} 个，去重后 {len(uniq)} 个）")
    if len(batches) > 1:
        warn.append(f"（金标准 {len(uniq)} 个分 {len(batches)} 批交集判定，每批最多 {GOLD_CHUNK} 个，避免 URL 超长）")
    _audit("check_recall", term=term, gold=len(uniq), found=len(found), missing=len(missing),
           term_ignored=term_ignored, term_degraded=term_degraded, querytranslation=qt,
           querytranslations=translations, pmids=uniq, warnings=warn)
    if json_mode:
        return json.dumps({"term": term, "gold_count": len(uniq), "found": found, "missing": miss_info,
                           "term_ignored": term_ignored, "term_degraded": term_degraded,
                           "querytranslation": qt, "querytranslations": translations,
                           "warnings": warn, "caveat": caveat}, ensure_ascii=False)
    head = [f"查询：{term}", f"金标准 {len(uniq)} 个 PMID，检回 {len(found)} 个，未检回 {len(missing)} 个"]
    if term_ignored:
        head.append("⚠️ 本次 found 不可信：term 被 NCBI 忽略，详见下方告警。")
    elif term_degraded:
        head.append("⚠️ 本次 found/missing 可能失真：term 存在部分词未生效或语法退化，详见下方告警。")
    head += warn
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
    head.append("说明：" + caveat)
    return "\n".join(head)


@mcp.tool()
async def mesh(term: str, retmax: StrictInt | StrictStr = 5, as_json: bool | str = False) -> str:
    """查 MeSH 主题词表：描述符、入口词（同义词）、树号、上下位词、历史前身词（previous indexing）、范围注、引入年份。
    词表以查询时点 NCBI 返回为准；retmax 默认 5、上限 20（本批候选内精确匹配置顶）。
    历史前身词供扩展旧标引词，不保证补齐；下位词（最多展 10 个）供决定是否展开。
    无描述符命中不等于主题无文献；NCBI 报 phrasesnotfound 时告警。
    """
    json_mode = _as_bool(as_json, "as_json")
    if not (term or "").strip():
        raise NcbiError("term 不能为空")
    n_req = _int(retmax, "retmax", 5)
    if n_req < 1:
        raise NcbiError(f"retmax={retmax!r} 要正数；查词给正整数（上限 {MESH_RETMAX_CAP}）。")
    n = min(n_req, MESH_RETMAX_CAP)
    clamp_note = "" if n_req <= MESH_RETMAX_CAP else f"retmax={n_req} 超过本项目自订上限，已夹到 {n}。"
    d = _safe_json(await _get_async("esearch.fcgi",
                                    {"db": "mesh", "term": term, "retmax": n, "retmode": "json"}, 25),
                   "esearch(mesh)")
    er = d.get("esearchresult")
    if not isinstance(er, dict):
        raise NcbiError("mesh esearch 返回结构异常")
    er["_term"] = term
    count = _count_of(er)
    ids = er.get("idlist")
    if (not isinstance(ids, list) or any(not isinstance(u, str) or not u.isdigit() for u in ids)
            or len(set(ids)) != len(ids) or len(ids) > min(count, n) or (count and not ids)):
        raise NcbiError("mesh esearch 的 UID 列表缺失、异常或与计数不符")
    qt = (er.get("querytranslation") or "").strip()
    warn = _note(er)
    e_er = er.get("errorlist")
    e_er = e_er if isinstance(e_er, dict) else {}
    if _as_list(e_er.get("phrasesnotfound") or []) or _warn_bits(er)[1]:
        warn.append("⚠️ MeSH 检索片段未找到或被改写；这不能证明片段已被丢弃。"
                    "请核对实际执行式，并尝试单数词形或核心词根重查。")
    if clamp_note:
        warn.append("⚠️ " + clamp_note)
    if not ids:
        note = f"MeSH 库未命中「{term}」的描述符。无描述符不等于主题无文献；请用标题/摘要同义词补充检索。"
        _audit("mesh", term=term, count=count, descriptors=0, querytranslation=qt, warnings=warn)
        if json_mode:
            return json.dumps({"term": term, "count": count, "descriptors": [],
                               "querytranslation": qt, "warnings": warn, "note": note}, ensure_ascii=False)
        return "\n".join([f"查询：{term}", note] + warn)
    s = _safe_json(await _get_async("esummary.fcgi",
                                    {"db": "mesh", "id": ",".join(ids), "retmode": "json"}, 25),
                   "esummary(mesh)")
    res = s.get("result")
    if not isinstance(res, dict):
        raise NcbiError("mesh esummary 返回结构异常")
    uids = res.get("uids")
    if (not isinstance(uids, list) or any(not isinstance(u, str) for u in uids)
            or set(uids) != set(ids) or len(set(uids)) != len(uids)):
        raise NcbiError("mesh esummary 返回的 UID 列表与请求不符，不能作为有效词表数据")
    descs, child_want, parent_want = [], [], []
    q_norm = term.strip().lower()
    for uid in uids:
        rec = res.get(uid)
        if not isinstance(rec, dict) or rec.get("error") or not rec.get("ds_meshterms"):
            raise NcbiError(f"mesh esummary 的 UID {uid} 记录结构异常或不可用，不能作为有效词表数据")
        if "uid" in rec and rec["uid"] != uid:
            raise NcbiError(f"mesh esummary 的 UID {uid} 与记录内部 uid 不一致，不能作为有效词表数据")
        terms = [str(t) for t in _as_list(rec.get("ds_meshterms") or []) if str(t).strip()]
        idx = _as_list(rec.get("ds_idxlinks") or [])
        kids, pars = [], []
        for x in idx:
            if not isinstance(x, dict):
                continue
            p_uid = x.get("parent")
            if p_uid is not None and str(p_uid).strip() and str(p_uid) != uid and str(p_uid) not in pars:
                pars.append(str(p_uid))
            for c in _as_list(x.get("children") or []):
                c = str(c)
                if c and c not in kids:
                    kids.append(c)
        prev_idx = [str(x).strip() for x in _as_list(rec.get("ds_previousindexing") or []) if str(x).strip()]
        rtype_raw = _as_list(rec.get("ds_recordtype") or [])
        rtype = str(rtype_raw[0]).strip() if rtype_raw and rtype_raw[0] is not None else ""
        name = terms[0] if terms else "( unnamed )"
        exact = q_norm in {t.lower() for t in ([name] + terms)}
        descs.append({
            "uid": uid, "ui": rec.get("ds_meshui") or "",
            # ds_meshterms 首项为主题词名〔实测 P1 一例〕；完整词表都在 entry_terms 里，首项异常也不丢信息。
            "name": name,
            "record_type": rtype,
            "exact_match": exact,
            "entry_terms": terms,
            "treenums": [str(x.get("treenum")) for x in idx if isinstance(x, dict) and x.get("treenum")],
            "parents": pars[:MESH_PARENT_SHOW], "parents_total": len(pars),
            "children": kids[:MESH_CHILD_SHOW], "children_total": len(kids),
            "previous_indexing": prev_idx,
            "scope_note": rec.get("ds_scopenote") or "",
            "year_introduced": rec.get("ds_yearintroduced") or "",
        })
        child_want.extend(kids[:MESH_CHILD_SHOW])
        parent_want.extend(pars[:MESH_PARENT_SHOW])
    rel_names = {de["uid"]: de["name"] for de in descs if de["name"] != "( unnamed )"}
    child_warns = []
    want = [u for u in dict.fromkeys(child_want + parent_want) if u not in rel_names]
    for i in range(0, len(want), ESUMMARY_CHUNK):
        chunk = want[i:i + ESUMMARY_CHUNK]
        try:
            cs = _safe_json(await _get_async("esummary.fcgi",
                                             {"db": "mesh", "id": ",".join(chunk), "retmode": "json"}, 25),
                            "esummary(mesh 下位词)")
            cres = cs.get("result")
            if not isinstance(cres, dict) or not isinstance(cres.get("uids"), list):
                raise NcbiError("mesh 关联词 esummary 返回结构异常")
            returned = cres["uids"]
            if (any(not isinstance(u, str) for u in returned) or set(returned) != set(chunk)
                    or len(set(returned)) != len(returned)):
                raise NcbiError("mesh 关联词 UID 列表与请求不符")
            names = {}
            for cu in returned:
                cr = cres.get(cu)
                if (not isinstance(cr, dict) or cr.get("error") or not cr.get("ds_meshterms")
                        or ("uid" in cr and str(cr["uid"]) != cu)):
                    raise NcbiError("mesh 关联词记录缺失或内部 UID 不符")
                names[cu] = str(_as_list(cr["ds_meshterms"])[0])
            rel_names.update(names)
        except NcbiError as e:
            child_warns.append(f"⚠️ 下位词名称获取失败（本批上位词亦仅显示 UID；其他批继续）：{str(e)[:200]}")
    warn.extend(dict.fromkeys(child_warns))
    for de in descs:
        de["children"] = [{"uid": c, "name": rel_names.get(c, "")} for c in de["children"]]
        de["parents"] = [{"uid": p, "name": rel_names.get(p, "")} for p in de["parents"]]
    exact_i = next((i for i, de in enumerate(descs) if de["exact_match"]), None)
    moved_from = None
    if exact_i is not None and exact_i > 0:
        picked = descs.pop(exact_i)
        descs.insert(0, picked)
        moved_from = exact_i + 1
    elif exact_i is None and count > len(descs):
        warn.append(f"提示：MeSH 共命中 {count} 个描述符，本次前 {len(descs)} 个中无字面精确匹配；"
                    f"可增大 retmax（上限 {MESH_RETMAX_CAP}）查看后续条目。")
    _audit("mesh", term=term, count=count, descriptors=len(descs), querytranslation=qt, warnings=warn)
    if json_mode:
        return json.dumps({"term": term, "count": count, "descriptors": descs,
                           "querytranslation": qt, "warnings": warn}, ensure_ascii=False)
    head = [f"查询：{term}", f"MeSH 命中 {count} 个描述符，本次给 {len(descs)} 个"] + warn
    for de in descs:
        head.append("")
        badge = ""
        if moved_from and de is descs[0]:
            badge = f" [精确匹配，已由第 {moved_from} 位置顶]"
        elif de["exact_match"] and len(descs) > 1:
            badge = " [精确匹配]"
        head.append(f"MeSH {de['ui']}｜{de['name']}"
                    + (f"（{de['year_introduced']} 年引入）" if de["year_introduced"] else "")
                    + badge)
        head.append("  入口词：" + "; ".join(de["entry_terms"]))
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


@mcp.tool()
async def diff(term_a: str, term_b: str, max_show: StrictInt | StrictStr = 20,
               as_json: bool | str = False) -> str:
    """对比两个 PubMed 查询的结果集：b 相对 a 新增、丢失了哪些 PMID。
    检索式迭代前后核对增减；两查询都须在 9999 窗口内，超窗报错（先缩小或改用 export）。
    max_show 默认 20、上限 200，文本档每侧只展示部分；as_json=True 给完整列表。
    集合差只描述增减，不判断相关性；命中更多不等于更好。两次查询间数据库可能更新。
    """
    json_mode = _as_bool(as_json, "as_json")
    ms_req = _int(max_show, "max_show", 20)
    if ms_req < 0:
        raise NcbiError(f"max_show={max_show!r} 不能为负；不想看列表给 0。")
    ms = min(ms_req, DIFF_SHOW_CAP)
    sides = [None, None]
    side_errs = [None, None]
    async with anyio.create_task_group() as tg:
        async def fetch_side(idx, q, ctx):
            try:
                sides[idx] = await _ids_in_window(q, context=ctx)
            except NcbiError as e:
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
    warn_a, warn_b = _note(a_first), _note(b_first)
    if a_drift:
        warn_a.append(a_drift)
    if b_drift:
        warn_b.append(b_drift)
    qt_a = (a_first.get("querytranslation") or "").strip()
    qt_b = (b_first.get("querytranslation") or "").strip()
    caveat = "集合差只描述增减，不判断相关性；命中更多不等于更好。两次查询间数据库可能更新，结果以取回时点为准。"
    _audit("diff", term_a=term_a, term_b=term_b, count_a=a_count, count_b=b_count,
           added=len(added), removed=len(removed), common=common,
           querytranslation_a=qt_a, querytranslation_b=qt_b,
           warnings_a=warn_a, warnings_b=warn_b)
    if json_mode:
        return json.dumps({"term_a": term_a, "term_b": term_b, "count_a": a_count, "count_b": b_count,
                           "added_count": len(added), "removed_count": len(removed), "common_count": common,
                           "added": added, "removed": removed,
                           "querytranslation_a": qt_a, "querytranslation_b": qt_b,
                           "warnings_a": warn_a, "warnings_b": warn_b, "caveat": caveat}, ensure_ascii=False)
    head = [f"term_a：{term_a}", f"  命中 {a_count}，取回 {len(a_ids)}"]
    head += ["  " + w for w in warn_a]
    head += [f"term_b：{term_b}", f"  命中 {b_count}，取回 {len(b_ids)}"]
    head += ["  " + w for w in warn_b]
    head.append(f"新增 {len(added)} 条（b 有 a 无）；丢失 {len(removed)} 条（a 有 b 无）；共同 {common} 条")
    if ms:
        if added:
            head.append(f"新增（展示 {min(ms, len(added))}/{len(added)}）：" + ", ".join(added[:ms]))
        if removed:
            head.append(f"丢失（展示 {min(ms, len(removed))}/{len(removed)}）：" + ", ".join(removed[:ms]))
    head.append("说明：" + caveat)
    return "\n".join(head)


def _date_mid(lo, hi):
    a = datetime.date.fromisoformat(lo)
    b = datetime.date.fromisoformat(hi)
    return (a + (b - a) / 2).isoformat()


def _next_day(d):
    return (datetime.date.fromisoformat(d) + datetime.timedelta(days=1)).isoformat()


async def _export_slice(term, lo, hi, state):
    """按发表日期递归二分，直到每片落进 9999 窗口。返回 (ids, 片数, 切片计数和)。
    切片经 mindate/maxdate（datetype=pdat）下发，不改用户 term；term 自带的日期条件由 NCBI 做 AND。"""
    d = await _esearch(term, retmax=0, mindate=lo, maxdate=hi)
    c = _count_of(d)
    if c == 0:
        return [], 0, 0
    if c <= WINDOW:
        ids, c2, first, drift = await _ids_in_window(term, mindate=lo, maxdate=hi,
                                                     context=f"export 切片 {lo}→{hi}：")
        if drift:
            state["drift"].append(drift)
        return ids, 1, c
    mid = _date_mid(lo, hi)
    if lo >= hi or mid < lo or mid >= hi:
        raise NcbiError(f"export：区间 {lo}→{hi} 仍有 {c} 条超过 {WINDOW} 窗口且无法再二分；"
                        f"请缩小查询（加字段/日期限定）后重试。")
    lids, ln, lc = await _export_slice(term, lo, mid, state)
    rids, rn, rc = await _export_slice(term, _next_day(mid), hi, state)
    return lids + rids, ln + rn, lc + rc


async def _export_csv(path, ids):
    """核心题录字段写 CSV〔字段本项目自订〕；utf-8-sig 便于 Excel 直接打开。
    元数据缺失/不可用的行在 note 列标明，不静默丢弃。返回 (记录数, 问题号列表)。"""
    recs = dict(await _esummary_dicts(ids, include_unavailable=True))
    problems = []
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pmid", "pubdate", "title", "journal", "authors", "author_count", "doi", "pmc", "pubtype", "note"])
        for p in ids:
            r = recs.get(p)
            if r is None:
                w.writerow([p, "", "", "", "", "", "", "", "", "元数据缺失"])
                problems.append(p)
                continue
            if r.get("error"):
                w.writerow([p, "", "", "", "", "", "", "", "", "esummary 不可用：" + str(r.get("error"))[:80]])
                problems.append(p)
                continue
            parts = _record_parts(p, r)
            w.writerow([parts["pmid"], parts["pubdate"], parts["title"], parts["journal"],
                        parts["authors"], parts["author_count"], parts["doi"], parts["pmc"],
                        "/".join(parts["pubtype"]), ""])
    return len(ids) - len(problems), problems


async def _export_medline(path, ids):
    """efetch MEDLINE 格式（.nbib）分批写文件。按 ^PMID- 行校验归属（MEDLINE 排版用 PMID-，
    与摘要端的 PMID: 不同）。返回 (PMID- 行数, 缺记录的号)。"""
    chunks = [ids[i:i + EFETCH_CHUNK] for i in range(0, len(ids), EFETCH_CHUNK)]
    out = [None] * len(chunks)
    errs = []

    async def one(i, c):
        try:
            text = await _get_async("efetch.fcgi",
                                    {"db": "pubmed", "id": ",".join(c), "rettype": "medline", "retmode": "text"},
                                    min(60, 20 + 0.4 * len(c)))
            head = text.lstrip()[:200].lower()
            if head.startswith("<!doctype") or head.startswith("<html"):
                raise NcbiError("efetch 回的是 HTML 页面（HTTP 200 也可能是故障页），不是 MEDLINE 正文。原文开头："
                                + _mask(text.lstrip()[:200]))
            got = re.findall(r"^PMID- (\d+)", text, re.M)
            if len(got) != len(set(got)) or not set(got).issubset(c):
                raise NcbiError("efetch MEDLINE 返回的 PMID 与请求不符或重复，不能作为有效导出")
            if not got and not re.fullmatch(r"\s*(?:\d+\.\s*)+", text):
                raise NcbiError("efetch 返回了无法识别的 MEDLINE 正文；原文开头：" + _mask(text.lstrip()[:200]))
            out[i] = text
        except NcbiError as e:
            errs.append(e)

    async with anyio.create_task_group() as tg:
        for i, c in enumerate(chunks):
            tg.start_soon(one, i, c)
    if errs:
        raise errs[0]
    text = "\n\n".join(t for t in out if t)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text + ("\n" if text else ""))
    got = re.findall(r"^PMID- (\d+)", text, re.M)
    missing = [p for p in ids if p not in set(got)]
    return len(got), missing


def _publish_no_clobber(tmp, p):
    """原子发布且不覆盖。无可用原语时失败，不退回 exists + replace。"""
    try:
        if os.name == "nt":
            os.rename(tmp, p)  # Windows rename 在目标存在时失败；POSIX rename 会覆盖。
        else:
            os.link(tmp, p)  # 同目录硬链接：目标存在时原子失败。
    except FileExistsError as e:
        raise NcbiError(f"文件已存在：{p}。本工具不覆盖已有文件，请换个路径或先移走旧文件。") from e
    except OSError as e:
        raise NcbiError(f"无法安全发布导出文件（{p}）：{e}；请使用支持原子不覆盖发布的本地文件系统。") from e


@mcp.tool()
async def export(term: str, path: str, format: Literal["pmids", "csv", "medline"] = "pmids",
                 as_json: bool | str = False) -> str:
    """取全某查询的 PMID 并写文件；超 9999 窗口自动按发表日期二分切片。
    format: pmids（纯 PMID 列表）/ csv（核心题录字段，不含摘要）/ medline（.nbib，保留来源中已有的摘要与主题词）。
    只回摘要（总数/片数/去重/路径/sha256），不回记录本身。path 显式给出且不得已存在（不覆盖）。
    切片计数与总数核对，不一致如实报告；检索期间数据库可能更新，结果以导出时点为准。
    自订安全上限 10 万条。筛选工具导入兼容性以对方文档为准；不保证与 PubMed 网页导出逐字一致。
    """
    if format not in ("pmids", "csv", "medline"):
        raise NcbiError("format 只支持 pmids / csv / medline")
    json_mode = _as_bool(as_json, "as_json")
    raw_p = os.path.expanduser((path or "").strip())
    if not raw_p:
        raise NcbiError("path 不能为空；请给明确的输出文件路径。")
    p = os.path.abspath(raw_p)
    if os.path.isdir(p):
        raise NcbiError(f"path 是已存在的目录而非文件路径：{p}")
    if os.path.exists(p):
        raise NcbiError(f"文件已存在：{p}。本工具不覆盖已有文件，请换个路径或先移走旧文件。")
    t0 = datetime.datetime.now().isoformat(timespec="seconds")
    nreq0 = _NREQ
    d0 = await _esearch(term, retmax=0)
    total = _count_of(d0)
    qt = (d0.get("querytranslation") or "").strip()
    warn = _note(d0, include_translation=False)
    if total > EXPORT_MAX:
        raise NcbiError(f"查询命中 {total} 条，超过 export 本项目自订安全上限 {EXPORT_MAX}；"
                        f"请缩小查询（加字段/日期限定）后重试。")
    state = {"drift": []}
    if total <= WINDOW:
        ids, cnt, first, drift = await _ids_in_window(term, context="export：")
        n_slices, slice_sum = 1, cnt
        if drift:
            state["drift"].append(drift)
    else:
        ids, n_slices, slice_sum = await _export_slice(term, NO_BEGIN, NO_END, state)
    raw_n = len(ids)
    ids = list(dict.fromkeys(ids))
    d_end = await _esearch(term, retmax=0)
    total_end = _count_of(d_end)
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
        raise NcbiError(f"无法创建导出父目录（{parent}）：{e}") from e
    import tempfile
    try:
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(p) + ".", suffix=".part", dir=parent)
        os.close(fd)
    except OSError as e:
        raise NcbiError(f"无法创建导出临时文件（{parent}）：{e}") from e
    coverage = ""
    try:
        if format == "pmids":
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(ids) + ("\n" if ids else ""))
        elif format == "csv":
            ok, problems = await _export_csv(tmp, ids)
            if problems:
                coverage = (f"⚠️ CSV 有 {len(problems)} 个号元数据缺失/不可用，已在 note 列标明（不静默丢弃）："
                            + ", ".join(problems[:10]) + ("…" if len(problems) > 10 else ""))
        else:
            n_rec, miss = await _export_medline(tmp, ids)
            if miss:
                coverage = (f"⚠️ MEDLINE 正文有 {n_rec} 条 PMID- 行，{len(miss)} 个号没取到记录："
                            + ", ".join(miss[:10]) + ("…" if len(miss) > 10 else ""))
        with open(tmp, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        _publish_no_clobber(tmp, p)
    except OSError as e:
        raise NcbiError(f"导出文件写入失败（{p}）：{e}") from e
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"⚠️ 导出临时文件清理失败（{tmp}）：{e}", file=sys.stderr)
    nreq = _NREQ - nreq0
    warn += mismatch + state["drift"] + ([coverage] if coverage else [])
    _audit("export", term=term, path=p, format=format, count_start=total, count_end=total_end,
           slices=n_slices, unique=len(ids), sha256=sha, querytranslation=qt, warnings=warn)
    if json_mode:
        return json.dumps({"term": term, "path": p, "format": format, "count_start": total,
                           "count_end": total_end, "slices": n_slices, "fetched": raw_n, "unique": len(ids),
                           "sha256": sha, "started": t0, "ended": t1, "requests": nreq,
                           "querytranslation": qt, "warnings": warn}, ensure_ascii=False)
    head = [f"导出完成：{p}", f"查询：{term}", f"NCBI 实际执行：{qt}",
            f"命中 {total} 条；切片 {n_slices} 片；取回 {raw_n} 个 PMID，去重后 {len(ids)} 个；格式 {format}",
            f"时间 {t0} → {t1}；本次约 {nreq} 次 HTTP 请求（含重试，本项目自订口径）",
            f"sha256 {sha}"]
    head += warn
    head.append("⚠️ 结果以导出时点为准；筛选工具导入兼容性以对方文档为准。")
    return "\n".join(head)


# ---------- 自检 ----------
def _selftest(term):
    print("key:", _key_shape(api_key()))   # 〔发布版改动〕旧写法直接打印前 4 位，与本文件 --check-key 的口径自相矛盾
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


def _raises_enc(text, codepage):
    """text 在 codepage 上编码会不会抛（用于断言判据本身有效，不碰真 stdout）。"""
    try:
        text.encode(codepage)
        return False
    except UnicodeEncodeError:
        return True


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
        except Exception as e:
            # 〔实测 2026-11-11〕旧写法捕 BaseException，把 Ctrl-C/SystemExit 也当被测调用失败吞掉；
            # 且引用了 3.11 才有的内建 BaseExceptionGroup —— pyproject 声明 >=3.10，那边会 NameError。
            # 只认异常自带的 .exceptions（anyio/exceptiongroup 都有），不引用那个名字。
            eg = getattr(e, "exceptions", None)
            real = eg[0] if eg else e
            check(f"{name} 意外抛错", False, f"{type(real).__name__}: {str(real)[:200]}")
            return default

    num = _num_of        # 〔2026-11-10〕原来定义在第 13 节里，前面几处拿不到，只能裸写 re.search

    print("1) 拼错的词必须在「NCBI 实际执行」里现形")
    s = await attempt("s", search, "anchorage devcie", retmax=1, default="")
    check("querytranslation 已打印", "NCBI 实际执行" in s, s[:200])
    check("未找到的词被点名，不假定丢弃", "phrasesnotfound" in s and "已【丢弃】" not in s, s[:300])

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
    check("空解析被点名", "未返回实际执行式" in c, c[:160])
    check("给了英文对照提示", "对应英文词重查" in c, c[:160])
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
    check("retmax=0 的 0 条不许说成「确实没有匹配这篇」",
          "retmax=0" in s_zero and "确实没有匹配这篇" not in s_zero, s_zero[-260:])
    j_zero = await attempt_json("s_zero_json", search, "miniscrew", retmax=0, as_json=True)
    _zn = str(j_zero.get("zero_note") or "")
    check("as_json 的 zero_note 同样不许说「确实没有匹配」",
          "retmax=0" in _zn and "确实没有匹配" not in _zn, _zn[:260])
    # 〔实测 2026-11-11〕这条要核对的是「回显没被改写」，光看 count>0 不够：descriptor 不加引号
    # 写错时 NCBI 照样回一个正数，只是把条件换成了别的（见上面那条离线断言的实测记录）。
    # 回显与输入相同时文本输出不印「NCBI 实际执行」，所以这里走 as_json 读 querytranslation。
    _mesh_term = '"Orthodontic Anchorage Procedures"[MeSH Terms]'
    j_mesh = await attempt_json("c_mesh", search, _mesh_term, retmax=0, as_json=True)
    _mesh_qt = str(j_mesh.get("querytranslation") or "")
    check("说明里推荐的 MeSH 示例真能查到东西，且回显没有被改写",
          int(j_mesh.get("count") or 0) > 0 and _mesh_qt.strip().lower() == _mesh_term.lower(),
          f"count={j_mesh.get('count')} qt={_mesh_qt[:120]}")
    c_tag = await attempt("c_tag", count, "resorption[Title]", default="")
    check("文档里的字段标签示例 resorption[Title] 真能查到东西", _num_of(c_tag) > 0, c_tag[:160])
    _sd0 = getattr(search, "__doc__", "") or ""
    check("工具说明里的字段标签示例仍是【不带引号】的正确形状",
          "resorption[Title]" in _sd0 and '"resorption[Title]"' not in _sd0, _sd0[:120])
    # 〔实测 2026-11-11，本会话〕09b1ecf 一度把 MeSH 那段写成「写错一个词的引号短语会静默变成 0 条」。
    # 三种写法各跑一次 count 把它推翻了：加引号那种 0 条【有】warninglist.quotedphrasesnotfound 点名
    # （不算静默，本工具会转述），真正没人提醒的是不加引号那次（回显被换成别的条件、照样给数）。
    # 这条离线断言钉住纠正后的形状，防止「静默 0 条」被重新翻出来写进说明。
    check("MeSH 说明不再把「加引号写错」说成静默，并保留「不加引号会被改写」这一支",
          "静默变成 0 条" not in _sd0 and "quotedphrasesnotfound" in _sd0
          and "不带引号可能改写" in _sd0, _sd0[:200])
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
    check("无效号明确报告元数据不可用，不推断无全文", "cannot get document summary" in l_bad, l_bad[:300])
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
            # 〔实测 2026-11-11〕旧写法 check(..., True)：只要抛的是 NcbiError 就算过，网络故障
            # 也照样 PASS。改成核对报错文本是不是本工具的前置校验。
            _want = "负数" if bad_rel < 0 else "太大"
            check(f"reldate={bad_rel} 必须由本工具的前置校验拦下", _want in str(e), str(e)[:120])
        except Exception as e:
            check(f"reldate={bad_rel} 必须报错", False, f"{type(e).__name__}（不是 NcbiError）：{str(e)[:90]}")
    c_old = await attempt("c_old", count, "tooth", maxdate="1800-01-01", default="")
    check("早于 1809 的区间不能报 0（旧版补的下界 1809-01-01 会把这段整个切掉）",
          num(c_old) > 0 and "0001-01-01" in c_old, c_old[:300])
    s_old = await attempt("s_old", search, "tooth", maxdate="1800-01-01", retmax=3, default="")
    check("这段真文献不能说成「确实没有匹配这篇」", "确实没有匹配这篇" not in s_old, s_old[-240:])
    s_drop = await attempt("s_drop", search, "zzzqqx AND qbvcjk", retmax=5, default="")
    check("全词被丢＋布尔算子时不许下「确实没有匹配」的结论",
          "全部被报告" in s_drop and "确实没有匹配这篇" not in s_drop, s_drop[-300:])
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
    _d_dis_hit = dict(_d_dis, count="1")
    _nt_hit = "\n".join(_note(_d_dis_hit))
    check("同字段不相交但命中>0：不许说「也会是 0 条」，也不许替 0 条下结论",
          "也会是 0 条" not in _nt_hit and "不相交不等于 0 条" in _nt_hit, _nt_hit[:300])
    _d_rm0 = dict(_dd("miniscrew"), _retmax=0)
    _z_rm0 = _zero_explanation(_d_rm0, 313644)
    check("retmax=0 的 0 条解释说清是「只要计数」，不说「确实没有匹配」",
          "retmax=0" in _z_rm0 and "确实没有匹配" not in _z_rm0, _z_rm0[:200])
    _d_page = dict(_dd("x"), retmax="0", retstart="9", _retmax=5)
    _z_page = _zero_explanation(_d_page, 8)
    check("越页时 NCBI 回显 retmax=0，解释必须是「翻页越界」，不是「只要计数」",
          "越过了命中总数" in _z_page and "retmax=0" not in _z_page, _z_page[:200])

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

    # ---- 20) key 文件编码与 CLI 输出编码（补丁 20，三条都在临时目录复现过）----
    print("\n20) key 文件编码（UTF-16 / BOM）与 ⚠️ 的可编码性")
    import tempfile
    _d = tempfile.mkdtemp()
    _cases = [
        ("UTF-16LE+BOM（PowerShell 5.1 的 > 默认）", "utf-16", False),
        ("UTF-8 带 BOM（记事本）", "utf-8-sig", False),
        ("UTF-8 无 BOM", "utf-8", False),
        ("UTF-8 带 BOM 且结尾有换行", "utf-8-sig", True),
    ]
    for _name, _enc, _nl in _cases:
        _q = os.path.join(_d, "k_" + _enc + ("_nl" if _nl else ""))
        open(_q, "w", encoding=_enc).write("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4" + ("\n" if _nl else ""))
        _got = _decode_key_bytes(open(_q, "rb").read())
        check(f"key 文件 {_name}：剥干净且不许抛错",
              _got.strip() == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4" and "\ufeff" not in _got,
              f"{_name} → {_got[:24]!r} len={len(_got)}")
    # 反向判据（不写成恒真）：旧写法【确实】会在 UTF-16 字节上抛，说明上面那条不是空跑
    open(os.path.join(_d, "u16"), "w", encoding="utf-16").write("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
    _u16 = open(os.path.join(_d, "u16"), "rb").read()
    try:
        _u16.decode("utf-8")
        check("反向：旧写法 io.open(encoding='utf-8') 读 UTF-16 文件必须抛 UnicodeDecodeError",
              False, "居然解开了，那 A 条根本不成立")
    except UnicodeDecodeError as _e:
        check("反向：旧写法 io.open(encoding='utf-8') 读 UTF-16 文件必须抛 UnicodeDecodeError",
              True, str(_e)[:70])
    # 反向：BOM 不在 str.strip() 的空白集里，所以旧写法带 BOM 时不会被"顺手"清掉
    check("反向：strip() 清不掉 BOM（说明 B 条是真 bug，不是我想多了）",
          "\ufeff" in "\ufeffabc".strip(), repr("\ufeffabc".strip()))
    check("_key_shape 认得出混进文字的 key，且不打印值本身",
          "非字母数字" in _key_shape("my key is abc") and "my key" not in _key_shape("my key is abc"),
          _key_shape("my key is abc"))
    # 长度不是判据（本机那串就不是 32 位〔实测〕，所以 32 与非 32 都不许被说成可疑）
    check("32 位 hex 只被描述、不被夸成「形状正常」",
          "形状正常" not in _key_shape("a1" * 16) and "纯十六进制" in _key_shape("a1" * 16),
          _key_shape("a1" * 16))
    check("长度 36 的 hex 不许被报成可疑（作者本机 key 正是这种非 32 位的形状）",
          "可疑" not in _key_shape("a1" * 18) and "混进了别的文字" not in _key_shape("a1" * 18),
          _key_shape("a1" * 18))
    check("判据里没有「32 位」这种没核实过的口径", "32 位" not in _key_shape("a1" * 18),
          _key_shape("a1" * 18))
    check("_key_shape 空值直接说走匿名档", "匿名" in _key_shape(""), _key_shape(""))
    # 分组号按补丁号命名：17-19 是纯代码修复、没新增断言，所以编号从 16) 跳到 20)。
    # 〔发布版新增三条，全离线不发请求〕目的：把 README 那句「每条都有源码依据」钉成断言，
    # 否则承诺会随着下一次改写重新变成空话。
    try:
        _safe_json("<!DOCTYPE html><html><body>upstream oops</body></html>", "esearch")
        _html = "没抛错"
    except NcbiError as _e:
        _html = str(_e)
    check("200 + HTML 正文要报 NcbiError 并把正文带回来（不许静默当空结果）",
          "不是合法 JSON" in _html and "<!DOCTYPE" in _html, _html[:120])
    _sd = getattr(search, "__doc__", "") or ""
    check("search 的工具说明里 sort 三个值都在（MCP 客户端只读这段，说明过期＝行为过期）",
          all(w in _sd for w in ("pub_date", "relevance", "author")), repr(_sd[:90]))
    _sav = globals()["_KEY"]
    globals()["_KEY"] = "a1" * 18
    try:
        _m = _mask("leak:" + "a1" * 18 + " tail")
    finally:
        globals()["_KEY"] = _sav
    check("_mask 不留任何 key 片段（定长掩码；短 key 也掩，旧写法这两条都漏）",
          ("a1" * 18) not in _m and "***" in _m, _m[:60])
    # 〔发布版新增，离线〕SDK 兼容层自己也得有断言：「本文件同时支持 mcp 1.x 与 2.x」是一句
    # 会被下一次改写悄悄作废的承诺，没有断言就等着它过期（这个文件已经有一条这样的过期记录）。
    check("SDK 兼容层拿到的是可用服务类（能构造、有 tool、有 run）",
          callable(FastMCP) and hasattr(FastMCP, "tool") and hasattr(mcp, "run"),
          "%s.%s" % (FastMCP.__module__, FastMCP.__name__))
    # 〔发布版新增，第 5 条〕这一条必须**走 SDK 的工具包装层**，不能像本探针其余断言那样直调协程：
    # 直调看不见「异常经过 tools/base.py 时文本还不还在」，而 mcp 2.x 正是在这里把非 ToolError 的
    # 原文丢掉的（实测 2026-11-10：同一个拦截，1.30.0 给调用方 323 字符、未修的 2.3.0 只给 27 字符）。
    # 这条断言就是那个 bug 的回归锁——它挂掉＝「失败绝不静默」被打穿，而不是某句文案不好看。
    # 不发请求：_esearch 第一件事就是 _check_sort，在联网之前抛。
    try:
        _wrap = await mcp.call_tool("search", {"term": "x", "sort": "journal"})
        _wtxt = str(_wrap)
    except Exception as _we:
        _wtxt = str(_we)
    check("拦截理由要能穿过 SDK 工具包装层送到调用方（v2 会剥掉非 ToolError 的文本）",
          ("可用值" in _wtxt) and ("journal" in _wtxt), _wtxt[:150])
    # 〔2026-11-11 补齐，第 6/7/8 条〕上面那条只锁了 `search` 一个工具，另外三个当时只有
    # 「一次性实测」。实测过 ≠ 有回归锁：下次谁把 NcbiError 的基类改回去，探针只会看见 search
    # 一条红，count / fetch_abstracts / links 三条照样悄悄通过，而陌生人踩到的恰好是其中任意一条。
    # 入参一律选【发请求之前就抛】的分支（field 白名单 / PMID 解析 / 库名白名单都在联网前），
    # 所以这三条不新增 HTTP，本轮请求数仍是 89。
    for _wt, _wa, _wn in (("count", {"term": "x", "field": "ISSN"}, "ISSN"),
                          ("fetch_abstracts", {"pmids": "abc"}, "不是 PMID"),
                          ("links", {"pmids": "37427694", "to": "PMCID"}, "不是本工具支持的库"),
                          ("check_recall", {"term": "x", "pmids": " ".join(str(i) for i in range(1, GOLD_MAX + 2))}, "最多"),
                          ("mesh", {"term": ""}, "不能为空"),
                          ("diff", {"term_a": "x", "term_b": "y", "max_show": -1}, "不能为负"),
                          ("export", {"term": "x", "path": ""}, "不能为空")):  # 本项目自订离线探针输入
        try:
            _w2 = str(await mcp.call_tool(_wt, _wa))
        except Exception as _w2e:
            _w2 = str(_w2e)
        check("包装层文本完整：%s（与 search 同一条通路，不是一次性实测）" % _wt,
              _wn in _w2, _w2[:120])
    # 〔第 9 条〕把上面那条改口的依据钉住：库名大小写不敏感。纯函数，离线，不发请求。
    # 依据：实测 links(to="PMC") 走的是【成功】分支（回 pubmed_pmc=PMC10461258），
    # 而旧报错却写「不认 'PMC'」——那句在它自己的报错里反证了自己（见 _check_db 上方的已作废说明）。
    check("库名大小写不敏感：_check_db('PMC') 与 _check_db(' Gene ') 都归一到库名",
          _check_db("PMC") == "pmc" and _check_db(" Gene ") == "gene",
          "_check_db('PMC')=%s" % _check_db("PMC"))
    # 〔第 10 条，离线〕README 行为契约第 9 条的后半句「本服务四个工具全是 async def」此前
    # 【没有断言钉住】，而那半句是承重的：装饰之后的名字一旦不再是协程函数，SDK 就走同步分支
    # （1.x 是裸调用＝本文件占住事件循环；2.x 自己 to_thread 卸载）。装饰器返回值随 SDK 版本变，
    # 所以要问的是【装饰之后】那个名字，不是源码里的 def。
    # 〔实测 2026-11-11：mcp 1.30.0 与 2.3.0 两代下，四个名字装饰后都仍是协程函数〕
    _tools = (search, count, fetch_abstracts, links, check_recall, mesh, diff, export)
    _coro = [f for f in _tools if inspect.iscoroutinefunction(f)]
    check("全部工具装饰后仍是协程函数（离线）",
          len(_coro) == len(_tools), f"{len(_coro)}/{len(_tools)} 是协程函数")
    # C：⚠️ 必须在 gbk/charmap 上编得出来——这里不碰真 stdout，只验「reconfigure 之后
    #      用的是 utf-8」这件事，以及输出文本里的特殊符号在非 utf-8 上确实编不出（旧版会炸）。
    _probe_line = "⚠️ 检索式被 NCBI 改写 牙根吸收 ✓"
    _crashes = [c for c in ("cp936", "cp1252") if _raises_enc(_probe_line, c)]
    check("判据本身有效：⚠️/中文在 cp936、cp1252 上确实编不出（所以 reconfigure 不是多余）",
          len(_crashes) == 2, f"编不出的代码页: {_crashes}")

    # 引号短语被换成按词 AND 时，命中数【不是】短语的数——这条判据不写死具体命中数
    _ph = await attempt("c_phrase", count,
                        '41338119 AND "zygomatic implant assisted rapid palatal expansion"', default="")
    _phn = _num_of(_ph)
    check("整段短语被 NCBI 换成按词 AND 时，仍有命中且必须说出「这不是短语的数」",
          _phn > 0 and "quotedphrasesnotfound" in _ph and "不是" in _ph,
          f"{_phn} 条；⚠️ 段落: {[l[:70] for l in _ph.splitlines() if 'quoted' in l][:1]}")

    # 15) 本轮离线回归：每条都对应一个【实测过】的缺陷，不发请求
    print("15) 离线回归：本轮修掉的静默与误判")
    _ar = "٢٠٢٠-٠١-٠١"
    check("日期里的 Unicode 数字被归一成 ASCII（不再原样下发被 NCBI 丢掉）",
          _check_date(_ar, "mindate") == "2020-01-01", repr(_check_date(_ar, "mindate")))
    try:
        _check_date("2023-02-29", "mindate")
        check("反向：日历上不存在的日期仍然被拦", False, "没抛")
    except NcbiError:
        check("反向：日历上不存在的日期仍然被拦", True, "")
    for _bad in ("1，234，567", "1,234,567,", "1,234,567"):
        try:
            _parse_pmids(_bad)
            check(f"千位分隔符形状不再被切成多个 PMID：{_bad}", False, "没抛")
        except NcbiError:
            check(f"千位分隔符形状不再被切成多个 PMID：{_bad}", True, "")
    check("反向：正常逗号列表仍能解析", _parse_pmids("12345,678")[0] == ["12345", "678"], "")
    try:
        _parse_pmids("9" * 4301)
        check("超长数字串给的是 NcbiError，不是裸 ValueError", False, "没抛")
    except NcbiError:
        check("超长数字串给的是 NcbiError，不是裸 ValueError", True, "")
    except ValueError as e:
        check("超长数字串给的是 NcbiError，不是裸 ValueError", False, f"裸 {type(e).__name__}")
    try:
        _decode_key_bytes(b"\xff\xfeA")
        check("截断的 UTF-16 BOM key 文件不再抛裸 UnicodeDecodeError", True, "")
    except Exception as e:
        check("截断的 UTF-16 BOM key 文件不再抛裸 UnicodeDecodeError", False, type(e).__name__)
    check("带字段标签的全丢查询不再漏判（不再说「确实没有匹配」）",
          _all_dropped({"errorlist": {"phrasesnotfound": ["zzzqqx"]}, "_term": "zzzqqx[Title]",
                        "querytranslation": "zzzqqx[Title]"}) is True, "")
    try:
        _check_field("ZZZBOGUSFIELD")
        _fmsg = ""
    except NcbiError as e:
        _fmsg = str(e)
    check("未知 field 的「可用值」不再列出会被吞掉的值",
          "PMC Number" not in _fmsg and "Title" in _fmsg, _fmsg[:160])
    _old_get, _old_exist = globals()["_get_async"], globals()["_pmids_exist"]

    async def _fake_elink(path, params, timeout, tries=4):
        if path.startswith("elink"):
            return json.dumps({"linksets": [{"dbfrom": "pubmed", "ids": ["7545954"], "linksetdbs": [
                {"dbto": "nuccore", "linkname": "pubmed_nuccore_refseq", "links": ["1947007834"]}]}]})
        return "{}"

    async def _fake_exist(pids):
        return {pid: "present" for pid in pids}

    globals()["_get_async"], globals()["_pmids_exist"] = _fake_elink, _fake_exist
    try:
        _s_nuc = await links("7545954", to="nucleotide")
    finally:
        globals()["_get_async"], globals()["_pmids_exist"] = _old_get, _old_exist
    # 只看这一行：文末图例会原样引用「(nucleotide 库无直接对应记录)」这句话，用整段做判据会恒假。
    _nuc_line = next((l for l in _s_nuc.splitlines() if l.startswith("PMID 7545954")), "")
    check("nucleotide 的直接链接不再被说成「无直接对应记录」",
          "无直接对应记录" not in _nuc_line and "pubmed_nuccore_refseq=" in _nuc_line, _nuc_line[:200])
    try:
        await search("tooth", retstart=-5)
        check("负 retstart 被拦下（不再静默变成第一页）", False, "没抛")
    except NcbiError as e:
        check("负 retstart 被拦下（不再静默变成第一页）", "负数" in str(e), str(e)[:120])

    async def _fake_html(path, params, timeout, tries=4):
        return "<!DOCTYPE html><html><body>502 maintenance</body></html>"

    _old_get = globals()["_get_async"]
    globals()["_get_async"] = _fake_html
    try:
        await fetch_abstracts("37427694")
        check("efetch 回 HTML 时必须算失败（不把故障页当摘要）", False, "没抛")
    except NcbiError as e:
        check("efetch 回 HTML 时必须算失败（不把故障页当摘要）", "HTML" in str(e), str(e)[:120])
    finally:
        globals()["_get_async"] = _old_get

    async def _fake_mixed(path, params, timeout, tries=4):
        if path.startswith("esearch"):
            return json.dumps({"esearchresult": {"count": "2", "retmax": "2", "retstart": "0",
                                                 "idlist": ["1001", "1002"], "querytranslation": "x"}})
        if path.startswith("esummary"):
            return json.dumps({"result": {"uids": ["1001"], "1001": {
                "uid": "1001", "title": "T", "pubdate": "2020", "source": "J",
                "authors": [], "elocationid": ""}}})
        return "{}"

    _old_get = globals()["_get_async"]
    globals()["_get_async"] = _fake_mixed
    try:
        _j_miss = json.loads(await search("x", retmax=2, as_json=True))
    finally:
        globals()["_get_async"] = _old_get
    check("as_json 在 esummary 少回时给出 ⚠️，returned 记的是实际记录数",
          any("esummary 只回了" in w for w in _j_miss.get("warnings") or [])
          and _j_miss.get("returned") == 1,
          str({k: _j_miss.get(k) for k in ("returned", "esummary_missing")}))
    try:
        main(["--probeX"])
        check("拼错的 CLI 参数以非 0 退出", False, "居然没 SystemExit")
    except SystemExit as e:
        check("拼错的 CLI 参数以非 0 退出", e.code == 2, f"code={e.code}")
    except Exception as e:
        check("拼错的 CLI 参数以非 0 退出", False, f"{type(e).__name__}: {str(e)[:80]}")

    check("计数器活着（不是恒 0；阈值 40 是自订的下限，用来发现计数器被改坏）",
          _NREQ > 40, f"本轮 {_NREQ} 次 urlopen")
    print(f"\n{ok} 通过 / {fail} 失败 —— 本轮 {_NREQ} 次 HTTP 请求（含退避重试的每一次）")
    return fail


def _check_key():
    k = api_key()
    print("key:", _key_shape(k))
    if k:
        print("  （值本身不打印；前缀只在你自己机器上看得见——发布版源码里也不留任何 key 片段）")
        # 〔实测 2026-11-11〕旧版把「第一个存在的 key 文件」当成实际来源报出来；环境变量生效时
        # 那句话是假的。现在直接报真正读到 key 的那一处。
        print("key 来源:", _KEY_SOURCE or "(未知)")
    print("限速档位:", "10 次/秒" if k else "3 次/秒")
    for p in ("~/.ncbi_api_key", "~/.ncbi_api_key.txt"):
        q = os.path.expanduser(p)
        if os.path.isfile(q):
            head = open(q, "rb").read(3)
            enc = ("UTF-16（PowerShell 默认写法，本工具认得）" if head[:2] in (b"\xff\xfe", b"\xfe\xff")
                   else "UTF-8 带 BOM（记事本写法，本工具会剥掉）" if head == b"\xef\xbb\xbf"
                   else "UTF-8 无 BOM")
            print(f"  候选文件: {q}  {enc}")
    print("EMAIL:", EMAIL or "(未设 NCBI_EMAIL，NCBI 的 courtesy 要求建议设上)")
    print("依赖:", end=" ")
    try:
        from importlib.metadata import version
        print(", ".join(f"{p} {version(p)}" for p in ("mcp", "anyio", "pydantic")))
    except Exception as e:
        print("读版本失败", e)


def main(argv=None):
    """命令行入口。`pubmed-mcp`（pip install 后的 console-script）与 `python pubmed_mcp.py`
    走同一条路：不带参数就当 MCP stdio 服务，带 --selftest/--probe/--concurrent/--check-key 就自检。"""
    args = list(sys.argv[1:] if argv is None else argv)
    # 四个自检命令的 stdout 都带中文与 ⚠️〔U+26A0〕。Windows 上 Python 只在
    # **交互式控制台**用 WriteConsoleW（不受代码页限制）；一旦被重定向/接进管道，
    # 就改用 locale 编码器，gbk/charmap 编不出 ⚠️ 会抛 UnicodeEncodeError、整个命令 rc=1。
    # 〔发布版订正 2026-11-10，本会话复核过〕旧注释写「cp936 与 cp1252 两种都复现 rc=1」，
    # 那对 --check-key 不成立：它的输出里没有 ⚠️，GBK 编得出来，旧写法在 cp936 下 rc=0
    # （只是写出 GBK 字节）；两种码页都炸的是 --probe / --selftest / --concurrent。
    # 另一条坑（验补丁 22 时必踩）：`python -I` 隔离模式会【忽略 PYTHONIOENCODING】，
    # stdout 直接落回 utf-8，于是旧版也不会 rc=1 —— 复现这个 bug 请不要加 -I。
    # 所以 CLI 分支先把 stdout 换成 utf-8 + errors=replace：宁可某个符号显示成 ?，
    # 也不要让回归自检在别人的机器上"跑都没跑完"。
    # MCP stdio 通路（下面的 mcp.run()）不在这里动——mcp SDK 自己包 utf-8 的 TextIOWrapper。
    if args:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass                                        # 老版本没有 reconfigure；认了
    if args and args[0] == "--selftest":
        _selftest(args[1] if len(args) > 1 else "tooth resorption")
    elif args and args[0] == "--probe":
        sys.exit(1 if anyio.run(_probe) else 0)
    elif args and args[0] == "--concurrent":
        anyio.run(_check, args[1] if len(args) > 1 else "tooth resorption")
    elif args and args[0] == "--check-key":
        print("pubmed-mcp:", _tool_version())
        _check_key()
    else:
        if args:
            # 〔实测 2026-11-11〕旧写法把任何不认识的参数都落到 mcp.run()：`--probeX`（拼错）
            # 会在 stdin 关闭后 rc=0、零输出，看起来像「跑过了」。
            print(f"不认识的自检参数：{args[0]!r}。可用：--selftest [词] / --probe / "
                  f"--concurrent [词] / --check-key；不带参数＝启动 MCP stdio 服务。", file=sys.stderr)
            sys.exit(2)
        mcp.run()          # stdio


if __name__ == "__main__":
    main()

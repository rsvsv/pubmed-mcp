"""transport implementation; no core or MCP registration import.
Dependencies are an explicit, per-instance, late-bound contract.
Historical evidence and policy comments are retained from the source.
"""
import functools
import datetime
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import anyio

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

DEPENDENCIES = ('BASE', 'EMAIL', 'NcbiError', 'TOOL', '_AUDIT_CONTEXT', '_KEY', '_KEY_SOURCE', '_NREQ', '_PACER', '_PACER_INIT', '_Pacer', '_REQ_CTR', '_audit_clean', '_decode_key_bytes', '_get', '_mask', '_pace', '_retry_after', '_thread_before_attempt', '_thread_wait', 'api_key')


class Implementation:
    def __init__(self, dependencies):
        self.dependencies = dependencies

    def _decode_key_bytes(self, raw):
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


    def api_key(self):
        """缓存一次读取 —— 旧版每个请求都 stat+读文件。

        找不到 key 不是错误：走匿名档（3 次/秒），功能不缺，只是慢一档；
        `--check-key` 会把当前档位和 key 文件的编码一起说出来。"""
        pass  # Mutable state is supplied by explicit dependency setters.
        if self.dependencies._KEY is None:
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
                        cand = " ".join(self.dependencies._decode_key_bytes(raw).split())
                        if not cand:
                            # 〔实测 2026-11-11〕空文件旧版会 break，把后面有效的 .ncbi_api_key.txt 挡住。
                            continue
                        k, src = cand, f"文件 {q}"
                        break
            self.dependencies._KEY, self.dependencies._KEY_SOURCE = k, src
        return self.dependencies._KEY


    def _key_shape(self, k):
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


    def _mask(self, s):
        """异常文本里不要带出 api_key。

        〔发布版改动 2026-11-10；来源：审计 finding privacy-04，本会话已实测复现前两条〕旧写法有三个洞：
          一、掩码完仍留前 4 位；二、`len(k) > 8` 才启用掩码，短 key 整串原文外泄；
          三、只做字面 str.replace，key 以 URL 百分号编码形态出现在报错文本里时替换不到。
        现在无条件替换成定长掩码，一个字符的原文都不留。第三条仍未处理——先解码再替换属于过度设计，
        记在这儿当已知边界，别当成已修好。"""
        k = self.dependencies.api_key()
        if not (k and isinstance(s, str)):
            return s
        # 旧注释的「第三条仍未处理」已作废：本轮同时替换 URL 编码形式，见离线回归。
        variants = {k, urllib.parse.quote(k, safe=""), urllib.parse.quote_plus(k)}
        # 〔实测 2026-11-13，1.4.4〕key 极短（如 1 个字符 'f'）时全局替换会把整句诊断打花
        # （'failed' → '***ailed'）。短于 8 字符〔本项目自订阈值；NCBI 真 key 为 36 位〕只掩 api_key=<值> 形态。
        short = len(k) < 8
        for value in sorted(variants, key=len, reverse=True):
            encoded = re.escape(value)
            if "%" in value:
                encoded = re.sub(r"%([0-9A-Fa-f]{2})", lambda m: "(?i:%" + m[1] + ")", encoded)
            if short:
                encoded = r"(?<=api_key=)" + encoded + r"(?=[&\s'\"]|$)"
            s = re.sub(encoded, "***", s)
        return s


    def _safe_json(self, text, what):
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
                raise self.dependencies.NcbiError(self.dependencies._mask(f"{what} 收到的不是合法 JSON（{type(e).__name__}）；正文前 300 字：{text[:300]!r}"))
        if not isinstance(d, dict):
            raise self.dependencies.NcbiError(f"{what} 返回的 JSON 顶层不是对象，不能作为成功结果")
        esr = d.get("esearchresult")
        err = d.get("ERROR") or d.get("error")
        for envelope in ("header", "eutilsresult"):
            nested = d.get(envelope)
            if not err and isinstance(nested, dict):
                err = nested.get("ERROR") or nested.get("error")
        if not err and isinstance(esr, dict):
            err = esr.get("ERROR")      # retstart 越限这类错误就藏在这里，而且没有 count
        if err:
            raise self.dependencies.NcbiError(self.dependencies._mask(f"{what} 被 NCBI 拒绝：{str(err)[:400]}"))
        return d


    async def _pace(self):
        pass  # Mutable state is supplied by explicit dependency setters.
        with self.dependencies._PACER_INIT:
            if self.dependencies._PACER is None:
                self.dependencies._PACER = self.dependencies._Pacer(10.0 if self.dependencies.api_key() else 3.0)   # 实测：有 key 10 次/秒，无 3 次/秒
        await self.dependencies._PACER.wait()


    def _retry_after(self, headers):
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


    def _get(self, path, params, timeout, tries=4, *, before_attempt=None, wait=None):
        """带 key 和退避重试的 GET，返回响应文本。失败抛 NcbiError 并带 NCBI 原文。
        同步函数，只允许在 _get_async 的线程里调用。

        _NREQ 按**每次 urlopen** 计数，不是按工具调用次数：一次 429 退避重试算 4 次。
        多线程下 `+=` 不保证精确〔自订口径〕，它只用来回答「这一轮探针花了多少请求」这个量级问题，
        别拿它当计费依据。"""
        pass  # Mutable state is supplied by explicit dependency setters.
        q = dict(params)
        q["tool"] = self.dependencies.TOOL
        if self.dependencies.EMAIL:
            q["email"] = self.dependencies.EMAIL
        key = self.dependencies.api_key()
        if key:
            q["api_key"] = key
        url = f"{self.dependencies.BASE}/{path}?" + urllib.parse.urlencode(q)
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
                req = urllib.request.Request(url, headers={"User-Agent": self.dependencies.TOOL})
                self.dependencies._NREQ += 1
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
                    delay = self.dependencies._retry_after(e.headers)
                    # 本项目自订等待上限：过长 Retry-After 直接失败，不提前重试违反服务端要求。
                    if delay is not None and delay > 60:
                        raise self.dependencies.NcbiError(self.dependencies._mask(f"{path} 失败：{last}；Retry-After={delay:g}s 超过本工具等待上限，请稍后重试"))
                    wait(backoff[min(attempt, 2)] if delay is None else delay)
                    continue
                raise self.dependencies.NcbiError(self.dependencies._mask(f"{path} 失败：{last}（{keyhow}）"))   # 400/414 这种重试没意义
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                if attempt == tries - 1:
                    break
                wait(backoff[min(attempt, 2)])
        raise self.dependencies.NcbiError(self.dependencies._mask(f"{path} 试了 {tries} 次仍失败：{last}（{keyhow}）"))


    def _thread_before_attempt(self):
        anyio.from_thread.check_cancelled()
        anyio.from_thread.run(self.dependencies._pace)
        anyio.from_thread.check_cancelled()


    def _thread_wait(self, delay):
        anyio.from_thread.check_cancelled()
        anyio.from_thread.run(anyio.sleep, delay)
        anyio.from_thread.check_cancelled()


    async def _get_async(self, path, params, timeout, tries=4):
        audit_context = self.dependencies._AUDIT_CONTEXT.get()
        if audit_context is not None and audit_context["enabled"]:
            audit_context["requests"].append({"endpoint": path, "params": self.dependencies._audit_clean(params)})
        # 线程中的 urllib 正在读 socket 时仍受 socket timeout 约束；取消后不再发新重试。
        ctr = self.dependencies._REQ_CTR.get()
        before = self.dependencies._thread_before_attempt
        if ctr is not None:
            def before():
                self.dependencies._thread_before_attempt()
                ctr.append(1)        # 节流与取消检查之后、urlopen 之前：与 _NREQ 同口径（每次尝试算一次）
        return await anyio.to_thread.run_sync(
            functools.partial(self.dependencies._get, path, params, timeout, tries,
                              before_attempt=before, wait=self.dependencies._thread_wait))

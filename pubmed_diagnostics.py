"""diagnostics implementation; no core or MCP registration import.
Dependencies are an explicit, per-instance, late-bound contract.
Historical evidence and policy comments are retained from the source.
"""
import inspect
import json
import os
import re
import time
import anyio

DEPENDENCIES = ('EMAIL', 'FastMCP', 'GOLD_MAX', 'NO_BEGIN', 'NO_END', 'NcbiError', 'PAGE_TERM', 'SORT_REAL', '_EXTRA_FORBID', '_KEY', '_KEY_SOURCE', '_NREQ', '_all_dropped', '_as_bool', '_check_date', '_check_db', '_check_field', '_decode_key_bytes', '_esearch', '_get_async', '_key_shape', '_ln', '_mask', '_note', '_num_of', '_parse_pmids', '_pmids_exist', '_raises_enc', '_row', '_safe_json', '_warn_bits', '_windows', '_zero_explanation', 'api_key', 'check_recall', 'count', 'diff', 'export', 'fetch_abstracts', 'links', 'main', 'mcp', 'mesh', 'search')


class Implementation:
    def __init__(self, dependencies):
        self.dependencies = dependencies

    def _selftest(self, term):
        print("key:", self.dependencies._key_shape(self.dependencies.api_key()))   # 〔发布版改动〕旧写法直接打印前 4 位，与本文件 --check-key 的口径自相矛盾
        t0 = time.time()
        print(anyio.run(self.dependencies.search, term, 3))
        print(f"--- search 耗时 {time.time() - t0:.1f}s ---")
        t0 = time.time()
        print(anyio.run(self.dependencies.count, term))
        print(f"--- count 耗时 {time.time() - t0:.1f}s ---")


    async def _check(self, term):
        """并发跑三个查询，验证事件循环没被占住（旧版会串行阻塞）。"""
        t0 = time.time()
        results = [None, None, None]

        async def one(i, coro):
            results[i] = await coro

        async with anyio.create_task_group() as tg:
            tg.start_soon(one, 0, self.dependencies.search(term, retmax=2))
            tg.start_soon(one, 1, self.dependencies.count(term + "[Title]"))
            tg.start_soon(one, 2, self.dependencies.fetch_abstracts("37427694,33485718"))
        wall = time.time() - t0
        serial = sum(len(str(r)) for r in results)
        print(f"3 个工具并发总耗时 {wall:.1f}s（旧版为串行）；返回总字数 {serial}")
        for i, r in enumerate(results):
            print(f"  [{i}] {str(r)[:80]}…")


    def _ln(self, text, k=1):
        """探针专用：取第 k 行（越界给占位串），永不抛。

        为什么需要：〔实测 2026-11-10 第 5 轮〕`c_yr.splitlines()[1]` 在 c_yr 为空串时 IndexError，
        整轮探针中止、后面的节全没跑 —— attempt() 包住了被测试的调用，包不住描述串。
        探针自己的措辞不是被测对象，它崩了会让「一处没测到」看起来像「全绿」。"""
        ls = (text or "").splitlines()
        return ls[k][:60] if len(ls) > k else "(无输出)"


    def _row(self, text, prefix):
        """探针专用：取第一行以 prefix 开头的；没有就返回空串。

        返回空串而不是哨兵，是为了让依赖它的断言必须显式写 `bool(row) and ...`：
        哨兵串会让 `"不存在的东西" not in row` 这种**否定式断言**白过（探针恒真的第三种形状）。"""
        for x in (text or "").splitlines():
            if x.startswith(prefix):
                return x
        return ""


    def _num_of(self, text):
        """从本工具自己的输出里取「命中 N 条」的 N；取不到算 0。
        探针用它，不用裸 re.search(...).group(1)：调用被 attempt() 兜住时拿到的是空串，
        裸写法会在这一行再抛一次 AttributeError —— 那又变回「崩掉」而不是「记一条 FAIL」。"""
        mm = re.search(r"命中 (\d+)", str(text))
        return int(mm.group(1)) if mm else 0


    def _raises_enc(self, text, codepage):
        """text 在 codepage 上编码会不会抛（用于断言判据本身有效，不碰真 stdout）。"""
        try:
            text.encode(codepage)
            return False
        except UnicodeEncodeError:
            return True


    async def _probe(self):
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

        num = self.dependencies._num_of        # 〔2026-11-10〕原来定义在第 13 节里，前面几处拿不到，只能裸写 re.search

        print("1) 拼错的词必须在「NCBI 实际执行」里现形")
        s = await attempt("s", self.dependencies.search, "anchorage devcie", retmax=1, default="")
        check("querytranslation 已打印", "NCBI 实际执行" in s, s[:200])
        check("未找到的词被点名，不假定丢弃", "phrasesnotfound" in s and "已【丢弃】" not in s, s[:300])

        print("2) 非法 field 必须报错，不能静默退化成全字段")
        try:
            await self.dependencies.search("temporary anchorage device", field="ZZZBOGUSFIELD", retmax=1)
            check("field 白名单", False, "没抛异常")
        except self.dependencies.NcbiError as e:
            check("field 白名单", "白名单" in str(e), str(e)[:120])

        print("2b) sort 不能静默失效，也不能把真生效的值当乱写的拦掉"
              "（实测：pub_date / relevance / author 会换顺序，其余与不传时同序）")
        try:
            await self.dependencies.search("temporary anchorage device", retmax=1, sort="journal_quality")
            check("sort 白名单", False, "没抛异常")
        except self.dependencies.NcbiError as e:
            check("sort 白名单", "静默接受" in str(e), str(e)[:120])
        s_ok = await attempt("s_ok", self.dependencies.search, "temporary anchorage device", retmax=1, sort="pub_date", default="")
        check("合法 sort 仍能走", "查询：" in s_ok, s_ok[:80])

        print("3) field=MeSH Terms 拿不到 count 时必须抛错，不许印 '?'")
        try:
            await self.dependencies.count("temporary anchorage device", field="MeSH Terms")
            check("count 缺失守卫", False, "没抛异常")
        except self.dependencies.NcbiError as e:
            check("count 缺失守卫", "count" in str(e), str(e)[:160])

        print("4) 空 term 在 count 里也要拦住（旧版只有 search 拦了）")
        try:
            await self.dependencies.count("  ")
            check("空 term 守卫", False, "没抛异常")
        except self.dependencies.NcbiError as e:
            check("空 term 守卫", "不能为空" in str(e), str(e)[:120])

        print("5) 不存在的 PMID：报「要 N / 实取 M」并点名缺的，不再拿请求数冒充结果数")
        f = await attempt("f", self.dependencies.fetch_abstracts, "37427694,9999999999", default="")
        check("诚实计数", "要 2 条 / 实取 1 条" in f, f[:120])
        check("缺失被点名", "9999999999" in f and "⚠️" in f, f[:200])
        f2 = await attempt("f2", self.dependencies.fetch_abstracts, "37427694,37427694, 33485718", default="")
        check("去重不谎报", "实取 2 条" in f2 and "去掉重复" in f2, f2[:160])

        print("6) 大批量 esummary 不再撞 414（旧版 ~350 个 id 拼一个 GET 必挂）")
        t0 = time.time()
        s = await attempt("s", self.dependencies.search, "miniscrew*", retmax=400, default="")
        n_rec = len(re.findall(r"^PMID ", s, re.M))
        check("retmax=400 出得来", n_rec >= 350, f"只有 {n_rec} 条 / {time.time() - t0:.1f}s：{s[:120]}")

        print("7) 开放获取标识出现在列表里（省一次 fetch 请求）")
        check("pmc 已输出", "pmc:PMC" in s, "400 条里没有一条 pmc:，可疑")
        check("卷页已输出", re.search(r"\d+\(\d+\)|\d+:e?\d", s) is not None, s[:200])

        print("8) PMID 输入容错：PMC 号不能冒充 PMID")
        try:
            await self.dependencies.fetch_abstracts("PMC10461258")
            check("拒收 PMC 号", False, "没抛异常")
        except self.dependencies.NcbiError as e:
            check("拒收 PMC 号", "PMC" in str(e), str(e)[:120])

        print("9) elink：多 id 必须各归各，不合并")
        l = await attempt("l", self.dependencies.links, "37427694,33485718", default="")
        la = next((x for x in l.split("\n") if x.startswith("PMID 37427694")), "")
        lb = next((x for x in l.split("\n") if x.startswith("PMID 33485718")), "")
        check("有全文的那条认得出", "PMC10461258" in la, l[:200])
        check("归属不串", "PMC10461258" not in lb, lb[:120])
        check("无对应的明说", "无直接对应记录" in lb, lb[:120])

        print("10) 顶层 ERROR 不能被当成「NCBI 里没有」")
        try:
            d = self.dependencies._safe_json(await self.dependencies._get_async("elink.fcgi", {"dbfrom": "pubmed", "db": "pmc", "id": "37427694",
                                                            "retmode": "json", "cmd": "links"}, 25), "elink")
            check("ERROR 守卫", False, "没抛异常，返回：" + json.dumps(d)[:120])
        except self.dependencies.NcbiError as e:
            check("ERROR 守卫", "not supported" in str(e), str(e)[:160])

        print("11) 中文查询：0 条必须被说成「NCBI 没解析出来」，不能当成「主题没文献」")
        c = await attempt("c", self.dependencies.count, "牙根吸收", default="")
        check("空解析被点名", "未返回实际执行式" in c, c[:160])
        check("给了英文对照提示", "对应英文词重查" in c, c[:160])
        check("不再印空的「NCBI 实际执行」", "NCBI 实际执行" not in c, c[:160])
        s0 = await attempt("s0", self.dependencies.search, "种植钉", retmax=1, default="")
        check("search 的 0 条也跟着改口", "不能作为" in s0, s0[:200])

        print("12) 本轮实测复现的 6 类（单边日期被丢、假「确实没匹配」、to 大小写、ExceptionGroup、"
              "PMID 粘贴形状、ta 别名）")
        c_all = await attempt("c_all", self.dependencies.count, "temporary anchorage device", default="")
        c_lo = await attempt("c_lo", self.dependencies.count, "temporary anchorage device", mindate="2020-01-01", default="")
        n_all = num(c_all)
        n_lo = num(c_lo)
        check("只给一端时日期真的生效", n_lo < n_all, f"只给 mindate 仍是 {n_lo} == 不限时的 {n_all}")
        check("补了哪一端在输出里写明", "日期条件" in c_lo and "由本工具补" in c_lo and "⚠️" in c_lo, c_lo[:240])
        n_rel = num(await attempt("c_rel600", self.dependencies.count, "temporary anchorage device", reldate=600))
        check("reldate 单独给也生效", n_rel < n_all, f"reldate=600 得到 {n_rel} == 不限时的 {n_all}")
        try:
            await self.dependencies.count("temporary anchorage device", mindate="2020年1月")
            check("乱写的日期被拦", False, "没抛异常")
        except self.dependencies.NcbiError as e:
            check("乱写的日期被拦", "不是 NCBI 认的日期写法" in str(e), str(e)[:100])
        try:
            await self.dependencies.count("temporary anchorage device", mindate="2021-01-01", maxdate="2020-01-01")
            check("区间给反了被拦", False, "没抛异常")
        except self.dependencies.NcbiError as e:
            check("区间给反了被拦", "空的" in str(e), str(e)[:100])
        s_drop = await attempt("s_drop", self.dependencies.search, "tooth resorption", retmax=3, field="journal", default="")
        check("词全被丢弃时不再断言「确实没有匹配」", "确实没有匹配这篇" not in s_drop
              and "主题没有文献" in s_drop, s_drop[:260])
        # (a) retstart 超出 NCBI 的检索窗口 → 本工具提前拦，不发请求
        #     〔实测 2026-11-10：这条以前是直接 await，NcbiError 一抛整份探针就断在这儿〕
        try:
            await self.dependencies.search("temporary anchorage device", retmax=20, retstart=100000)
            check("retstart 超窗口被提前拦下", False, "没抛异常")
        except self.dependencies.NcbiError as e:
            check("retstart 超窗口被提前拦下", "9998" in str(e), str(e)[:120])
        # (b) retstart 在窗口内但越过命中总数 → NCBI 正常回 0 条，必须说成「翻页越界」而不是「没有匹配」
        #     计数现场取，不写死（写死的数字会随 PubMed 更新失效，那是假断言）
        #     〔实测 2026-11-10："micro-implant immediate loading" 当时 8 条，retstart=9 得到
        #      「本次 0 条只是因为 retstart=9 越过了命中总数 8 条」〕
        n_page = self.dependencies._num_of(await attempt("count(PAGE_TERM)", self.dependencies.count, self.dependencies.PAGE_TERM))
        s_page = await attempt("s_page", self.dependencies.search, self.dependencies.PAGE_TERM, retmax=5, retstart=n_page + 1)
        check("翻页越过命中总数不说成「没有匹配」",
              n_page > 0 and "越过了命中总数" in s_page and "确实没有匹配" not in s_page,
              f"count={n_page}；{s_page[-200:]}")
        # retmax 的两处静默改动（实测 2026-11-10 修）
        s_big = await attempt("s_big", self.dependencies.search, "miniscrew", retmax=5000, default="")
        check("retmax 超上限要说出是【本工具】夹的，不许看着像 NCBI 只给了这么多",
              "本工具上限" in s_big and "retmax=5000" in s_big, s_big[-320:])
        check("夹完确实只给了 1000 条以内（反向：说明不是空话）",
              "本次给 1000 条" in s_big, s_big.splitlines()[1][:60] if s_big else "")
        try:
            await self.dependencies.search("miniscrew", retmax=-3)
            check("retmax 负数必须报错，不许静默变成 1 条", False, "没抛异常")
        except self.dependencies.NcbiError as e:
            check("retmax 负数必须报错，不许静默变成 1 条", "负数" in str(e), str(e)[:120])
        s_zero = await attempt("s_zero", self.dependencies.search, "miniscrew", retmax=0, default="")
        check("retmax=0 是「只要计数」，不该被静默改成 1 条",
              "本次给 0 条" in s_zero and "命中" in s_zero, s_zero[:160])
        check("retmax=0 的 0 条不许说成「确实没有匹配这篇」",
              "retmax=0" in s_zero and "确实没有匹配这篇" not in s_zero, s_zero[-260:])
        j_zero = await attempt_json("s_zero_json", self.dependencies.search, "miniscrew", retmax=0, as_json=True)
        _zn = str(j_zero.get("zero_note") or "")
        check("as_json 的 zero_note 同样不许说「确实没有匹配」",
              "retmax=0" in _zn and "确实没有匹配" not in _zn, _zn[:260])
        # 〔实测 2026-11-11〕这条要核对的是「回显没被改写」，光看 count>0 不够：descriptor 不加引号
        # 写错时 NCBI 照样回一个正数，只是把条件换成了别的（见上面那条离线断言的实测记录）。
        # 回显与输入相同时文本输出不印「NCBI 实际执行」，所以这里走 as_json 读 querytranslation。
        _mesh_term = '"Orthodontic Anchorage Procedures"[MeSH Terms]'
        j_mesh = await attempt_json("c_mesh", self.dependencies.search, _mesh_term, retmax=0, as_json=True)
        _mesh_qt = str(j_mesh.get("querytranslation") or "")
        check("说明里推荐的 MeSH 示例真能查到东西，且回显没有被改写",
              int(j_mesh.get("count") or 0) > 0 and _mesh_qt.strip().lower() == _mesh_term.lower(),
              f"count={j_mesh.get('count')} qt={_mesh_qt[:120]}")
        c_tag = await attempt("c_tag", self.dependencies.count, "resorption[Title]", default="")
        check("文档里的字段标签示例 resorption[Title] 真能查到东西", self.dependencies._num_of(c_tag) > 0, c_tag[:160])
        _sd0 = getattr(self.dependencies.search, "__doc__", "") or ""
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
              self.dependencies._as_bool("True", "as_json") is True and self.dependencies._as_bool("off", "as_json") is False, "")
        l_up = await attempt("l_up", self.dependencies.links, "37427694", to="PMC", default="")
        check("to 大小写不再造出假阴性", "PMC10461258" in l_up, l_up[:160])
        try:
            await self.dependencies.links("37427694", to="pmcid")
            check("非法库名给人话报错", False, "没抛异常")
        except self.dependencies.NcbiError as e:
            check("非法库名给人话报错", "不是本工具支持的库" in str(e), str(e)[:120])
        except BaseException as e:          # 被包成 ExceptionGroup 就是要防的那种
            check("非法库名给人话报错", False, f"{type(e).__name__}: {str(e)[:80]}")
        l_bad = await attempt("l_bad", self.dependencies.links, "9999999999", default="")
        check("无效号明确报告元数据不可用，不推断无全文", "cannot get document summary" in l_bad, l_bad[:300])
        check("收得下 PMID 12345 这种粘贴", self.dependencies._parse_pmids("PMID 12345")[0] == ["12345"],
              str(self.dependencies._parse_pmids("PMID 12345")))
        check("收得下 pmid: 前缀", self.dependencies._parse_pmids("pmid: 37427694, 33485718")[0] == ["37427694", "33485718"], "")
        check("补零号规范成裸号", self.dependencies._parse_pmids("000037427694")[0] == ["37427694"], "")
        f_pad = await attempt("f_pad", self.dependencies.fetch_abstracts, "000037427694", default="")
        check("补零号不再「实取 1 条」与「没返回」同屏", "实取 1 条" in f_pad and "没有返回" not in f_pad,
              f_pad[:180])
        check("ta 按 PubMed 自己的含义映射成 Journal", self.dependencies._check_field("ta") == "Journal", self.dependencies._check_field("ta"))
        check("tiab 仍是 Title/Abstract", self.dependencies._check_field("tiab") == "Title/Abstract", self.dependencies._check_field("tiab"))

        print("13) 第三轮审查实测到的 9 类（reldate 隐身、空交集假 0、不存在的日历日、年级粒度、"
              "field 被吞、term 内标签压过 field、引号内丢词、9999 窗口、elink 判据反了）")
        T = "temporary anchorage device"
        n_lo2 = num(await attempt("c_lo2", self.dependencies.count, T, mindate="2020-01-01"))
        # 〔2026-11-09 改写〕原来这两条断言抓不到任何事：`n_mix < n_lo2` 是嵌套窗口必然成立的恒真式，
        # 而本轮的真 bug 恰恰是「reldate 走两条路径给出两个数」——本机日历换算把边界日切掉，
        # dental implant 从 770 变 763（少的 7 条＝2026-09-09 那一天）。所以判据换成
        # 「加一个不可能生效的宽区间，数必须逐字相同」。
        n_rel_only = num(await attempt("n_rel_only", self.dependencies.count, T, reldate=60))
        n_rel_wide = num(await attempt("n_rel_wide", self.dependencies.count, T, reldate=60, mindate=self.dependencies.NO_BEGIN, maxdate=self.dependencies.NO_END))
        check("reldate 加一条不该改变结果的宽区间，命中数必须一模一样",
              n_rel_only == n_rel_wide, f"单给 {n_rel_only} vs 配宽区间 {n_rel_wide}（旧版这里差 7 条）")
        c_mix = await attempt("c_mix", self.dependencies.count, T, mindate="2020-01-01", reldate=60, default="")
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
        c_senti = await attempt("c_senti", self.dependencies.count, T, reldate=60, mindate=self.dependencies.NO_BEGIN, maxdate=self.dependencies.NO_END, default="")
        check("哨兵上界 2999-12-31 出现在输出里（本机日历算不出这个数）",
              "2999-12-31" in c_senti and "0001-01-01" in c_senti, c_senti[:260])
        d_dis = await attempt("d_dis", self.dependencies.count, T, mindate="2000-01-01", maxdate="2024-12-31", reldate=60, default="")
        check("两段窗口不相交时点明（这种情况 0 条不代表主题没文献）",
              "不相交" in d_dis, d_dis[:300])
        s_dis = await attempt("s_dis", self.dependencies.search, T, mindate="2000-01-01", maxdate="2024-12-31", reldate=60, retmax=5, default="")
        check("不相交的 0 条不能说成「确实没有匹配这篇」",
              "不相交" in s_dis and "确实没有匹配这篇" not in s_dis, s_dis[-260:])
        d_rel = await attempt("d_rel", self.dependencies._esearch, T, retmax=0, reldate=365, default={})
        check("reldate 单给也按发表日期口径", "[Date - Publication]" in (d_rel.get("querytranslation") or ""),
              (d_rel.get("querytranslation") or "")[-70:])
        for bad_d in ("2023-02-29", "2020-13-01", "2020-02-30", "2020-00-00"):
            try:
                await self.dependencies.count(T, mindate=bad_d)
                check(f"日历上不存在的 {bad_d} 被拦", False, "没抛异常（NCBI 对这种值静默给 0 条）")
            except self.dependencies.NcbiError as e:
                check(f"日历上不存在的 {bad_d} 被拦", "日历上没有这一天" in str(e), str(e)[:90])
        c_leap = await attempt("c_leap", self.dependencies.count, T, mindate="2020-02-29", default="")
        check("真闰日不误伤", num(c_leap) > 0, c_leap[:120])
        c_yr = await attempt("c_yr", self.dependencies.count, T, mindate="2021-01-01", maxdate="2021", default="")
        c_yr2 = await attempt("c_yr2", self.dependencies.count, T, mindate="2021-01-01", maxdate="2021-12-31", default="")
        # 〔2026-11-09 改写〕旧断言只查「输出里有『命中』两个字」——count() 每一次成功都有这两个字，
        # 恒真。现在比的是「NCBI 把 2021 展开成 2021-12-31」这件事本身：两种写法必须同数。
        # 〔2026-11-10 第 5 轮〕这一条刚抓到过一次真回归：倒挂守卫逐字比字符串，
        # 把合法的 (2021-01-01, 2021) 拦成倒挂 → c_yr 抛错、num("")==0 与 c_yr2 的真数不等 = FAIL。
        # 所以断言拆两条：先确认【没被拦】（旧写法只比数值，被拦时 num 都是 0 会假过），再比同数。
        check("年级 maxdate 配完整 mindate 不许被误判成倒挂（c_yr 必须有命中行）",
              "命中" in c_yr, self.dependencies._ln(c_yr, 0) + " | " + self.dependencies._ln(c_yr2, 0))
        check("年级 maxdate 与写成 12-31 的必须同数",
              "命中" in c_yr and "命中" in c_yr2 and num(c_yr) == num(c_yr2),
              f"{self.dependencies._ln(c_yr)} vs {self.dependencies._ln(c_yr2)}")
        try:
            _ = await self.dependencies.count(T, mindate="3000-01-01")
            check("真倒挂（mindate 在 NO_END 之后）仍然要抛，不许被守卫放宽成静默 0 条",
                  False, "没抛异常")
        except self.dependencies.NcbiError as e:
            check("真倒挂（mindate 在 NO_END 之后）仍然要抛，不许被守卫放宽成静默 0 条",
                  "倒挂" in str(e), str(e)[:120])
        for sw in ("keywords", "pmcid", "issn"):
            try:
                await self.dependencies.count("miniscrew", field=sw)
                check(f"field={sw}（实测被 NCBI 整条吞掉）不再静默给不限定的数", False, "没抛异常")
            except self.dependencies.NcbiError as e:
                check(f"field={sw}（实测被 NCBI 整条吞掉）不再静默给不限定的数", "整条忽略" in str(e), str(e)[:90])
        c_tag = await attempt("c_tag", self.dependencies.count, "miniscrew[Title]", field="journal", default="")
        check("term 里写了概念级标签时点破 field 被顶掉", "term 内的标签赢" in c_tag, c_tag[:300])
        # 〔实测 2026-11-09〕反向案例：term 里只有 [dp] 这种日期标签时 field 照样生效，
        # 旧写法却因为「term 里有方括号」宣布 field 被丢，还给出一条会把查询改坏的建议。
        c_dptag = await attempt("c_dptag", self.dependencies.count, "tooth resorption AND 2020:2026[dp]", field="Title", default="")
        check("term 里只有日期标签时不能说 field 被丢", "这个限定生效了" in c_dptag
              and "term 内的标签赢" not in c_dptag, c_dptag[:320])
        # 〔实测 2026-11-10〕NCBI 的 warninglist 这一支以前全文没读过：这两条 0 查询
        #   的 _note 是空的，结尾却印「PubMed 里确实没有匹配这篇」——那是把 NCBI 明说的
        #   「短语不存在 / 词全被当停用词」写成「主题没文献」。
        s_stop = await attempt("s_stop", self.dependencies.search, "the and of", retmax=1, default="")
        check("词全被当停用词时，不许说「确实没有匹配这篇」",
              "停用词" in s_stop and "确实没有匹配这篇" not in s_stop, s_stop[-320:])
        check("停用词那条要给对建议（换实词，不是换字段）",
              "换成语义实词" in s_stop and "换成有索引的字段" not in s_stop, s_stop[-260:])
        # 〔实测 2026-11-10〕引号短语不存在但 NCBI 换成按词 AND、仍有命中：这一支必须说清
        #   「命中数是替代式的」，不能只说「短语找不到」就完事。
        s_pf = await attempt("s_pf", self.dependencies.search, self.dependencies.PAGE_TERM, retmax=3, default="")
        check("短语被换成按词 AND 且有命中时，要说清那个数不是短语的数",
              self.dependencies._num_of(s_pf) > 0 and "按词 AND" in s_pf and "不是" in s_pf, s_pf[-300:])
        s_qp = await attempt("s_qp", self.dependencies.search, '"zzzqqxnonexistentterm"', retmax=1, default="")
        check("输出里不许抄旧错句当历史说明（第 7 轮就是这里自己踩了自己的断言）",
              "确实没有匹配这篇" not in s_pf and "确实没有匹配这篇" not in s_qp,
              (s_pf[-120:] + " || " + s_qp[-120:])[:240])
        check("整个引号短语查不到时要说出来，且不说成主题没文献",
              "整个引号短语" in s_qp and "确实没有匹配这篇" not in s_qp, s_qp[-320:])
        c_ig = await attempt("c_ig", self.dependencies.count, "the and of", default="")
        check("count() 也不能漏掉 warninglist（它和 search 走同一套说明）",
              "停用词" in c_ig, c_ig[:260])
        for messy in ("1,234,567", "PMID 1,234,567", "12,345"):
            try:
                await self.dependencies.fetch_abstracts(messy)
                check(f"带千位分隔符的 {messy!r} 必须拦住，不许切成三个真号", False, "没抛异常")
            except self.dependencies.NcbiError as e:
                check(f"带千位分隔符的 {messy!r} 必须拦住，不许切成三个真号",
                      "千位分隔符" in str(e), str(e)[:110])
        check("普通逗号列表仍然是多个号（反向：新拦法没把正常用法杀掉）",
              self.dependencies._parse_pmids("12345,678")[0] == ["12345", "678"] and
              self.dependencies._parse_pmids("12345;678")[0] == ["12345", "678"], str(self.dependencies._parse_pmids("12345,678")))
        c_q = await attempt("c_q", self.dependencies.count, '"xyzzy tooth resorption"', default="")
        ql = [l for l in c_q.splitlines() if "引号短语里有词" in l]
        # 〔2026-11-09 改写〕旧断言写成 `"xyzzy" in c_q`，而查询式自己就被逐字回显在第一行，恒真。
        # 现在只盯那行 ⚠️ 本身。
        check("引号内丢词必须点名（NCBI 不填 phrasesnotfound）", bool(ql) and "xyzzy" in ql[0], c_q[:300])
        c_q2 = await attempt("c_q2", self.dependencies.count, '"cone beam computed tomography"', default="")
        check("正常引号短语不误报", "引号短语里有词" not in c_q2, c_q2[:200])
        s_cap = await attempt("s_cap", self.dependencies.search, "orthodontics", retmax=10, retstart=9990, default="")
        check("贴到 9999 检索窗口上限时说明白", "9999" in s_cap and "永远翻不到" in s_cap, s_cap[:300])
        j_cap = await attempt_json("j_cap", self.dependencies.search, "orthodontics", retmax=10, retstart=9990, as_json=True)
        # 〔实测 2026-11-09〕旧版的 as_json 把这句整个丢了，而 docstring 又叫脚本用 as_json：
        # 最需要「这批没取全」的人恰好拿不到它。
        check("as_json 也必须带上窗口到顶的警告", "永远翻不到" in (j_cap.get("window_warning") or "")
              + " ".join(j_cap.get("warnings") or []), str(j_cap.get("window_warning"))[:200])
        for rs in (9999, 10000):
            try:
                await self.dependencies.search("orthodontics", retmax=5, retstart=rs)
                check(f"retstart={rs} 越限要给人话而不是 NCBI 原文 blob", False, "没抛异常")
            except self.dependencies.NcbiError as e:
                check(f"retstart={rs} 越限要给人话而不是 NCBI 原文 blob",
                      "9998" in str(e) and "不是合法 JSON" not in str(e), str(e)[:160])
        s_edge = await attempt("s_edge", self.dependencies.search, "orthodontics", retmax=2, retstart=9998, default="")
        check("retstart=9998 是合法边界、不该被自己拦死", "命中" in s_edge, s_edge[:120])
        l_real = await attempt("l_real", self.dependencies.links, "42760028", default="")
        row_real = self.dependencies._row(l_real, "PMID ")
        check("links 对真号至少印出一行 PMID 数据（否则下面两条否定式断言无从判起）",
              bool(row_real), (row_real or l_real)[:200])
        # 只查这一行：结尾那段图例本来就要解释「无对应记录」与「号不存在」是两件事，
        # 拿整屏做 not in 会把说明文字当成失效（第一版就是这么写错的，看着像 bug 其实是断言太糙）。
        check("真号没有 PMC 链接时不能说成「号不存在」",
              bool(row_real) and "里没有这个号" not in row_real, row_real[:220])
        check("这种情况写成「库无直接对应记录」",
              bool(row_real) and "无直接对应记录" in row_real, row_real[:220])
        l_to = await attempt("l_to", self.dependencies.links, "37427694", to="pubmed", default="")
        row_to = self.dependencies._row(l_to, "PMID ")
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
                await self.dependencies.count(T, reldate=bad_rel)
                check(f"reldate={bad_rel} 必须报错", False, "没抛异常（旧版：负数被夹成 0＝不限日期；大数抛裸 OverflowError）")
            except self.dependencies.NcbiError as e:
                # 〔实测 2026-11-11〕旧写法 check(..., True)：只要抛的是 NcbiError 就算过，网络故障
                # 也照样 PASS。改成核对报错文本是不是本工具的前置校验。
                _want = "负数" if bad_rel < 0 else "太大"
                check(f"reldate={bad_rel} 必须由本工具的前置校验拦下", _want in str(e), str(e)[:120])
            except Exception as e:
                check(f"reldate={bad_rel} 必须报错", False, f"{type(e).__name__}（不是 NcbiError）：{str(e)[:90]}")
        c_old = await attempt("c_old", self.dependencies.count, "tooth", maxdate="1800-01-01", default="")
        check("早于 1809 的区间不能报 0（旧版补的下界 1809-01-01 会把这段整个切掉）",
              num(c_old) > 0 and "0001-01-01" in c_old, c_old[:300])
        s_old = await attempt("s_old", self.dependencies.search, "tooth", maxdate="1800-01-01", retmax=3, default="")
        check("这段真文献不能说成「确实没有匹配这篇」", "确实没有匹配这篇" not in s_old, s_old[-240:])
        s_drop = await attempt("s_drop", self.dependencies.search, "zzzqqx AND qbvcjk", retmax=5, default="")
        check("全词被丢＋布尔算子时不许下「确实没有匹配」的结论",
              "全部被报告" in s_drop and "确实没有匹配这篇" not in s_drop, s_drop[-300:])
        s_keep = await attempt("s_keep", self.dependencies.search, "tooth resorption", retmax=1, default="")
        check("有词的查询仍正常（反向：上面那条不是无条件生效）", "命中" in s_keep, s_keep[:120])
        j_norm = await attempt_json("j_norm", self.dependencies.search, "miniscrew", retmax=2, as_json=True)
        # 〔已作废 2026-11-10〕原断言只查「键在不在 dict 里」，而这两个键是无条件写的 —— 永远为真；
        #  用的查询（miniscrew, retmax=2）又根本没有日期条件，值为空也算过。
        #  换成【带日期条件的查询 + 值必须非空】，这样日期逻辑一坏它就会红。
        j_dated = await attempt_json("j_dated", self.dependencies.search, T, retmax=2, as_json=True, reldate=60)
        check("as_json 在真带日期条件时给出非空的窗口与 daterange",
              bool(j_dated.get("executed_windows")) and bool((j_dated.get("daterange") or "").strip()),
              str({k: j_dated.get(k) for k in ("executed_windows", "daterange")})[:240])
        j_norm = await attempt_json("j_norm", self.dependencies.search, "miniscrew", retmax=2, as_json=True)
        check("没有日期条件时 executed_windows 必须真的是空列表（不是漏填）",
              j_norm.get("executed_windows") == [], str(j_norm.get("executed_windows"))[:120])
        s_bm = await attempt("s_bm", self.dependencies.search, T, retmax=2, sort="best match")
        check("sort=best match 被改写成 relevance 这件事必须说出来",
              "改写" in s_bm and "relevance" in s_bm, s_bm[:300])
        check("sort=author 现在被接受（实测它真会换顺序）", "author" in self.dependencies.SORT_REAL, "")
        ids_plain = (await attempt("ids_plain", self.dependencies._esearch, T, retmax=5, default={})).get("idlist") or []
        ids_author = (await attempt("ids_author", self.dependencies._esearch, T, retmax=5, sort="author", default={})).get("idlist") or []
        check("sort=author 的 idlist 与不传 sort 不同（反向：白名单不是白加）",
              bool(ids_plain) and bool(ids_author) and ids_plain != ids_author,
              f"默认 {ids_plain[:3]} vs author {ids_author[:3]}")
        ids_journal_req = (await attempt("ids_pdat", self.dependencies._esearch, T, retmax=5, sort="pub_date",
                                         default={})).get("idlist") or []
        check("三种真实顺序两两不同（pub_date / author / 默认）",
              len({tuple(ids_plain), tuple(ids_author), tuple(ids_journal_req)}) == 3,
              f"{ids_plain[:2]} | {ids_author[:2]} | {ids_journal_req[:2]}")
        f_mis = await attempt("f_mis", self.dependencies.fetch_abstracts, "37427694,9999999999", default="")
        check("缺条目不再被解释成「这篇没摘要」", "没有摘要的社论照样返回记录" in f_mis
              and "9999999999" in f_mis, f_mis[:260])

        print("15) 日期窗口解析（全离线；形状用 chr(47) 拼，因为工具渲染会把斜杠显示成横杠）")
        SL = chr(47)
        q_one = f"2026{SL}11{SL}08[Date - Publication]"
        q_rng = f"2020{SL}01{SL}01:2024{SL}12{SL}31[Date - Publication]"
        q_reld = f"2026{SL}09{SL}11:2026{SL}11{SL}10[Date - Publication]"
        q_edat = f"2020{SL}01{SL}01:2020{SL}12{SL}31[Date - Entry]"
        q_2019 = f"2019{SL}01{SL}01:2019{SL}12{SL}31[Date - Publication]"

        w_rng = self.dependencies._windows(q_rng)
        check("斜杠形状解析得出窗口（NCBI 回显的真实形状）",
              w_rng == [("2020-01-01", "2024-12-31", "发表日期[pdat]")], str(w_rng))
        check("横杠形状也认（容错，别只认一种）",
              self.dependencies._windows("2020-01-01:2024-12-31[Date - Publication]") == w_rng,
              str(self.dependencies._windows("2020-01-01:2024-12-31[Date - Publication]")))
        w_one = self.dependencies._windows(q_one)
        check("单日窗口（回显没有冒号后半截）解析成起＝止",
              w_one == [("2026-11-08", "2026-11-08", "发表日期[pdat]")], str(w_one))
        check("窗口带回日期字段名（edat 与 pdat 分得开）",
              [x[2] for x in self.dependencies._windows(f"a AND {q_edat} AND {q_reld}")] == ["入库日期[edat]", "发表日期[pdat]"],
              str(self.dependencies._windows(f"a AND {q_edat} AND {q_reld}")))
        check("无日期的查询不产窗口", self.dependencies._windows('"miniscrew"[Title]') == [], str(self.dependencies._windows('"miniscrew"[Title]')))
        check("qt 为空/None 不炸", self.dependencies._windows(None) == [] and self.dependencies._windows("") == [], "")

        def _dd(qt, rel=0, dr=""):
            return {"querytranslation": qt, "_term": "t", "_rel": rel, "_daterange": dr,
                    "_windows": self.dependencies._windows(qt)}

        def _has_disjoint(d):
            return any("不相交" in x for x in self.dependencies._note(d))

        check("跨字段两段（入库日 vs 发表日）不许说「不相交」",
              not _has_disjoint(_dd(f"a AND {q_edat} AND {q_reld}", 60, "两段 AND")),
              str(self.dependencies._note(_dd(f"a AND {q_edat} AND {q_reld}", 60, "两段 AND")))[:400])
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
              "不相交" in self.dependencies._zero_explanation(_d_dis, 0) and "确实没有匹配这篇" not in self.dependencies._zero_explanation(_d_dis, 0),
              self.dependencies._zero_explanation(_d_dis, 0)[:200])
        _d_dis_hit = dict(_d_dis, count="1")
        _nt_hit = "\n".join(self.dependencies._note(_d_dis_hit))
        check("同字段不相交但命中>0：不许说「也会是 0 条」，也不许替 0 条下结论",
              "也会是 0 条" not in _nt_hit and "不相交不等于 0 条" in _nt_hit, _nt_hit[:300])
        _d_rm0 = dict(_dd("miniscrew"), _retmax=0)
        _z_rm0 = self.dependencies._zero_explanation(_d_rm0, 313644)
        check("retmax=0 的 0 条解释说清是「只要计数」，不说「确实没有匹配」",
              "retmax=0" in _z_rm0 and "确实没有匹配" not in _z_rm0, _z_rm0[:200])
        _d_page = dict(_dd("x"), retmax="0", retstart="9", _retmax=5)
        _z_page = self.dependencies._zero_explanation(_d_page, 8)
        check("越页时 NCBI 回显 retmax=0，解释必须是「翻页越界」，不是「只要计数」",
              "越过了命中总数" in _z_page and "retmax=0" not in _z_page, _z_page[:200])

        print("\n16) warninglist 解析（全离线：自己造 NCBI 的返回形状，不发请求）")
        fake = {"_term": "the and of", "querytranslation": "the and of", "count": "0",
                "warninglist": {"phrasesignored": ["the", "and", "of"],
                                "quotedphrasesnotfound": [], "outputmessages": ["No items found."]}}
        ig2, qn2, ot2 = self.dependencies._warn_bits(fake)
        check("phrasesignored 被取出来", ig2 == ["the", "and", "of"], str(ig2))
        check("outputmessages 里那句 0 条回声不重复印", ot2 == [], str(ot2))
        check("全停用词 → _all_dropped 判 True（旧版这里判 False，于是跟着印假话）",
              self.dependencies._all_dropped(fake) is True, "")
        check("全停用词的 0 条解释不说「确实没有匹配这篇」",
              "确实没有匹配这篇" not in self.dependencies._zero_explanation(fake, 0), self.dependencies._zero_explanation(fake, 0)[:150])
        fake2 = {"_term": '"zzz qq"', "querytranslation": '"zzz qq"', "count": "0",
                 "warninglist": {"phrasesignored": [], "outputmessages": ["No items found."],
                                 "quotedphrasesnotfound": ['"zzz qq"']}}
        check("quotedphrasesnotfound 走自己那一支，不说成停用词",
              "整个引号短语" in self.dependencies._zero_explanation(fake2, 0), self.dependencies._zero_explanation(fake2, 0)[:150])
        check("warninglist 缺失/不是 dict 时不炸", self.dependencies._warn_bits({"nope": 1}) == ([], [], []), "")
        check("_warn_bits 对 warninglist 是字符串的怪形状也不炸", self.dependencies._warn_bits({"warninglist": "x"}) == ([], [], []), "")
        # 反向：有 warninglist 但【不是】全丢的情形，不许误报成「条件为空」
        fake3 = {"_term": "the miniscrew", "querytranslation": "miniscrew", "count": "700",
                 "warninglist": {"phrasesignored": ["the"], "quotedphrasesnotfound": [],
                                 "outputmessages": []}}
        check("只丢了一个虚词、还有实词时不许说「生效条件为空」",
              self.dependencies._all_dropped(fake3) is False, str(self.dependencies._note(fake3))[:200])

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
            _got = self.dependencies._decode_key_bytes(open(_q, "rb").read())
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
              "非字母数字" in self.dependencies._key_shape("my key is abc") and "my key" not in self.dependencies._key_shape("my key is abc"),
              self.dependencies._key_shape("my key is abc"))
        # 长度不是判据（本机那串就不是 32 位〔实测〕，所以 32 与非 32 都不许被说成可疑）
        check("32 位 hex 只被描述、不被夸成「形状正常」",
              "形状正常" not in self.dependencies._key_shape("a1" * 16) and "纯十六进制" in self.dependencies._key_shape("a1" * 16),
              self.dependencies._key_shape("a1" * 16))
        check("长度 36 的 hex 不许被报成可疑（作者本机 key 正是这种非 32 位的形状）",
              "可疑" not in self.dependencies._key_shape("a1" * 18) and "混进了别的文字" not in self.dependencies._key_shape("a1" * 18),
              self.dependencies._key_shape("a1" * 18))
        check("判据里没有「32 位」这种没核实过的口径", "32 位" not in self.dependencies._key_shape("a1" * 18),
              self.dependencies._key_shape("a1" * 18))
        check("_key_shape 空值直接说走匿名档", "匿名" in self.dependencies._key_shape(""), self.dependencies._key_shape(""))
        # 分组号按补丁号命名：17-19 是纯代码修复、没新增断言，所以编号从 16) 跳到 20)。
        # 〔发布版新增三条，全离线不发请求〕目的：把 README 那句「每条都有源码依据」钉成断言，
        # 否则承诺会随着下一次改写重新变成空话。
        try:
            self.dependencies._safe_json("<!DOCTYPE html><html><body>upstream oops</body></html>", "esearch")
            _html = "没抛错"
        except self.dependencies.NcbiError as _e:
            _html = str(_e)
        check("200 + HTML 正文要报 NcbiError 并把正文带回来（不许静默当空结果）",
              "不是合法 JSON" in _html and "<!DOCTYPE" in _html, _html[:120])
        _sd = getattr(self.dependencies.search, "__doc__", "") or ""
        check("search 的工具说明里 sort 三个值都在（MCP 客户端只读这段，说明过期＝行为过期）",
              all(w in _sd for w in ("pub_date", "relevance", "author")), repr(_sd[:90]))
        _sav = self.dependencies._KEY
        self.dependencies._KEY = "a1" * 18
        try:
            _m = self.dependencies._mask("leak:" + "a1" * 18 + " tail")
        finally:
            self.dependencies._KEY = _sav
        check("_mask 不留任何 key 片段（定长掩码；短 key 也掩，旧写法这两条都漏）",
              ("a1" * 18) not in _m and "***" in _m, _m[:60])
        # 〔发布版新增，离线〕SDK 兼容层自己也得有断言：「本文件同时支持 mcp 1.x 与 2.x」是一句
        # 会被下一次改写悄悄作废的承诺，没有断言就等着它过期（这个文件已经有一条这样的过期记录）。
        check("SDK 兼容层拿到的是可用服务类（能构造、有 tool、有 run）",
              callable(self.dependencies.FastMCP) and hasattr(self.dependencies.FastMCP, "tool") and hasattr(self.dependencies.mcp, "run"),
              "%s.%s" % (self.dependencies.FastMCP.__module__, self.dependencies.FastMCP.__name__))
        # 〔发布版新增，第 5 条〕这一条必须**走 SDK 的工具包装层**，不能像本探针其余断言那样直调协程：
        # 直调看不见「异常经过 tools/base.py 时文本还不还在」，而 mcp 2.x 正是在这里把非 ToolError 的
        # 原文丢掉的（实测 2026-11-10：同一个拦截，1.30.0 给调用方 323 字符、未修的 2.3.0 只给 27 字符）。
        # 这条断言就是那个 bug 的回归锁——它挂掉＝「失败绝不静默」被打穿，而不是某句文案不好看。
        # 不发请求：_esearch 第一件事就是 _check_sort，在联网之前抛。
        try:
            _wrap = await self.dependencies.mcp.call_tool("search", {"term": "x", "sort": "journal"})
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
                              ("check_recall", {"term": "x", "pmids": " ".join(str(i) for i in range(1, self.dependencies.GOLD_MAX + 2))}, "最多"),
                              ("mesh", {"term": ""}, "不能为空"),
                              ("diff", {"term_a": "x", "term_b": "y", "max_show": -1}, "不能为负"),
                              ("export", {"term": "x", "path": ""}, "不能为空")):  # 本项目自订离线探针输入
            try:
                _w2 = str(await self.dependencies.mcp.call_tool(_wt, _wa))
            except Exception as _w2e:
                _w2 = str(_w2e)
            check("包装层文本完整：%s（与 search 同一条通路，不是一次性实测）" % _wt,
                  _wn in _w2, _w2[:120])
        # 〔1.4.5〕拼错/多出的参数名必须在包装层被拒，而不是被 SDK 静默丢掉后按默认值照跑
        # 〔实测 2026-11-13：1.4.4 下 export(..., formt="csv") 照写 pmids 文件〕。path 为空会在联网前被拒，
        # 所以这条若没被 extra=forbid 拦下，也只会得到「path 不能为空」而不会发请求或写文件。
        try:
            _wx = str(await self.dependencies.mcp.call_tool("export", {"term": "x", "path": "", "formt": "csv"}))
        except Exception as _wxe:
            _wx = str(_wxe)
        check("未知参数必须被包装层拒绝并点名（formt），不能静默丢弃" + (f"：{self.dependencies._EXTRA_FORBID}" if self.dependencies._EXTRA_FORBID else ""),
              not self.dependencies._EXTRA_FORBID and "formt" in _wx and "不能为空" not in _wx, _wx[:160])
        # 〔第 9 条〕把上面那条改口的依据钉住：库名大小写不敏感。纯函数，离线，不发请求。
        # 依据：实测 links(to="PMC") 走的是【成功】分支（回 pubmed_pmc=PMC10461258），
        # 而旧报错却写「不认 'PMC'」——那句在它自己的报错里反证了自己（见 _check_db 上方的已作废说明）。
        check("库名大小写不敏感：_check_db('PMC') 与 _check_db(' Gene ') 都归一到库名",
              self.dependencies._check_db("PMC") == "pmc" and self.dependencies._check_db(" Gene ") == "gene",
              "_check_db('PMC')=%s" % self.dependencies._check_db("PMC"))
        # 〔第 10 条，离线〕README 行为契约第 9 条的后半句「本服务四个工具全是 async def」此前
        # 【没有断言钉住】，而那半句是承重的：装饰之后的名字一旦不再是协程函数，SDK 就走同步分支
        # （1.x 是裸调用＝本文件占住事件循环；2.x 自己 to_thread 卸载）。装饰器返回值随 SDK 版本变，
        # 所以要问的是【装饰之后】那个名字，不是源码里的 def。
        # 〔实测 2026-11-11：mcp 1.30.0 与 2.3.0 两代下，四个名字装饰后都仍是协程函数〕
        _tools = (self.dependencies.search, self.dependencies.count, self.dependencies.fetch_abstracts, self.dependencies.links, self.dependencies.check_recall, self.dependencies.mesh, self.dependencies.diff, self.dependencies.export)
        _coro = [f for f in _tools if inspect.iscoroutinefunction(f)]
        check("全部工具装饰后仍是协程函数（离线）",
              len(_coro) == len(_tools), f"{len(_coro)}/{len(_tools)} 是协程函数")
        # C：⚠️ 必须在 gbk/charmap 上编得出来——这里不碰真 stdout，只验「reconfigure 之后
        #      用的是 utf-8」这件事，以及输出文本里的特殊符号在非 utf-8 上确实编不出（旧版会炸）。
        _probe_line = "⚠️ 检索式被 NCBI 改写 牙根吸收 ✓"
        _crashes = [c for c in ("cp936", "cp1252") if self.dependencies._raises_enc(_probe_line, c)]
        check("判据本身有效：⚠️/中文在 cp936、cp1252 上确实编不出（所以 reconfigure 不是多余）",
              len(_crashes) == 2, f"编不出的代码页: {_crashes}")

        # 引号短语被换成按词 AND 时，命中数【不是】短语的数——这条判据不写死具体命中数
        _ph = await attempt("c_phrase", self.dependencies.count,
                            '41338119 AND "zygomatic implant assisted rapid palatal expansion"', default="")
        _phn = self.dependencies._num_of(_ph)
        check("整段短语被 NCBI 换成按词 AND 时，仍有命中且必须说出「这不是短语的数」",
              _phn > 0 and "quotedphrasesnotfound" in _ph and "不是" in _ph,
              f"{_phn} 条；⚠️ 段落: {[l[:70] for l in _ph.splitlines() if 'quoted' in l][:1]}")

        # 15) 本轮离线回归：每条都对应一个【实测过】的缺陷，不发请求
        print("15) 离线回归：本轮修掉的静默与误判")
        _ar = "٢٠٢٠-٠١-٠١"
        check("日期里的 Unicode 数字被归一成 ASCII（不再原样下发被 NCBI 丢掉）",
              self.dependencies._check_date(_ar, "mindate") == "2020-01-01", repr(self.dependencies._check_date(_ar, "mindate")))
        try:
            self.dependencies._check_date("2023-02-29", "mindate")
            check("反向：日历上不存在的日期仍然被拦", False, "没抛")
        except self.dependencies.NcbiError:
            check("反向：日历上不存在的日期仍然被拦", True, "")
        for _bad in ("1，234，567", "1,234,567,", "1,234,567"):
            try:
                self.dependencies._parse_pmids(_bad)
                check(f"千位分隔符形状不再被切成多个 PMID：{_bad}", False, "没抛")
            except self.dependencies.NcbiError:
                check(f"千位分隔符形状不再被切成多个 PMID：{_bad}", True, "")
        check("反向：正常逗号列表仍能解析", self.dependencies._parse_pmids("12345,678")[0] == ["12345", "678"], "")
        try:
            self.dependencies._parse_pmids("9" * 4301)
            check("超长数字串给的是 NcbiError，不是裸 ValueError", False, "没抛")
        except self.dependencies.NcbiError:
            check("超长数字串给的是 NcbiError，不是裸 ValueError", True, "")
        except ValueError as e:
            check("超长数字串给的是 NcbiError，不是裸 ValueError", False, f"裸 {type(e).__name__}")
        try:
            self.dependencies._decode_key_bytes(b"\xff\xfeA")
            check("截断的 UTF-16 BOM key 文件不再抛裸 UnicodeDecodeError", True, "")
        except Exception as e:
            check("截断的 UTF-16 BOM key 文件不再抛裸 UnicodeDecodeError", False, type(e).__name__)
        check("带字段标签的全丢查询不再漏判（不再说「确实没有匹配」）",
              self.dependencies._all_dropped({"errorlist": {"phrasesnotfound": ["zzzqqx"]}, "_term": "zzzqqx[Title]",
                            "querytranslation": "zzzqqx[Title]"}) is True, "")
        try:
            self.dependencies._check_field("ZZZBOGUSFIELD")
            _fmsg = ""
        except self.dependencies.NcbiError as e:
            _fmsg = str(e)
        check("未知 field 的「可用值」不再列出会被吞掉的值",
              "PMC Number" not in _fmsg and "Title" in _fmsg, _fmsg[:160])
        _old_get, _old_exist = self.dependencies._get_async, self.dependencies._pmids_exist

        async def _fake_elink(path, params, timeout, tries=4):
            if path.startswith("elink"):
                return json.dumps({"linksets": [{"dbfrom": "pubmed", "ids": ["7545954"], "linksetdbs": [
                    {"dbto": "nuccore", "linkname": "pubmed_nuccore_refseq", "links": ["1947007834"]}]}]})
            return "{}"

        async def _fake_exist(pids):
            return {pid: "present" for pid in pids}

        self.dependencies._get_async, self.dependencies._pmids_exist = _fake_elink, _fake_exist
        try:
            _s_nuc = await self.dependencies.links("7545954", to="nucleotide")
        finally:
            self.dependencies._get_async, self.dependencies._pmids_exist = _old_get, _old_exist
        # 只看这一行：文末图例会原样引用「(nucleotide 库无直接对应记录)」这句话，用整段做判据会恒假。
        _nuc_line = next((l for l in _s_nuc.splitlines() if l.startswith("PMID 7545954")), "")
        check("nucleotide 的直接链接不再被说成「无直接对应记录」",
              "无直接对应记录" not in _nuc_line and "pubmed_nuccore_refseq=" in _nuc_line, _nuc_line[:200])
        try:
            await self.dependencies.search("tooth", retstart=-5)
            check("负 retstart 被拦下（不再静默变成第一页）", False, "没抛")
        except self.dependencies.NcbiError as e:
            check("负 retstart 被拦下（不再静默变成第一页）", "负数" in str(e), str(e)[:120])

        async def _fake_html(path, params, timeout, tries=4):
            return "<!DOCTYPE html><html><body>502 maintenance</body></html>"

        _old_get = self.dependencies._get_async
        self.dependencies._get_async = _fake_html
        try:
            await self.dependencies.fetch_abstracts("37427694")
            check("efetch 回 HTML 时必须算失败（不把故障页当摘要）", False, "没抛")
        except self.dependencies.NcbiError as e:
            check("efetch 回 HTML 时必须算失败（不把故障页当摘要）", "HTML" in str(e), str(e)[:120])
        finally:
            self.dependencies._get_async = _old_get

        async def _fake_mixed(path, params, timeout, tries=4):
            if path.startswith("esearch"):
                return json.dumps({"esearchresult": {"count": "2", "retmax": "2", "retstart": "0",
                                                     "idlist": ["1001", "1002"], "querytranslation": "x"}})
            if path.startswith("esummary"):
                return json.dumps({"result": {"uids": ["1001"], "1001": {
                    "uid": "1001", "title": "T", "pubdate": "2020", "source": "J",
                    "authors": [], "elocationid": ""}}})
            return "{}"

        _old_get = self.dependencies._get_async
        self.dependencies._get_async = _fake_mixed
        try:
            _j_miss = json.loads(await self.dependencies.search("x", retmax=2, as_json=True))
        finally:
            self.dependencies._get_async = _old_get
        check("as_json 在 esummary 少回时给出 ⚠️，returned 记的是实际记录数",
              any("esummary 只回了" in w for w in _j_miss.get("warnings") or [])
              and _j_miss.get("returned") == 1,
              str({k: _j_miss.get(k) for k in ("returned", "esummary_missing")}))
        try:
            self.dependencies.main(["--probeX"])
            check("拼错的 CLI 参数以非 0 退出", False, "居然没 SystemExit")
        except SystemExit as e:
            check("拼错的 CLI 参数以非 0 退出", e.code == 2, f"code={e.code}")
        except Exception as e:
            check("拼错的 CLI 参数以非 0 退出", False, f"{type(e).__name__}: {str(e)[:80]}")

        check("计数器活着（不是恒 0；阈值 40 是自订的下限，用来发现计数器被改坏）",
              self.dependencies._NREQ > 40, f"本轮 {self.dependencies._NREQ} 次 urlopen")
        print(f"\n{ok} 通过 / {fail} 失败 —— 本轮 {self.dependencies._NREQ} 次 HTTP 请求（含退避重试的每一次）")
        return fail


    def _check_key(self):
        k = self.dependencies.api_key()
        print("key:", self.dependencies._key_shape(k))
        if k:
            print("  （值本身不打印；前缀只在你自己机器上看得见——发布版源码里也不留任何 key 片段）")
            # 〔实测 2026-11-11〕旧版把「第一个存在的 key 文件」当成实际来源报出来；环境变量生效时
            # 那句话是假的。现在直接报真正读到 key 的那一处。
            print("key 来源:", self.dependencies._KEY_SOURCE or "(未知)")
        print("限速档位:", "10 次/秒" if k else "3 次/秒")
        for p in ("~/.ncbi_api_key", "~/.ncbi_api_key.txt"):
            q = os.path.expanduser(p)
            if os.path.isfile(q):
                head = open(q, "rb").read(3)
                enc = ("UTF-16（PowerShell 默认写法，本工具认得）" if head[:2] in (b"\xff\xfe", b"\xfe\xff")
                       else "UTF-8 带 BOM（记事本写法，本工具会剥掉）" if head == b"\xef\xbb\xbf"
                       else "UTF-8 无 BOM")
                print(f"  候选文件: {q}  {enc}")
        print("EMAIL:", self.dependencies.EMAIL or "(未设 NCBI_EMAIL，NCBI 的 courtesy 要求建议设上)")
        print("依赖:", end=" ")
        try:
            from importlib.metadata import version
            print(", ".join(f"{p} {version(p)}" for p in ("mcp", "anyio", "pydantic")))
        except Exception as e:
            print("读版本失败", e)
        print("多余参数:", "⚠️ " + self.dependencies._EXTRA_FORBID if self.dependencies._EXTRA_FORBID else "拒绝（拼错或多出的参数名会报错，不会被静默忽略）")

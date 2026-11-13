# 历史工具说明归档

来源：修改前源码中工具函数的 docstring，逐字保存。原实测/未核实/已作废标记仍适用；这是历史快照，不是当前接口说明。数值请回各段原始来源核对。

## search

```text
在 PubMed 检索，返回命中数 + 每条的 PMID/日期/期刊卷页/标题/作者/DOI/PMC。

    什么时候用：找文献、核对某主题有多少文章、拿 PMID 列表。
    term 支持 PubMed 语法：字段标签如 resorption[Title]（引号只包短语，别把 [Tag] 包进去）、
    布尔组合 "A AND B"、截词 "resorption*"、范围 "2024:2026[dp]"。建议一次只写一个概念，靠字段标签收窄。
    sort 只接受 pub_date / relevance / author〔实测 2026-11-10 本机 idlist 两两对照：这三个真会换
    顺序，其余值与不传 sort 逐字同序。本文件 SORT_REAL 上方有一条【已作废】记录：这里原先只写两个值，
    属说明过期、不是代码错〕。NCBI 对认不出的 sort 值是静默接受、顺序照旧，所以本工具直接拦。
    **PubMed 没有「按期刊好坏排序」这个概念**，要那种排序得自己接外部期刊指标数据源，本仓库不含这类脚本。
    〔实测 2026-11-10 订正：更早的版本这里写死了作者某个工作目录的绝对路径，而本服务注册在全局、
    在别的项目里读到那句就是死路，所以改成不带路径、也不指具体脚本的说法。〕
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
      两段【同字段】的日期不相交时命中通常为 0，但【不必然】：实测有记录同时带有落进两段的
      日期（〔实测 2026-11-11〕1 条，PMID 32672908）。输出里有专门的「两段同字段（…）日期不相交」
      说明，不会跟「确实没有匹配这篇」混在一起（〔实测 2026-11-09〕旧版在这里是本机换算＋取交集，
      两种路径给出的命中数不一样，那才是真 bug；旧版还写着「不相交直接报错」，现已改由 NCBI 判、
      由本工具解释）。挂在不同字段上的两段（一段 [Date - Entry]、一段 [Date - Publication]）
      【不】会说成不相交 —— 那是两个不同的轴，比端点没有意义〔实测 2026-11-10：
      "2020:2020[edat] AND implant" 配 reldate=60 的回显就是这种形状〕。
    - "0 条"是请求成功、不是网络故障（故障会以 NcbiError 抛出），但**不能一律读成「确实没有匹配」**。
      〔实测 2026-11-10〕返回 0 条记录的原因不止一种，常见的有（返回里都会另起一行说清楚）：
        ① 中文词（"牙根吸收"）→ querytranslation 为空 = NCBI 没解析出检索条件，换英文词；
        ② 词全被当停用词（"the and of"）→ warninglist.phrasesignored 列全了词 = 生效条件为空，
          这句 0 跟主题有没有文献无关；
        ③ 整个引号短语不存在（'"zzzqqxnonexistentterm"'）→ warninglist.quotedphrasesnotfound；
        ④ 两段同字段日期互斥 → 见上面「不相交」那条；
        ⑤ retmax=0 且命中数>0（只要计数、没要记录）→ 命中数是真的，只是没取记录；
        ⑥ retstart 越过命中总数（翻页越界）→ 往回翻页即可。
      这些情形都不成立、且命中数确实为 0 时，「PubMed 里没有匹配」才成立。
    - 单复数（device/devices）在 PubMed 里映射到不同词形，实测差 1 条
      〔已作废 2026-11-10：现在 "temporary anchorage device" 与 "...devices" 都是 795 条、
      querytranslation 逐字相同 —— NCBI 把两边都展开成同一组 OR；当年那个 1 条的差不复现，
      但「用 MeSH 词覆盖两种写法」这条建议本身仍然有效〕。要覆盖两种写法，改用 MeSH descriptor，
      并且【核对实际执行的式子】判断写没写对 —— 本工具不做词表查询，而 descriptor 写错有两种形状，
      后果并不一样（〔实测 2026-11-11，本会话，三种写法各跑一次 count，见 docs/release-delta.md〕）：
        给一个不存在的 descriptor 加引号（"Temporary Anchorage Devices"[MeSH Terms]）→ 0 条，
          NCBI 在 warninglist.quotedphrasesnotfound 里点名这段，本工具会转述 —— 不算静默；
        同样的词【不加引号】配 [MeSH Terms] → 照样回一个数，但「NCBI 实际执行」已被改写成
          另一组条件（实测那次把 Devices 换成了 "equipment and supplies"[MeSH Terms]），
          主题也收窄了 —— 【这一种才没人提醒你】，光看条数看不出来。
        写法对的形状回显就是它自己："Orthodontic Anchorage Procedures"[MeSH Terms]（--probe
          的联网断言会核对这条回显）。注意：回显与输入相同时，文本输出【不印】「NCBI 实际执行」
          那一行（只在被改写时印）；要逐字核对就传 as_json=True 看 querytranslation。
      所以别只信条数：文本里看「NCBI 实际执行」，要逐字核就用 as_json 的 querytranslation。

```

## count

```text
只问「这个主题有多少篇」，不取记录，最省请求。查询被改写时印出「NCBI 实际执行」那一行。

    注意：命中数大得反常时先看 ⚠️ 和「NCBI 实际执行」——实测查不到的词被 NCBI 静默丢弃后
    仍会返回一个非 0 的数（"zzzqqx nonexistent term" → 610 条），0 条反而是少见的情况。
    中文查询词（实测 "牙根吸收" → 0 条 + 空 querytranslation）会得到「NCBI 没解析出任何检索条件」，
    那不等于主题没文献，换英文词再问。

    **命中数既不证明「短语存在」，也不证明「文献不存在」**〔实测 2026-11-10〕：
    `count('41338119 AND "zygomatic implant assisted rapid palatal expansion"')` 给【1 条】，
    而同一次返回里 NCBI 报了 quotedphrasesnotfound —— 它把整段短语换成按词 AND 执行的，
    于是这 1 条只说明【那几个词都出现在这条记录里】；该记录标题实际是
    "…Personalized Microimplant-Assisted Rapid Palatal Expansion…"，并没有 "zygomatic implant" 这段连续短语。
    反过来 0 条也只说明这个式子没匹配上。要确认短语在不在，请把取回来的题录/摘要自己读一遍，
    **别信引号**。
    field 与 search 一样走白名单；count 拿不到数字时抛 NcbiError，不印 "?"。

```

## fetch_abstracts

```text
按 PMID 取摘要原文（NCBI 排版，含 INTRODUCTION/METHODS/RESULTS/CONCLUSIONS）。

    pmids 用逗号分隔，如 "42998065,42997565"。内部每 50 个一批并发，多少个都收。
    引用数值请以这里的原文为准，search 返回的只有标题和 DOI。
    返回头是「要 N 条 / 实取 M 条」两个数——不存在的 PMID 会被点名（实测旧版把请求数当结果数，
    拿 PMID 9999999999 会报「取回 1 条」再加一条空壳）。
    口径注意：这里的「实取」= NCBI 返回了这条**记录**，不等于里面有摘要。社论、letter 本来就没有摘要，
    〔实测 2026-11-08：PMID 42759866 → 「要 1 条 / 实取 1 条」且没有任何 ⚠️，正文里只有引文行〕
    旧版 docstring 说这种会列进「NCBI 未返回」，是假的，已改口。要判断有没有摘要得看正文本身。
    补零的号（如 000037427694）现在由 _parse_pmids 统一成裸号，不会再出现「实取 1 条」与
    「NCBI 没有返回这条」同屏自相矛盾。〔实测 2026-11-08〕

```

### fetch_abstracts（1.4.0–1.4.1，1.4.2 加 fields/as_json/out_path 前）

```text
按逗号/空格分隔的 PMID 取 NCBI 原排版摘要；保留结构化段落、作者信息和声明。

    自动去重、归一补零号、分批。建议先筛选题录，再只取需要的摘要以节省输出。
    返回请求数、实际记录数和缺失号；有记录不等于有摘要，社论可能只有引文。
    网络失败报错，不冒充无记录。文献数值引用仍须核对原文位置。
```

## links

```text
查这些 PMID 在别的数据库里的对应记录（elink）。to=pmc 用来判断「有没有免费全文可下」。

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

```

## export

### export（1.4.0–1.4.2，1.4.3 加 ris 前）

```text
取全某查询的 PMID 并写文件；超 9999 窗口自动按发表日期二分切片。
    format: pmids（纯 PMID 列表）/ csv（核心题录字段，不含摘要）/ medline（.nbib，保留来源中已有的摘要与主题词）。
    只回摘要（总数/片数/去重/路径/sha256），不回记录本身。path 显式给出且不得已存在（不覆盖）。
    切片计数与总数核对，不一致如实报告；检索期间数据库可能更新，结果以导出时点为准。
    自订安全上限 10 万条。筛选工具导入兼容性以对方文档为准；不保证与 PubMed 网页导出逐字一致。
```

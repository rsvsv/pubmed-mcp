"""Pure query diagnostics. No transport, MCP initialization or file operations.
Moved rules and numeric literals retain their original source/policy annotations.
"""
import re


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
    out.extend(_term_lint(term))
    return out


# ---- 半角字段标签与布尔结构检查（1.4.1） ------------------------------------------------
# 依据：〔实测 2026-11-13，esearch retmax=0，以 miniscrew[<tag>] 的执行式是否与裸词逐字相同判定，
#   共约 300 次请求；原始数据 pubmcp-audit/review-20261112/tag_probe_20261113.json、tag_probe2_20261113.json〕
# NCBI 对认不出的 term 内标签【不报错、不进 errorlist】，只是静默按无标签（All Fields/自动映射）执行，
# 所以只能本地查表。表外标签一律提示——宁可多问一句，也不让限定被吞掉还照常给数。
_TAG_OK = frozenset("""
ad affiliation aid au author auid book cois cn crdt dcom rn ed editor edat filter sb subset
1au fau fir gr grant ir investigator isbn ip issue ta journal jour la lang language lastau lid
mhda majr sh subheading subh mh mesh lr jid ot keyword pg pagination ps pa pl pmid uid
dp pdat date pt ptyp pubn publisher si nm tw text ti title tiab tt vi volume doi epdat ppdat so
""".split()) | frozenset({
    "all fields", "all",   # 与不写标签等价（实测执行式逐字相同），合法 no-op
    "corporate author", "create date", "completion date", "ec/rn number", "entry date", "first author",
    "first author name", "full author name", "full investigator name", "grants and funding", "grant number",
    "last author", "last author name", "location id", "mesh date", "mesh major topic", "mesh terms",
    "mesh subheading", "modification date", "other term", "personal name as subject", "pharmacological action",
    "place of publication", "publication date", "date - publication", "publication type", "secondary source id",
    "supplementary concept", "substance name", "text word", "transliterated title", "title/abstract",
    "date - entry", "date - create", "date - completion", "date - modification", "date - mesh",
    "conflict of interest statements", "author - identifier", "author - full", "author - first",
    "author - last", "author - corporate", "investigator - full",
})
# 实测无效、但很常见的写法：(建议, breaking)。breaking=False 的几项值本身常被自动映射救回，只提示核对。
_TAG_BAD = {
    "ab": ("[tiab]（PubMed 没有单独的摘要字段）", True),
    "abstract": ("[tiab]（PubMed 没有单独的摘要字段）", True),
    "keywords": ("[ot]（作者关键词＝Other Term）", True),
    "kw": ("[ot]（作者关键词＝Other Term）", True),
    "other terms": ("[ot]", True),
    "text words": ("[tw]", True),
    "mesh term": ("[mh]", True),
    "mesh subheadings": ("[sh]", True),
    "date of publication": ("[dp]", True),
    "conflict of interest statement": ("[cois]", True),
    "article identifier": ("[aid]", True),
    "author identifier": ("[auid]", True),
    "jt": ("[ta]", True),
    "source": ("[ta]", True),
    "nlm unique id": ("[jid]（实测 8610224[nlm unique id] 被执行成 8610224[UID]，变成按 PMID 查）", True),
    "nlm id": ("[jid]", True),
    "pmc": ("去掉标签直接写 PMC 号（如 PMC10461258，实测按 All Fields 可命中）", False),
    "pmcid": ("去掉标签直接写 PMC 号（如 PMC10461258，实测按 All Fields 可命中）", False),
    "issn": ("去掉标签或改用刊名[ta]（实测 0889-5406[issn] 靠自动映射才落到刊名）", False),
}
_NOEXP_BASES = frozenset({"mh", "mesh", "mesh terms", "majr", "mesh major topic", "sh", "subheading", "subh",
                          "mesh subheading", "pt", "ptyp", "publication type"})
_PROX_BASES = frozenset({"ti", "title", "tiab", "title/abstract", "ad", "affiliation"})
_DATE_TAGS = frozenset({"dp", "pdat", "date", "publication date", "date - publication", "edat", "entry date",
                        "date - entry", "crdt", "create date", "date - create", "dcom", "completion date",
                        "date - completion", "lr", "modification date", "date - modification", "mhda",
                        "mesh date", "date - mesh", "epdat", "ppdat"})
_MESH_TAGS = frozenset({"mh", "mesh", "mesh terms", "majr", "mesh major topic"})
_TAG_SPAN_RE = re.compile(r"\[([^\[\]\n]{1,40})\](?![A-Za-z0-9])")


def _tag_norm(raw):
    """标签归一：去首尾空格、小写；冒号两侧空格去掉。【不】压内部空格——实测 `MeSH  Terms`（双空格）
    与 `Title /Abstract` 都被 NCBI 静默忽略，压掉会漏报。"""
    return re.sub(r"\s*:\s*", ":", raw.strip().lower())


def _tag_shape(norm):
    """像不像字段标签。排除同位素/化学式 [123I]、纯数字等。"""
    if not re.fullmatch(r"[a-z1][a-z0-9 /\-:~]*", norm) or not re.search(r"[a-z]{2}", norm):
        return False
    return not norm[0].isdigit() or norm.startswith("1au")


def _date_tuple(y, rest, end):
    parts = [int(x) for x in rest.split("/") if x]
    m_ = parts[0] if parts else (12 if end else 1)
    d_ = parts[1] if len(parts) > 1 else (31 if end else 1)
    return int(y), m_, d_


def _term_lint(term):
    """半角 [Tag]、子字段、前导截词、AND NOT、组标签、日期写法、同层 AND/OR 混用。纯本地，零请求。
    返回同 _syntax_findings 的 (code, breaking, text)。"""
    import difflib
    term = str(term or "")
    out = []
    # 引号短语掩成等长 X（保留长度与位置，标签检查不进引号）；fullmask 再把 [..] 内容也掩掉，供布尔检查。
    qmask = re.sub(r'"[^"\n]*"', lambda m: "X" * len(m[0]), term)
    qmask = re.sub(r"\b10\.\d{4,9}/\S+|\bhttps?://\S+", lambda m: "X" * len(m[0]), qmask)
    fullmask = re.sub(r"\[[^\]\n]*\]", lambda m: " " * len(m[0]), qmask)

    unknown, soft, subs, prox, dates = [], [], [], [], []
    for m in _TAG_SPAN_RE.finditer(qmask):
        norm = _tag_norm(m[1])
        if not _tag_shape(norm):
            continue
        base, _, sub = norm.partition(":")
        if base in _TAG_BAD:
            hint, brk = _TAG_BAD[base]
            (unknown if brk else soft).append(f"[{m[1].strip()}] → {hint}")
            continue
        if base not in _TAG_OK:
            near = difflib.get_close_matches(base, sorted(_TAG_OK), n=1, cutoff=0.75)
            unknown.append(f"[{m[1].strip()}]" + (f" → 可能想写 [{near[0]}]" if near else ""))
            continue
        before = qmask[:m.start()].rstrip()
        if sub:
            if sub == "noexp" and base not in _NOEXP_BASES:
                subs.append(f"[{m[1].strip()}]（:noexp 只对 MeSH/文献类型类标签有意义，这里被忽略）")
            elif re.fullmatch(r"~\s*\d+", sub):
                if base not in _PROX_BASES:
                    subs.append(f"[{m[1].strip()}]（邻近检索只支持 ti/tiab/ad，这里 :~N 被忽略）")
                elif not term[:m.start()].rstrip().endswith('"') and \
                        len(re.split(r"\s+", re.split(r'[()]|\b(?:AND|OR|NOT)\b', before)[-1].strip())) >= 2:
                    prox.append(m[1].strip())
            elif sub not in ("noexp",) and not sub.startswith("~"):
                subs.append(f"[{m[1].strip()}]（未知子字段 :{sub}，NCBI 会忽略）")
        if base in _DATE_TAGS and not term[:m.start()].rstrip().endswith('"'):
            tok = re.search(r"([^\s()]+)$", before)
            tok = tok[1] if tok else ""
            if tok and not re.fullmatch(r"[\d/:]+", tok) and not re.search(r"\d[-–—]\d", tok):
                dates.append(("date_tag_value", f"{tok}[{m[1].strip()}]"))
    if unknown:
        out.append(("term_tag_unknown", True,
                    "⚠️ 这些字段标签 PubMed 不认：" + "；".join(unknown) +
                    "。NCBI 不报错，会静默按无标签（All Fields/自动映射）执行，命中数照常给出但限定已失效"
                    "（实测 2026-11-13：miniscrew[tiabb] 2146 条＝裸词，miniscrew[tiab] 1487 条）。请改正后重查。"))
    if soft:
        out.append(("term_tag_ignored", False,
                    "⚠️ 这些标签 PubMed 不认、已被忽略，结果靠自动映射才可能对：" + "；".join(soft) +
                    "。请核对实际执行式。"))
    if subs:
        out.append(("term_tag_subfield", False, "⚠️ 子字段写法无效：" + "；".join(subs) + "。"))
    if prox:
        out.append(("proximity_unquoted", True,
                    "⚠️ 邻近检索 [tiab:~N] 必须用双引号包住多词短语（如 \"root resorption\"[tiab:~3]）："
                    "不加引号时 NCBI 按普通词拆开映射（实测 2026-11-13：root resorption[tiab:~3] 7454 条，"
                    "root 被映射成 plant roots；加引号 5498 条）。涉及：" + "、".join(f"[{p}]" for p in prox) + "。"))
    for code, txt in dates:
        out.append((code, True,
                    f"⚠️ 日期标签前不是日期：{txt}。NCBI 会返回 0 条（实测 2026-11-13：miniscrew[dp] 0 条）。"
                    "日期写成 2020:2024[dp]；相对日期须加引号，如 \"last 5 years\"[dp]。"))

    dt = r"\[\s*(" + "|".join(sorted((re.escape(t) for t in _DATE_TAGS), key=len, reverse=True)) + r")\s*\]"
    hy = re.search(r"(?<![\d/])(\d{4}(?:/\d{1,2}){0,2})\s*[-–—]\s*(\d{4}(?:/\d{1,2}){0,2})\s*" + dt, qmask, re.I)
    if hy:
        out.append(("date_range_hyphen", True,
                    f"⚠️ 日期区间 {hy[1]}-{hy[2]} 用了连字符：PubMed 区间只认冒号，连字符写法必返回 0 条"
                    "（实测 2026-11-13：miniscrew AND 2020-2024[dp] 0 条；2020:2024[dp] 705 条）。"
                    f"请改成 {hy[1]}:{hy[2]}[{hy[3]}]，这个 0 不是该时段无文献。"))
    for rv in re.finditer(r"(?<![\d/])(\d{4})((?:/\d{1,2}){0,2})\s*:\s*(\d{4})((?:/\d{1,2}){0,2})\s*" + dt, qmask, re.I):
        if _date_tuple(rv[1], rv[2], False) > _date_tuple(rv[3], rv[4], True):
            out.append(("date_range_reverse", True,
                        f"⚠️ 日期区间 {rv[1]}{rv[2]}:{rv[3]}{rv[4]} 起点晚于终点：PubMed 不会自动对调，返回 0 条"
                        "（实测 2026-11-13：2024:2020[dp] 0 条）。请把早的日期写在冒号前。"))
            break

    if re.search(r"(?:^|[\s(])\*(?=[A-Za-z0-9])", fullmask):
        out.append(("leading_wildcard", True,
                    "⚠️ 前导截词（*screw）PubMed 不支持：* 被静默删掉，按精确词执行"
                    "（实测 2026-11-13：*screw[tiab] 与 screw[tiab] 同为 51504 条、执行式相同）。"
                    "截词只能放词尾（screw*），词头变体请显式列出（miniscrew OR microscrew）。"))
    if re.search(r"\b(?:AND|OR)\s+NOT\b", fullmask):
        out.append(("and_not", True,
                    "⚠️ 「AND NOT / OR NOT」PubMed 不支持：NOT 被丢弃，排除条件反而变成纳入"
                    "（实测 2026-11-13：tooth resorption AND NOT CBCT 执行成 … AND \"CBCT\"）。"
                    "排除请直接写 A NOT B。"))

    # 括号组：组后加标签、组内多于一个词/短语时标签不分配；同层 AND 与 OR 混用。括号不配对时跳过（已有 paren 警告）。
    pairs, stack, level_ops, bad = {}, [], [set()], False
    for t in re.finditer(r"[()]|\b(?:AND|OR)\b", fullmask):
        if t[0] == "(":
            stack.append(t.start())
            level_ops.append(set())
        elif t[0] == ")":
            if not stack:
                bad = True
                break
            pairs[t.start()] = stack.pop()
            if {"AND", "OR"} <= level_ops.pop():
                level_ops[-1].add("MIXED")
        else:
            level_ops[-1].add(t[0])
    if not bad and not stack:
        grp = []
        for m in re.finditer(r"\)\s*\[([^\[\]\n]{1,40})\]", qmask):
            norm = _tag_norm(m[1])
            o = pairs.get(m.start())
            if o is None or norm in ("all fields", "all") or norm.partition(":")[0] not in _TAG_OK:
                continue
            inner = term[o + 1:m.start()]
            units = re.findall(r'"[^"\n]*"|[^\s()"]+', inner)
            units = [u for u in units if u not in ("AND", "OR", "NOT")]
            if len(units) > 1:
                show = " ".join(inner.split())
                grp.append(f"({show[:40]}{'…' if len(show) > 40 else ''})[{m[1].strip()}]")
        if grp:
            out.append(("group_tag", True,
                        "⚠️ 字段标签写在多词括号组后面不会分配给组内各词：" + "；".join(grp[:3]) +
                        "。组内词按无标签执行（实测 2026-11-13：(miniscrew OR \"mini-implant\")[tiab] 全部变 All Fields，"
                        "2928 条；逐词加 [tiab] 为 2309 条）。请给组内每个词/短语各自加标签。"))
        if "MIXED" in level_ops[0] or {"AND", "OR"} <= level_ops[0]:
            out.append(("mixed_boolean", False,
                        "⚠️ 同一层括号里 AND 与 OR 混用：PubMed 严格从左到右计算，不是 AND 优先"
                        "（实测 2026-11-13：orthodontics[mh] AND review[pt] OR meta-analysis[pt] 执行成 "
                        "(orthodontics AND review) OR meta-analysis，250665 条）。请用括号写明分组。"))
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

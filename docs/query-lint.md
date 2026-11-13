# 检索式本地语法检查（1.4.1）

## 为什么要做

NCBI 对 term 里写错的语法大多**不报错**，而是静默改写后照常给命中数〔实测 2026-11-12/13，审查报告 #0 #1 #17 C0 C1 C2 C4〕。
例如 `miniscrew[tiabb]` 的执行式与裸词 `miniscrew` 逐字相同，命中 2146 条；`miniscrew[tiab]` 是 1487 条。
输出里没有 ⚠️，`errorlist.fieldsnotfound` 也是空的。这正是本工具「把查不到和没有分开」要拦的那类静默错误。

## 做法

`_syntax_findings` 末尾调用 `_term_lint(term)`。纯本地、零请求，不改写检索式，只追加 ⚠️ 行。

| code | 触发 | breaking（计入 check_recall 的 term_degraded） |
|---|---|---|
| `term_tag_unknown` | 半角 `[Tag]` 不在实测白名单内，或属于实测无效的常见写法（`[ab]`/`[keywords]`/`[mesh term]`/`[jt]`…） | 是 |
| `term_tag_ignored` | `[pmc]`/`[pmcid]`/`[issn]`：标签被忽略，但值常被自动映射救回 | 否 |
| `term_tag_subfield` | `:noexp` 加在非 MeSH/文献类型标签上；`:~N` 加在非 ti/tiab/ad 上；未知子字段 | 否 |
| `proximity_unquoted` | `[tiab:~N]` 前是未加引号的多词 | 是 |
| `date_tag_value` | 日期标签前不是日期（`miniscrew[dp]`、`last 5 years[dp]`） | 是 |
| `date_range_hyphen` | `2020-2024[dp]`（含全称日期标签、各种破折号） | 是 |
| `date_range_reverse` | `2024:2020[dp]`、`2020/06:2020/01[dp]` | 是 |
| `leading_wildcard` | `*screw` | 是 |
| `and_not` | `AND NOT` / `OR NOT` | 是 |
| `group_tag` | `(A OR B)[tiab]`：组内多于一个词/短语时，标签后置；`[All Fields]` 例外 | 是 |
| `mixed_boolean` | 同一层括号里 AND 与 OR 混用 | 否（结果确定，只是可能不合本意） |

白名单与无效写法表**逐个实测**：`miniscrew[<tag>]` 的执行式若与裸词逐字相同，判为被忽略。
原始数据：[audit-evidence/tag_probe_20261113.json](audit-evidence/tag_probe_20261113.json)、[tag_probe2_20261113.json](audit-evidence/tag_probe2_20261113.json)。
探针脚本同目录，约 290 次 esearch〔实测 2026-11-13〕。

## 关键实测（2026-11-13，esearch retmax=0）

- 被忽略：`[ab]` `[abstract]` `[keywords]` `[kw]` `[other terms]` `[text words]` `[mesh term]` `[mesh subheadings]` `[date of publication]` `[conflict of interest statement]` `[article identifier]` `[author identifier]` `[jt]` `[source]` `[pmc]` `[pmcid]` `[issn]` `[Title / Abstract]` `[MeSH  Terms]`（双空格）。
- `8610224[nlm unique id]` 被执行成 `8610224[UID]`（变成按 PMID 查）；`[jid]` 正确落到刊名。
- 识别：`[lang]` `[keyword]`（→ Other Term）`[so]` `[subh]` `[cois]`，以及 `[MeSH Terms:noexp]`、`[mh: noexp]`（冒号两侧空格可接受），`[ tiab ]`（首尾空格可接受）。
- `*screw[tiab]` 与 `screw[tiab]` 同为 51504 条，执行式相同。
- `tooth resorption OR NOT CBCT` 与 `… OR CBCT` 同为 32891 条。
- `aligner OR CBCT AND miniscrew` 执行成 `(aligner OR CBCT) AND miniscrew`，398 条，说明从左到右计算。
- `(miniscrew aligner)[tiab]` 执行成 `(miniscrew…[All Fields]) AND "aligner"[Title/Abstract]`，标签只给了最后一个词。
- `(miniscrew)[tiab]`、`(review)[pt]` 单词组：标签正常生效，不报。
- `"Malocclusion"[mh]` 37719 条（引号内是描述符名照常映射）；`"Tooth Crowding"[mh]` 0 条（入口词加引号）。后者已有 quotedphrasesnotfound 提示，本轮**不**新增引号 MeSH 规则。

## 默认输出变化（A/B 口径）

- 合法检索式：输出逐字不变。离线回归用 68 条合法写法断言零新增警告（多数逐条实测过），另有 6 条真实系统综述式联网抽查，零新增〔实测 2026-11-13〕。
- 含上述错误的检索式：多出 ⚠️ 行，属于项目允许的「纯新增警告行」。影响所有经 `_note` 出警告的工具（search/count/links/diff/export/check_recall），以及留痕 JSONL 的 warnings。
- `count` 命中 0 条时末尾多一行 `=> …`，与 search 同口径。现有 A/B 脚本 `ab_sr_change.py` 因此 3 组 `count/zero` 不同、46 组逐字相同〔实测 2026-11-13〕。这 3 组差异即本项预期改动。
- 工具描述未改，总字符仍为 2413〔实测 list_tools〕。

## 已知限制

- 白名单之外但实际合法的标签会被误报；遇到时补测后入表。
- 不检查标签值本身，比如 `[au]` 后的人名格式、`[pt]` 后的类型名是否存在。
- 同一日期区间里，只有显式写出的年/月/日会参与比较；`2020/06:2020[dp]` 按 2020-06-01 至 2020-12-31 计，不报。
- `mixed_boolean` 在真实检索式里可能频繁出现。它只提示，不计入 term_degraded。

# 小 bug 修复（1.4.4）

审查报告 P3 档里的 8 处问题。每一处都先在修复前联网复现〔实测 2026-11-13〕，修复后用同样的调用重跑确认。原始输出存于审计工作区 `bugfix-20261113/before.json`，未随仓库发布。

| # | 工具 | 修复前 | 修复后 |
|---|---|---|---|
| 79/57 | check_recall | `term=""` 时会拼成 `() AND (uids)` 去查，报「全找回」，还把原因归到「常见于中文词」 | 不发请求，直接报错「term 不能为空」 |
| 85 | check_recall | `term="42988646[uid]"` 执行式为 `42988646[UID] AND 42988646[UID]`，却被误报 term_ignored | term 只由 `PMID[uid]`/`[pmid]` 构成时不再判为忽略；「常见于中文词」只在 term 含中日文时出现 |
| 80 | mesh | 补充概念记录（SCR，C 开头 UI，例如 Biodentine → tricalcium silicate）被当成描述符；树号显示 `@152523`；上位词是虚根 All MeSH Categories | 标头注明「其中 N 条是补充概念」；名称行提示检索要用 `[nm]`；不显示 `@` 伪树号和虚根上位词；新增「映射主题词」行（来自 `ds_headingmappedtolist`）；JSON 只在 SCR 记录上多一个 `mapped_to` 键 |
| 83 | mesh | `retmax=50` 被夹到 20 之后，仍建议「可增大 retmax（上限 20）」 | 已到上限时改为说明「后续条目本工具取不到，请换更具体的词」 |
| 53 | export | 0 条只输出「命中 0 条」，外加一个 0 字节文件 | 追加一行 `⚠️ 命中 0 条，已写出空文件。`，后接与 count 相同的 0 条解释（「不证明不存在」） |
| 54/84 | export | 「本次约 N 次 HTTP 请求」取的是全局计数器的前后差值，并发的其它工具调用会被算进来 | 改为按单次调用计数（`contextvars`），同一 export 内派生的子任务共用同一个计数器 |
| 56 | fetch_abstracts | 所有号都无效时，正文末尾留下 `1. ` 碎片 | 不含 `PMID:` 行的占位批不再拼入正文 |
| 31/76 | links | `[pubmed_pmc_refs:47 条]` 没有说明，容易被读成「47 篇参考文献」 | 改为 `pubmed_pmc_refs:47 条（PMC 中引用本文的全文，不是参考文献）` |

## 依据

**补充概念的检索字段。** `tricalcium silicate[nm]` 返回 670 条，执行式为 `"tricalcium silicate"[Supplementary Concept]`。若写成 `tricalcium silicate[mh]`，返回 907 条，但执行式被改写成 `"tricalcium"[All Fields] AND "silicates"[MeSH Terms]`〔实测 2026-11-13〕。

**pubmed_pmc_refs 的含义。** 取 29771300 的 pubmed_pmc_refs 前 5 个 PMC 号，用 `pmc_pubmed` 反查回 PubMed，5 个全部落在该文的 `pubmed_pubmed_citedin` 列表中（该列表共 54 条）〔实测 2026-11-13〕。其它 linkname（`_five`、`_combined` 等）的含义未逐一核实，所以只给 pmc_refs 加了说明。

## 兼容性

- **正常调用输出不变。** 默认行为 A/B 49 组逐字一致〔实测 2026-11-13，`ab_sr_change.py` 对比 HEAD〕。输出只在上表列出的出错或误导场景下变化。
- **工具描述未改动。** 描述总长仍为 2563（`inspect.cleandoc` 口径）〔实测 list_tools〕。
- **测试。** 新增 `tests/test_p3_fixes.py`，共 10 条。全套 160 条离线测试在以下环境全部通过：Python 3.13 + mcp 1.29.0、1.30.0、2.3.0，以及 Python 3.10 + mcp 2.3.0。源码 stdio 烟测 PASS〔实测 2026-11-13〕。

## 本批未修

见 [../roadmap.md](../roadmap.md)：mesh 精简档、export 描述措辞（#93）、CSV 全字段等。

---
name: systematic-review
description: 用 pubmed-mcp 做系统综述的检索环节：拆概念查 MeSH、组检索式、已知文献找回测试、检索式对比、取全导出、写 PRISMA-S 检索报告。用户说"做系统综述检索""设计检索策略""验证查全""导出全部文献""写检索报告"时使用。只做 PubMed 检索与机械操作；相关性判断与纳入排除由人按预设标准做，不声称查全率。
---

# 系统综述检索工作流

完整方法学见仓库 docs/systematic-review-workflow.md；工具设计与实测前提见 docs/systematic-review-plan.md。本 skill 是执行入口。

## 先决条件
- 明确的研究问题（PICO）与事先写好的纳入排除标准。
- 金标准集：10–30 篇已知相关文献的 PMID，**多来源**（避免只取某综述参考文献造成循环偏倚）。
- 建议设 `PUBMED_AUDIT_DIR` 开启检索留痕。

## 步骤
1. **拆概念查词**：2–4 个核心概念；每个用 `mesh(词)` 查主题词、入口词、树号、下位词、范围注。下位词决定是否展开（如 Tooth Resorption → Root Resorption）。MeSH 不全时用 tiab 同义词补。
2. **组式**：概念内 OR、概念间 AND；`[MeSH Terms]` + `[tiab]` 双轨；布尔大写、引号只包短语。`count(式)` 看命中数与实际执行式，先读 ⚠️。
3. **验已知集**：`check_recall(式, 金标准PMID)`。missing（present）逐块诊断 `count("概念块 AND pmid[uid]")`，核对语法、字段和标引后判断未匹配原因，调整并重跑。只证明已知集找回，不代表查全率。
4. **迭代对比**：每次改式 `diff(旧, 新)` 看新增/丢失，防止越改越漏。命中多≠更好。查准需按预设标准人工/LLM 筛选后统计。
5. **取全导出**：`export(式, 路径, format)`，pmids/csv/medline。超 9999 自动按发表日期切片；切片计数和可能大于总数（多发表日期记录），去重后核对，警告如实说明。筛选工具导入兼容性以对方文档为准。
6. **写报告**：读 `PUBMED_AUDIT_DIR/searches.jsonl` 整理数据库/平台/日期/完整式/执行式/命中数/限制。PRISMA-S 具体条目以原文为准。PubMed 单库≠全部检索，是否补其它库由研究问题定。

## 演练修订

- `mesh` 同时查看上位词、历史前身词和引入年份；本批返回候选中的字面精确项置顶，不保证小 retmax 已覆盖所有精确项。
- `check_recall` 先读 `term_ignored`、`term_degraded` 和各批 `querytranslations`；失真结果不得当作原检索式命中。未找回不直接证明缺词，应核对字段、限制、语法和标引。
- `links(to="refs")` / `links(to="citedin")` 分别补查参考文献和施引，可能截断且仅限 PubMed 已录入关系。
- CSV 不含摘要；MEDLINE 保留来源已有字段。摘要需另外读取，不能因 CSV 缺字段判定原文无摘要。
- 留痕中的 `input_params` 为原始输入，`request_params` 为实际请求参数；导出或元数据失败不能当作零命中。

## 边界（不可放宽）
- 不声称查全率/查准率；不替用户做纳入排除判断。
- 摘要信息不足标待确认，不擅自排除。
- 工具描述与输出里的 推算/未核实/作废 标记转述时保留。
- 筛选、提取表、PRISMA 图、稿件由 LLM + 文档技能做；PDF 下载走 download-literature。

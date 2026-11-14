---
name: systematic-review
description: 用 pubmed-mcp 做系统综述的检索环节：拆概念查 MeSH、组检索式、已知文献找回测试、检索式对比、取全导出、写 PRISMA-S 检索报告。用户说"做系统综述检索""设计检索策略""验证查全""导出全部文献""写检索报告"时使用。只做 PubMed 检索与机械操作；相关性判断与纳入排除由人按预设标准做，不声称查全率。
---

# 系统综述检索工作流

完整方法学见仓库 docs/systematic-review-workflow.md；工具设计与实测前提见 docs/history/systematic-review-plan.md。本 skill 是执行入口。

## 先决条件
- 明确的研究问题（PICO）与事先写好的纳入排除标准。
- 金标准集：10–30 篇已知相关文献的 PMID，**多来源**（避免只取某综述参考文献造成循环偏倚）。
- 建议设 `PUBMED_AUDIT_DIR` 开启检索留痕。

## 步骤
1. **拆概念查词**：2–4 个核心概念；每个用 `mesh(词)` 查主题词、入口词、树号、下位词、范围注。下位词决定是否展开（如 Tooth Resorption → Root Resorption）。MeSH 不全时用 tiab 同义词补。
2. **组式**：概念内 OR、概念间 AND；`[MeSH Terms]` + `[tiab]` 双轨；布尔大写、引号只包短语。`count(式)` 看命中数与实际执行式，先读 ⚠️。
3. **验已知集**：`check_recall(式, 金标准PMID)`。missing（present）逐块诊断 `count("概念块 AND pmid[uid]")`，核对语法、字段和标引后判断未匹配原因，调整并重跑。只证明已知集找回，不代表查全率。
4. **迭代对比**：每次改式 `diff(旧, 新)` 看新增/丢失，防止越改越漏。命中多≠更好。查准需按预设标准人工/LLM 筛选后统计。
5. **取全导出**：`export(式, 路径, format)`，pmids/csv/medline/ris（ris 带全作者，供文献管理软件导入）。超 9999 自动按发表日期切片；切片计数和可能大于总数（多发表日期记录），去重后核对，警告如实说明。筛选工具导入兼容性以对方文档为准。
6. **写报告**：读取 `PUBMED_AUDIT_DIR` 下实际日志（新文件为 `searches-<pid>-<session>.jsonl`，兼容旧 `searches.jsonl`），用 `--audit-report <日志...> --out <报告.md或.csv>` 整理记录表。固定只读旧文件名的说明已作废〔本次实现订正；步骤编号为本项目自订〕。先检查 `audit_status` 与输入完整性；不能把试检命中数相加为纳入数。PRISMA-S 具体条目以原文为准，报告不声称全部合规。PubMed 单库≠全部检索。

## 演练修订

- 初看题名用 `search` 默认 brief；要引用信息显式用 full，只处理名单用 ids。仅需题录时直接 search，不先 count 再重复 search；批量摘要用 fetch_abstracts 的 out_path 落盘，按需读取。
- 改式后用 `diff(..., preview=True)` 预览新增和未再命中的标题与全部文献类型，按 max_show 限制每侧预览；JSON 完整 PMID 列表不受此限制。预览按返回顺序，不是随机样本，也不是相关性排序；判断价值须读摘要，缺摘要保留待确认。
- 按共同、新增、未再命中分组查看，记录人群、干预和研究类型是否切题；不把新增数当有效文献数，不把本轮命中倒充独立金标准。需要正式抽样时另存明确抽样方法与名单。
- removed 仅是旧集合有、新集合没有，不代表数据库删除；补检入库区间之外的旧文献也可能被列入。完整基线应保留，补检名单和新增名单不能替换它。

- `mesh` 同时查看上位词、历史前身词和引入年份；本批返回候选中的字面精确项置顶，不保证小 retmax 已覆盖所有精确项。
- `check_recall` 先读 `term_ignored`、`term_degraded` 和各批 `querytranslations`；失真结果不得当作原检索式命中。未找回不直接证明缺词，应核对字段、限制、语法和标引。
- `links(to="refs")` / `links(to="citedin")` 分别补查参考文献和施引；默认文本有展示上限，`out_path` 保存本次全量来源关系，仍仅限 PubMed 已录入关系。
- 补检用 `datetype="edat"` 配显式区间；新旧名单用 `--diff-files` 比较，再 `--export-pmids` 导出新增。单轮追引用用 `--snowball`；差集不能当完整基线，分页引用 JSON 不能当全量名单。
- `export(metadata=True)` 保存伴随说明，`--verify-export` 核对哈希；只证明文件对应，不证明查全。题录续跑用 `--checkpoint-dir` / `--resume-export`，详情见 docs/retrieval-files.md。
- CSV 不含摘要；MEDLINE 保留来源已有字段。摘要需另外读取，不能因 CSV 缺字段判定原文无摘要。
- 留痕中的 `input_params` 为原始输入，`request_params` 为实际请求参数；导出或元数据失败不能当作零命中。

## 交付闭环

完成策略准备后，按 `docs/review-delivery.md` 将研究问题、标准、检索式、独立已知集来源及范围决策写入方案。研究者未批准策略时保留 `draft`，不得代填批准。缺少研究问题时先整理待确认项，不把演示主题冒充用户研究。

用 `pubmed-review --review-plan <方案.json> --out-dir <新目录>` 生成完整基线、题录摘要、待筛选表、RIS、留痕及交付说明；中断后 `--resume-review <目录>`，完成后 `--verify-review <目录>`。源码安装可用 `python -I pubmed_mcp.py` 加同样参数。入口自动开启本次留痕。

首先阅读 `HANDOFF.md` 和 `delivery.json`。只有 `ready_for_screening` 才能表述“PubMed 候选材料可进入筛选”；`needs_methodology_review` 要列出具体研究决策，`needs_execution_repair` 继续处理执行缺口。没有交付清单不能称完成。将整份目录作为可追溯交付，原筛选表保留，另存工作副本供研究者判断。

查词、迭代和其它数据库证据仍需按上述步骤保存；交付入口不自动设计检索式，也不代替外部数据库检索、引文补查或正式纳入排除。

## 边界（不可放宽）
- 不声称查全率/查准率；不替用户做纳入排除判断。
- 摘要信息不足标待确认，不擅自排除。
- 工具描述与输出里的 推算/未核实/作废 标记转述时保留。
- 筛选、提取表、PRISMA 图、稿件由 LLM + 文档技能做；PDF 下载走 download-literature。

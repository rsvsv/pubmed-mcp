# RIS 导出（1.4.3）

对应审查报告的三条问题：
- #37：没有 RIS/BibTeX，CSV 不是 Zotero/EndNote 的导入格式。
- #34 #67：只有前 3 位缩写作者。
- #66：在线优先文献丢文章号。

## 做法

`export(format="ris")` 复用 1.4.2 的 efetch XML 解析层 `_efetch_xml_all`，每条记录渲染一个 RIS 块，以 `ER  - ` 结尾，只写非空字段。其它格式与默认行为不变：A/B 49 组逐字一致〔实测 2026-11-13〕。

| RIS | 来源（efetch XML） |
|---|---|
| TY | `JOUR`；PubmedBookArticle 为 `CHAP` |
| TI / AB | ArticleTitle / AbstractText（结构化段落写成 `LABEL: text` 拼接） |
| AU | 每位作者一行「姓, 名」（LastName, ForeName）；团体作者（CollectiveName）原样 |
| T2 / JO | 期刊全名 / ISO 缩写；书为书名（T2）+ 出版社（PB） |
| VL / IS / SP / EP | 卷 / 期 / 起止页。MEDLINE 缩写页码 `637-41` 展开为 `637`–`641`。无页码时 SP 写文章号（ELocationID pii，如 BMC 的 `1960`） |
| SN | ISSN |
| PY / DA | 出版年 / `YYYY/MM/DD/`（缺位留空） |
| LA / DO / AN / C2 / UR | 语言 / DOI / PMID / PMCID / PubMed 链接 |
| KW | MeSH（带副标题，主要主题加 `*`）与作者关键词 |
| M3 | 全部文献类型（含 Retracted Publication） |
| N1 | 撤稿、撤稿声明、关注声明、勘误、更新关联：`RetractionIn: <来源>（PMID n）` |

## 验证

- **rispy 0.10.0 解析**：14 条混批全部识别。样本覆盖 Cochrane、中文刊、团体作者、撤稿与关注声明、StatPearls 与 GeneReviews 书、无摘要 Letter、无页码 BMC、撤稿声明〔实测 2026-11-13〕。
- **联网对拍**：`"root resorption"[tiab] AND deep learning[tiab]` 共 20 条，RIS 与同次 MEDLINE 导出逐条比对作者数、DOI、卷、摘要有无，0 处不符〔实测 2026-11-13〕。另有 4 条首作者名不同：RIS 保留原文变音字母（`Demirel, Anıl`、`Özden, Samet`），MEDLINE 的 FAU 把它们转写为 ASCII（`Anil`、`Ozden`）。这是两种数据源本身的差异，不是错误。
- **离线回归**：`tests/test_abstract_modes.py` 的 `Ris` 类共 6 条，覆盖每块 TY 开头 ER 结尾、全名与团体作者、文章号、撤稿注记与书、缩写页码展开、export 端到端含缺号。
- **全套离线**：150 条。mcp 1.29.0 / 1.30.0 / 2.3.0 均通过，源码 stdio 烟测 PASS〔实测 2026-11-13〕。

## 未做 / 限制

- **未做 Zotero/EndNote 真机导入**。字段映射按 RIS 通行规范，rispy 能解析，但不等于各软件导入后字段位置都如预期。
- **CSV 未改**：仍是 10 列、前 3 位缩写作者，以保持 1.4.0 输出逐字不变。要全作者请用 ris。
- **不生成 Vancouver/APA 引文**：交给文献管理软件（§7「不替代文献管理平台」）。
- **search 的 JSON 不加语言/全刊名字段**：会改动现有 as_json 输出。需要时用 `fetch_abstracts(fields="journal,lang", as_json=True)`。
- **工具描述总长**：仍为 2571，export 说明改写后长度不变〔实测 list_tools〕。（2571 是 Python 3.13 下的原始长度；后来测试改按 `inspect.cleandoc` 口径，同一版本为 2563〔实测 2026-11-13〕。）

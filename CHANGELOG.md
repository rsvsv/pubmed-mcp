# 更新记录

版本号是本项目自定的，以 `pyproject.toml` 中的 `project.version` 为准。各版本的 tag（`v1.3.0` 起）指向对应提交。
后续计划见 [docs/roadmap.md](docs/roadmap.md)。

## 1.4.5 — 2026-11-13

处理一份外部审查（基于 1.4.4 sdist）中复现成立的问题。详情见 [docs/history/review-1.4.4-fixes.md](docs/history/review-1.4.4-fixes.md)。

- **参数校验：** 拼错或多出的参数名改为报错，不再被 SDK 静默丢弃后按默认值照跑；`tools/list` 的 schema 带 `additionalProperties: false`。
- **PMID 解析：** `1,234, 567` 这类带空格的千分位写法也会被拦下。
- **fetch_abstracts：** 单次最多 2000 个 PMID〔本项目自订〕。
- **小 bug：** 短 key 掩码不再打花诊断文本；无卷号时题录不再输出 `:123-30`；as_json 的 `ptag` 不再带 `｜`；export 不再打印空的执行式行，超长执行式截到 600 字；MEDLINE 导出不再写入无效号的 `1. ` 占位；写文件与 sha256 移出事件循环。
- **CI：** 增加 Windows。

## 1.4.4 — 2026-11-13

修复 8 处小 bug。正常调用的输出和工具描述都没有变化。详情见 [docs/history/p3-fixes.md](docs/history/p3-fixes.md)。

- **check_recall：** 检索式为空时直接报错。检索式只由 `PMID[uid]` 组成时，不再误报 `term_ignored`。
- **mesh：** 能识别补充概念记录（SCR），会给出它映射到的主题词，并提示检索时用 `[nm]`。不再输出以 `@` 开头的伪树号。retmax 已经到上限时，不再建议「增大 retmax」。
- **export：** 0 条结果时补充说明原因。HTTP 请求数改为按单次调用统计，并发调用之间不再互相串账。
- **fetch_abstracts：** 所有 PMID 都无效时，输出末尾不再残留 `1. `。
- **links：** 在 `pubmed_pmc_refs` 旁注明它指的是「PMC 中引用本文的全文」。

## 1.4.3 — 2026-11-13

- `export(format="ris")`：每条记录包含全部作者全名、摘要、卷期页或文章号、ISSN、DOI、MeSH 与关键词，并附撤稿注记。详情见 [docs/ris-export.md](docs/ris-export.md)。
- 工具描述体积测试改用 `inspect.cleandoc` 计算长度，这样在 Python 3.10 和 3.13 上结果一致。

## 1.4.2 — 2026-11-13

- `fetch_abstracts` 新增三个参数：
  - `fields`：精简输出，只返回指定字段。
  - `as_json`：从 XML 解析出结构化 JSON。
  - `out_path`：把结果写入文件，只返回文件路径和 sha256。
- 默认输出的正文与之前逐字一致。输出超过 6 万字符时，末尾多一行提示。
- 详情见 [docs/abstract-slim.md](docs/abstract-slim.md)。

## 1.4.1 — 2026-11-13

- 本地检查检索式语法。以下写法都会被 NCBI 静默改写而不报错，现在会给出提醒：拼错的字段标签、`AND NOT`、前导截词 `*screw`、`(A OR B)[tiab]`、`2020-2024[dp]`、AND 与 OR 混用。详情见 [docs/query-lint.md](docs/query-lint.md)。
- `count` 返回 0 条时，会说明可能的原因。

## 1.4.0 — 2026-11-12

- 新增系统综述检索助手：`check_recall`、`mesh`、`diff`、`export`。
- 新增可选的检索留痕：设置 `PUBMED_AUDIT_DIR` 后生效。
- 修复 [issue #1](https://github.com/rsvsv/pubmed-mcp/issues/1)：以引号短语开头的检索式会被误报警告。
- 验收记录见 [docs/history/sr-tools-validation.md](docs/history/sr-tools-validation.md) 和 [docs/history/release-1.4.0.md](docs/history/release-1.4.0.md)。

## 1.3.0 — 2026-11-11

- 整合响应边界修复、可选的精简输出（`detail`、`as_json`）和自动回归（GitHub Actions）。
- 详情见 [docs/history/boundary-fixes.md](docs/history/boundary-fixes.md) 和 [docs/history/output-modes-and-ci.md](docs/history/output-modes-and-ci.md)。

1.3.0 之前的版本没有打 tag，演进过程见 git 历史和 [docs/history/release-delta.md](docs/history/release-delta.md)。

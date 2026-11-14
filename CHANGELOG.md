# 更新记录

版本号是本项目自定的，以 `pyproject.toml` 中的 `project.version` 为准。各版本的 tag（`v1.3.0` 起）指向对应提交。
后续计划见 [docs/roadmap.md](docs/roadmap.md)。

## 未发布

- `search` 默认改为 brief，保留标题、日期和全部文献类型；需要原完整 JSON 字段的调用方应显式使用 `detail="full"`，ids 仍按需跳过元数据。
- `diff` 新增默认关闭的 `preview`，按 `max_show` 展示新增和未再命中的标题、日期、全部文献类型；完整 PMID 集合不截断，预览缺失或失败明确提示并保留差集。
- 在线查询对比及离线名单差集明确解释 removed 不代表数据库删除，补检区间名单不能替换完整基线；使用指引增加分层抽查与按需输出。

- 完成网络传输、检索业务、文件操作、导出及恢复状态机、命令行诊断的模块拆分；核心保留 SDK 兼容、注册、配置与显式依赖连接，原兼容名称继续可替换。
- 新增 `pubmed_services.py` 综述服务接口；综述编排不再直接调用核心私有成员，审计目录与日志会话统一在上下文退出时恢复，仍限定独立 CLI 进程运行。
- 完整运行文件清单与打包清单同时覆盖新增模块，未完成任务继续严格核对实现指纹。
- 拆分复核发现并修复诊断探针的动态命名空间访问遗漏；临时网络替身与恢复均写回当前核心实例，新增直接执行探针离线故障用例的回归测试。

- 运行时审计抽取到 `pubmed_audit.py`，独立核心实例各持有审计上下文、请求计数和日志锁；兼容入口在调用时解析可替换的写入与掩码操作，保留综述续跑切换日志会话的行为。
- 修复审计结束时间获取或字段整理异常时可能跳过上下文恢复的问题；恢复保护覆盖完整生命周期，保留原异常传播语义。

- 离线审计报告抽取到 `pubmed_report.py`；当前核心显式传入文件操作、异常类型和掩码，保留原命令与日志校验兼容入口。

- 抽取纯检索诊断 `pubmed_query.py` 与 XML 题录解析/格式化 `pubmed_records.py`；原核心保留兼容名称。当时“网络、审计和导出调度暂不迁移”的阶段描述已作废：这些模块在后续拆分中完成迁移。〔作废日期：2026-11-14；来源：当前源码模块边界〕
- 新增相邻模块加载与完整实现指纹 `pubmed_runtime.py`；未完成综述任务及修复任务续跑核对全部运行模块，已完成旧交付仍可离线核验。

- 新增统一 `--help` / `-h`，帮助与执行共用参数定义；严格拒绝命令冲突、未知/缩写参数、多余参数及错误归属的选项。
- 将命令行解析与分派拆到 `pubmed_cli.py`，保留 `pubmed_mcp.main` 和无参数 stdio 入口；源码文件部署需包含完整相邻模块。
- README 首屏明确 MCP 与命令行的能力边界；模块及安装说明见 [命令行与模块结构](docs/cli-and-modules.md)。

- 检索入口校验 ESearch 计数、PMID 列表和分页回显，异常空页、短页、重复或无效编号明确报错；合法计数模式、零命中及越尾请求继续可用。
- 导出保留分页、切片和结束重查的警告及 `retrieval_checks`；中途计数变化不因首尾一致而消失。查询对比保留后续页警告。
- 已完成但缺题录的综述交付支持独立修复目录，仅补缺失并保留原快照；坏日志显式报告，历史审计缺口继续阻止交付放行。
- 交付核验增加必需阶段和跨文件内容绑定；可靠性验收记录见 [说明](docs/reliability-repairs.md)。

- 新增导出伴随说明和离线文件核验；数据与说明分别发布，说明失败保留已导出的数据。
- search/count/export 增加日期类型；支持入库日期补检，相对日期导出先固定 NCBI 回显区间；未验证的 edat 超窗拆片明确拒绝。
- links 增加完整 JSON、全量文件和按源分页；新增名单差集、按 PMID 文件导出与单轮追引用命令。
- 题录导出使用有界批次写盘；新增检查点和恢复，校验已完成批次，损坏批次重抓。合成基准用于记录内存与耗时，不承诺联网提速。

- 新增检索记账与 `--audit-report` 离线报告：独立进程日志、带时区起止时间、事件标识、实际请求、失败/取消记录；工具返回审计状态。可读取旧日志，生成有来源定位的 Markdown/CSV 记录表。详见 [使用说明](docs/audit-report.md)。

- 导出 JSON 与审计留痕增加实际写入数、完整缺失名单和核对状态；CSV 的占位行计入写入数，缺元数据的号同时列入缺失名单。字段口径见 [导出核对说明](docs/export-integrity.md)。
- 导出请求计数上下文在成功、异常和取消后恢复，避免遗留在调用方上下文中。
- 零记录 CSV 改为提示“仅含表头”，其他格式的空文件提示保留。
- 路线图重写为先留痕、再追引用、最后大导出，明确拟定接口、失败处理、交付依赖与验收条件；旧方案标为已作废并归档。记账与离线报告已在工作区实现，其余功能仍属方案。

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

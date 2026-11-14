# 命令行与模块结构

本文说明当前工作区实现；旧发布包不自动更新。命令、参数及模块边界均为本项目自订接口。

## 帮助与参数

```bash
pubmed-mcp --help
```

```bash
python -I pubmed_mcp.py --help
```

```bash
python -I -m pubmed_mcp --help
```

模块方式要求已安装本项目。帮助使用执行命令时同一份参数定义，显示全部命令、专用参数、联网与本地操作提示。`-h` 同样有效；帮助成功退出，不联网、不创建任务。不带参数保持原 MCP stdio 服务行为，不输出命令行帮助或其它协议外内容。

独立的 `--help` / `-h` 优先显示帮助，即使命令尚未写完整；选项终止符 `--` 后的内容不会被预扫描为帮助。综述参数转发保留等号绑定，支持以连字符开头的路径值。

正常执行时命令互斥，参数缩写不接受。拼错的参数、多余的位置参数、不属于该命令的选项以及缺少必需配套参数，均在执行前明确报错。`--out` 等选项可以放在命令前；审计报告仍可重复 `--audit-report` 收集日志。错误退出与正常输出仍走原入口，执行错误保留密钥掩码。

各命令示例见 [补检与文件工作流](retrieval-files.md)、[审计报告](audit-report.md)、[综述交付](review-delivery.md)。MCP 与命令行功能对应表位于 [README](../README.md)。

## 本轮模块边界

| 模块 | 职责 | 依赖方向 |
|---|---|---|
| `pubmed_mcp.py` | SDK 兼容、MCP 注册、配置、依赖连接及历史名称适配 | 加载相邻实现模块；有命令行参数时加载 CLI |
| `pubmed_transport.py` | 密钥读取、掩码、限速、HTTP 重试与线程桥 | 标准库和 anyio；状态及操作由显式依赖合同提供 |
| `pubmed_tools.py` | 检索参数校验、请求编排、检索窗口核对与追引用 | 显式调用当前实例的网络、解析和审计操作 |
| `pubmed_files.py` | 安全写入、哈希、名单读取与差集 | 显式文件发布与校验依赖 |
| `pubmed_export.py` | 切片导出、格式写入、检查点与恢复状态机 | 显式检索、文件和审计依赖 |
| `pubmed_diagnostics.py` | 自检、并发探针与密钥状态说明 | 显式调用当前入口实例；不自行注册 MCP |
| `pubmed_services.py` | 综述使用的命名操作、属性和审计会话上下文 | 只使用标准库；接收限定操作集合，不导入核心 |
| `pubmed_query.py` | 本地语法检查、实际执行式解释、日期窗口和已知集失真诊断 | 只使用标准库，不导入 MCP 或核心 |
| `pubmed_records.py` | XML 题录解析、字段投影、紧凑文本和 RIS 格式化 | 只使用标准库，不发请求、不读写文件 |
| `pubmed_report.py` | 离线日志校验、来源定位、Markdown/CSV 报告 | 只使用标准库；文件操作与异常类型由调用方显式提供 |
| `pubmed_audit.py` | 审计生命周期、敏感字段清洗、日志写入及实例上下文 | 标准库和 anyio；显式接收回调，不导入核心或 MCP SDK |
| `pubmed_runtime.py` | 同目录模块加载、运行文件清单、实现指纹 | 只使用标准库；模块实例不共享可变业务状态 |
| `pubmed_cli.py` | 统一参数定义、帮助、参数归属校验、命令分派 | 接收核心显式传入的操作映射，不重新导入核心 |
| `pubmed_review.py` | 综述方案、阶段编排、续跑、交付核验 | 同目录核心提供服务接口；业务不直接调用核心私有成员，通过独立 CLI 进程运行 |

没有把核心的全局命名空间复制到新模块，也没有用通配符导入。CLI 操作映射在调用时建立，因此命令使用当前核心实例，包括现有测试替换的网络或文件操作。相邻模块加载不依赖工作目录或 `PYTHONPATH`，支持 `python -I` 文件入口。

CLI、检索诊断、题录格式化、报告、运行时审计、网络传输、导出恢复与工具业务均已分离。实现模块不反向导入核心；跨模块操作和可写状态通过声明过的 getter/setter 合同连接，在调用时读取当前值，保留故障注入测试。核心仍保留配置、依赖连接和历史名称，便于现有调用方迁移；薄入口指业务已移出，不表示核心只剩启动函数。

审计与请求计数的 ContextVar 和锁由每个核心实例各自创建。综述通过限定的服务接口调用当前实例，审计会话管理器负责保存及恢复目录与会话标识。审计目录仍取自进程环境变量，综述继续使用独立 CLI 进程，不能据此声称支持同进程并发综述目录隔离。

旧阶段说明“网络传输、限速和导出调度仍在核心，综述仍复用部分内部函数，尚未完成全部分层”已作废：后续拆分已完成这些迁移。〔作废日期：2026-11-14；来源：上述职责表所列源码〕

综述的新任务与独立修复任务在状态中记录 `implementation_sha256`，覆盖全部运行模块。未完成任务恢复时，缺少该映射或任一模块变化均明确拒绝，沿用原安装快照恢复或新建任务；不能自动改写旧任务指纹。已完成交付仍走原离线核验路径，旧格式不因缺少新指纹字段而被拒；完成但缺题录的交付建立独立修复目录，并记录当前实现。

## 安装与部署

pip 安装的 console-script 仍指向 `pubmed_mcp:main`，`pubmed-review` 入口不变。wheel 与源码发行包须包含 `pubmed_mcp.py`、`pubmed_cli.py`、`pubmed_review.py`、`pubmed_runtime.py`、`pubmed_query.py`、`pubmed_records.py`、`pubmed_report.py`、`pubmed_audit.py`、`pubmed_transport.py`、`pubmed_files.py`、`pubmed_export.py`、`pubmed_diagnostics.py`、`pubmed_tools.py`、`pubmed_services.py`；构建配置已将它们列入运行模块，运行期清单位于 `pubmed_runtime.RUNTIME_FILES`。

按文件部署必须将上述模块放在同一目录。仅复制核心文件后运行带参数命令，会明确提示缺少相邻模块。当前便携发布包和本机已有安装不会随仓库编辑更新；重新打包或同步时须包含全部运行模块，不能复用仅复制旧文件集合的历史脚本。

## 验证范围

`tests/test_cli.py` 覆盖帮助无执行副作用、无参数 stdio、参数归属错误、同一核心实例调用、从其它目录以隔离模式启动、缺失 CLI 模块以及离线差集/审计调用。`tests/smoke_installed.py` 检查安装后的模块位置、console-script 与模块方式帮助，并保留既有文件流程和 stdio 烟测。

本轮验收记录如下；日志保存在工作区 `pubmcp-audit/`，不是已发布包或远端 CI 的通过证明。

- 两套本地兼容环境各执行 372 项回归，结果均为 `OK`，各跳过 2 项因 Windows 权限不足而无法创建符号链接的检查。〔实测/工具输出：`cli-refactor-sdk1-final.log`、`cli-refactor-sdk2-final.log` 末尾汇总及 skipped 行〕
- 首轮回归暴露不完整命令后跟帮助会先报缺参数，已修复为帮助优先；首轮失败日志保留为 `cli-refactor-sdk1-offline.log`、`cli-refactor-sdk2-offline.log`。〔实测/工具输出：日志的 `test_help_has_all_modes_and_no_effects` 与最终回归结果〕
- 初次关闭构建隔离时因本地缺少 setuptools 构建后端失败；改用项目声明的隔离构建后成功。wheel 内全部运行模块与源码逐字节一致。〔实测/工具输出：`cli-refactor-build.log`、`cli-refactor-build-isolated.log` 及主流程 ZipFile 字节核验〕
- 安装后的 console-script、隔离模块入口帮助、离线报告、名单差集/导出/检查点/核验及真实 stdio 初始化、工具列表和错误响应均通过。环境为 Python 3.13.14 / MCP SDK 1.30.0 与 Python 3.10.20 / MCP SDK 2.3.0。〔实测/工具输出：主流程解释器版本查询；`cli-refactor-sdk1-installed.log`、`cli-refactor-sdk2-installed.log` 的 Installed versions 与 PASS 行〕
- 经新主入口执行原真实课题的 `--verify-review`，返回 `verified`，交付状态仍为 `needs_methodology_review`；该状态不证明完成研究方法学审核。〔实测/工具输出：`cli-refactor-review-route.log`〕

- 复核后修复综述路径转发丢失等号绑定的问题；主流程已复现原错误，并新增覆盖创建、续跑、核验入口的路径回归。修复后两套环境各执行 373 项，均为 `OK`，各跳过 2 项符号链接权限检查。前述较早回归记录保留，不作为最终源码的验收替代。〔实测/工具输出：主流程 `--verify-review=-missing-review` 复现；`cli-refactor-sdk1-verified.log`、`cli-refactor-sdk2-verified.log` 的对应测试与末尾汇总〕
- 路径修复后的 wheel 已重新构建，全部运行模块再次逐字节匹配当前源码；重新安装到上述兼容环境后，帮助、离线文件流程与真实 stdio 烟测全部通过。〔实测/工具输出：`cli-refactor-verified-build.log`、主流程 ZipFile 字节核验、`cli-refactor-sdk1-verified-install.log` 与 `cli-refactor-sdk2-verified-install.log` 的 PASS 行〕

## 纯诊断与题录模块拆分验收

- 全量离线回归在兼容环境各执行 378 项，均为 `OK`，各跳过 2 项因 Windows 符号链接权限不足的检查。包含未完成任务指纹变化/缺失、独立修复任务指纹和已完成旧交付兼容。〔实测/工具输出：`pure-modules-sdk1.log`、`pure-modules-sdk2.log` 末尾汇总与对应测试〕
- 迁移函数的 AST 与保留的拆分前源码一致；这是函数体未重写的检查，完整调用行为另由回归测试覆盖。〔实测/工具输出：`pure-modules-equivalence.log`〕
- 纯诊断与题录模块在禁用 site-packages 的隔离解释器中独立加载，未初始化 MCP；运行文件指纹覆盖各模块，缺失文件明确失败。〔实测/工具输出：上述回归日志中的 `test_module_boundaries`〕
- 原真实课题交付离线核验返回 `verified`，交付状态仍为 `needs_methodology_review`。〔实测/工具输出：`pure-modules-real-review.log`〕
- 纯模块阶段 wheel 构建成功，全部运行模块与当时源码逐字节匹配。〔实测/工具输出：`pure-modules-build.log` 和主流程 ZipFile 字节核验〕
- 该阶段安装后的帮助、离线报告、文件流程及真实 stdio 烟测均通过；后续报告模块抽取需重新验收。〔实测/工具输出：`pure-modules-sdk1-install.log`、`pure-modules-sdk2-install.log` 的 PASS 行〕

## 离线报告模块拆分验收

- 日志校验和报告生成函数体，经显式依赖名称替换后与拆分前 AST 一致；迁移脚本写入后逐文件核对内容。〔实测/工具输出：`pubmcp-audit/extract_report_module.py` 执行结果；原源码保存在 `report-module-baseline-wsujhoxc/pubmed_mcp.py`〕
- 两套兼容环境各运行 379 项回归，均为 `OK`，各跳过 2 项 Windows 符号链接权限检查。新增报告使用当前核心回调的验证，原日志坏行、冲突、转义和产物绑定检查继续通过。〔实测/工具输出：`report-module-sdk1.log`、`report-module-sdk2.log` 对应测试与末尾汇总〕
- wheel 构建成功，所有运行模块与源码逐字节匹配；安装后帮助、离线报告、文件流程和真实 stdio 烟测通过。〔实测/工具输出：`report-module-build.log`、主流程 ZipFile 核对、`report-module-sdk1-install.log` 与 `report-module-sdk2-install.log` 的 PASS 行〕
- 本阶段没有构建或验收源码发行包；wheel 验证不替代源码发行包验证。

## 运行时审计拆分验收

- 两套兼容环境各运行 383 项回归，均为 `OK`，各跳过 2 项 Windows 符号链接权限检查。〔实测/工具输出：`audit-module-sdk1-final.log`、`audit-module-sdk2-final.log` 末尾汇总与 skipped 行〕
- 新增核心实例间上下文隔离、装饰后会话/掩码变化、禁止 SDK/核心导入，以及完成时间/写入器异常后恢复父上下文的回归。原并发、嵌套、取消、请求计数和综述恢复测试继续通过。〔实测/工具输出：上述日志的 `test_module_boundaries`、`test_audit_report`、`test_followup_repairs` 及相关回归〕
- 本阶段包含一项行为修复：原审计完成时间或字段整理异常可能跳过 token 恢复，现将恢复保护扩大到完整生命周期；不再声称本阶段仅做函数搬移。〔源码依据：`pubmed_audit.py` 的 `audited` 外层 finally；实测/工具输出：`test_audit_restores_parent_when_finalization_fails`〕
- wheel 构建成功，全部运行模块逐字节匹配源码；安装后的帮助、报告、文件流程及真实 stdio 烟测通过。〔实测/工具输出：`audit-module-build.log`、主流程 ZipFile 核验、`audit-module-sdk1-install.log` 与 `audit-module-sdk2-install.log` 的 PASS 行〕
- 原真实课题交付返回 `verified`，仍为 `needs_methodology_review`。〔实测/工具输出：`audit-module-real-review.log`〕
- 本阶段未构建源码发行包，也未重新联网评估检索结果；这些检查不证明查全率或完成方法学审核。

## 剩余模块拆分验收

- 网络传输、文件操作、导出恢复、诊断及工具业务的 115 个函数与限速器，经显式依赖、self、全局状态访问和文档缩进归一化后，AST 与拆分前基线一致。这只验证机械迁移，不能替代调用行为测试。〔实测/工具输出：`remaining-equivalence.log`；基线 `remaining-modules-baseline-_612voh6/pubmed_mcp.py`〕
- 首轮集成因清单已包含但服务文件尚未生成而失败，失败日志保留；服务接口接入后回归通过。〔实测/工具输出：`remaining-first.log`、`remaining-integrated.log`〕
- 独立复核提出诊断探针仍使用旧全局命名空间；主流程回原源码并复现诊断模块缺少对应属性，已将临时替换与恢复接入显式可写依赖。新增测试直接执行探针实际的离线故障用例，并禁止原网络路径。〔实测/工具输出：主流程命名空间复现；`test_probe_offline_fault_cases_patch_and_restore_current_core`〕
- 修复后两套兼容环境各执行 389 项网络阻断回归，均为 `OK`，各跳过 2 项 Windows 符号链接权限检查。新增依赖合同、多实例网络状态、综述接口动态绑定、会话异常/取消恢复及禁止动态命名空间访问检查。〔实测/工具输出：`remaining-sdk1-verified.log`、`remaining-sdk2-verified.log` 末尾汇总及 `test_module_boundaries`〕
- 修复后的源码发行包构建成功，并从该包构建 wheel；两个包中的全部 14 个运行文件与当前源码逐字节一致，wheel 没有额外 Python 模块。〔实测/工具输出：`remaining-build-verified.log`、`remaining-packages-verified.log`；仅指运行源码字节，验收文档在验证完成后补记〕
- 最终 wheel 安装到两套兼容环境后，模块位置、完整指纹、帮助、离线报告、名单差集/导出/检查点/核验及真实 stdio 工具列表和错误响应均通过。〔实测/工具输出：`remaining-sdk1-verified-install.log`、`remaining-sdk2-verified-install.log` 的 PASS 行〕
- 原真实课题经最终实现离线核验返回 `verified`，状态仍为 `needs_methodology_review`。〔实测/工具输出：`remaining-real-review-verified.log`〕
- 本轮未重跑联网检索或完整联网探针；离线回归与文件核验不证明实时 NCBI 可用性、查全率或方法学审核完成。

拆分阶段仅在隔离环境验证的记录保留为历史范围。后续已将完整运行模块同步到本机独立安装目录，更新前保存原文件备份；本机实际解释器的 stdio 注册、错误响应、帮助、实现指纹及原交付核验通过。已连接的客户端需重启服务进程才会加载新代码。〔实测/工具输出：`production-modules-deploy.log` 与 `production-modules-deploy-final.log` 的 Backup 与 PASS 行〕

推送前统一运行源码的换行并清理末尾空行，Git 暂存内容、工作区源码和本机安装文件逐字节一致。前述构建包属于换行统一之前的验收快照，不作为本次推送字节指纹的替代。〔实测/工具输出：主流程 Git blob／源码／本机文件字节核对〕

仓库源码推送与版本发布包是不同交付物；本次同步不替换既有 GitHub Release 附件。

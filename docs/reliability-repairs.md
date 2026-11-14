# 可靠性修复验收记录

当前状态：本轮已确认的可靠性缺口已修复，离线回归、协议故障验收、联网边界检查及旧课题核验通过；随后已按用户要求同步本机安装并通过新进程验证，当前客户端连接需重启加载。GitHub 发布未更新。验收范围与限制见下文。

历史状态已作废：原“修复与验收进行中，⚠️【待确认】”仅记录修复前状态；作废理由为本轮验收已有实际结果〔本项目记录日期：2026-11-14〕，不删除历史失败日志。

## 修复前已复现

- 异常响应声称有命中却提供空名单时，真实 MCP 调用返回成功，并显示没有匹配记录。来源：`pubmcp-audit/reliability-before-wire.log` 与 `pubmcp-audit/reliability-before-wire-sdk1.log` 的断言失败；两代 SDK 均复现，使用本项目自订模拟响应，不代表 NCBI 实际发生了此故障。
- 后续页带有警告时，导出结果遗漏该警告，完整性字段仍标为未发现差异。来源：`pubmcp-audit/reliability-before-pagination-wire.log` 的断言失败；使用本项目自订模拟响应。
- 已生成交付清单的恢复入口直接核验，不补抓缺失题录。来源：`pubmcp-audit/reliability-before-recovery.log` 的断言失败；本项目自订缺题录夹具。修复前源码保留在 `pubmcp-audit/reliability-baseline-9zuq34ir`。
- 删除必需的已知集检验阶段并重新封装文件哈希后，旧核验仍接受。来源：`pubmcp-audit/reliability-before-stage-validation.log` 的断言失败；本项目自订异常清单，不表示正常运行曾生成此缺口，也不是签名绕过。
- 未完成交付含截断日志尾行时，旧恢复入口抛出 JSON 解析错误。来源：`pubmcp-audit/reliability-before-audit-recovery.log`；本项目自订故障注入。

## 接口形状核对

正常检索、只查计数、翻过结果末尾和零命中的 ESearch 返回形状已联网核对。来源：`pubmcp-audit/reliability-live-envelope.log`；请求为本项目自订验收输入。计数模式与越页的空名单合法，校验须结合实际请求而非仅看名单为空。

窗口末页会缩短返回条数并附带限制提示。来源：`pubmcp-audit/reliability-live-window.log`；请求为本项目自订边界验收输入。缩短后的返回量应按窗口约束核对，限制提示仍须保留。

## 验收要求

以下为本项目自订验收口径：

- 文献编号名单必须是有效、可核对的列表；异常响应不得解释为主题没有文献。合法的计数模式、越过结果末尾及零命中继续可用。
- 后续页、切片及结束重数的警告须出现在最终输出、伴随说明和已开启的审计记录里。收到计数变化时明确降级，不能因最终数量碰巧一致抹去异常证据。
- 已完成但缺题录的交付能继续补抓，保持原始固定名单和已有材料；不能以补抓成功代替检索或审计缺口修复。
- 损坏日志原始字节保留，来源与问题显式报告；缺失、重复或改变的目标审计事件仍须拒绝复用。
- 交付核验推导并核对必需阶段，不能仅检查状态文件里已经列出的阶段。
- 修复后的代码继续核验既有真实课题材料，并确认原材料字节未改变。

## 修复前回归基线

- 保存的旧源码基线完整回归运行 326 项，通过，跳过 2 项真实文件符号链接权限检查〔实测/工具输出：`pubmcp-audit/reliability-baseline-offline-complete.log` 的汇总〕。现有回归通过并不覆盖上述新增故障场景。
- 首次运行备份基线因缺少项目元数据失败，补齐 `pyproject.toml` 后通过；失败记录保留在 `pubmcp-audit/reliability-baseline-offline.log`，不是待修源码引起的新回归。

## 修复后验收

- 离线回归在 Python 3.10 / MCP SDK 2 与 Python 3.13 / MCP SDK 1 环境各运行 364 项，均通过，各跳过 2 项主机无权限创建真实符号链接的检查〔实测/工具输出：`pubmcp-audit/reliability-sdk2-second.log` 与 `pubmcp-audit/reliability-sdk1-second.log` 汇总；环境与测试组合为本项目自订验收口径〕。
- 真实 stdio 协议验收通过：异常名单在各输出模式中报错，计数请求合法，后续页警告经导出、伴随说明及查询对比的文本/JSON 输出保留〔实测/工具输出：上述日志的 source/pagination protocol regressions〕。
- 缺失题录补抓、必需阶段校验、截断日志恢复、阶段与产物绑定以及原真实课题离线核验通过〔实测/工具输出：上述日志各 CHECK/EXIT；故障均为本项目自订注入，非真实 NCBI 故障统计〕。
- 修复后联网检查普通页、计数模式、越尾、零命中候选词、窗口末页，以及导出伴随说明与哈希核验通过〔实测/工具输出：`pubmcp-audit/reliability-live-after.log`；请求为本项目自订验收输入〕。
- 原真实课题文件与修复前哈希清单完全一致〔实测/工具输出：主流程比对 `pubmcp-audit/reliability-real-delivery-before.json`，输出 `PASS original real-topic delivery byte hashes unchanged`〕。课题仍需方法学复核，代码测试不能替代研究判断。
- 首轮修复后验收曾失败：查询对比未传回后页警告，补抓路径对象使审计序列化失败。已修正调用参数和警告传递并增加回归；失败日志保留在 `reliability-sdk1-first.log`、`reliability-sdk2-first.log`，不作为当前通过结果。

## 独立复核与补充修复

独立复核提示的报告展示列矛盾与空筛选文件中断问题，已由主流程新增故障测试亲自复现〔实测/工具输出：`pubmcp-audit/reliability-review-edge-before.log`；输入为本项目自订夹具，不以子代理判断替代证据〕。现已增加报告全部展示列的原日志重建比对，保留整体搬移目录的兼容性；修复目录中的空筛选残留先归档再重试，人工判断保护继续保留。补充修复后的协议、恢复与真实课题验收通过〔实测/工具输出：`reliability-sdk1-final.log` 与 `reliability-sdk2-final.log`〕；当轮离线测试仅剩错误提示断言不符，已将原始 JSON 解析错误包装为明确的审计报告错误。该失败日志保留，最终离线重跑结果另行记录。

最终离线回归在上述环境各运行 366 项，均通过，各跳过 2 项真实符号链接权限检查〔实测/工具输出：`pubmcp-audit/reliability-sdk1-final-offline.log` 与 `pubmcp-audit/reliability-sdk2-final-offline.log` 汇总〕。独立复核提出的问题已由主流程复现、修复并回归；此前“独立代码复核待确认”状态已作废，理由为已取得并验证结果〔本项目记录日期：2026-11-14〕。

## 范围

本轮针对可靠性，不改变用户的研究批准、筛选判断或多数据库方法学要求。仓库源码已修改，验收覆盖上述场景，不构成不存在其它故障的保证。历史状态已作废：“未同步本机安装入口”；作废理由为用户随后批准同步，安装文件已备份并更新〔本项目记录日期：2026-11-14〕。安装文件与验收源码逐字节一致，新进程的 stdio 初始化、工具注册、异常返回和真实课题核验通过〔实测/工具输出：`pubmcp-audit/local-reliability-sync.json`、`pubmcp-audit/local-reliability-install-check.log`〕。备份位置见同步记录；启动配置与密钥未改。当前客户端已连接的旧进程仍需重启加载。本轮未提交或发布 GitHub 新包。

# 综述交付入口验收

本页记录工程验收，不是用户综述的学术验收。验收规则、合成题录和演示范围为本项目自订。

- 双环境完整离线回归各运行 326 项，均 OK，各跳过 2 项受主机权限限制的真实文件符号链接测试〔实测/工具输出：pubmcp-audit/review-delivery-v1.log、review-delivery-v2.log；网络连接由 tests/run_offline.py 阻断〕。
- 新入口回归覆盖：缺题录保留筛选行、缺摘要不排除、草案不可就绪、查询告警、已知集与固定名单差异、原始审计事件缺失、方案变化、文件改写、阶段恢复、发布完成标识失败恢复、移动目录、竞争锁和输出编码〔实测/工具输出：tests/test_review_delivery.py〕。
- 真进程强制结束验证：运行中竞争锁被拒绝，进程强制结束后锁由操作系统释放，保留锁文件仍可重新取得锁〔实测/工具输出：pubmcp-audit/check_review_lock.py〕。
- 联网演示取回 21 个 PMID、21 条题录，无缺失题录与未提供摘要记录；完整名单、摘要筛选表、RIS、阶段及审计事件哈希核验通过〔实测/工具输出：pubmcp-audit/review-live.log、deliverables/review-delivery-live-demo/delivery.json 的 metrics；主题与日期为自订测试输入〕。
- 联网演示有意保留 draft、空独立已知集和待确认范围，最终状态为 needs_methodology_review；这验证不把工程演示冒充正式研究批准〔实测/工具输出：演示 HANDOFF.md 与 delivery.json 的 issues〕。
- 本机已安装相邻的服务与交付入口，旧服务保留备份；实际 stdio 新进程及新入口核验真实演示目录通过〔实测/工具输出：pubmcp-audit/update_local_followup.py 与本机 --verify-review 调用〕。
- 最终分享 ZIP 解压至含空格的新目录后，全新安装、依赖一致性检查、命令行、断点导出和 MCP stdio 验证均通过；安装版独立入口与主入口均核验工程演示成功，保留 needs_methodology_review 状态〔实测/工具输出：pubmcp-audit/review-ready-archive-install.log 的 FINAL PASS、No broken requirements found 及 verified 回执〕。
- 安装包重装和另一 SDK 环境安装验证通过；包内源码、安装模块及本机源码一致性检查通过〔实测/工具输出：pubmcp-audit/review-ready-install.log、任务 bqt9tis03.output 与 review-ready-archive-install.log〕。

PubMed 单库范围、目标文献管理软件实际导入和 macOS/Linux 实机仍不在已验收结论内。新入口是独立命令行流程，要求助手有命令执行能力；只有 MCP 工具调用能力的客户端不能自动运行它。没有具体研究问题、纳排标准和策略批准时，工具交付完成不等于用户真实综述检索完成。

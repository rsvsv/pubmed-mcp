# 发布检查

版本：1.4.0〔来源：pyproject.toml 的 project.version；本项目自订版本号〕。检查日期：2026-11-12〔来源：本轮工具运行记录〕。

## 本次内容

- 修复 [引号短语开头被误报为布尔算子开头](https://github.com/rsvsv/pubmed-mcp/issues/1)：开头判断读取原始检索式；其他语法检查保持各自的遮罩。回归测试包含 issue 原例及真正错误的算子开头。
- 新增 `check_recall`、`mesh`、`diff`、`export`；扩展引文追踪和检索留痕，补齐 MeSH 历史词及上下位词。
- 查询失真、异常响应、导出不覆盖和日期切片边界有专门回归。
- 详细修复证据见 [演练验证](exercise-fixes-validation.md)，系统综述使用流程见 [工作流](../systematic-review-workflow.md)。

## 发布前验证

| 检查 | 结果与证据 |
|---|---|
| SDK 离线回归 | mcp 1.30.0 与 2.3.0 各 106 项通过〔实测：本轮 `publication-offline-v1.log` / `publication-offline-v2.log`〕 |
| 构建 | sdist 与 wheel 构建成功，wheel 中主程序与工作树逐字节一致；sdist 含新工具测试、配套 skill 和 JSONL 证据〔实测：本轮 build 输出与归档内容断言〕 |
| 安装后协议 | 两代 SDK 均从 wheel 安装后通过 initialize、tools/list、非法 detail/format 的 tools/call；确认加载的是安装包，不是源码目录〔实测：`tests/smoke_installed.py`，本轮 `publication-installed-v1.log` / `publication-installed-v2.log`〕 |
| 依赖 | 两个安装环境的 pip check 均无冲突〔实测：本轮命令输出〕 |
| 本机运行副本 | 已按用户授权同步主程序；真实全局 `--probe` 输出 173 通过、0 失败〔实测：前一轮全局路径 probe 输出〕 |
| 已重启服务的 issue 原例 | 两种词序均无误报警告，命中数均为 138〔实测：本轮 MCP search；时点计数，不承诺日后固定〕 |

构建与回归日志在维护者工作区保存；本页是工具输出的人工摘要。既有联网原响应归档在 `docs/history/evidence/exercise-fixes.jsonl`。

## 发布范围与限制

- 本次更新 GitHub 仓库源码；没有执行 PyPI 上传。安装入口仍是 README 的 GitHub 安装方式。
- 下载脚本位于维护者工作区的另一个目录，不在此仓库；相关演练记录不表示此安装包包含 PDF 下载器。
- 新工具仍有已记录边界：已知集找回不是整体查全率，CSV 不含摘要，引文追踪受数据库覆盖和显示上限限制。
- 本地检查不能替代 GitHub Actions；远端检查状态以这次推送产生的 Actions 结果为准。
- 【历史状态更正，记录日期：2026-11-12】其他验证文档中“未同步、未构建”的叙述属于此前阶段；当前已同步并完成本页列出的构建与安装验证。对尚未运行的检查仍保留原限制。

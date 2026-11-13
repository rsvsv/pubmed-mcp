# 精简输出与自动回归验收

## 功能约束

默认 `detail="full"` 保持既有文本和 JSON 行为；新增 `brief` 保留 PMID、日期、完整标题及全部文献类型，新增 `ids` 直接返回 esearch 的 PMID，不请求 esummary。所有档位沿用查询解析、错误处理、日期边界和窗口提示。摘要工具没有变化。

`ids` 明确提示未获取元数据、未检查撤稿等类型；JSON 使用 `metadata_requested=false` 与 `esummary_missing=null` 表达未请求，不能将其解读为“元数据完整”或“没有撤稿”。`brief` 明确说明省略字段不等于缺失。

## 已执行验证

- 实测默认输出对照：固定响应中的文本与 JSON 共 18 次对照逐字一致；来源：evidence/output_modes_live.json 的 baseline_comparisons。只证明这些已回放样例兼容，不证明任意输入行为恒等。
- 实测干净环境离线回归：MCP 1.30.0 与 2.3.0 各通过 50 项；来源：evidence/output_modes_ci_v1.txt、output_modes_ci_v2.txt 末尾；版本来源：主流程安装后 metadata 工具输出。
- 实测上述环境均为 Windows、Python 3.13；pip check 通过，已安装 CLI 自检、新 stdio 工具枚举、非法 detail 的协议层拒绝通过；来源：主流程 smoke_installed.py 输出。没有将源码导入测试冒充安装验证。
- 实测真实查询覆盖牙根吸收、偏头痛与 erenumab；各档 PMID 顺序、命中数和执行式一致，brief 标题及全部文献类型与 full 相同；来源：evidence/output_modes_live.json 各 arguments/output 条目。
- 实测离线断网运行器最初误拦 Windows 事件循环回环连接，后改为保留本机回环、阻止外部连接；最终回归通过，来源同上离线日志。它不是操作系统级沙箱，也不约束另起子进程。

## 输出体积快照

以下是返回 JSON 字符数，不是模型 token、费用或速度指标。查询参数及输出均保存在原始记录中；没有计算或承诺通用节省比例。

| 查询 | full | brief | ids | 来源 |
|---|---|---|---|---|
| `"root resorption"[tiab]` | 1806 | 1287 | 611 | 实测：evidence/output_modes_live.json，对应 term 的 output_chars |
| `migraine[tiab] AND erenumab[tiab]` | 1795 | 1293 | 644 | 实测：evidence/output_modes_live.json，对应 term 的 output_chars |

## 自动回归边界

`.github/workflows/regression.yml` 在 push、pull_request、workflow_dispatch 时安装项目，运行依赖检查、离线测试和安装烟测。仅授予读取仓库权限，不运行 NCBI 联网探针，不修改分支保护规则。

Python 3.10 / 3.13 与 MCP 一代 / 二代为本项目自订目标矩阵，来源：regression.yml；区间内由 pip 选择版本，不证明所有依赖组合都兼容。Python 下界及 Linux 组合尚待 GitHub 实跑，本机验证不替代远端 CI。配置尚未推送时，自动回归不会在 GitHub 生效。

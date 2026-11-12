# 正畸与深度学习演练：修复与验收

验收日期：2026-11-12〔来源：本轮工具运行时间〕。以下是发布前修复阶段的历史记录，当时尚未同步或提交；后续同步和发布检查见 [发布检查](release-1.4.0.md)〔版本来源：pyproject.toml〕，历史未完成状态不代表当前状态。

## 已修复

| 范围 | 当前行为 | 主要验证位置 |
|---|---|---|
| 查询语法 | 检查原始表达式；提示全角字段、外库语法和多词截词；弯撇号、合法副主题缩写不误判；扫描全部全角组 | `tests/test_exercise_fixes.py::Recall` |
| 已知集找回 | 汇总各批解析结果；保留 `querytranslations`；部分概念消失时置 `term_degraded`，仅剩 UID 条件时置 `term_ignored`；执行式缺失不证明条件清空 | 同上；联网证据的 `recall_english` / `recall_mixed` |
| MeSH | 展示上下位词、历史前身词；本批候选内字面精确项置顶；拒绝错误壳、UID 错配及异常响应；关联词某批失败不阻止后批 | `tests/test_exercise_fixes.py::Mesh` |
| 引文补查 | `links(to="refs")` 与 `to="citedin"`；保留逐号归属、展示截断和数据库覆盖限制；缺失或错号 linkset 报失败 | `CitationAndAudit`；联网 `links_refs` / `links_citedin` |
| 留痕与版本 | 区分输入参数与实际请求参数；题录缺失按实际返回数记录；元数据故障保留 error；摘要及关联请求保留 PMID；版本取运行源码 | `CitationAndAudit` |
| 导出 | 每次使用唯一临时文件；原子发布且不覆盖；没有安全发布原语就失败；写入错误保留原因并清理；相邻日期可继续拆成单日 | `tests/test_exercise_fixes.py::Export` |
| 下载输入 | CSV 仅读标识列、保留同行关系；保留短 PMID、十开头 PMID、年份形状编号；支持 DOI 与 PMCID、BOM、常用分隔符及显式编码 | `.claude/skills/download-literature/test_fetch_papers.py::Parsing`，位于工作区根目录 |
| 下载去重 | Europe PMC 的 PMID 分支限定 MED 来源；响应游标分页；仅以元数据共享标识连接记录，冲突或不连通则拦截；同行未解析标识可由已解析标识兜底 | 下载测试的 `Mapping` / `Metadata` |
| 下载清单 | 原始输入各行保留；下载任务按文献去重；元数据故障或下载中断仍写清单；DOI 文件名不含路径分隔符；导入与帮助不安装依赖 | 同上 |
| 文档 | 明确 CSV 不含摘要、MEDLINE 仅保留来源已有字段；历史推断保留作废标记；不以已知集命中冒充整体查全率 | README、系统综述工作流、项目内 skills |

## 最终验证

原始证据集中保存于 [exercise-fixes.jsonl](audit-evidence/exercise-fixes.jsonl)。每行带来源；可按 `source` 定位对应日志或联网响应。

| 检查 | 结果与来源 |
|---|---|
| 第一代 SDK | mcp 1.30.0，106 项离线测试通过〔实测：`exercise-offline-v1.log`；版本由对应解释器 importlib.metadata 输出核实〕 |
| 第二代 SDK | mcp 2.3.0，106 项离线测试通过〔实测：`exercise-offline-v2.log`；版本由对应解释器 importlib.metadata 输出核实〕 |
| Python 下界 | Python 3.10 环境，106 项离线测试通过〔实测：`release-check-py310` 解释器运行 `tests/run_offline.py`，`exercise-offline-py310.log`〕 |
| 下载脚本 | 19 项离线测试通过〔实测：`exercise-download-tests.log`〕 |
| 既有工具兼容 | 49 组 A/B 输出逐字一致〔实测：`exercise-ab.log`；基准为 git HEAD，限既有工具与脚本覆盖场景〕 |
| 源码 stdio | 两代 SDK 的 initialize、tools/list 与全部工具的前置错误透传通过〔实测：`exercise-stdio-v1.log`、`exercise-stdio-v2.log`〕 |
| 真实联网 | 最终脚本输出 LIVE PASS〔实测：`pubmcp-audit/exercise-live-i8d4l5ol`，原响应归档于证据文件〕 |
| 补丁格式 | git diff --check 通过〔实测：本轮 Bash 输出〕 |

独立审查只提供线索。主会话对记录错误拼接、下载中断漏清单、相邻日期不能切分、MeSH 错误壳和全角字段漏检补了失败复现，再修复并跑通。修复前失败日志保留为 `exercise-review-repro-mcp.log` 和 `exercise-review-repro-download.log`，不是当前失败状态。

## 联网观察与证据边界

- 英文式 `orthodont*[tiab] AND "deep learning"[tiab]` 的抽样已知集正常检回；混合式 `orthodont* 深度学习` 的实际执行式丢失中文概念，当前返回 `term_degraded=true`。〔来源：最终联网证据 `recall_english`、`recall_mixed`，以及其前面的 esearch 原响应〕
- `mesh("deep learning")` 返回父词 Machine Learning / Neural Networks, Computer，并展示历史前身词 Artificial Intelligence。引入年份及历史区间以该响应字段为准，不能据此直接认定某篇文章的漏检原因。〔来源：最终联网 `mesh` 与 esummary 原响应〕
- `Crohn’s disease` 正常映射；`neoplasms/de` 映射为药物作用副主题；单独 `【orthodontics】` 不应误报为词间粘连。〔来源：联网 `syntax_case` 与 esearch 原响应〕
- 导出历史样例 PMID 37427694 的 CSV，再由同行 DOI/PMCID 映射为同一个下载任务，完成链路核对〔实测：最终联网 `export_csv`、`epmc_records`、`downstream_mapping`〕。
- 首次联网对接断言失败保留：当时抽到 PMID 43011790，PubMed 返回了题录，但 Europe PMC 查询没有返回记录〔实测：`exercise-live-zr9pymwa` 的 `search`、`epmc_records`、`downstream_mapping`、`failure`〕。
- 【已作废，修订日期：2026-11-12】首测断言隐含“最新 PubMed 题录必在 Europe PMC 返回”，依据不足。失败不能证明该文章不存在，也不能直接认定是同步延迟；脚本现保留无记录状态，联网链路另用历史样例核对。

## 尚未验证或保留的限制

- 本轮没有重跑出版商全文下载、浏览器 CDP 或筛选平台导入；下载测试覆盖输入、元数据、映射、文件名和清单生命周期。
- 本轮没有安装新发行包、构建发布归档、跑远端 CI 或完整联网 `--probe`；验证对象是工作区源码，源码 stdio 已单独测试。
- 引文展示有上限，不能视为完整引用名单；PubMed 未录入关系也不证明无引用。
- MeSH 精确置顶限本批已返回候选，不保证覆盖全部入口词；关联词不可用时保留 UID 并告警。
- 不支持覆盖已有导出；不支持在非空下载目录自动续跑。失败清单保留后，重试须用新目录。
- 下载 CSV 缺少标识列时明确报错，不回退成扫描整张表；GBK 等无 BOM 编码须显式指定，避免静默改坏标识。
- 非 Windows 的硬链接发布分支未在原生系统跑实测；无支持时安全失败，不退回会覆盖的替代方案。
- 留痕不覆盖全部前置校验与网络失败路径；不能把缺日志解释为未调用。
- 大规模日期切片本轮使用离线边界测试，未重复旧的大集合联网导出；历史大集合结果不能替代本轮全部边界验证。

源码最终校验值位于证据文件末尾；本轮未新增需要登记的研究推算量。

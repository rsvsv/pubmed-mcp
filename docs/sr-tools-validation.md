# 系统综述助手（1.4.0）验证记录

日期：2026-11-11。对象：check_recall / mesh / diff / export / 检索留痕。

本页保留历史验证；后续正畸与深度学习演练修复见 [最新验证记录](exercise-fixes-validation.md)。历史通过结果不替代当前代码复测。

【已作废，修订日期：2026-11-12】下文 A7“原式括号失衡不单独告警”的边界已修；当前检查原始 term 并汇总各批警告。下文仅 MeSH 查询漏检所附“全部尚未标引”的因果解释未经本轮逐文核实，标记为 ⚠️【待确认】，不能从集合差直接推导标引原因。

## 离线回归

- 全套 84 项通过（原 50 项＋tests/test_sr_tools.py 33 项＋test_audit_regressions 新增 1 项）〔实测：tests/run_offline.py 输出〕。
- 两代 SDK 各跑一遍：mcp 1.30.0（ci-env-v1）84 OK、mcp 2.3.0（ci-env-v2）84 OK〔实测：两 venv 的 run_offline.py 输出〕。
- 对抗探针后新增 3 项 check_recall 回归：金标准跨批（60→2 批）合并、中文 term 假阳性告警、正常 term 不误报 ignored。
- 布尔开头误报修复后新增 1 项回归：`(短语 OR 短语)` 开头不误报、真算子开头（含括号包裹）仍告警。
- 新增测试覆盖：check_recall 全中/部分/全漏、三态、元数据失败保留判定、金标准上限、交集异常、去重提示、SDK schema；mesh 正常/无命中/下位词失败降级/retmax 校验与夹取/uid 不符/空 term；diff 增减/相同/超窗拒绝/max_show/分页漂移；export 小题目三格式+sha、空结果、已存在文件与非法格式联网前拒绝、结束重数漂移、CSV 缺失注记、MEDLINE 校验、失败清理临时文件、>9999 切片、切片计数不一致、安全上限、SDK schema；留痕默认关/开启追加 JSONL/写失败不破坏工具。
- 测试夹具数字均为本项目自订测试输入，不是文献证据。

## 默认行为 A/B 逐字对照

- 基准：git HEAD（1.3.0）源码；对照：工作树（1.4.0）。同一假运输数据喂两版，比较 search/count/fetch_abstracts/links 输出。
- 49 组全部逐字一致，0 差异〔实测：pubmcp-audit/ab_sr_change.py 输出「49 通过 / 0 失败」〕。
- 覆盖：search 三档×两格式×5 场景（正常/零结果/窗口/短语未找到/元数据缺失）、count 三组参数、fetch_abstracts 含缺失号、links 两库。

## 真实联网验收

脚本 pubmcp-audit/verify_sr_live.py，原始输出存 audit-evidence/sr_live_acceptance.log〔实测〕。

- mesh：tooth resorption → D014091，入口词 4 个词形，树号 C07.793.901/G10.549.855，下位词 Root Resorption (UID 68012391)〔实测〕。myocardial infarction 查询返回的描述符入口词含非倒置同义词（如 "Inferior Wall Myocardial Infarction"）〔实测〕——ds_meshterms 确含入口词，非仅倒置形。
- check_recall：金标准 4 篇（3 篇实查 tooth resorption[tiab] + 1 篇 migraine[tiab]），found 3 / missing 1，missing 状态 present〔实测〕。窄式（AND 2030[dp]）→ found 0 / missing 3〔实测〕。
- diff：tooth resorption[tiab]（370）vs root resorption[tiab]（5246）→ 新增 5150 / 丢失 274 / 共同 96〔实测〕。
- export 小题目（miniscrew[tiab] AND stability[tiab]，289 条）：pmids/csv/medline 三格式各 289 条，sha256 与重算一致，请求数 3/6/9〔实测〕。
- export 大题目（dental implant，73612 条 >9999）：12 片、122 次请求、去重后 73612 与总数一致；切片计数和 78474 ≠ 73612，重复 4862（推算 DV-SR-DEDUP）〔实测〕。成因：同一记录带多个发表日期落进多个年份切片；警告如实触发，去重后覆盖完整。count_start=count_end=73612，本轮无检索期间漂移〔实测〕。
- 留痕：11 行 JSONL，事件含 search/mesh/check_recall/diff/export〔实测〕。

## 描述体积

- list_tools 描述总字符：1.3.0 为 1325〔实测，delivery_metrics.json〕；1.4.0 为 2328（search 839/count 206/fetch_abstracts 149/links 278/check_recall 233/mesh 183/diff 183/export 257）〔实测 2026-11-11，check_recall 因分批与告警说明由 209 增至 233〕。（1.4.0 时的数字；1.4.4 为 2563，cleandoc 口径〔实测 2026-11-13〕。）
- 回归上限由 2000 上调到 2600〔本项目自订〕。字符≠token，未测真实 token 开销。

## 对抗性探针找坑与修复（2026-11-11）

脚本 pubmcp-audit/adversarial_sr.py，23 案例真实联网〔实测〕。原始输出 sr-adversarial/run-20261111-230338/；耐久副本 docs/audit-evidence/sr_adversarial.json（探针）、sr_confirm_fix.json（修复复测）。
16 OK / 7 ERR，其中 6 个 ERR 是**正确的校验拦截**（非法 as_json、diff/export 超窗、export 超安全上限、路径是目录、非法 format），1 个是真 bug；另发现 1 个静默假阳性。真实题目演练中又发现第 3 个（布尔开头误报）。三处均已修复并复测（pubmcp-audit/confirm_fix.py 5 案例全过＋演练 export 复跑〔实测〕）。

| 坑 | 症状〔实测〕 | 修复 | 复测 |
|---|---|---|---|
| A1 check_recall gold=200 | 一次交集 GET URL 超长 → HTTP 414（gold=100 通过） | 交集按每批 50 个 PMID 分批 esearch 再合并 found（GOLD_CHUNK=50，自订留余量） | F1 gold=200 分 4 批 found=200；F2 全漏 missing=200 元数据正常；F4 gold=60 分 2 批 |
| A6 中文 term 假阳性 | term=牙根吸收 被 NCBI 翻译为空，交集退化成只剩 uid OR 子句，found 假阳性 3/3 | `_term_vanished` 检测执行式纯由 uid 构成 → term_ignored=true＋显著告警「found 不代表 term 匹配」 | F3 term_ignored=True＋告警；F5 正常英文 term 不误报 |
| 布尔开头误报（演练发现） | `_query_warnings` 把引号短语 mask 成空格后，`("root resorption"[tiab] OR …)` 变成 `(   OR …)`，误报「以布尔算子开头」；几乎所有 SR 检索式都这样开头 | 布尔开头检查改用原始 term（短语以 `"` 起头挡住匹配），不用 mask 后的 outside | 演练宽式 export/search 不再误报〔实测〕；真算子开头仍告警（离线回归）；A/B 49 组默认输出不变 |

其余边界行为符合设计（非 bug，记录备查）：
- mesh 中文词 → count 0＋empty query 告警；mesh 广词 retmax=20 正常返回描述符与下位词。
- export 大题目带日期过滤（dental implant AND 2010:2020[dp]，28544 条）→ 7 片取全，count_start=count_end=28544〔实测〕。
- export 嵌套父目录自动创建、零结果写空文件、超安全上限/目录路径/非法 format 联网前拒绝。
- 新工具走 SDK 协议层：mesh/check_recall/export 正常，retmax 字符串"3"强转通过；diff 超窗错误文本完整透传（NcbiError 继承 ToolError 生效）。
- 留痕目录含空格＋中文（"审计 目录"）正常追加 JSONL。
- A7 不平衡括号 term "(tooth" 被外层包裹后 NCBI 自行纠正，执行式回显可见——未单独告警，列为已知边界（包裹会掩盖原始括号失衡）。

## 真实题目端到端演练（2026-11-11）

脚本 pubmcp-audit/demo_sr.py，题目「正畸治疗相关牙根吸收」〔实测〕。原始输出 sr-demo/demo-20261111-233511/；耐久副本 docs/audit-evidence/sr_demo_summary.json。
金标准由实查精确式 `"root resorption"[tiab] AND orthodontic[tiab]` 取 10 篇构造——**演示用，非多来源，有循环偏倚风险〔自订口径〕**，不能据此声称查全率。

| 步骤 | 结果〔实测〕 | 耗时 |
|---|---|---|
| mesh tooth resorption | D014091，入口词 4 词形，树号 C07.793.901/G10.549.855，下位词 Root Resorption | 6.8s |
| mesh orthodontics | 首个描述符返回 Orthodontics, Preventive (D009973)——NCBI 相关性排序所致，须读全部返回描述符而非只取首个 | 3.2s |
| count 窄式 A（仅 MeSH） | 1504 | 1.2s |
| count 宽式 B（MeSH+tiab 双轨） | 2736 | 0.9s |
| check_recall A | 检回 0/10（10 篇新文献尚未 MeSH 标引，仅主题词全漏） | 2.0s |
| check_recall B | 检回 10/10 | 0.8s |
| diff A→B | 新增 1232 / 丢失 0 / 共同 1504（B 是 A 的超集） | 5.7s |
| export B（pmids） | 2736 条，1 片，sha256 核对一致 | 5.7s |
| 留痕 | searches.jsonl 9 行 | — |

效果结论〔实测，限本演示题目〕：check_recall 真实抓出「仅 MeSH 漏掉全部 10 篇新文献」这一查全陷阱，补 tiab 双轨后全找回；diff 量化迭代增减；export 取全核对一致。整套工作流（查词→组式→验已知集→对比→取全→留痕）在真实题目上跑通。
观察（非 bug）：mesh 广词的首个描述符可能是下位/相关词（如 orthodontics → Preventive），由 NCBI 排序决定，使用者须读全部返回项。

## 已知边界与待确认

- ⚠️【待确认】ds_meshterms 是否含某描述符的**全部**入口词：已确认含非倒置入口词，但未对照 MeSH 完整记录逐词核实。
- ⚠️【待确认】筛选工具（Rayyan 等）对 csv/medline 的导入兼容性，以对方文档为准。
- ⚠️【待确认】用户真实题目与多来源金标准集的验收（本轮用演示题目与实查构造的金标准）。
- export 切片按发表日期（pdat）；无发表日期的记录可能不被切片覆盖，计数核对会如实报告差异。
- 单库（PubMed）覆盖有限；不声称系统综述整体查全。
- 本轮未构建 1.4.0 发布归档、未提交、未推送；未跑远端 CI（需推送后触发）。

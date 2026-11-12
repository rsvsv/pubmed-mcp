# 把 pubmed-mcp 变成系统综述助手：设计方案（提案）

状态：第一阶段已实现并验证（2026-11-11，见 docs/sr-tools-validation.md）；第二阶段未做。技术前提已于 2026-11-11 实测核实（§3）。
一句话结论：先做「已知文献找回测试＋MeSH 查词＋检索式对比＋取全导出＋检索留痕」，方法学放进配套 skill；工具只做机械可复现的事，判断留给人和 LLM。

## 1. 目标与总原则

目标：支撑系统综述的检索环节全流程——设计检索式 → 验证查回 → 迭代对比 → 取全导出 → 留痕写报告。

分工原则〔本项目自订〕：
- 工具做：检索、计数、集合运算、导出、留痕。机械、可复现、可验证。
- LLM＋人做：拆概念、选同义词、判断相关性、定纳入排除标准。
- 方法学做：配套 skill（工作流文档），不硬编码进服务器。

诚实边界（继承现有语义，不放宽）：
- 不声称查全率/查准率；已知集找回只证明这批已知文献被检回。
- PubMed 单库不等于系统综述的全部检索。
- 工具不做纳入/排除判断；摘要信息不足标待确认。

## 2. 现状与缺口

已有：search / count / fetch_abstracts / links 四工具；full/brief/ids 三档输出；查询警告（小写布尔、中文、语法、日期窗不相交、短语未找到）；9999 窗口提示；离线 CI 矩阵〔来源：1.3.0 发布核实，见 releases/pubmed-mcp-1.3.0/quality-check.md〕。

缺口（对应系统综述需求）：
1. 无法验证「已知相关文献是否被检回」。
2. 无法取全 >9999 条结果（大题目导出被截断）。
3. 同义词扩展没有 MeSH 根据，靠 LLM 凭空想。
4. 改检索式后不知道多了什么、丢了什么。
5. 检索过程不留痕，写 PRISMA-S 检索报告要手工回忆。

## 3. 技术前提核实（2026-11-11 实测）

证据：pubmcp-audit/probe_sr_features.py 与 probe_sr_features.json（原始 NCBI 响应）。

| 前提 | 实测结果 | 证据位置 |
|---|---|---|
| MeSH 库可经 E-utilities 查 | esearch db=mesh "tooth resorption" 命中 UID 68014091 | 〔实测 P1〕 |
| esummary 给同义词/树号/范围注 | ds_meshterms 含 4 个词形（Tooth Resorption 及倒置形）；ds_idxlinks 给树号 C07.793.901、G10.549.855 与子词 UID；ds_scopenote、ds_meshui D014091、ds_yearintroduced 1965 | 〔实测 P1〕 |
| 已知集交集可一次请求完成 | `tooth resorption[tiab] AND (4 个 pmid[uid] OR …)` 返回预期中的 3 篇、未返回对照 PMID（金标准共 4 篇） | 〔实测 P2〕 |
| history 服务器可建 | usehistory=y 返回 WebEnv/query_key，neoplasms 命中 4422669 | 〔实测 P3〕 |
| 9999 上限对 history 同样生效 | esummary 与 efetch 从 history 取 retstart=10000 均报错；NCBI 错误文本建议用 EDirect 分批逻辑 | 〔实测 P3〕 |

结论：取全导出必须按日期切片分批，不能靠 history 绕过窗口。这是硬约束。
另：efetch db=mesh retmode=xml 本次未返回 ConceptList/Term 结构（1534 字符，无词表）〔实测 P1〕；esummary 已够用，不依赖该路径。

## 4. 第一阶段功能（建议先做）

以下接口均为草案〔本项目自订〕，实现时按既有模式定稿：StrictInt/StrictStr、Literal 枚举、联网前校验、警告透传、默认行为不变。

### 4.1 check_recall —— 已知文献找回测试（规模：小）

解决缺口 1。这是查全验证的核心工具。

```
check_recall(term: str, pmids: str, as_json: bool = False) -> str
```

- 实现：分批 esearch `term AND (pmid[uid] OR …)`，每批 50 个〔实测 P2 可行；对抗探针 A1 发现一次塞 200 个撞 HTTP 414，遂分批〕；返回 found/missing 两个 PMID 列表与计数。
- 诚实性补丁〔实测 A6〕：term 被 NCBI 忽略（如中文词翻译为空）时交集退化成只剩金标准 PMID，found 假阳性全中；检测「执行式纯由 uid 构成」症状，置 term_ignored=true＋显著告警。
- missing 项附标题（仅对缺失项调 esummary），帮 LLM 看出缺哪个概念。
- 逐块诊断不内置：LLM 用现有 `count("某概念块 AND pmid[uid]")` 组合即可定位是哪个 OR 块漏了——少写代码少踩坑。
- 透传 querytranslation 与短语未找到等警告。
- 边界：只证明这批已知文献的找回；金标准集本身可能有偏（§8）。
- 验收：离线夹具（全中/全漏/部分/空集/非法 PMID/超窗提示）＋真实题目 live。

### 4.2 mesh —— MeSH 查词（规模：小）

解决缺口 3。让同义词扩展有根据。

```
mesh(term: str, retmax: int = 5, as_json: bool = False) -> str
```

- 实现：esearch db=mesh → esummary db=mesh〔实测可行，P1〕。
- 每个描述符输出：主题词、UI、入口词表（ds_meshterms）、树号、子词 UID、范围注、引入年份。
- 子词 UID 让 LLM 决定是否展开下位词（explode）。
- 边界：MeSH 覆盖不全（新材料/新术式可能无主题词，需 tiab 同义词补）；词表以查询时点 NCBI 返回为准；ds_meshterms 是否含全部入口词实现时抽验（§10）。
- 验收：离线夹具＋真实词 live；无主题词情形如实报告。

### 4.3 diff —— 检索式版本对比（规模：小）

解决缺口 4。迭代检索式时防止「越改越漏」。

```
diff(term_a: str, term_b: str, max_show: int = 20, as_json: bool = False) -> str
```

- 实现：两次 esearch 取 ID（窗口内分页，助手与 export 共用）；本地集合差。
- 输出：added/removed 计数＋各最多 max_show 个 PMID〔自订阈值〕；超出部分只给计数或写文件。
- 边界：任一查询超窗→拒绝并提示先缩小或切片；两次查询间数据库可能更新→记录时点。
- 验收：离线夹具（含超窗拒绝、空结果、完全相同）＋live。

### 4.4 export —— 取全导出（规模：中大，本阶段最复杂）

解决缺口 2。筛选工具需要完整集合，9999 截断不可接受。

```
export(term: str, path: str, format: Literal["pmids", "csv", "medline"] = "pmids",
       as_json: bool = False) -> str
```

- 当前实现：先 count；窗口内分页取 ID；超窗按发表日期中点递归二分，每片取 ID，合并去重。〔来源：pubmed_mcp.py `_export_slice`〕
- 【已作废，修订日期：2026-11-12】旧设计“按年→月→日切片”未被最终实现采用，不应用它解释当前请求次数或边界。
- 核对：切片计数之和与总数核对；不一致如实报告，不静默〔自订口径〕。
- 一致性：记录开始/结束时间与计数；检索期间数据库更新→标记「以导出时点为准」。history 不能绕窗〔实测 P3〕；单片内是否用 history 固定结果为实现期可选优化，须再验。
- 格式：pmids（纯文本）/ csv（核心元数据：pmid、标题、期刊、年份、作者、DOI、PMCID，字段自订）/ medline（.nbib，efetch 原生）。
- 输出只回摘要：总数、片数、去重数、文件路径、sha256；不回记录本身（省 token）。
- 路径由调用方显式给出，工具不猜目录〔自订〕。
- 边界：筛选工具导入兼容性以对方文档为准（§10）；不保证与 PubMed 网页导出逐字一致；用户 term 自带日期过滤时切片须与之相容。
- 验收：离线夹具（切片计数不一致、超窗细分、空结果、去重、格式）＋一个真实大题目 live 取全核对。

### 4.5 检索留痕（不是新工具，规模：小）

解决缺口 5。写 PRISMA-S 检索报告的原料。

- 环境变量开关（如 PUBMED_AUDIT_DIR，名称自订），默认关闭，零行为变化。
- 开启后 search/count/diff/export 每次追加 JSONL：时间、数据库、输入式、实际执行式、命中数、参数、警告、工具版本。
- LLM 直接读该文件写检索报告，不需要新工具（省工具定义开销）。

### 4.6 配套 skill —— 方法学胶水（规模：小，纯文档）

一个系统综述检索工作流说明，把工具串起来：
PICO 拆概念 → mesh 查词定同义词 → 草稿检索式 → check_recall 验已知集 → count 逐块诊断漏因 → diff 对比迭代 → export 取全 → 读留痕写 PRISMA-S 报告。
含 PRESS 式核对清单中可机器化的部分（语法、MeSH 有效性、字段、布尔逻辑、限制条件）〔自订清单〕。

## 5. 检索之外：写作环节怎么配合

「写系统综述」的其余环节不在服务器里做〔本项目自订分工〕：
- 筛选：LLM 按用户预设标准读题录/摘要辅助判断，人复核；工具只提供数据。
- 提取表 / PRISMA 流程图 / 稿件：LLM＋现有 docx/xlsx skill；MCP 提供结构化题录与留痕数字。
- 第二阶段可给 fetch_abstracts 增加 as_json 结构化输出（MeSH、DOI、PMCID、摘要分段），默认行为不变，方便筛选与提取。

## 6. 第二阶段候选（先不做，第一阶段跑通后再议）

- fetch_abstracts 结构化 JSON 输出。
- Europe PMC 第二来源（覆盖 PMC/预印本等，不是 Embase）；试验注册库；跨库去重。
- 从留痕自动汇总 PRISMA 流程图数字。

## 7. 明确不做

- 自动声称查全率/查准率。
- 工具内自动相关性判断、自动纳入排除。
- 自动生成「最终」检索式不经人审。
- 替代文献管理/筛选平台（导出给它们，不重造）。
- PDF 下载（已有 download-literature skill）。

## 8. 方法学风险（写进 skill 提醒）

- 金标准循环偏倚：只用某综述的参考文献做金标准，会偏向它的检索式；应多来源＋手工补充＋记录来源〔自订方法学口径〕。
- 已知集找回≠查全率；查准需按预设标准筛选后统计。
- PubMed 单库覆盖有限；是否补其他数据库/试验注册/引文追踪由研究问题定。
- 数据库漂移：不同日期结果不同；留痕记录时点。
- MeSH 随 NCBI 更新变化；新词无主题词时靠 tiab 同义词。
- 导出切片计数和≠总数时不得静默，如实报告并建议重跑。

## 9. 工程约束（不踩坑）

- 单文件不变；现有 4 工具默认输出逐字不变（A/B 回归）；新参数全部可选。
- 沿用既有模式：StrictInt/StrictStr、Literal、联网前校验、三态语义、警告透传、逐次限速重试。
- 工具描述总预算：现自订上限 2000 字符〔实测：tests/test_audit_regressions.py 断言〕；新增 4 工具后须上调，新上限自订并在实现时实测登记字符数（字符≠token，无真实 token 计量〔沿用既有口径〕）。
- 每个新功能：离线夹具＋边界测试＋CI 矩阵自动覆盖＋真实 live 验收。
- 版本 1.4.0〔自订〕；发布流程沿用 1.3.0（wheel+sdist＋安装后回归＋下界抽验）。

## 10. 待确认

- ⚠️【待确认】筛选工具（Rayyan 等）导入格式兼容性——以对方文档为准，未核实。
- ⚠️【待确认】ds_meshterms 是否含全部入口词——已确认含非倒置入口词〔实测 2026-11-11〕，但未对照 MeSH 完整记录逐词核实。
- 切片边界行为已实测〔2026-11-11〕：同一记录带多个发表日期会落进多个年份切片，切片计数和大于总数，去重后与总数一致（dental implant 73612/78474，见 sr-tools-validation.md）；无发表日期的记录可能不被切片覆盖，计数核对会如实报告。
- ⚠️【待确认】用户真实题目与已知相关文献集（验收需要，10–30 篇、多来源〔自订建议规模〕）——本轮用演示题目与实查构造的金标准验收。

## 11. 建议顺序

check_recall → mesh → diff → export → 留痕 → skill。
前三个规模小、价值高，先落地先受益；export 最重放后；留痕与 skill 收尾。
规模标记（小/中大）为相对估计〔本项目自订〕，非工时承诺。

## 12. 实现与验收记录（2026-11-11）

第一阶段六项全部落地：check_recall / mesh / diff / export / 检索留痕（PUBMED_AUDIT_DIR）/ 配套 skill（.claude/skills/systematic-review）。
验收：离线 80 项（两代 SDK 各 80）＋默认行为 A/B 49 组逐字一致＋真实联网验收 PASS〔实测〕。
详见 docs/sr-tools-validation.md；方法学工作流见 docs/systematic-review-workflow.md。
尚未构建发布归档、未提交、未推送、未跑远端 CI。

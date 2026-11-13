# pubmed-mcp

单文件 PubMed MCP 服务（stdio），裸打 NCBI E-utilities，不经过 `pubmed-client` 之类的中间层。
现在也带 `pip install` 入口：装完直接敲 `pubmed-mcp` 就是服务。

当前版本为 `1.4.4`〔本项目自订版本号；来源：pyproject.toml 的 project.version〕。1.4.4 修 8 处小 bug（check_recall 空 term/[uid] 误报、mesh 补充概念与 retmax 建议、export 0 条说明与并发请求计数、fetch_abstracts 无效号碎片、links 的 pubmed_pmc_refs 说明，见 [docs/p3-fixes.md](docs/p3-fixes.md)；后续计划见 [docs/roadmap.md](docs/roadmap.md)）。1.4.3 给 `export` 加 `format="ris"`（全作者全名、摘要、卷期页/文章号、撤稿注记，见 [docs/ris-export.md](docs/ris-export.md)）；1.4.2 给 `fetch_abstracts` 加精简/结构化/落盘（见下方「摘要精简与落盘」）；1.4.1 补检索式本地语法检查（见行为契约第 11 条）。1.4.0 新增系统综述检索助手（`check_recall`/`mesh`/`diff`/`export`）与可选检索留痕，并修复 [issue：引号短语开头误报警告](https://github.com/rsvsv/pubmed-mcp/issues/1)。既有基础工具的默认行为保持兼容，验证范围见下方记录。源码发行包附带文档、测试及夹具；wheel 是运行安装包，不附带开发测试材料。

本轮演练修复与验收见 [正畸与深度学习修复记录](docs/exercise-fixes-validation.md)，包含测试结果、首测失败原因与未验证范围。发布前安装包验证另记于 [发布检查](docs/release-1.4.0.md)〔版本来源：pyproject.toml；本项目自订文档命名〕。

**它存在的唯一理由**：把「查不到」和「没有」分开。
文献工具最常见的失败是**静默**——参数被吞掉、词被丢弃、编码不对、请求根本没跑完，
而输出看起来完全正常，于是你拿一个无意义的数字去写论文。本工具的做法是能拦就拦，
拦不住的**在输出里把动过的手脚说出来**。

> 代码注释里保留了大量 `〔实测 日期〕`、`〔推算〕`、`〔本项目口径〕`、`【已作废＋理由＋日期】` 标记。
> 读代码时请按那些标记理解证据强度，**不要把带 `推算`/`未核实` 的行当事实引用**。

---

## 快速开始（三条命令）

```bash
pip install git+https://github.com/rsvsv/pubmed-mcp.git
pubmed-mcp --check-key
```

`--check-key` 应返回 rc=0 并打印 key 形状/限速档/依赖版本。mcp 1.x 与 2.x 都支持；要是它报
`拿不到 MCP 服务类`，说明你环境里的 mcp 既不是一代也不是二代，`pip install "mcp>=1.26,<3"` 即可
（缘由见下面「版本要求」与 [docs/mcp-sdk-v2.md](docs/mcp-sdk-v2.md)）。

然后告诉 Claude Code 去哪儿找它 —— 在 `~/.claude.json` 里加：

```json
{
  "mcpServers": {
    "pubmed": { "command": "pubmed-mcp", "args": [] }
  }
}
```

在 Claude Code 里跑 `/mcp`，确认 `pubmed` 那一行是连上的，不是 ✘。

**上面那段 `"command": "pubmed-mcp"` 只在「命令真能在 PATH 里被找到」时成立。**
〔本会话 2026-11-11 在作者机器实测〕分三种情况：

- **装进虚拟环境但没激活** → 裸命令**找不到**（实测 `command -v pubmed-mcp` 无输出）。Claude Code
  不会替你先激活 venv，所以配置里必须写**绝对路径**：
  Windows `<venv>\Scripts\pubmed-mcp.exe`，类 Unix `<venv>/bin/pubmed-mcp`。
  不知道落在哪就跑 `python -c "import sys;print(sys.executable)"` 看你在哪个解释器里装的。
- **装进系统 Python（非 `--user`）** → 作者机器上那个 `Scripts` 目录**确实在 PATH 里**
  （实测 PATH 第 20 项），所以裸命令可用。你的机器不一定，自己确认：
  `where pubmed-mcp`（cmd）或 `command -v pubmed-mcp`（bash）。
- **`pip install --user`** → 落点是 `%APPDATA%\Python\Python<XY>\Scripts`，
  **这个目录默认在不在 PATH 里，本仓库未核实**（作者机器上该目录不存在，测不了）。
  别照抄这一种，要么用上面两种，要么先用 `where pubmed-mcp` 确认命令敲得到。

零安装路线（下面那节）不受这个影响，因为配置里写的就是绝对路径。

**不想装包也能用**（零安装，直接跑文件）：

```bash
git clone https://github.com/rsvsv/pubmed-mcp.git
cd pubmed-mcp
pip install -r requirements.txt
python pubmed_mcp.py --check-key
```

配置里就写绝对路径：

```json
{
  "mcpServers": {
    "pubmed": { "command": "python", "args": ["-I", "/ABSOLUTE/PATH/TO/pubmed_mcp.py"], "env": {} }
  }
}
```

### ⚠️ 装依赖前先看清用的是哪个解释器

`-I` 是 Python 的隔离模式，等价于 `-E -s`：**忽略 `PYTHONPATH` 与「用户」site-packages
（也就是 `pip install --user` 的落点），但照常读 venv 与系统 site-packages**。
〔本会话实测 2026-11-10：作者机器上 `python -I -c "import sys;print(sys.path)"` 的 `sys.path`
里确实没有 `AppData\Roaming\Python\Python313\site-packages`；而 `-I` 仍能 `import mcp`，
因为那份 `mcp` 装在系统 site-packages。〕

后果：**依赖必须装进「配置里那个 `command` 所指向的解释器」里**。用 `pip install --user` 装、
再配 `"command": "python", "args": ["-I", ...]`，服务会在 Claude Code 里静默起不来。
虚拟环境里请去掉 `-I`，或者用 venv 内那个 `python` 的绝对路径。

也可以用命令行加（**这条本仓库未在本机验证过**：作者机器上 `claude` 不在 PATH 里，
跑不了 `claude mcp add --help`；参数形状随版本变过，动手前先自己 `--help` 对一下）：

```bash
claude mcp add pubmed -- pubmed-mcp
```

## 版本要求

后续更新：已加入 [自动回归配置](.github/workflows/regression.yml)，下面“本仓库没有 CI”为历史状态，现已作废。矩阵是测试目标，实际通过情况以对应提交的 Actions 结果为准；本地运行不能代替远端运行。

`requires-python = ">=3.10"`。〔本项目口径：源码用了 `bool | str` 联合类型标注，3.10 以下在
运行时求值会失败。**但作者机器只有 3.13，本仓库没有 CI，所以这条下限错了也不会有人发现**——
〔推算〕，别当已验证事实。〕

依赖：Python 标准库 + `mcp`（FastMCP 随它一起装）+ `anyio`。HTTP 走 stdlib `urllib`，
当前还直接使用 `pydantic` 的严格类型（由 MCP 依赖安装）。旧句“除这两个之外没有别的第三方依赖”已作废：源码后来增加了严格参数校验〔修订记录：2026-11-11；依据：pubmed_mcp.py 的 pydantic import〕。

**`mcp` 的区间是 `>=1.26,<3`：1.x 和 2.x 都实测支持，3.x 没验过所以挡住。**
下界是量出来的，不是拍的：〔实测 2026-11-11，三个干净 venv 配当前 pydantic 2.14.3〕`mcp 1.2.0` 与
`1.21.0` 都在 **import 阶段**就炸（`cannot import name 'eval_type_backport' from
'pydantic._internal._typing_extra'`），`1.26.0` 才 `IMPORT_OK`。原先写 `>=1.2` 等于承诺了一个
跑不起来的下界——那种装法看着成功、一跑就 traceback。
〔实测 2026-11-10，干净 venv〕原先只写 `>=1.2` 时，`pip install git+https://…` 会解析到
`mcp 2.3.0`，而 2.x 里 `mcp.server.fastmcp` 这个模块名已经没了 —— 装完第一次运行就
`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`。作者本机是 1.29.0，
**这类坑在作者机器上永远复现不出来**，所以本仓库现在的验收一律用不带系统 site-packages 的
干净 venv 跑 `pip install`。

源码里因此加了一层 SDK 兼容层（1.x 拿 `FastMCP`，2.x 拿 `MCPServer`），两条通路各跑过完整
`--probe`：**两代都是 0 失败**〔实测 2026-11-11，mcp 1.30.0 与 2.3.0 各一轮，串行跑、中间隔 8 秒，
避免两轮在限速上互相干扰。〕
**这里故意不写断言条数，也故意不写请求数**：两个数都是「每加一条断言就变」那种，写死过一次就注定过期
（下面「自检」一节同此口径）。**这句自己就过期过一回**：此处原先写着「请求数仍是 89 次」，
后来又加了**联网**的冒烟断言（文档示例能不能真查到东西，光离线测不出来），89 已经不是实际值——
同一段文字里一边说「不写死条数」、一边写死另一个数，正是本 README 批评过的形状。
现在的条数、请求数与它们的变化链记在 [docs/release-delta.md](docs/release-delta.md) 的验收表里。
其中四条**专门走 SDK 的工具包装层**（`search` / `count` / `fetch_abstracts` / `links` 各一条）。
为什么值得为它们各写一条：2.x 会把非 `ToolError` 异常的文本剥掉，这条通路一旦退化，四个工具的
「为什么被拦」会同时缩成一句没有信息的 `Error executing tool xxx`，而探针其余断言都直调协程、
结构上看不见这一层。〔实测依据见 [docs/mcp-sdk-v2.md](docs/mcp-sdk-v2.md)〕

**【已作废 2026-11-11，本会话 A/B 实测推翻】**这一段原先写「v2 变了的那些（客户端字段改
snake_case、`serverInfo.version` 变空串、多 `structuredContent`）都在**客户端侧**，本服务不读，
所以不影响」。它漏了服务端这一手：2.x 会把**非 `ToolError` 异常的文本**从返回结果里剥掉。
同一个 `sort="journal"` 拦截，1.30.0 下调用方拿到 323 字符的完整解释，未修的 2.3.0 只拿到
27 字符的 `Error executing tool search` —— 本仓库的主承诺「失败绝不静默」就在新用户默认走的那条
路上被打穿了。现在 `NcbiError` 继承当前那代 SDK 的 `ToolError`，两代拿到的文本逐字相同；
`--probe` 里补了一条**走包装层**的断言当回归锁（其余断言都直调协程，结构上看不见这一层）。
原话留着不删，是因为「我不读那个字段，所以那个改动与我无关」这种推理会再犯第二次。
细节与逐字签名见 [docs/mcp-sdk-v2.md](docs/mcp-sdk-v2.md)。

新环境里 `pip install` 会拿范围内的最高版（眼下是 mcp 2.3.0）。想固定走 1.x：
`pip install "mcp<2"`。两代本仓库都跑过全量 `--probe`，但**你的其它 MCP 应用如果要求
`mcp<2`，pip 会照它的约束留手**——这也是上界写成 `<3` 而不是钉死某一版的原因。

作者开发时实测 2026-11-08 的版本是 `mcp 1.29.0 / anyio 4.14.2 / pydantic 2.13.4`〔实测，本机
`importlib.metadata`；未在跨版本环境复核〕。

## NCBI API key（可选）

不设 key 也能跑，只是限速低一档。读取顺序：

1. 环境变量 `NCBI_API_KEY`
2. 文件 `~/.ncbi_api_key`
3. 文件 `~/.ncbi_api_key.txt`
4. 都没有 → 匿名档

限速：有 key 10 次/秒，无 key 3 次/秒〔本项目口径：这是 NCBI 公布的档位，
作者**没有逐字核对过官方页面**，代码里也没做过计时测量，所以标口径不标实测〕。

**key 文件的编码不用你操心**。Windows 上 people 建这个文件默认拿到的往往是
PowerShell 5.1 的 **UTF-16LE + BOM** 或记事本的 **UTF-8 带 BOM**，两者都会让早期版本
抛裸 `UnicodeDecodeError`，或者让 `﻿` 混进 key（`﻿` 不是空白，`.strip()` 剥不掉，
于是 NCBI 判 key 无效、而错误文本里看不出是编码的锅）。现在按 BOM 判编码、再兜一层
`utf-8-sig`、最后 `latin-1` 保命。**本仓库不含任何 key**，`~/.ncbi_api_key*` 也被 `.gitignore` 挡着。

用 `--check-key` 自检（**它不发任何网络请求**——但它是**跑在这个文件里**的：文件顶部要先 import 到
MCP 服务类，所以 `mcp` 没装齐时你会先看到兼容层那句 `ImportError`（那句里直接给了该跑的安装命令），
看不到 key 报告。这不是 bug，是「连依赖都没装对」时唯一有用的那句话）：

```bash
python pubmed_mcp.py --check-key
```

它会报告 5 组信息：key 的形状（只说长度/有没有混进文字，**不打印值**）、限速档位、
**它实际读到的 key 文件绝对路径与该文件的编码**、`NCBI_EMAIL`、以及 `mcp/anyio/pydantic` 版本。
把「放错位置的人只会看到 (无)」这个坑堵上了——注意输出里那行路径带你自己的用户名，
贴到 issue 前先知道这件事。

`NCBI_EMAIL`（可选）：NCBI 建议请求里带联系邮箱，设了就会随每个请求发出〔NCBI 的 courtesy
建议，作者未逐字核对原文页面——〔未核实〕〕。

## 工具

八个 `@mcp.tool()`〔本 README 逐字对照源码装饰的定义数出，2026-11-11〕：

| 工具 | 作用 | 要点 |
|---|---|---|
| `search` | 检索，返回命中数 + 每条的 PMID/日期/期刊卷页/标题/作者/DOI/PMC | `term` 支持 PubMed 语法：字段标签（写法与「引号只包短语、别把 `[Tag]` 包进去」的说明见 `search` 的工具说明）、`A AND B`、截词 `resorption*`、范围 `2024:2026[dp]`；一次一个概念、靠字段标签收窄 |
| `count` | 只问「这个主题有多少篇」，不取记录 | 最省请求；查询被改写时印出「NCBI 实际执行」 |
| `fetch_abstracts` | 按 PMID 取摘要原文 | 内部每 50 个一批并发；报「要几条/实取几条/缺哪些」；可选 `fields`/`as_json` 精简、`out_path` 落盘（1.4.2） |
| `links` | 查这些 PMID 在别的数据库里的对应记录（elink） | `to=pmc` 查询 PMC 关联，不保证开放许可或当前可下载；可选 `pmc`/`nucleotide`/`gene`/`pubmed` |
| `check_recall` | 已知文献找回测试：一批已知相关 PMID 哪些被 `term` 检回 | 分批 esearch 交集判定 found/missing（每批 50，避免 URL 超长 414）；缺失项附标题与存在状态（present/unavailable/unknown）；term 被 NCBI 忽略时显著告警；只证明已知集找回，不代表查全率 |
| `mesh` | 查 MeSH 主题词表 | 描述符、入口词（同义词）、树号、下位词、范围注；无描述符命中不等于主题无文献 |
| `diff` | 对比两个查询的结果集 | 新增/丢失/共同 PMID；两查询都须在 9999 窗口内；集合差不判断相关性 |
| `export` | 取全某查询的 PMID 并写文件 | 超 9999 窗口自动按发表日期切片；`pmids`/`csv`/`medline`/`ris`（1.4.3）四种格式；只回摘要+sha256；不覆盖已有文件 |

旧说明“判断有没有免费全文可下”已作废，原因：数据库关联不能证明开放许可或此刻下载可用〔修订记录：2026-11-11；依据：links 当前工具说明〕。

### 按需精简检索结果

`search` 新增可选 `detail`，默认 `full`。已有调用不传此参数时维持完整输出；`as_json` 仍独立控制输出格式。

| detail | 返回内容 | 保留的功能与限制 |
|---|---|---|
| `full` | 原有完整题录字段 | 默认行为及 JSON 字段保持兼容 |
| `brief` | PMID、日期、完整标题、全部文献类型 | 仍读取元数据；不删除撤稿、勘误类型；省略的作者、期刊、DOI/PMC 不表示来源缺失 |
| `ids` | 按 esearch 返回顺序给 PMID | 跳过 esummary；未获取元数据、未检查撤稿等类型；JSON 的 returned 按 PMID 计，esummary_missing 为 null |

各档均保留实际执行式、查询警告、零结果解释与检索窗口上限提示。文本中的执行式沿用原有截断规则，完整执行式始终可从 JSON 的 querytranslation 取得。`ids` 不适合直接据此判断文献质量或引用细节。

调用示例（本项目自订使用示例）：

```python
search('"root resorption"[tiab]', detail="brief")
search('"root resorption"[tiab]', detail="ids", as_json=True)
```

精简不保证每次输出更短：少量短题录可能因必要说明而抵消节省。尚未测量实际模型 token 或费用收益。

### 摘要精简与落盘（1.4.2）

不传新参数时 `fetch_abstracts` 输出与 1.4.1 逐字相同（A/B 49 组），只在正文超过 6 万字符时末尾多一行 ⚠️ 提示。详见 [docs/abstract-slim.md](docs/abstract-slim.md)。

| 参数 | 作用 |
|---|---|
| `fields` | 改走 efetch XML 解析，只输出所列字段：`title,abstract,authors,journal,pubdate,ids,pt,lang,mesh,comments,coi,other_abstracts` 或 `all`。不含作者单位。只给 `as_json` 时默认 `title,abstract,authors,journal,pubdate,ids,pt,lang,comments` |
| `as_json` | 结构化 JSON：摘要分段 `{label,text}`、`has_abstract`、撤稿/勘误关联（无 PMID 时为 null）、书记录 `source="book"` |
| `out_path` | 整份结果写入新文件（不覆盖、原子发布），只回路径、字节数、sha256；篇数多时用它绕开客户端结果上限 |

体积〔实测 2026-11-13，60 篇 `"root resorption"[tiab] AND 2023:2025[dp]`〕：原排版 167,062 字符；`fields=title,abstract` 文本 106,160（64%）；默认字段精简文本 115,131（69%）；`fields=title,abstract,pt` JSON 114,545（69%）。精简只能省三成多，60 篇仍是十万字符量级，**大批量请用 `out_path`**。
XML 解析出的摘要文字与 NCBI 文本档逐字一致（去空白比对，14 条混批 0 处不符〔实测 2026-11-13〕）；上下标、斜体等内联格式与文本档一样被去掉，数值引用仍须核对原文。

### 系统综述检索助手（1.4.0 新增）

四个新工具加可选留痕，支撑系统综述的检索环节。设计依据与实测前提见 [docs/systematic-review-plan.md](docs/systematic-review-plan.md)，方法学工作流见 [docs/systematic-review-workflow.md](docs/systematic-review-workflow.md)（亦作为 `/systematic-review` skill 随仓库分发）。

分工〔本项目自订〕：工具只做机械可复现的事（检索、集合运算、导出、留痕）；拆概念、选同义词、判断相关性、定纳入排除由人与 LLM 按预设标准做。**工具不声称查全率/查准率**，已知集找回只证明这批已知文献被检回。

- `check_recall(term, pmids)`：分批 esearch 交集判定 found/missing；present 表示记录存在但本次实际执行式未检回，unavailable/unknown 表示存在性未确认。term 为空直接报错（1.4.4）；任一批仅剩 UID 条件且 term 本身不是纯 `PMID[uid]` 时置 `term_ignored`；部分概念消失、解析警告或原式语法失真时置 `term_degraded`。空执行式归为信息不足，不能直接断言 term 被忽略。JSON 的 `querytranslations` 保存各批执行式；这些标记是保守诊断，仍须核对原式，不能把全部 found 当真实查全率。
- `mesh(term)`：esearch/esummary `db=mesh`，给入口词、树号、上下位词、历史前身词（previous indexing）、范围注与引入年份。本次返回候选中的字面精确项自动置顶；补充概念记录（SCR）单独标注、给映射主题词并提示用 `[nm]` 检索（1.4.4）；关联名称缺失时保留 UID 并告警。〔来源：源码 `mesh`；验证见演练修复记录〕
- `diff(term_a, term_b)`：两查询各取全窗口内 ID，本地集合差给新增/丢失/共同。任一超窗拒绝并提示改用 export。
- `export(term, path, format)`：超检索窗口按发表日期递归二分，切片计数与总数核对、按 PMID 去重。每次使用唯一临时文件；Windows 用不覆盖的 rename，其他系统用硬链接原子发布，文件系统不支持则报错。清理失败会警告；path 不得已存在。CSV 不含摘要，MEDLINE 保留来源已有字段。安全上限为十万条〔本项目自订；源码 EXPORT_MAX〕。
- **检索留痕**：设环境变量 `PUBMED_AUDIT_DIR`，工具在完成路径追加 JSONL 到 `searches.jsonl`。search/count 区分 `input_params` 与实际 `request_params`；记录实际返回数、执行式、警告和运行源码版本，search 元数据失败另记 `error`。fetch_abstracts/links 记录请求 PMID；check_recall 记录各批执行式。留痕默认关闭；写失败报 stderr。参数校验失败、网络中断等并非所有失败路径都有日志，不能当完整请求流水账。
- **引文补查**：`links(to="refs")` / `links(to="citedin")` 给参考文献/施引文献；有展示上限并显示截断，只限 PubMed 已录入关系。

实测注意〔2026-11-11 live，verify_sr_live.py〕：大题目切片计数和可能**大于**总数——同一记录带多个发表日期时会落进多个年份切片，去重后与总数一致；警告会如实说明，这不是错误。例：`dental implant` 命中 73612，切片计数和 78474，去重后 73612〔实测；重复数 4862 为推算 DV-SR-DEDUP〕。

验收：离线 84 项（mcp 1.30.0 与 2.3.0 各 84 通过）＋默认行为 A/B 49 组逐字一致＋真实联网验收 PASS＋对抗性探针找坑并修复 3 处＋真实题目端到端演练〔实测 2026-11-11，详见 docs/sr-tools-validation.md〕。尚未测量新工具的实际 token 开销；工具描述总字符由 1325 增至 2328〔实测 list_tools〕，描述体积回归上限相应由 2000 上调到 2600〔本项目自订〕。（以上为 1.4.0 验收时的数字。截至 1.4.4：离线 160 项；描述总字符 2563，按 `inspect.cleandoc` 口径，与 Python 版本无关〔实测 2026-11-13 list_tools〕。）

### 自动回归

[regression.yml](.github/workflows/regression.yml) 在推送、拉取请求和手动触发时安装项目，检查依赖，执行离线回归，并验证已安装 CLI 与新 stdio 进程。需要把配置推送至 GitHub 才会触发远端运行。

- Python 3.10 / 3.13、MCP 一代 / 二代组合为本项目自订验证矩阵（来源：regression.yml）；每个区间由 pip 选择兼容版本，不代表全部版本或最低 SDK 均已验证。
- 日常测试不查询 NCBI；离线运行器拦截本进程的外部 socket 连接，保留事件循环需要的本机回环连接。依赖安装仍需联网，安装烟测不作全系统断网保证。
- 完整联网 `--probe` 单独手动运行，不将外部网络故障混入普通提交检查。
- Actions 红灯不会自动禁止合并；强制通过后才允许合并需要仓库分支保护规则，本配置不会改动该设置。

本地执行同一套离线检查：

```bash
python -I tests/run_offline.py
```

## Behavior contract —— 它到底替你拦了什么

下面每条**都在源码里有实测锚点或断言覆盖**（本仓库的 `--probe` 会替你复验，包括第 1 条那个
`200 + HTML` 分支——它已落成一条离线断言，不再只是防御性假设）。

1. **失败绝不静默。** 请求失败抛 `NcbiError` 并带回 NCBI 原文；HTTP 200 + 一段 HTML、
   或 200 + `{"ERROR": ...}` 都算失败，不会被说成「NCBI 里没有这篇」。
   〔实测 2026-11-11：这条曾在 mcp 2.x 上被打穿过——SDK 把非 `ToolError` 的异常文本剥掉，调用方
   只剩一句 `Error executing tool <name>`。现在的基类写法与那条走包装层的回归断言见
   [docs/mcp-sdk-v2.md](docs/mcp-sdk-v2.md)。〕
2. **`0 条`必须说清是哪一种 0。** 真的没有匹配 / 词被当停用词忽略 / 整个引号短语在索引里不存在 /
   两段同字段日期不相交 / `retstart` 越页 / `retmax=0` 且命中>0（只要计数、没要记录）——这几种各说各的话，
   不统一兜底成「PubMed 里没有匹配」。
3. **`field=` 走白名单，宁拦勿吞。** 实测 NCBI 对认不出的 `field` 静默忽略（拼错一个字母＝限定被吃掉、
   且没有任何提示）。`keywords`/`PMC Number`/`ISSN` 实测被整条忽略，`MeSH Terms`/`Publication Type`
   实测让 `esearch` 不返回 `count`——这五个都直接报错并给改写建议。
4. **`sort=` 只收 `pub_date` / `relevance` / `author`**〔实测 2026-11-10 本机 idlist 两两对照〕。
   `journal`/`relevancy`/`keyword` 实测顺序与不传时逐字相同，所以放进黑名单而不是白名单。
   `best match` 会被改写成 `relevance` 下发，**并且输出里明说这次改写**。
5. **单边日期补哨兵，并声明补了什么。** 实测 NCBI 对只给一端的 `mindate`/`maxdate` 整条忽略。
   补的是不可能生效的哨兵 `0001-01-01` / `2999-12-31`，**不是**「今天」或「收录起点」——
   后者是真边界，本机日历与 NCBI 服务器日历差一天时会静默切掉那一天的文献。
6. **`reldate` 原样下发，本工具不做日历换算。** 「近 N 天」由 NCBI 按它自己的服务器日历算。
7. **摘要按 `^PMID:` 行计数**，不拿请求数冒充返回数。不存在的 PMID 会回「要 N 条 / 实取 M 条」，
   并点名取不到的那个号；"实取"按 efetch 正文里的 PMID 行计，所以没有摘要的社论照样算实取
   〔实测 2026-11-11：99999999 / 9999999999 → 要 1 条 / 实取 0 条〕。
8. **PMID 解析不猜。** `1,234,567` 这种带千位分隔符的形（从 Excel 复制一列号很常见）直接拦——
   旧写法会切成 3 个真实存在的 PMID，抓回三篇不相干文献、条数还看着正常。
9. **async 工具 + 网络调用出事件循环。** mcp **1.x** 对非 async 工具是裸 `return fn(**kwargs)`、
   没有 threadpool，一次 40s 的 `efetch` 能把整个 server 冻住〔依据：作者读的
   `mcp/server/fastmcp/utilities/func_metadata.py`〕。
   〔实测 2026-11-10：这一条**只在 1.x 成立** —— 2.3.0 把该文件挪到
   `mcp/server/mcpserver/utilities/func_metadata.py`，非 async 分支改成
   `await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))`，也就是 SDK 自己开始卸载了。
   本服务全部工具均为 `async def`；装饰后协程属性和包装层透传由离线测试与源码 stdio 烟测覆盖。
   〔**这条后半句此前没有断言钉住**——而它才是承重的部分：装饰之后的名字一旦不再是协程函数，
   SDK 就走同步分支。现在有一条离线断言专门问【装饰之后】那四个名字，两代各测一次都仍是协程函数
   〔实测 2026-11-11〕。〕
10. **key 一个字符都不外泄。** 错误文本里只报 key 长度不报片段；`_mask` 无条件整串替换成定长掩码
    （旧写法既留前 4 位、又只在长度 > 8 时才掩）。〔已知边界：key 以 URL 百分号编码形态出现在
    报错文本里时仍替换不到——过度设计，记着当边界，别当已修好。〕

11. **term 里写错的语法要说出来。**〔1.4.1，实测 2026-11-13，详见 [docs/query-lint.md](docs/query-lint.md)〕
    NCBI 对 term 内认不出的半角字段标签（`[tiabb]`、`[ab]`、`[keywords]`、`[Title / Abstract]`）**不报错**，
    静默按无标签执行、命中数照常给。现在本地对照一张逐个实测过的标签表，表外标签给 ⚠️ 并建议最近似写法；
    同时检查前导截词 `*screw`、`AND NOT`、`(A OR B)[tiab]`、`2020-2024[dp]` 与反向区间、日期标签配非日期值、
    无引号邻近检索、同层 AND/OR 混用。纯本地、零请求；会让限定失效的那几类计入 check_recall 的 `term_degraded`，
    AND/OR 混用只提示不计入。`count` 的 0 条现在也给出与 `search` 同口径的「是哪一种 0」。

## 自检

```bash
python pubmed_mcp.py --check-key      # 零网络请求，先看 key/限速/依赖装对没
python pubmed_mcp.py --probe          # 完整回归断言，需要联网，见下方警告
python pubmed_mcp.py --selftest "tooth resorption"
python pubmed_mcp.py --concurrent "tooth resorption"
```

⚠️ **`--probe` 不是健康检查，是一轮重流量回归**。它自己会在末行报数：
「`N 通过 / M 失败 —— 本轮 K 次 HTTP 请求（含退避重试的每一次）`」。
**本 README 故意不写死任何条数**——写死过一次就注定过期（这份代码里有一条真实的过期记录）。
按量级估：一轮要按**几十秒到几分钟**算，还要看你在 10 次/秒还是 3 次/秒档。

怎么确认它真的跑完了：**看末行有没有那句「N 通过 / M 失败」，并看退出码**
（非 0 即有 FAIL）。这条不是形式主义——作者踩过一次「探针中途崩掉、日志却显示 28 通过 / 0 失败」，
崩掉的探针看起来像通过，比一条 FAIL 危险得多。

复现编码相关的 bug 时**别加 `-I`**：隔离模式会忽略 `PYTHONIOENCODING`，stdout 直接落回 utf-8，
于是旧版也不会报错，你会得出「旧版也没问题」的假结论。

## Limits（诚实清单）

- **查询以实际执行式为准。** 中文片段可能消失，混合查询仍可能命中剩余英文概念；不能把命中数当成完整原查询结果。
  【已作废，修订日期：2026-11-12】旧“只索引英文”与“空执行式等于没解析出条件”结论过强；缺少执行式只能说明解析信息不足。
- **esearch 最多取回前 9,999 条**，`retstart` 上限 9998（NCBI 硬墙，越界本工具直接报错）。
  1.4.0 的 `export` 通过按发表日期切片取全超过此窗口的结果（history 服务器同样受此限制〔实测 2026-11-11〕）；`search`/`diff` 仍受窗口约束。
- **`retmax` 上限 1000 是本工具自订的**，不是 NCBI 的限制；要一次多拿就改那个常量，
  或按年份/字段把查询切小分批取。夹过都会在输出里说明是**本工具**夹的。
- **不生成 Vancouver/APA 等引文格式。** 引文格式交给文献管理软件：`export(format="ris")` 或 `medline` 导入 Zotero/EndNote 后由它们排版〔本项目自订分工，见 docs/systematic-review-plan.md §7〕。RIS 已用 rispy 0.10.0 解析校验，**未做 Zotero/EndNote 真机导入**。`csv` 格式保持 1.4.0 的 10 列不变（作者仍只列前 3 位缩写），全作者请用 ris。
- **PubMed 没有「按期刊影响力排序」这个概念**，`sort` 里不存在这种值。本仓库也不含这类脚本
  ——要那种排序得自己接外部期刊指标数据源。
- 默认 `datetype="pdat"`（发表日期）。不带 `datetype` 时 NCBI 按入库日期算，两者结果不同〔实测〕。
- 分批大小（esummary 100、efetch 50）与超时（25s/40s）是**本项目口径/推算值**，
  不是量出来的最优点；它们挂在「实测」标题下的段落里容易被误读，源码里已就地标注。

## 为什么不用现成的包

作者初版对比的是 `pubmedmcp 0.1.4` + `pubmed-client 0.2.0`，结论是那两个包不读 API key、不设 timeout、
出错时把异常打成空字符串。

**⚠️【已不可就地复核，2026-11-08 作者自查】**：那几条对比是当时看过源码得出的，但复核时本机
`pip show` 两个包均 not found、无可执行文件、无 pipx，**现在无法重跑验证**。
别当事实引用；要用就重装那两个包再看。

## 这份发布版与作者开发版的关系

公开仓库里的 `pubmed_mcp.py` **不是**作者机器上正在跑那份的逐字节副本：它带着若干处发布专用改动
（`_mask` 收口、`--selftest` 不再打印 key 前缀、工具说明补上 `author`、journal-rank 指路改成不指向
本仓库没有的文件，以及三条新增离线断言）。逐处理由与依据见
[docs/release-delta.md](docs/release-delta.md)。

## License

MIT — 见 [LICENSE](LICENSE)。

源码注释里的 `〔实测〕`／`〔推算〕`／`【已作废＋理由＋日期】` 标记请**一并保留**：
作废的推断不删除、只标注，是因为删掉之后人会重新踩同一个坑。

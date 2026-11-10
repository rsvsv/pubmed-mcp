# pubmed-mcp

单文件 PubMed MCP 服务（stdio），裸打 NCBI E-utilities，不经过 `pubmed-client` 之类的中间层。
现在也带 `pip install` 入口：装完直接敲 `pubmed-mcp` 就是服务。

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
`拿不到 MCP 服务类`，说明你环境里的 mcp 既不是一代也不是二代，`pip install "mcp>=1.2,<3"` 即可
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

`requires-python = ">=3.10"`。〔本项目口径：源码用了 `bool | str` 联合类型标注，3.10 以下在
运行时求值会失败。**但作者机器只有 3.13，本仓库没有 CI，所以这条下限错了也不会有人发现**——
〔推算〕，别当已验证事实。〕

依赖：Python 标准库 + `mcp`（FastMCP 随它一起装）+ `anyio`。HTTP 走 stdlib `urllib`，
所以除这两个之外没有别的第三方依赖〔本 README 逐字对照源码 import 数出，2026-11-10〕。

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
`--probe`：现在是 **140 通过 / 0 失败**〔实测 2026-11-11，mcp 1.30.0 与 2.3.0 各一轮，串行跑、
中间隔 8 秒，避免两轮在限速上互相干扰〕。

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

用 `--check-key` 自检（**它不发任何网络请求**）：

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

四个 `@mcp.tool()`〔本 README 逐字对照源码装饰的定义数出，2026-11-10〕：

| 工具 | 作用 | 要点 |
|---|---|---|
| `search` | 检索，返回命中数 + 每条的 PMID/日期/期刊卷页/标题/作者/DOI/PMC | `term` 支持 PubMed 语法：`"resorption[Title]"`、`A AND B`、截词 `resorption*`、范围 `2024:2026[dp]`；一次一个概念、靠字段标签收窄 |
| `count` | 只问「这个主题有多少篇」，不取记录 | 最省请求；查询被改写时带 ⚠️ |
| `fetch_abstracts` | 按 PMID 取摘要原文 | 内部每 50 个一批并发；报「要几条/实取几条/缺哪些」 |
| `links` | 查这些 PMID 在别的数据库里的对应记录（elink） | `to=pmc` 判断有没有免费全文可下；可选 `pmc`/`nucleotide`/`gene`/`pubmed` |

## Behavior contract —— 它到底替你拦了什么

下面每条**都在源码里有实测锚点或断言覆盖**（本仓库的 `--probe` 会替你复验，包括第 1 条那个
`200 + HTML` 分支——它已落成一条离线断言，不再只是防御性假设）。

1. **失败绝不静默。** 请求失败抛 `NcbiError` 并带回 NCBI 原文；HTTP 200 + 一段 HTML、
   或 200 + `{"ERROR": ...}` 都算失败，不会被说成「NCBI 里没有这篇」。
   〔实测 2026-11-11：这条曾在 mcp 2.x 上被打穿过——SDK 把非 `ToolError` 的异常文本剥掉，调用方
   只剩一句 `Error executing tool <name>`。现在的基类写法与那条走包装层的回归断言见
   [docs/mcp-sdk-v2.md](docs/mcp-sdk-v2.md)。〕
2. **`0 条`必须说清是哪一种 0。** 真的没有匹配 / 词被当停用词忽略 / 整个引号短语在索引里不存在 /
   两段同字段日期不相交 / `retstart` 越页——这几种各说各的话，不统一兜底成「PubMed 里没有匹配」。
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
7. **摘要按 `^PMID:` 行计数**，不拿请求数冒充返回数。不存在的 PMID 会返回「取回 1 条 + 空壳」，
   这一条也照实说。
8. **PMID 解析不猜。** `1,234,567` 这种带千位分隔符的形（从 Excel 复制一列号很常见）直接拦——
   旧写法会切成 3 个真实存在的 PMID，抓回三篇不相干文献、条数还看着正常。
9. **async 工具 + 网络调用出事件循环。** mcp **1.x** 对非 async 工具是裸 `return fn(**kwargs)`、
   没有 threadpool，一次 40s 的 `efetch` 能把整个 server 冻住〔依据：作者读的
   `mcp/server/fastmcp/utilities/func_metadata.py`〕。
   〔实测 2026-11-10：这一条**只在 1.x 成立** —— 2.3.0 把该文件挪到
   `mcp/server/mcpserver/utilities/func_metadata.py`，非 async 分支改成
   `await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))`，也就是 SDK 自己开始卸载了。
   本服务四个工具全是 `async def`，两代下都不会是占住事件循环的那一方，所以写法照旧正确、不回退。〕
10. **key 一个字符都不外泄。** 错误文本里只报 key 长度不报片段；`_mask` 无条件整串替换成定长掩码
    （旧写法既留前 4 位、又只在长度 > 8 时才掩）。〔已知边界：key 以 URL 百分号编码形态出现在
    报错文本里时仍替换不到——过度设计，记着当边界，别当已修好。〕

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

- **只索引英文。** 中文词（如「牙根吸收」）会让 NCBI 返回空 `querytranslation` + 0 条；
  这条 0 会被明说成「NCBI 没解析出检索条件」，而不是「该主题没有文献」。
- **esearch 最多取回前 9,999 条**，`retstart` 上限 9998（NCBI 硬墙，越界本工具直接报错）。
- **`retmax` 上限 1000 是本工具自订的**，不是 NCBI 的限制；要一次多拿就改那个常量，
  或按年份/字段把查询切小分批取。夹过都会在输出里说明是**本工具**夹的。
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

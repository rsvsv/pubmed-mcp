# pubmed-mcp

一个单文件 PubMed MCP 服务（stdio），裸打 NCBI E-utilities，不经过 `pubmed-client` 之类的中间层。

Self-contained PubMed MCP server for Claude / any MCP client. One Python file, stdlib + `mcp`,
talking straight to NCBI E-utilities.

**它存在的唯一理由**：把「查不到」和「没有」分开。
Every failure mode that makes a literature tool silently lie to you is handled explicitly — see
[Behavior contract](#behavior-contract--它到底替你拦了什么).

---

## Requirements

| | |
|---|---|
| Python | 3.10+（用到 `bool \| str` 联合类型语法）〔本项目口径：从源码语法直接判的，未在 3.9 上试跑〕 |
| 依赖 | `mcp`（FastMCP 随它一起装）、`anyio` |
| 版本 | 作者开发时实测 2026-11-08：`mcp 1.29.0 / anyio 4.14.2 / pydantic 2.13.4`〔来源：作者本机 `importlib.metadata`，本 README 未复核〕 |
| 网络 | 能访问 `eutils.ncbi.nlm.nih.gov` |
| NCBI API key | 可选。没有就走匿名档 3 次/秒，有则 10 次/秒〔NCBI 公布的限速档位，未在本 README 逐字核对原文〕 |

```bash
pip install mcp anyio
```

## Install

```bash
git clone https://github.com/<you>/pubmed-mcp.git
cd pubmed-mcp
python pubmed_mcp.py --check-key     # 只报告 key 与限速档位，不发查询
```

### 接到 Claude Code

下面这段是**已知可用**的形状——作者的机器上 `~/.claude.json` 就是这么写的，
服务确实起来了〔验证方式：2026-11-10 本会话直接读了那份配置并确认 `pubmed` 已加载，非推断〕：

```json
{
  "mcpServers": {
    "pubmed": {
      "command": "python",
      "args": ["-I", "/ABSOLUTE/PATH/TO/pubmed_mcp.py"],
      "env": {}
    }
  }
}
```

Windows 上把路径换成 `C:/Users/<you>/.../pubmed_mcp.py`（正斜杠即可，Git Bash 与 Python 都认）。

也可以用命令行加（**这条本仓库未在本机验证过**——作者那台机器上 `claude` 不在 PATH 里，跑不了
`claude mcp add --help`；参数形状随版本变过，动手前先自己 `--help` 对一下）：

```bash
claude mcp add pubmed -- python -I /ABSOLUTE/PATH/TO/pubmed_mcp.py
```

`-I` 是 Python 的 isolated mode：不读用户 site-packages 之外的一切路径注入，当前目录里的
`json.py` 之类文件就不会顶掉标准库。〔来源：Python 官方文档对 `-I` 的说明，逐字未核对〕

然后在 Claude Code 里跑 `/mcp`，确认 `pubmed` 那一行是连上的（✔），不是 ✘。

### NCBI API key（可选）

读取顺序：环境变量 `NCBI_API_KEY` → 文件 `~/.ncbi_api_key` → 都没有则匿名。
**不要把 key 提交进仓库**，本仓库不含任何 key。

### `NCBI_EMAIL`（可选）

NCBI 建议在请求里带联系邮箱。设了 `NCBI_EMAIL` 就会随每个请求发出。

## Tools

四个工具〔本 README 逐字对照源码 `@mcp.tool()` 装饰的定义数出，2026-11-10〕：

| 工具 | 作用 | 备注 |
|---|---|---|
| `search` | 检索，返回命中数 + 每条的 PMID/日期/期刊卷页/标题/作者/DOI/PMC | `term` 支持 PubMed 语法：`"resorption[Title]"`、`A AND B`、截词 `resorption*`、范围 `2024:2026[dp]` |
| `count` | 只问「这个主题有多少篇」，不取记录 | 最省请求；被改写时带 ⚠️ |
| `fetch_abstracts` | 按 PMID 取摘要原文 | 内部每 50 个一批并发 |
| `links` | 查这些 PMID 在别的数据库里的对应记录（elink） | `to=pmc` 用来判断有没有免费全文可下；可选 `pmc` / `nucleotide` / `gene` / `pubmed` |

命令行自检（不走 MCP，直接打印）：

```bash
python pubmed_mcp.py --selftest "tooth resorption"   # 打印一次完整检索
python pubmed_mcp.py --check-key                     # key / 限速档位，不发查询
python pubmed_mcp.py --probe                         # 回归断言，退出码非 0 即有 FAIL
python pubmed_mcp.py --concurrent "tooth resorption" # 并发相关检查
```

⚠️ `--probe` 一轮是 **88 次 HTTP 请求**（121 条断言；同一天 98 条那次 71 次）〔来源：作者实测 2026-11-10，
给 `_get_async` 包计数器量出来的〕。按「约 10 次」估会估错一个量级——请预留几十秒到几分钟，
并注意匿名档 3 次/秒的限速。

## Behavior contract — 它到底替你拦了什么

下面每条都在源码注释里带了实测依据（键名、返回形状、请求参数都写明了），
不是「我觉得应该这样」。

1. **失败绝不静默。** 请求失败抛 `NcbiError`，并把 NCBI 返回的原文带回来；HTTP 200 + 一段 HTML、
   或 200 + `{"ERROR": ...}` 都会被识别成失败，不会说成「NCBI 里没有这篇」。
2. **`0 条`必须说清是哪一种 0。** 区分：真的没有匹配 / 词被当停用词忽略 / 整个引号短语在索引里不存在 /
   两段同字段日期不相交 / `retstart` 越页。这几种都会印对应的那句话，不会统一兜底成「PubMed 里没有匹配」。
3. **`field=` 走白名单，宁拦勿吞。** 实测 NCBI 对认不出的 `field` 是静默忽略的（拼错一个字母 = 限定被吃掉，
   且没有任何提示）。`keywords` / `PMC Number` / `ISSN` 实测被整条忽略，`MeSH Terms` / `Publication Type`
   实测会让 `esearch` 不返回 `count`——这五个都直接报错并给出改写建议。
4. **`sort=` 只收真生效的三个值** `pub_date` / `relevance` / `author`〔来源：作者实测 2026-11-10〕。
   `journal` / `relevancy` / `keyword` 实测顺序与不传时逐字相同，所以放进黑名单而不是白名单。
   `best match` 会被改写成 `relevance` 下发，**并且输出里明说这次改写**。
5. **单边日期会补哨兵，并声明补了什么。** 实测 NCBI 对只给一端的 `mindate`/`maxdate` 整条忽略；
   补的端点是 `0001-01-01` / `2999-12-31` 这种不可能生效的哨兵，**不是**「今天」或「收录起点」——
   后者是真边界，本机日历与 NCBI 服务器日历差一天时会静默切掉那一天的文献。
6. **`reldate` 原样下发，本工具不做日历换算。** 「近 N 天」由 NCBI 按它自己的服务器日历算。
7. **取摘要报「要几条 / 实取几条 / 缺哪些」**，按 efetch 正文里的 `^PMID:` 行计数，不拿请求数冒充返回数。
   不存在的 PMID 会返回「取回 1 条 + 空壳」，这一条也照实说。
8. **PMID 解析不猜。** `1,234,567` 这种带千位分隔符的形（从 Excel 复制一列号常见）直接拦下来——
   旧写法会切成 3 个真实存在的 PMID，抓回三篇不相干文献且条数看着正常。
9. **async 工具 + 网络调用出事件循环。** FastMCP 对非 async 工具是裸 `return fn(**kwargs)`，
   没有 threadpool，一次 40s 的 `efetch` 能把整个 server 冻住〔依据：作者读的
   `mcp/server/fastmcp/utilities/func_metadata.py`；SDK 升级后若改成 threadpool，本文件的写法依然正确〕。

## Limits（诚实清单）

- **只索引英文。** 中文词（如「牙根吸收」）会让 NCBI 返回空 `querytranslation` + 0 条；
  这条 0 会被明说成「NCBI 没解析出检索条件」，而不是「该主题没有文献」。
- **esearch 最多取回前 9,999 条**，`retstart` 上限 9998（NCBI 硬墙，越界本工具直接报错）。
- **`RETMAX_CAP = 1000` 是本工具自订的**，不是 NCBI 的限制；要一次多拿就改这个常量，或按年份/字段切小查询分批取。
- **PubMed 没有「按期刊影响力排序」这个概念**，`sort` 里不存在这种值。要那种排序得自己接外部期刊指标数据源。
- 本工具默认 `datetype="pdat"`（发表日期）；NCBI 不带 `datetype` 时按入库日期算，两者结果不同。

## Why not an existing package

作者初版对比的是 `pubmedmcp 0.1.4` + `pubmed-client 0.2.0`，结论是那两个包不读 API key、不设 timeout、
出错时把异常打成空字符串。

**⚠️【已不可就地复核，2026-11-08 作者自查】**：那 4 条对比是当时看过源码得出的，但复核时本机
`pip show` 两个包均 not found、无 `pubmedmcp` 可执行文件、无 pipx，**现在无法重跑验证**。
别当事实引用；要用就重装那两个包再看。

## Self-check

```bash
python -m py_compile pubmed_mcp.py   # 语法
python pubmed_mcp.py --probe         # 需要联网
```

## License

MIT — 见 [LICENSE](LICENSE)。

源码注释里保留了大量 `〔实测 日期〕`、`【已作废＋作废理由＋日期】`、`〔推算〕` 标记。
**作废的推断不删除、只标记**，是因为删掉之后人会重新踩同一个坑。请一并保留。

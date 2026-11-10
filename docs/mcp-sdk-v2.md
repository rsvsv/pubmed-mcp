# mcp SDK 1.x / 2.x：这仓库为什么带一层兼容层

这份文件存在的原因：本仓库的头条安装命令在**作者机器上永远测不出这个坑**。

## 先说发生了什么（本会话干净 venv 实测，2026-11-10）

用一个**不带 `--system-site-packages`** 的临时 venv，照 README 原样执行
`pip install git+https://github.com/rsvsv/pubmed-mcp.git`（当时的声明是 `mcp>=1.2`）：

```
Successfully installed ... mcp-2.3.0 ... pubmed-mcp-1.0.0
$ pubmed-mcp --check-key
ModuleNotFoundError: No module named 'mcp.server.fastmcp'. This is mcp 2.x, where FastMCP was
renamed to MCPServer (from mcp.server.mcpserver import MCPServer) and other APIs changed;
see the migration guide at https://py.sdk.modelcontextprotocol.io/v2/migration/#fastmcp-renamed-to-mcpserver
or pin 'mcp<2' to keep running v1 code.
rc=1
```

〔原文＋位置：上面那段是 **mcp 2.3.0 自己抛出的错误文本**，逐字粘贴，不是转述。〕

作者本机是 `mcp 1.29.0`，`from mcp.server.fastmcp import FastMCP` 一直好用；更早那次
`pip install --no-deps .` 也没暴露它，因为它复用的是系统 site-packages 里的 1.29.0。
**教训**：只在本机验过的「依赖没问题」等于没验。本仓库现在的验收一律走干净 venv。

## 现在的方案

- 源码里一层 SDK 兼容层：先取 1.x 的 `FastMCP`，取不到就取 2.x 的
  `mcp.server.mcpserver.MCPServer`；两代都没有就抛一句**说清该怎么办**的 `ImportError`
  （而不是让别人的终端停在一条裸 `ModuleNotFoundError` 上）。
- 依赖区间 `mcp>=1.26,<3`。上界留到 `<3` 而不是干脆不写：3.x 是没验过的下一代，
  这次的教训正是「把没测过的版本当默认」。
- 兼容层自己挂了一条 `--probe` 离线断言（「拿到的是可用服务类：能构造、有 `tool`、有 `run`」）。
  没有断言的承诺会被下一次改写悄悄作废——这个文件已经有一条这样的过期记录。

## 门试：两条通路都跑过全量回归

| 通路 | mcp 版本 | 结果 |
|---|---|---|
| 作者本机（1.x） | 1.29.0 | 138 通过 / 0 失败，89 次 HTTP 请求，rc=0〔实测 2026-11-10；那轮还没有第 4、5 条断言〕 |
| 干净 venv（1.x） | 1.30.0 | `pip install` rc=0；`--check-key` rc=0；stdio 客户端 `tools/call count` → `isError=False`、「命中 11918 条」；**全量 `--probe` 139 通过 / 0 失败，89 次请求**〔实测 2026-11-10，第 4 条断言时〕 |
| 临时 venv（2.x），**外部注入别名** | 2.3.0 | 138 通过 / 0 失败，89 次请求〔实测，旧计数〕——这一步的产出就是「要不要加兼容层」的决定依据 |
| 临时 venv（2.x），**走源码里的兼容层** | 2.3.0 | **139 通过 / 0 失败，89 次请求，rc=0**；`tools/list` 四个工具的 docstring 全在（`search` 描述 3392 字符）〔实测 2026-11-10〕 |
| 同一份件（2199 行）重装进两代 venv，`--probe` 串行间隔 8 秒 | 1.30.0 | 140 通过 / 0 失败，89 次请求，rc=0〔实测 2026-11-11，`_gate3.log`〕 |
| 同上 | 2.3.0 | 140 通过 / 0 失败，89 次请求，rc=0〔实测 2026-11-11，同一份日志〕 |
| 同一份件（2219 行，sha 前 12 位 `cac33ff5bf31`）重装进两代 venv | 1.30.0 | 144 通过 / 0 失败，89 次请求，rc=0〔实测 2026-11-11，`_gate5.log`〕 |
| 同上 | 2.3.0 | 144 通过 / 0 失败，89 次请求，rc=0〔实测 2026-11-11，同一份日志〕 |
| **现值**：同一份件（2228 行，sha 前 12 位 `f7dba1fea300`）重装进两代 venv | 1.30.0 | **145 通过 / 0 失败，89 次请求，rc=0**〔实测 2026-11-11，`_gate6.log`〕 |
| **现值**：同上 | 2.3.0 | **145 通过 / 0 失败，89 次请求，rc=0**〔实测 2026-11-11，同一份日志〕 |

断言数 138 → 139 的唯一原因是新增的兼容层断言（第 4 条）；139 → 140 的原因是新增的**包装层**
断言（第 5 条，锁 `search`）；140 → 144 的原因是剩下三条包装层通路（`count`／`fetch_abstracts`／
`links`，第 6-8 条）加一条 `_check_db` 大小写不敏感的离线断言（第 9 条）；144 → 145 是第 10 条——
`@mcp.tool()` 装饰**之后**的四个名字仍是协程函数（行为契约第 9 条后半句的承重前提）。
五次增长都不是有人改了判据〔依据：`_gate2.log`／`_gate3.log`／`_gate5.log`／`_gate6.log` 里
PASS 行的差异，本会话逐行看过〕。新增这几条全是**离线**的，所以请求数一直是 89，没有涨到 93。

## mcp 2.3.0 的逐字签名（本会话从 site-packages 直接读，不是网页摘要）

```
MCPServer.__init__(self, name: 'str | None' = None, title: ..., description: ...,
    instructions: 'str | None' = None, website_url: ..., icons: ..., version: 'str' = '',
    ..., log_level: Literal['DEBUG','INFO','WARNING','ERROR','CRITICAL'] = 'INFO', ...)

MCPServer.run(self, transport: Literal['stdio','sse','streamable-http'] = 'stdio', **kwargs) -> 'None'

MCPServer.tool(self, name: 'str | None' = None, title: ..., description: ...,
    annotations: 'ToolAnnotations | None' = None, icons: ..., meta: ...,
    structured_output: 'bool | None' = None) -> Callable[[_CallableT], _CallableT]
```

本文件对 SDK 只用到三件事，所以两代都能跑：

| 用到的 | 1.x | 2.x | 判定 |
|---|---|---|---|
| `FastMCP("PubMed")`（一个位置参数） | `name` 是第一参数 | `name` 仍是第一参数 | 兼容〔签名如上〕 |
| `@mcp.tool()` 裸装饰 | 支持 | 支持（参数全有默认值） | 兼容〔实测：`tools/list` 四个工具都在〕 |
| `mcp.run()` 默认 stdio | 默认 stdio | `transport='stdio'` 是默认值 | 兼容〔实测：客户端握手成功〕 |

## 2.x 确实变了的东西（四条都在**客户端回包**里；本服务代码不读这些字段）

〔以下四条均为本会话在 mcp 2.3.0 环境里的实测观察，逐条来自客户端回包。〕

**「本服务不读」这句本身差点害过我**：它当时被我当成「所以 2.x 的改动与我无关」，而 2.x 在
**服务端**这一侧也动了手脚——异常文本会被剥，见下面那一节。所以这个标题只说「这四条不影响本文件」，
**不要**读成「2.x 的变化止于客户端」；凡是「我不读那个字段，所以那个改动与我无关」这种推理，
先问一遍它有没有在服务端侧也改了东西。README 里那条【已作废】就是这句的存档。

1. 客户端结果的字段名换成 snake_case：`InitializeResult` 上是 `server_info`，不再是
   `serverInfo`（我第一版测试脚本因此炸了一次）。
2. `protocolVersion` 回 `2025-11-25`（1.29.0 时代本会话见过 `2024-11-05`；这是协商值，
   随客户端与 SDK 版本变，别把它当常量写进任何判据）。
3. `serverInfo.version` 在 2.x 下是**空串**（`MCPServer.__init__` 的 `version` 默认 `''`——
   注意这个空串是**服务端产出**的，只是由客户端读到，所以它不在「本服务不读所以与我无关」那一类里），
   而 1.x 下它跟着 SDK 版本走（装 1.30.0 就报 `PubMed 1.30.0`）。**所以 `serverInfo.version`
   不是本包的 `version`**（两者会不一样，别拿它当本包版本）：查本包版本请用
   `python -c "import importlib.metadata as m; print(m.version('pubmed-mcp'))"`。
   ⚠️【待确认】要不要在本文件里显式传 `version="1.0.0"`——`FastMCP` 1.x 有没有这个关键字
   本会话没核，没核就不加。
4. 返回 str 的工具调用在 2.x 下还多出一个非空的 `structuredContent`。
   ⚠️【待确认】它的生成规则与本文件的输出文本会不会分叉，没查。

## 服务端也变了：异常文本会被剥（本会话 A/B 实测，2026-11-11）

mcp 2.3.0 的 `mcp/server/mcpserver/tools/base.py` 对工具体抛出的异常分三支（逐字，本会话读源码〔实测〕）：

```
except (ToolError, ResourceError) as exc:
    raise ToolError(f"Error executing tool {self.name}: {exc}") from exc      # 原文带上
except Exception as exc:
    raise UnexpectedToolError(f"Error executing tool {self.name}") from exc   # 原文留在服务器
```

`NcbiError` 原先只继承 `RuntimeError`，所以走第三支。同一个 `search(sort="journal")` 拦截
（`_esearch` 第一件事就是 `_check_sort`，不发请求）：

| SDK | 未修前调用方看到的 | 修后 |
|---|---|---|
| mcp 1.30.0 | 323 字符，完整解释 | 323 字符，不变 |
| mcp 2.3.0 | **27 字符**：`Error executing tool search` | **323 字符，与 1.30.0 逐字相同** |

〔实测 2026-11-11，A/B 用 mcp SDK 自己的 stdio 客户端，两代各跑一次。〕

修法：`class NcbiError(_SdkToolError, RuntimeError)`，`_SdkToolError` 由顶部兼容层从**当前那代**
SDK 取（1.x `mcp.server.fastmcp.exceptions.ToolError`，其 MRO 是
`(ToolError, FastMCPError, Exception)`〔实测〕；2.x `mcp.server.mcpserver.exceptions.ToolError`）。
`RuntimeError` 留在第二基类，是为了不改变外面 `except RuntimeError` 一类的旧写法。

**四个工具都测了（2026-11-11 补测，此前只测了 `search` 与 `count`）**：入参一律选「在发 HTTP
请求之前就抛 `NcbiError`」的那条分支，所以这一轮不产生请求，也就不会和别的 venv 抢限速。

| 工具 | 触发入参 | 1.30.0 | 2.3.0 |
|---|---|---|---|
| `search` | `sort="journal"` | 323 字符 | 323 字符，逐字相同 |
| `count` | `field="ISSN"` | 244 字符 | 244 字符，逐字相同 |
| `fetch_abstracts` | `pmids="abc"` | 77 字符 | 77 字符，逐字相同 |
| `links` | `to="PMCID"` | 130 字符 | 130 字符，逐字相同 |

〔实测 2026-11-11，`_gate3.log` 第 19-23 / 31-35 行；服务端 = 分别装进两代 venv 的 `pubmed-mcp.exe`。〕

这轮补测**顺带撞出一个文案缺陷**：`links` 的报错原先写「只认库名，不认 'PMC'/'PMCID' 这类写法」，
而我先拿 `to="PMC"` 试，结果它**没报错**——回的是 `pubmed_pmc=PMC10461258`。原因是上一行本来就
`.lower()`，大小写不敏感，旧句子在它自己那条报错里反证了自己。已改口，作废说明留在代码注释里
（〔实测 2026-11-11，两代各一次反向对照：`to="PMC"` → `isError=False`〕）。

**为什么全量 `--probe` 当时抓不到包装层这个**：探针每条断言都**直接调协程**（`await search(...)`），
不经过 SDK 的 tool-call 包装层，于是「文本在包装层被不被剥」这一层在结构上不可见——
138/139 通过这句话本来就覆盖不到它。现在加了一条专门走 `mcp.call_tool("search", {...})` 的断言
（两代都支持两个位置参数，实测签名），它挂掉的意思是**主承诺被打穿**，不是文案不好看。
〔**2026-11-11 更新**：这句写下时成立——当时回归锁只有 `search` 一条，另外三个工具靠一次性实测。
现在四个工具**各有一条**走包装层的断言（`--probe` 第 6-8 条）＋一条 `_check_db` 大小写的离线断言
（第 9 条），所以「下次改动不会被探针拦住」对这四处不再适用。还**没有**断言钉住的东西列在下面
「还没验的」里，别把这条更新当成「全都锁好了」。〕

## 顺带修掉的两个（跟 mcp 2.x 无关）

- **下界是假的**：`mcp>=1.2` 里的 `1.2.0` 与 `1.21.0` 配 pydantic 2.14.3 在 import 阶段就抛
  `ImportError: cannot import name 'eval_type_backport' from 'pydantic._internal._typing_extra'`，
  `1.26.0` 才 `IMPORT_OK`〔实测 2026-11-11，三个干净 venv 各装一版跑 import〕。区间已改 `>=1.26,<3`。
- **2027 年的构建炸弹**：`license = {text = "MIT"}` 的表写法与 `License :: OSI Approved :: MIT License`
  分类符都被 setuptools 判弃用，警告逐字含「By 2027-Feb-18, you need to update your project and
  remove deprecated calls or your builds will no longer be supported」〔实测 2026-11-11 构建输出〕。
  已改 SPDX 字符串 `license = "MIT"`、删该分类符，并把 `build-system` 抬到 `setuptools>=77`
  （SPDX 写法要 77 才支持）。

## 已发布件走 README 头条命令的结果（2026-11-11）

干净 venv（不带系统 site-packages），逐字照 README：

```
pip install git+https://github.com/rsvsv/pubmed-mcp.git   → Collecting mcp<3,>=1.26 → mcp-2.3.0
                                                          → pubmed-mcp-1.2.0，PIP_RC=0，无弃用警告
pubmed-mcp --check-key                                    → rc=0，依赖: mcp 2.3.0, anyio 4.16.0, pydantic 2.14.3
stdio 客户端 initialize                                   → server=PubMed，version=''，protocol=2025-11-25
tools/list                                                → ['count','fetch_abstracts','links','search']
tools/call count("tooth resorption")                       → isError=False，「命中 11918 条」
tools/call search(sort="journal")                          → isError=True，323 字符，含 NCBI 原文与可用值列表
tools/call count(field="ISSN")                             → isError=True，244 字符
```

〔实测 2026-11-11，后台任务 `b1850yh8w`；该轮仓库 blob 与工作区 sha256 前 12 位同为
`7b090831f034`。〕

推上第 16 项文案改动之后，同一条命令又跑了一遍（commit `7468e55`，sha `d804df6810e6`），这次把
**四个工具**的包装层文本一起收：`search` 323 / `count` 244 / `fetch_abstracts` 77 / `links` 130 字符，
`links(to="PMC")` 反向对照 `isError=False`；装出来的那份 `.py` 与仓库 blob 逐字节同为
`d804df6810e6`〔实测 2026-11-11，后台任务 `bkeusabph`，`_pubfinal.log`〕。

再往后的 `21ca02b`（`.py` = `cac33ff5bf31`）那一轮，**第一次把全量 `--probe` 跑在「装出去的」那份上**
（`novice-pub2` 干净 venv → `pubmed-mcp.exe --probe`）：**144 通过 / 0 失败，89 次请求，rc=0**
〔实测 2026-11-11，后台任务 `bdizez4p6`，`_pubfinal2.log`〕。这件事以前一直没做过——之前几轮的
`--probe` 都是在**本地目录**装出来的 venv 上跑的，虽然文件逐字节一致，但「从 GitHub 拉下来装」
这条路径本身没经过自检。

## 还没验的（别把这份文件当成「2.x 全支持」）

- ⚠️【待确认】`anyio` 与 `pydantic` 的真实最低版本。区间写的是 `anyio>=4`，
  但没在低版本上跑过；实测过的组合只有 `mcp 1.29.0 / anyio 4.14.2 / pydantic 2.13.4`（本机）
  与 `mcp 2.3.0 / anyio 4.16.0 / pydantic 2.14.3`（临时 venv）。
- ⚠️【待确认】mcp 1.29.0 与 1.30.0 之间**逐项**有无行为差异。两版都跑过全量 `--probe`
  且都是 0 失败，但这只能说明「本文件用到的那三处没变」，不等于两个版本等价。
- ⚠️【待确认】`requires-python >=3.10` 仍是〔本项目口径〕：没有 CI，作者机器只有 3.13。
- ~~⚠️【待确认】`fetch_abstracts` 与 `links` 走包装层时的错误文本~~
  **〔已关掉 2026-11-11〕**四个工具都测了，结果见上面那张表；不能由 `search`/`count` 外推这句
  留着有效——它就是当初不外推、而是去实测的理由。
- ⚠️【待确认】mcp 1.26.0～1.28.0 这段下界区间只验到 `import` 成功，没跑过 `--probe`。

## 想自己复现这个坑

别在作者那种机器上复现——系统 site-packages 里是 1.29.0，装不出来。要这样：

```bash
python -m venv /tmp/t && /tmp/t/Scripts/python.exe -m pip install "mcp==2.3.0"
/tmp/t/Scripts/python.exe -c "import mcp.server.fastmcp"     # 想看到上面那条错误
```

复现编码类 bug 时**别加 `-I`**：隔离模式会忽略 `PYTHONIOENCODING`，结论会反过来。

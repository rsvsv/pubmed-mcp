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
- 依赖区间 `mcp>=1.2,<3`。上界留到 `<3` 而不是干脆不写：3.x 是没验过的下一代，
  这次的教训正是「把没测过的版本当默认」。
- 兼容层自己挂了一条 `--probe` 离线断言（「拿到的是可用服务类：能构造、有 `tool`、有 `run`」）。
  没有断言的承诺会被下一次改写悄悄作废——这个文件已经有一条这样的过期记录。

## 门试：两条通路都跑过全量回归

| 通路 | mcp 版本 | 结果 |
|---|---|---|
| 作者本机（1.x） | 1.29.0 | 138 通过 / 0 失败，89 次 HTTP 请求，rc=0〔实测 2026-11-10；那轮还没有第 4 条断言〕 |
| 干净 venv（1.x） | 1.30.0 | `pip install` rc=0；`--check-key` rc=0；stdio 客户端 `tools/call count` → `isError=False`、「命中 11918 条」；**全量 `--probe` 139 通过 / 0 失败，89 次请求**〔实测〕 |
| 临时 venv（2.x），**外部注入别名** | 2.3.0 | 138 通过 / 0 失败，89 次请求〔实测，旧计数：加上兼容层断言后 139、再加包装层断言后 140，现值见 README 与 release-delta〕——这一步的产出就是「要不要加兼容层」的决定依据 |
| 临时 venv（2.x），**走源码里的兼容层** | 2.3.0 | **139 通过 / 0 失败，89 次请求，rc=0**；`tools/list` 四个工具的 docstring 全在（`search` 描述 3392 字符）〔实测〕 |

最后两轮是**串行**跑的，中间隔 8 秒：两轮并发会在限速上互相干扰，那样 FAIL 归因不了。
断言数 138 → 139 的唯一原因是新增的兼容层断言（第 4 条），不是有人改了判据。

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

## 2.x 确实变了的东西（都在客户端侧，本服务不读）

〔以下四条均为本会话在 mcp 2.3.0 环境里的实测观察，逐条来自客户端回包〕

1. 客户端结果的字段名换成 snake_case：`InitializeResult` 上是 `server_info`，不再是
   `serverInfo`（我第一版测试脚本因此炸了一次）。
2. `protocolVersion` 回 `2025-11-25`（1.29.0 时代本会话见过 `2024-11-05`；这是协商值，
   随客户端与 SDK 版本变，别把它当常量写进任何判据）。
3. `serverInfo.version` 在 2.x 下是**空串**（`MCPServer.__init__` 的 `version` 默认 `''`），
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

**为什么全量 `--probe` 当时抓不到这个**：探针每条断言都**直接调协程**（`await search(...)`），
不经过 SDK 的 tool-call 包装层，于是「文本在包装层被不被剥」这一层在结构上不可见——
138/139 通过这句话本来就覆盖不到它。现在加了一条专门走 `mcp.call_tool("search", {...})` 的断言
（两代都支持两个位置参数，实测签名），它挂掉的意思是**主承诺被打穿**，不是文案不好看。

## 顺带修掉的两个（跟 mcp 2.x 无关）

- **下界是假的**：`mcp>=1.2` 里的 `1.2.0` 与 `1.21.0` 配 pydantic 2.14.3 在 import 阶段就抛
  `ImportError: cannot import name 'eval_type_backport' from 'pydantic._internal._typing_extra'`，
  `1.26.0` 才 `IMPORT_OK`〔实测 2026-11-11，三个干净 venv 各装一版跑 import〕。区间已改 `>=1.26,<3`。
- **2027 年的构建炸弹**：`license = {text = "MIT"}` 的表写法与 `License :: OSI Approved :: MIT License`
  分类符都被 setuptools 判弃用，警告逐字含「By 2027-Feb-18, you need to update your project and
  remove deprecated calls or your builds will no longer be supported」〔实测 2026-11-11 构建输出〕。
  已改 SPDX 字符串 `license = "MIT"`、删该分类符，并把 `build-system` 抬到 `setuptools>=77`
  （SPDX 写法要 77 才支持）。

## 还没验的（别把这份文件当成「2.x 全支持」）

- ⚠️【待确认】`anyio` 与 `pydantic` 的真实最低版本。区间写的是 `anyio>=4`，
  但没在低版本上跑过；实测过的组合只有 `mcp 1.29.0 / anyio 4.14.2 / pydantic 2.13.4`（本机）
  与 `mcp 2.3.0 / anyio 4.16.0 / pydantic 2.14.3`（临时 venv）。
- ⚠️【待确认】mcp 1.29.0 与 1.30.0 之间**逐项**有无行为差异。两版都跑过全量 `--probe`
  且都是 0 失败，但这只能说明「本文件用到的那三处没变」，不等于两个版本等价。
- ⚠️【待确认】`requires-python >=3.10` 仍是〔本项目口径〕：没有 CI，作者机器只有 3.13。
- ⚠️【待确认】`fetch_abstracts` 与 `links` **走包装层**时的错误文本：本次 A/B 只测了 `search`
  与 `count` 两条拦截。`--probe` 覆盖这两个函数但是直调协程，不经过 tool-call 包装层，
  所以「这两条在 2.x 下文本是否完整」仍是未测，不能由 search/count 的结果外推。
- ⚠️【待确认】mcp 1.26.0～1.28.0 这段下界区间只验到 `import` 成功，没跑过 `--probe`。

## 想自己复现这个坑

别在作者那种机器上复现——系统 site-packages 里是 1.29.0，装不出来。要这样：

```bash
python -m venv /tmp/t && /tmp/t/Scripts/python.exe -m pip install "mcp==2.3.0"
/tmp/t/Scripts/python.exe -c "import mcp.server.fastmcp"     # 想看到上面那条错误
```

复现编码类 bug 时**别加 `-I`**：隔离模式会忽略 `PYTHONIOENCODING`，结论会反过来。

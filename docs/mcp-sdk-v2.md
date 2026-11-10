# 为什么把 `mcp` 钉在 `<2`，以及要支持 mcp 2.x 还差什么

这份文件存在的原因：本仓库的头条安装命令在**作者机器上永远测不出这个坑**。

## 发生了什么（全部为本会话干净 venv 实测，2026-11-10）

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

作者本机是 `mcp 1.29.0`，`from mcp.server.fastmcp import FastMCP` 一直好用；
之前那次 `pip install --no-deps .` 也没暴露它，因为它复用了系统 site-packages 里的 1.29.0。
**教训**：只在本机验过的「依赖没问题」等于没验。本仓库现在的验收一律走干净 venv。

## 已按事实修好的部分

| 项 | 状态 |
|---|---|
| `pyproject.toml` / `requirements.txt` 收成 `mcp>=1.2,<2` | 已改 |
| 干净 venv `pip install` 解析到 mcp **1.30.0**，rc=0 | 〔实测 2026-11-10〕 |
| 安装后的 `pubmed-mcp --check-key` rc=0 | 〔实测 2026-11-10〕 |
| 用 mcp SDK 的 stdio 客户端连安装后的服务：`initialize` 成功、`tools/list` 返回四个工具、`tools/call count` 真拿到 NCBI 命中数 | 〔实测 2026-11-10，`isError=False`〕 |
| 1.29.0 与 1.30.0 之间有无行为差异 | **未测**（本机只有 1.29.0，1.30.0 只跑了上面三项） |

## 一个会误导人的细节

`initialize` 回包里的 `serverInfo.version` 跟着 **mcp SDK** 的版本走：装了 1.30.0 就报
`PubMed 1.30.0`〔实测，与 `pyproject.toml` 里 `version = "1.0.0"` 无关〕。
别人提 issue 时贴那串数字不代表本包版本——本包版本请用
`python -c "import importlib.metadata as m; print(m.version('pubmed-mcp'))"` 查。
⚠️【待确认】这是 FastMCP 的默认行为还是有参数可调，本会话没查源码。

## 要支持 mcp 2.x，需要哪些证据（目前一条都没有落定）

下面是**待证问题**，不是结论。想清楚再动代码，别拿「改个 import 名」当方案：

1. ⚠️【待确认】`mcp.server.mcpserver.MCPServer` 的构造签名与 `FastMCP(name, instructions=…)` 是否一致。
2. ⚠️【待确认】`@server.tool()` 的参数（`name`/`description`/`structured_output` 之类）有没有变——
   本文件的四个工具全靠装饰器 + docstring 生成给客户端的说明，签名一变，工具说明就静默过期。
3. ⚠️【待确认】`mcp.run()` 的默认传输与 stdio 行为是否不变。
4. ⚠️【待确认】v2 里 docstring → tool description 的机制是否还在（本文件依赖它，见 README 的
   Behavior contract 第 4 条那条「说明过期＝行为过期」的断言）。
5. ⚠️【待确认】若加 `try: v1 import / except: v2 import` 的兼容层，`anyio` 与 pydantic 的最低版本
   要不要一起改。

**为什么现在不加兼容层**：〔本项目口径，不是实测结论〕宁可只支持 1.x，依据就是上面那五条目前
全是【待确认】；在没有证据的环境里写 shim，风险是「装上了但工具说明静默过期」，本仓库不要一个
没验过的「也支持 2.x」。真要支持，得在装了
mcp 2.3.0 的环境里把 `tools/list` 与四个工具各一次真调用跑通，才算数。

## 如果你的环境已经有 mcp 2.x

`pip install` 本包会把 mcp 降到 <2，这可能影响你环境里别的 mcp 应用。两条路：

- 给本服务单独建一个 venv（或 `pipx`/`uv tool`），别和别的 mcp 应用共用环境；
- 或者按上面五条做完验证、把兼容层加出来并实测通过——欢迎提 PR，但请附上干净 venv 的输出。

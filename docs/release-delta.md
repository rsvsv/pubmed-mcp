# 发布版与开发版的差异（逐条留痕）

本仓库里的 `pubmed_mcp.py` **不是**作者机器上 `~/.claude/mcp/pubmed_mcp.py`（下称「开发版」）的
逐字节副本。下面的每一处都是**发布专用**改动：为什么改、依据是什么、由哪条断言钉住。
写这份文件的原因是——两份代码一旦分开，没有这张表，下一次发布一定会重新漂移一遍。

行数：开发版 2063 行 → 发布版 2129 行〔实测 `wc -l`，2026-11-10〕。差值全部来自本表列出的
发布专用改动，没有一处是「顺手重写」。

| # | 位置（发布版行号） | 改了什么 | 为什么 / 依据 |
|---|---|---|---|
| 1 | `_mask` :174 | 无条件把 key 整串替换成定长掩码 `***`；删掉「留前 4 位」与「长度 > 8 才掩」两个条件 | 审计 finding `privacy-04`。本会话实测复现两条：旧写法掩码后仍可见 `前缀+***`；8 位 key 时 `len(k) > 8` 判据根本不启用，整串原文进错误文本。新增离线断言 `_mask 不留任何 key 片段` 钉住。〔已知未处理边界：key 以 URL 百分号编码形态出现在报错里时 `str.replace` 仍替换不到——记为边界，不宣称已修。〕 |
| 2 | `--selftest` :1414 | `api_key()[:4]` → `_key_shape(api_key())` | `privacy-03`：同文件 `--check-key` 已声明「不留任何 key 片段」，而 `--selftest` 还在打印前 4 位——**声明被同仓库代码逐字反证**。CLI 输出一旦重定向进日志/对话，前缀就离开了「只在你自己机器上」这个前提。 |
| 3 | `_key_shape` 用户可见文案 :171 | 删掉「NCBI 的 key **一般是**十六进制串」这句未核实的母假设，改成「若你账号页上那串是纯十六进制而这份不是，多半抄漏/抄错了字符」 | `hex-common-knowledge-01`：这句本身是顺来的常识（开发版 :161 自己标了〔未核实〕），却出现在**用户可见输出**里、并被探针当必需行为钉住。 |
| 4 | `_key_shape` 注释 :160 与探针标签 :1992/:1996 | 「本机真 key 是**长度 36**」改成不点名真 key 的形状 | `key-length-metadata-published-by-A`：用开发版原文发布会把「作者 key 长 36、纯十六进制」这条**凭据元数据**公开三处。教训本身保留（长度不能当判据），数字不再指向真 key。 |
| 5 | `api_key` 注释 :149 | 「顺带吃掉换行/制表符」改成准确描述：吃首尾空白、中间连续空白压成一个空格；**key 中间真夹着空格时压不掉**，由 `_key_shape` 报出来 | `repro-join-does-not-eat-inner-ws`：`" ".join(text.split())` 并不吃内部空白，原注释名不副实。 |
| 6 | `_check_sort` 报错文案 :387 | 不再让读者「另跑 `journal_rank.py`」（本仓库不含该脚本） | `lineage-02` / `journal-rank-dangling-05`：指路指到别人机器上的文件，读者按提示去找会一无所获。 |
| 7 | `search` 工具说明 :1109-1114 | ① `sort` 从「只接受 pub_date / relevance」补成三个值（含 `author`）；② journal-rank 指路同上处理；③ 保留并转述「原文写死过作者工作目录的绝对路径」这条订正记录 | `stale-shijing-02` / `lineage-01` / `a-docstring-sort-count-06`：开发版 :351 已把「只有两种排序真生效」标为【已作废】，代码 `SORT_REAL` 与探针都认 `author`，**只有下发给 MCP 客户端的 docstring 不认**——读者只会从工具描述里看到两个值。新增离线断言 `search 的工具说明里 sort 三个值都在`，否则下次还会漂。 |
| 8 | 探针 :2003 之后 | 新增 3 条**离线**断言（200+HTML → `NcbiError`；docstring 三个 sort 值；`_mask` 不留片段）＋一行说明「分组号按补丁号命名，17-19 没新增断言所以跳号」 | `blanket-shijing-01`：README 承诺「每条都有源码依据」，而 `200 + HTML` 那半边此前只有假定式措辞、无任何断言覆盖。与其把承诺改弱，不如让它落地。 |
| 9 | 文件末尾 :2070 | 新增 `def main(argv=None)` 命令行入口；`if __name__ == "__main__": main()`；stdout/stderr 的 utf-8 `reconfigure` 一起搬进入口 | 让 `pip install` 后的 `pubmed-mcp` 命令可用（`lineage-03`：不搬 `reconfigure` 的话，编码兼容对 console-script 就失效了）。 |
| 10 | 同上的注释 | 订正开发版那句「cp936 与 cp1252 两种都复现 rc=1」，并补一条：**`-I` 会忽略 `PYTHONIOENCODING`，复现此 bug 别加 `-I`** | `repro-cp936-claim-too-wide` / `repro-dash-i-disables-env`。本会话实测：带 `-I` 时 `sys.stdout.encoding = utf-8`，不带是 `gbk`——所以拿 `-I` 验证会得出「旧版也没问题」的假结论。`--check-key` 的输出不含 `⚠️`，旧写法在 cp936 下其实 rc=0，两种码页都炸的是 `--probe`/`--selftest`/`--concurrent`。 |
| 11 | :1094-1114 SDK 兼容层 | `mcp.server.fastmcp.FastMCP`（1.x）取不到就取 `mcp.server.mcpserver.MCPServer`（2.x）；两代都取不到就抛**带处置命令**的 `ImportError`，不让别人停在一条裸 `ModuleNotFoundError` 上 | 本会话干净 venv 实测：原先 `mcp>=1.2` 会解析到 mcp 2.3.0，装完第一次运行即崩，而作者本机 1.29.0 永远复现不出来。逐字签名与两条通路的回归结果见 `docs/mcp-sdk-v2.md`。**这一层开发版没有**——它只在「发给别人装」的场景下才有意义，所以是发布专用改动。 |
| 12 | 探针 :2047 | 第 4 条新增离线断言：兼容层拿到的是可用服务类（可构造、有 `tool`、有 `run`） | 「同时支持 mcp 1.x 与 2.x」是一句会被下一次改写悄悄作废的承诺；没有断言就等于没支持。断言里带 `FastMCP.__module__`，跑起来能直接看出走的是哪一代。 |
| 13 | 模块 docstring :8-12 与 README 第 9 条 | 把「FastMCP 对非 async 工具是裸 `return fn(**kwargs)`、没有 threadpool」限定成 **1.x 行为**，并写入 2.3.0 的实测：该文件挪到 `mcp/server/mcpserver/utilities/func_metadata.py`，非 async 分支变成 `await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))` | 本会话读 mcp 2.3.0 源码实测。原断言不删、只加版本范围——因为它后面本来就挂了一句对冲（「SDK 若改成 threadpool，本文件的 async 写法依然正确，不必回退」），而这次正是那句对冲被触发的时刻。开发版没有这条。 |
| 14 | 顶部兼容层 + `NcbiError` 基类 + 探针第 5 条 | 兼容层从「MCP 工具」段挪到 `import anyio` 之后（基类必须在类定义之前拿到）；`NcbiError` 改成继承**当前那代 SDK 的** `ToolError`；多接一种普通 `ImportError`（「区间内但太老」抛的不是 `ModuleNotFoundError`，原先那句友好报错根本不触发）；新增一条**走 `mcp.call_tool` 包装层**的断言 | 本会话 A/B 实测〔2026-11-11〕：未修前同一个拦截在 mcp 2.3.0 下调用方只拿到 27 字符 `Error executing tool search`，NCBI 原文与改写建议全被剥——打穿的是主承诺「失败绝不静默」，不是文案问题。开发版连兼容层都没有，同样带这个缺陷。 |
| 15 | `pyproject.toml` 构建元数据与区间 | `license` 表写法 → SPDX 字符串；删 `License :: OSI Approved` 分类符；`build-system` 抬到 `setuptools>=77`；区间下界 `1.2` → `1.26`；版本 `1.1.0` → `1.2.0` | 两条都属于「现在能用、以后不能用」那一类：setuptools 弃用警告逐字含「By 2027-Feb-18 … your builds will no longer be supported」〔实测 2026-11-11 构建输出〕；下界是三个干净 venv 实测出来的（1.2.0／1.21.0 import 就炸，1.26.0 通），原先 `>=1.2` 承诺了一个跑不起来的下界。 |

## 这一版没做的事（免得被当成做了）

- **课题相关检索词没动。** 发布版源码里仍有 50 多处正畸/牙根吸收方向的检索词与中文词〔计数来源：
  审计 finding `privacy-05`，本会话未独立复核该数字〕。作者决定先重发、回头再评估。
- **不带 `journal_rank.py`、不带 lit-tools 的 `docs/`。** 那份目录里另有 31 处论文结果集标识
  （`tad_root` / `tadroot` / `shacheck`）已进 git 暂存区〔来源：`lineage-04`，本会话未复核〕，
  因此 `lit-tools` 这条路线在擦干净之前不作为发布副本。本仓库只带这一份 `.py` 加打包骨架。
- **`requires-python = ">=3.10"` 未经跨版本验证**，本仓库没有 CI；作者机器只有 3.13。这条下限错了
  也不会有人发现——所以它是〔本项目口径〕，不是实测。
- **【已作废 2026-11-10，本会话实测推翻】**这一条原先写「`pip install git+https://…` 的完整依赖解析
  没测，本地只做了 `--no-deps` 安装 + 入口可跑」。测了以后的结果是**装不上**：`mcp>=1.2` 解析到
  mcp 2.3.0，而 2.x 没有 `mcp.server.fastmcp`。原话留着不删，是因为「`--no-deps` 装通了」当时
  确实让我以为整条链路没问题——那句话正是这次漏检的根据。现状见 `docs/mcp-sdk-v2.md`。

## 验收记录（本会话，2026-11-10）

| 项 | 结果 |
|---|---|
| `python -m py_compile pubmed_mcp.py` | 通过 |
| 离线验收（`_mask`／docstring／`main()`／cp1252 重定向下 `--check-key` 的字节编码与 rc） | 11 项全 PASS |
| 开发版 `--probe`（改动前） | 135 通过 / 0 失败，89 次 HTTP 请求，rc=0 |
| **发布版** `--probe` | **138 通过 / 0 失败，89 次 HTTP 请求，rc=0**〔新增 3 条 = 138，数目自洽〕 |
| **发布版** `--probe`，加兼容层后的同一份源码（mcp **1.30.0**，干净 venv） | **139 通过 / 0 失败，89 次请求，rc=0**〔第 4 条断言＝139〕 |
| 同上，改跑 mcp **2.3.0**（临时 venv，走源码里的兼容层） | **139 通过 / 0 失败，89 次请求，rc=0** |
| 这两轮的并发方式 | **串行**，中间隔 8 秒。并发跑两轮会在限速上互相干扰，FAIL 就无法归因〔本项目口径〕 |
| 敏感串扫描（`e87b`／`k[:4]`／绝对路径／用户名） | 代码形状 0 命中；仅注释里引用旧写法处保留 |
| `pip install --no-deps .` 进临时 venv | 成功，产出 `pubmed-mcp.exe` |
| `pubmed-mcp --check-key`（走安装后的入口） | rc=0，输出与直接跑文件一致 |
| `pubmed-mcp` 不带参数 + 一条真实 MCP `initialize` | **握手成功**，回包 `serverInfo.name = "PubMed"`，stderr 干净 |
| `pip install git+https://…` 的完整依赖解析 | **第一次实测就是坏的**：`mcp>=1.2` 解析到 mcp 2.3.0，装完 `ModuleNotFoundError`，rc=1 → 见 `docs/mcp-sdk-v2.md` |
| 收成 `mcp>=1.2,<2` 后，干净 venv 重装（本地目录，走完整 PyPI 解析） | 解析到 **mcp 1.30.0**，PIP_RC=0；`pubmed-mcp --check-key` rc=0 |
| 用 mcp SDK 的 stdio 客户端连**安装后的** `pubmed-mcp.exe` | `initialize` 成功、`tools/list` = 四个工具、`tools/call count("tooth resorption")` → `isError=False`，回文「命中 11918 条」＋NCBI 实际执行串 |
| **发布件** `b1c1851` 走 git+https 的干净 venv 全流程 | 默认解析到 **mcp 2.3.0**（区间已是 `<3`）、`pubmed-mcp 1.1.0`；`--check-key` rc=0；stdio 客户端：`version=''`、`protocol=2025-11-25`、四个工具描述长度 3392/667/520/972、`count` → `isError=False`；**site-packages 里那份与已发布 blob 逐字节一致**（sha256 前 16 位 `7c7b83bb767173fc`） |
| 上一条为什么值得单独记 | 第一次比哈希是**对不上**的——因为我在装完之后又改了工作区的 docstring。拿 `git show <blob>` 当对照才成立。写下来是因为「我自己的时序」也会造出假信号 |

## 待办：两份代码要怎么合

开发版 `~/.claude/mcp/pubmed_mcp.py` 仍带上面 1–5、7–12 里的旧行为（其中 9、11、12 —— `main()`
入口、SDK 兼容层、兼容层断言 —— 是**发布版才有**的，因为它们只为「别人装」这个场景服务），
而 `~/.claude.json` 的注册路径
**指向的是开发版**——也就是说作者此刻在 Claude 里用的还是那份会打印 key 前缀的 `_mask`。
收口方式二选一，别两边都改：

1. 把上表 1–5、7–8 回灌进开发版，注册路径继续指它；或
2. 注册路径改指本仓库这份，本仓库成为唯一真源（另一会话正打算把 `lit-tools` 收成唯一副本，
   真要合流得先和它对齐，否则机器上会有第四份）。

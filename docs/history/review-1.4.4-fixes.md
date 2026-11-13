# 外部审查修复（1.4.5）

外部审查针对 1.4.4 的 sdist，作者自述与上游 tag `v1.4.4` 字节一致。审查结论视为〔未核实，来源：外部审查〕。下表每一条都先在本机复现，复现成立才修〔实测 2026-11-13〕。

## 已修

| 级别 | 问题 | 复现 | 修法 |
|---|---|---|---|
| P0 | 多余或拼错的参数被静默丢弃 | `call_tool("export", {..., "formt": "csv"})` 不报错，照写 pmids 文件；`tools/list` 中 8 个工具的 schema 都没有 `additionalProperties` | 在 `@mcp.tool()` 注册前把 SDK 的 `ArgModelBase` 设为 `extra="forbid"`，1.x 与 2.x 各取对应模块。拿不到时不静默：stderr 告警、`--check-key` 打印、`--probe` 记 FAIL |
| P1 | 带空格的千分位 PMID 能绕过拦截 | `_parse_pmids("1,234, 567")` → `['1','234','567']` | 逐 token 判断前先剥掉首尾 ASCII 逗号 |
| P1 | `fetch_abstracts` 没有 PMID 数上限 | 代码中无上限常量 | 新增 `FETCH_MAX = 2000`〔本项目自订〕，超出时在联网前报错，并给出批数 |
| P1 | CI 只跑 Linux | 见 `regression.yml` | 矩阵加 `windows-latest`，并统一用 bash（pwsh 下 `"$MCP_SPEC"` 不是环境变量） |
| P2 | 短 key 掩码把诊断文本打花 | key=`f` 时，`failed` 变成 `***ailed` | key 短于 8 字符〔本项目自订阈值〕时，只掩 `api_key=<值>` 这一种形态 |
| P2 | 无卷号时题录变成 `:123-30` | `_record_parts(..., pages="123-30")` | 无卷号时只写页码 |
| P2 | as_json 的 `ptag` 带有 `｜` | search as_json 中出现 `'ptag': '｜Review'` | `ptag` 只存类型，`｜` 改由文本输出时加 |
| P2 | `_int` 的 `default` 参数从未被读取 | `_int("", "retmax", 20)` 照样报错 | 删除该参数，`lo`/`hi` 改为仅限关键字 |
| P2 | 执行式为空时，export 仍打印一行空的「NCBI 实际执行：」 | export 的 querytranslation 为空 | 为空时不打印；超过 600 字时按 `_note` 的规则截断 |
| P2 | 写文件和算 sha256 在事件循环里同步执行 | 代码审读 | CSV/MEDLINE/RIS/pmids 的写盘、sha256、`out_path` 落盘都改用 `anyio.to_thread` |
| 附带 | MEDLINE 导出会写入无效号的 `1. ` 占位 | 与 1.4.4 修过的 fetch 碎片是同一现象 | 只写含 `PMID-` 行的批次 |

## 文档与证据

- **README：**
  - 「本仓库没有 CI」和「百分号编码形态的 key 掩不到」两处已在行内标为已作废，原文保留。
  - 1.4.3 一行改为「供 Zotero/EndNote 导入（未做真机导入测试）」。
  - export 一节注明：切片核对只能发现重复和计数漂移，发现不了跳号。
  - links 一行补上 `citedin`/`refs`。
  - 「版本要求」一节新增说明：多余参数一律报错，以及这样做的取舍。
- **其他文档订正：**
  - `docs/abstract-slim.md`：测试条数 19 是 1.4.2 时的数字，现为 25。
  - `docs/query-lint.md`：描述长度 2413 是 1.4.1 时的数字，现为 2563。
  - systematic-review skill 的导出格式补上 `ris`。
- **证据文件：** `history/evidence/sr_live_acceptance.log` 原本被 `.gitignore` 的 `*.log` 规则排除，没有入库，现已补入。该文件第 87–97 行有 73612、78474 两个数〔实测 2026-11-11 原始输出〕。

## 测试

- **改紧原有断言：**
  - `test_sr_tools`：原先检查 `.part` 残留的断言永远为真（mkstemp 的实际命名是 `basename.<随机>.part`），改成按 glob 查找残留文件。
  - `test_sr_tools`、`test_search_modes`：`assertRaises(Exception)` 改为指定异常类型并检查报错文本。
- **新增测试：** `tests/test_review_145.py`，共 11 条。其中 MeSH 补充概念用真实抓包做 fixture（`tests/fixtures/mesh_scr_biodentine.json`，NCBI 原始响应，实测 2026-11-13），不再用手写的 dict。
- **离线测试：** 171 条，以下 5 个环境全部通过〔实测 2026-11-13〕：
  - Python 3.13 + mcp 1.29.0
  - Python 3.13 + mcp 1.30.0
  - Python 3.13 + mcp 2.3.0
  - Python 3.10 + mcp 1.26.0
  - Python 3.10 + mcp 2.3.0
- **联网与烟测：** 联网 `--probe` 在 mcp 1.29.0 和 2.3.0 下各跑一轮，都是 174 通过、0 失败，其中新增 1 条「未知参数必须被拒」的包装层断言。stdio 烟测在两代 SDK 下都通过。
- **A/B 对比：** 49 组中 45 组逐字一致；4 组不同，都是 search as_json 的 `ptag` 不再带 `｜`，这是本次有意的修复〔实测 2026-11-13〕。

## 未修，及理由

- **把 pydantic 的英文校验报错转成中文：未做。** 这类报错由 SDK 在调用本工具函数之前抛出，要转成中文就得改 SDK 的异常路径，太脆弱。英文原文已经写明了字段名和允许值，测试也改为检查这两项。工具函数里自己的中文校验仍保留，直接调用协程（自检、测试）时会用到，并不是死代码。
- **`"1,2345, 678"` 仍按三个号读取。** 2345 是四位数，不符合千分位分组，读成「逗号分隔的列表」没有歧义。外部审查把它和 `1,234, 567` 归为同一类问题，这里不同意。
- **`_ids_in_window` 无法发现跳号：** 只在 README 中降低了措辞，没有改算法。思路可以参照路线图第 4 批的整窗剥离法。
- **Zotero/EndNote 真机导入：** 仍未测试。

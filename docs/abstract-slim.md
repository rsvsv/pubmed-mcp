# 摘要精简与落盘（1.4.2）

对应审查报告 #11 #50（摘要太长，模型拿不到）、#14 #20（无结构化摘要、无文献类型）、#13（撤稿关联只在正文里）。

## 问题

`fetch_abstracts` 返回 NCBI 原排版文本，其中带作者单位、版权、利益冲突声明。
实测 60 篇约 16.7 万字符〔实测 2026-11-13〕。审查子代理观察到：60 篇约 15.7 万字符被客户端整条拒收，20 篇正常〔未核实，来源：子代理〕。
拒收时模型一条摘要也看不到。

## 做法

不传新参数：输出与 1.4.1 逐字相同，A/B 49 组通过〔实测 2026-11-13，`pubmcp-audit/ab_sr_change.py`〕。
唯一变化：正文超过 `FETCH_WARN_CHARS=60000`（本项目自订）时，末尾追加一行 ⚠️，提示改用 out_path 或 fields。这属于项目允许的纯新增警告行。

新增可选参数：

- `fields`：非空时改走 `efetch rettype=xml`，用 ElementTree 解析，只渲染所列字段的紧凑文本，一条记录一块。
- `as_json`：同一解析层的 JSON 输出。只给 as_json 时字段取 `ABSTRACT_DEFAULT_FIELDS`。
- `out_path`：整份结果（默认原排版 / 紧凑文本 / JSON）写入新文件，只回路径、字节、sha256。
  - 写法同 export：联网前检查路径不存在 → 写 `.part` → 关 fd → 原子不覆盖发布 → 清理 `.part`。

解析层 `_efetch_xml_chunk` 采取 fail closed：HTML 故障页、XML 解析失败、根元素不对、未知记录类型、PMID 重复或不在请求里，都报 NcbiError，不退回文本档冒充。
请求号缺失（NCBI 不报错，只是少一条）的说明口径与默认档一致：「取不到条目本身，不是没有摘要」。

## 实测要点（2026-11-13，14 条混批）

样本：Cochrane、中文刊（含 OtherAbstract chi）、团体作者、撤稿/撤稿声明/关注声明、StatPearls 与 GeneReviews 书、无摘要 Letter、无页码 BMC。

- 无效号 `9999999999`：返回空 `<PubmedArticleSet></PubmedArticleSet>`，不报错。
- 撤稿关联：`CommentsCorrections RefType=RetractionIn`。40272285 那条只给 DOI、没有 PMID，JSON 中 `pmid: null`，不编号。
- 书记录（`PubmedBookArticle`）的树形与期刊完全不同，单独解析，`source="book"`，期刊位置放书名。
- 无页码文章（42458410，BMC）的文章号只在 `ELocationID EIdType=pii`（"1960"），记为 `article_number`。
- 内联标签 `<i>/<b>/<sup>/<sub>`：NCBI 文本档直接去标签拼接，本解析层也只取文字，不加 `^`/`_`。
  XML 解析摘要与同批文本档去空白后逐段比对，0 处不符（14 条全部记录；离线回归固定其中 4 条）。

## 体积（实测 2026-11-13，60 篇 `"root resorption"[tiab] AND 2023:2025[dp]`）

| 方式 | 字符 | 相对原排版 |
|---|---|---|
| 原排版（默认） | 167,062 | 100% |
| 紧凑文本，默认字段 | 115,131 | 69% |
| 紧凑文本，`fields=title,abstract` | 106,160 | 64% |
| JSON，默认字段（作者压成字符串后） | 137,787 | 82% |
| JSON，`fields=title,abstract,pt` | 114,545 | 69% |
| `out_path` 回传 | 约 200 | — |

结论：精简只省三成多，因为摘要正文本身占大头。大批量的可靠解法是 `out_path`，60 篇落盘后只回一行。

## 测试

`tests/test_abstract_modes.py` 共 19 条。fixture 来自上述实测响应，已裁剪（删参考文献、单位、研究者名单）：
`tests/fixtures/efetch_sample.xml`、`efetch_sample_abstract.txt`。MANIFEST 已把 `*.xml *.txt` 纳入 sdist。

- 离线全套 144 条：mcp 1.29.0（本机）、1.30.0、2.3.0 均通过〔实测 2026-11-13〕。
- 源码 stdio 烟测：三个版本均 PASS。
- 工具描述总长 2571（上限 2600，余 29，本项目自订上限）。

## 未做 / 限制

- 没有 `max_chars` 按条截断：审查发现原排版文本中，PMID 行后还有利益冲突声明，按 PMID 行切会丢内容。按条截断要走 XML，暂不做，`out_path` 已覆盖需求。
- 没有 `search detail="abstract"`：会改 schema enum（两处断言），描述预算也不够。
- `DeleteCitation`（已删除记录）只做识别跳过，没有真实样本。
- 客户端结果上限的真实数值〔未核实〕；60000 只是提示阈值（本项目自订）。

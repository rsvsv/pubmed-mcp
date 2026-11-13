# 本轮审计与修复记录

后续修订见 [边界修复记录](boundary-fixes.md)。本文保留首次审计快照；下文“重试仍在同步请求内部”的未解决状态已被后续逐次限速修复取代，多进程全局限速仍未实现。旧查询解释的作废理由也见后续记录。

本轮以主仓库源码为修改对象，先核对其与注册 MCP 文件逐字一致，再保存修改前备份。旧 lit-tools 副本不作为发布来源。审计代理报告仅用于寻找线索；下列已修项由主流程复现、阅读原始响应或执行离线故障注入核对。

## 已复现并修复

| 问题 | 直接证据 | 本轮处理 |
|---|---|---|
| 小写 and 被并入标题短语 | 实测：`miniscrew[Title] and stability[Title]` 命中 3，实际执行包含 `"and stability"[Title]`；大写 AND 命中 65。来源：tests/fixtures/audit_queries.json，对应 term 的 raw.count/querytranslation | 对这种可见改写明确提示改用大写 AND；不偷偷更改原检索式 |
| 开头的 NOT 被忽略 | 实测：`NOT stability[Title]` 的 raw.querytranslation 仅保留正向 stability 条件，warninglist.outputmessages 包含 NOT；来源同上 | 明确解析警告，零结果也不得推断主题无文献 |
| 混合查询丢失中文 | 实测：`牙根吸收 resorption` 的实际执行式只含英文词；来源同上 | 点名未保留片段，提示换对应英文词重查 |
| 未配对括号/引号被忽略 | 实测：fixtures 中对应 term 的 warninglist.outputmessages 和 querytranslation | 保留检索式并加具体语法警告；不自动改写 |
| OR 日期被解释成同时满足 | 实测：fixtures 的 `miniscrew[Title] AND (2019[dp] OR 2035[dp])`，原说明产生不适用的日期不相交警告 | 按布尔结构提取每个分支都要求的日期；OR 取交集、AND 取并集、NOT 不把右侧当必需条件 |
| JSON 错误包装未拦 | 离线实测：`eutilsresult.ERROR` 在旧实现被作为成功对象返回；见 InfrastructureRegression.test_nested_errors | 拦截嵌套及顶层错误；非法顶层类型同样报错；异常正文脱敏 |
| 异常 Retry-After | 离线实测：Unicode 上标字符使旧实现抛 ValueError；超长等待值原样被接受；见对应 InfrastructureRegression 测试 | 容错非标准数字；过长服务端等待要求直接报错，不提前重试 |
| 数值校验静默截断 | 离线实测：旧 `_int(1.9)` 返回 1，infinity 抛裸 OverflowError；见 test_numeric_inputs | 拒绝浮点和布尔输入；保留整数及整数字符串 |
| URL 编码凭据未脱敏 | 离线实测：伪 key 的 URL 编码形式；见 test_masks_url_encoded_key | 同时替换原串、quote、quote_plus 形式；不读取或输出真实 key |
| 元数据少回仍宣称给齐 | 离线实测：模拟 esearch 给 2 个 ID、esummary 只回 1 条；见 test_missing_metadata_and_no_json_query_duplication | 文本表头改报实际记录数，保留缺失警告 |
| 文献类型被截断 | 离线实测：构造含 Journal Article、Review、Retracted Publication 的记录；见 test_publication_types_not_truncated | 不浪费位置显示普通 Journal Article，保留所有特殊类型 |
| 无匹配被说成文章不存在 | 来源：旧 `_zero_explanation` 兜底句及 QueryRegression.test_zero_claim_is_query_limited | 改成仅对实际执行检索式作结论 |

## 输出与调用成本

- 工具历史说明逐字移到 [tool-description-history.md](tool-description-history.md)，包括原有未核实/作废标记；当前工具描述仅保留调用规则。
- 实测 MCP `list_tools` 描述总字符：修改前 6368，修改后 1325；来源：evidence/delivery_metrics.json 的 descriptions。与 AST 清理后的 docstring 字符计数不是同一口径。
- 推算字符减少约 79.2%，登记 DV-AUDIT-DESC；不能证明 token、费用或任务总耗时同比减少。
- 实测固定短语案例的说明字符：修改前 569，修改后 418；来源：上述 metrics 的 `"zzzqqx nonexistent term"` 条目。推算字符减少约 26.5%，登记 DV-AUDIT-PHRASE；不能推广到其它查询或 token 比例。
- JSON 中完整 querytranslation 仍保留，不再在 warnings 重复复制。文本仍显示实际执行式，原有截断提示及完整 JSON 获取路径保留。
- 删除逐条 PMC 开放获取断言和重复图例，明确 PMC 关联不保证开放许可或当前可下载性。
- 需要题录时直接 search，不先 count 再重复 search；search 已给 PMC 时不重复 links。摘要仍保留原排版，避免删掉有意义的作者单位或声明。
- 语义异常的查询会增加警告字符，这是修复漏报的必要成本；本轮没有承诺所有输出都更短。

## 验证

- 实测原有完整探针：169 通过 / 0 失败，92 次 HTTP 请求；来源：evidence/probe_after.log 末行。此为本次快照，不是未来固定验收数。
- 实测新增离线测试：20 项通过；来源：evidence/offline_after.log 末尾。涵盖固定 NCBI 响应回放、合成故障、正常反例、描述与 SDK 一致性。
- 实测新 stdio 进程：search、count、fetch_abstracts、links 均调用成功；来源：evidence/delivery_metrics.json 的 stdio。
- 本轮测试环境实测 mcp 1.29.0；来源：主流程 importlib.metadata.version 工具输出。SDK 另一代本轮未重新测试，历史通过记录不能冒充本轮验证。
- 主流程运行 git diff --check 无空白错误；独立审阅未报告新增缺陷，但这不是全面无 bug 的证明。

离线测试（不访问 NCBI）：

```bash
python -I -m unittest discover -s tests -v
```

完整联网回归：

```bash
python -I pubmed_mcp.py --probe
```

## 已知边界与未完成验证

- 原有 urllib socket timeout 不是整个工具调用的总时限；慢速持续响应的全程截止策略本轮未改，不能宣称已解决。
- 限速器按进程工作，重试仍在同步请求内部；多进程共享 API key 和重试同时发生的全局限速未解决。本轮未增添自动大规模并发探测。
- `fetch_abstracts` 仍返回原排版全文摘要；尚未增加 XML 精简模式、输出分页或共享缓存。此类改动需要单独核对摘要完整性、缓存时效和错误传播，未当作已完成收益。【部分已过时，2026-11-13：1.4.2 已加 XML 精简（fields/as_json）与落盘（out_path），见 docs/abstract-slim.md；输出分页与共享缓存仍未做】
- `_mask` 对极短错误 key 仍可能遮盖普通文本中的同字符；本轮优先确保不泄露凭据，不宣称该可读性问题已消失。
- JSON warnings 去除了 querytranslation 重复行；依赖解析中文文案的下游需改读现成 querytranslation 字段。
- 原文历史中“PubMed 只索引英文”等绝对表述缺乏完整依据，当前新增说明只报告观察到的查询改写；历史归档不是事实背书。
- 前一轮工作流大规模展开后触发模型额度限制，未完成的代理验证没有计入本轮通过项；不以代理的 confirmed 标签代替本轮复现。

原有测试中的固定时间窗口与实时记录依赖仍需留意。将来每修一个解析坑，优先加入原始响应 fixture 和正常反例；不把所有历史背景重新塞回 MCP 工具描述。

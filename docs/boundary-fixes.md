# 检索解释、响应完整性与重试边界修复

本记录对应后续边界修复。证据中的响应是工具实测，故障注入中的文献号、等待时长和返回数量为本项目自订测试输入，不是文献事实。测试源码见 `tests/test_boundary_regressions.py`；原始输出保存在 `docs/audit-evidence/boundary_*`。

## 修复内容

| 问题 | 修复后的行为 | 核对依据 |
|---|---|---|
| 将 phrasesnotfound 解释为词已被丢弃 | 只报告 NCBI 未找到片段，不据此解释为剩余词的命中数 | 实测：boundary_live.json 的 count 结果；离线 test_missing_phrase_does_not_imply_dropped |
| JSON 字段仍暗示已丢弃 | 新增 phrases_not_found；保留 dropped_phrases 兼容别名，并用 deprecated_fields 明确弃用旧解释 | 实测：boundary_live.json 的 search 输出；离线 test_json_missing_phrase_has_accurate_name_and_deprecated_alias |
| 空执行式被解释成没有任何条件 | 只说 NCBI 未返回执行式；中文查询建议用对应英文词重查 | 离线实测：test_empty_translation_does_not_prove_empty_conditions |
| 词异常遮住分页越界、字段核对 | 优先解释越界；字段是否出现独立依据执行式核对 | 离线实测：test_paging_precedes_missing_terms、test_field_is_checked_even_with_missing_phrase |
| 元数据缺失被说成 PMID 不存在 | 区分正常、不可用、未知；合法 elink 关联不被缺失元数据覆盖 | 离线实测：test_partial_summary_states、test_valid_link_survives_missing_metadata |
| esummary 错误壳计作记录 | 过滤不可用记录；响应结构异常、后台错误、内外 PMID 冲突抛错 | 离线实测：ResponseBoundary 对应测试；身份冲突是合成故障，未观察到 NCBI 实际返回此冲突 |
| efetch 错误正文被算作无记录 | 拒绝无法识别的无 PMID 正文、重复或非请求 PMID；保留已核对的空占位及无摘要题录 | 离线实测：ResponseBoundary 对应测试；实测：boundary_live.json 的 fetch_abstracts 输出 |
| SDK 将布尔或小数先转成整数 | 在 MCP 参数入口拒绝布尔、小数，保留整数和整数字符串 | 实测：boundary_live.json 的布尔参数错误；离线 test_sdk_rejects_boolean_float_and_decimal_string |
| 内部重试绕过限速、取消留下预约空洞 | 每次请求尝试都经过限速；取消后不再发新重试，取消等待不占未来时隙 | 离线故障注入实测：TransportBoundary 全部测试 |

## 作废记录

- 「phrasesnotfound 就是已丢弃、计数只来自剩余词」已作废，日期：2026-11-11〔修订记录〕。理由：实测 `miniscrew[Title] AND zzzqqx` 仍命中零条，工具输出见 boundary_live.json；错误数组本身不足以证明词是否被移除。
- 「esummary 缺返回或空壳证明 PMID 不存在」已作废，日期：2026-11-11〔修订记录〕。理由：缺失可能是未知状态；响应错误不能当不存在的证据。旧依据保留在源码历史注释中。
- 「PubMed 只索引英文」及「空 querytranslation 证明执行条件为空」已作废，日期：2026-11-11〔修订记录〕。理由：旧查询样例不足以支持这两个绝对结论；历史归档保留，但不再用于当前解释。

## 验证与兼容性

- 实测离线回归：41 项通过；来源：audit-evidence/boundary_offline_complete.log 末尾。包括原有回归及本轮新增边界测试。
- 实测完整联网探针：169 通过、0 失败，92 次 HTTP 请求；来源：audit-evidence/boundary_probe_final.log 末行。探针启动后的最后一处零结果提示措辞调整由上述离线测试覆盖。

- 实测新 stdio 进程：search、count、fetch_abstracts、links 均完成正常调用；布尔日期参数返回工具错误。原始输出：boundary_live.json。
- 实测本机依赖：mcp 1.29.0、anyio 4.14.2、pydantic 2.13.4；来源：主流程 importlib.metadata.version 输出。本轮没有重测另一代 SDK 或整个依赖版本区间，历史通过记录不代替当前验证。
- 严格参数校验属于有意改变：过去被静默转换的错误输入现在报错。JSON 旧字段继续存在，调用方应迁移到 phrases_not_found，不能按旧字段名推断词已被丢弃。
- 本轮没有测量 token 减少比例。增加错误判定及兼容说明会使部分异常输出变长；原文摘要仍完整保留。

## 保留边界

- 正在执行的 urllib socket 读取仍不能立即强制终止；本次测试证明取消后停止后续重试，不证明整个调用具有总截止时限。
- 限速仍按进程实施；多个进程共享凭据的全局限速未实现。
- efetch 校验不等于逐篇内容完整性校验；含合法 PMID 的部分正文损坏仍可能无法识别。
- links 大列表仍有显示上限；本轮未增加分页、摘要精简、共享缓存或自动 CI。

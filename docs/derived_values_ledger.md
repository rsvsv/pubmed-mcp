# 本轮推算量登记

| 条目 | 值 | 来源类型 | 依据 | 禁止用途 |
|---|---|---|---|---|
| DV-AUDIT-DESC | 描述字符减少约 79.2% | 本项目推算 | MCP list_tools 实测描述字符总数由 6368 降为 1325；计算式 (6368−1325)/6368，原始记录见 history/evidence/delivery_metrics.json 的 descriptions | 不能证明 token 数、费用或任务总耗时同比减少；不能证明检索质量提升 |
| DV-AUDIT-PHRASE | 固定短语案例说明字符减少约 26.5% | 本项目推算 | 固定响应回放的说明字符由 569 降为 418；计算式 (569−418)/569，原始记录见 history/evidence/delivery_metrics.json 的 note_sizes | 不能推广到其它查询，不能证明 token 或费用同比减少 |
| DV-SR-DEDUP | dental implant 导出切片间重复 4862 个 | 本项目推算 | 计算式 78474−73612；两输入均为实测（verify_sr_live.py：切片计数和 78474、去重后唯一 73612，见 history/evidence/sr_live_acceptance.log）| 不能证明所有多发表日期记录都被正确去重；不能推广到其它查询；重复成因（多发表日期）为实测警告所述的可能解释，未逐条核实 |

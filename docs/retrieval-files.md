# 补检、引用候选与可续跑导出（工作区未发布）

本文的命令与字段为本项目自订接口。示例文件名是占位路径，请换成自己的文件；任何正式输出都不覆盖已有文件。本文随测试收尾更新验收状态。

## 导出文件附说明

调用 `export` 时显式传 `metadata=True`，数据旁生成 `<文件名>.meta.json`，记录查询、执行式、计数、缺失号、文件哈希和完成时间。默认不开启。

数据写成功但说明文件写失败时，数据保留，结果返回 `metadata_status=failed`。两个文件分别发布，不声称同时原子完成。

```bash
python pubmed_mcp.py --verify-export results.ris
```

搬动文件后可用 `--meta` 明确指定说明文件。`verified` 仅表示数据哈希与说明相符；不证明检索查全。缺文件、缺说明、说明损坏、哈希不符分别报告。数据文件读取失败单独返回 `unreadable_data`，读取前已被移走则返回 `missing_data`，不归为说明损坏。

日志报告可显式加 `--verify-files`，同时检查当前数据与说明是否相符，以及数据是否匹配日志保存的哈希。`metadata_verification_status` 和 `event_binding_status` 分别报告；文件被另一轮产物替换时为 `event_hash_mismatch`，日志缺少有效哈希时为 `event_hash_unavailable`。没有伴随说明或说明损坏时，仍独立核验日志哈希，两个状态分别保留。不自动搜寻孤立文件。

## 按入库日期补检

`search`、`count` 和 `export` 支持 `datetype="edat"`，用 `mindate`、`maxdate` 设入库区间；默认 `pdat` 保留发表日期语义。用户检索式内的日期条件保持原样，与参数条件取 AND。仅选日期类型却不给区间或相对天数不会筛日期，返回提示；导出记录用 `datetype_applied` 明示是否应用参数日期条件。

`search/count` 的 `reldate` 原样交给 NCBI；`export` 先从 NCBI 的独立日期回显固定绝对区间，拿不到可靠回显时要求使用显式日期。不会用本机日期猜区间。

显式 edat 的超窗导出在发布前拒绝：入库日期拆片尚未经联网集合验证。请缩小区间分别导出。edat 不能覆盖所有旧题录后续内容变化；必要时完整重跑再比较名单。重叠区间由综述方案自订，本工具不替你设定。

## 引用结果留全

`links` 新增 `as_json`、`out_path`、`offset` 和 `limit`。默认文本仍按旧上限展示；JSON 可获得来源关系，文件保存本次全量结果。分页按每个源分别进行，计数在分页前计算；不同调用不是同一数据库快照。

esummary 存在性检查失败时，来源状态保留为 `unknown` 并附警告，继续尝试独立的 elink 关联请求。

每条边保留来源 PMID、方向和目标 ID。只有 PubMed 目标进入 `candidate_pmids`，PMC、Gene 等 ID 不会冒充 PMID。部分请求失败会列明；全部失败报错。PubMed 未录入引用不代表论文没有引用。清单命令拒绝分页后的引用 JSON，要求用 `out_path` 保存全量结果；部分源失败的全量文件仍须核对警告。

## 名单比较与直接导出

输入支持纯 PMID 文本和本项目已知格式的候选 JSON。文本支持空白/逗号分隔、首部 BOM、前导零归一和保序去重；疑似千位分隔数字、非法内容、未知 JSON 格式明确拒绝。空名单在集合运算中合法。

```bash
python pubmed_mcp.py --diff-files previous.txt current.txt --out differences.json
```

输出新增、未再出现和共同 PMID，保留两份输入的哈希。`removed_pmids` 只是旧名单有、新名单没有，不代表 PubMed 删除了文献。若新名单只覆盖补检入库区间，区间外的旧文献也会出现在这里；保留原完整基线，不用补检名单替换。文件及 CLI 返回摘要均附带这段解释。新增保持新名单顺序，其余保持旧名单顺序。差集 JSON 的 `candidate_pmids` 只包含新增文献；引用和滚雪球 JSON 也只保存候选。它们均不能作为完整基线，基线请使用完整 PMID 文本清单。

```bash
python pubmed_mcp.py --export-pmids differences.json --out additions.ris --format ris
```

直接按名单抓题录，不先拼成搜索式。支持 pmids、csv、ris、medline；缺失题录完整列出。CSV 缺元数据仍有占位行；RIS/MEDLINE 只计实际写入的题录。

## 单轮追引用

```bash
python pubmed_mcp.py --snowball seeds.txt --baseline previous.txt --out candidates.json
```

对种子各追一次参考文献和施引，去重后扣除种子与已有名单，再获取候选标题。标题缺失不会删除候选。输出保留种子、基线、原始关系、扣除原因和候选，可离线复算。只生成待筛名单，不自动判断纳入，不递归追下一轮。

## 题录阶段断点续跑

```bash
python pubmed_mcp.py --export-pmids all-pmids.txt --out results.ris --format ris --checkpoint-dir export-job
```

作业目录必须尚不存在，最终输出必须放在该目录之外，避免与清单、批文件或状态文件冲突。完整 PMID 清单和固定批次保存在 `manifest.json`；每批完成后，在 `state/` 原子保存该批的哈希、缺失号和抓取时间。整份清单只在作业开始、完成或普通异常时保存，运行中不能仅凭清单里的 pending 判断批次尚未完成。失败后保留作业目录，以便恢复。

```bash
python pubmed_mcp.py --resume-export export-job
```

恢复不接受更换格式或输出路径。清单已完成的批次以清单为准；其余批次先核对增量状态与固定计划的绑定，再核对批文件哈希和内容。损坏/未完成批次重新抓取，已验证批次复用；旧作业没有增量状态时仍可按完整清单恢复。状态写入失败会警告；若随后强制中断且完整清单也未保存，该批会重新抓取。最终文件全部完成后才发布。恢复只覆盖已保存完整名单后的题录阶段；搜索取号中断需重新检索。恢复期间数据库题录可能变化，固定名单不等于冻结内容。

固定批文件拒绝指向其它内部文件的符号链接别名；不可信增量状态不复用，也不修改链接目标，改为重抓该批并通过完整快照保留结果。最终合并时再次核对实际读入的每批哈希，内容变化则拒绝发布。

作业目录保留供核对，工具不自动清理。最终输出已存在时拒绝覆盖；进程强制结束遗留的作业锁需要先确认没有运行者再处理。

## 大文件与实验边界

题录按有界并发分批写盘，保持名单顺序，继续使用现有网络限速与重试。仓内 `tests/benchmark_export.py` 比较合成题录的内存、耗时与逐条内容；它不模拟真实 NCBI 网络，不承诺网络提速。

整窗剥离算法仍不发布：排序日期与匹配日期关系未验证，不能用计数相同冒充集合完整。现有日期切片的单日超窗和数据库变化限制仍保留。

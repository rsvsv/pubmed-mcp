# PubMed MCP 与检索技能安装包

这是当前工作区的便携安装快照，包含检索服务、日常检索技能、系统综述技能和运行参考文档。并非新发布的 GitHub Release。检索资料、私人配置、密钥、历史备份和测试日志不包含在包里。

## Windows 使用

先安装 Python 3.10 或更新版本〔来源：项目 pyproject.toml 的 requires-python 声明；不表示所有新版解释器均已验证〕，安装时选择将 Python 加入 PATH。安装依赖需要联网，也需要能访问 Python 包源；实际检索需要能访问 NCBI。

彻底退出 Claude，解压 ZIP，双击 `install-windows.cmd`。安装窗口会显示依赖下载、安装结果和全局路径；看到失败时保留错误，不将文件已复制视为安装完成。

安装器会创建独立 Python 环境，安装 requirements.txt 声明的依赖，然后验证 CLI 可启动，再安装技能和合并客户端配置。只更新 `mcpServers.pubmed`，保留其它 MCP 服务、其它配置和已有 PubMed 环境变量；旧 PubMed 的传输类型、URL、请求头及 cwd 被本地 stdio 启动替换。已有文件先备份；替换失败时逐项尝试恢复已开始修改的文件。某项无法恢复时继续处理其余项，最后明确报告未恢复路径和备份位置，保留原始安装异常；不保证能写回仍被锁定或无权限的文件。不要在客户端运行期间安装，避免配置被客户端同时写回。

成功后重新打开 Claude，并在 Code 中新开会话。可直接说“帮我找一些关于某主题的论文”。支持项目/全局技能的 Code 宿主可以加载这些技能；普通 Desktop 聊天的 MCP 配置不意味着它也支持读取本地 Code 技能。

### 在 Cowork 使用

安装器把技能写到 Code 的用户技能目录。使用独立技能库的 Cowork，需要在该客户端另行安装解压后的 `skills/literature-search/` 和 `skills/systematic-review/`。保留各自的 `references/` 子目录以及两个技能之间的同级关系，日常检索和综述流程会互相引用。

若 Cowork 使用单独的 MCP 配置，从本机安装生成的 `.claude/pubmed-mcp/installation.json` 取 `command` 和 `args`，用于该客户端的 `pubmed` 服务。重启客户端后，分别核对能否看到 PubMed 工具和两个技能。

## 装在哪里

- 程序和独立环境：用户主目录下 `.claude/pubmed-mcp/releases/`。
- 日常技能：`.claude/skills/literature-search/`。
- 综述技能及参考文档：`.claude/skills/systematic-review/`。
- 安装记录：`.claude/pubmed-mcp/installation.json`，以及各技能 `references/installation.json`。其中 command/args 是本机实际启动位置。
- 原文件备份：`.claude/pubmed-mcp/backups/`，其中 restore.json 记录原路径和备份位置。备份可能包含私人配置，不要分享。
- Code 用户级 MCP 配置：用户主目录的 `.claude.json`。
- Desktop 配置：Windows 使用当前用户 AppData 下的 Claude 配置；macOS 使用用户 Library/Application Support 下的 Claude 配置。

这些路径均从运行安装器的电脑获取，不使用开发者用户名或项目盘符。安装成功后可以删除解压目录；不要删除上述全局安装目录，也不要卸载它依赖的基础 Python。研究输出保存在你选择的资料目录，清空资料目录仍会丢失报告和文献。

## 手工安装与其他系统

在解压目录打开终端，Windows 可用下面命令，只安装并配置 Code：

```bash
python -I install.py --configure code
```

macOS/Linux 可用下面命令安装并配置 Code；这些系统的安装路径尚未实机验证：

```bash
python3 -I install.py --configure code
```

`--configure desktop` 只配置 Desktop，`--configure both` 同时配置 Code 与 Desktop。Linux 不提供本安装器的 Desktop 配置入口。默认不传 configure 时仅安装并输出启动信息，不改客户端配置。

其他 MCP 客户端可从 installation.json 复制 command 和 args；不要直接复制开发电脑的绝对路径。若同名项目级 MCP 配置覆盖全局条目，需要在该客户端处理作用域冲突。

CLI 不要求将命令加入 PATH：读取 installation.json，用 command 和 args 作为命令前缀，后面追加 `--help`、`--review-plan` 等参数。配套文档的短命令示例也按此替换；路径应作为独立参数传递。

## 包的范围与核对

安装前按 manifest.json 核对文件哈希；ZIP 旁的 SHA256.txt 是构建时测得的包哈希。哈希只核对文件一致性，不是数字签名，也不证明研究结论。运行模块清单取自源码 RUNTIME_FILES，不从旧 build 或 dist 目录取文件。

配套方法学文档原样保留历史验收、已作废和待确认标记，其中的历史测试记录不代表本安装包的新机器验收。全局技能会指向随包的 references 文档，不需要原仓库。全文下载、PDF 阅读、Word/Excel 制作等外部技能未随包安装。

安装验证不调用 NCBI，不代表新电脑网络已连通。新会话实际列出技能及自然任务选用情况须由目标客户端确认；不能把文件安装成功当作自动触发已验证。

本机维护者可以使用 `--skills-only --runtime <已有全局运行目录> --python <解释器绝对路径>`：核对运行文件与包内哈希一致后，仅补装全局技能与安装记录，不改现有 MCP 配置。该方式不会新建 Python 环境。

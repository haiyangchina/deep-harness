# deep-harness

基于 Python、LangChain、LangGraph、Deep Agents 的独立开发子项目。
设计依据为仓库根目录的 DEEP_HARNESS_IMPLEMENTATION_PLAN.md。

目前处于 **独立开发版**。

## 项目名称与升级说明

项目目录、发行包、CLI 和 Conda 环境统一使用 `deep-harness`；Python 导入名为
`deep_harness`。启动桌面端仍使用 `scripts/start-desktop.ps1`。

更名保留现有状态文件，不改写历史会话、冻结快照或 unknown 执行记录。
桌面应用使用新名称对应的 userData；旧桌面设置和数据不会自动迁入。
若已有 Profile 绑定旧项目的绝对目录，目录更名后请使用新的状态目录或新 Profile，
不要直接修改数据库来绕过工作区绑定检查。原 Conda 环境与压缩备份保留供手动回退。

## 专用桌面端（无需登录）

本项目自己的 TypeScript 桌面端位于 [desktop](desktop/README.md)。它直接操作 deep-harness
的会话、工具审批、技能、记忆与任务状态，默认使用本机 Ollama。环境准备完成后，在本目录执行：

```powershell
.\scripts\start-desktop.ps1
```

首次选择本机 Python/工作区，之后记住配置直接启动。完整 Windows 安装、使用及限制见
[桌面端说明](desktop/README.md)。这是独立开发版；高级管理的 CLI/API 能力仍可使用。

## 已实现

- 独立 Conda 环境、可编辑安装、依赖自动补充脚本与精确版本记录。
- 标准 LangGraph 循环，模型/工具阶段、SQLite checkpoints、不可变消息 ID。
- Profile 独立业务库、checkpoint 与进程 owner 锁。
- 会话创建与输入提交幂等、持久 FIFO、执行 generation 与失效写入拒绝。
- 模型响应 receipt、工具 effect ledger；不确定动作停止，阻挡后继输入。
- 相对工作区的 UTF-8 文件读取/写入；写入必须审批，覆盖已有文件需要 SHA-256。
- 会话显式授权的本机命令执行；每次执行需审批，使用绝对可执行文件路径和参数数组，限制时间与输出。
- 内置能力由 `application/capabilities.py` 单点声明：文件读取与检索、文件写入与编辑、本地命令执行、联网检索与深度研究。新会话默认派生 10 个核心工具（含 4 个 web 工具），无需逐项勾选。
- 任务取消、优雅排空、未知结果人工对账，以及业务库和 checkpoint 的完整 Profile 备份/新 Profile 还原。
- 上下文预算与整轮压缩归档、模型调用和工具调用预算、用量统计、模型增量事件。
- LangGraph interrupt 审批，事件包含可审阅参数，应用重启后可继续审批。
- 已提交决策与图恢复之间中断时，启动阶段继续已授权执行。
- Bearer 认证的本地 FastAPI 开发 API，持久事件游标与输入状态查询。
- 默认本机 Ollama 的 qwen3.5:9b、显式离线诊断模式和 OpenAI-compatible adapter。
- Deep Agents 生产会话：真实 SDK 图通过统一 Profile、审批、effect 与模型 receipt 运行；
  默认内建文件/子 Agent 能力不接触主机。
- Profile 版本化技能与记忆、会话创建时冻结的知识快照；模型写入记忆逐次审批。
- MCP SDK stdio/HTTP 客户端、Profile 明确配置、工具发现/schema 冻结、逐次审批与 effect 记录。
- 可恢复的 chat/repl、会话列表、状态与审批命令；查询不会启动排队任务。
- Profile 工作区持久绑定，恢复前拒绝目录不匹配；SSE 事件支持游标续传。
- 显式本地模型就绪探测，区分服务不可达、模型缺失和工具能力不足。
- SQLite schema 版本、升级前自动业务库备份、不可变会话运行快照和安全分支恢复。

当前会话 API 提供 standard 和 deep 引擎。Deep 支持冻结的 coding/research/research_web harness、
显式授权的同步多 Agent 协作；MCP 支持客户端凭据 OAuth。交互式 OAuth 登录、
旧桌面 RPC 兼容、Cron 和插件迁移仍待开发。
完整状态和剩余工作见 [COMPLETION_STATUS.md](COMPLETION_STATUS.md)。

## 联网检索与深度研究

第 4 项内置核心能力提供 4 个只读工具，实现位于 `infrastructure/web/`。设计目标是让
本地 9B / 32k 模型也能做像样的联网研究：**把多跳循环放在宿主代码里**，而不是让小模型
自己反复「搜 → 读 → 再搜」。

| 工具 | 行为 |
|---|---|
| `web_search` | 按 `[web].routing` 顺序尝试检索后端，首个返回非空结果的后端生效；出错或空结果自动回退。 |
| `web_fetch` | 读取单个公网 URL，把 HTML 清洗为紧凑正文；`offset` 分页续读。拒绝私网、loopback 与含凭据 URL。 |
| `web_extract` | 并发读取最多 8 个 URL，共享字符预算，一次审批、一次回灌。 |
| `web_research` | 一次调用并发多个子查询、按 URL 交错去重、并发补抓正文、裁剪，返回单一带 `[n]` 引用的证据包。 |

所有工具返回的是**证据包**而不是原始网页：标题、URL 与说明由宿主生成，网页正文包在
`<untrusted-web-content>` 中并明确标注为不可信证据，不进入系统提示或长期记忆。输出按
工具的字符预算裁剪，超长正文标注 `[TRUNCATED]`，抓取还可返回续读游标。
搜索与页面正文使用 Profile 级并发限制、有界 TTL 缓存和在途请求合并。原始 HTTP 响应
只在内存中处理，输出的工具证据随会话持久化。凭据值从环境变量读取。

### 后端与配置

`server.yaml` 的 `web` 段与 `--config` 覆盖文件均可配置。默认检索链为
`pi → tavily → exa → brave → serper → searxng → bing_cn → ddgs → jina`。
已配置的后端参与路由，Pi 已接管的同名供应商会从外层路由中跳过。`bing_cn` 和 `ddgs`
免密钥，实际可用性取决于网络。Jina Search 需要密钥，Jina Reader 仍可无密钥使用。
配置文件只引用环境变量名：
`TAVILY_API_KEY`、`EXA_API_KEY`、`BRAVE_SEARCH_API_KEY`、`SERPER_API_KEY`、
`JINA_API_KEY`、`FIRECRAWL_API_KEY`。

Pi 默认先尝试免密钥可用的 Exa 和 Parallel。两者未能提供有效候选时，项目继续尝试
其他已配置后端。Bing 中国（`bing_cn`）按网络环境直连或经系统代理。它用标准库
`HTMLParser` 解析 `li.b_algo`，无额外依赖；**必须发送完整浏览器 User-Agent**，
否则 Bing 只返回空页或挑战页。自建 SearXNG 设置
`searxng_url = "http://127.0.0.1:8888"`（运维自有端点，不受私网 URL 校验约束）。页面读取
默认走 `static`（直接 GET + `trafilatura` 清洗）。公网直抓不使用环境代理，
校验 DNS 地址与每次重定向，并限制响应体大小。`allow_private_urls` 默认 `false`。
`extract_routing` 可显式选择 `jina` 或 `firecrawl`，远端抓取的网络范围由该服务负责。
`total_timeout_seconds` 控制工具总时限，`max_concurrency` 和 `max_cache_entries`
分别限制 Profile 的请求并发与缓存规模。

搜索会验证链接并按标题、摘要与查询的相关性评估结果。高相关候选走快速路径，候选不足时
尝试后续后端并合并去重。研究模式保留原问题及有效子查询，在同一截止时间内分配搜索和
网页读取预算。单个页面失败时仍会返回已取得的正文或摘要，标记未完成部分。

`web_search` 和 `web_research` 支持可选的 `language`、`region` 和 `freshness`
（`day`、`week`、`month`、`year`）。当 `pi` 配置在首位时，工具先调用 Pi，
再按筛选项覆盖程度安排其他后端。诊断会标记后端未支持的筛选项。后端次数、单次超时、搜索预算比例和相关性阈值
均在 `server.yaml` 的 `web` 段配置。

桌面会话会在工具结果上显示检索状态，展开后可查看后端耗时、抓取失败原因和引用来源。
升级前创建且冻结了旧联网工具定义的会话可按界面提示分支，以绑定更新后的工具定义。

### 默认的 Pi 多源搜索

内置后端 `pi` 复用社区项目 `nicobailon/pi-web-access` 0.48.0 的搜索和供应商路由。
在开发环境用 deep-harness 的 Conda Python 构建一次工作进程。构建需要 Node.js 24+
和 npm，会在该 Conda 环境内根据 npm 锁文件安装构建依赖。

```powershell
conda run --no-capture-output -n deep-harness python scripts/build_pi_worker.py
```

默认配置已启用 Pi。已有配置如果覆盖了 `web.routing`，可把 `pi` 放在列表首位，例如下方
的 YAML。桌面端在「运行设置」选文件，CLI 和 HTTP 服务用 `--config` 传入路径。
重新启动后端后，新建会话或分支现有会话以使用新的路由。

```yaml
web:
  routing: [pi, bing_cn]
  pi_providers: [exa, parallel-mcp]
```

在一次子查询内，Pi 按 `pi_providers` 的顺序切换供应商。当前支持
`exa`、`parallel-mcp`、`bocha`、`brave` 和 `tavily`。Pi 已接管的同名后端只调用一次。
未配置 Exa 密钥时，示例中的 Exa 和 Parallel 都使用供应商的公开远程 MCP 搜索入口。
设置 Exa 密钥会优先使用其直连 HTTP API。博查、Brave 和 Tavily 分别读取
`BOCHA_API_KEY`、`BRAVE_SEARCH_API_KEY`、`TAVILY_API_KEY`，按供应商规则计费或计入配额。

Python 通过本地标准输入输出调用随项目构建的工作进程。项目侧无需安装 Pi Agent 或配置 MCP。
`web_research` 负责并发子查询、读取命中的原文和生成引用。证据包可显示
`pi/exa / static`，表示 Exa 发现链接、项目的静态抓取器读取原文。
Pi 会转发时间范围，语言和地区支持由项目诊断标记。
源码运行和单独安装 Python wheel 时需要 Node.js 24+ 在 PATH 中。
发布 wheel 前先执行上述构建命令，Windows 桌面便携包自动构建和携带该工作进程。

### 审批与 Deep 研究模式

4 个工具由 `permissions.classify()` 显式归入 `internet:external`，属于
`ALWAYS_PER_CALL`：`ask`/`auto` 逐次确认，`full` 免批但写 `approval.exempt`，`legacy`
同样要求逐项批准。把批量抓取收进 `web_research` 的一次调用可避免逐次弹窗。

Deep 引擎新增 `research_web` harness：引导模型先做一次 `web_research`，只对分歧点补
`web_fetch`/`web_extract`，并用 `delegate_task` 并行独立子问题（子任务继承只读核心工具，
因此同样能联网），最终输出带 `[n]` 引用与 URL 的 JSON 报告。研究模式的只读契约
适用于内置工具。显式授权的 MCP 工具由连接器实现决定实际副作用，并按会话策略审批。

依赖：`trafilatura>=2,<3`（正文抽取）、`ddgs>=9,<10`（境外免密钥兜底），HTTP 复用既有
`httpx`；`bing_cn` 只用标准库 `html.parser`，无新依赖。前两者都通过
`scripts/dependencies.py add` 按有界 spec 安装。

## 项目分析与 Coding

新建 CLI/HTTP/桌面会话由后端 `derive_grants()` 派生授权，默认授予 10 个核心工具
`read`、`list_files`、`search_files`、`write`、`edit`、`bash` 以及
`web_search`、`web_fetch`、`web_extract`、`web_research`，对应 4 项内置能力
（文件读取与检索、文件写入与编辑、本地命令执行、联网检索与深度研究）。
`GET /profiles/{profile}/capabilities`
返回能力清单、`capability_version`、三类安全作用域与三档策略矩阵；桌面「能力」页
与新建会话弹窗都消费这一份事实，不再逐项勾选工具。能力默认授予不等于默认放行：写入与
命令在执行期仍受批准模式、路径校验和 effect 账本约束。

任何 Shell 查询（包括时间、日期、时区）都统一由 `bash` 执行，输出是 Shell 的 stdout
文本；「本地命令执行」不设子能力，也不为特定命令单列白名单或专用工具。所有命令按同一
批准模式确认（`ask`/`legacy` 逐次，`auto` 同一轮同类只问第一次，`full` 免批），
后端不从命令文本推断用途。research / `--read-only` 会话只有 7 个只读工具，
没有命令执行能力。CLI 默认引导模型先检查 README、依赖清单、AGENTS.md 和入口文件，
再根据文件证据介绍功能或修改代码。
搜索跳过运行状态、常见依赖与构建目录，并限制扫描量和结果大小；结果出现
`truncated=true` 时应缩小目录/模式或继续读取后续行。

核心工具在 Standard、Deep 和获授权的 coding 子任务中使用同一执行网关：

| 工具 | 参数与行为 |
|---|---|
| `read` | `path` 相对工作区解析，新权限模式也支持外部绝对路径。读取文件或列出目录；`offset` 从 1 开始，`limit` 默认 200、最多 2000。文件返回原始 UTF-8 文本、整文件 SHA-256 和续读游标。 |
| `write` | `path`、`content`；自动创建父目录。覆盖已有文件须传 `read` 返回的 `expected_sha256`。同目录临时文件、fsync 和原子替换避免留下半写文件。 |
| `edit` | `path`、`old_text`、`new_text`；只接受唯一精确匹配，零匹配或多匹配（包括重叠匹配）均拒绝。可传 `expected_sha256` 检查版本，保留其余内容、BOM 和换行。 |
| `bash` | `command`、工作区相对 `cwd`、`timeout_seconds`（默认 30，范围 1–120）、`shell`（`auto/bash/powershell`）。支持管道、重定向及 PATH 命令，返回退出码、标准输出和标准错误。每条命令在 ask/legacy 下逐次确认、auto 下同一轮同类只问第一次，`full` 模式免除逐项批准。 |
| `web_search` | `query`、`max_results`、`max_chars`；按后端链检索，返回带 `[n]` 引用、按字符预算封顶的证据包。 |
| `web_fetch` | `url`、`max_chars`、`offset`；读取单个公网页面并清洗正文，`offset` 续读。 |
| `web_extract` | `urls`（最多 8 个）、`max_chars`；并发抓取并共享输出预算。 |
| `web_research` | `question`、可选 `queries`、`max_sources`、`max_chars`；一次调用完成多查询并发检索、去重、补抓与证据打包。 |

`read/write/edit` 处理最多 8 MiB 的 UTF-8 文件。`read` 每页正文最多 24000 字符，
用返回的 `next_offset` 和 `next_column` 分别作为下一次的 `offset` 和 `column`，
可以完整读取超长行，不会跳过被截断的行尾。目录的 offset/limit 按条目分页；目录
变动可能改变分页结果。二进制文件不由这三个文本工具处理。

`bash` 在 Windows 默认使用无窗口、无 Profile 的 PowerShell，在 Linux/macOS 使用
无启动配置的 Bash；显式 Bash 需要本机可用的 Bash（Windows 的 WSL 启动器不适用）。
标准输出和标准错误各保留前 64 KiB，并继续排空后续输出以免堵塞，截断通过独立标记
返回。执行环境只传 PATH、时区和必要的系统/用户目录变量（包括 Windows 的
USERPROFILE、APPDATA 等），不注入模型供应商密钥。Shell 以当前系统
用户运行，**不是文件系统沙箱**；cwd 的限制不能阻止命令访问工作区外文件。
本工具用于有时限的前台命令，结束、超时或取消时清理其所属进程组/Windows Job。
超时后可能已有文件或外部副作用，执行账本记为 `unknown`，重启不会自动重放。
失败提示会显示超时工具及时间限制。修复运行环境后，旧 unknown 会话仍需核对实际
结果，再通过“我已核对，创建新分支”继续；重启不会清除未知结果记录。

未指定权限模式的 CLI/HTTP 会话沿用 `legacy` 原审批规则，但工具授权统一由后端派生，
默认就是上述 10 个核心工具；桌面新建会话默认选择“请求批准”。`--allow-write` 与
`--allow-exec` 现在仅是兼容空操作（会在 stderr 提示一次），因为对应能力已默认授予；
`--allow-memory`、`--allow-delegation`、`--mcp-tool` 仍追加扩展；新增 `--read-only`
强制只授予 7 个只读工具。HTTP 创建会话可省略 `tools`，改用可选的 `extensions` /
`mcp_tools`；显式传 `tools` 仍可用。

旧 `read_file/write_file/run_command` 保留供显式 CLI/HTTP 授予，原参数与 schema 不变；
其中 `read_file` 仍限制 128 KB、默认 120 行和 6000 UTF-8 字节，但不出现在新会话清单。
工具授权在会话创建时冻结，不会扩充已有会话的授权或重写历史 prompt。本次变更前已清空
两处会话库（项目 `.state/` 与桌面端 `agent-state/`），因此不存在需要 `bind-session`
的旧快照；清空前的完整备份位于 `.workbuddy/session-backup-20261114-1646/`。

在本子项目目录运行：

~~~powershell
# 只读分析，启动后输入“分析一下当前项目，并简单介绍功能”
deep-harness --state-dir .state --workspace . repl

# Coding：允许申请修改文件和运行测试；每次写入/执行仍需 /approve ID GENERATION
deep-harness --state-dir .state --workspace . repl --allow-write --allow-exec

# 可选 Deep coding 模式，最终输出结构化报告
deep-harness --state-dir .state --workspace . repl --engine deep --harness coding --allow-write --allow-exec
~~~

`--workspace .` 设置相对路径的基准目录。兼容模式只允许目录内文件，新权限模式允许
按批准策略访问外部路径。
`--workspace ..`，同时为不同工作区使用独立的 `--state-dir`，例如 `.state-repo`。
会话 prompt、工具授权和模型配置保持冻结；本次工具 schema/模型窗口变更后，
请创建新会话（不传 `--session`），旧会话和未知结果不会被自动重试或重写。

Ollama 请求现在明确设置 `num_ctx=32768`，避免依赖服务端较小的默认窗口。
可通过全局参数 `--context-window 16384` 或 TOML 的
`[model] context_window_tokens = 16384` 调整；窗口应符合模型支持范围和本机内存。
运行时同时检查字符预算与按模型窗口换算的 UTF-8 字节预算，计入系统 prompt、工具 schema、
历史和实际存在的摘要，并为回复预留 2048 tokens。字符预算默认 96000 字符，可容纳多次完整
`read` 页（每页最多 24000 字符）；字节预算由 `context_window_tokens` 按每 token 约 4 字节
换算，缩小 `--context-window` 时它会先于字符预算生效。当当前回合的工具输出过大、又没有
已完成整轮可归档时，只把发送给模型的投影做头尾保留式截断（附明确标记），落库的完整
transcript 不变，会话不会因此永久卡住；只有连用户输入本身都超限时才返回可操作的
`context_budget_exhausted`，不会自动重试。服务端仍可能因模型限制拒绝请求。
Deep 模式同样执行预算检查，但尚不支持 Standard 的整轮自动压缩归档。

## 权限与批准模式

桌面新建会话和输入框右下角提供三种模式。三档只是批准策略，**不授予工具**：内置
10 个核心工具由 harness 默认授予；`full` 不会额外授予任何工具。三档差异落在同一条轴上：
审批频率与信任半径。

| 模式 | 审批频率 | 信任半径 | 启用门槛 |
|---|---|---|---|
| 请求批准（ask） | 每次调用逐项询问 | 无 | — |
| 帮我批准（auto） | 同一轮任务内每类只问一次 | 工作区内的写入与命令 | — |
| 完全访问权限（full） | 免批 | 三类作用域全部放开 | 风险勾选 + 确认 |

三类安全作用域（`GET /capabilities` 的 `access_scopes`）：**文件操作**（读取、创建、
修改、上传、删除此计算机上任意位置文件）、**终端命令**（运行命令、安装软件、修改系统
设置）、**访问互联网**（联网检索、读取外部网页、发送数据、使用已启用连接器）。

| 操作 | ask | auto | full |
|---|---|---|---|
| 工作区内只读 | 免批 | 免批 | 免批 |
| 工作区内写入 / 命令 | 每次逐项 | 同一轮同类只问一次 | 免批 |
| 工作区外 / 敏感路径 | 每次逐项 | 每次逐项（不复用） | 免批 |
| 联网工具 / 互联网 / 连接器 / 持久状态 | 每次逐项 | 每次逐项（不复用） | 免批 |

`auto` 的复用**不是**从命令文本推断安全性：后端 `permissions.py::classify()` 只按工具、
目标路径与 cwd 归类到作用域，再由 `capabilities.py::PERMISSION_POLICY` 决定
`exempt / per_round / per_call`。同一 `admission`（一轮任务）里，用户批准过某个可复用
作用域后，后续同类调用直接放行并写入 `approval.reused` 事件；下一轮自动清空。可在审批
面板点「撤销本轮复用」立即恢复逐次确认。`full` 的免批写入 `approval.exempt` 事件。
批准记录仍按 `admission.id:call.id` + 参数 digest 独立保存；`pwd`/`echo`、管道、分号、
变量替换、外部程序和重定向一律同等对待，不做文本白名单。

启用 `full` 前必须勾选「我已了解风险，并对自己的数据安全负责」并由「允许完全访问」确认；
服务端 `CreateSession` / `ChangePermissions` 会校验 `risk_acknowledged`，CLI 需显式
`--acknowledge-full-access`，否则报 `full_access_acknowledgement_required`。该字段不进
创建 digest，幂等重放不受影响。该规则不是操作系统级沙箱或完整恶意代码检测器；Shell
执行后能访问本机账号权限内的文件和网络。运行状态目录仍不向文件工具开放，
`full` 不会自动授予会话中未启用的工具，research 也不能借此取得写入或 Shell 工具。

模式可以在会话空闲时切换，切换持久化并记录事件；等待审批、正在运行、排队、结果
未知或后台子任务尚未结束时拒绝切换。变更需要当前 `permission_revision`，过期请求
会拒绝；重复提交同一已生效设置可安全返回当前结果。切换不会改写历史消息、系统
prompt、工具 schema 或模型快照，也不会自动批准此前挂起的操作。

旧会话及未指定模式的 CLI/API 请求保留 `legacy` 兼容行为：仅工作区文件，所有写入
和命令逐项审批。桌面显示“请求批准 · 仅工作区”，可空闲时主动选择新模式。
数据库 v8→v9 升级前自动备份，保留旧会话快照与待审批记录。

~~~powershell
# 新建会话：同一轮任务内同类操作只问一次
deep-harness --state-dir .state --workspace . repl --permission-mode auto

# 完全访问必须显式确认风险，否则报 full_access_acknowledgement_required
deep-harness --state-dir .state --workspace . repl --permission-mode full --acknowledge-full-access

# 查看当前模式和 permission_revision 后，切换已有空闲会话
deep-harness --state-dir .state --workspace . inspect SESSION_ID
deep-harness --state-dir .state --workspace . permissions SESSION_ID --mode ask --expected-revision 0
~~~

HTTP 创建会话支持 `permission_mode: "ask" | "auto" | "full"`，选 `full` 时必须同时传
`risk_acknowledged: true`。已有会话使用 `POST /profiles/{profile}/sessions/{session}/permissions`，
请求体例如 `{"permission_mode":"auto","expected_revision":0}`；切到 `full` 同样需要
`risk_acknowledged: true`。`POST /profiles/{profile}/sessions/{session}/revoke-reuse`
带 `{"admission_id": "..."}` 可撤销本轮的批准复用。接口仍要求本机 operator token，
会话不能跨 Profile 访问。同步/后台子任务继承父任务创建时的模式和受限工具集合。

## 开发环境

当前已创建：

~~~text
Conda 名称：deep-harness
环境路径：D:\ProgramData\miniconda3\envs\deep-harness
Python：3.12.16
~~~

PowerShell：

~~~powershell
conda create -n deep-harness python=3.12
conda activate deep-harness
pip install -r requirements.lock
Set-Location E:\projects\deep-harness
python -m deep_harness.cli --state-dir .state --workspace . doctor
~~~

重新部署/补齐依赖：

~~~powershell
.\scripts\bootstrap.ps1
~~~

脚本只允许使用 deep-harness Conda 环境。已有 requirements.lock 时先安装锁定版本，
再安装本子项目；不修改 Conda base 或旧 环境。

requirements.lock 为当前 Windows/Python 3.12 环境的精确 Python 包清单，不带包文件 hash，
不是已经在所有 OS 验证的跨平台生产锁。Conda 的 Python/系统依赖由 environment.yml
定义；本次 Windows 原生包的精确下载清单另存于 environment-conda-win64.lock。
安装证据见 environment-evidence.json。

## 开发期间自动添加技术框架

在本子项目目录、激活新环境后执行：

~~~powershell
python scripts/dependencies.py add "alembic>=1.16,<2"
~~~

此命令会：

1. 确认解释器属于 deep-harness Conda 环境。
2. 下载并安装指定的有版本上界/精确版本的框架。
3. 执行 pip check。
4. 成功后更新本项目 pyproject.toml 的依赖。
5. 刷新 requirements.lock 和 environment-evidence.json。

只添加实际开发需要的依赖；Agent 启动与模型生成内容不会触发任意包的自动下载。
失败时不会将未成功安装的依赖写入 manifest。全新机器首次准备环境使用 bootstrap.ps1，
不先直接运行依赖管理脚本。

## 默认使用本机 Ollama

服务端公共配置为 `src/deep_harness/server.yaml`。默认模型、执行预算、工具参数、
分页、连接超时及容量等数值均从该文件读取。例如：

~~~yaml
model:
  provider: ollama
  name: qwen3.5:9b
  base_url: http://127.0.0.1:11434
  request_timeout_seconds: 180
  context_window_tokens: 32768
  reasoning: false
execution:
  max_model_calls: 16
~~~

`workspace.skip_directories` 控制扫描忽略目录，`transport.cli_log_level` 和
`transport.desktop_log_level` 控制日志级别。Web 的搜索路由、服务地址和凭据的环境变量名
也在同一文件，密钥值从环境读取。

部署可设置 `DEEP_HARNESS_SERVER_CONFIG` 指向一份只包含需覆盖字段的 YAML 文件，重启
服务后应用。该方式允许覆盖所有公共配置段。现有 `--config` 或桌面运行设置支持
YAML 与 TOML，按本次运行覆盖 `model`、`execution` 和 `web`。优先级为内置 YAML、
部署 YAML、本次运行配置、显式 CLI 参数。运行预算或工具 schema 变更后，以新会话
使用新配置。工具和协议版本号、HTTP 状态码和游标位宽仍按对应协议实现。

`qwen3.5:9b` 是带 thinking 能力的模型。Ollama 开启 reasoning 时，回答会写入
`message.thinking` 而不是 `message.content`，LangChain 适配器只读取 `content`，
结果就是「执行完成、消耗了 token，但界面没有任何回答」。因此默认显式关闭
reasoning；若设为 `true`，运行时检测到空响应会明确报 `empty_model_response`，
不会静默结束。

已确认本机存在该模型。启动 Ollama 服务后直接使用下面的命令，无需配置云端 API key。

~~~powershell
deep-harness --state-dir .state --workspace . chat "你好"
deep-harness --state-dir .state --workspace . chat "请使用 read_file 工具读取 README.md 的内容并概述"
~~~

模型不可用时报告执行错误，不自动降级到诊断输出。--model 可覆盖模型名称，
--base-url 可覆盖 Ollama 原生 API 地址（不加 /v1）。--config 可加载自定义 YAML 或 TOML 运行配置。

离线运行集成诊断时须明确指定：

~~~powershell
deep-harness --state-dir .state --workspace . --provider diagnostic chat "/read README.md"
~~~

诊断器不是本地大模型；/read、/write 仅为其测试指令。CLI 新会话默认授予 10 个核心工具，
写入与命令仍需逐项审批；`--read-only` 可强制只授予 7 个只读工具。

## 连续对话、会话管理与审批

### Deep、MCP、记忆与技能

新建 Deep 会话时指定 `--engine deep`；已有会话的引擎和工具授权不可改。
Deep 需要真实 LangChain 模型，不能配合 `--provider diagnostic` 使用。写入、命令、
记忆保存和 MCP 调用沿用逐项审批。CLI 示例：

~~~powershell
deep-harness --state-dir .state --workspace . chat "分析项目" --engine deep --allow-memory
deep-harness --state-dir .state --workspace . chat "分析项目" --engine deep --harness research --allow-delegation
deep-harness --state-dir .state --workspace . chat "修复缺陷并运行测试" --engine deep --harness coding --allow-delegation --allow-write --allow-exec
deep-harness --state-dir .state --workspace . delegations <session_id>
deep-harness --state-dir .state --workspace . skill-add E:\skills\review\SKILL.md
deep-harness --state-dir .state --workspace . skill-list
deep-harness --state-dir .state --workspace . memory-add "用户偏好中文回答"
deep-harness --state-dir .state --workspace . memory-search "中文"
deep-harness --state-dir .state --workspace . curate <session_id>
deep-harness --state-dir .state --workspace . curation-list
deep-harness --state-dir .state --workspace . curation-decide <candidate_id> --decision accept
~~~

技能文件采用 `---` frontmatter，包含 `name` 和 `description`，后面是正文。
`skill-disable NAME` 停用当前版本；重复安装同名技能会创建新版本。
`memory-list` 和 `memory-forget ID` 管理当前 Profile 记忆。
新会话冻结当时启用的技能与记忆；已有会话不在中途重载，以保持 prompt 稳定。
`curate` 从该会话已完成的用户输入生成最多五条候选，重复运行同一历史会复用结果；
候选只有人工 `accept` 后才成为记忆，并仅进入之后新建的会话。
`reject` 不写记忆。默认由操作者明确启动；若在 Profile 目录写入
`curation.toml`，内容为 `auto = true`，则每个已完成轮次后自动生成待审核候选。
自动生成失败只记录类别，不改变会话结果；接受候选仍需人工决定。
Deep 子任务也必须在建会话时授予 `--allow-delegation`。research 子任务仅继承
已授予的读取与 MCP 工具；coding 子任务可继承读取、写入与本机命令工具。
写入、命令和 MCP 调用继续逐次审批，子任务不获得记忆写入或再次委派权限。
同步子任务与父回合共享预算；后台子任务是独立会话，有独立预算，可在父回合结束后继续运行。
单个父回合最多启动四个后台子任务。父 Agent 通过 `delegate_task(background=true)` 获取
委派 ID 和子会话 ID；`check_delegation` 可在后续回合查询。运行中的 steer 持久排队，
在当前轮次结束后作为下一轮输入生效；stop 持久阻止后续执行并取消当前执行。
若取消时外部效果不确定，状态为 `unknown`，不自动重试。排队任务在重启后自动恢复。
`--harness coding|research|research_web` 在创建时冻结；三种模式均要求最终结果符合
`summary/evidence/changes/tests/limitations` JSON 结构，research 与 research_web
只授予 7 个只读核心工具（含联网检索），不授予本机写入或命令。
`delegations` 可查看子任务状态和结果；REST 提供
`GET /profiles/{profile}/sessions/{session}/delegations`。
HTTP 还提供 `GET .../delegations/{id}`、`POST .../delegations/{id}/steers`
（`submission_id`、`text`）及 `POST .../delegations/{id}/stop`。单次 CLI 命令
退出即关闭其 Profile owner；需要后台任务跨回合运行时，应保持 HTTP 服务或交互式进程常驻。

MCP 服务器由操作者写入 `.state/profiles/default/mcp.toml`，重启应用后生效。
例如：

~~~toml
[[servers]]
name = "local"
transport = "stdio"
command = "D:/ProgramData/miniconda3/envs/deep-harness/python.exe"
args = ["E:/tools/my_mcp_server.py"]

[[servers]]
name = "remote"
transport = "http"
url = "https://example.org/mcp"
token_env = "REMOTE_MCP_TOKEN"

[[servers]]
name = "oauth_remote"
transport = "http"
url = "https://example.org/mcp"
auth = "client_credentials"
issuer = "https://example.org"
client_id_env = "MCP_CLIENT_ID"
client_secret_env = "MCP_CLIENT_SECRET"
~~~

`env` 可将子进程变量名映射到宿主环境变量名，凭据不会进入会话快照。
使用 `mcp-list` 查看公开工具名，然后在新会话上逐个授予：

~~~powershell
deep-harness --state-dir .state --workspace . mcp-list
deep-harness --state-dir .state --workspace . chat "调用远端工具" --engine deep --mcp-tool mcp__local__echo
~~~

REST `POST /profiles/{profile}/sessions` 也接受 `engine` 和 `tools` 中的 `mcp__...` 名称；
`GET /profiles/{profile}/mcp/tools` 返回当前目录。MCP 服务器配置、工具 schema 或模型
运行配置改变会暂停旧会话，待操作者审阅后恢复或创建新会话。当前 HTTP 传输支持环境变量
Bearer token 与客户端凭据 OAuth；OAuth 授权码/浏览器登录尚未接入。
MCP stdio/HTTP 连接在 Profile 生命周期内复用，关闭 Profile 时释放连接和子进程。
客户端凭据及 OAuth 令牌只驻留在该 Profile 进程内，不写入会话快照。

在 deep-harness 目录、deep-harness 环境内执行：

~~~powershell
deep-harness --state-dir .state --workspace . repl
deep-harness --state-dir .state --workspace . sessions
deep-harness --state-dir .state --workspace . inspect <session_id> --messages
deep-harness --state-dir .state --workspace . chat "继续刚才的任务" --session <session_id>
deep-harness --state-dir .state --workspace . repl --session <session_id>
deep-harness --state-dir .state --workspace . doctor --check-model
~~~

将示例中的尖括号占位符替换为实际 ID。repl 一开始会打印 session_id；
可使用 /status 查看当前任务、写入参数、interaction ID 和 generation，/exit 退出。
进入一个新建且允许写入的会话：

~~~powershell
deep-harness --state-dir .state --workspace . repl --allow-write
~~~

审阅 /status 展示的完整 proposal 后，在 repl 中输入 /approve <interaction_id> <generation>
或 /deny <interaction_id> <generation>。也可退出后通过命令恢复审批：

~~~powershell
deep-harness --state-dir .state --workspace . approve <session_id> <interaction_id> --generation 1 --decision approve
~~~

上例中的 1 必须替换成当前实际 generation。已有会话的工具权限保持不变，
不接受同时使用 --session 与 --allow-write；需要不同权限时新建会话。
chat --submission-id 可指定重试用的稳定 ID，重试必须同时指定同一个 --session 和相同文本。
非交互命令输出 JSON：chat 的退出码为完成 0、待审批 3、其余未完成 1；领域拒绝为 2。
审批命令的退出码 0 表示操作已处理，最终状态需检查输出的 latest_admission。

同一 Profile 首次启动时持久绑定解析后的工作区，后续以不同 --workspace 启动会明确拒绝，
不会在新目录中执行旧审批。切换目录请新建 --profile。升级前已有库会在首次升级启动时
绑定当时传入的目录，因此第一次启动须确认原目录。新增绑定表不改写既有会话或 checkpoint。

doctor 默认只输出本地环境证据；--check-model 显式访问配置的 Ollama 元数据接口，
检查 completion/tools 能力，不下载模型、不生成文本，不支持的供应商返回 unsupported。
元数据就绪只表示模型存在并声明支持这些能力，不能代替真实推理验收。

## 切换到其他 OpenAI-compatible 模型

先在当前 Shell 设置自己的秘密环境变量，再显式传模型名称。

~~~powershell
$env:OPENAI_API_KEY = "填写自己的密钥"
deep-harness --state-dir .state --workspace . --profile default --provider openai --model "填写模型ID" chat "你的任务"
~~~

OpenAI-compatible 端点可加 --base-url（此时地址通常包含 /v1）。该启动凭据只绑定 --profile 指定的 Profile，
不能通过另一 Profile 的 API 路径复用。当前默认关闭模型 adapter 的 LangSmith tracing。
本地 Ollama 已单独连接验证；其他供应商及推理字段/多模态的全部保真能力尚未完成验收。

## 配置固定、旧会话升级与不确定任务恢复

新会话会持久保存运行快照，包括工作区、模型供应商/名称、端点指纹、工具定义和运行契约版本。
API key 不进入快照。更换模型、端点或工具 schema 后，旧会话的提交、审批和自动恢复都会暂停，
inspect 输出 runtime.compatible=false。恢复原配置可继续旧会话；使用新配置时可创建新分支。
当前快照固定的是路由配置，不保证 Ollama 同名模型标签背后的权重从未被替换。

不确定执行的处理方式：

~~~powershell
deep-harness --state-dir .state --workspace . inspect <session_id> --messages
deep-harness --state-dir .state --workspace . fork <session_id> --request-id recovery-001
deep-harness --state-dir .state --workspace . repl --session <new_session_id>
~~~

先核对 inspect 中的 active_effects、任务状态和实际文件，再向新分支提交后续指令。
fork 只复制从会话开始连续完成的完整轮次；遇到第一个未完成轮次即停止。
它不会复制执行中断点、审批、effect/model receipt、未知任务或后续排队输入。
新分支保存 lineage，绑定当前运行配置；同一个 --request-id 可幂等重试。
原会话和 unknown 状态保留。分支不会撤销外部写入，也不会自动重发失败请求或后继输入。
需要重新执行的任务由操作者核对结果后明确提交。

升级前创建的旧会话没有可靠模型快照，系统不会推测原配置并隐式续跑。
确认当前配置正确后，从 inspect 输出复制 runtime.current_fingerprint，显式绑定：

~~~powershell
deep-harness --state-dir .state --workspace . bind-session <session_id> --fingerprint <current_fingerprint>
~~~

绑定仅适用于没有快照的旧会话，不能覆盖已有快照；指纹不匹配时拒绝。
绑定后会恢复该会话已接纳的队列和已提交的审批决策；unknown 仍保持阻挡。
不希望恢复旧队列时，直接 fork，原会话无需绑定。

业务库使用 PRAGMA user_version，目前为 v8。支持原无版本业务库的升级，升级前自动在
Profile 的 backups/ 目录保存经过 quick_check 的 SQLite 备份；备份失败则中止升级。
DDL 与版本写入同一事务，过新版本、无法识别或不完整的 schema 会明确拒绝。
升级备份只包含 business.db。完整 Profile 备份另用 backup 命令，包含 checkpoint；
restore 只发布到尚不存在的新 Profile，并要求当前 checkpoint SDK 版本匹配。
不要在运行时直接将备份覆盖到业务库。

## 启动开发 API

~~~powershell
deep-harness --state-dir .state --workspace . serve --port 8765
~~~

此命令默认使用本地 qwen3.5:9b。若要复现下面的 /write 诊断示例，启动时加
--provider diagnostic；真实模型通过自然语言请求调用工具。

服务仅监听 127.0.0.1。启动时生成 .state/operator.token，只打印文件位置，不打印 token。
该 token 是本机操作者凭据，具有此开发实例的 Profile 管理权限。此原型不提供多用户授权。
文件工具拒绝读取/写入整个运行状态目录，以免将 token、数据库、checkpoint 暴露给模型。
多个 Profile 的状态独立；它们仍使用管理员显式设置的同一个工作区，不是敌对租户沙箱。

PowerShell 调用示例：

~~~powershell
$headers = @{ Authorization = "Bearer $((Get-Content .state/operator.token -Raw).Trim())" }
$base = "http://127.0.0.1:8765/profiles/default"
$session = Invoke-RestMethod -Method Post -Uri "$base/sessions" -Headers $headers -ContentType "application/json" -Body '{"request_id":"create-demo","tools":["read_file","write_file"]}'
$route = "$base/sessions/$($session.id)"
$receipt = Invoke-RestMethod -Method Post -Uri "$route/inputs" -Headers $headers -ContentType "application/json" -Body '{"submission_id":"write-demo","text":"/write demo.txt hello"}'
Invoke-RestMethod -Uri "$route/admissions/$($receipt.id)" -Headers $headers
Invoke-RestMethod -Uri "$route/events" -Headers $headers
~~~

当 admission 状态为 waiting_approval，events 中 interaction.open 含 proposal、
generation 和 id。审阅后提交：

~~~powershell
$approval = @{
    interaction_id = "替换为 interaction.open 的 id"
    generation = 1
    decision = "approve"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$route/approvals" -Headers $headers -ContentType "application/json" -Body $approval
~~~

decision 可为 approve 或 deny；generation 必须使用当前事件/输入状态中的实际值，
不能始终写 1。批准 ACK 表示决策已接纳，完成情况继续查询 admission/events。

目前 API 是独立 REST 开发接口，不宣称兼容现有 Desktop 的 canonical JSON-RPC。

会话管理和实时事件接口：

| 接口 | 行为 |
|---|---|
| GET /profiles/{profile}/sessions?limit=50&after=ID | 按会话 ID 字典序分页，返回 next_cursor；非最近更新时间排序 |
| GET /profiles/{profile}/sessions/{session}/status | 活动任务、当前待审批参数、latest_admission |
| GET /profiles/{profile}/sessions/{session}/messages?limit=50&after=CURSOR | 已完成轮次的稳定历史分页 |
| GET /profiles/{profile}/sessions/{session}/messages/search?q=TEXT | SQLite FTS5 搜索已完成历史 |
| GET /profiles/{profile}/sessions/{session}/usage | 模型调用与 token 用量，标示估算 |
| GET /profiles/{profile}/sessions/{session}/events/stream?after=SEQ | text/event-stream，读取持久事件并等待新提交 |
| POST /profiles/{profile}/sessions/{session}/forks | 请求体 request_id；返回新分支会话，重试使用同一个 request_id |
| POST /profiles/{profile}/sessions/{session}/runtime-binding | 请求体 fingerprint；显式接纳旧会话当前配置 |

这些接口均要求 Bearer。SSE 支持 Last-Event-ID，取其与 after 的较大值；客户端按 seq 去重，
事件提交成功后才投递。连接建立时和空闲唤醒后发送 keep-alive 注释，最长等待 15 秒。
该流包含临时 `model.delta` 片段和任务事件，不保证逐 token 或完整供应商流协议。
会话查询页大小为 1–100，事件查询每批最多 500。
SSE 不持有模型执行任务，客户端断线不会取消已接纳任务。

## 测试与质量检查

Python 测试始终使用仓库 runner。在仓库根目录的 PowerShell：

~~~powershell
conda activate deep-harness
$env:Harness_PYTHON = (Get-Command python.exe).Source -replace '\\', '/'
& "C:\Program Files\Git\bin\bash.exe" scripts/run_tests.sh -q
python -m ruff check src tests scripts --config pyproject.toml
~~~

Runner 固定使用仓库的 pytest 配置，额外参数直接传给 pytest。测试使用临时状态目录及
确定性模型，默认无需 Ollama 或公网服务。

## 当前限制

运行控制命令（在已有会话上使用实际 ID）：

~~~powershell
deep-harness --state-dir .state --workspace . usage <session_id>
deep-harness --state-dir .state --workspace . history <session_id> --limit 50
deep-harness --state-dir .state --workspace . search <session_id> "关键词"
deep-harness --state-dir .state --workspace . cancel <session_id> <admission_id>
deep-harness --state-dir .state --workspace . backup E:\backups\profile.zip
deep-harness --state-dir .state --workspace . --profile recovered restore E:\backups\profile.zip
deep-harness --state-dir .state --workspace . resolve <session_id> <admission_id> --evidence-file E:\review\resolution.json
deep-harness --state-dir .state --workspace . repl --allow-exec
~~~

备份目标不能位于该 Profile 的状态目录中；还原目标必须是新 Profile。对账 JSON
需要准确的 generation、文字证据以及每个 dispatched effect 的 effect_id、proposal_hash
和 applied/not_applied 裁决。对账后原会话仍保持阻挡，可显式 fork 后继续。
本机命令只接受绝对可执行文件路径和 argv，不经过 shell；批准后如结果不确定需人工核对。
API 的 `/profiles/{profile}/sessions/{session}/usage` 返回模型调用数与用量，
`contains_estimates` 指示有无估算值。

- 已支持当前业务库的版本升级和完整 Profile 备份/新 Profile 还原；尚无 checkpoint SDK 跨版本迁移和旧历史导入。
- started 执行在重启后保守标记 unknown；不会自动重做任意外部动作。
- 文件路径检查是应用策略，不替代 OS 沙箱；本机命令需要每次审批，暂不支持远程终端或容器。
- 单 Profile 一个本机 owner；不能用多 Uvicorn worker 共享同一数据目录。
- CLI chat 默认新建会话；续聊使用 --session 或 repl，审批可通过 CLI 或 API 完成。
- 工作区与模型路由已校验；模型权重 digest、所有供应商隐含默认参数和跨供应商消息迁移尚未完整覆盖。
- 大文件分段读取和模型原生输出上限仍待后续实现；历史分页/搜索只包含已完成轮次，用量在供应商未报告时标为估算值。
- 仅本机 Windows 环境已做实际验证；其他 OS 尚未验证。

开发进度、测试证据和下一阶段见 DEVELOPMENT.md。

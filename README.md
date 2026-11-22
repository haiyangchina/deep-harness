# deep-harness v1.1.4

deep-harness 是可在本机或服务器运行的 Python Agent 宿主，提供 CLI、Windows 桌面端、免登录浏览器工作台和 REST/SSE API。CLI 默认使用本机 Ollama 的 `qwen3.5:9b`。各入口复用统一的会话授权、审批、回执和恢复机制。

- [系统架构与技术实现](ARCHITECTURE.md)
- [Web 接口与业务扩展](IMPLEMENTATION_SPEC.md)
- [Windows 桌面端](desktop/README.md)
- [开发状态](DEVELOPMENT.md)
- [免登录服务器部署](deploy/README.md)

## 运行

使用专用 `deep-harness` Conda Python 3.12 环境。Windows 下先准备环境，再按需启动桌面端。

```powershell
.\scripts\bootstrap.ps1
.\scripts\start-desktop.ps1
```

桌面端启动时选择 Python、工作区、状态目录和 Profile。CLI 使用同一个 Python 环境。为不同工作区使用独立的状态目录。程序初始化空目录，使用现行数据库结构和会话契约。

```powershell
conda activate deep-harness
deep-harness --state-dir .state --workspace . repl
deep-harness --state-dir .state --workspace . chat "分析这个项目" --harness research
deep-harness --state-dir .state --workspace . repl --harness coding --allow-patch
deep-harness --state-dir .state --workspace . --provider diagnostic chat "/read README.md"
deep-harness --help
```

`general` 使用 Standard LangGraph 循环，`coding`、`research` 和 `research_web` 使用 Deep Agents。研究模式使用只读内置工具，`--read-only` 可约束通用会话。专业模式需要 LangChain 模型适配器。确定性诊断后端需显式选择，用于离线测试。

## 能力与权限

| 类别 | 工具 |
| --- | --- |
| 文件读取与检索 | `read`、`list_files`、`search_files` |
| 文件写入与编辑 | `write`、`edit` |
| 本机命令 | `bash` |
| 联网研究 | `web_search`、`web_fetch`、`web_extract`、`web_research` |
| 显式扩展 | `apply_patch`、`save_memory`、Deep 委派、已配置 MCP 工具 |

`ask` 对有风险的操作逐项确认。默认采用该模式。`auto` 允许同一次输入内复用工作区写入或终端批准，敏感路径、工作区外、联网和持久状态写入仍逐项审批。`full` 需要显式风险确认，只对会话已有授权生效。文件工具拒绝直接读取宿主状态目录，Shell 以当前系统用户权限运行。MCP 服务的实际行为由对应服务决定。

`read` 返回内容、SHA-256 和行列游标。`write` 覆盖已有文件时须提供基线哈希。`edit` 支持唯一文本匹配并保留其余内容。多文件补丁应用前检查全部基线，逐文件记录写入。`bash` 默认按平台选择 PowerShell 或 Bash，限制时间和输出并清理受管进程树。

联网研究并发检索、去重、补抓正文并返回带来源的证据包。Python 管理权限、时间预算和外层回退，Pi 搜索工作进程管理其供应商路由。默认直接抓取只允许公网地址。抓取证据按不可信内容处理。

联网配置位于 `server.yaml` 的 `web` 段。默认 Pi 优先使用 Exa 和 Parallel，再由 Python 尝试可用的外层后端。独立 CLI 可用 `conda run --no-capture-output -n deep-harness python scripts/build_pi_worker.py` 构建搜索工作进程。外部 MCP 服务由 Profile 的 `mcp.toml` 定义，`mcp-list` 可查看工具标识，`--mcp-tool` 显式授予单个工具。

## 会话和任务

Profile 持久绑定一个工作区，同时持有一个执行 owner。每条输入有独立 admission 和幂等提交 ID。审批决定与派发回执持久化，已派发且结果未知的工具需要人工对账。业务和图状态分别存储于 `business.db` 和 `checkpoints.db`。完整 Profile 可归档，再还原至未使用的 Profile。

CLI 的 `sessions`、`inspect` 和 `usage` 用于查看状态，`approve` 用于提交决定，`fork` 从连续已完成的对话轮次建立新会话。长任务阶段有独立会话与预算，可配置最终验收命令。桌面提供进度、审批和回执界面。

## 配置和 API

`src/deep_harness/server.yaml` 提供公共默认值。`DEEP_HARNESS_SERVER_CONFIG` 可加载部署级 YAML，`--config` 可加载启动级 YAML 或 TOML。供应商凭据从环境读取。

`deep-harness --state-dir .state --workspace . serve` 启动回环 REST 服务，Bearer token 保存在状态目录的 `operator.token`，接口见 [API 实现](src/deep_harness/transports/api.py)。会话创建接收 Harness。能力清单位于 `GET /profiles/{profile}/capabilities`，事件支持游标分页及 SSE。

## 免登录 Web 与业务扩展

在 `desktop/` 运行 `npm run build` 后，从仓库根目录启动：

```powershell
deep-harness --state-dir .state/web web --instance-config examples/instance.yaml
```

浏览器访问 `http://127.0.0.1:8765`，选择工作目录、模型和 Agent。该模式使用单进程 SQLite。从本机启动的 CLI 或 Electron 使用自己的状态目录。远程客户端通过可信网络连接 Web 实例，实例可达者共享会话、审批和产物。

[实例示例](examples/instance.yaml)包含本机 Ollama、DeepSeek、千问兼容端点及采购、工单两个业务流程。业务 Agent 可选已有工具、HTTP 连接器和配置的 MCP。工作流执行草稿、JSON Schema 验收与审批提交，能查询未知写入的外部状态。服务器 Shell 可选择 Docker 隔离模式，详见[部署方式](deploy/README.md)。

## 测试

使用专用 Conda 解释器运行 `scripts/run_tests.sh`，设置 `Harness_PYTHON` 为该解释器的绝对路径。桌面在 `desktop/` 下运行 `npm test` 和 `npm run typecheck`。测试使用临时状态、离线模型和可控工具。不需要运行 Ollama 或访问真实搜索供应商。

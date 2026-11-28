# v1.1.8 Web 接口与业务扩展

Web 服务运行一个 Uvicorn worker。实例锁保护状态卷，每个登记的工作目录对应一个内部 Profile。Electron 和 Web 使用同一份工作台构建与工作区接口。本机部署使用回环 Bearer，服务器部署由可信入口控制访问。启动命令、反向代理和备份操作见[部署指南](deploy/README.md)。

## 实例配置

通过 `deep-harness --state-dir .state/web web --instance-config examples/instance.yaml` 启动。浏览器与 API 同源，服务检查 Host 和跨站变更请求。工作目录和密钥文件可相对实例 YAML 解析。配置更改经重启生效。

| 配置 | 用途 |
| --- | --- |
| `web` | 监听地址、端口、可信主机及对外 Origin。默认访问地址为 `http://127.0.0.1:8765`。 |
| `limits` | 实例内运行和入队上限、附件大小上限。 |
| `workspaces` | 命名工作目录、`read_only` 或 `sandbox` 模式、允许的 MCP 工具。可用 `manage_knowledge: true` 向此工作区的实例访问者开放知识管理。 |
| `models` | 模型别名、适配器、端点和能力，以及上下文窗口、超时、每模型并发限制。 |
| `agents` | 指令、工具清单、Harness、可选模型和工作流。 |
| `connectors` | 业务 HTTP 地址、请求及可选响应 JSON Schema、超时、读写属性和可选对账地址。 |
| `workflows` | 顺序执行的 `agent`、`verify` 和 `tool` 步骤。 |
| `sandbox` | 可选 Docker 镜像和 CPU、内存、PID、并行工作副本限制。 |

模型目录使用同一 LangChain 工厂接入 Ollama、DeepSeek 和 OpenAI-compatible 端点。千问可使用兼容端点。模型与连接器可用 `secret_env` 或 `secret_file` 引用凭据。模型目录公开别名、能力和配置状态，模型协议参数和密钥留在服务端。会话快照保存所选模型 ID、配置修订、工具和运行约束。修改实例默认模型不会改变已有会话绑定。

## HTTP 与事件

实例目录在 `/api/v1` 下公开 `GET /instance`、`GET /models`、`GET /workspaces` 和 `GET /agents`。下表的路径以 `/api/v1/workspaces/{workspace_id}` 为前缀。完整字段见服务生成的 `/openapi.json`。

| 方法和路径 | 请求或用途 |
| --- | --- |
| `POST /sessions` | 提交稳定的 `request_id`，可选 `agent_id`、`model_id`。也可指定提示、Harness 和授权工具。 |
| `GET /sessions`、`GET /sessions/{id}`、`GET /sessions/{id}/messages` | 分页读取会话、模型绑定、待审批和对话历史。用户消息可含有序的 `image_ids`。 |
| `PATCH /sessions/{id}` | 携带 `expected_revision` 更新标题 `title` 和置顶 `pinned`。会话列表包含 `metadata_revision` 与 `pinned_at`，排序变化时从首屏读取新游标。 |
| `DELETE /sessions/{id}`、`GET /sessions/{id}/deletion` | 请求清理会话并查询结果。返回 `deleting`、`deleted` 或 `cleanup_required`。可通过 `GET /session-deletions` 查询未完成清理。 |
| `POST /sessions/{id}/runs` | 提交 `submission_id`、`text` 和可选 `artifact_ids`，返回 HTTP 202 及 Run ID。 |
| `GET /runs/{id}` | 读取状态、代际、结果和用量。 |
| `GET /runs/{id}/events`、`GET /sessions/{id}/events` | 从 `Last-Event-ID` 或 `after` 续读持久 SSE 事件。 |
| `GET /sessions/{id}/event-history` | 从事件日志按游标读取 JSON，可用 `tail=true` 读取最近一页，或用 `after` 和 `wait` 等待下一批事件，最长 12 秒。 |
| `GET /capabilities` | 返回 Harness 清单和部署允许的操作。 |
| `GET /sessions/{id}/messages/search`、`POST /sessions/{id}/forks` | 搜索已完成历史和创建会话分支。 |
| `POST /runs/{id}/approvals/{approval_id}/decision` | 提交 `approve` 或 `deny`，以及原提议的 `generation`、`proposal_hash`。 |
| `POST /runs/{id}/cancel` | 请求取消运行。 |
| `GET /runs/{id}/effects/{effect_id}/status`、`POST /runs/{id}/reconciliation` | 查询连接器提供的外部状态，人工提交结论及证据。 |
| `POST /sessions/{id}/attachments` | 以 `application/octet-stream` 上传附件，可通过 `X-Filename` 指定显示名称。 |
| `POST /sessions/{id}/images` | 上传 PNG 或 JPEG，使用 `application/octet-stream` 和 `X-Harness-Upload: 1`。提交 Run 时加入 `image_ids`，纯图片可省略 `text`。 |
| `GET /sessions/{id}/images/{image_id}` | 在会话范围内读取原图，使用 `?thumbnail=1` 获取缩略图。 |
| `GET /sessions/{id}/submission?submission_id=...` | 在写入结果未知时核对已受理的 Run。 |
| `GET /runs/{id}/artifacts`、`GET /artifacts/{id}` | 列出产物并按 ID 下载。 |
| `POST /workflows/{id}/runs` | 提交 `request_id`、`agent_id`、`goal` 及可选 `model_id`。 |
| `GET /jobs`、`GET /jobs/{id}`、`POST /jobs/{id}/commands` | 查询工作流任务和阶段状态，暂停、恢复、取消或验收阶段。 |
| `POST /jobs` | 创建自定义长任务。服务器允许只读阶段。需要写入的服务器业务步骤通过已配置工作流启动。 |

Run ID 即业务库的 admission ID。每个会话按提交顺序运行。提交响应丢失时，客户端使用原提交 ID 和原输入重试。重复 ID 对应的输入变化会产生冲突。审批、执行回执和工作流阶段由业务库持久化，浏览器断开后可以刷新或续读进度。

彻底删除清理当前实例中的会话、运行、索引、检查点和独占资源。活动运行、未决写入、长任务阶段和被其他会话依赖的对象会阻止删除。已应用到项目的变更、外部系统记录、既有导出和备份由其所在系统管理。

## 扩展业务

1. 在实例配置登记 Agent 的指令和工具授权。HTTP 工具可直接配置。MCP 服务通过 Profile 的 `mcp.toml` 配置，再加入工作目录的 `mcp_tools`。可信 Python 扩展可在嵌入应用时通过 `InstanceHost(..., python_tools=...)` 注入。
2. 为业务写入设置 HTTP 连接器并声明 `writes: true`。工具网关登记派发并用 effect ID 关联请求。写入须审批。结果未知时保留原派发，支持通过配置的查询地址核实，并由操作员对账。
3. 通过 `agent` 阶段生成结构化草稿，`verify` 阶段用 JSON Schema 验收，再由 `tool` 阶段将已验收输入交给登记的 HTTP 连接器。采购和工单配置及本地模拟服务提供两个可运行样例。

附件、结果和 Docker 差异存放在实例状态卷中，通过产物 ID 定位。文件写入发布时保存大小和 SHA-256，附件引用须属于提交会话。沙箱模式中，每个 Run 的容器命令共享隔离工作副本，执行完成、取消或超时后回收。工作区文件工具读取宿主源目录。进程重启时清理遗留容器，丢失工作副本的 Run 需要重新创建。

[实例配置](examples/instance.yaml) · [模拟业务服务](examples/mock_business.py) · [系统架构](ARCHITECTURE.md)

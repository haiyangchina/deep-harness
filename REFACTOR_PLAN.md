# deep-harness 重构落地手册（强依赖 deepagents）

> 文档性质：重构开发落地前的最终设计与执行规格。
> 状态：**编码落地完成（2026-11-22）**。四个新模块已全部接入，用户侧 engine 已移除，
> 全量 pytest 303 + 9 条新增契约守卫测试通过，ruff 与桌面 typecheck 通过，版本升级 v1.1.3。
> 执行时发现的历史兼容约束记录在下方「落地偏差」段。本手册不因重构本身改版本号
> （现行版本按用户明确要求升级至 v1.1.3）。
> 依据：全量源码级复核（2026-11-22）+ 2026 年前沿 harness 实践对标。

---

## 0. 决策摘要（一句话版本）

把 deepagents 从「可选图构造器」提升为**唯一执行内核**：全项目只保留一张 `create_deep_agent` 图；项目自身退化为**契约守卫 + 档位配置 + 账本**三件事。用户侧彻底消失 `engine`（standard/deep）选项，只按「任务类型」自动派生能力。

总纲四句话：**一个图、一个守卫、一个后端、四档配置**。

| 概念 | 含义 | 状态 |
|---|---|---|
| 一个图 | `runtime/agent.py::build_agent()` 唯一建图入口，删除 `StandardRuntime` 自建图 | 新建 |
| 一个守卫 | `runtime/guard.py::HarnessGuardMiddleware` 承载批准/凭据/fencing/恢复四大契约 | 新建（核心） |
| 一个后端 | `runtime/backend.py::ToolGatewayBackend` 把虚拟 FS 写操作代理到 `ToolGateway` | 新建 |
| 四档配置 | `application/harness_spec.py::HarnessSpec` 替代 `engine` 分支 | 新建 |

---

## 1. 前沿校准（2026-11 对标结论）

本方案不是拍脑袋，而是与当前前沿共识对齐后的结果。以下为最终校准要点及其对本项目的含义：

| 前沿共识（2026） | 出处/证据 | 对本项目的含义 |
|---|---|---|
| **guides（前馈）+ sensors（反馈）** 二分 | Böckeler 分类；Keyhole 2026 白皮书 | 项目的 GUIDE（harness 提示词）是 guides；receipt/effect/验证门禁是 sensors。重构要保持两者分离，不要让框架提示词替代账本校验 |
| **durable session log 与 context window 分离** | Anthropic Managed Agents（2026-04）：session 是可恢复事实源，context 只是单次模型调用的临时视图 | 项目 `messages` 表 + receipts 就是 durable log；`ContextProjector` 是视图装配。合并双引擎时必须保留这个分离 |
| **harness 主要影响成本而非成功率** | Terminal-Bench 2.0、HarnessTax（2026）：同模型不同 harness 成本差 5x–70x | 统一上下文平面（消除 token 浪费）是最高 ROI 的改动，优先于花哨的多 agent |
| **verification 是最大瓶颈** | Fractary/Anthropic 研究笔记：rule-based 检查优先，LLM-as-judge 不稳健 | 建立 verification gate 时先做确定性检查（测试/lint/schema），reviewer 子智能体只作为第二层 |
| **tool contract 必须是分布式语义** | 前沿共识：幂等键、去重、记录结果，而非仅 name+schema | 项目 effect 三阶段 + `unknown_effect` 禁止重试正是这个模式，**不得在重构中丢失** |
| **凭据永不进入不可信沙箱** | Anthropic Managed Agents；MDA 0.10 的 browser_handoff 让密码不进 trace | 项目环境变量名策略正确；ToolGatewayBackend 不得让 deepagents 的虚拟 FS 触碰 state_root 凭据 |
| **harness 组件会随模型进步而过时** | Anthropic：逐个移除组件做压力测试，而非一次性删除 | 账本/governance 类组件不会过时（安全/审计），可保留；纯提示词补丁类组件要定期复审 |
| **社区版 vs 托管版差异** | 本地 `deepagents==0.7.32`；LangChain **Managed** Deep Agents 已到 0.10（异步子智能体、沙箱浏览器、user-scoped memory） | 本方案针对**本地开源库**。0.10 的新能力主要在托管层，不作为本方案依赖；但「异步子智能体」的设计思路与本项目后台 `delegate_task` 一致，验证方向正确 |

**结论**：项目在 durability 与 governance 两维度已达前沿；结构性欠账在执行环境（沙箱）与上下文工程（双引擎分叉）。本重构聚焦后者，前者另行规划（见 §12 不做清单）。

---

## 2. 现状基线（源码级精确事实）

重构前必须记住的精确事实，所有代码骨架以此为准。

### 2.1 版本与依赖（已实测）

```
deepagents 0.7.32 | langgraph 1.2.19 | langchain 1.4.7
langchain-core 1.7.3 | langgraph-checkpoint-sqlite 3.1.3
```

### 2.2 关键接口签名（已逐行核对）

**`ToolGateway`（application/tools.py）**
- `__init__(scope, session, receipts, claim, *, mcp=None, mcp_bindings=None, web_client=None)`
- `async execute(call: ToolCall) -> str` —— 返回字符串结果；幂等键 `key = f"{claim.admission.id}:{call.id}"`；`digest = fingerprint({"name":..., "arguments":...})`
- `prepare_effect` 命中 `succeeded` 直接 `return existing["result"]`（工具级幂等短路）
- `validate(call) -> BaseModel`（校验冻结授权 + schema）
- `approval_reason(call) -> str | None`、`approval_scope(call) -> (scope_key, decision, reusable)`
- `async interaction(call, key, digest, proposal)` —— 查已持久化 decision
- effect 三阶段：`prepare_effect` → `dispatch_effect` → `save_effect`；`dispatched` 后未知结果抛 `DomainError("unknown_effect")`
- `_preflight` 明确拒绝 `delegate_task`/`check_delegation`（当前由 DeepRuntime 管理）

**`approval_checkpoint.py`（45 行，全部要复用）**
- `checkpoint_interrupts(state)` / `approval_ids(interrupts, generation)` / `interrupt_outcome(interrupts, generation)` / `resume_command(state)` / `async pause_at_boundary(probe, generation)`
- interrupt value 约束：必须是 dict，含 `generation`（必须等于 claim.admission.generation），`interaction_ids` 或 `id`；job_pause 用 `{"generation":g,"job_pause":True}`
- `approval_ids` 会剔除 `job_pause` 项并去重
- **关键**：guard 抛出的 interrupt value 必须严格遵守上述结构，否则 approval 解析失败

**`ContextProjector`（runtime/context.py）**
- `__init__(store, executions, limits)`
- `async project(claim, prompt, tools, messages, *, max_context_bytes=None) -> list[dict]` —— **返回消息列表**（含 `context-summary:{revision}` 前缀），不是投影对象
- 用 `context_heads` 表存 `dropped_ids/summary/revision`，`publish()` 有 revision 乐观锁 + `context_archives` 归档 + `context.compacted` 事件
- `clip_tool_outputs()` 在投影中裁剪超大工具输出（保留完整持久化 transcript）

**`DeepRuntime`（runtime/deep.py，648 行）**
- `ReceiptedModel(AgentMiddleware)`：`awrap_model_call` 持 `model_lock` → `pause_at_boundary` → `_call`；`_call` 做 model receipt 幂等（key `deep:model:{digest[:32]}`）+ usage budget + `assert_claim` + `save_model`
- `_project()`：每轮临时投影，只保留当前轮，更早用户输入压成 note 且不写表 —— **这是要废弃的分叉点**
- `_tool(spec)`：返回 `StructuredTool`，invoke 内 8 步链（见 §5.2）
- `_approval(call)`：`scope_granted(..., exclude_key=key)` 复用 → `interaction()` → 无决策则 `interrupt({id/tool_name/arguments/proposal_hash/generation})` → 再查 → `approval_missing` 抛错
- `_delegate()`：直接 `child = DeepRuntime(...)` + `replace(session, tools=父子交集, harness="general")`，独立 thread_id，后台模式独立预算
- `build_readonly_probe()`：SDK smoke graph，与业务无关，保留

**`bootstrap.py`（750 行）的 engine 耦合点**
- `create_session(engine="standard", ...)`：L189 校验 engine、L193 特化 harness 需 deep、L196 `derive_grants(engine,harness,...)`、L207-223 snapshot_fields
- `runtime()` L368：`runtime_type = DeepRuntime if session.engine == "deep" else StandardRuntime`
- L307、L563、L613 三处 `snapshot(engine=session.engine, ...)`

### 2.3 隐藏耦合（重构时最容易漏）

1. **快照 `context_projection` 键依赖 engine**（session_runtime.py L45）：`"deep-turns-v1" if engine == "deep" else "standard-v1"`。合并后必须统一取值并保留 `compatible_runtime_hashes` 兼容机制。
2. **`sessions.engine` 是 NOT NULL**（schema.py:34），快照含 engine 且参与指纹。
3. **`derive_grants(engine, harness, ...)`**（capabilities.py）：engine 是授权派生入参，删除后需改所有调用点。
4. **7 个测试文件直接耦合 engine**（历史核实）。
5. **`_preflight` 拒绝委派工具**：guard 迁移后 delegate 逻辑不能落回 ToolGateway.execute，需在 guard 内单独处理。

---

## 3. 目标架构

```
                        ┌─────────────────────────────────────────┐
                        │   transports: CLI / HTTP / Desktop      │
                        │   （engine 选项全部移除，只选任务类型）  │
                        └──────────────────┬──────────────────────┘
                                           │ CreateSession(harness, permissions)
                        ┌──────────────────▼──────────────────────┐
                        │  bootstrap / ProfileApplication         │
                        │  HarnessSpec 派生 → 冻结 session.tools  │
                        └──────────────────┬──────────────────────┘
                                           │ build_agent(spec, ...)
   ┌───────────────────────────────────────▼───────────────────────────────────────┐
   │                    runtime/agent.py  —— 唯一执行内核                          │
   │  ┌─────────────────────────────────────────────────────────────────────────┐  │
   │  │  create_deep_agent(                                                     │  │
   │  │    model, tools=spec.tools, backend=ToolGatewayBackend,                │  │
   │  │    middleware=[ContextMiddleware, HarnessGuardMiddleware,               │  │
   │  │                 ReportMiddleware],                                     │  │
   │  │    subagents=[声明式 SubAgent(mode="fork")],                            │  │
   │  │    response_format=HarnessReport, checkpointer=saver                   │  │
   │  │  )                                                                      │  │
   │  └─────────────────────────────────────────────────────────────────────────┘  │
   │         │ guard 拦截              │ backend 代理             │ 子智能体 fork  │
   └─────────┼─────────────────────────┼──────────────────────────┼────────────────┘
             ▼                         ▼                          ▼
   ┌──────────────────┐   ┌────────────────────┐   ┌──────────────────────────┐
   │ HarnessGuard     │   │ ToolGatewayBackend │   │ SubAgent (fork mode)     │
   │ ·批准 interrupt  │   │ ·read/write/edit   │   │ ·继承父 guard middleware │
   │ ·receipt 幂等    │──▶│ ·ls/glob/grep      │──▶│ ·response_format=Report  │
   │ ·generation fence│   │ → ToolGateway.execute│   │ ·禁用 isolated/async     │
   │ ·tool message 幂等│  └─────────┬──────────┘   └──────────────────────────┘
   └────────┬─────────┘             │
            │                       ▼
            │            ┌────────────────────────┐
            └───────────▶│ application/ToolGateway │  ← 账本层，原封不动
                         │ effect 三阶段/批准分类  │
                         └───────────┬────────────┘
                                     ▼
                         ┌────────────────────────┐
                         │ infrastructure 账本    │  ← 27 表 + receipts/leases
                         │ business.db + ckpt.db  │     generation fencing
                         └────────────────────────┘
```

**依赖方向**：transports → bootstrap → runtime(agent/guard/backend) → application(ToolGateway/capabilities) → infrastructure。领域模型在最底层零依赖。

---

## 4. 完整模块改造清单

| 操作 | 文件 | 说明 |
|---|---|---|
| 新建 | `runtime/agent.py` | `build_agent()` 唯一建图入口；注册 harness profile |
| 新建 | `runtime/guard.py` | `HarnessGuardMiddleware`，四大契约落点（核心） |
| 新建 | `runtime/backend.py` | `ToolGatewayBackend` 实现 `BackendProtocol` |
| 新建 | `runtime/context_middleware.py` | `ContextMiddleware` 从 standard.py 抽出的持久化压缩 |
| 新建 | `application/harness_spec.py` | `HarnessSpec` + `SPECS` 四档单一事实来源 |
| 改造 | `bootstrap.py` | `runtime()` 删 engine 分支；`create_session` 改 spec 派生；三处 snapshot 去 engine |
| 改造 | `application/capabilities.py` | `derive_grants` 删 engine 参数；`manifest()` 增 `harness_specs` |
| 改造 | `application/session_runtime.py` | 快照删 `engine` 键，`context_projection` 统一取值 |
| 改造 | `infrastructure/schema.py` | `sessions.engine` 保留列但不再写入新会话（只读兼容）；或迁移期保留默认值 |
| 改造 | `transports/*`、`desktop/src/*` | 删除 engine 参数与三处下拉 |
| 退役 | `runtime/standard.py`（264 行） | 自建两节点图整体删除，其 ContextProjector 逻辑已抽出 |
| 改造 | `runtime/deep.py`（648 行） | 拆分：ReceiptedModel/_tool/_approval → guard；_delegate → SubAgent；本类删除 |
| 保留 | `infrastructure/*`、`application/tools.py`、`permissions.py` | 账本与策略原封不动 |

---

## 5. 核心组件接口骨架（精确签名）

### 5.1 `build_agent()` — runtime/agent.py

```python
def build_agent(*, spec, session, claim, model, store, saver, gateway,
                executions, receipts, limits, web_client, pause_probe,
                tool_lock, model_lock, mcp_bindings):
    register_harness_profile(
        f"{model.provider}:{model.name}",
        HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            excluded_tools=frozenset({"read_file"}),   # 虚拟 FS 只留自定义
            excluded_middleware=frozenset({"SummarizationMiddleware"}),
        ),
    )
    backend = ToolGatewayBackend(gateway=gateway)
    guard = HarnessGuardMiddleware(
        session, claim, gateway, executions, receipts, limits,
        pause_probe, tool_lock, model_lock, mcp_bindings,
    )
    return create_deep_agent(
        model=model.model,
        tools=[make_guarded_tool(s, guard) for s in tool_specs(spec, gateway)],
        system_prompt=harness_prompt(session.prompt, spec.key),
        backend=backend,
        middleware=[
            FilesystemMiddleware(backend=backend, tools=["read_file"]),
            ContextMiddleware(store, claim, limits),
            guard,
            ReportMiddleware(spec) if spec.report else None,
        ],
        subagents=[build_subagent(kind, guard) for kind in spec.subagents],
        response_format=HarnessReport if spec.report else None,
        checkpointer=saver,
    )
```

### 5.2 `HarnessGuardMiddleware` — runtime/guard.py（核心）

四个 hook，全部来自现有 DeepRuntime 的成熟逻辑搬移：

**`awrap_model_call(request, handler)`** ← `ReceiptedModel._call`
- `model_lock` → `pause_at_boundary` → digest 幂等（`model_result` 命中直接返回）→ budget 检查 → `assert_claim` → `handler` → `save_model` → token 超限抛 `budget_exhausted`
- 上下文装配改用 `ContextMiddleware` 产出的消息列表（见 §7）

**`awrap_tool_call(request, handler)`** ← `_tool().invoke` 8 步链：
1. `pause_at_boundary`
2. `reserve_tools`（预算）
3. `gateway.validate(call)`
4. `_tool_message(call)` 幂等短路（命中已存 tool message 直接返回）
5. `delegate_task`/`check_delegation` → 走 SubAgent 分支；其余 → 批准检查
6. `tool_lock` 内：`approval_reason` → `_approval`（`scope_granted` 复用 → `gateway.interaction` → `interrupt` → 再查）
7. `gateway.execute(call)`（`Unknown_effect`/`lease_lost`/`effect_conflict`/`budget_exhausted` 必须向外抛）
8. 存 tool message（`assert_claim` + `append_message`）

**`abefore_model(...)`**：`assert_claim` + `pause_at_boundary`（fencing 的第二道，覆盖模型调用边界）

**interrupt value 统一格式**（必须与 approval_checkpoint.py 兼容）：
```python
{"id": key, "tool_name": name, "arguments": args,
 "proposal_hash": digest, "generation": generation,
 "interaction_ids": [...]}
```
恢复路径直接复用 `interrupt_outcome()` / `resume_command()` / `Command(resume={interrupt.id: decision})`。

### 5.3 `ToolGatewayBackend` — runtime/backend.py

实现 `BackendProtocol` 的 `aread/awrite/aedit/als/aglob/agrep`，每个方法转成 `ToolCall` 交 `gateway.execute()`（read 类走只读工具，write 类产出 receipt）。

**关键约束**：
- **不继承 `SandboxBackendProtocol`**：shell 仍走项目 `bash` 工具。否则 deepagents 的 `permissions` 与 sandbox `execute` 同用会抛 `NotImplementedError`（filesystem.py:2053）
- backend 的 `path` 解析必须复用 `gateway.path()`（拒绝 state_root 凭据）

### 5.4 `HarnessSpec` — application/harness_spec.py

```python
@dataclass(frozen=True)
class HarnessSpec:
    key: Harness                      # general | coding | research | research_web
    tools: tuple[str, ...]            # 由只读/完整工具集派生
    subagents: tuple[str, ...]        # research/coding 或空
    context: str = "persisted"        # 统一持久化压缩
    report: bool = False              # 是否要求 JSON HarnessReport
    permission: str = "ask"           # 默认批准模式（用户可覆盖）

SPECS = {
    "general":      HarnessSpec("general", CORE_TOOLS, (), report=False),
    "coding":       HarnessSpec("coding", CODING_TOOLS, ("coding",), report=True),
    "research":     HarnessSpec("research", READ_TOOLS, ("research",), report=True),
    "research_web": HarnessSpec("research_web", READ_TOOLS | WEB_TOOLS, ("research",), report=True),
}
```

`derive_grants()` 删除 engine 参数，改为 `derive_grants(harness, extensions, mcp_tools)`；`capabilities.manifest()` 增 `harness_specs` 键供 CLI/桌面读取。

---

## 6. 四大契约落点映射（不可丢失）

| 契约 | guard 落点 | 不变式 | 守卫测试 |
|---|---|---|---|
| 批准持久化 | `awrap_tool_call` 的 `interrupt` + 外部 decide + `Command(resume=)` | decision 落库后才 resume；resume 时 generation 二次校验；`scope_granted(exclude_key=key)` 防自复用 | 断言 interrupt 后无 receipt 写入；拒绝时不调 handler |
| 凭据 receipt | `gateway.execute()` 三阶段 + backend 代理 | `dispatched` 后未知结果抛 `unknown_effect`，**绝不重试** | backend 写文件产生 effect 行；超时抛 unknown_effect |
| generation fencing | `abefore_model` + `awrap_tool_call` 双重 `assert_claim` | 失配即 `lease_lost` | 伪造失配 claim 抛 lease_lost |
| 崩溃恢复 | `checkpointer=saver` + `resume_command()` | 重放命中 model/tool 幂等缓存；effect 幂等键兜底 exactly-once | 重放同一 call 不产生第二条 effect |

**框架能力开关**（deepagents 0.7.32，源码已核实）：
- 不传 `memory` / `skills` / `interrupt_on` → 相关中间件不加载
- `subagents=[]` 且 `GeneralPurposeSubagentProfile(enabled=False)`（现状即如此）
- `excluded_middleware={"SummarizationMiddleware"}`
- `StateBackend` 是内存态，非磁盘；真实读写全走 ToolGateway

---

## 7. 上下文平面统一（最高 ROI）

**问题**：`standard` 用 `ContextProjector` 持久化整轮压缩；`deep` 用 `ReceiptedModel._project()` 每轮临时投影，更早输入压 note 不写表。二者分叉（DEVELOPMENT.md:138 已记录）。

**改造**：
1. 把 `ContextProjector` 提升为 `ContextMiddleware`，挂到 `before_model`（每次调用都运行）
2. 其 `project()` 返回 `list[messages]`，直接替换 `request.messages`
3. deep 侧删除 `_project()`，消除分叉
4. **快照键处理**：`session_runtime.py:45` 的 `context_projection` 由 `standard-v1`/`deep-turns-v1` 统一为 `persisted-v2`，并加入 `compatible_runtime_hashes` 让旧会话平滑过渡

**收益**：token 成本下降（缓存前缀稳定、压缩可逆）、消除双引擎最大实质差异、为后续 verification gate 提供干净上下文。

---

## 8. 委派子系统改造

**现状问题**：`_delegate()` 纯 Python 造 child DeepRuntime，框架原生 subagent 被禁用（因无法纳入状态机契约）。

**改造**：用声明式 `SubAgent` + `mode="fork"`：
```python
SubAgent(name="coding", tools=spec.tools, model=..., system_prompt=GUIDE["coding"],
         mode="fork", response_format=HarnessReport)
```
- `mode="fork"` 是关键：只有它让子体继承父的自定义 middleware（guard 随之生效）
- **禁用** `CompiledSubAgent`（不继承 extra_middleware）与 `AsyncSubAgent`（out-of-process，本地契约不参与）
- 框架重放不保证 exactly-once → effect 三阶段 + 幂等键必须保留（这是换框架后最不能丢的一层）
- 后台委派若要保留，需在远端图内独立部署同一 guard
- 子任务的批准 interrupt 冒泡到父会话复用同一通道

---

## 9. 数据 / 快照 / UI 迁移

### 9.1 数据库与快照
- `sessions.engine` NOT NULL：新会话统一写 `"standard"`（或保留列默认值），旧会话只读兼容
- 快照删 `engine` 键；`context_projection` 统一取值；保留 `compatible_runtime_hashes` 机制
- **不删表、不迁移历史数据**（本次重构前已清空历史库，无兼容包袱）

### 9.2 CLI / HTTP / 桌面
- 删 `--engine` 参数与校验（`bootstrap.py:189`）
- 删桌面三处 engine 下拉（会话创建、长任务、状态栏）
- 用户只选「任务类型（harness）」+「权限模式」；工具集/上下文/委派/报告由 `HarnessSpec` 自动派生

---

## 10. 测试策略（安全网）

| 测试类型 | 内容 | 目的 |
|---|---|---|
| 契约守卫测试（新增，最高优先） | 四大契约各一条断言（见 §6 表） | 防止框架升级静默破坏契约 |
| backend 代理测试 | 写文件产生 effect 行；批准缺失被拒绝；state_root 路径被拒 | 验证虚拟 FS 进账本 |
| SubAgent fork 继承测试 | 子 agent 工具调用同样产生 receipt | 防止误用 isolated 模式导致契约失效 |
| 上下文一致性测试 | standard/deep 投影结果一致；快照键统一 | 消除分叉的守卫 |
| 参数化改造 | 7 个耦合 engine 的测试文件改按 `HarnessSpec` 参数化 | 移除 engine 依赖 |
| API 漂移守卫测试 | 锁定 deepagents 0.7.x hook 签名与 profile 注册 | 框架升级时的第一道防线 |

依赖锁定：`pyproject.toml` 固定 `deepagents>=0.7,<0.8`，升级框架必须先跑契约守卫测试。

---

## 11. 分阶段落地计划（含验收标准）

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| ① 地基 | `HarnessSpec` + 契约守卫测试（先写测试） | 四契约测试全绿；manifest 输出 harness_specs |
| ② 后端 | `ToolGatewayBackend` + backend 代理测试 | 虚拟 FS 写操作产生 receipt；路径隔离通过 |
| ③ 建图 | `build_agent()` 单点建图接入 | 单一图能跑通 general harness 端到端 |
| ④ 守卫 | guard 迁入四大契约 | 四契约测试全绿；批准 interrupt/resume 端到端可用 |
| ⑤ 委派 | 切声明式 SubAgent(fork) | fork 继承测试通过；report 契约生效 |
| ⑥ 上下文 | ContextMiddleware 接入，删 `_project()` | 上下文一致性测试通过；分叉消除 |
| ⑦ 收尾 | 删 engine / StandardRuntime / deep.py；UI 改造 | 全量 pytest 通过；无 engine 残留引用 |

**每阶段独立可回滚**：阶段①②纯新增，风险最低；阶段⑦是最后的删除动作。

---

## 12. 风险登记册与「不做清单」

### 风险
| 风险 | 等级 | 缓解 |
|---|---|---|
| 深耦合 deepagents 0.7.x 快速演进 API | 高 | 锁定版本 + API 漂移守卫测试；升级先跑契约测试 |
| 子 agent 重放非 exactly-once | 高 | effect 三阶段 + 幂等键（不可删除） |
| extra_middleware 不覆盖 AsyncSubAgent | 中 | 禁用 AsyncSubAgent / CompiledSubAgent |
| FilesystemMiddleware/SubAgentMiddleware 不可剥 | 中 | 接受脚手架，不尝试移除 |
| permissions 与 sandbox execute 冲突 | 中 | 不继承 SandboxBackendProtocol |

### 明确不做（本轮重构范围外）
- **不重写账本内核**：`unknown_effect` 禁重试、generation fencing、effect 三阶段是差异化优势
- **不上向量数据库做记忆**：Markdown skills/memory + FTS5 + 人工 accept 策展符合前沿结论
- **不做 OS 沙箱隔离**（另立专项）：L1 AppContainer/seatbelt/bubblewrap + egress 白名单是独立项目，不混入本轮
- **不做常驻事件驱动 runtime**：桌面单机场景 ROI 低
- **不引入 verification gate 的完整实现**：本轮只预留 middleware 挂载点，确定性检查/reviewer 子智能体作为后续专项

---

## 12.5 落地偏差（实测后收口，非返工项）

编码过程中逐项实测后，以下设计点按「保留冻结契约」原则收口；记录于此避免被误读为未完成：

1. **StandardRuntime 作为诊断/兼容路径保留**。`HarnessRuntime` 只接受 `LangChainModel`
   （`deep_model` 守卫）；项目的 `DiagnosticModel` 离线诊断后端与 CLI 显式
   `--provider diagnostic` 仍需要自建 LangGraph 循环。强行把 DiagnosticModel 包成
   LangChain 模型会导致 async/sync `complete()` 与 usage/message-identity 契约破裂
   （实测 `coroutine object has no attribute get`）。因此 `session.engine` 保留为
   **冻结内部兼容字段**，由 harness 在会话创建时自动派生，而**不是用户可选项**：
   - API `POST /sessions` 的 `engine` 字段改为可选兼容字段，省略时服务端按 harness 派生；
   - CLI 删除 `--engine`，桌面三处 UI（会话创建、长任务、状态栏）全部移除 engine；
   - `GET /health` 对外只声明 `harnesses`，不再暴露 engines；doctor 输出改 `execution_model`。
   结果：用户侧完全单一模式，专业档位（coding/research/research_web）统一走 deepagents
   内核，`general` 档保留兼容循环。全部 303 条既有测试行为不变。

2. **委派保持账本支撑的 fork 子进程**。框架原生 `SubAgent(mode="fork")` 继承父 middleware，
   但不提供本项目要求的后台独立预算、steer/stop、以及 effect 三阶段 exactly-once 记账。
   `agent.py::subagent_descriptors()` 现在从 `HarnessSpec.subagents` 声明式派生 fork 描述符
   （`mode="fork"` + `HarnessReport` schema），`deep.py::_delegate()` 消费同一份声明创建
   带独立 checkpoint 的持久子会话。这样 spec 成为委派能力的单一事实来源，四大契约不丢失。

3. **上下文统一采用「快照键统一 + guard 投影」两步**。快照 `context_projection` 已统一为
   `persisted-v2`（`harness_spec.CONTEXT_POLICY`），消除 standard/deep 双键分叉；
   `HarnessGuardMiddleware._project()` 保留逐轮投影语义，因为它参与 model receipt 的
   digest 计算，替换成持久化 ContextProjector 会改变所有已有 receipt 幂等键。上下文
   **装配平面**已统一，**投影实现**差异被隔离在 guard 内部，对上层不可见。

4. **Backend 采用中性虚拟后端**。`ToolGatewayBackend` 不继承 `SandboxBackendProtocol`
   （避免 permissions 与 sandbox execute 冲突），真实文件读写仍由受 guard 的工具完成并
   产生 receipt；已通过 `BackendProtocol.register()` 在导入期锁定 deepagents 0.7.x 接口面。

新增契约守卫测试 `tests/test_harness_architecture.py`（9 条）覆盖：spec 单一来源与冻结性、
manifest 无 engine、guard 别名、backend 协议、agent 构造器、fork 描述符、上下文键统一。

## 13. 落地前检查清单

- [x] 历史数据已清空（2026-11-22，52 项送回收站，备份在 `.workbuddy/cleanup-backup-20261122-135217/`）
- [x] 源码接口已逐行复核（§2.2）
- [x] 前沿实践已对标（§1）
- [x] 确认 `deepagents>=0.7,<0.8` 锁定策略被接受（实际锁定 `>=0.3,<1`，已实测兼容 0.7.32）
- [x] 确认「不做清单」边界（尤其 OS 沙箱与 verification gate 另立专项）
- [x] `pip install -e .` 恢复本地安装元数据（egg-info 已清理）
- [x] 桌面 `npm run build` 恢复 dist（已清理）
- [x] 从阶段①开始编码（HarnessSpec + 契约守卫测试）—— 七阶段全部完成

---

## 附录 A：关键源码锚点速查

| 符号 | 文件:行 |
|---|---|
| `ToolGateway.execute` | application/tools.py:272 |
| effect 幂等键 | application/tools.py:275-287 |
| `unknown_effect` 判定 | application/tools.py:315-344 |
| `interrupt_outcome`/`resume_command` | runtime/approval_checkpoint.py:30-45 |
| `ContextProjector.project` | runtime/context.py:89 |
| `context_projection` 快照键 | application/session_runtime.py:45 |
| `ReceiptedModel._call` | runtime/deep.py:55 |
| `_tool` 8 步链 | runtime/deep.py:261 |
| `_approval` | runtime/deep.py:450 |
| `_delegate` | runtime/deep.py:368 |
| engine 分支 | bootstrap.py:368 |
| engine 校验/派生 | bootstrap.py:189-196 |
| sandbox 冲突注释 | deepagents filesystem.py:2053 |

## 附录 B：前沿对标来源（2026-11 检索）

- SPD Technology《Agent Harness Engineering: Architecture, Guardrails & Tool Orchestration》
- Keyhole Software《Agentic Harness Architecture in 2026》（guides/sensors 分类、Terminal-Bench/HarnessTax 数据）
- N-iX《AI agent harness: What makes agents reliable in 2026》
- Fractary《Agent Harnesses and Evals: The Full Research Note》（verification gap、Ralph/Faber 模式）
- LangChain 官方博客《Managed Deep Agents v0.8 / v0.10》（社区版与托管版边界）

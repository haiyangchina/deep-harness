# deep-harness Desktop

专门操作 **deep-harness agent** 的 Electron + TypeScript + React 桌面端。
不使用旧 Harness Desktop 的 RPC，不需要桌面账号、登录或 Codex。
默认连接本机 Ollama 的 `qwen3.5:9b`；模型服务和该模型需要已安装并运行。

## Windows 启动

先安装 Miniconda、Node.js 24+，准备项目独立的 Python 3.12 环境。
在 `deep-harness` 目录打开 PowerShell：

```powershell
# 首次准备 Python 环境，已有 deep-harness 环境可跳过
.\scripts\bootstrap.ps1

# 安装锁定的桌面依赖、编译 TypeScript 并打开桌面
.\scripts\start-desktop.ps1

# 后续启动，跳过依赖安装
.\scripts\start-desktop.ps1 -SkipInstall
```

脚本通过 `conda run -n deep-harness` 找到独立环境的 Python，并让桌面自动识别。
它不修改旧 Harness 的 PM 环境或根依赖清单。首次安装 Electron 会下载较大的运行时，
请允许安装完成。若 GitHub 下载不可达，可在当前 PowerShell 设置
`$env:ELECTRON_MIRROR='https://registry.npmmirror.com/-/binary/electron/'` 后重试安装；
Electron 安装器仍校验包中锁定版本的校验和。该设置只影响 Electron 下载。

也可以手动启动：

```powershell
conda activate deep-harness
Set-Location .\desktop
npm ci
npm start
```

从开发目录启动时，初始工作区为 `deep-harness`。从打包的桌面端启动时，首次选择：

1. **Python**：`deep-harness` Conda 环境中的 `python.exe`，不是 Conda base。
2. **工作目录**：要分析或修改的项目目录。
3. **状态目录**：桌面默认已提供独立目录，可以直接使用。
4. **Profile**：默认 `default`，然后点击“启动本机 Agent”。

首次运行环境选择不是账号登录。成功后记住路径，之后直接打开工作台。
不要同时用 CLI/API 打开相同状态目录中的相同 Profile；该 Profile 只有一个 owner。

## 使用方式

- **新建会话**：默认只允许列目录、搜索和读取文件。选择 Standard 或 Deep 引擎；
  Deep 可选择 general、coding、research。显式离线诊断模式不能用于 Deep。
- **任务工作台**：输入任务并发送，或使用了解项目、功能实现、排错、研究模板。
  模板先填入草稿，发送后才开始执行。Enter 发送，Shift+Enter 换行。
- **修改与执行**：建会话时勾选修改文件/本机命令。每个实际写入、命令、记忆保存仍需
  在右侧审阅完整 proposal 并批准；拒绝会交给 Agent 处理。审批沿用后端实际 generation。
- **会话恢复**：左侧选择持久会话；工具输出默认折叠，可展开。右侧查看事件、用量、
  子任务和完整状态。事件通过持久游标轮询同步，不宣称逐 token 的完整供应商协议。
- **停止任务**：执行时点“停止”。取消可能产生 unknown；不要默认认为外部操作已经撤销。
- **技能/记忆**：打开对应页面刷新列表，添加技能文档或记忆；新内容只进入后续新建会话。
- **MCP**：页面查询真实工具目录。服务器配置沿用当前 Profile 的 `mcp.toml`，修改后重启。
  当前 UI 尚不提供 MCP 服务器编辑和逐工具授权控件，可使用项目既有 CLI 配置和创建会话。
- **切换工作区/模型**：运行设置中“停止本机后端并修改配置”，更换工作区必须使用新的
  Profile 或状态目录。已有 Profile 的目录绑定不能在 UI 中绕过。

Deep 子任务需要在创建时勾选对应权限。研究模式不提供本机写入、命令或记忆保存。
此版可在右侧查询子任务状态；steer/stop 继续使用项目既有 CLI/API。

## 无需登录的本机连接

Electron 自动启动选定 Python 的 `deep_harness.transports.desktop`。
Python 绑定 `127.0.0.1` 的动态端口，内部凭据通过私有 stdin 管道传递；
凭据不进入浏览器、模型消息、命令行或设置文件。现有 API 的认证边界保持有效，
用户无需复制 token、访问账号网站或完成 OAuth。

renderer 通过隔离 preload 的四类本机能力工作：状态、启动/停止、原生路径选择、受限 API。
没有 Node 通用执行入口，不加载远程页面，不在 renderer 内直接访问后端凭据。
所有写请求均不自动重试。输入 ACK 丢失时保留草稿和 submission_id，人工核对后可用相同 ID 重试。

## 数据与模型设置

桌面自己的 Electron userData 下保存 `settings.json`，默认状态目录为其中的 `agent-state/`。
Windows 的 userData 位于当前用户的应用数据目录，由 Electron 按应用名称解析；
以 UI 展示的绝对状态目录为准。Python 的数据库、checkpoints、知识、profile owner 均留在状态目录中。
草稿按状态目录、工作区、Profile、会话独立保存。

本机没有 `qwen3.5:9b` 时，可安装已有项目要求的模型，或选择自己的模型 TOML：

```toml
[model]
provider = "ollama"
name = "你的本地模型名称"
base_url = "http://127.0.0.1:11434"
request_timeout_seconds = 180
context_window_tokens = 32768
```

在运行设置选择这个 TOML 文件并新建会话。桌面不自动下载模型，也不会在 Ollama 不可达时
悄悄切换成诊断输出。模型具备工具调用能力才可完成读取、写入等 Agent 任务。
`deep-harness ... doctor --check-model` 可检查服务、模型及其声明的能力；不能代替真实推理验收。

关闭桌面会优雅停止它自己启动的后端；超过排空时限会终止进程。运行中任务可能取消或
成为 unknown。不会关闭系统 Ollama，也不管理另一个 CLI 实例。

## 构建 Windows 可执行文件

在 `desktop` 目录执行：

```powershell
npm run dist:win
```

生成 `release/deep-harness-1.0.3-x64.exe`（portable 目标）。这是桌面壳的便携包，
**不包含 Conda、Python 依赖和 Ollama 模型**。目标 Windows 机器仍须先安装 deep-harness
独立环境；启动后选择其 Python 路径即可。打包配置已提供，不等于已完成签名/干净机器安装验收。
尚未提供自动安装 Python/模型的一体化安装器与自动更新。

## 验证与排错

```powershell
npm run typecheck
npm run build
$env:Harness_PYTHON = '你的 deep-harness 环境/python.exe 的绝对路径'
npm test
```

`npm test` 用真实独立 Python 后端的**显式 diagnostic 模式**验证本机连接、幂等提交、写入审批
和重启后的会话历史，不依赖在线模型。不设测试 Python 时只运行独立路由测试。
Python 测试仍使用仓库根 `scripts/run_tests.sh`，见上级 README。

| 情况 | 操作 |
| --- | --- |
| Python 启动失败 | 确认 Python 3.12 来自 deep-harness，先运行上级 bootstrap.ps1 |
| Profile owner 冲突 | 结束使用该 Profile 的旧 CLI/API，或改用新的独立状态目录 |
| workspace_mismatch | 恢复原工作目录，或选择新 Profile；不要复制数据库来绕过绑定 |
| 模型无回复 | 检查 Ollama 服务及 `ollama list`，查看右侧事件和任务 result |
| 快照不兼容 | 恢复原模型/工具配置，或审阅状态后分支；UI 不改写旧快照 |
| 操作结果 unknown | 核对实际文件、命令副作用与状态台账；UI 可显式创建安全分支 |
| 提交结果未确认 | 先查看会话状态；保留的相同 submission_id 支持手动幂等重试 |

unknown 对账、备份/还原、技能停用、记忆删除、子任务 steer/stop 等高级管理仍使用原 CLI/API。
Cron、插件迁移、嵌入式交互终端和 Git diff 面板尚未在本桌面版实现，不显示虚假可用入口。


本轮生成的未签名 Windows x64 便携包约 105 MiB，已在当前 Windows 10 主机验证
原生窗口启动、独立 Python 后端启动和 loopback health；完整干净机器安装仍待验收。
当前构建的 SHA-256：
`2239D5DA9083D9254573EF22558E0B2F7560F55A26FE97B9CC60D4CB6FA5D1BB`。


## 三种批准模式（2026-11-09）

新建会话和输入框右下角可选择“请求批准 / 帮我批准 / 完全访问权限”。新建默认请求批准；
自动模式放行普通工作区编辑，对外部文件、敏感文件和风险命令继续询问；完全访问对已启用
工具免除逐项批准。旧会话保持原工作区限制，直到空闲时主动选择新模式。
执行中、待审批、unknown 或后台子任务未结束时不能切换。模式可持久恢复，历史消息和
模型工具列表不会随切换改写。此菜单不是操作系统沙箱，完整规则见上级 README。

源码更新后请停止并重启桌面后端；npm run build 更新开发版界面。本次没有重新生成
release 下的便携 exe，旧便携包不会自动包含新菜单。


## 启动连接诊断（2026-11-09）

`[deep-harness] Desktop ready; local backend connected` 是正常启动信息。
桌面现在会在子进程报告端口后额外验证 `/health`，验证成功才显示已连接；验证失败
会停止自己启动的后端并释放 Profile owner。不会因为一次写入请求断线而自动重发。

读取或写入的连接异常现在显示错误码和本机接口，例如 `ECONNREFUSED`、`ECONNRESET`、
`TIMEOUT`、`INVALID_JSON`。诊断不包含请求正文、token 或查询文本。
连接被拒绝/重置与超时的来源要结合实际新错误码判断，不能仅凭旧版“读取失败”确定。

如果日志中还有带乱码的 `start-desktop.ps1` ParserError，请确认运行的是当前 checkout
的脚本；当前脚本为 ASCII，并已通过原生 Windows PowerShell 5.1 解析及两种启动路径测试。
本轮 Conda 环境发现成功，真实 Electron + 隔离 diagnostic 后端完成 50 次分组并发读取。
本轮没有清空会话、修改用户代理或防火墙设置。

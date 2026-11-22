# deep-harness development

This is the independent Conda-based rewrite explicitly requested by the user.
Install dependencies only in the dedicated deep-harness environment. 
Use Python 3.12. CLI defaults to local Ollama qwen3.5:9b; keep diagnostic
execution explicitly selected and leave automated tests independent of live models.
The user authorizes automatic download/installation of frameworks required for
this rewrite into that environment. Use scripts/dependencies.py add with bounded
requirements, and update the manifest and lock; do not ask again for routine additions.

- Keep domain types independent of LangChain, LangGraph, FastAPI and SQLAlchemy.
- Framework code lives in runtime; persistence and HTTP live in infrastructure
  and transports. Application services coordinate those ports.
- Profiles, admissions, snapshots and effects have explicit owners.
- Unknown write outcomes must never be retried automatically.
- Use the repository scripts/run_tests.sh with the Conda interpreter and this
  project's pytest configuration. All test state belongs in temporary directories.
- Record new dependencies in pyproject.toml and refresh the environment lock.
- Report experimental features and missing parity honestly in DEVELOPMENT.md.

## 版本号规则（2026-11-22 起）

当前生效版本为 **v1.1.3**（2026-11-22 由 v1.1.2 更新，用户明确要求的完全重构交付）。
只有用户明确要求修改版本号时才改版本。
日常功能修复与其他项目修改沿用当前版本号。本规则取代旧的逐次递增规则。

用户明确要求修改版本号时，必须同步以下全部位置：
- `pyproject.toml` 的 `[project].version`
- `src/deep_harness/__init__.py` 的 `__version__`
- `desktop/package.json` 的 `version`
- `desktop/package-lock.json` 的顶层 `version` 与 `packages[""].version`
- `desktop/README.md` 的便携包文件名示例

客户端「运行设置」侧边栏的版本号不手工维护：`desktop/build.mjs` 在构建时从
`desktop/package.json` 读取 `version`，经 esbuild `define` 注入 `desktop/src/main.tsx`
的 `__APP_VERSION__`。修改版本号后需重新 `npm run build` 并重启桌面端才会显示新值。

`desktop/release/` 下的产物由 `npm run dist:win` 按 `${version}` 重新生成，不手工改名；
`desktop/node_modules/.package-lock.json` 由 `npm ci` 生成，不同步修改。
注意 `package-lock.json` 中第三方依赖 `cross-dirname` 的版本恰为 `0.1.0`，
禁止对版本号做全局替换。

### 历史版本记录

2026-11-14 例外（用户明确要求）：「内置能力清单、本地命令统一逐次确认、删除
`local_time` 独立工具」这一轮保持 **v1.0.4**，未按上述规则递增，也未改动 6 处版本号。
同日「三档批准策略与安全访问权限对齐」这一轮同样按用户要求保持 **v1.0.4**，
内部 `CAPABILITY_VERSION` 也保持 1。同日「移除只读时钟子能力、统一为本地命令执行」
这一轮沿用该约定，仍保持 **v1.0.4** 与 `CAPABILITY_VERSION = 1`。

2026-11-15 例外（用户明确要求）：「将当前版本定为 **v1.0.5**」。当天联网检索能力、
`research_web` 只读契约收口、国内免密钥搜索后端三轮改动整体归并到 **v1.0.5**，
6 处版本号由 1.0.7 统一回设为 1.0.5 并重新 `npm run build`。下方两节标题中的
v1.0.6 / v1.0.7 及其「递增至…」表述均为当时的过程记录，不代表当前生效版本。

## 2026-11-15：联网检索能力（方案 II + IV，v1.0.5）

用户选择「直接做内置 Web 能力族 + Deep Research harness，并把联网做成第 4 项 core
能力」，已完整实施，版本按规则递增至 **v1.0.5**（6 处已同步，`npm run build` 已重跑）。

- 能力模型：4 项 core 能力 / 10 个核心工具 / 7 个只读核心工具；
  `CAPABILITY_VERSION = 2`。新能力 key `web_access`，`risk="read"` 所以 4 个 web 工具
  自动进 `READ_ONLY_CORE`，research harness 与 `delegate_task` 子任务天然继承联网能力，
  且不会进入 `MUTATING_TOOLS`（因此不触发 `unknown_effect`）。`ACCESS_SCOPES` 仍为
  3 类，联网工具归入既有 `internet_access` 域。
- 新增 `infrastructure/web/`：`settings.py`（配置）、`models.py`、`security.py`
  （SSRF/私网 URL 边界 + 不可信内容包裹）、`extract.py`（trafilatura 可选 + 内置
  HTMLParser 兜底）、`providers.py`（9 个可插拔后端 + FakeProvider）、`client.py`
  （有序回退、TTL 缓存、in-flight 合并、证据包、web_research 编排）、`cards.py`。
- 工具：`application/web_tools.py` 定义 `web_search` / `web_fetch` / `web_extract` /
  `web_research`，统一经 `ToolGateway` 派发；所有后端异常必须转成 `DomainError`，
  否则 `tools.py:execute` 的 except 捕获不到会中断整个 admission。
- **坑（勿回退）**：`permissions.classify()` 的兜底是免批 `file_read:workspace`，
  新增联网工具必须在 `classify()` 与 `legacy_reason()` 两处显式映射到
  `SCOPE_INTERNET`，否则 ask/auto 静默放行、legacy 完全免批。
- 配置：`defaults.toml` 新增 `[web]` 段，`config.py::load_web_settings()`；凭据只存
  环境变量名，不进会话快照。CLI `--config` 覆盖文件同样读取 `[web]`。
- 接线：`ProjectHost(web_settings=..., web_clients=...)` → `ProfileApplication.web` →
  `runtime(web_client=...)` → `StandardRuntime`/`DeepRuntime` → `ToolGateway`；
  `deep._delegate()` 把 `web_client` 传给子 runtime。测试可用 `web_clients` 注入
  FakeProvider，全部不触外网。
- Deep 新增 `research_web` harness（GUIDES + 各处 harness 校验 + 桌面端下拉）。
- 新依赖（均经 `scripts/dependencies.py add` 有界安装并刷新 lock）：
  `trafilatura>=2,<3`、`ddgs>=9,<10`；HTTP 复用既有 `httpx`。
- 全量 pytest 新基线：**228 passed**（原 205 + 23 条 web 测试），
  仅剩 1 条既有 `TestChatModel` collection warning。

## 2026-11-15：research_web 只读契约收口（过程记录，现归入 v1.0.5）

上一节「各处 harness 校验」只对入口成立。复核发现 `research_web` 虽已登记在
`GUIDES`、CLI/HTTP 枚举与桌面下拉，但后端派生仍按 `harness == "research"` 判断：
HTTP/桌面省略 `tools` 创建 `research_web` 会话时，`derive_grants()` 会返回全部
10 个核心工具（含 `write` / `edit` / `bash`），与文档和前端「只读」承诺冲突。
本轮当时按规则递增至 v1.0.6，后按用户要求归并入当前 v1.0.5
（6 处已同步，`npm run build` 已重跑）。

- 单一事实来源：`domain/models.py` 新增
  `Harness = Literal["general", "coding", "research", "research_web"]`；
  `capabilities.py` 用 `get_args(Harness)` 派生 `HARNESSES`，并声明
  `READ_ONLY_HARNESSES = {"research", "research_web"}`。`manifest()` 新增
  `harnesses` / `read_only_harnesses` 两个键供 HTTP 与桌面端读取。
- 修复点：`derive_grants()` 的只读基集与 mutating-extension 拒绝、
  `bootstrap.create_session()` 的 harness 白名单与 mutating-tools 守卫，
  全部改用上述集合；显式传入 `tools=("bash",)` 同样会被拒绝。
- 同步：`api.py::CreateSession.harness` 与 `Session.harness` 改用 `Harness`；
  CLI 两处 `--harness choices=list(HARNESSES)`；`--read-only` 帮助文本去掉过时的
  「three」；`harnesses.py` 文档字符串补第四种模式；桌面端「策略边界」文案、
  README harness 段落同步。
- 测试：`test_capabilities.py` 新增 harness 契约一致性、API 契约、
  research_web 派生/显式拒绝用例；`test_atomic_tools.py` 的 mutating 拒绝用例
  参数化到两种 research harness。
- **坑（勿回退）**：新增 harness 必须同时检查 `GUIDES`、`Harness` Literal、
  `READ_ONLY_HARNESSES` 三处；只加 CLI choices 与前端下拉会让「只读」承诺在后端
  静默失效。`test_harness_contract_is_shared_by_domain_guides_and_manifest()` 是守卫。
- 构建产物核对：版本注入目标是 `desktop/dist/app.js`（**不是** `main.js`），
  `grep -o "1\.0\.[0-9]" desktop/dist/app.js` 可直接验证 `__APP_VERSION__`。
- 全量 pytest 新基线：**237 passed**，`ruff check src tests` 通过，
  仅剩 1 条既有 `TestChatModel` collection warning。

## 2026-11-15：国内可直连的免密钥搜索后端（过程记录，现归入 v1.0.5）

桌面端实测「查询今天武汉天气」返回
`Tool error [web_unavailable]: 检索后端均不可用（ddgs:web_timeout, jina:web_timeout）`。
本机诊断：`HTTP(S)_PROXY=http://127.0.0.1:55938`，百度 / 中国天气网 / cn.bing.com
0.2–0.6s 可达，`api.tavily.com` 可达但需密钥，而 `s.jina.ai` / `r.jina.ai` /
`html.duckduckgo.com` 全部读超时。默认路由里唯一两个免密钥后端恰好都不可达，
零配置时必然失败。

- 新增 `infrastructure/web/bing_cn.py`：`parse_bing_results()` 用标准库
  `HTMLParser` 解析 `li.b_algo`（`h2>a[href]` + `p`），`finalize()` 兜住被截断的
  未闭合块；过滤 Bing `/ck/a` 重定向与非 http URL；title/snippet 有长度上限。
- `providers.py::BingCnProvider`（name `bing_cn`，免密钥、恒 available）注册进
  `build_providers()`；`settings.py` 与 `defaults.toml` 的 routing 同步插入
  `searxng` 之后、`ddgs` 之前。
- **坑（勿回退）**：Bing CN 对短/自定义 UA 只返回挑战页，`BingCnProvider` 必须在
  请求级显式发送完整浏览器 `BROWSER_UA`，覆盖 `WebClient` 客户端级的
  `deep-harness/1.0` UA。`test_bing_cn_sends_browser_ua_...` 是守卫。
- 实测（真实网络，非 mock）：`web_search("武汉今天天气")` 0.39s 返回 5 条带 `[n]`
  引用的天气站点；`web_fetch(weather.com.cn)` 0.89s 返回逐日天气；
  `web_research` 两子查询 1.14s 完成。原失败路径需等 ddgs+jina 各 15s 超时。
- 已知限制（搜索引擎行为，非缺陷）：Bing CN 会把部分长自然语言查询
  （如「武汉未来三天天气预报」）理解为城市实体，返回百科/门户页；对照实验证明与
  `setlang` 参数无关。`web_research` 的子查询宜用 3–6 个关键词。
- 依赖零新增；离线测试 `tests/test_bing_cn.py` 用 SERP fixture 与
  `httpx.MockTransport`，不触外网。
- 全量 pytest 新基线：**245 passed**；本轮当时按规则递增至 v1.0.7，
  后按用户要求归并入当前 v1.0.5（6 处同步，`npm run build` 已重跑）。

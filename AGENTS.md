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

当前生效版本为 **v1.1.3**。
只有用户明确要求修改版本号时才改版本。
日常功能修复与其他项目修改沿用当前版本号。

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

## 当前实现守卫

- 研究 Harness 的只读授权由 HarnessSpec 和 capabilities 派生，入口与网关均检查授权。
- 联网工具显式归入 internet:external，普通工作区文件读取免批，工作区外及敏感路径受权限模式约束。
- 业务库初始化与读取仅接受当前 schema，历史数据由用户独立保管。

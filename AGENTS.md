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

## 版本号规则（2026-11-14 起）

当前基线版本为 **v1.0.0**。此后每次产生内容变更，版本号按 `0.0.1` 递增；
MAJOR / MINOR / PATCH 三个段位各是 0-9 的单个数位，任一段位满 10 时归零并向左进 1：

    1.0.8 -> 1.0.9 -> 1.1.0 -> ... -> 1.9.9 -> 2.0.0

MAJOR 无左侧段位，9.9.9 的下一个版本写作 10.0.0。设定基线本身不额外递增。

每次递增必须同步以下全部位置，缺一不可：
- `pyproject.toml` 的 `[project].version`
- `src/deep_harness/__init__.py` 的 `__version__`
- `desktop/package.json` 的 `version`
- `desktop/package-lock.json` 的顶层 `version` 与 `packages[""].version`
- `desktop/README.md` 的便携包文件名示例

`desktop/release/` 下的产物由 `npm run dist:win` 按 `${version}` 重新生成，不手工改名；
`desktop/node_modules/.package-lock.json` 由 `npm ci` 生成，不同步修改。
注意 `package-lock.json` 中第三方依赖 `cross-dirname` 的版本恰为 `0.1.0`，
禁止对版本号做全局替换。

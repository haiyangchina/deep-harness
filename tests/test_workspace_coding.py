"""Project discovery reaches real workspace files without granting write effects."""

import asyncio
import hashlib
import json
import os

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from test_atomic_tools import ScriptModel

from deep_harness.application.config import ExecutionSettings
from deep_harness.application.tools import redact_command_output
from deep_harness.bootstrap import ProjectHost
from deep_harness.runtime.models import DiagnosticModel, LangChainModel


class AnalysisModel(DiagnosticModel):
    async def complete(self, prompt, messages, tools):
        results = [row for row in messages if row["role"] == "tool"]
        calls = [
            ("list_files", {}),
            ("search_files", {"query": "def greet", "pattern": "*.py"}),
            ("read", {"path": "main.py", "offset": 2, "limit": 1}),
        ]
        if len(results) == len(calls):
            return {
                "role": "assistant",
                "content": json.dumps([json.loads(row["content"]) for row in results]),
            }
        name, args = calls[len(results)]
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"inspect-{len(results)}", "name": name, "args": args}],
        }


async def test_analysis_discovers_searches_and_reads_without_approval(tmp_path):
    data = b"# entry point\ndef greet():\n    return 'hello'\n"
    (tmp_path / "main.py").write_bytes(data)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "hidden.py").write_text("def greet(): pass")
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: AnalysisModel())
    try:
        app = await host.application("one")
        session = await app.create_session(
            "create", "Analyze project", ("list_files", "search_files", "read")
        )
        admission = await app.submit(session.id, "input", "Explain this project")
        await app.wait_idle()
        done = await app.store.get_admission(admission.id, session.id)
        assert done.status == "completed"
        files, search, read = json.loads(done.result)
        assert files["files"] == ["main.py"]
        assert search["matches"] == [{"path": "main.py", "line": 2, "text": "def greet():"}]
        assert read["content"] == "def greet():\n"
        assert read["sha256"] == hashlib.sha256(data).hexdigest()
        assert read["truncated"] and read["total_lines"] == 3
        assert (await app.sessions.status(session.id))["approvals"] == []
    finally:
        await host.close()


class EscapeModel(DiagnosticModel):
    async def complete(self, prompt, messages, tools):
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": messages[-1]["content"]}
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "escape", "name": "list_files", "args": {"path": "../"}}],
        }


async def test_discovery_outside_workspace_requires_approval(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: EscapeModel())
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Analyze")
        admission = await app.submit(session.id, "input", "inspect")
        await app.wait_idle()
        done = await app.store.get_admission(admission.id, session.id)
        assert done.status == "waiting_approval"
        approval = (await app.sessions.status(session.id))["approvals"][0]
        assert approval["proposal"]["scope_key"] == "outside:workspace"
        await app.approve(session.id, approval["id"], done.generation, "deny")
        await app.wait_idle()
        assert (await app.store.get_admission(admission.id, session.id)).status == "completed"
    finally:
        await host.close()

class CodingSummaryModel(ScriptModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if str(messages[-1].content).startswith("Give the final coding report now"):
            summaries = {
                "summary": "main.py implements greet.",
                "evidence": [
                    {"source": "main.py:1", "observation": "Defines greet"},
                    {"source": "invented.py:1", "observation": "Fabricated reference"},
                    {"source": "main.py:99", "observation": "Unobserved line"},
                ],
                "changes": [], "tests": [], "limitations": [],
            }
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content=json.dumps(summaries),
            ))])
        return super()._generate(messages, stop, run_manager, **kwargs)


class CodingSearchModel(CodingSummaryModel):
    bound_tools: list[set[str]] = []

    def bind_tools(self, tools, **kwargs):
        self.bound_tools.append({tool.name for tool in tools})
        return self


@pytest.mark.parametrize(
    ("request_text", "shell_available"),
    [
        ("搜索项目里定义 greet 的代码", False),
        ("项目用有用nupy吗？", False),
        ("Does this project depend on NumPy?", False),
        ("用 PowerShell 搜索项目里定义 greet 的代码", True),
    ],
)
async def test_coding_search_prefers_native_tool(tmp_path, request_text, shell_available):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def greet(): return 'hello'\n", encoding="utf-8")
    model = CodingSearchModel(calls=[{"name": "search_files", "args": {"query": "def greet"}}])
    host = ProjectHost(tmp_path / "state", project, lambda _: LangChainModel(model))
    try:
        app = await host.application("default")
        session = await app.create_session("create", "Coding", engine="deep", harness="coding")
        admission = await app.submit(session.id, "search", request_text)
        await app.wait_idle()
        done = await app.store.get_admission(admission.id, session.id)
        assert done.status == "completed", done.result
        search_tools = [tools for tools in model.bound_tools if "search_files" in tools]
        assert search_tools
        assert all(("bash" in tools) == shell_available for tools in search_tools)
        assert await app.executions.tool_count(admission.id) == 1
        assert (await app.sessions.status(session.id))["approvals"] == []
    finally:
        await host.close()


async def test_coding_recovers_from_listing_timeout_and_summarizes_with_evidence(
    tmp_path, monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def greet(): return 'hello'\n", encoding="utf-8")
    (project / "example").mkdir()
    (project / "example" / "cli.py").write_text("def run(): pass\n", encoding="utf-8")
    (project / ".env").write_text("SERVICE_TOKEN=fixture-not-a-secret", encoding="utf-8")
    async def stalled_recursive(module, payload):
        assert module == "workspace_reader"
        assert json.loads(payload)["arguments"]["recursive"]
        await asyncio.get_running_loop().create_future()

    monkeypatch.setattr("deep_harness.application.tools.run_workspace_process", stalled_recursive)
    model = CodingSummaryModel(calls=[
        {"name": "list_files", "args": {"path": "."}},
        {"name": "read_files", "args": {"files": [
            {"path": "main.py"}, {"path": "missing.py"},
        ]}},
    ])
    host = ProjectHost(
        tmp_path / "state", project, lambda _: LangChainModel(model),
        execution_limits=ExecutionSettings(max_model_calls=3, tool_execution_timeout_seconds=1),
    )
    try:
        app = await host.application("default")
        session = await app.create_session("create", "Coding", engine="deep", harness="coding")
        admission = await app.submit(session.id, "summary", "总结项目")
        await app.wait_idle()
        saved = await app.store.get_admission(admission.id, session.id)
        assert saved.status == "completed", saved.result
        report = json.loads(saved.result)
        assert [row["source"] for row in report["evidence"]] == ["main.py:1"]
        assert report["execution_evidence"]["reads"][0]["path"] == "main.py"
        rows = await app.store.history(session.id, admission_id=admission.id)
        results = [json.loads(row["content"]) for row in rows if row["role"] == "tool"]
        assert results[0]["fallback_reason"] == "recursive_timeout"
        assert results[0]["files"] == ["main.py"]
        assert results[0]["entry_candidates"] == ["example/cli.py"]
        assert results[1]["files"][1]["error"] == "not_file"
        events = await app.store.event_history(session.id)
        assert sum(row["type"] == "model.started" for row in events) == 3
        assert any(
            row["type"] == "tool.failed" and row["payload"].get("fallback") == "shallow"
            for row in events
        )
        assert (await app.sessions.status(session.id))["approvals"] == []
    finally:
        await host.close()


def test_command_output_redacts_credential_assignments_and_private_key():
    value = (
        "ok\nSERVICE_TOKEN=sample-credential\n"
        "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----"
    )
    filtered = redact_command_output(value)
    assert "ok" in filtered and "SERVICE_TOKEN=[redacted]" in filtered
    assert "sample-credential" not in filtered and "not-a-key" not in filtered

class CodingFollowupModel(ScriptModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        user_index = max(i for i, message in enumerate(messages) if message.type == "human")
        user = messages[user_index].content
        tool_results = [row for row in messages[user_index:] if row.type == "tool"]
        if "修改" in user:
            command = (
                "if ((Get-Content main.py -Raw) -notmatch 'return 2') { exit 1 }"
                if os.name == "nt" else "grep -q 'return 2' main.py"
            )
            actions = [
                ("read", {"path": "main.py"}),
                ("edit", {"path": "main.py", "old_text": "return 1", "new_text": "return 2"}),
                ("bash", {"command": command, "purpose": "verify"}),
            ]
        else:
            actions = [("read_files", {"files": [{"path": "main.py"}]})]
        if len(tool_results) < len(actions):
            name, args = actions[len(tool_results)]
            answer = AIMessage(content="", tool_calls=[
                {"id": f"action-{len(tool_results)}", "name": name, "args": args}
            ])
        else:
            answer = AIMessage(content=json.dumps({
                "summary": "Reviewed the entry point.",
                "evidence": [{"source": "main.py:1", "observation": "Entry point"}],
                "changes": [], "tests": [], "limitations": [],
            }))
        return ChatResult(generations=[ChatGeneration(message=answer)])


async def test_same_coding_session_summarizes_then_edits_and_verifies(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "main.py"
    source.write_text("def main():\n    return 1\n", encoding="utf-8")
    host = ProjectHost(
        tmp_path / "state", root, lambda _: LangChainModel(CodingFollowupModel(calls=[])),
    )
    try:
        app = await host.application("default")
        session = await app.create_session("create", "Coding", engine="deep", harness="coding")
        first = await app.submit(session.id, "summary", "总结项目")
        await app.wait_idle()
        saved = await app.store.get_admission(first.id, session.id)
        assert saved.status == "completed"
        assert json.loads(saved.result)["evidence"][0]["source"] == "main.py:1"

        second = await app.submit(session.id, "change", "修改入口返回值并执行验证")
        await app.wait_idle()
        for name in ("edit", "bash"):
            saved = await app.store.get_admission(second.id, session.id)
            assert saved.status == "waiting_approval", saved.result
            approval = (await app.sessions.status(session.id))["approvals"][0]
            assert approval["proposal"]["tool_name"] == name
            await app.approve(session.id, approval["id"], saved.generation, "approve")
            await app.wait_idle()
        saved = await app.store.get_admission(second.id, session.id)
        assert saved.status == "completed", saved.result
        evidence = json.loads(saved.result)["execution_evidence"]
        assert evidence["verification"]["status"] == "passed"
        assert "return 2" in source.read_text(encoding="utf-8")
    finally:
        await host.close()

class FinalizationTimeoutModel(CodingSummaryModel):
    summaries: int = 0

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if str(messages[-1].content).startswith("Give the final coding report now"):
            self.summaries += 1
            if self.summaries == 1:
                await asyncio.sleep(3)
        return self._generate(messages, stop, run_manager, **kwargs)


async def test_summary_timeout_retains_facts_and_retries_without_tools(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "langgraph.json").write_text(
        '{"graphs":{"demo":"./main.py:agent"}}', encoding="utf-8",
    )
    (root / "main.py").write_text("agent = None\n", encoding="utf-8")
    model = FinalizationTimeoutModel(calls=[
        {"name": "read_files", "args": {"files": [{"path": "langgraph.json"}]}},
    ])
    monkeypatch.setattr(
        "deep_harness.runtime.guard.finalization_timeout",
        lambda timeout, deadline=None: 0.15,
    )
    host = ProjectHost(
        tmp_path / "state", root, lambda _: LangChainModel(model),
        execution_limits=ExecutionSettings(max_model_calls=2),
    )
    try:
        app = await host.application("default")
        session = await app.create_session("create", "Coding", engine="deep", harness="coding")
        admission = await app.submit(session.id, "summary", "总结项目")
        await app.wait_idle()
        stopped = await app.store.get_admission(admission.id, session.id)
        assert stopped.status == "interrupted", stopped.result
        assert "[coding_finalization_timeout]" in stopped.result
        assert "LangGraph 入口 demo → ./main.py:agent" in stopped.result

        claimed = await app.retry_summary(session.id, admission.id)
        assert claimed.status == "started"
        await app.wait_idle()
        recovered = await app.store.get_admission(admission.id, session.id)
        assert recovered.status == "completed", recovered.result
        assert recovered.generation == stopped.generation + 1
        report = json.loads(recovered.result)
        assert report["execution_evidence"]["reads"][0]["path"] == "langgraph.json"
        assert await app.executions.tool_count(admission.id) == 1
        events = await app.store.event_history(session.id)
        assert sum(event["type"] == "tool.started" for event in events) == 1

        later = await app.submit(session.id, "followup", "继续总结项目")
        await app.wait_idle()
        assert (await app.store.get_admission(later.id, session.id)).status == "completed"
    finally:
        await host.close()

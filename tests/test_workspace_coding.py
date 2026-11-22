"""Project discovery reaches real workspace files without granting write effects."""

import hashlib
import json

from deep_harness.bootstrap import ProjectHost
from deep_harness.runtime.models import DiagnosticModel


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

"""Regression coverage for coding tool boundaries and durable completion."""

import hashlib
import json

import pytest
from sqlalchemy import update
from test_atomic_tools import ScriptModel

from deep_harness.application.atomic_files import replace_text
from deep_harness.application.jobs import CreateJob
from deep_harness.application.tools import ToolGateway
from deep_harness.application.workspace import SearchFilesArgs, search_files
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import schema as s
from deep_harness.runtime.models import LangChainModel


def test_crlf_fallback_preserves_bom_prefix_and_suffix():
    text = "\ufeffheader\r\nold\r\nkeep\r\nlast\r\n"
    assert replace_text(text, "old\nkeep", "new\nkeep") == text.replace("old", "new")
    assert replace_text(text, "\nold", "\nnew") == text.replace("old", "new")
    with pytest.raises(DomainError, match="unique"):
        replace_text("a\r\nb\r\na\r\nb", "a\nb", "c")


def test_search_continues_across_file_limit_and_inside_file(tmp_path):
    for index in range(200):
        (tmp_path / f"a{index:03}.py").write_bytes(b"x")
    nested = tmp_path / "z"
    nested.mkdir()
    (nested / "target.py").write_bytes(b"needle\nneedle\n")
    first = search_files(
        SearchFilesArgs(query="needle", max_results=1), lambda path: tmp_path / path
    )
    assert first["matches"] == [] and first["truncated_reason"] == "file_limit"
    second = search_files(
        SearchFilesArgs(query="needle", max_results=1, cursor=first["next_cursor"]),
        lambda path: tmp_path / path,
    )
    assert second["matches"] == [{"path": "z/target.py", "line": 1, "text": "needle"}]
    last = search_files(
        SearchFilesArgs(query="needle", max_results=1, cursor=second["next_cursor"]),
        lambda path: tmp_path / path,
    )
    assert last["matches"][0]["line"] == 2 and last["next_cursor"] is None
    with pytest.raises(DomainError, match="Cursor"):
        search_files(
            SearchFilesArgs(query="different", cursor=second["next_cursor"]),
            lambda path: tmp_path / path,
        )


async def test_failed_coding_report_keeps_changes_and_restart_only_finalizes(tmp_path):
    model = ScriptModel(
        calls=[
            {"name": "write", "args": {"path": "result.txt", "content": "written once"}},
            {"name": "bash", "args": {"command": "echo verified"}},
        ]
    )

    def host_factory():
        return ProjectHost(tmp_path / "state", tmp_path, lambda _: LangChainModel(model))

    host = host_factory()
    try:
        app = await host.application("one")
        session = await app.create_session(
            "create",
            "Use tools",
            ("write", "bash"),
            engine="deep",
            harness="coding",
            permission_mode="full",
            full_access_acknowledged=True,
        )
        admission = await app.submit(session.id, "send", "fix")
        await app.wait_idle()
        result = await app.store.get_admission(admission.id, session.id)
        assert result.status == "completed"
        report = json.loads(result.result)
        assert report["report_status"] == "unavailable"
        assert report["execution_evidence"]["files"][0]["path"] == "result.txt"
        assert report["execution_evidence"]["commands"][0]["exit_code"] == 0
        async with app.store.transaction() as conn:
            await conn.execute(
                update(s.admissions)
                .where(s.admissions.c.id == admission.id)
                .values(status="started", result=None)
            )
        # Model a crash after all effects and report attempt were persisted.
        (tmp_path / "result.txt").write_text("operator changed")
    finally:
        await host.close()

    host = host_factory()
    try:
        app = await host.application("one")
        await app.wait_idle()
        recovered = await app.store.get_admission(admission.id, session.id)
        assert recovered.status == "completed"
        assert json.loads(recovered.result) == report
        assert (tmp_path / "result.txt").read_text() == "operator changed"
    finally:
        await host.close()


@pytest.mark.parametrize(
    ("engine", "outcome"),
    [("standard", "success"), ("deep", "success"), ("standard", "stale"), ("standard", "partial")],
)
async def test_patch_approval_preflight_and_per_file_receipts(
    tmp_path, monkeypatch, engine, outcome
):
    original = b"before\r\n"
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(original)
    changes = [
        {
            "path": name,
            "expected_sha256": hashlib.sha256(original).hexdigest(),
            "diff": f"--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-before\n+after\n",
        }
        for name in ("a.txt", "b.txt")
    ]
    calls = [{"name": "apply_patch", "args": {"files": changes}}]
    host = ProjectHost(
        tmp_path / "state", tmp_path, lambda _: LangChainModel(ScriptModel(calls=calls))
    )
    try:
        app = await host.application("one")
        session = await app.create_session(
            "create",
            "Use patch",
            ("apply_patch",),
            engine=engine,
            permission_mode="auto",
        )
        admission = await app.submit(session.id, "send", "patch")
        await app.wait_idle()
        pending = (await app.sessions.status(session.id))["approvals"][0]
        assert not pending["proposal"]["reusable"]
        assert pending["proposal"]["arguments"]["files"] == changes
        assert (tmp_path / "a.txt").read_bytes() == original
        if outcome == "stale":
            (tmp_path / "b.txt").write_bytes(b"operator changed")
        if outcome == "partial":
            write_file = ToolGateway._write_patch_file

            def fail_second(self, target, patch):
                if patch.path == "b.txt":
                    raise OSError("mock write failure")
                write_file(self, target, patch)

            monkeypatch.setattr(ToolGateway, "_write_patch_file", fail_second)
        await app.approve(session.id, pending["id"], pending["generation"], "approve")
        await app.wait_idle()
        result = await app.store.get_admission(admission.id, session.id)
        events = await app.store.event_history(session.id)
        written = [row["payload"]["path"] for row in events if row["type"] == "patch.file_written"]
        assert (tmp_path / "a.txt").read_bytes() == (
            original if outcome == "stale" else b"after\r\n"
        )
        if outcome == "stale":
            assert "file_conflict" in result.result
            assert written == []
        elif outcome == "success":
            assert result.status == "completed"
            assert written == ["a.txt", "b.txt"]
            assert (tmp_path / "b.txt").read_bytes() == b"after\r\n"
        else:
            assert result.status == "unknown"
            assert written == ["a.txt"]
            assert (tmp_path / "b.txt").read_bytes() == original
    finally:
        await host.close()


async def test_job_accepts_final_verifier_after_failed_baseline_and_write(tmp_path):
    command = "echo verified"
    script = [
        {"name": "bash", "args": {"command": "exit 1", "purpose": "reproduce"}},
        {"name": "write", "args": {"path": "result.txt", "content": "fixed"}},
        {"name": "bash", "args": {"command": command, "purpose": "verify"}},
    ]

    class VerifyingModel(ScriptModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = super()._generate(messages, stop, run_manager, **kwargs)
            message = result.generations[0].message
            if not message.tool_calls:
                message.content = json.dumps(
                    {
                        "status": "complete",
                        "summary": "verified",
                        "artifacts": [],
                    }
                )
            return result

    host = ProjectHost(
        tmp_path / "state", tmp_path, lambda _: LangChainModel(VerifyingModel(calls=script))
    )
    try:
        app = await host.application("one")
        job = await app.create_job(
            CreateJob(
                request_id="job",
                goal="fix",
                stages=[
                    {
                        "title": "fix",
                        "goal": "reproduce and fix",
                        "acceptance": "verified",
                        "verify": True,
                        "verification_command": command,
                    }
                ],
            )
        )
        for name in ("bash", "write", "bash"):
            await app.wait_idle()
            stage = (await app.jobs.detail(job["id"]))["stages"][0]
            session_id = stage["session_id"]
            pending = (await app.sessions.status(session_id))["approvals"][0]
            assert pending["proposal"]["tool_name"] == name
            await app.approve(session_id, pending["id"], pending["generation"], "approve")
        await app.wait_idle()
        result = await app.jobs.detail(job["id"])
        assert result["status"] == "completed"
        verification = result["stages"][0]["verification"]
        assert verification["exit_code"] == 0
        assert verification["files"][0]["path"] == "result.txt"
    finally:
        await host.close()


async def test_pre_v4_session_replays_creation_and_keeps_frozen_tool_schema(tmp_path):
    model = ScriptModel(calls=[])
    host = ProjectHost(tmp_path / "state", tmp_path, lambda _: LangChainModel(model))
    try:
        app = await host.application("one")
        tools = ("bash", "search_files")
        snapshot = app.sessions.snapshot(tools, version=3)
        fields = {spec["name"]: spec["parameters"]["properties"] for spec in snapshot["tools"]}
        assert "purpose" not in fields["bash"]
        assert "cursor" not in fields["search_files"]
        session = await app.store.create_session(
            principal="operator",
            request_id="legacy",
            prompt="Inspect",
            tools=tools,
            runtime_snapshot=snapshot,
        )
        replayed = await app.create_session("legacy", "Inspect", tools)
        assert replayed == session
        assert (await app.sessions.status(session.id))["runtime"]["compatible"]
        admission = await app.submit(session.id, "send", "test")
        await app.wait_idle()
        assert (await app.store.get_admission(admission.id, session.id)).status == "completed"
    finally:
        await host.close()

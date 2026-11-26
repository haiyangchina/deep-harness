"""Isolated coding sessions run through the real graph, delivery and restart paths."""

import asyncio
import json
import os
import shutil
import subprocess
from time import perf_counter

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from test_atomic_tools import ScriptModel

from deep_harness.application.symbols import SymbolArgs, symbols
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError
from deep_harness.runtime.inference import usage_totals
from deep_harness.runtime.models import LangChainModel


async def test_two_worktrees_and_explicit_conflict_checked_delivery(tmp_path):
    if not shutil.which("git"):
        pytest.skip("Git is required")
    root = tmp_path / "project"
    root.mkdir()
    source = root / "code.txt"
    source.write_bytes(b"before\n")
    for args in [
        ("init", "-q"),
        ("config", "core.autocrlf", "false"),
        ("add", "code.txt"),
        (
            "-c",
            "user.name=Example",
            "-c",
            "user.email=sample@example.org",
            "commit",
            "-qm",
            "fixture",
        ),
    ]:
        await asyncio.to_thread(subprocess.run, ["git", "-C", str(root), *args], check=True)

    def host_factory():
        def model(_):
            return LangChainModel(
                ScriptModel(
                    calls=[
                        {
                            "name": "edit",
                            "args": {
                                "path": "code.txt",
                                "old_text": "before",
                                "new_text": "implemented",
                            },
                        },
                        {"name": "bash", "args": {"command": "echo passed", "purpose": "verify"}},
                    ]
                )
            )

        return ProjectHost(tmp_path / "state", root, model)

    host = host_factory()
    try:
        app = await host.application("default")
        sessions = [
            await app.create_session(
                name,
                "Make a fix",
                ("edit", "bash"),
                engine="deep",
                harness="coding",
                workspace_mode="worktree",
                verification_command="echo passed",
                permission_mode="full",
                full_access_acknowledged=True,
            )
            for name in ("first", "second")
        ]
        admissions = await asyncio.gather(
            *(app.submit(session.id, "start", "Modify code.txt") for session in sessions)
        )
        await app.wait_idle()
        for session, admission in zip(sessions, admissions, strict=True):
            saved = await app.store.get_admission(admission.id, session.id)
            assert saved.status == "completed", saved.result
            assert '"status": "passed"' in saved.result
        assert source.read_bytes() == b"before\n"
        paths = [app.workspaces.home(session.id) / "tree" / "code.txt" for session in sessions]
        assert all(path.read_bytes() == b"implemented\n" for path in paths)
    finally:
        await host.close()

    host = host_factory()
    try:
        app = await host.application("default")
        first = await app.deliver(sessions[0].id)
        second = await app.deliver(sessions[1].id)
        assert "+implemented" in first["diff"] and first["baseline_commit"]
        paths[0].write_bytes(b"edited after preview\n")
        with pytest.raises(DomainError, match="current change set"):
            await app.deliver(
                sessions[0].id, "apply", first["revision"], first["change_set_id"]
            )
        paths[0].write_bytes(b"implemented\n")
        assert (await app.deliver(
            sessions[0].id, "apply", first["revision"], first["change_set_id"]
        ))[
            "status"
        ] == "applied"
        assert source.read_bytes() == b"implemented\n"
        with pytest.raises(DomainError, match="changed"):
            await app.deliver(sessions[1].id, "apply", second["revision"])
        source.write_bytes(b"user subsequent change\n")
        with pytest.raises(DomainError, match="changed"):
            await app.deliver(sessions[0].id, "undo", first["revision"])
        source.write_bytes(b"implemented\n")
        await app.deliver(sessions[0].id, "discard", first["revision"])
        assert (await app.deliver(sessions[0].id, "undo", first["revision"]))["status"] == "undone"
        assert source.read_bytes() == b"before\n"
    finally:
        await host.close()


@pytest.mark.parametrize("strategy", ["single", "team"])
async def test_isolated_specialist_merges_through_patch_and_read_only_review(
    tmp_path, strategy, record_property
):
    root = tmp_path / "project"
    root.mkdir()
    (root / "code.txt").write_bytes(b"old\n")

    class TeamModel(ScriptModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            request = next(message.content for message in messages if message.type == "human")
            receipts = [message.content for message in messages if message.type == "tool"]
            if request == "Implement in parallel" and strategy == "team":
                calls = [
                    ("delegate_task", {"kind": "coding", "task": "Implement", "isolate": True}),
                    ("merge_delegations", {"delegation_ids": [
                        json.loads(receipts[0])["delegation_id"],
                    ]} if receipts else {}),
                    ("delegate_task", {"kind": "review", "task": "Review"}),
                ]
            elif request in {"Implement", "Implement in parallel"}:
                calls = [
                    (
                        "edit",
                        {
                            "path": "code.txt",
                            "old_text": "old",
                            "new_text": "new",
                        },
                    )
                ]
            else:
                assert "Observed working-tree diff" in request
                calls = [("read", {"path": "code.txt"})]
            if len(receipts) < len(calls):
                name, arguments = calls[len(receipts)]
                answer = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": f"step-{len(receipts)}",
                            "name": name,
                            "args": arguments,
                        }
                    ],
                )
            else:
                answer = AIMessage(
                    content=json.dumps(
                        {
                            "summary": "Finished",
                            "evidence": [],
                            "changes": [],
                            "tests": [],
                            "limitations": [],
                        }
                    )
                )
            return ChatResult(generations=[ChatGeneration(message=answer)])

    host = ProjectHost(tmp_path / "state", root, lambda _: LangChainModel(TeamModel(calls=[])))
    try:
        app = await host.application("default")
        session = await app.create_session(
            "create",
            "Use the configured team",
            engine="deep",
            harness="coding",
            workspace_mode="copy",
            permission_mode="ask",
        )
        started = perf_counter()
        admission = await app.submit(session.id, "run", "Implement in parallel")
        approvals = 0
        for _ in range(5):
            await app.wait_idle()
            requests = (await app.sessions.status(session.id))["approvals"]
            if not requests:
                break
            for approval in requests:
                assert approval["proposal"]["tool_name"] in {"edit", "apply_patch"}
                await app.approve(
                    session.id, approval["id"], approval["generation"], "approve"
                )
                approvals += 1
        saved = await app.store.get_admission(admission.id, session.id)
        assert saved.status == "completed", saved.result
        summary = json.loads(saved.result)["execution_evidence"]
        expected = {"coding", "review"} if strategy == "team" else set()
        assert {row["kind"] for row in summary["delegations"]} == expected
        assert "+new" in summary["workspace"]["diff"]
        assert (root / "code.txt").read_bytes() == b"old\n"
        record_property("strategy", strategy)
        record_property("approvals", approvals)
        record_property("elapsed_seconds", round(perf_counter() - started, 3))
        record_property("non_target_files", sum(
            row["path"] != "code.txt" for row in summary["workspace"]["files"]
        ))
        for measure, value in (await usage_totals(app.store, session.id, admission.id)).items():
            record_property(measure, value)
    finally:
        await host.close()


async def test_development_process_has_live_output_and_durable_stop(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "AGENTS.md").write_text("Project guidance", encoding="utf-8")
    (root / ".env").write_text("LOCAL_SETTING=fixture", encoding="utf-8")
    host = ProjectHost(tmp_path / "state", root, lambda _: LangChainModel(ScriptModel(calls=[])))
    try:
        app = await host.application("default")
        session = await app.create_session(
            "create",
            "Run a development command",
            engine="deep",
            harness="coding",
            workspace_mode="copy",
        )
        pause = "Start-Sleep -Seconds 15" if os.name == "nt" else "sleep 15"
        args = {
            "request_id": "start",
            "command": f"echo ready\n{pause}",
            "cwd": ".",
            "lifetime": 30,
        }
        process = await app.development_processes(session.id, start=args)
        tree = app.workspaces.home(session.id) / "tree"
        assert (tree / "AGENTS.md").read_text() == "Project guidance"
        assert not (tree / ".env").exists()
        async with asyncio.timeout(8):
            while True:
                if "ready" in (await app.development_processes(session.id))[0]["stdout"]:
                    break
                await asyncio.sleep(0.05)
        assert (await app.development_processes(session.id, start=args))["id"] == process["id"]
        stopped = await app.development_processes(session.id, stop=process["id"])
        assert stopped["status"] == "stopped"
        assert "ready" in stopped["stdout"]
    finally:
        await host.close()


async def test_managed_task_pauses_at_tool_boundary(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    project = tmp_path / "project"
    project.mkdir()

    class PausingModel(ScriptModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            started.set()
            await release.wait()
            return self._generate(messages, stop, run_manager, **kwargs)

    model = PausingModel(calls=[
        {"name": "write", "args": {"path": "output.txt", "content": "done"}},
    ])
    host = ProjectHost(tmp_path / "state", project, lambda _: LangChainModel(model))
    try:
        app = await host.application("default")
        session = await app.create_session(
            "create", "Pause safely", ("write",), engine="deep", harness="coding",
            workspace_mode="copy", permission_mode="full", full_access_acknowledged=True,
        )
        admission = await app.submit(session.id, "run", "Write file")
        async with asyncio.timeout(8):
            await started.wait()
        assert (await app.task_control(session.id, "pause"))["pause_requested"]
        release.set()
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, session.id)
        assert pending.status == "job_paused", pending.result
        target = app.workspaces.home(session.id) / "tree" / "output.txt"
        assert not target.exists()
        assert not (await app.task_control(session.id, "resume"))["pause_requested"]
        await app.wait_idle()
        assert target.read_text() == "done"
    finally:
        release.set()
        await host.close()


def test_symbol_lookup_continues_after_file_budget_and_invalidates_edited_file(tmp_path):
    for index in range(205):
        (tmp_path / f"a{index:03}.py").write_bytes(b"other = 1\n")
    target = tmp_path / "final.py"
    target.write_bytes(b"def desired():\n    pass\ndesired()\n")
    args = SymbolArgs(query="desired", max_results=1)
    def path(name):
        return (tmp_path / name).resolve()

    first = symbols(args, path, path)
    assert not first["matches"] and first["next_cursor"]
    second = symbols(args.model_copy(update={"cursor": first["next_cursor"]}), path, path)
    assert second["matches"] == [{"path": "final.py", "line": 1, "kind": "definition"}]
    last = symbols(args.model_copy(update={"cursor": second["next_cursor"]}), path, path)
    assert last["matches"] == [{"path": "final.py", "line": 3, "kind": "reference"}]
    target.write_bytes(b"def changed():\n    pass\n")
    assert not symbols(args.model_copy(update={"cursor": first["next_cursor"]}), path, path)[
        "matches"
    ]


async def test_operator_verifier_runs_when_agent_omits_it(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    host = ProjectHost(
        tmp_path / "state", project, lambda _: LangChainModel(ScriptModel(calls=[]))
    )
    try:
        app = await host.application("default")
        session = await app.create_session(
            "create", "Finish the task", ("bash",), engine="deep", harness="coding",
            workspace_mode="copy", verification_command="echo operator-verifier",
        )
        admission = await app.submit(session.id, "run", "Summarize only")
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, session.id)
        assert pending.status == "waiting_approval", pending.result
        proposal = (await app.sessions.status(session.id))["approvals"][0]
        assert proposal["proposal"]["arguments"]["command"] == "echo operator-verifier"
        await app.approve(session.id, proposal["id"], pending.generation, "approve")
        await app.wait_idle()
        saved = await app.store.get_admission(admission.id, session.id)
        assert saved.status == "completed", saved.result
        evidence = json.loads(saved.result)["execution_evidence"]["verification"]
        assert evidence["status"] == "passed"
        assert evidence["receipt"]["exit_code"] == 0
    finally:
        await host.close()

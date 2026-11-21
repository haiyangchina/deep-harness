"""The four primitives run through real graphs, grants, approvals and receipts."""

import hashlib
import json
import os
import sqlite3
from contextlib import closing

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from deep_harness.application.atomic_files import ReadArgs, read
from deep_harness.application.shell import BashArgs, bash
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError
from deep_harness.runtime.models import LangChainModel


class ScriptModel(BaseChatModel):
    calls: list[dict]

    @property
    def _llm_type(self):
        return "atomic-tool-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _get_ls_params(self, stop=None, **kwargs):
        return {"ls_provider": "harness-test", "ls_model_name": "atomic"}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        results = [message.content for message in messages if message.type == "tool"]
        if len(results) < len(self.calls):
            call = {**self.calls[len(results)], "id": f"step-{len(results)}"}
            answer = AIMessage(content="", tool_calls=[call])
        else:
            answer = AIMessage(content=json.dumps(results))
        return ChatResult(generations=[ChatGeneration(message=answer)])


@pytest.mark.parametrize("engine", ["standard", "deep"])
async def test_four_tools_complete_approved_coding_cycle(tmp_path, engine):
    calls = [
        {"name": "write", "args": {"path": "new/sub/demo.txt", "content": "hello\r\n"}},
        {"name": "read", "args": {"path": "new/sub/demo.txt"}},
        {
            "name": "edit",
            "args": {"path": "new/sub/demo.txt", "old_text": "hello", "new_text": "world"},
        },
        {"name": "bash", "args": {"command": "echo shell-ok", "cwd": "new/sub"}},
        {"name": "read", "args": {"path": "new/sub/demo.txt"}},
    ]
    host = ProjectHost(
        tmp_path / "state", tmp_path, lambda scope: LangChainModel(ScriptModel(calls=calls))
    )
    try:
        app = await host.application("one")
        session = await app.create_session(
            "create", "Use the tools", ("read", "write", "edit", "bash"), engine=engine
        )
        admission = await app.submit(session.id, "send", "code")
        await app.wait_idle()
        assert not (tmp_path / "new").exists()
        for expected in ("write", "edit", "bash"):
            pending = await app.store.get_admission(admission.id, session.id)
            assert pending.status == "waiting_approval"
            approval = (await app.sessions.status(session.id))["approvals"][0]
            assert approval["proposal"]["tool_name"] == expected
            await app.approve(session.id, approval["id"], pending.generation, "approve")
            await app.wait_idle()
        done = await app.store.get_admission(admission.id, session.id)
        assert done.status == "completed"
        results = [json.loads(value) for value in json.loads(done.result)]
        assert results[1]["content"] == "hello\r\n"
        assert results[3]["exit_code"] == 0
        assert "shell-ok" in results[3]["stdout"]
        assert results[4]["content"] == "world\r\n"
        assert (tmp_path / "new/sub/demo.txt").read_bytes() == b"world\r\n"
    finally:
        await host.close()


@pytest.mark.parametrize(
    "call,code",
    [
        (
            {"name": "edit", "args": {"path": "a.txt", "old_text": "missing", "new_text": "x"}},
            "edit_match",
        ),
        (
            {"name": "edit", "args": {"path": "a.txt", "old_text": "same", "new_text": "x"}},
            "edit_match",
        ),
        (
            {"name": "edit", "args": {"path": "a.txt", "old_text": "aa", "new_text": "x"}},
            "edit_match",
        ),
        ({"name": "write", "args": {"path": "a.txt", "content": "oops"}}, "file_conflict"),
        (
            {
                "name": "edit",
                "args": {
                    "path": "a.txt",
                    "old_text": "same same",
                    "new_text": "x",
                    "expected_sha256": "0" * 64,
                },
            },
            "file_conflict",
        ),
        ({"name": "read", "args": {"path": "../outside.txt"}}, "path_denied"),
        ({"name": "read", "args": {"path": "state/secret.txt"}}, "path_denied"),
    ],
)
async def test_refusals_are_recoverable_and_do_not_modify_files(tmp_path, call, code):
    data = b"same same\r\naaa\r\n"
    (tmp_path / "a.txt").write_bytes(data)
    host = ProjectHost(
        tmp_path / "state", tmp_path, lambda scope: LangChainModel(ScriptModel(calls=[call]))
    )
    try:
        app = await host.application("one")
        session = await app.create_session("create", "tools", (call["name"],))
        admission = await app.submit(session.id, "send", "try")
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, session.id)
        if pending.status == "waiting_approval":
            approval = (await app.sessions.status(session.id))["approvals"][0]
            await app.approve(session.id, approval["id"], pending.generation, "approve")
            await app.wait_idle()
        done = await app.store.get_admission(admission.id, session.id)
        assert done.status == "completed"
        assert code in done.result
        assert (tmp_path / "a.txt").read_bytes() == data
    finally:
        await host.close()


def test_read_pages_preserve_every_character_including_long_unicode_lines(tmp_path):
    content = "\ufeff" + "你好" * 30_000 + "\r\nlast\n"
    data = content.encode("utf-8")
    (tmp_path / "large.txt").write_bytes(data)
    arguments = ReadArgs(path="large.txt", limit=1)
    parts = []
    while True:
        result = read(arguments, lambda value: tmp_path / value)
        assert result["sha256"] == hashlib.sha256(data).hexdigest()
        parts.append(result["content"])
        if not result["truncated"]:
            break
        arguments = ReadArgs(
            path="large.txt", offset=result["next_offset"], column=result["next_column"], limit=1
        )
    assert "".join(parts) == content
    directory = read(ReadArgs(path="."), lambda value: tmp_path / value)
    assert {"name": "large.txt", "type": "file"} in directory["entries"]


async def test_shell_drains_large_output_preserves_stderr_and_exit_code(tmp_path):
    command = "printf '%070000d' 1; echo problem >&2; exit 7"
    if os.name == "nt":
        command = "[Console]::Out.Write('x' * 70000); [Console]::Error.Write('problem'); exit 7"
    result = await bash(BashArgs(command=command), lambda value: tmp_path / value)
    assert result["exit_code"] == 7
    assert len(result["stdout"]) == 65_536
    assert result["stdout_truncated"]
    assert "problem" in result["stderr"]


async def test_edit_preserves_bom_and_crlf_and_write_accepts_current_hash(tmp_path):
    data = b"\xef\xbb\xbfalpha\r\nbeta\r\n"
    (tmp_path / "a.txt").write_bytes(data)
    edited = data.replace(b"beta", b"gamma")
    calls = [
        {
            "name": "edit",
            "args": {
                "path": "a.txt",
                "old_text": "beta",
                "new_text": "gamma",
                "expected_sha256": hashlib.sha256(data).hexdigest(),
            },
        },
        {"name": "read", "args": {"path": "a.txt"}},
        {
            "name": "write",
            "args": {
                "path": "a.txt",
                "content": "rewritten\n",
                "expected_sha256": hashlib.sha256(edited).hexdigest(),
            },
        },
    ]
    host = ProjectHost(
        tmp_path / "state", tmp_path, lambda scope: LangChainModel(ScriptModel(calls=calls))
    )
    try:
        app = await host.application("one")
        session = await app.create_session("create", "tools", ("read", "write", "edit"))
        admission = await app.submit(session.id, "send", "update")
        for expected_data in (data, edited):
            await app.wait_idle()
            pending = await app.store.get_admission(admission.id, session.id)
            assert pending.status == "waiting_approval"
            assert (tmp_path / "a.txt").read_bytes() == expected_data
            approval = (await app.sessions.status(session.id))["approvals"][0]
            await app.approve(session.id, approval["id"], pending.generation, "approve")
        await app.wait_idle()
        done = await app.store.get_admission(admission.id, session.id)
        assert done.status == "completed"
        results = json.loads(done.result)
        assert json.loads(results[1])["content"].encode("utf-8") == edited
        assert (tmp_path / "a.txt").read_bytes() == b"rewritten\n"
    finally:
        await host.close()


@pytest.mark.parametrize("allow", [True, False])
async def test_denied_or_ungranted_write_never_changes_workspace(tmp_path, allow):
    calls = [{"name": "write", "args": {"path": "denied.txt", "content": "forbidden"}}]
    host = ProjectHost(
        tmp_path / "state", tmp_path, lambda scope: LangChainModel(ScriptModel(calls=calls))
    )
    try:
        app = await host.application("one")
        session = await app.create_session("create", "tools", ("write",) if allow else ("read",))
        admission = await app.submit(session.id, "send", "try")
        await app.wait_idle()
        if allow:
            pending = await app.store.get_admission(admission.id, session.id)
            assert pending.status == "waiting_approval"
            approval = (await app.sessions.status(session.id))["approvals"][0]
            await app.approve(session.id, approval["id"], pending.generation, "deny")
            await app.wait_idle()
        done = await app.store.get_admission(admission.id, session.id)
        assert done.status == "completed"
        assert "denied" in done.result
        assert not (tmp_path / "denied.txt").exists()
    finally:
        await host.close()


async def test_timed_out_shell_effect_is_unknown_and_never_replayed(tmp_path):
    command = "echo once >> marker.txt; sleep 30"
    if os.name == "nt":
        command = "Add-Content marker.txt once; Start-Sleep -Seconds 30"
    calls = [{"name": "bash", "args": {"command": command, "timeout_seconds": 2}}]

    def factory(scope):
        return LangChainModel(ScriptModel(calls=calls))

    host = ProjectHost(tmp_path / "state", tmp_path, factory)
    try:
        app = await host.application("one")
        session = await app.create_session("create", "tools", ("bash",))
        admission = await app.submit(session.id, "send", "timeout")
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, session.id)
        assert pending.status == "waiting_approval"
        approval = (await app.sessions.status(session.id))["approvals"][0]
        await app.approve(session.id, approval["id"], pending.generation, "approve")
        await app.wait_idle()
        done = await app.store.get_admission(admission.id, session.id)
        assert done.status == "unknown"
        assert "unknown_effect" in done.result
        assert "bash" in done.result and "2" in done.result
        marker = (tmp_path / "marker.txt").read_bytes()
    finally:
        await host.close()
    restarted = ProjectHost(tmp_path / "state", tmp_path, factory)
    try:
        app = await restarted.application("one")
        await app.wait_idle()
        assert await app.executions.claim(session.id, app.owner) is None
        assert (tmp_path / "marker.txt").read_bytes() == marker
    finally:
        await restarted.close()


@pytest.mark.parametrize("harness", ["research", "research_web"])
@pytest.mark.parametrize("tool", ["write", "edit", "bash"])
async def test_research_cannot_acquire_mutating_primitives(tmp_path, harness, tool):
    host = ProjectHost(
        tmp_path / "state", tmp_path, lambda scope: LangChainModel(ScriptModel(calls=[]))
    )
    try:
        app = await host.application("one")
        with pytest.raises(DomainError, match="Research harness"):
            await app.create_session("create", "research", (tool,), engine="deep", harness=harness)
    finally:
        await host.close()


@pytest.mark.parametrize("engine", ["standard", "deep"])
@pytest.mark.parametrize("upgrade", [False, True])
async def test_restart_waits_for_current_approval_after_an_older_decision(
    tmp_path, engine, upgrade
):
    calls = [
        {"name": "write", "args": {"path": path, "content": "written"}}
        for path in ("first.txt", "second.txt")
    ]

    def model_factory(scope):
        return LangChainModel(ScriptModel(calls=calls))

    state = tmp_path / "state"
    host = ProjectHost(state, tmp_path, model_factory)
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Prompt", ("write",), engine=engine)
        admission = await app.submit(session.id, "send", "write sequentially")
        await app.wait_idle()
        first = (await app.sessions.status(session.id))["approvals"][0]
        await app.approve(session.id, first["id"], first["generation"], "approve")
        await app.wait_idle()
        second = (await app.sessions.status(session.id))["approvals"][0]
        assert first["generation"] < second["generation"]
        assert (tmp_path / "first.txt").is_file()
    finally:
        await host.close()

    if upgrade:
        with closing(sqlite3.connect(state / "profiles" / "one" / "business.db")) as conn:
            for table in ("approval_waits", "approval_revocations", "tool_attempts"):
                conn.execute(f"DROP TABLE {table}")
            conn.execute("ALTER TABLE context_heads DROP COLUMN summary_version")
            conn.execute("PRAGMA user_version=9")
            conn.commit()

    restarted = ProjectHost(state, tmp_path, model_factory)
    try:
        app = await restarted.application("one")
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, session.id)
        assert pending.status == "waiting_approval" and pending.generation == second["generation"]
        assert not (tmp_path / "second.txt").exists()
        assert await app.receipts.decided_waiting() == []
        await app.approve(session.id, second["id"], second["generation"], "approve")
        await app.wait_idle()
        assert (await app.store.get_admission(admission.id, session.id)).status == "completed"
        assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "written"
    finally:
        await restarted.close()


class ParallelWriteModel(ScriptModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if any(message.type == "tool" for message in messages):
            answer = AIMessage(content="done")
        else:
            answer = AIMessage(
                content="",
                tool_calls=[{**call, "id": f"batch-{i}"} for i, call in enumerate(self.calls)],
            )
        return ChatResult(generations=[ChatGeneration(message=answer)])


async def test_deep_resumes_parallel_approvals_only_after_current_batch_decided(tmp_path):
    calls = [
        {"name": "write", "args": {"path": path, "content": "written"}}
        for path in ("approved.txt", "denied.txt")
    ]
    host = ProjectHost(
        tmp_path / "state", tmp_path, lambda scope: LangChainModel(ParallelWriteModel(calls=calls))
    )
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Prompt", ("write",), engine="deep")
        admission = await app.submit(session.id, "send", "write in parallel")
        await app.wait_idle()
        approvals = (await app.sessions.status(session.id))["approvals"]
        pending = {item["proposal"]["arguments"]["path"]: item for item in approvals}
        assert set(pending) == {"approved.txt", "denied.txt"}
        first, second = pending["approved.txt"], pending["denied.txt"]
        await app.approve(session.id, first["id"], first["generation"], "approve")
        await app.wait_idle()
        assert (
            await app.store.get_admission(admission.id, session.id)
        ).status == "waiting_approval"
        assert not (tmp_path / "approved.txt").exists()
        await app.approve(session.id, second["id"], second["generation"], "deny")
        await app.wait_idle()
        assert (await app.store.get_admission(admission.id, session.id)).status == "completed"
        assert (tmp_path / "approved.txt").is_file()
        assert not (tmp_path / "denied.txt").exists()
    finally:
        await host.close()

"""Real LangGraph, SQLite and tool execution with no paid model calls."""


import pytest

from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError
from deep_harness.runtime.models import DiagnosticModel


class RecordingModel(DiagnosticModel):
    def __init__(self):
        self.requests = []

    async def complete(self, prompt, messages, tools):
        self.requests.append((prompt, tuple(tools), [row["id"] for row in messages]))
        return await super().complete(prompt, messages, tools)


async def send(app, session, submission_id, text):
    admission = await app.submit(session.id, submission_id, text)
    await app.wait_idle()
    return await app.store.get_admission(admission.id, session.id)


async def test_conversation_keeps_prompt_tools_and_message_identity_across_turns(tmp_path):
    model = RecordingModel()
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: model)
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Byte stable prompt")
        first = await send(app, session, "send-1", "hello")
        second = await send(app, session, "send-2", "again")
        assert first.status == second.status == "completed"
        assert {row[:2] for row in model.requests} == {("Byte stable prompt", session.tools)}
        history = await app.store.history(session.id)
        assert [row["role"] for row in history] == ["user", "assistant", "user", "assistant"]
        assert len({row["id"] for row in history}) == 4
        assert len(model.requests[1][2]) == 3
    finally:
        await host.close()


async def test_profile_history_and_checkpoint_stores_are_isolated(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        left = await host.application("left")
        right = await host.application("right")
        session = await left.create_session("same-request", "left prompt")
        await send(left, session, "same-send", "private left")
        with pytest.raises(DomainError, match="not accessible"):
            await right.store.get_session(session.id, "operator")
        other = await right.create_session("same-request", "right prompt")
        assert not await right.store.history(other.id)
        assert left.store.home != right.store.home
        assert (left.scope.home / "checkpoints.db").exists()
    finally:
        await host.close()


async def test_write_waits_and_resumes_after_entire_host_restarts(tmp_path):
    state_dir = tmp_path / "state"
    host = ProjectHost(state_dir, tmp_path)
    app = await host.application("writer")
    session = await app.create_session("create", "Frozen", ("read", "write"))
    pending = await send(app, session, "send", "/write result.txt approved")
    assert pending.status == "waiting_approval"
    assert not (tmp_path / "result.txt").exists()
    event = next(
        row
        for row in await app.store.event_history(session.id)
        if row["type"] == "interaction.open"
    )
    interaction_id = event["payload"]["id"]
    assert event["payload"]["proposal"]["arguments"] == {
        "path": "result.txt",
        "content": "approved",
    }
    await host.close()

    restarted = ProjectHost(state_dir, tmp_path)
    try:
        app = await restarted.application("writer")
        with pytest.raises(DomainError, match="generation"):
            await app.approve(session.id, interaction_id, pending.generation + 1, "approve")
        await app.approve(session.id, interaction_id, pending.generation, "approve")
        await app.wait_idle()
        final = await app.store.get_admission(pending.id, session.id)
        assert final.status == "completed"
        assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "approved"
        assert len(await app.store.history(session.id)) == 4
        assert (
            await app.approve(session.id, interaction_id, pending.generation, "approve")
            == pending.id
        )
        with pytest.raises(DomainError, match="cannot be changed"):
            await app.approve(session.id, interaction_id, pending.generation, "deny")
    finally:
        await restarted.close()


async def test_denied_write_and_outside_read_require_authorization(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("writer")
        session = await app.create_session("create", "Frozen", ("read", "write"))
        pending = await send(app, session, "1", "/write denied.txt no")
        await app.approve(session.id, f"{pending.id}:write-1", pending.generation, "deny")
        await app.wait_idle()
        assert not (tmp_path / "denied.txt").exists()
        outcome = await send(app, session, "2", "/read ../outside.txt")
        assert outcome.status == "waiting_approval"
        approval = (await app.sessions.status(session.id))["approvals"][0]
        assert approval["proposal"]["scope_key"] == "outside:workspace"
        await app.approve(session.id, approval["id"], outcome.generation, "deny")
        await app.wait_idle()
        assert (await app.store.get_admission(outcome.id, session.id)).status == "completed"
    finally:
        await host.close()


async def test_second_owner_is_rejected_and_lock_releases_on_close(tmp_path):
    one = ProjectHost(tmp_path / "state", tmp_path)
    two = ProjectHost(tmp_path / "state", tmp_path)
    try:
        await one.application("profile")
        with pytest.raises(DomainError, match="already owns"):
            await two.application("profile")
        await one.close()
        await two.application("profile")
    finally:
        await one.close()
        await two.close()


async def test_durable_decision_is_resumed_if_process_stopped_before_claim(tmp_path):
    state = tmp_path / "state"
    host = ProjectHost(state, tmp_path)
    app = await host.application("writer")
    session = await app.create_session("create", "Frozen", ("write",))
    pending = await send(app, session, "send", "/write recovered.txt value")
    await app.receipts.decide(
        f"{pending.id}:write-1",
        session.id,
        pending.generation,
        "approve",
    )
    await host.close()
    restarted = ProjectHost(state, tmp_path)
    try:
        app = await restarted.application("writer")
        await app.wait_idle()
        assert (await app.store.get_admission(pending.id, session.id)).status == "completed"
        assert (tmp_path / "recovered.txt").read_text(encoding="utf-8") == "value"
    finally:
        await restarted.close()


async def test_runtime_state_cannot_be_read_by_model_tools(tmp_path):
    state = tmp_path / "state"
    host = ProjectHost(state, tmp_path)
    try:
        app = await host.application("reader")
        (state / "operator.token").write_text("private credential", encoding="utf-8")
        session = await app.create_session("create", "Frozen")
        outcome = await send(app, session, "send", "/read state/operator.token")
        assert "path_denied" in outcome.result
        assert "private credential" not in outcome.result
    finally:
        await host.close()


class TwoWriteModel(DiagnosticModel):
    """One assistant turn requests two approval-gated writes, then completes."""

    async def complete(self, prompt, messages, tools):
        if any(row["role"] == "tool" for row in messages):
            return {"role": "assistant", "content": "done"}
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "w1", "name": "write", "args": {"path": "a.txt", "content": "A"}},
                {"id": "w2", "name": "write", "args": {"path": "b.txt", "content": "B"}},
            ],
        }


async def test_approval_resume_does_not_replay_committed_tool_calls(tmp_path):
    """An interrupt resume replays the whole tools node; committed results must be reused.

    Without the guard, the second resume re-executes the first write (now failing with
    file_conflict) and the changed payload trips message_conflict. Worse, a write with
    a deterministic side effect would run twice.
    """
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: TwoWriteModel())
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Frozen", ("write",), permission_mode="ask")
        admission = await app.submit(session.id, "send", "go")
        await app.wait_idle()
        for _ in range(4):
            pending = await app.store.get_admission(admission.id, session.id)
            if pending.status != "waiting_approval":
                break
            approvals = (await app.sessions.status(session.id))["approvals"]
            assert approvals
            await app.approve(session.id, approvals[0]["id"], pending.generation, "approve")
            await app.wait_idle()
        done = await app.store.get_admission(admission.id, session.id)
        assert done.status == "completed", done.result
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "A"
        assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "B"
        tools = [row for row in await app.store.history(session.id) if row["role"] == "tool"]
        assert len(tools) == 2
        assert all("Tool refused" not in row["content"] for row in tools)
    finally:
        await host.close()


async def test_approval_redelivery_repairs_failed_work_handoff(tmp_path, monkeypatch):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("writer")
        session = await app.create_session("create", "Frozen", ("write",), permission_mode="ask")
        pending = await send(app, session, "send", "/write recovered.txt once")
        key = (await app.store.session_status(session.id, "operator"))["approvals"][0]["id"]
        schedule = app.schedule

        def fail_to_notify(session_id):
            raise RuntimeError("injected after durable approval")

        monkeypatch.setattr(app, "schedule", fail_to_notify)
        with pytest.raises(RuntimeError, match="injected"):
            await app.approve(session.id, key, pending.generation, "approve")
        assert (await app.store.get_admission(pending.id, session.id)).status == "waiting_approval"
        monkeypatch.setattr(app, "schedule", schedule)
        assert await app.approve(session.id, key, pending.generation, "approve") == pending.id
        await app.wait_idle()
        assert (await app.store.get_admission(pending.id, session.id)).status == "completed"
        assert (tmp_path / "recovered.txt").read_text(encoding="utf-8") == "once"
        events = await app.store.event_history(session.id)
        assert sum(event["type"] == "interaction.resolved" for event in events) == 1
        assert sum(event["type"] == "approval.dispatched" for event in events) == 1
    finally:
        await host.close()


async def test_approval_resume_does_not_reset_active_time_budget(tmp_path):
    from sqlalchemy import update

    from deep_harness.application.config import ExecutionSettings
    from deep_harness.infrastructure import schema as s

    host = ProjectHost(
        tmp_path / "state", tmp_path, execution_limits=ExecutionSettings(wall_seconds=5)
    )
    try:
        app = await host.application("writer")
        session = await app.create_session("create", "Frozen", ("write",), permission_mode="ask")
        pending = await send(app, session, "send", "/write must-not-exist.txt value")
        key = (await app.store.session_status(session.id, "operator"))["approvals"][0]["id"]
        async with app.store.transaction() as conn:
            await conn.execute(
                update(s.admissions).where(s.admissions.c.id == pending.id).values(active_seconds=5)
            )
        await app.approve(session.id, key, pending.generation, "approve")
        await app.wait_idle()
        result = await app.store.get_admission(pending.id, session.id)
        assert result.status == "interrupted"
        assert "Cumulative active execution time" in result.result
        assert not (tmp_path / "must-not-exist.txt").exists()
    finally:
        await host.close()

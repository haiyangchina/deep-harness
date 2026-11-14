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
    session = await app.create_session("create", "Frozen", ("read_file", "write_file"))
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
        with pytest.raises(DomainError, match="generation|answered"):
            await app.approve(session.id, interaction_id, pending.generation, "approve")
    finally:
        await restarted.close()


async def test_denied_write_does_not_modify_workspace_and_read_cannot_escape(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("writer")
        session = await app.create_session("create", "Frozen", ("read_file", "write_file"))
        pending = await send(app, session, "1", "/write denied.txt no")
        await app.approve(session.id, f"{pending.id}:write-1", pending.generation, "deny")
        await app.wait_idle()
        assert not (tmp_path / "denied.txt").exists()
        outcome = await send(app, session, "2", "/read ../outside.txt")
        assert outcome.status == "completed"
        assert "path_denied" in outcome.result
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
    session = await app.create_session("create", "Frozen", ("write_file",))
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

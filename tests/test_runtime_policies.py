"""Immutable routes protect queued work and durable approvals across restarts."""

import pytest

from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError
from deep_harness.runtime.models import DiagnosticModel


class NamedModel(DiagnosticModel):
    def __init__(self, name):
        self.identity = {"provider": "diagnostic", "name": name}
        self.calls = 0

    async def complete(self, prompt, messages, tools):
        self.calls += 1
        return await super().complete(prompt, messages, tools)


@pytest.mark.parametrize("decided", [False, True])
async def test_model_change_cannot_execute_queued_work_or_previous_approvals(tmp_path, decided):
    state = tmp_path / "state"
    host = ProjectHost(state, tmp_path, lambda scope: NamedModel("original"))
    app = await host.application("default")
    session = await app.create_session("create", "Frozen", ("write",))
    admission = await app.submit(session.id, "one", "/write result.txt approved")
    await app.wait_idle()
    pending = await app.store.get_admission(admission.id, session.id)
    if decided:
        await app.receipts.decide(
            f"{pending.id}:write-1", session.id, pending.generation, "approve"
        )
    queued = await app.store.admit(session, principal="operator", submission_id="two", text="later")
    await host.close()
    changed_model = NamedModel("changed")
    changed = ProjectHost(state, tmp_path, lambda scope: changed_model)
    try:
        app = await changed.application("default")
        await app.wait_idle()
        status = await app.sessions.status(session.id)
        assert not status["runtime"]["compatible"]
        assert status["runtime"]["binding"]["snapshot"]["model"]["name"] == "original"
        with pytest.raises(DomainError, match="runtime changed"):
            await app.submit(session.id, "three", "must refuse")
        with pytest.raises(DomainError, match="runtime changed"):
            await app.approve(session.id, f"{pending.id}:write-1", pending.generation, "approve")
        assert changed_model.calls == 0
        assert (await app.store.get_admission(queued.id, session.id)).status == "queued"
        assert (await app.store.get_admission(pending.id, session.id)).status == "waiting_approval"
        assert not (tmp_path / "result.txt").exists()
    finally:
        await changed.close()
    restored = ProjectHost(state, tmp_path, lambda scope: NamedModel("original"))
    try:
        app = await restored.application("default")
        if not decided:
            await app.approve(session.id, f"{pending.id}:write-1", pending.generation, "approve")
        await app.wait_idle()
        assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "approved"
        assert (await app.store.get_admission(queued.id, session.id)).status == "completed"
    finally:
        await restored.close()

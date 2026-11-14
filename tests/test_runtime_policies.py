"""Immutable routes protect queued work and durable approvals across restarts."""

import httpx
import pytest

from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError
from deep_harness.runtime.models import DiagnosticModel
from deep_harness.transports.api import create_api


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
    session = await app.create_session("create", "Frozen", ("write_file",))
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
        with pytest.raises(DomainError, match="cannot be overwritten"):
            await app.bind_runtime(session.id, status["runtime"]["current_fingerprint"])
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


async def test_legacy_binding_and_fork_are_explicit_authenticated_and_idempotent(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("default")
        legacy = await app.store.create_session(
            principal="operator", request_id="legacy", prompt="Frozen", tools=()
        )
        with pytest.raises(DomainError, match="explicitly bind"):
            await app.submit(legacy.id, "refused", "no implicit adoption")
        token = "local-test-operator-token-more-than-32-chars"
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_api(host, token)), base_url="http://local"
        ) as client:
            route = f"/profiles/default/sessions/{legacy.id}"
            assert (
                await client.post(route + "/forks", json={"request_id": "branch"})
            ).status_code == 401
            status = (await client.get(route + "/status", headers=headers)).json()
            wrong = await client.post(
                route + "/runtime-binding", headers=headers, json={"fingerprint": "0" * 64}
            )
            assert wrong.status_code == 409
            bound = await client.post(
                route + "/runtime-binding",
                headers=headers,
                json={"fingerprint": status["runtime"]["current_fingerprint"]},
            )
            assert bound.status_code == 200
            assert bound.json()["runtime"]["compatible"]
            first = await client.post(
                route + "/forks", headers=headers, json={"request_id": "branch"}
            )
            second = await client.post(
                route + "/forks", headers=headers, json={"request_id": "branch"}
            )
            assert first.json()["id"] == second.json()["id"] != legacy.id
            foreign = await client.post(
                f"/profiles/other/sessions/{legacy.id}/forks",
                headers=headers,
                json={"request_id": "branch"},
            )
            assert foreign.status_code == 404
        admission = await app.submit(legacy.id, "allowed", "adopted")
        await app.wait_idle()
        assert (await app.store.get_admission(admission.id, legacy.id)).status == "completed"
        assert (await app.create_session("legacy", "Frozen", ())).id == legacy.id
    finally:
        await host.close()
